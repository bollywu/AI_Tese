"""C/C++ 源码静态解析：函数清单提取（供 analyzer 阶段 / 报告展示）。

注意：覆盖率闭环的**权威**函数清单来自 gcov JSON（gcov -i 对每个插桩编译单元
都会枚举全部函数，含未执行者，精确到行号），本模块只是构建前的轻量静态扫描，
用于需求解析阶段给 analyzer-agent 提供"项目里有哪些函数"的全景。

实现：括号深度扫描 + 行首启发式匹配，不依赖 ctags（保持零外部工具依赖；
ctags 存在时优先用 ctags 提升准确度）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 函数定义启发式：[修饰/返回类型] name(args...) { 开头
# 排除控制流关键字；C++ 的 ::（类外定义）与运算符重载简单放行。
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "else", "do",
    "sizeof", "defined", "case", "default",
}

_FUNC_HEAD = re.compile(
    r"^(?P<sig>(?:[A-Za-z_][\w:]*(?:<[^;{}]*>)?\s+)+"  # 返回类型（含模板）
    r"|(?:static|inline|extern|virtual|explicit)\s+)?"   # 或仅有修饰符开头
    r"(?P<name>[A-Za-z_~][\w:<>~]*)\s*"                  # 函数名（含 Class::method）
    r"\((?:[^;{}()]|\([^()]*\))*\)\s*"                    # 参数表（允许嵌套一层括号）
    r"(?:const\s*)?(?:noexcept\s*)?(?:->\s*[\w:<>&*\s]+)?$"  # 尾置返回/noexcept
)

_LINE_COMMENT = re.compile(r"//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass
class FunctionInfo:
    file: str          # 相对源码根的路径
    name: str          # 函数名（C++ 含类限定）
    line: int          # 定义起始行
    signature: str     # 单行签名摘要

    def to_dict(self) -> dict:
        return {"file": self.file, "name": self.name, "line": self.line,
                "signature": self.signature}


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT.sub(" ", text)
    text = "\n".join(_LINE_COMMENT.sub("", ln) for ln in text.splitlines())
    return text


def extract_functions_source(path: Path, source_root: Path) -> list[FunctionInfo]:
    """单个 .c/.cpp 文件的函数提取（括号深度启发式）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = _strip_comments(text)
    rel = path.relative_to(source_root).as_posix()
    results: list[FunctionInfo] = []
    depth = 0
    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if depth == 0 and stripped and not stripped.startswith("#"):
            m = _FUNC_HEAD.match(stripped)
            if m:
                name = m.group("name")
                # 函数体必须在后续若干行内出现 "{"（避免把声明/调用当定义）
                lookahead = "\n".join(lines[i - 1: i + 4])
                if "{" in lookahead and name not in _KEYWORDS and len(name) > 1:
                    results.append(FunctionInfo(
                        file=rel, name=name, line=i,
                        signature=stripped[:120],
                    ))
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
    return results


def extract_functions_ctags(source_root: Path, files: list[Path]) -> list[FunctionInfo] | None:
    """ctags 可用时优先走 ctags（准确度更高）；不可用返回 None 走正则。"""
    ctags = shutil.which("ctags")
    if not ctags:
        return None
    try:
        proc = subprocess.run(
            [ctags, "--output-format=json", "-f", "-",
             "--kinds-c=f", "--kinds-c++=f", "--fields=+n",
             *[str(p) for p in files]],
            capture_output=True, text=True, timeout=120, cwd=source_root,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    results: list[FunctionInfo] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            import json
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = entry.get("name", "")
        path = entry.get("path", "")
        line_no = entry.get("line", 0)
        if not name or not path or not line_no:
            continue
        try:
            rel = Path(path).resolve().relative_to(source_root).as_posix()
        except ValueError:
            continue
        signature = (entry.get("signature") or name)[:120]
        results.append(FunctionInfo(file=rel, name=name, line=int(line_no),
                                    signature=signature))
    return results or None


def function_inventory(files: list[Path], source_root: Path) -> list[FunctionInfo]:
    """全量函数清单：ctags 优先，正则兜底。"""
    if not files:
        return []
    via_ctags = extract_functions_ctags(source_root, files)
    if via_ctags is not None:
        return via_ctags
    results: list[FunctionInfo] = []
    for p in files:
        results.extend(extract_functions_source(p, source_root))
    return results
