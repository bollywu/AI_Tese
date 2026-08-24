"""可观测性事件发射器（通用化）。

事件写入 <source>/.aicoverage/runs/<run_id>/events.jsonl（append + flock）。
确定性代码在每个状态机节点自动发事件，不依赖 LLM 记得发——这是"确定性驱动"
相对"LLM 驱动"的天然优势（经实战验证的设计）。

ROOT 从 ProjectConfig 读取（每个目标项目自己的 .aicoverage/ 目录），无全局单例。
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
    # task（LLM agent 调用）
    "task.call", "task.return", "task.retry", "task.backoff",
    "hallucination.detected", "context.compact",
    # artifact / coverage / execute
    "artifact.write", "artifact.missing",
    "coverage.snapshot", "coverage.delta",
    "execute.completed",
    # build
    "build.ok", "build.fail",
    # 诊断与恢复
    "diagnostic", "recovery.attempt", "recovery.action", "recovery.result",
    # 其它
    "warn", "error", "custom",
}

# 稳定诊断码表（消费方按 code 判定，不解析 message 文案）
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
    "EARLY_STOP":           {"severity": "low",    "title": "闭环早停"},
}

# 观测静默开关（单测防污染生产 runs/ 目录）
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
    # 兜底：source 目录约定（与 state.py 共用 runs_dir 传入，一般不会走到这里）
    from .config import find_config
    cfg_path = find_config()
    src = cfg_path.parent
    return src / ".aicoverage" / "runs" / run_id / "events.jsonl"


def emit(event_type: str, run_id: str, *, iter_n: int | None = None,
         stage: str | None = None, agent: str | None = None,
         data: dict[str, Any] | None = None, runs_dir: Path | None = None) -> dict[str, Any]:
    """发射一条事件。未知类型不抛异常（可观测性不能成为闭环单点故障）。"""
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
    """prompt 内容锚点（sha256 + 长度）——model-visible ⟺ logged 不变式。"""
    return {
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "length": len(prompt),
    }
