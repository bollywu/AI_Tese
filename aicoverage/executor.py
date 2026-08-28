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
from dataclasses import dataclass, field
from pathlib import Path

from .config import ProjectConfig
from .gcov import clean_gcda, collect as gcov_collect

# Worst-status merge order when several junit <testcase> entries map to the same
# bare test-function name (parametrized cases): a single failure marks the function.
_WORST_ORDER = {"pass": 0, "skipped": 1, "error": 2, "fail": 3}


def _bare_case_name(name: str) -> str:
    """Bare test-function name: strip file/class prefixes, parametrize brackets
    and Go subtest suffixes.

    "test_foo[param0]" -> "test_foo"; "tests/test_x.py::test_foo" -> "test_foo";
    "TestD/sub_case" (go test -v subtest) -> "TestD".
    """
    n = name.split("::")[-1]
    return n.split("[", 1)[0].split("/", 1)[0].strip()


@dataclass
class ExecutionResult:
    verdict: str                 # PASS | FAIL | BLOCKED
    failure_kind: str = "none"   # none | case_fail | env_blocked | timeout_blocked | all_skipped

    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    junit_path: Path | None = None
    coverage_path: Path | None = None
    log_path: Path | None = None
    detail: str = ""
    # Per-case results keyed by bare test-function name -> worst status
    # ("pass" | "fail" | "error" | "skipped"). Populated from junit.xml (C/C++) or
    # `--- PASS/FAIL:` lines (Go). Lets consumers (scan-track adjudication, quality)
    # attribute an outcome to ONE case instead of the whole-run verdict.
    cases: dict[str, str] = field(default_factory=dict)
    # Cases whose status flipped between the main run and the deterministic flaky
    # re-run (see _flaky_rerun). Empty when no re-run happened or re-run was skipped.
    flaky_cases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict, "failure_kind": self.failure_kind,
            "tests": self.tests, "failures": self.failures,
            "errors": self.errors, "skipped": self.skipped,
            "duration_s": round(self.duration_s, 1),
            "junit": str(self.junit_path) if self.junit_path else None,
            "coverage": str(self.coverage_path) if self.coverage_path else None,
            "detail": self.detail,
            "cases": self.cases,
            "flaky_cases": self.flaky_cases,
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


def _parse_junit_cases(junit_path: Path) -> dict[str, str]:
    """Parse junit.xml -> {bare_test_function_name: worst_status}.

    Status per <testcase>: "fail" (has <failure/>), "error" (has <error/>),
    "skipped" (has <skipped/>), else "pass". Parametrized cases sharing one
    function name merge to the worst status, so callers can look a result up by
    the bare function name (e.g. manifest's test_function declaration).
    """
    cases: dict[str, str] = {}
    try:
        root = ET.parse(junit_path).getroot()
    except (ET.ParseError, OSError):
        return cases
    for tc in root.iter("testcase"):
        name = _bare_case_name(tc.get("name", ""))
        if not name:
            continue
        if tc.find("failure") is not None:
            status = "fail"
        elif tc.find("error") is not None:
            status = "error"
        elif tc.find("skipped") is not None:
            status = "skipped"
        else:
            status = "pass"
        prev = cases.get(name)
        if prev is None or _WORST_ORDER[status] > _WORST_ORDER[prev]:
            cases[name] = status
    return cases


# soft-dependency probe cache: {interpreter: pytest_timeout available?}
_TIMEOUT_PROBE: dict[str, bool] = {}
_XDIST_PROBE: dict[str, bool] = {}


def _has_pytest_timeout(py: str) -> bool:
    """Whether the interpreter has pytest-timeout installed (probed once per py)."""
    if py not in _TIMEOUT_PROBE:
        try:
            proc = subprocess.run([py, "-c", "import pytest_timeout"],
                                  capture_output=True, timeout=30)
            _TIMEOUT_PROBE[py] = proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            _TIMEOUT_PROBE[py] = False
    return _TIMEOUT_PROBE[py]


def _has_xdist(py: str) -> bool:
    """Whether the interpreter has pytest-xdist installed (probed once per py)."""
    if py not in _XDIST_PROBE:
        try:
            proc = subprocess.run([py, "-c", "import xdist"],
                                  capture_output=True, timeout=30)
            _XDIST_PROBE[py] = proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            _XDIST_PROBE[py] = False
    return _XDIST_PROBE[py]


def _preflight_check(cfg: ProjectConfig, test_files: list[Path] | None) -> str | None:
    """Deterministic fail-fast checks before spending a full pytest run.

    Returns a blocking detail string, or None when all checks pass:
      1. instrumented binary exists (C/C++ only) -- the #1 root cause of the
         "all cases skipped but verdict=PASS" false positive (conftest skips when
         the binary is missing, and pytest exits 0 on all-skip);
      2. every test file to run is syntactically valid Python (a gen-produced
         broken file would otherwise fail pytest collection with an obscure error).
    """
    if getattr(cfg, "language", "c") != "go":
        bp = cfg.binary_path
        if bp is not None and not bp.exists():
            return (f"被测二进制不存在: {bp}（先 aicov build；"
                    f"否则 conftest 会 skip 全部用例形成假 PASS）")
    import ast
    files = test_files if test_files else (
        sorted(cfg.test_dir.glob("test_*.py")) if cfg.test_dir.is_dir() else [])
    broken: list[str] = []
    for p in files:
        try:
            ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        except SyntaxError as e:
            broken.append(f"{p.name}:{e.lineno}")
        except OSError:
            broken.append(f"{p.name}(不可读)")
    if broken:
        return f"测试文件语法错误，pytest 无法收集: {', '.join(broken[:5])}"
    return None


def _flaky_rerun(cfg: ProjectConfig, iter_dir: Path, cmd: list[str],
                 timeout: int, cases_before: dict[str, str]) -> list[str]:
    """Deterministic flaky detection: re-run the suite once and diff per-case status.

    A case failing in one run but passing in the other (or vice versa) is flaky
    with factual evidence; cases failing in BOTH runs are stable failures. Returns
    the flaky function-name list (empty when the re-run itself is inconclusive:
    timeout / no junit). Artifacts land in junit_rerun.xml / pytest_rerun.log.
    """
    rerun_junit = iter_dir / "junit_rerun.xml"
    rerun_log = iter_dir / "pytest_rerun.log"
    full_cmd = list(cmd)
    full_cmd[full_cmd.index("--junitxml") + 1] = str(rerun_junit)
    try:
        proc = subprocess.run(
            full_cmd, cwd=str(cfg.source_path), capture_output=True, text=True,
            timeout=timeout, env=_build_env(cfg),
        )
        log = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        rerun_log.write_text(f"TIMEOUT after {timeout}s\n{out}", encoding="utf-8")
        return []
    rerun_log.write_text(log, encoding="utf-8")
    if not rerun_junit.exists():
        return []
    cases_after = _parse_junit_cases(rerun_junit)
    if not cases_after:
        return []
    failed_before = {n for n, s in cases_before.items() if s in ("fail", "error")}
    failed_after = {n for n, s in cases_after.items() if s in ("fail", "error")}
    return sorted(failed_before ^ failed_after)


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
      - Rust:  `cargo llvm-cov --lcov` / `cargo tarpaulin` + lcov collection
      - Java:  `mvn test` / `gradle test jacocoTestReport` + jacoco.xml collection

    Args:
        test_files: run only the given test files (targeted verification after gen); None = full test_dir.
        collect_coverage: whether to run coverage collection after execution.
    """
    lang = getattr(cfg, "language", "c")
    if lang == "go":
        return run_go_tests(cfg, iter_dir, timeout=timeout, collect_coverage=collect_coverage)
    if lang == "rust":
        return run_rust_tests(cfg, iter_dir, timeout=timeout, collect_coverage=collect_coverage)
    if lang == "java":
        return run_java_tests(cfg, iter_dir, timeout=timeout, collect_coverage=collect_coverage)
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
    # Per-case status from `--- PASS/FAIL/SKIP:` lines (subtests merge into parent,
    # worst-of). Go run has no junit, so this is the per-case source for adjudication.
    result.cases = _parse_go_cases(log)

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
        # all-skip guard: go test exits 0 when every test skipped -- same false-PASS
        # shape as the pytest case (a skipped test verifies nothing)
        if result.cases and all(s == "skipped" for s in result.cases.values()):
            result.verdict = "BLOCKED"
            result.failure_kind = "all_skipped"
            result.detail = f"全部 {len(result.cases)} 个 Go 用例被跳过——未真正验证任何行为"
        else:
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


def _parse_go_cases(log: str) -> dict[str, str]:
    """Parse `go test -v` output -> {bare_test_name: worst_status}.

    Lines look like `--- PASS: TestFoo (0.01s)` / `--- FAIL: TestFoo/sub_case`.
    Subtest names ("TestFoo/sub") merge into the parent ("TestFoo"), worst-of --
    a failing subtest marks the parent function failed.
    """
    cases: dict[str, str] = {}
    for line in log.splitlines():
        s = line.strip()
        status = None
        if s.startswith("--- PASS:"):
            status = "pass"
        elif s.startswith("--- FAIL:"):
            status = "fail"
        elif s.startswith("--- SKIP:"):
            status = "skipped"
        if status is None:
            continue
        rest = s.split(":", 1)[1].strip()
        if not rest:
            continue
        name = _bare_case_name(rest.split()[0])
        if not name:
            continue
        prev = cases.get(name)
        if prev is None or _WORST_ORDER[status] > _WORST_ORDER[prev]:
            cases[name] = status
    return cases


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


def run_rust_tests(
    cfg: ProjectConfig,
    iter_dir: Path,
    *,
    timeout: int | None = None,
    collect_coverage: bool = True,
) -> ExecutionResult:
    """Run Rust tests with native coverage instrumentation, then collect lcov.

    Producer selected by [rust].cov_tool:
      - llvm-cov (preferred): `cargo llvm-cov test --lcov --output-path <lcov>`
      - tarpaulin:            `cargo tarpaulin --out Lcov --output-dir <dir>`
    Both write an lcov report parsed by rust_cover.collect_rust.
    Artifacts mirror the Go contract: execution.json, cargo.log, coverage.json.
    """
    result = ExecutionResult(verdict="BLOCKED")
    iter_dir.mkdir(parents=True, exist_ok=True)
    log_path = iter_dir / "cargo.log"
    coverage_path = iter_dir / "coverage.json"

    timeout = timeout or cfg.test_timeout
    assert timeout > 0, "test.timeout 必须为正数"

    cargo = getattr(cfg, "cargo_bin", "cargo") or "cargo"
    tool = getattr(cfg, "rust_cov_tool", "llvm-cov") or "llvm-cov"
    if shutil.which(cargo) is None:
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = f"cargo 不存在（PATH 中未找到 {cargo!r}）"
        (iter_dir / "execution.json").write_text(
            __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8")
        return result

    lcov = cfg.lcov
    lcov.parent.mkdir(parents=True, exist_ok=True)
    if lcov.exists():
        lcov.unlink()
    if tool == "llvm-cov":
        cmd = [cargo, "llvm-cov", "test", "--lcov", "--output-path", str(lcov)]
        lcov_actual = lcov
    else:  # tarpaulin writes <output-dir>/lcov.info
        out_dir = lcov.parent
        cmd = [cargo, "tarpaulin", "--out", "Lcov", "--output-dir", str(out_dir)]
        lcov_actual = out_dir / "lcov.info"

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

    passed, failed = _parse_cargo_test_output(log)
    result.tests = passed + failed
    result.failures = failed

    if collect_coverage and lcov_actual.exists():
        from .rust_cover import collect_rust
        report = collect_rust(cfg.source_path, lcov_actual,
                              include_filter=cfg.include_globs,
                              exclude_filter=cfg.exclude_globs)
        report.save(coverage_path)
        result.coverage_path = coverage_path

    if rc == 124:
        result.verdict = "BLOCKED"
        result.failure_kind = "timeout_blocked"
        result.detail = f"cargo test 超过 {timeout}s 被强制终止"
    elif rc == 0:
        result.verdict = "PASS"
    elif _rust_env_blocked(log):
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = "cargo 未正常执行（编译/环境错误）"
    else:
        result.verdict = "FAIL"
        result.failure_kind = "case_fail"

    (iter_dir / "execution.json").write_text(
        __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8")
    return result


def _parse_cargo_test_output(log: str) -> tuple[int, int]:
    """Aggregate (passed, failed) across all `test result: ...` summary lines
    (one per test target; --lib / bins / integration tests each print one)."""
    import re
    passed = failed = 0
    for m in re.finditer(r"test result:\s*\w+\.\s*(\d+)\s*passed;\s*(\d+)\s*failed", log):
        passed += int(m.group(1))
        failed += int(m.group(2))
    return passed, failed


def _rust_env_blocked(log: str) -> bool:
    import re
    markers = [r"error\[E\d+\]", r"error: could not compile", r"no such command",
               r"failed to resolve", r"is not installed"]
    return any(re.search(mk, log) for mk in markers)


def run_java_tests(
    cfg: ProjectConfig,
    iter_dir: Path,
    *,
    timeout: int | None = None,
    collect_coverage: bool = True,
) -> ExecutionResult:
    """Run Java tests with JaCoCo agent instrumentation, then collect jacoco.xml.

    Build tool by [java].build_tool: maven (`mvn test jacoco:report`) /
    gradle (`gradle test jacocoTestReport`) / auto (pom.xml -> maven, else
    build.gradle[.kts] -> gradle). JaCoCo must be configured in the build file
    (the javaagent is attached by the build plugin, not by AIcoverage).
    Artifacts mirror the Go contract: execution.json, <tool>.log, coverage.json.
    """
    result = ExecutionResult(verdict="BLOCKED")
    iter_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = iter_dir / "coverage.json"

    timeout = timeout or cfg.test_timeout
    assert timeout > 0, "test.timeout 必须为正数"

    tool = getattr(cfg, "java_build_tool", "auto") or "auto"
    if tool == "auto":
        if (cfg.source_path / "pom.xml").exists():
            tool = "maven"
        elif ((cfg.source_path / "build.gradle").exists()
              or (cfg.source_path / "build.gradle.kts").exists()):
            tool = "gradle"
        else:
            result.verdict = "BLOCKED"
            result.failure_kind = "env_blocked"
            result.detail = "未找到 pom.xml / build.gradle[.kts]，无法判定构建工具"
            (iter_dir / "execution.json").write_text(
                __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8")
            return result

    if tool == "maven":
        binary = getattr(cfg, "mvn_bin", "mvn") or "mvn"
        cmd = [binary, "test", "jacoco:report"]
        log_name = "mvn.log"
    else:
        binary = getattr(cfg, "gradle_bin", "gradle") or "gradle"
        cmd = [binary, "test", "jacocoTestReport"]
        log_name = "gradle.log"

    if shutil.which(binary) is None:
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = f"{tool} 不存在（PATH 中未找到 {binary!r}）"
        (iter_dir / "execution.json").write_text(
            __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8")
        return result

    log_path = iter_dir / log_name
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

    passed, failed = _parse_java_test_output(log)
    result.tests = passed + failed
    result.failures = failed

    jacoco = cfg.jacoco
    if collect_coverage and jacoco.exists():
        from .java_cover import collect_java
        report = collect_java(cfg.source_path, jacoco,
                              include_filter=cfg.include_globs,
                              exclude_filter=cfg.exclude_globs)
        report.save(coverage_path)
        result.coverage_path = coverage_path

    if rc == 124:
        result.verdict = "BLOCKED"
        result.failure_kind = "timeout_blocked"
        result.detail = f"{tool} 超过 {timeout}s 被强制终止"
    elif rc == 0:
        result.verdict = "PASS"
    elif _java_env_blocked(log):
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = f"{tool} 未正常执行（编译/环境错误）"
    else:
        result.verdict = "FAIL"
        result.failure_kind = "case_fail"

    (iter_dir / "execution.json").write_text(
        __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8")
    return result


def _parse_java_test_output(log: str) -> tuple[int, int]:
    """Aggregate (passed, failed) from surefire summary lines.

    Surefire prints per-class lines ("Tests run: 5, Failures: 1, Errors: 0,
    Skipped: 0") and a final summary line. Take the LAST "Tests run" line that
    carries Failures/Errors (the summary), avoiding double-counting per-class lines.
    Gradle's output is not parsed (no stable summary format); rc decides there.
    """
    import re
    matches = list(re.finditer(
        r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", log))
    if not matches:
        return 0, 0
    m = matches[-1]
    total = int(m.group(1))
    failed = int(m.group(2)) + int(m.group(3))
    return total - failed, failed


def _java_env_blocked(log: str) -> bool:
    import re
    markers = [r"COMPILATION ERROR", r"Could not resolve dependencies",
               r"BUILD FAILURE.*pom", r"Plugin .* not found", r"JAVA_HOME"]
    return any(re.search(mk, log) for mk in markers)


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

    # 0. Deterministic pre-flight (fail fast instead of wasting a whole pytest run)
    block = _preflight_check(cfg, test_files)
    if block:
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = block
        (iter_dir / "execution.json").write_text(
            __import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return result

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
    # Per-case timeout when pytest-timeout is installed (soft dependency): one
    # hanging case then fails itself instead of eating the whole-suite budget
    # (a suite-level rc=124 marks the ENTIRE round BLOCKED, losing everything).
    if _has_pytest_timeout(py):
        cmd += ["--timeout", str(timeout)]
    # Parallel execution via pytest-xdist (soft dependency, [test] workers):
    # 0=off; >0 = -n <N>; -1 = -n auto. Safe with gcov (.gcda merges are atomic
    # at process exit) and with harness local_server/free_port (random ports).
    workers = getattr(cfg, "workers", 0)
    if workers != 0 and _has_xdist(py):
        cmd += ["-n", "auto" if workers < 0 else str(workers)]

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
        result.cases = _parse_junit_cases(junit_path)

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
        # All-skip guard (2026-08-27 hardening): pytest exits 0 even when EVERY case
        # was skipped, and the scaffold's target fixture skips when the binary is
        # missing. A bare rc==0->PASS would record a green round that verified
        # nothing (the classic false positive). All-skip is BLOCKED instead.
        if result.tests > 0 and result.skipped == result.tests:
            result.verdict = "BLOCKED"
            result.failure_kind = "all_skipped"
            result.detail = (f"全部 {result.tests} 个用例被跳过——未真正验证任何行为"
                             f"（典型原因：被测二进制缺失 / 环境不满足，见 pytest.log 中 skip 理由）")
        else:
            result.verdict = "PASS"
    elif rc in (3, 4, 5) or result.tests == 0:
        # pytest rc: 2=test failures, 3=internal error, 4=usage error, 5=no tests collected
        result.verdict = "BLOCKED"
        result.failure_kind = "env_blocked"
        result.detail = f"pytest rc={rc}（未正常执行用例，疑似环境/收集问题）"
    else:
        result.verdict = "FAIL"
        result.failure_kind = "case_fail"

    # 6. Deterministic flaky detection (2026-08-27 hardening): re-run once on case
    # failure and diff per-case status. Gives quality-agent factual flaky evidence
    # instead of a guess ("same input, different outcome" is measured, not inferred).
    if (result.verdict == "FAIL" and getattr(cfg, "flaky_rerun", True)
            and result.cases):
        result.flaky_cases = _flaky_rerun(cfg, iter_dir, cmd, timeout, result.cases)
        if result.flaky_cases:
            result.detail = (result.detail + " | " if result.detail else "") + \
                f"flaky 复检：{len(result.flaky_cases)} 个用例两次运行结果不一致"

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
