"""badcase 自回归沉淀（双向闭环，业界 badcase 知识库的"自愈"实践）。

- **读侧（回归）**：`badcase_hint()` 把 badcase 库速查索引注入 gen prompt——
  agent 生成用例前先看已知坑，防重复踩踏。
- **写侧（沉淀）**：quality-agent 在 quality_report.json 里**提议**
  `badcase_candidates` → `merge_candidates()` 用**确定性 Python 代码**校验
  格式、查重、分配编号后落盘。LLM 只提议、代码裁决写入——不让 LLM 直接
  写库（防格式崩坏与重复写入），符合 AIcoverage"确定性优先"哲学。

双层库：
- 工具级：`aicoverage/badcases/BASE.md`（随 AIcoverage 分发，跨项目通用坑，
  种子内容来自 2026-08 真实事故复盘；只读，不由闭环改写）
- 项目级：`<source>/.aicoverage/badcases.md`（每项目自动累积，闭环可写）

条目格式（编号前缀 AICB）：

    ## AICB-NNN: 标题
    - **类别**: <category>
    - **症状**: <symptom>
    - **根因**: <root_cause>
    - **修复/预防**: <prevention>
    - **影响**: <affects>（哪个 agent/阶段）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_BASE_PATH = Path(__file__).parent / "badcases" / "BASE.md"

#: 注入 prompt 的最大条目数（防止大库撑爆上下文；索引表始终全量）
_HINT_MAX_DETAILS = 12

#: quality-agent 提议条目的必填字段（缺失即拒绝合并，绝不含糊入库）
_REQUIRED_FIELDS = ("title", "category", "symptom", "root_cause", "prevention")


@dataclass
class BadcaseEntry:
    id: str
    title: str
    category: str = ""
    symptom: str = ""
    root_cause: str = ""
    prevention: str = ""
    affects: str = ""
    # 详情原文（含字段外的补充行），用于去重比较
    raw: str = field(default="", repr=False)


# ── 解析 ────────────────────────────────────────────────────────

_ENTRY_RE = re.compile(r"^## (AICB-\d+):\s*(.+)$", re.MULTILINE)


def parse_badcases(path: Path) -> list[BadcaseEntry]:
    """解析 badcase 库 Markdown → 条目列表。文件不存在/格式异常返回 []（读侧
    fail-soft：坏库不应阻断闭环，只是提示降级）。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[BadcaseEntry] = []
    matches = list(_ENTRY_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        entry = BadcaseEntry(id=m.group(1), title=m.group(2).strip())
        for key, attr in (("类别", "category"), ("症状", "symptom"),
                          ("根因", "root_cause"), ("修复/预防", "prevention"),
                          ("影响", "affects")):
            km = re.search(rf"-\s*\*\*{re.escape(key)}\*\*[:：]?\s*(.+)", body)
            if km:
                setattr(entry, attr, km.group(1).strip())
        entry.raw = body.strip()
        entries.append(entry)
    return entries


def project_badcases_path(workspace: Path) -> Path:
    """项目级 badcase 库固定路径（<source>/.aicoverage/badcases.md）。"""
    return workspace / "badcases.md"


def _load_by_workspace(workspace: Path) -> list[BadcaseEntry]:
    """合并 工具级 BASE + 项目级 条目（工具级在前）。"""
    return parse_badcases(_BASE_PATH) + parse_badcases(project_badcases_path(workspace))


def load_all(cfg) -> list[BadcaseEntry]:
    """读侧统一入口（cfg: ProjectConfig）。"""
    return _load_by_workspace(cfg.workspace)


# ── 读侧：prompt 注入提示 ─────────────────────────────────────────

def badcase_hint(cfg) -> str:
    """注入 gen prompt 的 badcase 提示（读侧核心）。无库时返回空串。"""
    entries = load_all(cfg)
    if not entries:
        return ""
    lines = [
        "\n## 已知 badcase 速查（生成用例前先过一遍，防重复踩坑）",
        "| 编号 | 标题 | 类别 |",
        "|------|------|------|",
    ]
    for e in entries:
        lines.append(f"| {e.id} | {e.title} | {e.category} |")
    details = [e for e in entries if e.category == "gen-quality"][:_HINT_MAX_DETAILS]
    if details:
        lines.append("")
        lines.append("与用例生成直接相关的条目摘要：")
        for e in details:
            lines.append(f"- **{e.id} {e.title}**：{e.prevention}")
    lines.append("")
    lines.append("> 命中以上任一模式时按其预防规则规避；完整条目见"
                 f" `{_BASE_PATH}` 与 `{project_badcases_path(cfg.workspace)}`。")
    return "\n".join(lines) + "\n"


# ── 写侧：确定性合并（LLM 提议、代码裁决）─────────────────────────

def _normalize(s: str) -> str:
    """查重用归一化：去空白/标点、小写。"""
    return re.sub(r"[\s\W]+", "", (s or "")).lower()


def merge_candidates(workspace: Path, candidates: list[dict], *,
                     source: str = "quality-agent") -> dict:
    """把 quality-agent 提议的 badcase_candidates 合并进项目级库。

    确定性规则（每条独立裁决，坏条目不阻断好条目）：
    1. 格式校验：必填字段（title/category/symptom/root_cause/prevention）缺一即拒
    2. 查重：与现有条目（BASE+项目级）标题归一化相同 → 跳过
    3. 编号：跨库最大号 +1 自增分配 AICB-NNN（避免与 BASE 撞号）
    4. 落盘：追加到 `<workspace>/badcases.md`（首次创建带文件头与索引表）

    Returns:
        {"merged": [{id,title,category}...], "rejected": [...], "path": str}
    """
    path = project_badcases_path(workspace)
    existing = _load_by_workspace(workspace)
    existing_titles = {_normalize(e.title) for e in existing}
    max_num = 0
    for e in existing:
        m = re.match(r"AICB-(\d+)", e.id)
        if m:
            max_num = max(max_num, int(m.group(1)))

    merged: list[dict] = []
    rejected: list[dict] = []
    next_num = max_num + 1
    for cand in candidates or []:
        if not isinstance(cand, dict):
            rejected.append({"candidate": str(cand)[:80], "reason": "非对象结构"})
            continue
        missing = [f for f in _REQUIRED_FIELDS if not str(cand.get(f, "")).strip()]
        if missing:
            rejected.append({"candidate": str(cand.get("title", "?"))[:80],
                             "reason": f"缺必填字段: {', '.join(missing)}"})
            continue
        title = str(cand["title"]).strip()
        if _normalize(title) in existing_titles:
            rejected.append({"candidate": title[:80], "reason": "标题与现有条目重复"})
            continue
        new_id = f"AICB-{next_num:03d}"
        next_num += 1
        merged.append({
            "id": new_id, "title": title,
            "category": str(cand.get("category", "")).strip(),
            "text": _render_entry(new_id, cand, source),
        })
        existing_titles.add(_normalize(title))

    if merged:
        path.parent.mkdir(parents=True, exist_ok=True)
        first_time = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if first_time:
                f.write(
                    "# AIcoverage Badcase 知识库（项目级，自动累积）\n\n"
                    "> 由闭环 quality-agent 提议、确定性代码校验合并入库（LLM 提议、"
                    "代码裁决）。工具级通用坑见 AIcoverage 内置 badcases/BASE.md。\n"
                    "> 本文件只存条目（不维护索引表——索引在注入 prompt 时动态生成）。\n")
            else:
                f.write("\n")
            for m in merged:
                f.write(m["text"] + "\n")
    return {
        "merged": [{k: m[k] for k in ("id", "title", "category")} for m in merged],
        "rejected": rejected, "path": str(path),
    }


def _render_entry(new_id: str, cand: dict, source: str) -> str:
    def g(k: str) -> str:
        return str(cand.get(k, "")).strip()

    affects = g("affects") or "gen-agent"
    return (
        f"## {new_id}: {g('title')}\n\n"
        f"- **类别**: {g('category')}\n"
        f"- **症状**: {g('symptom')}\n"
        f"- **根因**: {g('root_cause')}\n"
        f"- **修复/预防**: {g('prevention')}\n"
        f"- **影响**: {affects}（来源：{source}，{date.today().isoformat()}）\n"
    )
