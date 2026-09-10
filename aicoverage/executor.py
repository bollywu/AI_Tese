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

import os
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import observability as obs
from .backends import get_backend
from .config import ProjectConfig


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


def collect_coverage_artifact(cfg: ProjectConfig, coverage_path: Path) -> Any:
    """gcov 采集并落盘（宿主机与沙箱容器共用这一份逻辑，保证过滤/ut 标记一致）。"""
    report = get_backend(cfg).collect(
        cfg,
        include_filter=cfg.include_globs, exclude_filter=cfg.exclude_globs,
        ut_dir=cfg.ut_obj_path,
    )
    report.save(coverage_path)
    return report


def run_tests(
    cfg: ProjectConfig,
    iter_dir: Path,
    *,
    test_files: list[Path] | None = None,
    timeout: int | None = None,
    collect_coverage: bool = True,
    python: str | None = None,
    sandbox: Any | None = None,
) -> ExecutionResult:
    """Run pytest (defaults to the whole test_dir), then collect gcov coverage and write artifacts.

    Args:
        test_files: run only the given test files (targeted verification after gen); None = full test_dir.
        collect_coverage: whether to run gcov collection after execution.
        sandbox: 非空时 pytest（以及默认的 gcov 采集）在沙箱内执行；None = 宿主机直跑。
            被测二进制（harness run_binary）、单测 driver、local_server 都跑在 pytest
            进程内，随之一起被隔离。
    """
    result = ExecutionResult(verdict="BLOCKED")
    iter_dir.mkdir(parents=True, exist_ok=True)
    junit_path = iter_dir / "junit.xml"
    log_path = iter_dir / "pytest.log"
    coverage_path = iter_dir / "coverage.json"

    in_sandbox = sandbox is not None and sandbox.name != "host"
    if in_sandbox and not python:
        # 容器内的解释器不能靠宿主机探测（resolve_python 检查的是宿主机 pytest）
        python = str(getattr(cfg, "sandbox_python", "python3") or "python3")
    py = python or resolve_python(cfg)
    timeout = timeout or cfg.test_timeout
    assert timeout > 0, "test.timeout 必须为正数（0 的语义是瞬间 kill 而非无限等待）"

    # 1. Clear round counters (gcov: remove .gcda; go: empty GOCOVERDIR; java: delete exec)
    #    so this round's coverage reflects only this round's tests
    #    （.gcda 在挂载共享的源码树里，宿主机删得到，无需进容器）
    if collect_coverage:
        get_backend(cfg).clean(cfg)

    # 2. pytest
    if test_files:
        targets = [str(p) for p in test_files]
    else:
        targets = [cfg.test_dirname]
    cmd = [py, "-m", "pytest", *targets, "-v", "--junitxml", str(junit_path),
           "-p", "no:cacheprovider"]

    import time
    start = time.time()
    if in_sandbox:
        res = sandbox.run(
            shlex.join(cmd), cwd=cfg.source_path, env=_build_env(cfg),
            timeout=timeout,
            network=bool(getattr(cfg, "sandbox_network_test", False)),
        )
        rc, log = res.rc, res.log
    else:
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
    #
    # 沙箱模式下默认在容器内采集：.gcda 由容器内的 gcc 运行时写出，宿主机 gcov
    # 版本一旦不匹配会整轮解析失败；同源采集彻底消除该风险，失败再回退宿主机。
    if collect_coverage:
        collected = False
        if in_sandbox and getattr(cfg, "sandbox_collect_in_container", True):
            from .sandbox import sandbox_collect_command
            cres = sandbox.run(
                sandbox_collect_command(cfg, coverage_path),
                cwd=cfg.source_path, env=_build_env(cfg),
                timeout=max(timeout, 600), network=False,
            )
            if cres.rc == 0 and coverage_path.exists():
                collected = True
            else:
                # run_id 从 AICOV_RUN_DIR 反推（executor 层不持有 run 上下文）
                run_dir_env = os.environ.get("AICOV_RUN_DIR", "")
                obs.emit_diagnostic(
                    "SANDBOX_COLLECT_FALLBACK",
                    Path(run_dir_env).name if run_dir_env else "",
                    runs_dir=Path(run_dir_env).parent if run_dir_env else None,
                    message=f"容器内 gcov 采集失败（rc={cres.rc}），回退宿主机采集",
                    context={"log_tail": cres.log[-800:]},
                )
                print("      ⚠️ 容器内 gcov 采集失败，回退宿主机采集（注意 gcc/gcov 版本一致性）")
        if not collected:
            report = collect_coverage_artifact(cfg, coverage_path)
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
