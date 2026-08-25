"""gcov coverage backend: parses `gcov -i -b` intermediate JSON (.gcov.json / .gcov.json.gz).

gcc >= 9 supports `-i` (intermediate JSON); gcc 12 defaults to gzip-compressed output. JSON structure:

    {"gcc_version": "...", "format_version": "1",
     "files": [{
         "file": "src/wrk.c",
         "current_working_directory": "/build/cwd",     # compile-time cwd (for relative-path reconstruction)
         "functions": [{"name", "demangled_name", "start_line", "end_line",
                        "execution_count", "blocks", "blocks_executed", ...}],
         "lines": [{"line_number", "count", "unexecuted_block",
                    "function_name",
                    "branches": [{"count", "fallthrough", "throw"}, ...]}]
     }]}

Metric semantics (aligned with classic coverage tools):
- function coverage = functions with execution_count > 0 / all functions
- branch coverage = branches with count > 0 (taken at least once) / all branches
- line coverage = lines with count > 0 / all lines (auxiliary metric)
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ── 数据模型 ────────────────────────────────────────────────────────

@dataclass
class FunctionCov:
    file: str                 # relative to source root (normalized)
    name: str                 # function name (demangled preferred)
    start_line: int
    end_line: int
    execution_count: int
    blocks: int
    blocks_executed: int
    ut_hit: bool = False      # True = covered only by unit-test driver (E2E-missed)

    @property
    def hit(self) -> bool:
        return self.execution_count > 0

    def to_dict(self) -> dict:
        return {"file": self.file, "name": self.name,
                "start_line": self.start_line, "end_line": self.end_line,
                "execution_count": self.execution_count, "hit": self.hit,
                "blocks": self.blocks, "blocks_executed": self.blocks_executed,
                "ut_hit": self.ut_hit}


@dataclass
class BranchCov:
    file: str
    line: int
    function: str
    count: int
    fallthrough: bool
    throw: bool

    @property
    def hit(self) -> bool:
        return self.count > 0


@dataclass
class FileCov:
    file: str
    functions: dict[str, FunctionCov] = field(default_factory=dict)   # name -> cov
    branches: list[BranchCov] = field(default_factory=list)
    lines_total: int = 0
    lines_hit: int = 0
    # line no -> execution count (only gcov-identified executable lines; used for HTML line coloring)
    line_counts: dict[int, int] = field(default_factory=dict)


@dataclass
class CoverageReport:
    """A complete snapshot of one coverage collection (serializable to coverage.json)."""
    created_at: str = ""
    files: dict[str, FileCov] = field(default_factory=dict)   # rel file -> FileCov

    # ── Aggregate metrics ──
    @property
    def functions(self) -> list[FunctionCov]:
        return [f for fc in self.files.values() for f in fc.functions.values()]

    @property
    def func_total(self) -> int:
        return sum(len(fc.functions) for fc in self.files.values())

    @property
    def func_hit(self) -> int:
        return sum(1 for f in self.functions if f.hit)

    @property
    def func_pct(self) -> float:
        return round(self.func_hit * 100.0 / self.func_total, 2) if self.func_total else 0.0

    @property
    def branch_total(self) -> int:
        return sum(len(fc.branches) for fc in self.files.values())

    @property
    def branch_hit(self) -> int:
        return sum(1 for fc in self.files.values() for b in fc.branches if b.hit)

    @property
    def cond_pct(self) -> float:
        return round(self.branch_hit * 100.0 / self.branch_total, 2) if self.branch_total else 0.0

    @property
    def line_total(self) -> int:
        return sum(fc.lines_total for fc in self.files.values())

    @property
    def line_hit(self) -> int:
        return sum(fc.lines_hit for fc in self.files.values())

    @property
    def line_pct(self) -> float:
        return round(self.line_hit * 100.0 / self.line_total, 2) if self.line_total else 0.0

    def uncovered_functions(self) -> list[FunctionCov]:
        """Uncovered functions (sorted by file, then line; execution count 0)."""
        return sorted(
            (f for f in self.functions if not f.hit),
            key=lambda f: (f.file, f.start_line),
        )

    def delta(self, previous: "CoverageReport | None") -> dict:
        """Increment relative to the previous round (pp = percentage points)."""
        if previous is None:
            return {"func_pp": self.func_pct, "cond_pp": self.cond_pct,
                    "newly_hit": [f.to_dict() for f in self.functions if f.hit]}
        prev_hit = {(f.file, f.name) for f in previous.functions if f.hit}
        newly = [f.to_dict() for f in self.functions
                 if f.hit and (f.file, f.name) not in prev_hit]
        return {
            "func_pp": round(self.func_pct - previous.func_pct, 2),
            "cond_pp": round(self.cond_pct - previous.cond_pct, 2),
            "newly_hit": newly,
        }

    # ── Serialization ──
    def to_dict(self) -> dict:
        return {
            "created_at": self.created_at or datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "func_total": self.func_total, "func_hit": self.func_hit,
                "func_pct": self.func_pct,
                "branch_total": self.branch_total, "branch_hit": self.branch_hit,
                "cond_pct": self.cond_pct,
                "line_total": self.line_total, "line_hit": self.line_hit,
                "line_pct": self.line_pct,
                "uncovered_func_count": len(self.uncovered_functions()),
            },
            "files": {
                rel: {
                    "functions": [f.to_dict() for f in sorted(fc.functions.values(),
                                                              key=lambda x: x.start_line)],
                    "branches": [
                        {"line": b.line, "function": b.function, "count": b.count,
                         "fallthrough": b.fallthrough, "throw": b.throw}
                        for b in sorted(fc.branches, key=lambda x: x.line)
                    ],
                    "branch_total": len(fc.branches),
                    "branch_hit": sum(1 for b in fc.branches if b.hit),
                    "lines_total": fc.lines_total,
                    "lines_hit": fc.lines_hit,
                    # line no -> count (for HTML line coloring; keys as strings per JSON spec)
                    "line_counts": {str(k): v for k, v in sorted(fc.line_counts.items())},
                }
                for rel, fc in sorted(self.files.items())
            },
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "CoverageReport":
        """Restore from coverage.json (incl. branches/lines so cross-round delta and threshold checks stay correct)."""
        data = json.loads(path.read_text(encoding="utf-8"))
        report = cls(created_at=data.get("created_at", ""))
        for rel, fc_data in data.get("files", {}).items():
            fc = FileCov(file=rel)
            for fd in fc_data.get("functions", []):
                fc.functions[fd["name"]] = FunctionCov(
                    file=rel, name=fd["name"],
                    start_line=fd.get("start_line", 0), end_line=fd.get("end_line", 0),
                    execution_count=fd.get("execution_count", 0),
                    blocks=fd.get("blocks", 0), blocks_executed=fd.get("blocks_executed", 0),
                    ut_hit=bool(fd.get("ut_hit", False)),
                )
            for bd in fc_data.get("branches", []):
                fc.branches.append(BranchCov(
                    file=rel, line=bd.get("line", 0), function=bd.get("function", ""),
                    count=bd.get("count", 0), fallthrough=bd.get("fallthrough", False),
                    throw=bd.get("throw", False),
                ))
            fc.lines_total = fc_data.get("lines_total", 0)
            fc.lines_hit = fc_data.get("lines_hit", 0)
            fc.line_counts = {
                int(k): int(v) for k, v in (fc_data.get("line_counts") or {}).items()
            }
            report.files[rel] = fc
        return report

    def summary_text(self) -> str:
        """Human-readable summary (shared by terminal/report)."""
        lines = [
            f"函数覆盖: {self.func_hit}/{self.func_total} = {self.func_pct:.2f}%",
            f"分支覆盖: {self.branch_hit}/{self.branch_total} = {self.cond_pct:.2f}%",
            f"行覆盖:   {self.line_hit}/{self.line_total} = {self.line_pct:.2f}%",
        ]
        unc = self.uncovered_functions()
        if unc:
            lines.append(f"未覆盖函数: {len(unc)} 个（前 20）:")
            for f in unc[:20]:
                lines.append(f"  - {f.file}:{f.start_line} {f.name}")
        return "\n".join(lines)


# ── Collection ─────────────────────────────────────────────────────────

def find_gcno_files(source_root: Path, exclude_dir: Path | None = None) -> list[Path]:
    """All .gcno files under the source tree (instrumented compilation-unit markers)."""
    results: list[Path] = []
    for p in sorted(source_root.rglob("*.gcno")):
        if exclude_dir is not None:
            try:
                p.resolve().relative_to(exclude_dir.resolve())
                continue    # skip artifacts under the test dir
            except ValueError:
                pass
        results.append(p)
    return results


def clean_gcda(source_root: Path, exclude_dir: Path | None = None) -> int:
    """Remove all .gcda (runtime counter files); returns the number removed."""
    n = 0
    for p in source_root.rglob("*.gcda"):
        if exclude_dir is not None:
            try:
                p.resolve().relative_to(exclude_dir.resolve())
                continue
            except ValueError:
                pass
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _read_gcov_json(path: Path) -> dict | None:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                return json.load(f)
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, gzip.BadGzipFile):
        return None


def _normalize_file(file_field: str, compile_cwd: str, source_root: Path) -> str | None:
    """Reconstruct the gcov JSON 'file' field into a normalized path relative to source_root."""
    if not file_field:
        return None
    p = Path(file_field)
    if not p.is_absolute() and compile_cwd:
        p = Path(compile_cwd) / p
    p = Path(p.resolve()) if p.is_absolute() else p
    try:
        return p.resolve().relative_to(source_root).as_posix()
    except ValueError:
        return None


def collect(
    source_root: Path,
    gcov_bin: str = "gcov",
    *,
    include_filter=None,
    exclude_filter=None,
    out_dir: Path | None = None,
    timeout_per_file: int = 60,
    ut_dir: Path | None = None,
) -> CoverageReport:
    """Run gcov and aggregate coverage.

    Steps:
    1. Find all .gcno (without .gcda gcov still outputs all-zero counts -- i.e. the
       "function-inventory baseline")
    2. For each `gcov -i -b <gcno>`, write .gcov.json[.gz] into its **own subdir**
       (2026-08-24 incident 1 fix: libtool projects commonly double-compile
       "static + PIC shared lib", so one source file yields two same-basename .gcno;
       the old implementation flattened all outputs into one dir and same-named files
       overwrote each other. Per-gcno subdirs eliminate the filename collision.)
    3. Aggregate **all** raw records by (file) (one rel path may have several copies
       from double compilation), then for each (file, line/function) take the copy
       with the **larger count** (see merge logic below).
       (2026-08-24 incident 2 fix -- discovered by gen-agent itself running gcov
       during the real ModSecurity loop iter6: subdirs were named with unpadded
       integer strings ("0","1",...,"122"); the old implementation decided processing
       order via `sorted(path-string)` and "first-wins" (seen_files); but string order
       is not numeric order (`"122" < "56"`). When the "no-.gcda static compile"
       subdir sorts before the "real copy with .gcda", the zero data is written first
       and occupies the slot, so real coverage is read as 0% -- all 25 target
       functions in iter6 hit this bug, real coverage contributions from new cases
       were fully swallowed, making coverage identical to iter5 and appearing
       "not progressing".
       The current merge strategy depends on no ordering: for each (file, line) take
       the record with the **largest count** across all duplicate compilations (real
       execution data's count is naturally >= 0 data, so it wins under any order),
       eliminating the ordering dependency fundamentally.)
    """
    report = CoverageReport(created_at=datetime.now().isoformat(timespec="seconds"))
    gcno_files = find_gcno_files(source_root)
    if not gcno_files:
        return report

    # Unit-test source detection: .gcno under ut_dir (e.g. .aicoverage/ut/) belongs to
    # unit-test driver artifacts. Functions hit but E2E-missed (outside ut dir)
    # -> ut_hit=True (covered only by unit test).
    ut_root = Path(ut_dir).resolve() if ut_dir else None

    work_dir = out_dir or (source_root / ".aicoverage" / "coverage_raw")
    # Always start fresh (the old partial-glob cleanup can't handle the new subdir structure)
    if work_dir.exists():
        import shutil as _shutil
        _shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    from .globutil import glob_matches

    # ut flag per subdir gcno: subdir index -> is unit-test source.
    # gcov runs in parallel via a thread pool (each .gcno is an independent subprocess
    # writing to its own subdir, no interference), significantly speeding up projects
    # with many .gcno files (P3 perf optimization).
    gcno_is_ut: dict[int, bool] = {}

    def _run_gcov(i_gcno: tuple[int, Path]) -> bool:
        i, gcno = i_gcno
        sub = work_dir / str(i)
        sub.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [gcov_bin, "-i", "-b", "-c", str(gcno)],
                cwd=sub, capture_output=True, timeout=timeout_per_file,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        try:
            return ut_root is not None and gcno.resolve().is_relative_to(ut_root)
        except ValueError:
            return False

    import concurrent.futures as _cf
    workers = min(8, max(1, (os.cpu_count() or 4)))
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_run_gcov, list(enumerate(gcno_files))))
    for i, is_ut in enumerate(results):
        gcno_is_ut[i] = is_ut

    # Order-independent: no sorted(); processing order doesn't affect the merge
    json_paths = list(work_dir.rglob("*.gcov.json")) + list(work_dir.rglob("*.gcov.json.gz"))

    # Collect all raw file_entries by rel path (one rel may have several copies from
    # double compilation), tagging each record with whether it came from unit-test
    # artifacts (to distinguish E2E vs unit-test coverage sources).
    raw_by_rel: dict[str, list[tuple[dict, bool]]] = {}
    for jp in json_paths:
        data = _read_gcov_json(jp)
        if not isinstance(data, dict) or "files" not in data:
            continue
        compile_cwd = data.get("current_working_directory", "")
        # jp lives under work_dir/<index>/, look up that gcno's ut flag
        try:
            idx = int(jp.parent.name)
        except ValueError:
            idx = -1
        is_ut = gcno_is_ut.get(idx, False)
        for file_entry in data["files"]:
            rel = _normalize_file(file_entry.get("file", ""), compile_cwd, source_root)
            if rel is None:
                continue
            if include_filter and not glob_matches(rel, include_filter):
                continue
            if exclude_filter and glob_matches(rel, exclude_filter):
                continue
            raw_by_rel.setdefault(rel, []).append((file_entry, is_ut))

    for rel, entries in raw_by_rel.items():
        fc = FileCov(file=rel)

        # Functions: for same-named functions across duplicate compilations, take the
        # one with the larger execution_count. Also keep an "E2E-only (non-unit-test)"
        # best stat to determine ut_hit.
        func_best: dict[str, dict] = {}
        func_best_e2e: dict[str, dict] = {}
        for entry, is_ut in entries:
            for fn in entry.get("functions", []):
                name = fn.get("demangled_name") or fn.get("name") or ""
                if not name:
                    continue
                prev = func_best.get(name)
                if prev is None or int(fn.get("execution_count", 0)) > int(prev.get("execution_count", 0)):
                    func_best[name] = fn
                if not is_ut:
                    prev_e = func_best_e2e.get(name)
                    if prev_e is None or int(fn.get("execution_count", 0)) > int(prev_e.get("execution_count", 0)):
                        func_best_e2e[name] = fn
        for name, fn in func_best.items():
            e2e_fn = func_best_e2e.get(name)
            e2e_hit = bool(e2e_fn and int(e2e_fn.get("execution_count", 0)) > 0)
            hit = int(fn.get("execution_count", 0)) > 0
            fc.functions[name] = FunctionCov(
                file=rel, name=name,
                start_line=int(fn.get("start_line", 0)),
                end_line=int(fn.get("end_line", 0)),
                execution_count=int(fn.get("execution_count", 0)),
                blocks=int(fn.get("blocks", 0)),
                blocks_executed=int(fn.get("blocks_executed", 0)),
                ut_hit=bool(hit and not e2e_hit),
            )

        # Lines + branches: for each line take the copy with the larger count (the line's
        # branch list travels with it as a whole, keeping T/F order consistent within one
        # source and avoiding cross-copy misalignment)
        line_best_count: dict[int, int] = {}
        line_best_branches: dict[int, list[dict]] = {}
        line_best_fname: dict[int, str] = {}
        for entry, _is_ut in entries:
            for ln in entry.get("lines", []):
                line_no = int(ln.get("line_number", 0))
                count = int(ln.get("count", 0))
                if line_no not in line_best_count or count > line_best_count[line_no]:
                    line_best_count[line_no] = count
                    line_best_branches[line_no] = ln.get("branches", []) or []
                    line_best_fname[line_no] = ln.get("function_name", "") or ""

        fc.lines_total = len(line_best_count)
        fc.lines_hit = sum(1 for c in line_best_count.values() if c > 0)
        fc.line_counts = dict(line_best_count)
        for line_no, branches in line_best_branches.items():
            fname = line_best_fname.get(line_no, "")
            for br in branches:
                fc.branches.append(BranchCov(
                    file=rel, line=line_no, function=fname,
                    count=int(br.get("count", 0)),
                    fallthrough=bool(br.get("fallthrough", False)),
                    throw=bool(br.get("throw", False)),
                ))

        report.files[rel] = fc
    return report
