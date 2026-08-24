"""Agent 定义：工具集约束 + system prompt 加载。

AIcoverage 的 LLM agent 全景（执行与覆盖率采集为确定性代码，不设 LLM agent）：

| agent           | 职责                                     | 可写范围            |
|-----------------|------------------------------------------|---------------------|
| analyzer-agent  | 需求解析/源码理解 → 分析报告+测试计划     | .aicoverage/        |
| coverage-agent  | 未覆盖函数根因分类 → gap_items.json      | run/iter 目录       |
| gen-agent       | 生成/修改 pytest 用例 → manifest.json     | tests/（含 harness）|
| verify-agent    | 静态审查 → verify_report.json            | 仅 verify_report.json|
| quality-agent   | 失败归因/flaky 识别 → quality_report.json | run/iter 目录       |
| scan-agent      | MR 增量 diff 语义扫描 → scan_issues.json  | 仅 scan_issues.json |
| kb-agent        | 代码知识库构建 → wiki/ + AGENTS.md        | 仅 wiki/ 与 AGENTS.md |

扫描轨的复现用例生成复用 gen-agent（同一工具白名单/hooks），仅通过
`run_agent(prompt_override=...)` 换用 `prompts/scan_gen_agent.md`（正向断言
约定与覆盖率轨不同，见该文件）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).parent / "prompts"

# SDK 底层子 agent 派发工具名（实测结论："Agent" 才是真实名字）
_DISPATCH = ["Agent", "Task"]

AGENT_TOOLS: dict[str, list[str]] = {
    "analyzer-agent": ["Bash", "Read", "Glob", "Grep", "LS", "Write", "TodoWrite"],
    "coverage-agent": ["Bash", "Read", "Glob", "Grep", "LS", "Write", "TodoWrite"],
    "gen-agent": [
        "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
        "TodoWrite", "delete_file",
    ],
    "verify-agent": ["Bash", "Read", "Write"],
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
    """加载 agent system prompt。

    prompts_dir（来自配置 [knowledge] prompts_dir）下存在同名 .md 时整份覆盖，
    否则用内置 aicoverage/prompts/<name>.md。文件名规则：连字符 → 下划线。
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
    """构造 SDK AgentDefinition（chat/主编排模式用；loop 单点调用走 system_prompt 路径）。"""
    from codebuddy_agent_sdk import AgentDefinition

    return AgentDefinition(
        description=get_description(agent_name),
        tools=get_agent_tools(agent_name),
        prompt=prompt or load_prompt(agent_name, prompts_dir),
        model=model,
    )
