"""Changed lines -> changed functions: CodeGraph line-range reverse-lookup attribution (design doc §3.2).

Design validated on a real project. Core principle: **the function name is the result of a
reverse lookup from line numbers, not a direct diff output** -- it does not trust `git diff`
hunk-header function-name hints (which may refer to the previous function or drop the class
qualification); they are only used for post-hoc cross-validation.

Three resolutions:
- codegraph_range: CodeGraph line-range hit and consistent with the hunk-header hint (or no hint)
- conflict: CodeGraph result and the hunk-header hint completely disagree; at least one is wrong
  -- excluded from the loop-gate denominator, forwarded to manual review
- (files that can't be attributed go to `unresolved_files`, likewise not guessed)

Deliberately no "fall back to regex when the CodeGraph index is missing" -- falling back would
give up this module's entire value. When the index is missing it raises `CodeGraphNotAvailable`
so the caller can build the index.
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
    """A changed function and its attribution trustworthiness."""

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
        """(file, qualified_name) -- never split("::"); the qualified name is kept as-is."""
        return (self.file, self.qualified_name)

    def as_target(self) -> tuple[str, str]:
        """Convert to (file, bare_name) form for the coverage/batching chain (existing coverage
        artifacts store bare names; qualified_name is only for manual review/dedup key)."""
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
    """The complete result of one diff extraction."""

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
    """Extract candidate function names from hunk-header context (cross-validation only, not a result)."""
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
    """Deterministically extract changed functions.

    Flow:
     1. `git diff -U0` -> per-file changed line numbers (mrdiff.collect_file_diffs)
     2. CodeGraph line-range reverse lookup -> qualified functions
     3. hunk-header cross-validation: results completely disagree with hints -> mark conflict
     4. files whose changed lines fall in no indexed function -> unresolved_files

    Raises:
        callgraph.CodeGraphNotAvailable: index missing.
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
