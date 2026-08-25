"""本地测试执行器：pytest 子进程 + junit.xml + gcov 采集 + execution.json。

与「LLM 包装远程执行」方案的根本区别：
AIcoverage 的执行是**确定性 Python**，零 LLM 参与——执行本身没有任何需要
模型决策的环节，交给 subprocess 更快、更可靠（彻底消灭 execute-agent 的
"幻觉不执行"事故类别）。LLM 只参与执行前（gen/verify）与执行后（quality）。

产物契约（每个 iter 目录下）：
  junit.xml          — pytest 原生 --junitxml
  pytest.log         — 完整 stdout/stderr
  execution.json     — {verdict, tests, failures, errors, skipped, duration_s, coverage_path}
  coverage.json      — gcov 采集结果（CoverageReport.to_dict）
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
    """解析跑 pytest 的解释器：显式配置 > sys.executable（有 pytest 时）> python3。"""
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
    """解析 junit.xml → (tests, failures, errors, skipped)。"""
    try:
        root = ET.parse(junit_path).getroot()
        # 兼容 <testsuites><testsuite/> 与裸 <testsuite/> 两种结构
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
    """执行 pytest（默认整个 test_dir），随后采集 gcov 覆盖率并写产物。

    Args:
        test_files: 只跑指定用例文件（gen 后定向验证）；None = 全量 test_dir。
        collect_coverage: 是否在执行后跑 gcov 采集。
    """
    result = ExecutionResult(verdict="BLOCKED")
    iter_dir.mkdir(parents=True, exist_ok=True)
    junit_path = iter_dir / "junit.xml"
    log_path = iter_dir / "pytest.log"
    coverage_path = iter_dir / "coverage.json"

    py = python or resolve_python(cfg)
    timeout = timeout or cfg.test_timeout
    assert timeout > 0, "test.timeout 必须为正数（0 的语义是瞬间 kill 而非无限等待）"

    # 1. 清 .gcda，保证本轮覆盖率只反映本轮测试
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

    # 3. junit 解析
    if junit_path.exists():
        result.junit_path = junit_path
        result.tests, result.failures, result.errors, result.skipped = _parse_junit(junit_path)

    # 4. 覆盖率采集
    # 超时（rc=124）也尝试采集：进程虽被强杀，但已执行用例的 .gcda 计数会保留
    # （gcov 运行时计数按行累计到 .gcda），丢了等于浪费整轮执行；gcov 解析对
    # 不完整/损坏的 .gcda 有容错（_read_gcov_json 返回 None 跳过）。
    # ut_dir：标记"仅单测 driver 覆盖"的函数（E2E 未命中的），报告可区分来源。
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
        # pytest rc: 2=测试失败, 3=内部错误, 4=用法错误, 5=未收集到测试
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
    # 强制非交互、稳定 locale
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env
