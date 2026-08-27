"""Go coverage backend: parses `go test -coverprofile` output (statement-level).

Go's toolchain is natively instrumented: `go test -coverprofile=cover.out` runs
tests and records statement coverage with zero compile-time instrumentation
(unlike C/C++ gcov). The coverprofile format is simple text:

    mode: set
    <pkg>/<file>.go:<startLine>.<startCol>,<endLine>.<endCol> <numStmt> <count>

Each line is one statement block. `count` > 0 means the block was executed at
least once. This backend turns those statement records into the language-neutral
`CoverageReport` model (functions / lines / branches).

Function coverage is NOT present in the profile, so it is reconstructed by:
  1. statically extracting every `func` definition (name + body line range)
  2. a function is HIT iff any statement inside its body has count > 0
     (execution_count = max statement count in the body, as an approximation)

Branch coverage (cond) is derived from conditional statements: a Go `if`/`for`/
`switch` statement normally maps to a 2-way branch profile when it appears as
its own statement block with count 0 (the false path). This mirrors the C/gcov
"condition coverage" semantics used elsewhere in AIcoverage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .gcov import CoverageReport, FileCov, FunctionCov


# ── coverprofile parsing ──────────────────────────────────────────────

# e.g.  verify/mathx/calc.go:3.24,4.11 1 1
_COVER_LINE = re.compile(
    r"^(?P<file>.+\.go):(?P<sl>\d+)\.(?P<sc>\d+),(?P<el>\d+)\.(?P<ec>\d+) "
    r"(?P<num>\d+) (?P<count>\d+)$"
)


@dataclass
class StmtBlock:
    file: str            # package-relative .go path (as in the profile)
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    num_stmt: int
    count: int

    @property
    def hit(self) -> bool:
        return self.count > 0


@dataclass
class GoFunc:
    """A statically-extracted Go function definition (used for function-level hit)."""
    file: str            # path relative to source root
    name: str            # full name incl. receiver, e.g. "(*Server).Start"
    start_line: int
    end_line: int        # body closing brace line (inclusive)


def parse_coverprofile(path: Path | str) -> list[StmtBlock]:
    """Parse a `go test -coverprofile` file into statement blocks (mode line skipped)."""
    blocks: list[StmtBlock] = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return blocks
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("mode:"):
            continue
        m = _COVER_LINE.match(line)
        if not m:
            continue
        blocks.append(StmtBlock(
            file=m.group("file"),
            start_line=int(m.group("sl")), start_col=int(m.group("sc")),
            end_line=int(m.group("el")), end_col=int(m.group("ec")),
            num_stmt=int(m.group("num")), count=int(m.group("count")),
        ))
    return blocks


# ── Go function extraction (regex + brace-depth) ─────────────────────
#
# Handles:
#   func Add(a, b int) int { ... }                     plain function
#   func (s *Server) Start(ctx) error { ... }          method with receiver
#   func Map[K comparable](...)... { ... }             generic
# Skips:
#   anonymous funcs not at statement start (name group requires identifier)
#   interface method declarations (no `{` body)

_FUNC_DEF = re.compile(
    r"^func\s+"                       # func keyword at line start
    r"(?P<recv>\([^)]*\)\s+)?"        # optional receiver
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?)\s*"  # function name (+type params)
)


def _find_body(lines: list[str], start: int) -> tuple[int, int] | None:
    """From a `func` definition line, find the body span [start_line, end_line].

    The signature may span multiple lines before the `{`. Returns (body_start,
    body_end) 0-based, or None if no body found. The line range is inclusive and
    covers the whole function from the `func` line to the closing brace.
    """
    n = len(lines)
    # Join from the func line onward until we hit a `{` (the body opener).
    idx = start
    joined = lines[idx]
    while "{" not in joined:
        idx += 1
        if idx >= n:
            return None
        joined += lines[idx]
    # The body starts on line `idx`. Now brace-match to find the closing `}`.
    # Everything from the `{` onward contributes to depth.
    open_pos = joined.index("{")
    depth = 0
    body_end = idx
    remaining = joined[open_pos:]
    while True:
        depth += remaining.count("{") - remaining.count("}")
        if depth <= 0:
            break
        body_end += 1
        if body_end >= n:
            return None
        remaining = lines[body_end]
    return start, body_end


def extract_go_functions(path: Path, source_root: Path) -> list[GoFunc]:
    """Extract Go function definitions from a single .go file (name + body line range)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    try:
        rel = path.relative_to(source_root).as_posix()
    except ValueError:
        rel = path.as_posix()
    funcs: list[GoFunc] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        # A Go function definition starts with `func ` at the beginning of a statement.
        if stripped.startswith("func ") or stripped == "func":
            m = _FUNC_DEF.match(stripped)
            if not m:
                i += 1
                continue
            span = _find_body(lines, i)
            if span is None:
                i += 1
                continue
            start_line, end_line = span
            recv = (m.group("recv") or "").strip()
            name = m.group("name")
            full_name = f"{recv} {name}".strip() if recv else name
            funcs.append(GoFunc(
                file=rel, name=full_name,
                start_line=start_line + 1, end_line=end_line + 1,
            ))
            i = end_line + 1
            continue
        i += 1
    return funcs


# ── Branch / line aggregation ─────────────────────────────────────────

def _aggregate_by_file(blocks: list[StmtBlock], source_root: Path,
                       include_filter, exclude_filter) -> dict[str, list[StmtBlock]]:
    """Group statement blocks by normalized source-relative path, applying filters.

    The profile uses package-relative paths (e.g. `verify/mathx/calc.go`). We
    resolve them against source_root so the report's `file` field is consistent
    with the C/C++ backend (source-root-relative).
    """
    from .globutil import glob_matches

    by_file: dict[str, list[StmtBlock]] = {}
    for b in blocks:
        rel = _resolve_source_path(source_root, b.file)
        if rel is None:
            continue
        if include_filter and not glob_matches(rel, include_filter):
            continue
        if exclude_filter and glob_matches(rel, exclude_filter):
            continue
        by_file.setdefault(rel, []).append(b)
    return by_file


def _resolve_source_path(source_root: Path, profile_path: str) -> str | None:
    """Resolve a coverprofile file path to a source-root-relative path.

    The profile records the package's import path (e.g. `verify/mathx/calc.go`),
    which is `module + / + rel-to-module-root`. The module name is not a real
    filesystem segment, so we try the most likely candidates and pick the first
    one that actually exists on disk under source_root:
      1. as-is (profile path == source-relative path)
      2. with the first segment (module name) stripped
      3. basename only (as a last resort)
    """
    p = Path(profile_path)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(source_root / p)
        if len(p.parts) > 1:
            candidates.append(source_root / Path(*p.parts[1:]))
        candidates.append(source_root / p.name)
    for cand in candidates:
        try:
            rel = cand.resolve().relative_to(source_root.resolve())
            if cand.exists():
                return rel.as_posix()
        except ValueError:
            continue
    # Fallback: even if not on disk, prefer the module-stripped form for consistency
    if len(p.parts) > 1:
        return Path(*p.parts[1:]).as_posix()
    return p.as_posix()


def collect_go(
    source_root: Path,
    coverprofile: Path | str,
    *,
    include_filter=None,
    exclude_filter=None,
    out_dir: Path | None = None,
) -> CoverageReport:
    """Build a CoverageReport from a `go test -coverprofile` output.

    - function coverage: Go func definitions hit if any statement in their body ran
    - line coverage: executable (statement) lines; line_counts = max block count
    - branch coverage: Go's statement coverage provides no reliable branch counters,
      so branch_total stays 0 (the loop skips the cond threshold when there are no
      branches), rather than fabricating pseudo-branches from uncovered statement blocks.
    """
    report = CoverageReport(created_at=datetime.now().isoformat(timespec="seconds"))
    blocks = parse_coverprofile(coverprofile)
    if not blocks:
        return report

    by_file = _aggregate_by_file(blocks, source_root, include_filter, exclude_filter)

    # Cache go-func inventories per file
    func_cache: dict[str, list[GoFunc]] = {}
    for rel in by_file:
        fp = source_root / rel
        if fp.is_file():
            func_cache[rel] = extract_go_functions(fp, source_root)
        else:
            func_cache[rel] = []

    for rel, file_blocks in by_file.items():
        fc = FileCov(file=rel)
        # Per-line max count (statement granularity)
        line_counts: dict[int, int] = {}
        for b in file_blocks:
            for ln in range(b.start_line, b.end_line + 1):
                line_counts[ln] = max(line_counts.get(ln, 0), b.count)

        fc.line_counts = line_counts
        fc.lines_total = len(line_counts)
        fc.lines_hit = sum(1 for c in line_counts.values() if c > 0)

        # Function coverage from extracted Go funcs
        for gf in func_cache.get(rel, []):
            stmts = [b for b in file_blocks
                     if b.start_line <= gf.end_line and b.end_line >= gf.start_line]
            count = max((b.count for b in stmts), default=0)
            fc.functions[gf.name] = FunctionCov(
                file=rel, name=gf.name,
                start_line=gf.start_line, end_line=gf.end_line,
                execution_count=count,
                blocks=len(stmts), blocks_executed=sum(1 for b in stmts if b.hit),
            )

        report.files[rel] = fc
    return report
