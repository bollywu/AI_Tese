"""Agent definitions: tool-set constraints + system-prompt loading.

AIcoverage's LLM agent landscape (execution & coverage collection are deterministic code,
no LLM agent there):

| agent           | responsibility                                 | writable scope        |
|-----------------|-------------------------------------------------|-----------------------|
| analyzer-agent  | requirement parsing/source understanding -> analysis report + test plan | .aicoverage/ |
| coverage-agent  | uncovered-function root-cause classification -> gap_items.json | run/iter dir |
| gen-agent       | generate/modify pytest cases -> manifest.json   | tests/ (incl. harness)|
| verify-agent    | static review -> verify_report.json             | verify_report.json only|
| quality-agent   | failure attribution/flaky identification -> quality_report.json | run/iter dir |
| scan-agent      | MR incremental diff semantic scan -> scan_issues.json | scan_issues.json only |
| kb-agent        | code knowledge-base build -> wiki/ + AGENTS.md  | wiki/ & AGENTS.md only|

The scan track reuses gen-agent for reproduction-case generation (same tool whitelist/hooks),
only swapping in `prompts/scan_gen_agent.md` via `run_agent(prompt_override=...)` (its
positive-assertion conventions differ from the coverage track; see that file).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).parent / "prompts"

# SDK's underlying sub-agent dispatch tool name (tested conclusion: "Agent" is the real name)
_DISPATCH = ["Agent", "Task"]

AGENT_TOOLS: dict[str, list[str]] = {
    "analyzer-agent": ["Bash", "Read", "Glob", "Grep", "LS", "Write", "TodoWrite"],
    "coverage-agent": ["Bash", "Read", "Glob", "Grep", "LS", "Write", "TodoWrite"],
    "gen-agent": [
        "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
        "TodoWrite", "delete_file",
    ],
    # verify-agent is static review: no execution of anything -- no Bash at all
    # (a Bash whitelist entry is an arbitrary-execution hole; Grep/Glob added so it
    # can cross-check harness signatures without reading whole files).
    "verify-agent": ["Read", "Grep", "Glob", "Write"],
    "quality-agent": ["Bash", "Read", "Write", "Glob", "Grep", "LS", "TodoWrite"],
    "scan-agent": ["Bash", "Read", "Glob", "Grep", "LS", "Write"],
    "kb-agent": ["Bash", "Read", "Glob", "Grep", "LS", "Write", "TodoWrite"],
}

AGENT_DESCRIPTIONS: dict[str, str] = {
    "analyzer-agent": (
        "需求解析与源码理解 Agent。读取被测 C/C++ 项目源码与（可选的）需求描述，"
        "产出项目分析报告与测试计划。触发词：需求解析 / 源码分析 / 测试计划。"
    ),
    "coverage-agent": (
        "覆盖率缺口分析 Agent。基于确定性 gcov 采集的 coverage.json，"
        "对未覆盖函数做根因分类与补测建议。触发词：覆盖率缺口 / 未覆盖分析。"
    ),
    "gen-agent": (
        "测试用例生成 Agent。根据覆盖率缺口、需求分析、verify/quality 反馈生成或修改 "
        "pytest 用例（驱动任意 C/C++ 目标）。遵守原子函数搭积木铁律，"
        "绝不执行 pytest，只产出用例文件与 manifest。"
    ),
    "verify-agent": (
        "用例静态审查 Agent。只做静态语义审查：铁律合规、反模式、原子化结构，"
        "输出 verify_report.json。不执行 pytest、不运行被测程序、不做运行结果判断。"
    ),
    "quality-agent": (
        "用例质量分析 Agent。基于 junit.xml/execution.json 分析失败原因、识别 flaky，"
        "输出 quality_report.json 与 modify_case action_items。不执行用例、不修改代码。"
    ),
    "scan-agent": (
        "增量代码扫描 Agent（完全本地、零外部平台依赖）。对 MR 变更函数及其调用链"
        "上下文做聚焦式语义扫描（内存安全/整数/资源/错误处理/逻辑/并发/注入），"
        "产出结构化 scan_issues.json。宁可零产出不可产出噪声。"
    ),
    "kb-agent": (
        "代码知识库构建 Agent（wikirize 方法论适配）。从源码生成 lookup-first 的"
        "项目 wiki（source-map/entrypoints/flows/contracts/verification），"
        "供后续 agent 导航降低探索成本。只写 wiki/ 与 AGENTS.md。"
    ),
}

ALL_AGENTS = list(AGENT_TOOLS.keys())


def get_agent_tools(agent_name: str) -> list[str]:
    return AGENT_TOOLS.get(agent_name, ["Bash", "Read"])


def get_description(agent_name: str) -> str:
    return AGENT_DESCRIPTIONS.get(agent_name, "")


def load_prompt(agent_name: str, prompts_dir: Path | None = None) -> str:
    """Load an agent system prompt.

    If a same-named .md exists under prompts_dir (from config [knowledge] prompts_dir), it
    fully overrides; otherwise use the built-in aicoverage/prompts/<name>.md. Filename rule:
    hyphen -> underscore.
    """
    file_name = agent_name.replace("-", "_")
    prompt_path = PROMPTS_DIR / f"{file_name}.md"
    if prompts_dir is not None:
        override = prompts_dir / f"{file_name}.md"
        if override.exists():
            prompt_path = override
    if not prompt_path.exists():
        raise FileNotFoundError(f"未找到 prompt 文件: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def build_agent_definition(agent_name: str, model: str, prompt: str | None = None,
                           prompts_dir: Path | None = None):
    """Construct an SDK AgentDefinition (for chat/main-orchestrator mode; loop's single-point
    calls use the system_prompt path)."""
    from codebuddy_agent_sdk import AgentDefinition

    return AgentDefinition(
        description=get_description(agent_name),
        tools=get_agent_tools(agent_name),
        prompt=prompt or load_prompt(agent_name, prompts_dir),
        model=model,
    )
