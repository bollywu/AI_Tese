"""Bug-report cross-validation (deterministic, zero LLM).

quality-agent's `product_suspect` / `report_bug` items are the most valuable loop
output but also the easiest to hallucinate: an invented "file:line" or a bug
claimed on a case that actually PASSED both sound plausible and pollute the final
report. This module validates each report against hard facts before the report
trusts it (plan 3.1):

  1. the evidence must cite a source location (file:line) whose file actually
     exists under the project source root;
  2. the referenced test must really have FAILED (checked against the executor's
     per-case results in execution.json "cases");
  3. (base comparison, plan 3.2) for MR loops, failing cases can be re-run against
     the base version in an isolated `git worktree` -- pass@base + fail@head is
     factual proof the change introduced a regression. Opt-in because it costs a
     full extra build; the main working tree is never touched (worktree only).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import ProjectConfig

_SRC_LOC_RE = re.compile(r"(?P<file>[\w./\\-]+\.[A-Za-z]{1,4}):(?P<line>\d+)")


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def validate_bug_reports(cfg: ProjectConfig, quality: dict,
                         execution_cases: dict[str, str]) -> dict:
    """Cross-validate every report_bug action item / product_suspect failure.

    Args:
        quality: parsed quality_report.json (failures[].action == "report_bug" and
            action_items[].type == "report_bug" are both validated).
        execution_cases: per-case results {bare_test_name: status} from
            execution.json (executor._parse_junit_cases / _parse_go_cases).

    Returns:
        {"valid": [...], "invalid": [{"item", "reason"}]} -- the final report
        presents only the valid ones as suspected defects and lists the invalid
        ones separately as "证据不足（已降级）" so nothing disappears silently.
    """
    valid: list[dict] = []
    invalid: list[dict] = []

    candidates: list[dict] = []
    for f in quality.get("failures") or []:
        if f.get("action") == "report_bug":
            candidates.append(f)
    for a in quality.get("action_items") or []:
        if a.get("type") == "report_bug":
            candidates.append(a)

    for item in candidates:
        # Collect the textual haystack: evidence + suggestion + test name
        ev = str(item.get("evidence") or item.get("suggestion") or "")
        test_name = str(item.get("test") or "").split("::")[-1].split("[")[0]

        # Rule 1: the cited source location must exist
        locs = _SRC_LOC_RE.findall(ev)
        if not locs:
            invalid.append({"item": item,
                            "reason": "证据未引用源码位置（file:line）——不可核实"})
            continue
        files = {loc[0] for loc in locs}
        src = cfg.source_path
        on_disk = any(
            (src / f).exists() or (src / f.lstrip("./")).exists()
            or any(p.name == Path(f).name for p in src.rglob(Path(f).name))
            for f in files
        )
        if not on_disk:
            invalid.append({"item": item,
                            "reason": f"证据引用的文件不存在于源码树: {sorted(files)}"})
            continue

        # Rule 2: the referenced test must actually have failed
        if execution_cases and test_name:
            status = execution_cases.get(test_name)
            if status not in ("fail", "error", None):
                invalid.append({"item": item,
                                "reason": f"引用的用例 {test_name} 实际状态为 {status}"
                                          f"（非失败）——疑似臆测"})
                continue

        valid.append(item)
    return {"valid": valid, "invalid": invalid}


# ── Base-version comparison (plan 3.2, opt-in) ─────────────────────────


def compare_base_head(cfg: ProjectConfig, test_files: list[Path],
                      *, base_ref: str, work_dir: Path | None = None) -> dict | None:
    """Re-run the given failing test files against the base version in an isolated
    git worktree, then diff per-case status against the (already recorded) head run.

    Pipeline (the main working tree is never modified):
      git worktree add --detach <tmp> <base_ref>
        -> copy aicoverage.toml + tests/ (conftest/harness/case files) into it
        -> run the instrumented build there (cfg.build_cmd, cwd=worktree)
        -> run pytest on the copied test files (AICOV_SRC/AICOV_BINARY point at
           the worktree) and parse junit per-case status
        -> git worktree remove

    Returns {"base_cases": {...}, "head_cases": {...}} (caller supplies head_cases
    via load_head_cases) or None when any step fails (missing git / build error /
    no junit). Failures are returned as None, never raised: comparison is an
    evidence-enhancement, not a loop dependency.
    """
    if not shutil.which("git"):
        return None
    from .config import NON_BUILD_LANGUAGES
    if getattr(cfg, "language", "c") in NON_BUILD_LANGUAGES:
        return None  # non-build languages have no instrumented binary to swap; not supported
    work_dir = work_dir or cfg.workspace / "base_compare"
    work_dir.mkdir(parents=True, exist_ok=True)
    worktree = work_dir / "wt"
    if worktree.exists():
        _run_git(cfg.source_path, ["worktree", "remove", "--force", str(worktree)])
    rc = _run_git(cfg.source_path,
                  ["worktree", "add", "--detach", str(worktree), base_ref])
    if rc != 0:
        return None
    try:
        # Copy the test scaffold (conftest + harness + the failing case files)
        (worktree / cfg.test_dirname).mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg.conftest_path, worktree / cfg.test_dirname / "conftest.py")
        lib_src = cfg.tests_lib_dir
        if lib_src.is_dir():
            shutil.copytree(lib_src, worktree / cfg.test_dirname / "lib",
                            dirs_exist_ok=True)
        for tf in test_files:
            if tf.is_file():
                shutil.copy2(tf, worktree / cfg.test_dirname / tf.name)
        # Instrumented build inside the worktree
        if cfg.clean_cmd:
            subprocess.run(cfg.clean_cmd, shell=True, cwd=str(worktree),
                           capture_output=True, timeout=cfg.test_timeout)
        build = subprocess.run(cfg.build_cmd, shell=True, cwd=str(worktree),
                               capture_output=True, text=True,
                               timeout=max(cfg.test_timeout, 600))
        if build.returncode != 0:
            return None
        base_binary = (worktree / cfg.binary if cfg.binary else None)
        # Run pytest against the base build
        from .executor import _build_env, _parse_junit_cases, resolve_python
        env = dict(_build_env(cfg))
        if base_binary is not None:
            env["AICOV_BINARY"] = str(base_binary)
            env["AICOV_SRC"] = str(worktree)
        junit = work_dir / "base_junit.xml"
        cmd = [resolve_python(cfg), "-m", "pytest",
               *[str(worktree / cfg.test_dirname / tf.name) for tf in test_files],
               "-v", "--junitxml", str(junit), "-p", "no:cacheprovider"]
        proc = subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True,
                              timeout=cfg.test_timeout, env=env)
        (work_dir / "base_pytest.log").write_text(
            (proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        if not junit.exists():
            return None
        return {"base_cases": _parse_junit_cases(junit)}
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        _run_git(cfg.source_path, ["worktree", "remove", "--force", str(worktree)])


def _run_git(cwd: Path, args: list[str]) -> int:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd),
                              capture_output=True, text=True, timeout=120)
        return proc.returncode
    except (subprocess.TimeoutExpired, OSError):
        return 1


def regression_verdicts(head_cases: dict[str, str],
                        base_cases: dict[str, str]) -> dict[str, str]:
    """Pure comparison: per-case base-vs-head verdict for failing head cases.

      pass@base + fail@head  -> regression_confirmed (the change introduced it)
      fail@base + fail@head  -> preexisting (not introduced by this change)
      missing on either side -> unknown
    """
    out: dict[str, str] = {}
    for name, status in head_cases.items():
        if status not in ("fail", "error"):
            continue
        b = base_cases.get(name)
        if b is None:
            out[name] = "unknown"
        elif b == "pass":
            out[name] = "regression_confirmed"
        else:
            out[name] = "preexisting"
    return out
