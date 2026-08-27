"""Local test executor: pytest subprocess + junit.xml + gcov collection + execution.json.

Fundamental difference from an "LLM-wrapped remote execution" scheme:
AIcoverage execution is deterministic Python, with zero LLM involvement -- there
is no step requiring model decisions, so handing it to subprocess is faster and
more reliable (eliminating the "hallucinated-not-executed" incident class).
LLM only participates before execution (gen/verify) and after (quality).

Artifact contract (per iter directory):
  junit.xml          -- pytest native --junitxml
  pytest.log         -- full stdout/stderr
  execution.json     -- {verdict, tests, failures, errors, skipped, duration_s, coverage_path}
  coverage.json      -- gcov collection result (CoverageReport.to_dict)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .gcov import clean_gcda, collect as gcov_collect


@dataclass
class ExecutionResult:
    verdict: str                 # PASS | FAIL | BLOCKED
    failure_kind: str = "none"   # none | case_fail | env_blocked | timeout_blocked

    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    junit_path: Path | None = None
    coverage_path: Path | None = None
    log_path: Path | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict, "failure_kind": self.failure_kind,
            "tests": self.tests, "failures": self.failures,
            "errors": self.errors, "skipped": self.skipped,
            "duration_s": round(self.duration_s, 1),
            "junit": str(self.junit_path) if self.junit_path else None,
            "coverage": str(self.coverage_path) if self.coverage_path else None,
            "detail": self.detail,
        }


def resolve_python(cfg: ProjectConfig) -> str:
    """Resolve the interpreter used to run pytest: explicit config > sys.executable (when it has pytest) > python3."""
    candidates: list[str] = []
    if cfg.test_python and cfg.test_python != "auto":
        return cfg.test_python
    candidates.append(sys.executable)
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            candidates.append(p)
    for py in candidates:
        try:
            proc = subprocess.run(
                [py, "-m", "pytest", "--version"],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                return py
        except (subprocess.TimeoutExpired, OSError):
            continue
    raise SystemExit(
        "❌ 找不到可用的 pytest 解释器。请在 aicoverage.toml 的 [test] python "
        "里显式指定一个装有 pytest 的 Python 绝对路径。"
    )


def _parse_junit(junit_path: Path) -> tuple[int, int, int, int]:
    """Parse junit.xml -> (tests, failures, errors, skipped)."""
    try:
        root = ET.parse(junit_path).getroot()
        # handle both <testsuites><testsuite/> and bare <testsuite/> structures
        suites = root.findall(".//testsuite")
        if not suites:
            suites = [root] if root.tag == "testsuite" else []
        t = f = e = s = 0
        for su in suites:
            t += int(su.get("tests", 0))
            f += int(su.get("failures", 0))
            e += int(su.get("errors", 0))
            s += int(su.get("skipped", 0))
        return t, f, e, s
    except (ET.ParseError, OSError, ValueError):
        return 0, 0, 0, 0


def run_tests(
    cfg: ProjectConfig,
    iter_dir: Path,
    *,
    test_files: list[Path] | None = None,
    timeout: int | None = None,
    collect_coverage: bool = True,
    python: str | None = None,
) -> ExecutionResult:
    """Run tests then collect coverage, writing artifacts to iter_dir.

    Dispatches by language:
      - C/C++: pytest subprocess + junit.xml + gcov collection
      - Go:    `go test -coverprofile=...` + coverprofile collection

    Args:
        test_files: run only the given test files (targeted verification after gen); None = full test_dir.
        collect_coverage: whether to run coverage collection after execution.
    """
    if getattr(cfg, "language", "c") == "go":
        return run_go_tests(cfg, iter_dir, timeout=timeout, collect_coverage=collect_coverage)
    return _run_c_tests(cfg, iter_dir, test_files=test_files, timeout=timeout,
                        collect_coverage=collect_coverage, python=python)


def run_go_tests(
    cfg: ProjectConfig,
    iter_dir: Path,
    *,
    timeout: int | None = None,
    collect_coverage: bool = True,
) -> ExecutionResult:
    """Run `go test -coverprofile=...` over the configured packages, then collect Go coverage.

    Go's toolchain instruments and reports statement coverage natively; no separate
    build step / binary is required. Artifacts mirror the C/C++ contract:
      execution.json, <log>.log, coverage.json (CoverageReport via go_cover.collect_go).
    """
    result = ExecutionResult(verdict="BLOCKED")
    iter_dir.mkdir(parents=True, exist_ok=True)
    log_path = iter_dir / "gotest.log"
    coverage_path = iter_dir / "coverage.json"

    timeout = timeout or cfg.test_timeout
    assert timeout > 0, "test.timeout 必须为正数"

    go_bin = getattr(cfg, "go_bin", "go") or "go"
    coverprofile = cfg.coverprofile
    coverprofile.parent.mkdir(parents=True, exist_ok=True)
    # Remove stale profile so a failed run doesn't leave old data.
    if coverprofile.exists():
        coverprofile.unlink()

    # -v surfaces per-test `--- PASS/FAIL` lines so test counts are meaningful.
    cmd = [go_bin, "test", "-v", "-coverprofile", str(coverprofile)]
    tags = getattr(cfg, "go_build_tags", "") or ""
    if tags:
        cmd += ["-tags", tags]
    cmd += list(getattr(cfg, "go_packages", ["./..."]))

    import time
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cfg.source_path), capture_output=True, text=True,
            timeout=timeout, env=_build_env(cfg),
        )
        log = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        log = f"TIMEOUT after {timeout}s\n{out}"
        rc = 124
    result.duration_s = time.time() - start
    log_path.write_text(log, encoding="utf-8")
    result.log_path = log_path

    # Parse Go test output: "ok <pkg> <dur>" / "FAIL <pkg>" lines; count tests.
    passed, failed = _parse_go_test_output(log)
    result.tests = passed + failed
    result.failures = failed

    if collect_coverage and coverprofile.exists():
        from .go_cover import collect_go
        report = collect_go(
            cfg.source_path, coverprofile,
            include_filter=cfg.include_globs, exclude_filter=cfg.exclude_globs,
        )
        report.save(coverage_path)
        result.coverage_path = coverage_path

    # Verdict: Go exits nonzero on any test failure (both env errors and case fails).
    if rc == 124:
        result.verdict = "BLOCKED"
        result.failure_kind = "timeout_blocked"
        result.detail = f"go test 超过 {timeout}s 被强制终止"
    elif rc == 0:
        result.verdict = "PASS"
    elif _go_env_blocked(log):
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = "go test 未正常执行（编译/环境错误）"
    else:
        result.verdict = "FAIL"
        result.failure_kind = "case_fail"

    (iter_dir / "execution.json").write_text(
        __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def _parse_go_test_output(log: str) -> tuple[int, int]:
    """Extract (passed, failed) test counts from `go test -v`-style output.

    Falls back to the package `ok`/`FAIL` summary when verbose names are absent.
    """
    passed = 0
    failed = 0
    for line in log.splitlines():
        line = line.strip()
        # verbose per-test result lines look like:  === RUN / --- PASS: / --- FAIL:
        if line.startswith("--- PASS:"):
            passed += 1
        elif line.startswith("--- FAIL:"):
            failed += 1
        elif line.startswith("PASS") or line.startswith("FAIL"):
            # package summary; ignore (already counted via --- lines when -v is used)
            pass
    if passed == 0 and failed == 0:
        # Not verbose: approximate by counting package-level failures.
        failed = sum(1 for ln in log.splitlines() if ln.startswith("FAIL"))
    return passed, failed


def _go_env_blocked(log: str) -> bool:
    """Heuristic: Go compile/build errors indicate an environment problem, not a test failure."""
    markers = ["build failed", "cannot find package", "no required module provides package",
               "package .* is not in std", "compile:", "[build failed]",
               "setup failed", "could not import"]
    import re
    for mk in markers:
        if re.search(mk, log):
            return True
    return False


def _run_c_tests(
    cfg: ProjectConfig,
    iter_dir: Path,
    *,
    test_files: list[Path] | None = None,
    timeout: int | None = None,
    collect_coverage: bool = True,
    python: str | None = None,
) -> ExecutionResult:
    """Run pytest (defaults to the whole test_dir), then collect gcov coverage and write artifacts.

    Args:
        test_files: run only the given test files (targeted verification after gen); None = full test_dir.
        collect_coverage: whether to run gcov collection after execution.
    """
    result = ExecutionResult(verdict="BLOCKED")
    iter_dir.mkdir(parents=True, exist_ok=True)
    junit_path = iter_dir / "junit.xml"
    log_path = iter_dir / "pytest.log"
    coverage_path = iter_dir / "coverage.json"

    py = python or resolve_python(cfg)
    timeout = timeout or cfg.test_timeout
    assert timeout > 0, "test.timeout 必须为正数（0 的语义是瞬间 kill 而非无限等待）"

    # 1. Clear .gcda so this round's coverage reflects only this round's tests
    if collect_coverage:
        clean_gcda(cfg.source_path)

    # 2. pytest
    if test_files:
        targets = [str(p) for p in test_files]
    else:
        targets = [cfg.test_dirname]
    cmd = [py, "-m", "pytest", *targets, "-v", "--junitxml", str(junit_path),
           "-p", "no:cacheprovider"]

    import time
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cfg.source_path), capture_output=True, text=True,
            timeout=timeout,
            env=_build_env(cfg),
        )
        log = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        log = f"TIMEOUT after {timeout}s\n{out}"
        rc = 124
    result.duration_s = time.time() - start
    log_path.write_text(log, encoding="utf-8")
    result.log_path = log_path

    # 3. junit parsing
    if junit_path.exists():
        result.junit_path = junit_path
        result.tests, result.failures, result.errors, result.skipped = _parse_junit(junit_path)

    # 4. Coverage collection
    # Also attempt collection on timeout (rc=124): although the process was killed,
    # the .gcda counts from already-executed cases persist (gcov runtime counts
    # accumulate by line into .gcda); discarding them wastes the whole round.
    # gcov parsing tolerates incomplete/corrupt .gcda (_read_gcov_json returns None).
    # ut_dir marks functions covered only by unit-test drivers (E2E-missed) so the
    # report can distinguish coverage sources.
    if collect_coverage:
        report = gcov_collect(
            cfg.source_path, cfg.gcov_bin,
            include_filter=cfg.include_globs, exclude_filter=cfg.exclude_globs,
            ut_dir=cfg.ut_obj_path,
        )
        report.save(coverage_path)
        result.coverage_path = coverage_path

    # 5. verdict
    if rc == 124:
        result.verdict = "BLOCKED"
        result.failure_kind = "timeout_blocked"
        result.detail = f"pytest 超过 {timeout}s 被强制终止"
    elif rc == 0:
        result.verdict = "PASS"
    elif rc in (3, 4, 5) or result.tests == 0:
        # pytest rc: 2=test failures, 3=internal error, 4=usage error, 5=no tests collected
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = f"pytest rc={rc}（未正常执行用例，疑似环境/收集问题）"
    else:
        result.verdict = "FAIL"
        result.failure_kind = "case_fail"

    (iter_dir / "execution.json").write_text(
        __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def _build_env(cfg: ProjectConfig) -> dict[str, str]:
    import os
    env = dict(os.environ)
    env.update(cfg.to_env())
    # force non-interactive, stable locale
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env
