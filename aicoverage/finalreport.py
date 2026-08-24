"""最终报告生成器：把一次闭环的全部产物汇总为人可读的 Markdown。

报告章节（对应"评审者不看 JSON 就能复核"的目标）：
  1. 概览            —— 项目/需求/达标线/结论/最终覆盖率
  2. 每轮增量明细     —— 每轮覆盖率变化（绝对值 + Δpp + 新命中函数数）+ 各阶段结论
  3. 用例执行结果     —— 每轮 junit 统计（用例数/通过/失败/错误/跳过/耗时）+ 失败用例归因
  4. 用例清单        —— 逐文件列出用例函数（来自 manifest + 磁盘实测），标注所属轮次
  5. 未覆盖原因分析   —— 逐函数给出根因编码(N1-N6)/证据/建议，区分"可补"与"噪声/不可达"
  6. 疑似产品缺陷     —— quality-agent 判定的 report_bug
  7. 产物索引        —— HTML 报告地址（含打开方式）+ 各类 JSON 路径

设计原则：所有数字与结论都来自磁盘产物（loop_state/junit/execution/gap_items/
manifest/quality_report/coverage.json），本模块只做汇总排版，不做任何推断。
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import ProjectConfig
from .gcov import CoverageReport

# 根因编码 → 人类可读说明（与 prompts/coverage_agent.md 的分类体系一致）
CAUSE_LABELS: dict[str, str] = {
    "N1": "需要特定运行环境/多进程/信号（黑盒难触达）",
    "N2": "需要网络对端/真实协议交互",
    "N3": "错误路径（分配失败/解码失败/异常输入）",
    "N4": "需要精细输入构造（参数组合/边界值/状态机分支）",
    "N5": "疑似死代码/平台相关/无调用点（不建议强测）",
    "N6": "可直接触达的普通逻辑",
}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _iter_dirs(run_dir: Path) -> list[Path]:
    """按 iter 数值升序（避免 iter_10 排在 iter_2 前）。"""
    dirs = []
    for d in run_dir.glob("iter_*"):
        m = re.fullmatch(r"iter_(\d+)", d.name)
        if d.is_dir() and m:
            dirs.append((int(m.group(1)), d))
    return [d for _, d in sorted(dirs)]


def _junit_cases(junit_path: Path) -> tuple[dict, list[dict]]:
    """解析 junit.xml → (统计, 失败用例列表)。

    统计: {tests, failures, errors, skipped, time}
    失败用例: [{name, classname, kind, message}]
    """
    stats = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    failed: list[dict] = []
    if not junit_path.exists():
        return stats, failed
    try:
        root = ET.parse(junit_path).getroot()
    except (ET.ParseError, OSError):
        return stats, failed
    suites = root.findall(".//testsuite") or ([root] if root.tag == "testsuite" else [])
    for su in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            try:
                stats[key] += int(su.get(key, 0))
            except ValueError:
                pass
        try:
            stats["time"] += float(su.get("time", 0) or 0)
        except ValueError:
            pass
    for case in root.iter("testcase"):
        for tag, kind in (("failure", "failure"), ("error", "error")):
            node = case.find(tag)
            if node is not None:
                msg = (node.get("message") or (node.text or "")).strip()
                failed.append({
                    "name": case.get("name", ""),
                    "classname": case.get("classname", ""),
                    "kind": kind,
                    "message": " ".join(msg.split())[:220],
                })
    return stats, failed


def _collect_test_functions(test_dir: Path) -> dict[str, list[str]]:
    """扫描测试目录，得到 {文件名: [test 函数名]}（磁盘实测，非 manifest 声明）。"""
    result: dict[str, list[str]] = {}
    if not test_dir.is_dir():
        return result
    pattern = re.compile(r"^\s*def (test_\w+)", re.MULTILINE)
    for p in sorted(test_dir.glob("test_*.py")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        result[p.name] = pattern.findall(text)
    return result


def _gen_origin(run_dir: Path) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """从各轮 manifest 得到 {文件: [新建轮次]} 与 {文件: [修改轮次]}。"""
    created: dict[str, list[int]] = {}
    modified: dict[str, list[int]] = {}
    for d in _iter_dirs(run_dir):
        m = _load_json(d / "manifest.json")
        if not m:
            continue
        n = int(d.name.split("_")[1])
        for f in m.get("test_files") or []:
            created.setdefault(Path(str(f)).name, []).append(n)
        for f in m.get("modified_files") or []:
            modified.setdefault(Path(str(f)).name, []).append(n)
    return created, modified


def _uncovered_reasons(run_dir: Path) -> dict[tuple[str, str], dict]:
    """汇总所有轮次 gap_items 的根因判定，key=(file, function)。

    后出现的轮次覆盖先前结论（越靠后的分析越准确，通常带更完整证据）。
    同时并入 manifest 的 verdict_unreachable / verdict_noop（gen-agent 的复核结论，
    优先级最高——它是在读过源码后对"能否用黑盒用例补"的最终裁决）。
    """
    reasons: dict[tuple[str, str], dict] = {}
    for d in _iter_dirs(run_dir):
        gap = _load_json(d / "gap_items.json") or {}
        for bucket, group in (("items", "可补"), ("noise", "噪声")):
            for it in gap.get(bucket) or []:
                if not isinstance(it, dict):
                    continue
                key = (str(it.get("file", "")), str(it.get("function", "")))
                if not key[1]:
                    continue
                reasons[key] = {
                    "cause": it.get("cause", ""),
                    "group": group,
                    "priority": it.get("priority", ""),
                    "evidence": it.get("evidence", "") or it.get("note", ""),
                    "suggestion": it.get("suggestion", ""),
                    "verdict": "",
                    "iter": int(d.name.split("_")[1]),
                }
        manifest = _load_json(d / "manifest.json") or {}
        for bucket in ("verdict_unreachable", "verdict_noop"):
            for it in manifest.get(bucket) or []:
                if not isinstance(it, dict):
                    continue
                key = (str(it.get("file", "")), str(it.get("function", "")))
                if not key[1]:
                    continue
                entry = reasons.setdefault(key, {"cause": "", "group": "噪声",
                                                 "priority": "", "evidence": "",
                                                 "suggestion": "", "iter": 0})
                entry["verdict"] = it.get("verdict") or bucket
                # gen-agent 的 reason 通常最完整，优先作为证据
                if it.get("reason"):
                    entry["evidence"] = it["reason"]
                entry["group"] = "不可达/无收益"
    return reasons


def write_final_report(
    cfg: ProjectConfig,
    runs_dir: Path,
    run_id: str,
    state: dict,
    path: Path,
    html_index: Path | None = None,
) -> None:
    """生成最终 Markdown 报告。"""
    run_dir = runs_dir / run_id
    L: list[str] = []
    # 章节自动编号：保证五项必备内容编号连续，且不会因某章节缺席而跳号
    _sec = {"n": 0}

    def sec(title: str) -> None:
        _sec["n"] += 1
        L.append(f"## {_sec['n']}. {title}")

    # ── 1. 概览 ────────────────────────────────────────────────
    L += [f"# AIcoverage 闭环报告 — {run_id}", ""]
    L.append(f"- **项目**：{cfg.display_name}（`{cfg.source_path}`）")
    if state.get("requirement"):
        L.append(f"- **需求**：{state['requirement'][:500]}")
    thr = state.get("thresholds", {})
    L.append(f"- **达标线**：函数覆盖 ≥ {thr.get('func_pct')}% 且 分支覆盖 ≥ {thr.get('cond_pct')}%")
    L.append(f"- **结论**：**{state.get('status')}**（`{state.get('exit_reason')}`）")

    iters = sorted(state.get("iterations", []), key=lambda x: x.get("iter", 0))
    covered_iters = [it for it in iters if it.get("coverage_after")]
    baseline_cov = _baseline_coverage(run_dir)
    last_cov_path = _last_coverage_path(run_dir)
    final_report = CoverageReport.load(last_cov_path) if last_cov_path else None

    if final_report is not None:
        base_txt = ""
        if baseline_cov is not None:
            base_txt = (f"（起始 {baseline_cov.func_pct:.2f}% / {baseline_cov.cond_pct:.2f}%，"
                        f"累计提升 函数 +{final_report.func_pct - baseline_cov.func_pct:.2f}pp、"
                        f"分支 +{final_report.cond_pct - baseline_cov.cond_pct:.2f}pp）")
        L.append(f"- **最终覆盖率**：函数 **{final_report.func_pct:.2f}%** "
                 f"({final_report.func_hit}/{final_report.func_total})、"
                 f"分支 **{final_report.cond_pct:.2f}%** "
                 f"({final_report.branch_hit}/{final_report.branch_total})、"
                 f"行 {final_report.line_pct:.2f}% "
                 f"({final_report.line_hit}/{final_report.line_total}){base_txt}")
    if html_index is not None:
        L.append(f"- **HTML 覆盖率报告**：`{html_index}`")
    L.append("")

    # ── 2. 每轮增量明细 ────────────────────────────────────────
    sec("每轮覆盖率增量")
    L.append("")
    L.append("| 轮次 | 用例产出 | 执行结论 | 函数覆盖 | Δ函数 | 分支覆盖 | Δ分支 | 本轮新命中函数 |")
    L.append("|------|---------|---------|---------|-------|---------|-------|--------------|")
    if baseline_cov is not None:
        L.append(f"| 基线 | — | {_baseline_verdict(run_dir)} | "
                 f"{baseline_cov.func_pct:.2f}% ({baseline_cov.func_hit}/{baseline_cov.func_total}) | — | "
                 f"{baseline_cov.cond_pct:.2f}% ({baseline_cov.branch_hit}/{baseline_cov.branch_total}) | — | — |")
    for it in iters:
        cov = it.get("coverage_after") or {}
        d = it.get("delta") or {}
        func_txt = (f"{cov['func_pct']:.2f}% ({cov.get('func_hit','?')}/{cov.get('func_total','?')})"
                    if cov.get("func_pct") is not None else "—")
        cond_txt = (f"{cov['cond_pct']:.2f}% ({cov.get('branch_hit','?')}/{cov.get('branch_total','?')})"
                    if cov.get("cond_pct") is not None else "—")
        L.append(
            f"| {it.get('iter')} | {_gen_label(it.get('gen_output'))} | "
            f"{it.get('execute_verdict', '—')} | {func_txt} | {_pp(d.get('func_pp'))} | "
            f"{cond_txt} | {_pp(d.get('cond_pp'))} | {d.get('newly_hit', '—')} |")
    if not iters:
        L.append("| — | — | — | — | — | — | — | — |")
    L.append("")
    L.append("> Δ 为相对上一轮的百分点变化；「本轮新命中函数」= 上一轮未覆盖、本轮被覆盖的函数个数。")
    L.append("")

    stage_rows = _stage_summaries(run_dir)
    if stage_rows:
        L.append("### 各轮阶段结论")
        L.append("")
        L.append("| 轮次 | 缺口分析 | 用例生成 | 静态审查 | 质量分析 |")
        L.append("|------|---------|---------|---------|---------|")
        for r in stage_rows:
            L.append(f"| {r['iter']} | {r['gap']} | {r['gen']} | {r['verify']} | {r['quality']} |")
        L.append("")

    # ── 3. 用例执行结果 ────────────────────────────────────────
    sec("用例执行结果")
    L.append("")
    L.append("| 轮次 | verdict | 用例数 | 通过 | 失败 | 错误 | 跳过 | 耗时(s) |")
    L.append("|------|---------|-------|------|------|------|------|--------|")
    exec_rows = []
    for d in _iter_dirs(run_dir):
        execution = _load_json(d / "execution.json")
        if not execution:
            continue
        stats, failed = _junit_cases(d / "junit.xml")
        n = int(d.name.split("_")[1])
        passed = max(stats["tests"] - stats["failures"] - stats["errors"] - stats["skipped"], 0)
        label = "基线" if n == 0 else str(n)
        L.append(f"| {label} | {execution.get('verdict', '—')} | {stats['tests']} | {passed} | "
                 f"{stats['failures']} | {stats['errors']} | {stats['skipped']} | "
                 f"{execution.get('duration_s', stats['time']):.1f} |")
        exec_rows.append((label, failed))
    if not exec_rows:
        L.append("| — | 未执行 | 0 | 0 | 0 | 0 | 0 | 0.0 |")
    L.append("")
    if not exec_rows:
        L.append("⚠️ 本次运行没有任何轮次产出执行结果（`execution.json` 缺失）——"
                 "可能是插桩构建失败、gen 阶段未产出用例，或闭环在执行前就早停。")
        L.append("")

    fail_blocks = [(label, failed) for label, failed in exec_rows if failed]
    if fail_blocks:
        L.append("### 失败/错误用例明细与归因")
        L.append("")
        for label, failed in fail_blocks:
            L.append(f"**轮次 {label}**：")
            qual = _quality_for_iter(run_dir, label)
            for f in failed:
                name = f"{f['classname']}::{f['name']}" if f["classname"] else f["name"]
                L.append(f"- `{name}`（{f['kind']}）")
                if f["message"]:
                    L.append(f"  - 报错：{f['message']}")
                info = qual.get(f["name"]) or qual.get(name)
                if info:
                    L.append(f"  - 归因：**{info.get('kind', '?')}** — {info.get('evidence', '')}")
                    if info.get("suggestion"):
                        L.append(f"  - 修复建议：{info['suggestion']}")
            L.append("")
    elif exec_rows:
        L.append("最后一轮执行无失败/错误用例。")
        L.append("")

    # ── 4. 用例清单 ────────────────────────────────────────────
    disk_cases = _collect_test_functions(cfg.test_dir)
    created, modified = _gen_origin(run_dir)
    total_funcs = sum(len(v) for v in disk_cases.values())
    sec(f"用例清单（{len(disk_cases)} 个文件 / {total_funcs} 个用例函数）")
    L.append("")
    L.append(f"用例目录：`{cfg.test_dir}`　原子函数库：`{cfg.tests_lib_dir / 'harness.py'}`")
    L.append("")
    if not disk_cases:
        L.append(f"⚠️ 用例目录下未发现任何 `test_*.py`（检查路径：`{cfg.test_dir}`）。")
        L.append("")
    for fname, funcs in sorted(disk_cases.items()):
        tags = []
        if fname in created:
            tags.append(f"iter {','.join(str(i) for i in sorted(set(created[fname])))} 新建")
        if fname in modified:
            tags.append(f"iter {','.join(str(i) for i in sorted(set(modified[fname])))} 修改")
        if not tags:
            tags.append("闭环前已存在")
        L.append(f"- **`{fname}`**（{'；'.join(tags)}，{len(funcs)} 个用例）")
        for fn in funcs:
            L.append(f"  - `{fn}`")
    L.append("")

    # ── 5. 未覆盖原因分析（必备章节）────────────────────────────
    reasons = _uncovered_reasons(run_dir)
    unc = final_report.uncovered_functions() if final_report is not None else []
    if final_report is None:
        sec("未覆盖函数与原因")
        L.append("")
        L.append("⚠️ 本次运行未采集到覆盖率数据（`coverage.json` 缺失），无法列出未覆盖函数。")
        L.append("常见原因：插桩构建失败（见 `build.log`）、pytest 未真正执行、"
                 "或 gcov 未生成 `.gcda`（构建命令缺少 `--coverage`）。")
        if reasons:
            L.append("")
            L.append("以下是各轮 coverage-agent 已产出的根因判定（供参考）：")
            L.append("")
            L.append("| 文件 | 函数 | 根因 | 判定 | 原因/证据 |")
            L.append("|------|------|------|------|----------|")
            for (fpath, fname_), info in sorted(reasons.items()):
                L.append(f"| `{fpath}` | `{fname_}` | {info.get('cause') or '—'} | "
                         f"{info.get('verdict') or info.get('group') or '—'} | "
                         f"{_cell(info.get('evidence') or '—')} |")
        L.append("")
    else:
        sec(f"未覆盖函数与原因（{len(unc)} 个）")
        L.append("")
        if unc:
            counter: dict[str, int] = {}
            for f in unc:
                info = reasons.get((f.file, f.name), {})
                key = info.get("cause") or "未分类"
                counter[key] = counter.get(key, 0) + 1
            L.append("按根因分布：")
            for cause, cnt in sorted(counter.items()):
                L.append(f"- **{cause}**（{CAUSE_LABELS.get(cause, '未由 coverage-agent 分类')}）：{cnt} 个")
            L.append("")
            L.append("| 文件:行 | 函数 | 根因 | 判定 | 原因/证据 | 建议 |")
            L.append("|---------|------|------|------|----------|------|")
            for f in unc:
                info = reasons.get((f.file, f.name), {})
                cause = info.get("cause") or "—"
                verdict = info.get("verdict") or info.get("group") or "—"
                ev = _cell(info.get("evidence") or "（本轮未产出根因分析）")
                sug = _cell(info.get("suggestion") or "—")
                L.append(f"| `{f.file}:{f.start_line}` | `{f.name}` | {cause} | {verdict} | {ev} | {sug} |")
            L.append("")
            L.append("> 根因编码含义：" + "；".join(
                f"**{k}**={v}" for k, v in CAUSE_LABELS.items()) + "。")
            L.append("> 完整证据链见各轮 `iter_N/gap_items.json` 与 `iter_N/manifest.json` "
                     "（`verdict_unreachable` / `verdict_noop` 字段）。")
            L.append("")
        else:
            L.append("✅ 无未覆盖函数（全部函数均已被执行）。")
            L.append("")

    # ── 6. 疑似产品缺陷 ────────────────────────────────────────
    bugs: list[dict] = []
    for d in _iter_dirs(run_dir):
        q = _load_json(d / "quality_report.json") or {}
        for item in q.get("action_items") or []:
            if item.get("type") == "report_bug":
                bugs.append({**item, "iter": int(d.name.split("_")[1])})
    if bugs:
        sec("疑似产品缺陷（quality-agent 判定，待人工确认）")
        L.append("")
        for b in bugs:
            L.append(f"- **{b.get('file', '?')}**（iter {b['iter']}）：{b.get('suggestion', '')}")
        L.append("")

    # ── 7. 产物索引 ────────────────────────────────────────────
    sec("产物索引")
    L.append("")
    if html_index is not None:
        L.append(f"- **HTML 覆盖率报告**：`{html_index}`")
        L.append(f"  - 打开方式：`python3 -m http.server 8000 -d {html_index.parent}`"
                 " → 浏览 <http://127.0.0.1:8000/>")
        L.append("  - 内容：层级下钻式目录树导航 + 四列指标"
                 "（Function coverage / Uncovered functions / Condition-decision"
                 " coverage / Uncovered C-D）+ **每个函数一行的覆盖结果** +"
                 " 源码逐行着色与分支 T/F 标注")
    else:
        L.append("- **HTML 覆盖率报告**：⚠️ 本次未生成（覆盖率数据缺失或生成失败，"
                 "详见终端日志）。可在补齐 coverage.json 后手动生成："
                 "`aicov html --run-id " + run_id + "`")
    L.append(f"- 用例目录：`{cfg.test_dir}`（harness：`{cfg.tests_lib_dir / 'harness.py'}`）")
    L.append(f"- 状态机（单一真源）：`{run_dir / 'loop_state.json'}`")
    L.append(f"- 事件流：`{run_dir / 'events.jsonl'}`")
    L.append(f"- 需求分析 / 测试计划：`{run_dir / 'analysis.md'}`、`{run_dir / 'test_plan.json'}`")
    L.append(f"- 构建日志：`{run_dir / 'build.log'}`")
    L.append(f"- 各轮产物：`{run_dir}/iter_N/`")
    L.append("  - `gap_items.json`（缺口根因）、`manifest.json`（用例产出/不可达裁决）、"
             "`verify_report.json`（静态审查）")
    L.append("  - `junit.xml`、`pytest.log`、`execution.json`（执行）、`coverage.json`（覆盖率，含逐行计数）、"
             "`quality_report.json`（失败归因）")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ── 辅助 ────────────────────────────────────────────────────────

def _cell(text: str, limit: int = 200) -> str:
    """Markdown 表格单元格：压缩空白、转义竖线、截断。"""
    t = " ".join(str(text).split()).replace("|", "\\|")
    return t[:limit] + ("…" if len(t) > limit else "")


def _pp(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}pp"
    except (TypeError, ValueError):
        return str(v)


def _gen_label(v) -> str:
    return {"ok": "有新用例", "empty": "无新用例", None: "—"}.get(v, str(v))


def _baseline_coverage(run_dir: Path) -> CoverageReport | None:
    for p in (run_dir / "iter_0" / "coverage.json", run_dir / "baseline_coverage.json"):
        if p.exists():
            try:
                return CoverageReport.load(p)
            except (OSError, json.JSONDecodeError):
                continue
    return None


def _baseline_verdict(run_dir: Path) -> str:
    execution = _load_json(run_dir / "iter_0" / "execution.json")
    return execution.get("verdict", "—") if execution else "未执行（无已有用例）"


def _last_coverage_path(run_dir: Path) -> Path | None:
    covs = [d / "coverage.json" for d in _iter_dirs(run_dir) if (d / "coverage.json").exists()]
    if covs:
        return covs[-1]
    fallback = run_dir / "baseline_coverage.json"
    return fallback if fallback.exists() else None


def _quality_for_iter(run_dir: Path, label: str) -> dict[str, dict]:
    """某轮 quality_report 的失败归因，按用例名索引。"""
    if label == "基线":
        return {}
    q = _load_json(run_dir / f"iter_{label}" / "quality_report.json") or {}
    out: dict[str, dict] = {}
    for f in q.get("failures") or []:
        test = str(f.get("test", ""))
        out[test] = f
        if "::" in test:
            out[test.split("::")[-1]] = f
    return out


def _stage_summaries(run_dir: Path) -> list[dict]:
    """每轮各阶段一句话结论。"""
    rows = []
    for d in _iter_dirs(run_dir):
        n = int(d.name.split("_")[1])
        if n == 0:
            continue
        gap = _load_json(d / "gap_items.json") or {}
        manifest = _load_json(d / "manifest.json") or {}
        verify = _load_json(d / "verify_report.json") or {}
        quality = _load_json(d / "quality_report.json") or {}
        gap_txt = "—"
        if gap:
            gap_txt = (f"P0 {len(gap.get('items') or [])} / 噪声 "
                       f"{len(gap.get('noise') or [])}（共 {gap.get('total_uncovered', '?')} 未覆盖）")
        gen_txt = "—"
        if manifest:
            n_files = len(manifest.get("test_files") or [])
            n_funcs = len(manifest.get("new_functions") or [])
            n_mod = len(manifest.get("modified_files") or [])
            if n_files or n_funcs or n_mod:
                gen_txt = f"新建 {n_files} 文件 / {n_funcs} 用例"
                if n_mod:
                    gen_txt += f"，修改 {n_mod} 文件"
            else:
                unreach = len(manifest.get("verdict_unreachable") or []) + \
                          len(manifest.get("verdict_noop") or [])
                gen_txt = (f"无新用例（判定 {unreach} 个函数黑盒不可达/无收益）"
                           if unreach else "无新用例")
        verify_txt = "—"
        if verify:
            problems = verify.get("problems") or []
            errs = sum(1 for p in problems if p.get("severity") == "error")
            verify_txt = f"{verify.get('verdict', '?')}（error {errs} / warn {len(problems) - errs}）"
        quality_txt = "—"
        if quality:
            m = quality.get("metrics") or {}
            quality_txt = (f"{quality.get('verdict', '?')}（失败 {len(quality.get('failures') or [])}，"
                           f"action {len(quality.get('action_items') or [])}）")
            if m.get("tests"):
                quality_txt += f"，{m['tests']} 用例"
        rows.append({"iter": n, "gap": gap_txt, "gen": gen_txt,
                     "verify": verify_txt, "quality": quality_txt})
    return rows
