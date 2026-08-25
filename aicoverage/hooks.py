"""Hooks security constraints (generalized).

Iron-rule-hardening principle: even if the LLM wants to violate, it is programmatically
blocked. Interception design:

| Interception item                            | Handling                                    |
|----------------------------------------------|---------------------------------------------|
| Binary-specific fake-arg whitelist            | removed -- CLI args differ wildly across     |
|                                              | projects, no unified whitelist possible; use |
|                                              | [guard] custom blacklist instead            |
| Skill tool whitelist                          | removed -- AIcoverage doesn't depend on the  |
|                                              | skill system                                |
| benchmark anti-leak                          | removed -- no closed eval scenario          |
| Dangerous command blacklist (rm -rf/sudo/...) | kept                                         |
| gen-agent forbidden from running pytest       | kept (execution belongs to deterministic executor) |
| write_guard write-dir whitelist               | kept, role-differentiated (see _write_allowed) |

"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig

# Global dangerous-command blacklist
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bsudo\b",
    r"\bchmod\s+777\b",
    r">\s*/dev/sd",
    r"\bmkfs\b",
    r"\bdd\s+if=",
]

# gen-agent forbidden from running test commands (iron rule: execution belongs only to the
# deterministic executor)
GEN_BLOCKED = [
    r"\bpytest\b",
    r"\bpython\s+-m\s+pytest\b",
    r"\buv\s+run\s+pytest\b",
    r"\bgit\s+(push|reset|checkout\s+--)\b",
]

# verify-agent is read-only (may only write the report JSON)
_VERIFY_WRITABLE = ("verify_report.json",)

# scan-agent is read-only (may only write the scan-artifact JSON)
_SCAN_WRITABLE = ("scan_issues.json",)

# kb-agent: may only write the wiki/ dir and the root AGENTS.md (knowledge-base build)
_KB_WRITABLE_FILES = ("AGENTS.md",)


def _write_allowed(agent_name: str, cfg: ProjectConfig) -> list[Path]:
    """Return the writable-dir whitelist per agent role (absolute path prefixes)."""
    tmp = Path("/tmp")
    src = cfg.source_path
    run_dir = Path(os.environ.get("AICOV_RUN_DIR", "")) if os.environ.get("AICOV_RUN_DIR") else None
    iter_dir = Path(os.environ.get("AICOV_ITER_DIR", "")) if os.environ.get("AICOV_ITER_DIR") else None
    if agent_name == "gen-agent":
        # cases + harness live in the test dir; /tmp allowed
        allowed = [src, tmp]
    elif agent_name == "verify-agent":
        # read-only review; verify_report.json is pre-set by loop; writes only into run/iter dirs
        allowed = [d for d in (run_dir, iter_dir, tmp) if d is not None]
    else:
        # analyzer/coverage/quality: only write run/iter artifact dirs
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
            return True   # relative-path write targets are under cwd; conservatively allow
        p = p.resolve()
        for root in allowed:
            if str(p).startswith(str(root)):
                return True
        return False
    except (OSError, RuntimeError):
        return False


def make_security_hooks(agent_name: str, cfg: ProjectConfig):
    """Build the SDK hooks config for a given agent.

    Returns dict[HookEvent, list[HookMatcher]] (SDK dataclass; 2026-08-21 gotcha:
    passing a bare dict raises 'dict' object has no attribute 'hooks'), passed into
    CodeBuddyAgentOptions(hooks=...).
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

        # gen-agent extra bans: no test execution, no git ops
        if agent_name == "gen-agent":
            for pattern in GEN_BLOCKED:
                if re.search(pattern, cmd):
                    return {
                        "decision": "block",
                        "reason": (f"gen-agent 铁律：禁止执行测试/git 命令（匹配 {pattern}）。"
                                   "只生成/修改用例文件，执行由确定性 executor 负责。"),
                    }
        # verify-agent ban: must not run the target binary (static review does not execute)
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

        # verify-agent may only write verify_report.json
        if agent_name == "verify-agent":
            if Path(file_path).name not in _VERIFY_WRITABLE:
                return {"decision": "block",
                        "reason": "verify-agent 是只读审查角色，只能写 verify_report.json。"}
            return {}

        # scan-agent may only write scan_issues.json
        if agent_name == "scan-agent":
            if Path(file_path).name not in _SCAN_WRITABLE:
                return {"decision": "block",
                        "reason": "scan-agent 是只读扫描角色，只能写 scan_issues.json。"}
            return {}

        # kb-agent: may only write <source>/wiki/** and <source>/AGENTS.md
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
        # gen-agent must not modify target source (only tests/)
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
