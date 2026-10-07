"""看板索引层单测：三类 run 识别、摘要字段、半写容错（docs/PLAN_dashboard.md §2.6）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.dashboard import store  # noqa: E402


def _mk_loop_run(runs_dir: Path, run_id: str, **over) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    state = {
        "run_id": run_id,
        "trigger": {"type": "manual"},
        "thresholds": {"func_pct": 100.0, "cond_pct": 85.0},
        "limits": {"max_iter": 6, "max_verify_retry": 2, "no_progress_iters": 2},
        "iterations": [],
        "current_iter": 2,
        "status": "done",
        "exit_reason": "threshold_met",
        "final_metrics": {"func_pct": 100.0, "cond_pct": 90.0},
        "usage": {"cost_usd": 1.5, "total_tokens": 12345},
        "created_at": "2026-10-06T10:00:00",
        "finished_at": "2026-10-06T10:05:00",
    }
    state.update(over)
    (d / "loop_state.json").write_text(json.dumps(state), encoding="utf-8")
    return d


def _mk_mr_run(runs_dir: Path, run_id: str) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    (d / "mr_summary.json").write_text(json.dumps({
        "master_run_id": run_id,
        "base_ref": "main", "head_ref": "HEAD",
        "thresholds": {"func_pct": 100.0, "cond_pct": 85.0},
        "status": "done", "exit_reason": "all_batches_met",
        "coverage_batches": [{"batch_index": 0, "run_id": "RUN_1"},
                             {"batch_index": 1, "run_id": "RUN_2"}],
        "scan": None,
    }), encoding="utf-8")
    return d


def _mk_analyze_run(runs_dir: Path, run_id: str) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    (d / "analysis.md").write_text("# 需求分析", encoding="utf-8")
    return d


class TestScanRuns:
    def test_empty(self, tmp_path):
        assert store.scan_runs(tmp_path / "nope") == []

    def test_three_run_types(self, tmp_path):
        runs = tmp_path / "runs"
        _mk_loop_run(runs, "RUN_20261006_100000")
        _mk_mr_run(runs, "MR_20261006_110000")
        _mk_analyze_run(runs, "RUN_20261006_090000")
        # 空目录（崩溃在写任何产物前）应被跳过
        (runs / "RUN_empty").mkdir(parents=True)

        out = {r["run_id"]: r for r in store.scan_runs(runs)}
        assert set(out) == {"RUN_20261006_100000", "MR_20261006_110000",
                            "RUN_20261006_090000"}
        assert out["RUN_20261006_100000"]["type"] == store.RUN_TYPE_LOOP
        assert out["MR_20261006_110000"]["type"] == store.RUN_TYPE_MR
        assert out["RUN_20261006_090000"]["type"] == store.RUN_TYPE_ANALYZE
        assert out["RUN_20261006_090000"]["status"] == "analyze-only"

    def test_loop_summary_fields(self, tmp_path):
        runs = tmp_path / "runs"
        _mk_loop_run(runs, "RUN_1")
        (r,) = store.scan_runs(runs)
        assert r["status"] == "done"
        assert r["exit_reason"] == "threshold_met"
        assert r["current_iter"] == 2 and r["max_iter"] == 6
        assert r["duration_sec"] == 300.0
        assert r["usage"] == {"cost_usd": 1.5, "total_tokens": 12345}
        assert r["final_metrics"]["func_pct"] == 100.0

    def test_mr_summary_fields_and_batches(self, tmp_path):
        runs = tmp_path / "runs"
        _mk_mr_run(runs, "MR_1")
        (r,) = store.scan_runs(runs)
        assert r["status"] == "done"
        assert r["base_ref"] == "main" and r["head_ref"] == "HEAD"
        assert r["batch_count"] == 2
        assert r["batch_run_ids"] == ["RUN_1", "RUN_2"]

    def test_running_run_without_finished_at(self, tmp_path):
        """崩溃/被 kill 的 run 缺 finished_at：running 态按 now 计算，非 running 为 None。"""
        runs = tmp_path / "runs"
        _mk_loop_run(runs, "RUN_live", status="running", finished_at="")
        (r,) = store.scan_runs(runs)
        assert r["duration_sec"] is not None and r["duration_sec"] >= 0

        _mk_loop_run(runs, "RUN_dead", status="error", finished_at="")
        out = {x["run_id"]: x for x in store.scan_runs(runs)}
        assert out["RUN_dead"]["duration_sec"] is None

    def test_corrupt_state_skipped_not_fatal(self, tmp_path):
        runs = tmp_path / "runs"
        d = runs / "RUN_bad"
        d.mkdir(parents=True)
        (d / "loop_state.json").write_text("{not json", encoding="utf-8")
        _mk_loop_run(runs, "RUN_ok")
        out = store.scan_runs(runs)
        assert [r["run_id"] for r in out] == ["RUN_ok"]


class TestGetRun:
    def test_loop_detail_contains_state(self, tmp_path):
        runs = tmp_path / "runs"
        _mk_loop_run(runs, "RUN_1")
        d = store.get_run(runs, "RUN_1")
        assert d["type"] == store.RUN_TYPE_LOOP
        assert d["state"]["run_id"] == "RUN_1"

    def test_mr_detail_contains_summary(self, tmp_path):
        runs = tmp_path / "runs"
        _mk_mr_run(runs, "MR_1")
        d = store.get_run(runs, "MR_1")
        assert d["type"] == store.RUN_TYPE_MR
        assert d["summary"]["master_run_id"] == "MR_1"

    def test_missing_run(self, tmp_path):
        assert store.get_run(tmp_path, "RUN_nope") is None
