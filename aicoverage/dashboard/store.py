"""Run 索引层：只读扫描 <source>/.aicoverage/runs/，输出看板消费的结构化摘要。

对应 docs/PLAN_dashboard.md §2.2 / §2.6：

- 三类 run 识别（不能只扫 loop_state.json，MR 主 run 没有它）：
  loop run（loop_state.json）/ MR master run（mr_summary.json）/
  analyze-only（仅 analysis.md，与 cli.py:_cmd_report 行为一致）
- 只读 + 半写容错：loop_state.json 为整文件重写，读到半写 JSON 时重试
- 懒加载：scan_runs() 只读状态/摘要文件，不解析 iter_N/coverage.json 等大产物
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

RUN_TYPE_LOOP = "loop"
RUN_TYPE_MR = "mr"
RUN_TYPE_ANALYZE = "analyze"


def _read_json(path: Path, retries: int = 2) -> dict[str, Any] | None:
    """读 JSON 文件；半写（闭环正在整文件重写）时短暂重试，失败返回 None。"""
    for attempt in range(retries + 1):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if attempt < retries:
                time.sleep(0.05)
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_sec(state: dict[str, Any]) -> float | None:
    """耗时（秒）：finished_at 缺失（崩溃/被 kill 的 run）时按运行中处理。"""
    created = _parse_ts(state.get("created_at"))
    if created is None:
        return None
    finished = _parse_ts(state.get("finished_at"))
    end = finished or (datetime.now() if state.get("status") == "running" else None)
    if end is None:
        return None
    return round((end - created).total_seconds(), 1)


def _loop_summary(run_id: str, state: dict[str, Any]) -> dict[str, Any]:
    limits = state.get("limits") or {}
    usage = state.get("usage") or {}
    return {
        "run_id": state.get("run_id", run_id),
        "type": RUN_TYPE_LOOP,
        "status": state.get("status", "unknown"),
        "exit_reason": state.get("exit_reason", ""),
        "created_at": state.get("created_at", ""),
        "finished_at": state.get("finished_at", ""),
        "duration_sec": _duration_sec(state),
        "current_iter": state.get("current_iter", 0),
        "max_iter": limits.get("max_iter"),
        "thresholds": state.get("thresholds") or {},
        "final_metrics": state.get("final_metrics") or {},
        "usage": {
            "cost_usd": float(usage.get("cost_usd", 0.0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        },
        "requirement": (state.get("requirement") or "")[:200],
    }


def _mr_summary(run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    batches = summary.get("coverage_batches") or []
    return {
        "run_id": summary.get("master_run_id", run_id),
        "type": RUN_TYPE_MR,
        "status": summary.get("status", "unknown"),
        "exit_reason": summary.get("exit_reason", ""),
        "created_at": "",
        "finished_at": "",
        "duration_sec": None,
        "base_ref": summary.get("base_ref", ""),
        "head_ref": summary.get("head_ref", ""),
        "thresholds": summary.get("thresholds") or {},
        "batch_count": len(batches),
        "batch_run_ids": [b.get("run_id") for b in batches if b.get("run_id")],
        "scan": summary.get("scan"),
    }


def scan_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """列出全部 run（新→旧）。列表数据与 `aicov report --list` 同源同义。"""
    out: list[dict[str, Any]] = []
    if not runs_dir.exists():
        return out
    for d in runs_dir.iterdir():
        if not d.is_dir():
            continue
        state_file = d / "loop_state.json"
        mr_file = d / "mr_summary.json"
        if state_file.exists():
            state = _read_json(state_file)
            if state is not None:
                out.append(_loop_summary(d.name, state))
        elif mr_file.exists():
            summary = _read_json(mr_file)
            if summary is not None:
                out.append(_mr_summary(d.name, summary))
        elif (d / "analysis.md").exists():
            out.append({
                "run_id": d.name,
                "type": RUN_TYPE_ANALYZE,
                "status": "analyze-only",
                "exit_reason": "",
                "created_at": "",
                "finished_at": "",
                "duration_sec": None,
            })
    out.sort(key=lambda r: (r.get("created_at") or "", r["run_id"]), reverse=True)
    return out


def get_run(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    """单个 run 详情：摘要 + 原始状态/摘要 JSON。run 不存在返回 None。"""
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        return None
    state_file = run_dir / "loop_state.json"
    mr_file = run_dir / "mr_summary.json"
    if state_file.exists():
        state = _read_json(state_file)
        if state is None:
            return None
        detail = _loop_summary(run_id, state)
        detail["state"] = state
        return detail
    if mr_file.exists():
        summary = _read_json(mr_file)
        if summary is None:
            return None
        detail = _mr_summary(run_id, summary)
        detail["summary"] = summary
        return detail
    if (run_dir / "analysis.md").exists():
        return {
            "run_id": run_id,
            "type": RUN_TYPE_ANALYZE,
            "status": "analyze-only",
            "exit_reason": "",
            "created_at": "",
            "finished_at": "",
            "duration_sec": None,
        }
    return None
