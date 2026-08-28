"""Cross-run coverage history (append-only JSONL).

A single run's report only knows its own baseline; there was no answer to "how
has this project's coverage evolved over time". Every run appends one entry to
`<source>/.aicoverage/history.jsonl`; `aicov history` renders the trend table.

Entry schema (one JSON object per line, append-only):
    {"ts", "run_id", "trigger", "status", "exit_reason",
     "func_pct", "cond_pct", "line_pct",
     "func_hit", "func_total", "branch_hit", "branch_total"}

JSONL (not a JSON array) so a crash mid-append corrupts at most one line and
no read-modify-write race exists for back-to-back runs.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def history_path(workspace: Path) -> Path:
    """Fixed history path inside the project workspace."""
    return workspace / "history.jsonl"


def append_history(workspace: Path, entry: dict) -> None:
    """Append one run entry. Best-effort: history must never break a loop exit."""
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
        with history_path(workspace).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_history(workspace: Path) -> list[dict]:
    """Load all entries (corrupt lines are skipped, not fatal)."""
    p = history_path(workspace)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def render_history(entries: list[dict]) -> str:
    """Render the trend table (Markdown)."""
    if not entries:
        return "（无历史记录——跑一次 aicov loop 后自动生成）"
    lines = [
        "| 时间 | run_id | 触发 | 结论 | 函数覆盖 | 分支覆盖 | 行覆盖 |",
        "|------|--------|------|------|---------|---------|--------|",
    ]
    for e in entries:
        lines.append(
            f"| {e.get('ts', '?')} | `{e.get('run_id', '?')}` | "
            f"{e.get('trigger', '—')} | {e.get('status', '—')}"
            f"（{e.get('exit_reason', '—')}） | "
            f"{e.get('func_pct', 0):.2f}% ({e.get('func_hit', 0)}/{e.get('func_total', 0)}) | "
            f"{e.get('cond_pct', 0):.2f}% ({e.get('branch_hit', 0)}/{e.get('branch_total', 0)}) | "
            f"{e.get('line_pct', 0):.2f}% |")
    # trend summary: first vs last
    if len(entries) > 1:
        first, last = entries[0], entries[-1]
        d_func = last.get("func_pct", 0) - first.get("func_pct", 0)
        d_cond = last.get("cond_pct", 0) - first.get("cond_pct", 0)
        lines.append("")
        lines.append(f"> 相对首次 run（{first.get('ts', '?')}）演进：函数 {d_func:+.2f}pp、"
                     f"分支 {d_cond:+.2f}pp（共 {len(entries)} 次 run）")
    return "\n".join(lines)
