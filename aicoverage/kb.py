"""代码知识库构建轨：kb-agent 生成 wiki/（wikirize 方法论适配）。

用户需求（2026-08-24）：在执行完整闭环（loop/mr）之前，可选择先根据代码
构建知识库——后续 analyzer/coverage/gen/scan agent 通过 wiki 导航降低源码
探索成本（wikirize 基准数据：agent 探索 -45.9% token / -28.8% 时间），
这也直接缓解 ModSecurity 闭环观测到的 AGENT_CONTEXT_OVERFLOW 问题。

与 wikirize 原版 skill 的关系：方法论（7 阶段 / lookup-first 页面 / Source
Truth Order / 必备页面清单）完整保留并注明来源；差异点是本实现由
AIcoverage 的 kb-agent（CodeBuddy SDK 单 agent 顺序执行）承载，而非外部
skill 生态（`npx skills add`）——保持 AIcoverage 自包含、零 node 依赖。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import observability as obs
from .agent_call import call_agent
from .config import ProjectConfig
from .runner import AgentRunner

#: 必备页面（Definition of Done 的最小集合；缺任一即视为构建不完整）
REQUIRED_PAGES = (
    "index.md",
    "agent-quickstart.md",
    "contributing-agent-rules.md",
    "coverage-manifest.md",
    "source-map.md",
)


def wiki_dir(cfg: ProjectConfig) -> Path:
    """wiki 固定约定路径（wikirize 约定：<source>/wiki/）。"""
    return cfg.source_path / "wiki"


def wiki_ready(cfg: ProjectConfig) -> bool:
    """wiki 已构建且必备页面齐备。"""
    d = wiki_dir(cfg)
    return d.is_dir() and all((d / p).exists() for p in REQUIRED_PAGES)


def wiki_navigation_hint(cfg: ProjectConfig) -> str:
    """注入各 agent prompt 的 wiki 导航提示（省 token 的核心机制：
    agent 先读地图再精读源码，而不是盲目 rglob 全仓库）。"""
    if not wiki_ready(cfg):
        return ""
    return (
        "\n## 项目知识库（wiki，优先导航）\n"
        f"项目已有 AI 生成的源码导航 wiki：`{wiki_dir(cfg)}/`。\n"
        "定位代码/理解结构时**先读** `wiki/agent-quickstart.md` 与 "
        "`wiki/source-map.md`（地图），再按指引进具体源码（真相）。\n"
        "wiki 只是定位器：页面中的路径/结论仍以源码为准，改动判断前必须读源码本体。\n"
    )


def _prompt_kb(cfg: ProjectConfig) -> str:
    files = cfg.source_files()
    files_preview = "\n".join(
        p.relative_to(cfg.source_path).as_posix() for p in files[:120]
    ) or "（include_globs 未匹配到文件）"
    lang_note = ("C++ 项目" if cfg.language == "cpp" else "C 项目")
    return f"""为被测项目构建代码知识库（wikirize 方法论）。

项目：{cfg.display_name}（{lang_note}，构建系统产物：{cfg.binary_path}）
源码根：$AICOV_SRC = {cfg.source_path}
wiki 输出目录：{wiki_dir(cfg)}/
AGENTS.md：{cfg.source_path / 'AGENTS.md'}（追加 Project Wiki 章节，保留既有内容）

## include 范围内的源文件（前 120 个）
{files_preview}

## 任务
按你的 SOP（7 阶段）完成 wiki 构建并更新 AGENTS.md。
必备页面：{'、'.join(REQUIRED_PAGES)}（缺一不可，完成后会被确定性校验）。"""


async def run_kb_build(
    cfg: ProjectConfig, *, quiet: bool = False, force: bool = False,
) -> dict:
    """构建（或 --force 重建）项目 wiki。

    Returns:
        {"status": "ok|skipped|incomplete", "pages": [...], "missing": [...]}
    """
    if wiki_ready(cfg) and not force:
        print(f"✅ 知识库已存在且完整：{wiki_dir(cfg)}/（--force 可重建）")
        return {"status": "skipped", "pages": list(REQUIRED_PAGES), "missing": []}

    if wiki_dir(cfg).exists() and not force:
        missing = [p for p in REQUIRED_PAGES if not (wiki_dir(cfg) / p).exists()]
        print(f"⚠️ 检测到不完整 wiki（缺 {len(missing)} 个必备页面），将补齐重建")

    run_id = f"KB_{cfg.name}"
    run_dir = cfg.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    import os
    os.environ.update(cfg.to_env(run_dir=run_dir))

    print(f"▶ 知识库构建（kb-agent，输出 → {wiki_dir(cfg)}/）")
    obs.emit("stage.enter", run_id, stage="kb", runs_dir=cfg.runs_dir)

    runner = AgentRunner(cfg, quiet=quiet, run_dir=run_dir)
    await call_agent(
        runner, run_id, "kb-agent", _prompt_kb(cfg),
        runs_dir=cfg.runs_dir, stage="kb", max_retries=2,
    )

    # 确定性产物校验（Definition of Done 的机器可查部分）
    missing = [p for p in REQUIRED_PAGES if not (wiki_dir(cfg) / p).exists()]
    pages = sorted(
        p.relative_to(wiki_dir(cfg)).as_posix()
        for p in wiki_dir(cfg).rglob("*.md")
    ) if wiki_dir(cfg).is_dir() else []
    agents_md_updated = (cfg.source_path / "AGENTS.md").exists()

    result = {
        "status": "ok" if not missing else "incomplete",
        "pages": pages, "missing": missing,
        "agents_md": agents_md_updated,
        "total_pages": len(pages),
    }
    (run_dir / "kb_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    obs.emit("stage.exit", run_id, stage="kb", runs_dir=cfg.runs_dir,
             data={"status": result["status"], "pages": len(pages),
                   "missing": len(missing)})
    if missing:
        print(f"  ⚠️ 构建不完整：缺必备页面 {missing}")
    else:
        print(f"  ✅ 构建完成：{len(pages)} 个页面"
              + ("（含 AGENTS.md 维护规则）" if agents_md_updated else ""))
    return result
