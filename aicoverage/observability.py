"""Observability event emitter (generalized).

Events are written to <source>/.aicoverage/runs/<run_id>/events.jsonl (append + flock).
Deterministic code emits events automatically at each state-machine node, without
relying on the LLM to remember -- this is the natural advantage of "deterministic
drive" over "LLM drive" (a design validated in production).

ROOT is read from ProjectConfig (each target project's own .aicoverage/ dir); no global singleton.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import socket
from pathlib import Path
from typing import Any

VALID_EVENT_TYPES = {
    # loop
    "loop.start", "loop.exit", "loop.threshold_met", "loop.early_stop", "loop.error",
    # stage
    "stage.enter", "stage.exit", "stage.timeout",
    # task (LLM agent call)
    "task.call", "task.return", "task.retry", "task.backoff",
    "hallucination.detected", "context.compact",
    # artifact / coverage / execute
    "artifact.write", "artifact.missing",
    "coverage.snapshot", "coverage.delta",
    "execute.completed",
    # build
    "build.ok", "build.fail",
    # diagnostics & recovery
    "diagnostic", "recovery.attempt", "recovery.action", "recovery.result",
    # others
    "warn", "error", "custom",
}

# Stable diagnostic code table (consumers decide by code, not by parsing message text)
DIAGNOSTIC_CODES: dict[str, dict[str, str]] = {
    "AGENT_HALLUCINATION":  {"severity": "medium", "title": "子 agent 幻觉检测（tool_uses=0 触发重试）"},
    "AGENT_FAILED":         {"severity": "high",   "title": "子 agent 调用失败（含重试耗尽）"},
    "AGENT_RATE_LIMIT":     {"severity": "medium", "title": "子 agent 命中 429 限流（触发指数退避重试）"},
    "AGENT_TRANSIENT":      {"severity": "medium", "title": "子 agent 命中 5xx/网络瞬时错误（触发退避重试）"},
    "AGENT_NON_RETRYABLE":  {"severity": "high",   "title": "子 agent 命中不可重试错误（直接放弃）"},
    "AGENT_CONTEXT_OVERFLOW": {"severity": "high", "title": "子 agent 上下文/轮次耗尽（原样重试无意义）"},
    "BUILD_FAIL":           {"severity": "high",   "title": "插桩构建失败"},
    "NO_GCNO":              {"severity": "high",   "title": "构建成功但无 .gcno（插桩未生效）"},
    "EXECUTE_TIMEOUT":      {"severity": "high",   "title": "pytest 执行超时"},
    "EXECUTE_BLOCKED":      {"severity": "high",   "title": "pytest 未正常执行（环境阻塞）"},
    "GEN_NO_OUTPUT":        {"severity": "medium", "title": "gen-agent 未产出新用例"},
    "VERIFY_FAIL_EXCEEDED": {"severity": "medium", "title": "verify 失败次数超限"},
    "EXECUTE_FAIL_LOOP":    {"severity": "high",   "title": "连续多轮执行失败"},
    "COVERAGE_CEILING":     {"severity": "medium", "title": "覆盖率连续无增长（天花板）"},
    "MISSING_ARTIFACT":     {"severity": "medium", "title": "预期产物缺失"},
    "UNIT_CONFIRM_PENDING": {"severity": "medium", "title": "存在待人工确认的单测覆盖（E2E-first 门禁）"},
    "EARLY_STOP":           {"severity": "low",    "title": "闭环早停"},
}

# Observation silence switch (unit tests avoid polluting production runs/ dir)
_SILENT_ENV = "AICOV_OBS_SILENT"


def emit_diagnostic(code: str, run_id: str, message: str, *, severity: str | None = None,
                    iter_n: int | None = None, stage: str | None = None,
                    agent: str | None = None, context: dict[str, Any] | None = None,
                    runs_dir: Path | None = None) -> dict[str, Any]:
    if code not in DIAGNOSTIC_CODES:
        print(f"    [diagnostic] 未知诊断码: {code!r}（仍写入）")
        default_sev = severity or "medium"
    else:
        default_sev = severity or DIAGNOSTIC_CODES[code]["severity"]
    data: dict[str, Any] = {"code": code, "severity": default_sev, "message": message}
    if context:
        data["context"] = context
    return emit("diagnostic", run_id, iter_n=iter_n, stage=stage, agent=agent,
                data=data, runs_dir=runs_dir)


def emit_recovery(phase: str, run_id: str, *, action: str | None = None,
                  result: str | None = None, reason: str | None = None,
                  iter_n: int | None = None, stage: str | None = None,
                  agent: str | None = None, runs_dir: Path | None = None) -> dict[str, Any]:
    event_type = f"recovery.{phase}"
    if event_type not in VALID_EVENT_TYPES:
        print(f"    [observability] 未知恢复阶段: {phase!r}（仍写入）")
        event_type = "diagnostic"
    data: dict[str, Any] = {}
    if reason is not None:
        data["reason"] = reason
    if action is not None:
        data["action"] = action
    if result is not None:
        data["result"] = result
    return emit(event_type, run_id, iter_n=iter_n, stage=stage, agent=agent,
                data=data, runs_dir=runs_dir)


def _events_path(run_id: str, runs_dir: Path | None) -> Path:
    if runs_dir is not None:
        return runs_dir / run_id / "events.jsonl"
    # fallback: source-dir convention (runs_dir is passed like state.py; normally not reached)
    from .config import find_config
    cfg_path = find_config()
    src = cfg_path.parent
    return src / ".aicoverage" / "runs" / run_id / "events.jsonl"


def emit(event_type: str, run_id: str, *, iter_n: int | None = None,
         stage: str | None = None, agent: str | None = None,
         data: dict[str, Any] | None = None, runs_dir: Path | None = None) -> dict[str, Any]:
    """Emit one event. Unknown types do not raise (observability must not be the loop's single point of failure)."""
    if event_type not in VALID_EVENT_TYPES:
        print(f"    [observability] 未知事件类型: {event_type!r}（仍写入，不阻断流程）")

    event: dict[str, Any] = {
        "ts": dt.datetime.now().isoformat(timespec="milliseconds"),
        "type": event_type,
        "run_id": run_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
    }
    if iter_n is not None:
        event["iter"] = iter_n
    if stage is not None:
        event["stage"] = stage
    if agent is not None:
        event["agent"] = agent
    if data:
        event["data"] = data

    if os.environ.get(_SILENT_ENV, "").strip() in ("1", "true", "yes"):
        return event

    path = _events_path(run_id, runs_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(line)
                f.flush()
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError as e:
        print(f"    [observability] 写事件失败（忽略，不影响闭环）: {e}")
    return event


def prompt_anchor(prompt: str) -> dict[str, Any]:
    """Prompt content anchor (sha256 + length) -- the model-visible ⟺ logged invariant."""
    return {
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "length": len(prompt),
    }
