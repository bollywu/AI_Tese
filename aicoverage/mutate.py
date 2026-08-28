"""Mutation self-check (P3, plan 1.3): catch tests that PASS even when the program
under test is dead -- the definitive false-positive detector.

Principle: replace the instrumented binary with a "dead version" (/bin/true: any
invocation returns rc=0 with empty output) and re-run the round's new cases. A
case that STILL PASSES against a dead program cannot be verifying its behavior:

  - weak/tautological assertions (assert True, assert_eq(a,a), a needle that
    matches empty output, ...) pass trivially
  - cases whose preconditions silently skip (fixture skip would show as skipped,
    not pass -- those are excluded)

Usage:
    aicov mutate                     # latest run, latest iteration
    aicov mutate --run-id LOOP_xxx   # specific run
    aicov mutate --iter 2            # specific iteration of that run

Notes:
  - Unit-channel cases (compile_unit_driver/run_driver) are EXCLUDED: they compile
    their own driver binary, so mutating the instrumented binary means nothing to
    them (a false "still passes" would be a wrong accusation).
  - The original binary is always restored (try/finally), even on interruption.
  - Go projects are unsupported (no instrumented binary concept; go test runs the
    real code) -- the command exits with a clear message.
"""
from __future__ import annotations

import ast
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import ProjectConfig

_UNIT_CHANNEL_FUNCS = ("compile_unit_driver", "run_driver")

#: A minimal executable that exits 0 and prints nothing -- "the program is dead".
_DEAD_BINARY = Path("/bin/true")


@dataclass
class MutationResult:
    ok: bool
    run_id: str = ""
    iter_n: int = 0
    checked: list[str] = field(default_factory=list)      # case names verified
    suspicious: list[str] = field(default_factory=list)   # still PASS against dead binary
    skipped_cases: list[str] = field(default_factory=list)
    unit_cases: list[str] = field(default_factory=list)   # excluded unit-channel cases
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "run_id": self.run_id, "iter": self.iter_n,
            "checked": self.checked, "suspicious": self.suspicious,
            "skipped": self.skipped_cases, "unit_excluded": self.unit_cases,
            "detail": self.detail,
        }


def _unit_channel_functions(path: Path) -> set[str]:
    """Test-function names in `path` that exercise the unit channel (AST scan)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                fname = (n.func.id if isinstance(n.func, ast.Name)
                         else (n.func.attr if isinstance(n.func, ast.Attribute) else ""))
                if fname in _UNIT_CHANNEL_FUNCS:
                    out.add(node.name)
                    break
    return out


def _resolve_iter(cfg: ProjectConfig, run_id: str | None, iter_n: int | None,
                  ) -> tuple[Path | None, str, int, str]:
    """Resolve (iter_dir, run_id, iter_n, error). Latest-run/latest-iter defaults."""
    runs_dir = cfg.runs_dir
    if not runs_dir.is_dir():
        return None, "", 0, f"无 runs 目录: {runs_dir}（先跑一次 aicov loop）"
    if run_id is None:
        runs = sorted((d for d in runs_dir.iterdir()
                       if d.is_dir() and (d.name.startswith("LOOP_") or d.name.startswith("MR_"))),
                      key=lambda d: d.name)
        if not runs:
            return None, "", 0, f"{runs_dir} 下没有 LOOP_/MR_ run"
        run_dir = runs[-1]
        run_id = run_dir.name
    else:
        run_dir = runs_dir / run_id
        if not run_dir.is_dir():
            return None, run_id, 0, f"run 不存在: {run_id}"
    iters = sorted((d for d in run_dir.iterdir()
                    if d.is_dir() and d.name.startswith("iter_")),
                   key=lambda d: int(d.name.split("_")[1]))
    if not iters:
        return None, run_id, 0, f"{run_id} 无 iter_N 目录"
    if iter_n is None:
        iter_dir = iters[-1]
        iter_n = int(iter_dir.name.split("_")[1])
    else:
        iter_dir = run_dir / f"iter_{iter_n}"
        if not iter_dir.is_dir():
            return None, run_id, iter_n, f"{run_id} 无 iter_{iter_n}"
    return iter_dir, run_id, iter_n, ""


def run_mutation_check(cfg: ProjectConfig, *, run_id: str | None = None,
                       iter_n: int | None = None) -> MutationResult:
    """Re-run the round's new cases against a dead binary; still-PASS = suspect.

    C/C++ only. The binary is swapped for /bin/true for the duration of the run
    and always restored. Writes mutate_report.json into the resolved iter dir.
    """
    result = MutationResult(ok=False)
    if getattr(cfg, "language", "c") == "go":
        result.detail = ("Go 项目无被测二进制概念（go test 直接跑真实代码），"
                         "变异自检不适用")
        return result
    binary = cfg.binary_path
    if binary is None or not binary.exists():
        result.detail = f"被测二进制不存在: {binary}（先 aicov build）"
        return result
    if not _DEAD_BINARY.exists():
        result.detail = f"失效替身不存在: {_DEAD_BINARY}"
        return result

    iter_dir, run_id, iter_n, err = _resolve_iter(cfg, run_id, iter_n)
    if iter_dir is None:
        result.detail = err
        return result
    result.run_id, result.iter_n = run_id, iter_n

    manifest_path = iter_dir / "manifest.json"
    if not manifest_path.exists():
        result.detail = f"{run_id}/iter_{iter_n} 无 manifest.json（该轮未产出用例）"
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        result.detail = f"manifest.json 解析失败: {manifest_path}"
        return result

    test_files = [f for f in manifest.get("test_files", []) or []
                  if (cfg.test_dir / f).exists()]
    if not test_files:
        result.detail = "该轮 manifest 无可用测试文件"
        return result

    # split unit-channel cases out (they compile their own driver; dead-binary
    # mutation is meaningless to them)
    unit_fns: set[str] = set()
    for f in test_files:
        unit_fns |= _unit_channel_functions(cfg.test_dir / f)
    result.unit_cases = sorted(unit_fns)

    mut_dir = cfg.workspace / "mutate" / f"{run_id}_iter{iter_n}"
    mut_dir.mkdir(parents=True, exist_ok=True)

    # local config copy with flaky_rerun off (a second run under mutation is a
    # waste -- dead-binary results are deterministic)
    import copy
    mcfg = copy.copy(cfg)
    mcfg.flaky_rerun = False

    backup = mut_dir / f"{binary.name}.orig"
    try:
        shutil.copy2(binary, backup)
        shutil.copy2(_DEAD_BINARY, binary)
        from .executor import run_tests
        res = run_tests(mcfg, mut_dir,
                        test_files=[cfg.test_dir / f for f in test_files],
                        collect_coverage=False)
    except OSError as e:
        result.detail = f"二进制替换失败: {e}"
        return result
    finally:
        if backup.exists():
            shutil.copy2(backup, binary)  # restore the real binary, always
            backup.unlink(missing_ok=True)

    result.checked = sorted(res.cases)
    result.skipped_cases = sorted(n for n, s in res.cases.items() if s == "skipped")
    # The core verdict: still-PASS against a dead program = the case verified nothing
    result.suspicious = sorted(
        n for n, s in res.cases.items()
        if s == "pass" and n not in unit_fns
    )
    result.ok = True
    fail_n = sum(1 for n, s in res.cases.items()
                 if s in ("fail", "error") and n not in unit_fns)
    result.detail = (
        f"变异环境（失效二进制）重跑 {len(res.cases)} 个用例："
        f"{fail_n} 个如预期失败，{len(result.suspicious)} 个仍 PASS（假阳性嫌疑），"
        f"{len(result.skipped_cases)} 个跳过，{len(result.unit_cases)} 个单测通道用例已排除"
    )
    (iter_dir / "mutate_report.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return result
