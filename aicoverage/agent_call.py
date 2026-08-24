"""子 agent 调用统一封装（通用化）。

实战验证过的机制：
- 失败分类（正交事实集合）：rate_limit / transient / non_retryable /
  hallucination / context_overflow
- 幻觉误标治理（2026-08-21）：幻觉判定排除一切可识别基础设施异常，
  只有"纯净的 tool_uses=0"才是真幻觉
- 指数退避 + jitter + 总时长闸门（429 立即重试会加剧限流的教训）
- context_overflow 摘要重启（compact_hook）支持
- 事件流：task.call / task.return / task.retry / task.backoff /
  hallucination.detected / diagnostic / recovery.*

环境变量前缀统一为 AICOV_。
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from . import observability as obs
from .runner import AgentRunner, AgentRunResult


class FailureClass(Enum):
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    NON_RETRYABLE = "non_retryable"
    HALLUCINATION = "hallucination"
    CONTEXT_OVERFLOW = "context_overflow"


@dataclass(frozen=True)
class FailureClassification:
    hallucinated: bool = False
    rate_limited: bool = False
    non_retryable: bool = False
    transient: bool = False
    context_overflow: bool = False
    primary: FailureClass = FailureClass.TRANSIENT

    @property
    def facts(self) -> dict[str, bool]:
        return {
            "hallucinated": self.hallucinated,
            "rate_limited": self.rate_limited,
            "non_retryable": self.non_retryable,
            "transient": self.transient,
            "context_overflow": self.context_overflow,
        }


_RATE_LIMIT_KEYWORDS = ("429", "too many requests", "rate limit", "ratelimit",
                        "quota exceeded", "限流", "请求过多")
_TRANSIENT_KEYWORDS = ("500", "502", "503", "504", "timeout", "timed out",
                       "connection", "network", "unavailable", "超时", "连接",
                       "no response", "broken pipe")
_NON_RETRYABLE_KEYWORDS = ("400", "401", "403", "404", "invalid", "参数",
                           "authentication", "unauthorized", "not found")
_CONTEXT_OVERFLOW_KEYWORDS = (
    "max turns", "context length", "maximum context", "context window",
    "prompt is too long", "prompt too long", "input too large",
    "token limit", "too many tokens", "request too large",
    "轮次超限", "上下文超限",
)


class _RetryBackoffConfig:
    """退避参数（AICOV_RETRY_* 环境变量可覆盖）。"""

    def __init__(self) -> None:
        self.base_delay = self._env_float("AICOV_RETRY_BASE_DELAY", 15.0)
        self.max_delay = self._env_float("AICOV_RETRY_MAX_DELAY", 300.0)
        self.total_timeout = self._env_float("AICOV_RETRY_TOTAL_TIMEOUT", 600.0)

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def next_delay(self, attempt: int, cls: FailureClass) -> float:
        base = self.base_delay if cls is FailureClass.RATE_LIMIT else max(self.base_delay / 3.0, 2.0)
        exp = min(base * (2 ** (attempt - 1)), self.max_delay)
        return round(exp + random.uniform(0, exp * 0.2), 1)


_backoff_config = _RetryBackoffConfig()
_backoff_elapsed: dict[str, float] = {}
_COMPACT_MAX_PER_CALL = 2


def _ctx_pressure_threshold() -> float:
    raw = os.environ.get("AICOV_CTX_PRESSURE_TOKENS", "").strip()
    if not raw:
        return 800_000.0
    try:
        return float(raw)
    except ValueError:
        return 800_000.0


def _classify_facts(result: AgentRunResult) -> FailureClassification:
    summary = (result.summary or "").lower()
    rate_limited = any(kw in summary for kw in _RATE_LIMIT_KEYWORDS)
    non_retryable = any(kw in summary for kw in _NON_RETRYABLE_KEYWORDS)
    transient = any(kw in summary for kw in _TRANSIENT_KEYWORDS)
    context_overflow = any(kw in summary for kw in _CONTEXT_OVERFLOW_KEYWORDS)

    is_timeout = transient and ("timeout" in summary or "超时" in summary or "挂起" in summary)
    # 幻觉判定排除一切可识别异常（2026-08-21 幻觉误标治理结论）
    hallucinated = (result.tool_uses == 0
                    and not is_timeout and not context_overflow
                    and not rate_limited and not non_retryable and not transient)

    if context_overflow:
        primary = FailureClass.CONTEXT_OVERFLOW
    elif is_timeout:
        primary = FailureClass.TRANSIENT
    elif hallucinated:
        primary = FailureClass.HALLUCINATION
    elif rate_limited:
        primary = FailureClass.RATE_LIMIT
    elif non_retryable:
        primary = FailureClass.NON_RETRYABLE
    elif transient:
        primary = FailureClass.TRANSIENT
    else:
        primary = FailureClass.TRANSIENT
        transient = True
    return FailureClassification(
        hallucinated=hallucinated, rate_limited=rate_limited,
        non_retryable=non_retryable, transient=transient,
        context_overflow=context_overflow, primary=primary,
    )


async def call_agent(
    runner: AgentRunner,
    run_id: str,
    agent_name: str,
    prompt: str,
    *,
    runs_dir: Path,
    iter_n: int | None = None,
    stage: str | None = None,
    max_turns: int | None = None,
    permission_mode: str | None = None,
    max_retries: int = 1,
    compact_hook: "Callable[[AgentRunResult], str | None] | None" = None,
    prompt_override: str | None = None,
) -> AgentRunResult:
    """调用子 agent：事件发射 + 失败分类 + 退避重试。

    Args:
        max_retries: 最大尝试次数（含首次）。verify 等关键阶段建议 3。
        compact_hook: 上下文溢出/高 token 压力时的摘要重启钩子。
        prompt_override: 透传 runner.run_agent 的 system prompt 整份替换
            （扫描轨 gen 变体用）。
    """
    result: AgentRunResult | None = None
    attempt = 1
    compact_used = 0
    while True:
        obs.emit("task.call", run_id, iter_n=iter_n, stage=stage, agent=agent_name,
                 data={"attempt": attempt, "max_retries": max_retries,
                       "prompt": obs.prompt_anchor(prompt)},
                 runs_dir=runs_dir)
        if attempt > 1:
            obs.emit_recovery("attempt", run_id, stage=stage, agent=agent_name,
                              iter_n=iter_n, runs_dir=runs_dir,
                              reason=f"{agent_name} 第 {attempt - 1} 次调用失败，重试第 {attempt} 次")
        result = await runner.run_agent(
            agent_name, prompt, max_turns=max_turns, permission_mode=permission_mode,
            prompt_override=prompt_override,
        )
        obs.emit("task.return", run_id, iter_n=iter_n, stage=stage, agent=agent_name,
                 data={
                     "attempt": attempt, "tool_uses": result.tool_uses,
                     "duration_ms": result.duration_ms, "success": result.success,
                     "input_tokens": result.input_tokens,
                     "output_tokens": result.output_tokens,
                     "total_tokens": result.total_tokens,
                     "tool_calls": [{"name": tc.name, "detail": tc.detail, "error": tc.is_error}
                                    for tc in result.tool_calls],
                 }, runs_dir=runs_dir)
        if result.success:
            return result

        classification = _classify_facts(result)
        cls = classification.primary
        is_hallucination = classification.hallucinated

        if cls is FailureClass.NON_RETRYABLE:
            code = "AGENT_NON_RETRYABLE"
        elif cls is FailureClass.RATE_LIMIT:
            code = "AGENT_RATE_LIMIT"
        elif cls is FailureClass.TRANSIENT:
            code = "AGENT_TRANSIENT"
        elif cls is FailureClass.CONTEXT_OVERFLOW:
            code = "AGENT_CONTEXT_OVERFLOW"
        else:
            code = "AGENT_HALLUCINATION"
        obs.emit_diagnostic(code, run_id,
                            message=f"{agent_name} 第 {attempt} 次调用失败: {result.summary}",
                            iter_n=iter_n, stage=stage, agent=agent_name,
                            context={"attempt": attempt, "max_retries": max_retries,
                                     "tool_uses": result.tool_uses,
                                     "failure_class": cls.value,
                                     "facts": classification.facts},
                            runs_dir=runs_dir)
        if is_hallucination:
            obs.emit("hallucination.detected", run_id, iter_n=iter_n, stage=stage,
                     agent=agent_name,
                     data={"attempt": attempt, "reason": result.summary or "tool_uses=0"},
                     runs_dir=runs_dir)
        print(f"    ⚠️ {agent_name} 第 {attempt} 次调用失败[{cls.value}]: {result.summary}")

        # 摘要重启：上下文溢出 / 高 token 压力时原样重试无意义
        ctx_pressure = result.total_tokens >= _ctx_pressure_threshold()
        if compact_hook is not None and (cls is FailureClass.CONTEXT_OVERFLOW or ctx_pressure):
            if compact_used < _COMPACT_MAX_PER_CALL:
                try:
                    new_prompt = compact_hook(result)
                except Exception as hook_err:  # noqa: BLE001
                    print(f"    ⚠️ compact_hook 执行异常（按原分类处理）: {hook_err}")
                    new_prompt = None
                if new_prompt:
                    compact_used += 1
                    trigger = ("context_overflow" if cls is FailureClass.CONTEXT_OVERFLOW
                               else "token_pressure")
                    obs.emit("context.compact", run_id, iter_n=iter_n, stage=stage,
                             agent=agent_name,
                             data={"trigger": trigger,
                                   "prev_total_tokens": result.total_tokens,
                                   "compact_attempt": compact_used},
                             runs_dir=runs_dir)
                    print(f"    ♻️ 摘要重启({trigger})：旧会话 {result.total_tokens:,} tokens，"
                          f"重建精简 prompt 开新会话（第 {compact_used}/{_COMPACT_MAX_PER_CALL} 次）")
                    prompt = new_prompt
                    continue
            else:
                print(f"    ⛔ 摘要重启熔断（已用 {compact_used}/{_COMPACT_MAX_PER_CALL} 次）")

        if cls in (FailureClass.NON_RETRYABLE, FailureClass.CONTEXT_OVERFLOW):
            obs.emit_recovery("result", run_id, stage=stage, agent=agent_name,
                              iter_n=iter_n, result="failure", runs_dir=runs_dir,
                              reason=f"不可重试失败（{cls.value}），直接放弃")
            return result

        if attempt < max_retries:
            if cls in (FailureClass.RATE_LIMIT, FailureClass.TRANSIENT):
                delay = _backoff_config.next_delay(attempt, cls)
                total = _backoff_elapsed.get(agent_name, 0.0) + delay
                if total > _backoff_config.total_timeout:
                    obs.emit_recovery("result", run_id, stage=stage, agent=agent_name,
                                      iter_n=iter_n, result="failure", runs_dir=runs_dir,
                                      reason=f"退避累计时长 {total:.0f}s 超限，放弃重试")
                    print(f"    ⛔ {agent_name} 退避累计时长超限，放弃重试")
                    return result
                _backoff_elapsed[agent_name] = total
                obs.emit("task.backoff", run_id, iter_n=iter_n, stage=stage,
                         agent=agent_name, runs_dir=runs_dir,
                         data={"attempt": attempt, "delay_ms": round(delay * 1000),
                               "reason": cls.value, "elapsed_s": round(total, 1)})
                print(f"    ⏳ {agent_name} 命中 {cls.value}，退避 {delay}s 后重试"
                      f"（第 {attempt + 1}/{max_retries} 次）...")
                await asyncio.sleep(delay)
            obs.emit("task.retry", run_id, iter_n=iter_n, stage=stage, agent=agent_name,
                     runs_dir=runs_dir, data={"next_attempt": attempt + 1})
            print(f"    → 重试 task({agent_name})（第 {attempt + 1}/{max_retries} 次）")
            attempt += 1
        else:
            obs.emit_recovery("result", run_id, stage=stage, agent=agent_name,
                              iter_n=iter_n, result="failure", runs_dir=runs_dir,
                              reason="达到最大重试次数仍失败")
            break

    assert result is not None
    return result
