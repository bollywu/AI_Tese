"""增量覆盖率：从全量 `CoverageReport` 收窄出"仅含变更函数集合"的子集视图。

对应改造计划文档 §3.4——**函数级** scope 收窄（非逐行精确 diff 覆盖率）：

    增量 func_pct = 变更函数集合中"整个函数体"被执行过的比例
    增量 cond_pct = 变更函数集合内"整个函数体"的分支覆盖比例

这不是重新采集覆盖率，是对已采集的 `CoverageReport` 重新聚合出一份子集视图，
复用现有 `FileCov`/`FunctionCov`/`BranchCov` 结构与 `CoverageReport` 的全部
既有属性（`func_pct`/`cond_pct`/`delta()`/`uncovered_functions()` 等），
不新增任何判定逻辑——`loop.py` 的达标判断可以原样复用，只是喂给它的
`report` 换成这里算出来的子集。
"""
from __future__ import annotations

from .gcov import BranchCov, CoverageReport, FileCov

#: 变更函数的最小表示：(file, bare_name)，与 diffextract.ChangedFunction.as_target()
#: 的产出格式一致，也与 gcov.py 里函数字典的 key（demangled/bare name）对齐。
TargetFunctions = list[tuple[str, str]]


def _group_by_file(target_functions: TargetFunctions) -> dict[str, set[str]]:
    wanted: dict[str, set[str]] = {}
    for f, fn in target_functions:
        wanted.setdefault(f, set()).add(fn)
    return wanted


def scope_report(full: CoverageReport, target_functions: TargetFunctions) -> CoverageReport:
    """从全量 `CoverageReport` 中筛出只含 `target_functions` 的子集视图。

    分母收窄规则：
    - 函数：按 (file, name) 精确匹配。
    - 分支：`BranchCov.function` 是该分支的宿主函数名，收窄到同一批被选函数。
    - 行：只保留落在被选函数 `[start_line, end_line]` 区间内的行（用于 HTML
      报告的"仅显示变更函数"逐行着色模式）。

    不在 `full` 里出现的 target（拼写错误/该翻译单元未插桩/函数已删除）
    **不会**被静默忽略也不会被当成 0% ——用 `missing_targets()` 单独查出来，
    调用方必须在报告里明确说明，不能和"存在但未执行"混为一谈。
    """
    wanted = _group_by_file(target_functions)
    scoped = CoverageReport(created_at=full.created_at)

    for file, fc in full.files.items():
        names = wanted.get(file)
        if not names:
            continue
        new_fc = FileCov(file=file)
        for name, func in fc.functions.items():
            if name in names:
                new_fc.functions[name] = func
        if not new_fc.functions:
            continue

        new_fc.branches = [b for b in fc.branches if b.function in names]

        ranges = [(f2.start_line, f2.end_line) for f2 in new_fc.functions.values()]
        new_fc.line_counts = {
            ln: c for ln, c in fc.line_counts.items()
            if any(s <= ln <= e for s, e in ranges)
        }
        new_fc.lines_total = len(new_fc.line_counts)
        new_fc.lines_hit = sum(1 for c in new_fc.line_counts.values() if c > 0)
        scoped.files[file] = new_fc

    return scoped


def missing_targets(full: CoverageReport, target_functions: TargetFunctions) -> list[tuple[str, str]]:
    """target_functions 中不存在于 `full` 覆盖率数据里的 (file, name) 对。

    可能原因：函数名/文件名拼写不一致、该翻译单元未参与插桩构建、函数刚被
    删除但 diff 提取时机滞后。调用方（报告生成）必须把这些单独列出来，
    不能悄悄计入分母也不能当作"0% 覆盖"（那会和"真实存在但未执行"混淆，
    误导"未达标原因"分析）。
    """
    existing = {(f, name) for f, fc in full.files.items() for name in fc.functions}
    return [(f, fn) for f, fn in target_functions if (f, fn) not in existing]


def incremental_delta(
    before: CoverageReport, after: CoverageReport, target_functions: TargetFunctions,
) -> dict:
    """相对上一轮的增量（收窄到 target_functions 后再算 delta，复用
    `CoverageReport.delta()`，不重新发明增量计算逻辑）。"""
    scoped_before = scope_report(before, target_functions)
    scoped_after = scope_report(after, target_functions)
    d = scoped_after.delta(scoped_before)
    d.update({
        "scope_func_total": scoped_after.func_total,
        "scope_func_hit": scoped_after.func_hit,
        "scope_func_pct": scoped_after.func_pct,
        "scope_branch_total": scoped_after.branch_total,
        "scope_branch_hit": scoped_after.branch_hit,
        "scope_cond_pct": scoped_after.cond_pct,
    })
    return d


def scope_threshold_met(
    report: CoverageReport, target_functions: TargetFunctions,
    func_target: float, cond_target: float,
) -> tuple[bool, CoverageReport]:
    """判断收窄后的 scope 是否达标，返回 (是否达标, scope_report)。

    `loop.py` 现有的达标判断（`func_pct >= func_target and cond_pct >=
    cond_target`）原样复用，只是判断对象换成这里返回的 `scope_report`。
    """
    scoped = scope_report(report, target_functions)
    met = scoped.func_pct >= func_target and scoped.cond_pct >= cond_target
    return met, scoped
