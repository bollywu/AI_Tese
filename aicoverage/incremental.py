"""Incremental coverage: narrow a full `CoverageReport` into a subset view containing
only the changed-function set.

Corresponds to design doc §3.4 -- function-level scope narrowing (not line-precise
diff coverage):

    incremental func_pct = share of changed functions whose whole body was executed
    incremental cond_pct = branch coverage share within the changed functions' whole bodies

This is not re-collecting coverage; it re-aggregates an already-collected
`CoverageReport` into a subset view, reusing the existing
`FileCov`/`FunctionCov`/`BranchCov` structures and all existing `CoverageReport`
properties (`func_pct`/`cond_pct`/`delta()`/`uncovered_functions()` etc.), adding
no new decision logic -- loop.py's threshold checks can be reused as-is, just fed
the subset computed here.
"""
from __future__ import annotations

from .gcov import BranchCov, CoverageReport, FileCov

#: Minimal representation of a changed function: (file, bare_name), matching the
#: output format of diffextract.ChangedFunction.as_target() and the keys of the
#: function dict in gcov.py (demangled/bare name).
TargetFunctions = list[tuple[str, str]]


def _group_by_file(target_functions: TargetFunctions) -> dict[str, set[str]]:
    wanted: dict[str, set[str]] = {}
    for f, fn in target_functions:
        wanted.setdefault(f, set()).add(fn)
    return wanted


def scope_report(full: CoverageReport, target_functions: TargetFunctions) -> CoverageReport:
    """Filter the full `CoverageReport` into a subset view containing only `target_functions`.

    Denominator-narrowing rules:
    - functions: exact (file, name) match.
    - branches: `BranchCov.function` is that branch's host function name; narrow to the
      same set of selected functions.
    - lines: keep only lines within the selected functions' `[start_line, end_line]`
      ranges (for the HTML report's "show changed functions only" line-coloring mode).

    Targets absent from `full` (typo / that translation unit not instrumented /
    function already deleted) are **not** silently ignored nor treated as 0% --
    they are surfaced separately via `missing_targets()`; the caller must state them
    explicitly in the report and must not conflate them with "present but unexecuted".
    """
    wanted = _group_by_file(target_functions)
    scoped = CoverageReport(created_at=full.created_at)

    for file, fc in full.files.items():
        names = wanted.get(file)
        if not names:
            continue
        new_fc = FileCov(file=file)
        for name, func in fc.functions.items():
            if name in names:
                new_fc.functions[name] = func
        if not new_fc.functions:
            continue

        new_fc.branches = [b for b in fc.branches if b.function in names]

        ranges = [(f2.start_line, f2.end_line) for f2 in new_fc.functions.values()]
        new_fc.line_counts = {
            ln: c for ln, c in fc.line_counts.items()
            if any(s <= ln <= e for s, e in ranges)
        }
        new_fc.lines_total = len(new_fc.line_counts)
        new_fc.lines_hit = sum(1 for c in new_fc.line_counts.values() if c > 0)
        scoped.files[file] = new_fc

    return scoped


def missing_targets(full: CoverageReport, target_functions: TargetFunctions) -> list[tuple[str, str]]:
    """(file, name) pairs in target_functions that do not appear in `full`'s coverage data.

    Possible causes: function/file-name typo, that translation unit not part of the
    instrumented build, or the function just deleted but diff extraction lagged.
    The caller (report generation) must list these separately -- neither silently
    count them in the denominator nor treat them as "0% coverage" (which would be
    conflated with "truly present but unexecuted" and mislead the "why not met" analysis).
    """
    existing = {(f, name) for f, fc in full.files.items() for name in fc.functions}
    return [(f, fn) for f, fn in target_functions if (f, fn) not in existing]


def incremental_delta(
    before: CoverageReport, after: CoverageReport, target_functions: TargetFunctions,
) -> dict:
    """Delta relative to the previous round (narrow to target_functions, then reuse
    `CoverageReport.delta()`; no new delta logic invented)."""
    scoped_before = scope_report(before, target_functions)
    scoped_after = scope_report(after, target_functions)
    d = scoped_after.delta(scoped_before)
    d.update({
        "scope_func_total": scoped_after.func_total,
        "scope_func_hit": scoped_after.func_hit,
        "scope_func_pct": scoped_after.func_pct,
        "scope_branch_total": scoped_after.branch_total,
        "scope_branch_hit": scoped_after.branch_hit,
        "scope_cond_pct": scoped_after.cond_pct,
    })
    return d


def scope_threshold_met(
    report: CoverageReport, target_functions: TargetFunctions,
    func_target: float, cond_target: float,
) -> tuple[bool, CoverageReport]:
    """Judge whether the narrowed scope meets the threshold; returns (met, scope_report).

    loop.py's existing threshold check (`func_pct >= func_target and cond_pct >=
    cond_target`) is reused as-is, only the judged object becomes the `scope_report`
    returned here.
    """
    scoped = scope_report(report, target_functions)
    met = scoped.func_pct >= func_target and scoped.cond_pct >= cond_target
    return met, scoped
