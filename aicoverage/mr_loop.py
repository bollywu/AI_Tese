"""MR 增量闭环主编排（改造计划文档 §2 总体架构）。

双轨流水线（输入全部来自本地 git diff，**不访问任何代码托管/评审平台**，
适用于 GitHub / GitLab / 任意私有仓库的本地 clone——完全脱敏）：

    [M0] diff 提取（diffextract：git diff -U0 + CodeGraph 行区间归因）
    [M1] 调用链分批（callgraph.split_batches，chain 策略优先）
    [M2] 覆盖轨：逐批复用 run_loop(target_functions=batch)，
        分母收窄到本批变更函数，追求增量 func/cond 达标
    [M3] 扫描轨：scanverify.run_scan_track（scan-agent 聚焦扫描 →
        gen 复现用例（正向断言）→ verify → execute → 四态裁决）
    [M4] 汇总 MR 最终报告（mr_final_report.md）

通用化设计要点：
- 零平台依赖：diff 只来自本地 git ref（GitHub/GitLab/私有仓库 clone 后一视同仁）
- 覆盖轨复用 AIcoverage 自己的 run_loop，
  通过 target_functions/skip_build 参数注入，不复制状态机
- 每批独立 run_id（LOOP_ 前缀），MR 主目录（MR_ 前缀）只存汇总产物
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import callgraph
from . import diffextract
from . import observability as obs
from . import state as st
from .config import ProjectConfig
from .loop import run_loop
from .scanverify import render_scan_markdown, run_scan_track


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


async def run_mr_loop(
    cfg: ProjectConfig,
    *,
    base_ref: str,
    head_ref: str = "HEAD",
    func_target: float | None = None,
    cond_target: float | None = None,
    max_iter: int | None = None,
    split_by: str | None = None,
    skip_scan: bool = False,
    skip_coverage: bool = False,
    quiet: bool = False,
) -> dict:
    """MR 增量闭环主入口。

    Args:
        base_ref/head_ref: 本地 git ref（commit/branch/tag）。head_ref 的代码
            必须已是当前工作区内容（本闭环不 checkout——插桩构建的是工作区）。
        split_by: file | chain | size；None = 自动（CodeGraph 可用时 chain，否则 file）
        skip_scan / skip_coverage: 跳过对应轨道（调试用）
    """
    func_target = func_target if func_target is not None else cfg.func_target
    cond_target = cond_target if cond_target is not None else cfg.cond_target
    max_iter = max_iter or cfg.max_iter

    master_run_id = st.gen_run_id("MR")
    master_dir = cfg.runs_dir / master_run_id
    master_dir.mkdir(parents=True, exist_ok=True)
    print(f"▶ MR 增量闭环启动 run_id={master_run_id} "
          f"base={base_ref} head={head_ref}")
    obs.emit("loop.start", master_run_id, runs_dir=cfg.runs_dir,
             data={"trigger": "mr", "base": base_ref, "head": head_ref})
    os.environ.update(cfg.to_env(run_dir=master_dir))

    summary: dict = {
        "master_run_id": master_run_id, "base_ref": base_ref, "head_ref": head_ref,
        "thresholds": {"func_pct": func_target, "cond_pct": cond_target},
        "coverage_batches": [], "scan": None,
    }

    # ── [M0] diff 提取 ────────────────────────────────────────
    print("▶ [M0] diff 提取（CodeGraph 行区间归因）")
    if cfg.codegraph_enabled:
        if not callgraph.is_indexed(cfg.source_path, cfg.codegraph_index_dir):
            print(f"  ❌ CodeGraph 索引不存在（{cfg.source_path / cfg.codegraph_index_dir}）。"
                  f"请先执行: cd {cfg.source_path} && codegraph init")
            summary["status"] = "error"
            summary["exit_reason"] = "codegraph_not_indexed"
            _write_json(master_dir / "mr_summary.json", summary)
            return summary
    else:
        print("  ⚠️ [codegraph].enabled=false：diff 函数归因与调用链分批不可用，"
              "MR 闭环需要它——请开启并建立索引后重试。")
        summary["status"] = "error"
        summary["exit_reason"] = "codegraph_disabled"
        _write_json(master_dir / "mr_summary.json", summary)
        return summary

    ex = diffextract.extract(cfg.source_path, base_ref, head_ref,
                             index_dir=cfg.codegraph_index_dir)
    (master_dir / "code_diff.txt").write_text(ex.diff_text, encoding="utf-8")
    _write_json(master_dir / "changed_functions.json", ex.to_dict())
    trusted = ex.trusted_functions
    print(f"  变更：{len(ex.file_diffs)} 文件 / {len(ex.functions)} 函数"
          f"（可信 {len(trusted)} / conflict {len(ex.conflict_functions)} / "
          f"unresolved_files {len(ex.unresolved_files)}）")

    if not trusted and not ex.conflict_functions:
        print("  [M0] diff 中无可信变更函数（可能是非 C/C++ 改动/纯格式化）")
        summary.update({"status": "done", "exit_reason": "no_changed_functions",
                        "counts": ex.to_dict()["counts"]})
        _write_json(master_dir / "mr_summary.json", summary)
        return summary

    # ── [M1] 调用链分批 ───────────────────────────────────────
    print("▶ [M1] 调用链分批")
    targets = [f.as_target() for f in trusted]
    strategy = split_by or "chain"
    try:
        batches, unreachable = callgraph.split_batches(
            targets, strategy, batch_size=5,
            source_path=cfg.source_path,
            entrypoints=cfg.codegraph_entrypoints,
            index_dir=cfg.codegraph_index_dir,
        )
    except Exception as e:  # noqa: BLE001 — chain 依赖索引健康，失败回退 file
        print(f"  ⚠️ {strategy} 分批失败（{e}），回退 file 策略")
        strategy = "file"
        batches, unreachable = callgraph.split_batches(targets, "file"), []
    # unreachable（疑似死代码）单独成批仍进闭环：generate 会给真实结论，
    # 但在报告里明确标注"入口不可达，疑似死代码/未接线"
    if unreachable:
        batches = batches + [unreachable]
    _write_json(master_dir / "diff_batches.json", {
        "split_by": strategy, "batches": [[list(t) for t in b] for b in batches],
        "unreachable": [list(t) for t in unreachable],
    })
    print(f"  分批：{len(batches)} 批（策略={strategy}，unreachable {len(unreachable)} 个）")
    summary["split_by"] = strategy
    summary["unreachable"] = [list(t) for t in unreachable]

    # ── [M2] 覆盖轨：逐批 run_loop（scope 收窄） ───────────────
    if not skip_coverage and batches:
        print(f"▶ [M2] 覆盖轨（{len(batches)} 批，每批独立达标闭环）")
        first = True
        for i, batch in enumerate(batches, 1):
            batch_funcs = [f for f in trusted if f.as_target() in batch]
            ctx_lines = []
            for f in batch_funcs:
                ctx_lines.append(
                    f"- `{f.file}:{f.start_line}-{f.end_line}` {f.qualified_name}"
                    f"（本次改动行 {f.changed_lines[:8]}{'...' if len(f.changed_lines) > 8 else ''}）")
            if batch == unreachable:
                ctx_lines.append("\n⚠️ 本批函数经 CodeGraph 反向追溯**无法到达配置的入口**"
                                 f"（entrypoints={cfg.codegraph_entrypoints}），"
                                 "疑似死代码或未接线的新增函数。若确认死代码请在 manifest "
                                 "verdict_noop 里给出证据链，不要伪造用例。")
            target_context = "\n".join(ctx_lines)
            print(f"\n{'=' * 60}\n[MR 覆盖轨 批次 {i}/{len(batches)}] "
                  f"{len(batch)} 个函数\n{'=' * 60}")
            state = await run_loop(
                cfg,
                func_target=func_target, cond_target=cond_target,
                max_iter=max_iter,
                skip_analyze=True,          # MR 模式不需要全项目需求解析
                skip_build=not first,       # 首批构建，后续批复用插桩产物
                target_functions=batch,
                target_context=target_context,
                quiet=quiet,
            )
            fm = state.get("final_metrics", {}) or {}
            summary["coverage_batches"].append({
                "batch_index": i,
                "functions": [list(t) for t in batch],
                "run_id": state.get("run_id"),
                "status": state.get("status"), "exit_reason": state.get("exit_reason"),
                "func_pct": fm.get("func_pct"), "cond_pct": fm.get("cond_pct"),
            })
            first = False
        done = sum(1 for b in summary["coverage_batches"] if b["status"] == "done")
        print(f"\n[M2] 覆盖轨完成：{done}/{len(batches)} 批达标")

    # ── [M3] 扫描轨 ───────────────────────────────────────────
    scan_result = None
    if not skip_scan and summary.get("exit_reason") != "codegraph_disabled":
        print("▶ [M3] 扫描轨（open-code-review 优先 / scan-agent 兜底 → 复现验证 → 四态裁决）")
        scan_result = await run_scan_track(
            cfg, master_run_id, master_dir,
            changed_functions=[f.to_dict() for f in ex.functions],
            diff_text=ex.diff_text, quiet=quiet,
            base_ref=base_ref, head_ref=head_ref,
        )
        summary["scan"] = {
            "issues": len(scan_result.get("issues", [])),
            "verdicts": {k: v["verdict"] for k, v in
                         (scan_result.get("verdicts") or {}).items()},
        }

    # ── [M4] 汇总报告 ─────────────────────────────────────────
    batches_meta = summary.get("coverage_batches", [])
    done_count = sum(1 for b in batches_meta if b.get("status") == "done")
    summary["status"] = ("done" if batches_meta and done_count == len(batches_meta)
                         else ("partial" if batches_meta else "skipped"))
    summary["exit_reason"] = ("all_batches_met" if summary["status"] == "done"
                              else "partial_batches_not_met")
    _write_json(master_dir / "mr_summary.json", summary)
    report_path = master_dir / "mr_final_report.md"
    _write_mr_report(cfg, summary, ex, scan_result, report_path)
    obs.emit("loop.exit", master_run_id, runs_dir=cfg.runs_dir,
             data={"status": summary["status"], "report": str(report_path)})
    print(f"\n▶ MR 闭环结束：{summary['status']}")
    print(f"  最终报告：{report_path}")
    summary["report_path"] = str(report_path)
    return summary


def _write_mr_report(cfg: ProjectConfig, summary: dict, ex: diffextract.DiffExtraction,
                     scan_result: dict | None, path: Path) -> None:
    """MR 最终报告（双轨汇总）。"""
    counts = ex.to_dict()["counts"]
    L = [
        f"# AIcoverage MR 增量闭环报告 — {summary['master_run_id']}",
        "",
        f"- **基准**：`{summary['base_ref']}` → `{summary['head_ref']}`"
        f"（全部输入来自本地 git diff，零外部平台依赖）",
        f"- **变更**：{counts['files']} 文件 / {counts['functions']} 函数"
        f"（可信 {counts['trusted']} · conflict {counts['conflict']} · "
        f"unresolved_files {counts['unresolved_files']}）",
        f"- **分批策略**：{summary.get('split_by', '—')}",
        f"- **达标线**：函数 ≥ {summary['thresholds']['func_pct']}% 且 "
        f"分支 ≥ {summary['thresholds']['cond_pct']}%（增量 scope 分母）",
        "",
    ]
    if ex.conflict_functions:
        L.append("## ⚠️ 归因冲突（不入覆盖轨分母，需人工确认）")
        for f in ex.conflict_functions:
            L.append(f"- `{f.file}` {f.qualified_name}: {f.note[:200]}")
        L.append("")
    if ex.unresolved_files:
        L.append("## ⚠️ 改动行不在任何已索引函数内（需人工确认）")
        L.append("".join(f"- `{f}`\n" for f in ex.unresolved_files))

    # 覆盖轨
    batches = summary.get("coverage_batches", [])
    if batches:
        done = sum(1 for b in batches if b.get("status") == "done")
        L += ["## 覆盖轨结果（增量覆盖率）", "",
              f"{done}/{len(batches)} 批达标。", "",
              "| # | 函数数 | run_id | status | exit_reason | 增量func% | 增量cond% |",
              "|---|-------|--------|--------|-------------|----------|----------|"]
        for b in batches:
            L.append(
                f"| {b['batch_index']} | {len(b['functions'])} | `{b.get('run_id', '-')}` | "
                f"{b.get('status', '-')} | {b.get('exit_reason', '-')} | "
                f"{b.get('func_pct', '-')} | {b.get('cond_pct', '-')} |")
        L.append("")
        unreachable = summary.get("unreachable") or []
        if unreachable:
            L += ["### 入口不可达（疑似死代码/未接线）", ""]
            for f, fn in unreachable:
                L.append(f"- `{f}` :: `{fn}`（CodeGraph 反向追溯无法到达 "
                         f"`entrypoints`，已在对应批次的 gen 上下文里明确提示）")
            L.append("")
    elif summary.get("exit_reason") == "no_changed_functions":
        L += ["## 覆盖轨结果", "", "无可信变更函数，未运行。", ""]

    # 扫描轨
    if scan_result is not None:
        L += [render_scan_markdown(scan_result), ""]

    L += ["## 产物索引", "",
          f"- 变更函数清单：`{path.parent / 'changed_functions.json'}`",
          f"- diff 原文：`{path.parent / 'code_diff.txt'}`",
          f"- 分批明细：`{path.parent / 'diff_batches.json'}`",
          f"- 汇总状态：`{path.parent / 'mr_summary.json'}`"]
    for b in summary.get("coverage_batches", []):
        if b.get("run_id"):
            L.append(f"- 覆盖轨批次 {b['batch_index']}："
                     f"`{cfg.runs_dir / b['run_id'] / 'loop_final_report.md'}`")
    if scan_result is not None:
        L.append(f"- 扫描轨裁决：`{path.parent / 'scan' / 'bug_verification.json'}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
