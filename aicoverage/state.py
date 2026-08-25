"""loop_state.json state management (runs_dir parameterized, project-agnostic).

Single source of truth for all loop state: read before each update, write back after.
Contract fields (thresholds/limits/iterations/coverage_after/delta) stay stable;
runs_dir is passed by the caller (each target project is independent).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def gen_run_id(prefix: str = "RUN") -> str:
    return f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"


def runs_root(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / run_id


def iter_dir(runs_dir: Path, run_id: str, iter_n: int) -> Path:
    return runs_root(runs_dir, run_id) / f"iter_{iter_n}"


def init_loop_state(
    runs_dir: Path,
    run_id: str,
    trigger_type: str,
    thresholds: dict[str, float] | None = None,
    limits: dict[str, int] | None = None,
    requirement: str = "",
) -> dict:
    thresholds = thresholds or {"func_pct": 100.0, "cond_pct": 85.0}
    limits = limits or {"max_iter": 6, "max_verify_retry": 2, "no_progress_iters": 2}
    state = {
        "run_id": run_id,
        "trigger": {"type": trigger_type},
        "requirement": requirement,
        "thresholds": thresholds,
        "limits": limits,
        "iterations": [],
        "current_iter": 0,
        "status": "running",
        "exit_reason": "",
        "final_metrics": {},
        "created_at": datetime.now().isoformat(),
    }
    save_loop_state(runs_dir, run_id, state)
    runs_root(runs_dir, run_id).mkdir(parents=True, exist_ok=True)
    iter_dir(runs_dir, run_id, 1).mkdir(parents=True, exist_ok=True)
    return state


def load_loop_state(runs_dir: Path, run_id: str) -> dict:
    path = runs_root(runs_dir, run_id) / "loop_state.json"
    if not path.exists():
        raise FileNotFoundError(f"loop_state.json 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_loop_state(runs_dir: Path, run_id: str, state: dict) -> None:
    path = runs_root(runs_dir, run_id) / "loop_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def start_iteration(runs_dir: Path, run_id: str, iter_n: int) -> dict:
    state = load_loop_state(runs_dir, run_id)
    iter_dir(runs_dir, run_id, iter_n).mkdir(parents=True, exist_ok=True)
    state["current_iter"] = iter_n
    existing = {it["iter"] for it in state["iterations"]}
    if iter_n not in existing:
        state["iterations"].append({
            "iter": iter_n,
            "started_at": datetime.now().isoformat(),
            "stage": "started",
        })
    save_loop_state(runs_dir, run_id, state)
    return state


def update_iteration(runs_dir: Path, run_id: str, iter_n: int, updates: dict[str, Any]) -> dict:
    state = load_loop_state(runs_dir, run_id)
    for it in state["iterations"]:
        if it["iter"] == iter_n:
            it.update(updates)
            break
    save_loop_state(runs_dir, run_id, state)
    return state


def update_state(runs_dir: Path, run_id: str, updates: dict[str, Any]) -> dict:
    """Update top-level fields (e.g. MR mode writing scope metadata)."""
    state = load_loop_state(runs_dir, run_id)
    for k, v in updates.items():
        state[k] = v
    save_loop_state(runs_dir, run_id, state)
    return state


def set_exit(runs_dir: Path, run_id: str, status: str, exit_reason: str,
             final_metrics: dict | None = None) -> dict:
    state = load_loop_state(runs_dir, run_id)
    state["status"] = status
    state["exit_reason"] = exit_reason
    if final_metrics:
        state["final_metrics"].update(final_metrics)
    state["finished_at"] = datetime.now().isoformat()
    save_loop_state(runs_dir, run_id, state)
    return state


def check_threshold(state: dict, iter_n: int) -> bool:
    """Threshold check: func_pct and cond_pct both meet their thresholds."""
    current = next((it for it in state["iterations"] if it["iter"] == iter_n), None)
    if not current:
        return False
    cov_after = current.get("coverage_after", {})
    thresholds = state["thresholds"]
    return (cov_after.get("func_pct", 0) >= thresholds["func_pct"]
            and cov_after.get("cond_pct", 0) >= thresholds["cond_pct"])


def check_early_stop(state: dict) -> str | None:
    """Early-stop conditions:
    - current_iter >= max_iter -> max_iter_reached
    - no early stop when current_iter < 2
    - no_progress_iters consecutive rounds with execute_verdict=FAIL -> execute_fail_loop
    - no_progress_iters consecutive rounds with no coverage growth (and execute not FAIL) -> coverage_ceiling
    """
    limits = state["limits"]
    current_iter = state["current_iter"]
    if current_iter >= limits["max_iter"]:
        return "max_iter_reached"
    if current_iter < 2:
        return None
    iterations = sorted(state["iterations"], key=lambda x: x["iter"])
    recent = iterations[-limits["no_progress_iters"]:]
    if len(recent) >= limits["no_progress_iters"]:
        if all(it.get("execute_verdict") == "FAIL" for it in recent):
            return "execute_fail_loop"
        if all(it.get("delta", {}).get("func_pp", 0) <= 0
               and it.get("delta", {}).get("cond_pp", 0) <= 0
               and it.get("execute_verdict") != "FAIL" for it in recent):
            return "coverage_ceiling"
    return None
