"""变更行 → 变更函数：CodeGraph 行区间反查归因（改造计划文档 §3.2）。

设计经真实项目验证。核心原则：**函数名是从行号反查出来的结果，不是 diff 的
直接输出**——不相信 `git diff` hunk header 的函数名提示（它可能对应上一个
函数、可能丢类限定名），只用它做事后交叉校验。

三种 resolution：
- codegraph_range：CodeGraph 行区间命中且与 hunk header 提示一致（或无提示）
- conflict：CodeGraph 结果与 hunk header 提示完全对不上，两者至少一个错——
  不入闭环门禁分母，转人工复核
- （无法归因的文件进 `unresolved_files`，同样不猜测）

刻意不做"CodeGraph 索引缺失时退化到正则"——退化等于放弃本模块的全部价值。
索引缺失时直接抛 `CodeGraphNotAvailable`，让调用方去建索引。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import callgraph
from .mrdiff import FileDiff, collect_file_diffs

RESOLUTION_CODEGRAPH = "codegraph_range"
RESOLUTION_CONFLICT = "conflict"


@dataclass
class ChangedFunction:
    """一个变更函数及其归因可信度。"""

    file: str
    qualified_name: str
    bare_name: str
    changed_lines: list[int]
    start_line: int = 0
    end_line: int = 0
    signature: str = ""
    resolution: str = RESOLUTION_CODEGRAPH
    note: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """(file, qualified_name)——绝不 split("::")，限定名原样保留。"""
        return (self.file, self.qualified_name)

    def as_target(self) -> tuple[str, str]:
        """转成 (file, bare_name) 形态，供覆盖率/分批链路使用（现有覆盖率
        产物存的是裸函数名，qualified_name 只用于人工审阅/去重 key）。"""
        return (self.file, self.bare_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file, "qualified_name": self.qualified_name,
            "bare_name": self.bare_name, "changed_lines": self.changed_lines,
            "start_line": self.start_line, "end_line": self.end_line,
            "signature": self.signature, "resolution": self.resolution,
            "note": self.note,
        }


@dataclass
class DiffExtraction:
    """一次 diff 提取的完整结果。"""

    base_ref: str
    head_ref: str
    file_diffs: list[FileDiff] = field(default_factory=list)
    functions: list[ChangedFunction] = field(default_factory=list)
    unresolved_files: list[str] = field(default_factory=list)
    diff_text: str = ""
    index_sha: str = ""

    @property
    def trusted_functions(self) -> list[ChangedFunction]:
        return [f for f in self.functions if f.resolution == RESOLUTION_CODEGRAPH]

    @property
    def conflict_functions(self) -> list[ChangedFunction]:
        return [f for f in self.functions if f.resolution != RESOLUTION_CODEGRAPH]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ref": self.base_ref, "head_ref": self.head_ref,
            "index_sha": self.index_sha,
            "file_diffs": [fd.to_dict() for fd in self.file_diffs],
            "functions": [f.to_dict() for f in self.functions],
            "unresolved_files": self.unresolved_files,
            "counts": {
                "files": len(self.file_diffs), "functions": len(self.functions),
                "trusted": len(self.trusted_functions),
                "conflict": len(self.conflict_functions),
                "unresolved_files": len(self.unresolved_files),
            },
        }


def _hint_bare_names(hints: list[str]) -> set[str]:
    """从 hunk header 上下文抽出可能的函数名（仅用于交叉校验，不作为结果）。"""
    out: set[str] = set()
    for h in hints:
        paren = h.find("(")
        if paren <= 0:
            continue
        parts = h[:paren].strip().split()
        if not parts:
            continue
        bare = parts[-1].split("::")[-1].lstrip("*&").rstrip("*&")
        if bare and not bare.startswith("//"):
            out.add(bare)
    return out


def extract(
    source_path: Path, base_ref: str, head_ref: str, *,
    include_globs: list[str] | None = None, index_dir: str = ".codegraph",
) -> DiffExtraction:
    """确定性提取变更函数。

    流程：
      1. `git diff -U0` → 每文件改动行号（mrdiff.collect_file_diffs）
      2. CodeGraph 行区间反查 → 带限定名的函数
      3. hunk header 交叉校验：结果与提示完全对不上 → 标记 conflict
      4. 改动行不落在任何已索引函数内的文件 → unresolved_files

    Raises:
        callgraph.CodeGraphNotAvailable: 索引不存在。
    """
    file_diffs, diff_text = collect_file_diffs(
        source_path, base_ref, head_ref, include_globs=include_globs)
    ex = DiffExtraction(
        base_ref=base_ref, head_ref=head_ref, file_diffs=file_diffs,
        diff_text=diff_text, index_sha=callgraph.index_sha(source_path, index_dir),
    )
    if not file_diffs:
        return ex

    for fd in file_diffs:
        ranges = callgraph.functions_covering_lines(
            source_path, fd.file, fd.changed_lines, index_dir=index_dir)
        if not ranges:
            ex.unresolved_files.append(fd.file)
            continue

        hints = _hint_bare_names(fd.hunk_hints)
        hit_bare = {r.name.split("::")[-1] for r in ranges}
        cross_ok = (not hints) or bool(hints & hit_bare)

        for r in ranges:
            lines_in = [ln for ln in fd.changed_lines if r.start_line <= ln <= r.end_line]
            ex.functions.append(ChangedFunction(
                file=fd.file, qualified_name=r.qualified_name,
                bare_name=r.name.split("::")[-1], changed_lines=lines_in,
                start_line=r.start_line, end_line=r.end_line, signature=r.signature,
                resolution=RESOLUTION_CODEGRAPH if cross_ok else RESOLUTION_CONFLICT,
                note="" if cross_ok else (
                    f"hunk header 提示 {sorted(hints)} 与 CodeGraph 行区间反查结果 "
                    f"{sorted(hit_bare)} 不一致（索引区间 [{r.start_line}-{r.end_line}] "
                    f"跨 {r.end_line - r.start_line + 1} 行，可能过宽），"
                    "需人工确认宿主函数（不入闭环门禁分母）"
                ),
            ))
    return ex
