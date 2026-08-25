"""diff fetching: local git channel (fully local, zero external-platform dependency).

Only does one thing -- "extract which lines of which files changed"; it does **not guess
function names**. Function attribution is `diffextract.py`'s job, done via CodeGraph
line-range reverse-lookup; this module only produces a minimal-trust form:
`FileDiff(file, changed_lines, hunk_hints)`.

`hunk_hints` keeps the function-signature text that appears in hunk headers, solely for
`diffextract.py`'s cross-validation (when CodeGraph results completely disagree with the
hunk header, at least one side is untrustworthy and should degrade to a conflict rather than
half-trusting each) -- never used directly as the function-name result.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MAX_DIFF_CHARS = 50000

#: git diff hunk header: `@@ -a,b +c,d @@ <context, possibly a function signature>`
_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@(?:\s+(.*))?$")


@dataclass
class FileDiff:
    """A single file's changed-line set (the minimal-trust form of diff extraction)."""

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
    """Run `git diff -U0` to collect each file's changed line numbers (1-based, consistent with CodeGraph).

    ⚠️ `--relative` is required: `source_path` is often a subdir of the git repo (e.g. the
    repo root is one level up); without it, git outputs paths relative to the repo root, which
    won't match CodeGraph's `nodes.file_path` (relative to source_path), invalidating all
    downstream queries -- a lesson verified on a real repo.
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
                # pure-deletion hunk: no corresponding line in the new file; take the deletion
                # position as the "affected line"
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
