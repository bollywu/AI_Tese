"""AgentRunner：Agent SDK 调用封装（通用化）。

实现要点（均为实战踩坑结论，行为保持一致）：
- 单 agent 模式用 AppendSystemPrompt 注入 prompts/<name>.md（2026-08-11 修复：
  否则核心铁律从未进入上下文）
- options.tools 才是工具白名单（allowed_tools 只是免确认名单，不起限制作用）
- 活性超时（idle timeout）：持续思考不产出 → 判失败走退避重试（2026-08-17）
- 幻觉检测：tool_uses=0 判失败（重试分类见 agent_call.py）
- hooks 必须每次构造 options 都显式传入（2026-07-17 修复）

环境变量统一为 AICOV_* 前缀；Agent CLI binary 自动探测。
SDK 采用惰性导入——纯确定性阶段（build/coverage/report）不依赖 SDK。
"""
from __future__ import annotations

import asyncio
import dataclasses
import glob
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .agents import get_agent_tools, load_prompt

AGENT_DISPATCH_TOOLS = ["Agent", "Task"]


def _find_codebuddy_cli() -> str | None:
    """自动探测 Agent CLI binary。"""
    try:
        from codebuddy_agent_sdk._binary import get_cli_path
        path = get_cli_path()
        if path and os.path.exists(path) and os.access(path, os.X_OK):
            return str(path)
    except Exception:
        pass

    candidates = [
        "/usr/local/lib64/python3.11/site-packages/codebuddy_agent_sdk/bin/codebuddy-headless",
        "/usr/local/lib/python3.11/site-packages/codebuddy_agent_sdk/bin/codebuddy-headless",
        "/usr/local/lib/python3.12/site-packages/codebuddy_agent_sdk/bin/codebuddy-headless",
    ]
    for p in sys.path:
        if p and "site-packages" in p:
            candidates.append(f"{p}/codebuddy_agent_sdk/bin/codebuddy-headless")
    for c in candidates:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    for c in glob.glob(os.path.expanduser("~/.cache/uv/archive-v0/*/codebuddy_agent_sdk/bin/codebuddy-headless")):
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


@dataclass
class ToolCallRecord:
    name: str = ""
    detail: str = ""
    result_preview: str = ""
    is_error: bool = False
    duration_ms: int = 0


@dataclass
class AgentRunResult:
    agent_name: str
    success: bool = False
    tool_uses: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    summary: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    thinking_summaries: list[str] = field(default_factory=list)
    raw_messages: list[Any] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_creation_tokens)


class AgentRunner:
    """每个 agent 调用都无状态（对应"无状态铁律"），上下文通过文件系统传递。"""

    def __init__(self, cfg: ProjectConfig, *, quiet: bool = False,
                 run_dir: Path | None = None, iter_dir: Path | None = None):
        self.cfg = cfg
        self.run_dir = run_dir
        self.iter_dir = iter_dir
        self.verbose = os.environ.get("AICOV_VERBOSE", "").lower() in ("1", "true", "yes")
        self.quiet = quiet

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["AICOV_HOME"] = str(Path(__file__).parent.parent)
        env.update(self.cfg.to_env(run_dir=self.run_dir, iter_dir=self.iter_dir))
        if not env.get("CODEBUDDY_CODE_PATH"):
            cli_path = _find_codebuddy_cli()
            if cli_path:
                env["CODEBUDDY_CODE_PATH"] = cli_path
        return env

    @staticmethod
    def _idle_timeout() -> float:
        raw = os.environ.get("AICOV_AGENT_IDLE_TIMEOUT", "").strip()
        if not raw:
            return 300.0
        try:
            return float(raw)
        except ValueError:
            return 300.0

    async def run_agent(
        self,
        agent_name: str,
        prompt: str,
        max_turns: int | None = None,
        permission_mode: str | None = None,
        prompt_override: str | None = None,
    ) -> AgentRunResult:
        """prompt_override：整份替换该次调用的 system prompt（如扫描轨用
        scan_gen_agent.md 替换 gen-agent 的默认 prompt——工具白名单/hooks
        沿用 gen-agent，语义换为缺陷复现变体）。None = 用默认加载。"""
        from codebuddy_agent_sdk import CodeBuddySDKClient, CodeBuddyAgentOptions, AppendSystemPrompt
        from .hooks import make_security_hooks

        model = self.cfg.effective_gen_model if agent_name == "gen-agent" else self.cfg.model

        options_kwargs: dict[str, Any] = {
            "max_turns": max_turns or self.cfg.max_turns,
            "cwd": str(Path(__file__).parent.parent),
            "model": model,
            "permission_mode": permission_mode or self.cfg.permission_mode,
            "env": self._build_env(),
            "tools": get_agent_tools(agent_name),
            "system_prompt": AppendSystemPrompt(
                prompt_override if prompt_override is not None
                else load_prompt(agent_name, self.cfg.prompts_dir)
            ),
            "hooks": make_security_hooks(agent_name, self.cfg),
        }

        options = CodeBuddyAgentOptions(**options_kwargs)
        result = AgentRunResult(agent_name=agent_name)
        start = time.time()
        idle_timeout = self._idle_timeout()

        try:
            async with CodeBuddySDKClient(options=options) as client:
                await client.query(prompt)
                if idle_timeout > 0:
                    await self._receive_with_idle_timeout(client, result, idle_timeout)
                else:
                    async for msg in client.receive_response():
                        self._process_message(msg, result)
            result.duration_ms = int((time.time() - start) * 1000)
            if result.tool_uses == 0:
                result.success = False
                result.summary = f"幻觉检测: agent={agent_name} tool_uses=0"
            else:
                result.success = True
        except asyncio.TimeoutError:
            result.success = False
            result.summary = (f"agent={agent_name} 活性超时（idle timeout "
                              f"{idle_timeout:.0f}s 无新产出）")
            result.duration_ms = int((time.time() - start) * 1000)
        except Exception as e:  # noqa: BLE001 — SDK 异常统一分类重试
            result.success = False
            result.summary = f"agent={agent_name} 异常: {e}"
            result.duration_ms = int((time.time() - start) * 1000)
        return result

    async def _receive_with_idle_timeout(self, client: Any, result: AgentRunResult,
                                         idle_timeout: float) -> None:
        aiter = client.receive_response().__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            self._process_message(msg, result)

    def _process_message(self, msg: Any, result: AgentRunResult) -> None:
        """处理 SDK 消息流（dataclass 与 dict 两种形式兼容）。"""
        from codebuddy_agent_sdk import (
            AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
            ToolResultBlock, ThinkingBlock,
        )
        if hasattr(msg, "__dataclass_fields__"):
            try:
                result.raw_messages.append(dataclasses.asdict(msg))
            except TypeError:
                result.raw_messages.append(str(msg))
        elif isinstance(msg, dict):
            result.raw_messages.append(msg)
        else:
            result.raw_messages.append(str(msg))

        if isinstance(msg, AssistantMessage):
            for block in getattr(msg, "content", []):
                if isinstance(block, ToolUseBlock):
                    result.tool_uses += 1
                    tool_name = getattr(block, "name", "")
                    tool_input = getattr(block, "input", {}) or {}
                    detail = self._format_tool_input(tool_name, tool_input)
                    result.tool_calls.append(ToolCallRecord(name=tool_name, detail=detail))
                    if not self.verbose and not self.quiet:
                        self._print_tool_call_compact(result.agent_name, tool_name, detail)
                elif isinstance(block, ThinkingBlock):
                    thinking = getattr(block, "thinking", "")
                    if thinking and thinking.strip():
                        result.thinking_summaries.append(thinking.strip()[:200])
                elif isinstance(block, ToolResultBlock):
                    self._backfill_tool_result(result, block)
                elif isinstance(block, TextBlock) and not result.summary:
                    result.summary = getattr(block, "text", "")[:500]
        elif isinstance(msg, ResultMessage):
            result.duration_ms = getattr(msg, "duration_ms", result.duration_ms)
            result.cost_usd = getattr(msg, "total_cost_usd", 0.0) or 0.0
            usage = getattr(msg, "usage", None)
            if usage:
                result.input_tokens = getattr(usage, "input_tokens", 0) or 0
                result.output_tokens = getattr(usage, "output_tokens", 0) or 0
                result.cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
                result.cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
            if getattr(msg, "is_error", False):
                result.success = False
            if not self.verbose:
                self._print_agent_done_compact(result, msg)

    def _backfill_tool_result(self, result: AgentRunResult, block: Any) -> None:
        for record in reversed(result.tool_calls):
            if not record.result_preview and not record.is_error:
                content = getattr(block, "content", "")
                text = self._extract_text(content)
                record.result_preview = text.strip()[:200] if text else ""
                record.is_error = getattr(block, "is_error", False)
                break

    def _print_tool_call_compact(self, agent_name: str, tool_name: str, detail: str) -> None:
        line = f"    🔧 {agent_name} │ {tool_name}"
        if detail:
            line += f" {detail}"
        if len(line) > 200:
            line = line[:197] + "..."
        print(line, flush=True)

    def _print_agent_done_compact(self, result: AgentRunResult, msg: Any) -> None:
        duration = getattr(msg, "duration_ms", result.duration_ms)
        turns = getattr(msg, "num_turns", 0)
        is_error = getattr(msg, "is_error", False)
        tool_counts: dict[str, int] = {}
        for tc in result.tool_calls:
            tool_counts[tc.name] = tool_counts.get(tc.name, 0) + 1
        tool_stats = " ".join(f"{n}×{c}" for n, c in sorted(tool_counts.items()))
        icon = "❌" if is_error else "✅"

        def _fmt_tok(n: int) -> str:
            return f"{n/1000:.1f}k" if n >= 1000 else str(n)

        token_info = ""
        if result.total_tokens > 0:
            token_info = f"in={_fmt_tok(result.input_tokens)} out={_fmt_tok(result.output_tokens)}"
            if result.cache_read_tokens or result.cache_creation_tokens:
                token_info += f" cache={_fmt_tok(result.cache_read_tokens + result.cache_creation_tokens)}"
        line = f"    {icon} {result.agent_name} 完成 | {duration}ms | {turns}轮"
        if token_info:
            line += f" | {token_info}"
        if tool_stats:
            line += f" | 工具: {tool_stats}"
        print(line, flush=True)

    @staticmethod
    def _format_tool_input(tool_name: str, tool_input: dict) -> str:
        if not tool_input:
            return ""
        key_fields = {
            "Bash": ["command"], "execute_command": ["command"],
            "Read": ["filePath", "file_path"], "Write": ["filePath", "file_path"],
            "Edit": ["filePath", "file_path"], "replace_in_file": ["filePath", "file_path"],
            "Glob": ["pattern"], "Grep": ["pattern", "path"],
            "Agent": ["subagent_type", "description", "prompt"],
            "Task": ["subagent_type", "description", "prompt"],
        }
        fields = key_fields.get(tool_name, [])
        parts = []
        for f in fields:
            val = tool_input.get(f, "")
            if val:
                val_str = str(val)
                if len(val_str) > 150:
                    val_str = val_str[:147] + "..."
                parts.append(f"{f}={val_str}")
        return " " + " ".join(parts[:2]) if parts else ""

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, str):
                    parts.append(c)
                elif hasattr(c, "text"):
                    parts.append(getattr(c, "text", ""))
            return "\n".join(parts)
        return str(content) if content else ""

    async def read_artifact(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
