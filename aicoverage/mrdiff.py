"""diff 获取：本地 git 通道（完全本地，零外部平台依赖）。

只做"提取改了哪些文件的哪些行"这一件事——**不猜函数名**。函数归因是
`diffextract.py` 的职责，靠 CodeGraph 行区间反查完成，这里只产出最小可信
形态：`FileDiff(file, changed_lines, hunk_hints)`。

`hunk_hints` 保留 hunk header 里出现的函数签名文本，仅供 `diffextract.py`
做"交叉校验"用（CodeGraph 结果与 hunk header 完全对不上时，说明至少一方
不可信，应降级为 conflict 而不是各信一半）——绝不用它直接当函数名结果。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MAX_DIFF_CHARS = 50000

#: git diff hunk header：`@@ -a,b +c,d @@ <上下文，可能是函数签名>`
_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@(?:\s+(.*))?$")


@dataclass
class FileDiff:
    """单个文件的改动行集合（diff 提取的最小可信形态）。"""

    file: str
    changed_lines: list[int] = field(default_factory=list)
    hunk_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "changed_lines": self.changed_lines,
                "hunk_hints": self.hunk_hints}


def collect_file_diffs(
    source_path: Path, base_ref: str, head_ref: str, *,
    include_globs: list[str] | None = None, timeout: int = 60,
) -> tuple[list[FileDiff], str]:
    """跑 `git diff -U0` 收集每个文件的改动行号（1-based，与 CodeGraph 一致）。

    ⚠️ `--relative` 必须加：`source_path` 常是 git 仓库的子目录（如仓库根在
    上一层），不加时 git 输出的路径相对仓库根，与 CodeGraph `nodes.file_path`
    （相对 source_path）不匹配，后续所有查询全部失效——真实仓库验证过的教训。
    """
    globs = include_globs or ["*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hpp"]
    try:
        result = subprocess.run(
            ["git", "diff", "-U0", "--relative", f"{base_ref}..{head_ref}", "--", *globs],
            capture_output=True, cwd=str(source_path), timeout=timeout,
        )
        diff_text = result.stdout.decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        raise RuntimeError(f"git diff 失败: {e}") from e

    if not diff_text.strip():
        return [], ""

    by_file: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            fpath = line[6:].strip()
            current = by_file.setdefault(fpath, FileDiff(file=fpath))
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@") and current is not None:
            m = _HUNK_RE.match(line)
            if not m:
                continue
            new_start = int(m.group(1))
            new_count = int(m.group(2)) if m.group(2) is not None else 1
            if new_count == 0:
                # 纯删除 hunk：新文件里没有对应行，取删除位置那一行作为"受影响行"
                current.changed_lines.append(new_start)
            else:
                current.changed_lines.extend(range(new_start, new_start + new_count))
            hint = (m.group(3) or "").strip()
            if hint:
                current.hunk_hints.append(hint)
            continue

    for fd in by_file.values():
        fd.changed_lines = sorted(set(fd.changed_lines))

    truncated = diff_text
    if len(diff_text) > _MAX_DIFF_CHARS:
        truncated = diff_text[:_MAX_DIFF_CHARS] + f"\n... [diff truncated, total {len(diff_text)} chars]"
    return list(by_file.values()), truncated
