"""badcase self-regressing accumulation (a two-way loop; the industry's "self-healing"
badcase knowledge base practice).

- **Read side (regression)**: `badcase_hint()` injects a badcase quick-index into the gen
  prompt -- agents check known pitfalls before generating cases, avoiding re-stepping on them.
- **Write side (accumulation)**: quality-agent **proposes** `badcase_candidates` in
  quality_report.json -> `merge_candidates()` validates format, dedups, and assigns IDs via
  **deterministic Python code**, then persists. The LLM only proposes; code adjudicates writes
  -- the LLM is not allowed to write the library directly (prevents format corruption and
  duplicate writes), consistent with AIcoverage's "determinism-first" philosophy.

Two-layer library:
- Tool-level: `aicoverage/badcases/BASE.md` (ships with AIcoverage; cross-project common
  pitfalls; seed content from 2026-08 real-incident postmortems; read-only, not rewritten by the loop)
- Project-level: `<source>/.aicoverage/badcases.md` (auto-accumulates per project; the loop can write)

Entry format (ID prefix AICB):

    ## AICB-NNN: title
    - **类别**: <category>
    - **症状**: <symptom>
    - **根因**: <root_cause>
    - **修复/预防**: <prevention>
    - **影响**: <affects> (which agent/stage)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_BASE_PATH = Path(__file__).parent / "badcases" / "BASE.md"

#: Max entries injected into the prompt (keeps a large library from blowing up the context;
#: the index table is always full)
_HINT_MAX_DETAILS = 12

#: Required fields of quality-agent-proposed entries (missing any -> reject; never enter ambiguously)
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
    # raw detail text (incl. non-field extra lines), used for dedup comparison
    raw: str = field(default="", repr=False)


# ── Parsing ───────────────────────────────────────────────────────

_ENTRY_RE = re.compile(r"^## (AICB-\d+):\s*(.+)$", re.MULTILINE)


def parse_badcases(path: Path) -> list[BadcaseEntry]:
    """Parse the badcase-library Markdown -> entry list. Missing/format-broken file returns
    [] (read-side fail-soft: a broken library must not block the loop, only hint to degrade)."""
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
    """Project-level badcase library's fixed path (<source>/.aicoverage/badcases.md)."""
    return workspace / "badcases.md"


def _load_by_workspace(workspace: Path) -> list[BadcaseEntry]:
    """Merge tool-level BASE + project-level entries (tool-level first)."""
    return parse_badcases(_BASE_PATH) + parse_badcases(project_badcases_path(workspace))


def load_all(cfg) -> list[BadcaseEntry]:
    """Read-side unified entry (cfg: ProjectConfig)."""
    return _load_by_workspace(cfg.workspace)


# ── Read side: prompt-injection hint ────────────────────────────────

def badcase_hint(cfg) -> str:
    """The badcase hint injected into the gen prompt (the read-side core). Returns "" when no library."""
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


# ── Write side: deterministic merge (LLM proposes, code adjudicates) ──

def _normalize(s: str) -> str:
    """Dedup normalization: strip whitespace/punctuation, lowercase."""
    return re.sub(r"[\s\W]+", "", (s or "")).lower()


def merge_candidates(workspace: Path, candidates: list[dict], *,
                     source: str = "quality-agent") -> dict:
    """Merge quality-agent-proposed badcase_candidates into the project-level library.

    Deterministic rules (each entry adjudicated independently; a bad entry never blocks a good one):
    1. Format check: required fields (title/category/symptom/root_cause/prevention) -- reject if any missing
    2. Dedup: skip if normalized title equals an existing entry (BASE + project-level)
    3. ID: auto-increment the cross-library max +1 to assign AICB-NNN (avoids colliding with BASE)
    4. Persist: append to `<workspace>/badcases.md` (first creation adds a file header and index table)

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
