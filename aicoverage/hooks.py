"""Hooks 安全约束（通用化）。

铁律固化原则：即使 LLM 想违规也会被程序化拦截。拦截项设计：

| 拦截项                                      | 处理                                     |
|--------------------------------------------|------------------------------------------|
| 特定二进制伪参数白名单                      | 移除——任意项目的 CLI 参数千差万别，无法   |
|                                            | 用统一白名单约束，改为 [guard] 自定义黑名单 |
| Skill 工具白名单                           | 移除——AIcoverage 不依赖 skill 体系       |
| benchmark 防泄题                            | 移除——无封闭评测场景                     |
| 危险命令黑名单（rm -rf/sudo/...）           | 保留                                     |
| gen-agent 禁止执行 pytest                   | 保留（执行权归确定性 executor）           |
| write_guard 写入目录白名单                  | 保留，按 agent 角色区分（见 _write_allowed）|
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig

# 全局危险命令黑名单
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bsudo\b",
    r"\bchmod\s+777\b",
    r">\s*/dev/sd",
    r"\bmkfs\b",
    r"\bdd\s+if=",
]

# gen-agent 禁止执行测试命令（铁律：执行权只在确定性 executor）
GEN_BLOCKED = [
    r"\bpytest\b",
    r"\bpython\s+-m\s+pytest\b",
    r"\buv\s+run\s+pytest\b",
    r"\bgit\s+(push|reset|checkout\s+--)\b",
]

# verify-agent 只读（除报告 JSON 外不得写任何文件）
_VERIFY_WRITABLE = ("verify_report.json",)

# scan-agent 只读（除扫描产物 JSON 外不得写任何文件）
_SCAN_WRITABLE = ("scan_issues.json",)

# kb-agent：只准写 wiki/ 目录与根 AGENTS.md（知识库构建专用）
_KB_WRITABLE_FILES = ("AGENTS.md",)


def _write_allowed(agent_name: str, cfg: ProjectConfig) -> list[Path]:
    """按 agent 角色返回可写目录白名单（绝对路径前缀）。"""
    tmp = Path("/tmp")
    src = cfg.source_path
    run_dir = Path(os.environ.get("AICOV_RUN_DIR", "")) if os.environ.get("AICOV_RUN_DIR") else None
    iter_dir = Path(os.environ.get("AICOV_ITER_DIR", "")) if os.environ.get("AICOV_ITER_DIR") else None
    if agent_name == "gen-agent":
        # 用例 + harness 都在测试目录；/tmp 允许
        allowed = [src, tmp]
    elif agent_name == "verify-agent":
        # 只读审查；verify_report.json 由 loop 预置路径，写也只允许落在 run/iter 目录
        allowed = [d for d in (run_dir, iter_dir, tmp) if d is not None]
    else:
        # analyzer/coverage/quality：只写 run/iter 产物目录
        allowed = [d for d in (run_dir, iter_dir, tmp) if d is not None]
        if not allowed:
            allowed = [tmp]
    return allowed


def _path_matches(path_str: str, allowed: list[Path]) -> bool:
    if not path_str:
        return False
    try:
        p = Path(path_str).expanduser()
        if not p.is_absolute():
            return True   # 相对路径的写入目标在 cwd 下，交给目录白名单兜底不住时允许（保守放行）
        p = p.resolve()
        for root in allowed:
            if str(p).startswith(str(root)):
                return True
        return False
    except (OSError, RuntimeError):
        return False


def make_security_hooks(agent_name: str, cfg: ProjectConfig):
    """为指定 agent 生成 SDK hooks 配置。

    返回 dict[HookEvent, list[HookMatcher]]（SDK dataclass，2026-08-21 踩坑：
    传裸 dict 会报 'dict' object has no attribute 'hooks'），传入
    CodeBuddyAgentOptions(hooks=...)。
    """
    from codebuddy_agent_sdk import HookMatcher

    extra_blocked = [re.compile(p) for p in cfg.extra_blocked_commands]
    binary = cfg.binary_path
    binary_name = binary.name if binary else ""

    async def bash_guard(input_data: Any, tool_name: str | None, context: Any) -> dict:
        if tool_name not in ("Bash", "execute_command"):
            return {}
        cmd = ""
        if isinstance(input_data, dict):
            cmd = input_data.get("command", "") or input_data.get("tool_input", {}).get("command", "")
        elif isinstance(input_data, str):
            cmd = input_data
        if not cmd:
            return {}

        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, cmd):
                return {"decision": "block",
                        "reason": f"命令被安全策略拦截（匹配 {pattern}）"}
        for pattern in extra_blocked:
            if pattern.search(cmd):
                return {"decision": "block",
                        "reason": f"命令被项目 guard.blocked_commands 拦截（匹配 {pattern.pattern}）"}

        # gen-agent 额外禁令：不执行测试、不动 git
        if agent_name == "gen-agent":
            for pattern in GEN_BLOCKED:
                if re.search(pattern, cmd):
                    return {
                        "decision": "block",
                        "reason": (f"gen-agent 铁律：禁止执行测试/git 命令（匹配 {pattern}）。"
                                   "只生成/修改用例文件，执行由确定性 executor 负责。"),
                    }
        # verify-agent 禁令：不得运行被测二进制（静态审查不执行）
        if agent_name == "verify-agent" and binary_name and re.search(rf"\b{re.escape(binary_name)}\b", cmd):
            return {"decision": "block",
                    "reason": "verify-agent 铁律：静态审查阶段禁止运行被测二进制。"}
        return {}

    async def write_guard(input_data: Any, tool_name: str | None, context: Any) -> dict:
        if tool_name not in ("Write", "Edit", "replace_in_file", "MultiEdit", "delete_file"):
            return {}
        file_path = ""
        if isinstance(input_data, dict):
            file_path = (input_data.get("filePath") or input_data.get("file_path")
                         or input_data.get("path") or "")
        if not file_path:
            return {}

        # verify-agent 只允许写 verify_report.json
        if agent_name == "verify-agent":
            if Path(file_path).name not in _VERIFY_WRITABLE:
                return {"decision": "block",
                        "reason": "verify-agent 是只读审查角色，只能写 verify_report.json。"}
            return {}

        # scan-agent 只允许写 scan_issues.json
        if agent_name == "scan-agent":
            if Path(file_path).name not in _SCAN_WRITABLE:
                return {"decision": "block",
                        "reason": "scan-agent 是只读扫描角色，只能写 scan_issues.json。"}
            return {}

        # kb-agent：只准写 <source>/wiki/** 与 <source>/AGENTS.md
        if agent_name == "kb-agent":
            try:
                p = Path(file_path).expanduser().resolve()
            except (OSError, RuntimeError):
                return {"decision": "block", "reason": "路径不可解析"}
            wiki = (cfg.source_path / "wiki").resolve()
            in_wiki = str(p).startswith(str(wiki) + "/") or p == wiki
            is_agents_md = (p.parent == cfg.source_path.resolve()
                            and p.name in _KB_WRITABLE_FILES)
            if not (in_wiki or is_agents_md):
                return {"decision": "block",
                        "reason": (f"kb-agent 只能写 {wiki}/ 与 "
                                   f"{cfg.source_path / 'AGENTS.md'}。")}
            return {}

        allowed = _write_allowed(agent_name, cfg)
        if not _path_matches(str(file_path), allowed):
            return {
                "decision": "block",
                "reason": (f"写入路径越界: {file_path}。{agent_name} 允许写入的目录: "
                           f"{', '.join(str(a) for a in allowed)}"),
            }
        # gen-agent 不得修改被测源码（只能动 tests/）
        if agent_name == "gen-agent":
            try:
                p = Path(file_path).expanduser().resolve()
                if str(p).startswith(str(cfg.source_path)) and cfg.test_dir in p.parents:
                    return {}
                if str(p).startswith(str(cfg.source_path)):
                    return {"decision": "block",
                            "reason": (f"gen-agent 只能写测试目录 {cfg.test_dir}，"
                                       "不得修改被测源码。")}
            except (OSError, RuntimeError):
                pass
        return {}

    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash|execute_command", hooks=[bash_guard]),
            HookMatcher(
                matcher="Write|Edit|MultiEdit|replace_in_file|delete_file",
                hooks=[write_guard],
            ),
        ],
    }
