"""Rust coverage backend: parses lcov-format reports (statement/function level).

Rust's coverage toolchain is natively instrumented: `cargo llvm-cov --lcov
--output-path lcov.info` (or `cargo tarpaulin --out Lcov`) produces an lcov
report with zero --coverage-style compile flags. The lcov text format is simple
and stable:

    TN:
    SF:src/main.rs
    FN:10,main                     # function definition line
    FNDA:1,main                    # function execution count (hit data)
    FNF:2 / FNH:1                  # functions found / hit (redundant; recomputed)
    DA:10,1                        # line coverage (line, count)
    BRDA:13,0,0,1                  # branch coverage (line, block, branch, taken)
    end_of_record

This backend turns lcov records into the language-neutral CoverageReport model
(functions / lines / branches), mirroring go_cover.py's role for Go.

lcov carries no function end_line: it is approximated as the next function's
start_line - 1 (or the file's last covered line), which is sufficient for the
report's line-range coloring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .gcov import BranchCov, CoverageReport, FileCov, FunctionCov


@dataclass
class LcovFile:
    """Raw lcov records for one source file (one SF...end_of_record block)."""
    file: str                                        # path as recorded in SF
    fn_defs: dict[str, int] = field(default_factory=dict)    # FN: name -> def line
    fn_hits: dict[str, int] = field(default_factory=dict)    # FNDA: name -> count
    line_counts: dict[int, int] = field(default_factory=dict)  # DA: line -> count
    # BRDA: (line, block, branch) -> taken count
    branches: dict[tuple[int, int, int], int] = field(default_factory=dict)


def parse_lcov(path: Path | str) -> list[LcovFile]:
    """Parse an lcov report into per-file records (missing/corrupt file -> [])."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    files: list[LcovFile] = []
    cur: LcovFile | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            cur = LcovFile(file=line[3:])
            continue
        if cur is None:
            continue
        if line == "end_of_record":
            files.append(cur)
            cur = None
            continue
        if line.startswith("FN:"):
            body = line[3:]
            if "," in body:
                ln_s, name = body.split(",", 1)
                try:
                    cur.fn_defs[name.strip()] = int(ln_s)
                except ValueError:
                    continue
        elif line.startswith("FNDA:"):
            body = line[5:]
            if "," in body:
                cnt_s, name = body.split(",", 1)
                try:
                    cur.fn_hits[name.strip()] = int(cnt_s)
                except ValueError:
                    continue
        elif line.startswith("DA:"):
            body = line[3:]
            if "," in body:
                ln_s, cnt_s = body.split(",", 1)
                try:
                    ln, cnt = int(ln_s), int(cnt_s)
                except ValueError:
                    continue
                cur.line_counts[ln] = max(cur.line_counts.get(ln, 0), cnt)
        elif line.startswith("BRDA:"):
            body = line[5:]
            parts = body.split(",")
            if len(parts) >= 4:
                try:
                    ln = int(parts[0])
                    taken = 0 if parts[3] == "-" else int(parts[3])
                except ValueError:
                    continue
                cur.branches[(ln, int(parts[1]), int(parts[2]))] = taken
    if cur is not None:  # missing end_of_record at EOF: keep the trailing block
        files.append(cur)
    return files


def _resolve_rel(source_root: Path, sf: str) -> str | None:
    """Normalize an lcov SF path to a source-root-relative posix path."""
    p = Path(sf)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(source_root.resolve()).as_posix()
        except ValueError:
            return None
    if (source_root / p).exists():
        return p.as_posix()
    # lcov from `cargo llvm-cov` may record workspace-member-relative paths
    if len(p.parts) > 1 and (source_root / Path(*p.parts[1:])).exists():
        return Path(*p.parts[1:]).as_posix()
    return p.as_posix()


def collect_rust(
    source_root: Path,
    lcov_path: Path | str,
    *,
    include_filter=None,
    exclude_filter=None,
) -> CoverageReport:
    """Build a CoverageReport from an lcov report (cargo llvm-cov / tarpaulin).

    - function coverage: FN/FNDA records (hit iff FNDA > 0)
    - line coverage: DA records
    - branch coverage: BRDA records (hit iff taken > 0 / taken != "-")
    """
    from .globutil import glob_matches

    report = CoverageReport(created_at=datetime.now().isoformat(timespec="seconds"))
    for lf in parse_lcov(lcov_path):
        rel = _resolve_rel(source_root, lf.file)
        if rel is None:
            continue
        if include_filter and not glob_matches(rel, include_filter):
            continue
        if exclude_filter and glob_matches(rel, exclude_filter):
            continue

        fc = FileCov(file=rel)
        fc.line_counts = dict(lf.line_counts)
        fc.lines_total = len(lf.line_counts)
        fc.lines_hit = sum(1 for c in lf.line_counts.values() if c > 0)

        # end_line approximation: next function's start_line - 1, else last covered line
        last_line = max(lf.line_counts) if lf.line_counts else 0
        sorted_defs = sorted(lf.fn_defs.items(), key=lambda kv: kv[1])
        for idx, (name, start_line) in enumerate(sorted_defs):
            if idx + 1 < len(sorted_defs):
                end_line = sorted_defs[idx + 1][1] - 1
            else:
                end_line = max(start_line, last_line)
            count = lf.fn_hits.get(name, 0)
            fc.functions[name] = FunctionCov(
                file=rel, name=name,
                start_line=start_line, end_line=end_line,
                execution_count=count,
                blocks=0, blocks_executed=0,
            )

        for (ln, _block, _branch), taken in lf.branches.items():
            fn_name = ""
            for name, fcov in fc.functions.items():
                if fcov.start_line <= ln <= fcov.end_line:
                    fn_name = name
                    break
            fc.branches.append(BranchCov(
                file=rel, line=ln, function=fn_name,
                count=taken, fallthrough=False, throw=False,
            ))

        report.files[rel] = fc
    return report
