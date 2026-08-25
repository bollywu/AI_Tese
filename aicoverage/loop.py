"""Coverage-loop state machine (deterministically driven).

Flow (per iteration):

    [0] analyze   -- analyzer-agent: requirement parsing + source understanding (once, fail-soft)
    [1] build     -- deterministic instrumented build (early-stop on failure)
    [2] baseline  -- run existing cases once for a baseline coverage (or a gcov all-zero list if none)
    loop iter 1..max_iter:
      [a] gap      -- coverage-agent: uncovered-function root-cause classification (fail-soft, degrades to a bare list)
      [b] gen      -- gen-agent: generate/fix cases -> manifest.json
      [c] verify   -- verify-agent: static review; fail -> gen fix loop (<=max_verify_retry)
      [d] execute  -- deterministic executor: pytest + gcov collection -> junit/execution/coverage
      [e] quality  -- quality-agent (when execution not PASS): failure attribution -> action_items
      [f] update   -- state/event/delta update, threshold or early-stop decision
    [3] final     -- loop_final_report.md

Exit conditions: threshold_met | max_iter_reached | execute_fail_loop |
                 coverage_ceiling | gen_no_output | verify_fail_exceeded | build_failed
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from . import observability as obs
from . import state as st
from .agent_call import call_agent
from .build import build as do_build
from .config import ProjectConfig
from .docstyle import check_test_docstrings
from .executor import run_tests
from .gcov import CoverageReport, collect as gcov_collect
from .runner import AgentRunner


# ── Prompt construction ──────────────────────────────────────────────

def _prompt_analyze(cfg: ProjectConfig, run_dir: Path, requirement: str,
                    files_preview: str) -> str:
    from .kb import wiki_navigation_hint
    req_part = f"## 需求描述\n{requirement}\n" if requirement else ""
    return f"""对被测项目做需求解析与测试策划。

项目：{cfg.display_name}（{cfg.language}）
源码根：$AICOV_SRC = {cfg.source_path}
{wiki_navigation_hint(cfg)}{req_part}
## 源码文件清单（include 范围内）
{files_preview}

## 任务
按你的 SOP 分析源码，产出：
1. 分析报告 → {run_dir / "analysis.md"}
2. 测试计划 → {run_dir / "test_plan.json"}

完成后输出一行摘要。"""


def _prompt_gap(cfg: ProjectConfig, run_id: str, iter_n: int, iter_dir: Path,
                uncovered: list[dict], report_summary: dict) -> str:
    from .kb import wiki_navigation_hint
    items = json.dumps(uncovered[:60], ensure_ascii=False, indent=1)
    return f"""本轮覆盖率缺口分析（run_id={run_id} iter={iter_n}）。

当前覆盖率（确定性 gcov 采集，勿改动数字）：
- 函数: {report_summary.get('func_hit', 0)}/{report_summary.get('func_total', 0)} = {report_summary.get('func_pct', 0)}%
- 分支: {report_summary.get('branch_hit', 0)}/{report_summary.get('branch_total', 0)} = {report_summary.get('cond_pct', 0)}%
{wiki_navigation_hint(cfg)}
覆盖率明细: {iter_dir / "coverage.json"}
未覆盖函数（前 60 个）：
{items}

## 任务
逐个 Read 未覆盖函数的源码，按 N1-N6 分类根因，产出 → {iter_dir / "gap_items.json"}
P0（N3/N4/N6）进 items（≤25 个），其余进 noise。"""


def _unittest_hint(cfg: ProjectConfig) -> str:
    """单测通道引导：当缺口根因是 e2e 不可达（N1/N3/N5）时，提示 gen-agent
    走"直接调用目标函数"的单测通道，而不是死磕 run_binary 黑盒触发。"""
    cc = cfg.ut_compiler or "（跟随 build 体系，建议 gcc/g++）"
    return f"""
## 单测通道（e2e 不可达函数专用）
若某 gap 根因是 **N1（特定运行环境/多进程/信号）、N3（错误路径）、N5（死代码/平台相关/无调用点）**，
说明它难以/无法通过被测二进制 $AICOV_BINARY 的正常 E2E 流程触达。此时请走**单测通道**：
1. 写一个 `test_driver_<主题>.c`（含 main），`#include` 或 extern 声明目标函数，直接调用它并打印返回值/副作用
2. 用例体调 harness 原子函数：
   ```python
   res = compile_unit_driver("tests/drivers/test_driver_<主题>.c",
                             sources=["src/<目标函数所在文件>.c"],
                             out_name="ut_<主题>", include_dirs=["src"])
   assert_ut_compiled(res)
   r = run_driver("ut_<主题>", args=["..."])   # 传参让 driver 走不同分支
   assert_exit_code(r, 0)
   assert_stdout_contains(r, "<预期输出>")
   ```
3. driver 源文件放 `tests/drivers/`；单测二进制自动落 `{cfg.ut_obj_path}`（--coverage 插桩，
   gcov 采集天然兼容）。单测编译器：`{cc}`
4. 若目标函数依赖项目私有结构体/宏，driver 里 `#include` 对应头文件即可（include_dirs 传头文件目录）。
注意：单测只用于补 e2e 不可达的函数，能 E2E 触达的（N4/N6）仍优先 run_binary。
"""


def _prompt_gen(cfg: ProjectConfig, run_id: str, iter_n: int, iter_dir: Path,
                gap_items: list[dict], plan_summary: str,
                quality_actions: list[dict] | None,
                manifest_path: Path, *, target_context: str = "") -> str:
    from .badcase import badcase_hint
    from .kb import wiki_navigation_hint
    gap_json = json.dumps(gap_items[:25], ensure_ascii=False, indent=1)
    fix_part = ""
    if quality_actions:
        fix_part = ("## 上一轮失败修复（优先处理）\n"
                    + json.dumps(quality_actions, ensure_ascii=False, indent=1) + "\n")
    plan_part = f"## 测试计划（analyzer 产物摘要）\n{plan_summary}\n" if plan_summary else ""
    ctx_part = f"## MR 增量上下文（本次闭环只针对这些变更函数）\n{target_context}\n" if target_context else ""
    return f"""生成第 {iter_n} 轮测试用例（run_id={run_id}）。

被测项目：{cfg.display_name}（{cfg.language}），源码根 $AICOV_SRC = {cfg.source_path}
被测二进制：$AICOV_BINARY = {cfg.binary_path}
测试目录：$AICOV_TEST_DIR = {cfg.test_dir}
harness 原子函数库：{cfg.tests_lib_dir / "harness.py"}（先 Read 它！）
{wiki_navigation_hint(cfg)}{badcase_hint(cfg)}
{_unittest_hint(cfg)}
{plan_part}{fix_part}{ctx_part}## 本轮覆盖缺口（gap_items，按优先级排序）
{gap_json}

## 任务
1. Read harness.py 了解可用原子函数（缺什么先补什么）
2. Read 目标函数源码，断言预期值必须来自源码真实逻辑
3. 生成/修复 pytest 用例到 {cfg.test_dir}/（文件名 test_<主题>_<序号>.py）
4. 写 manifest → {manifest_path}

遵守原子函数搭积木铁律。绝不执行 pytest。"""


def _prompt_gen_fix(cfg: ProjectConfig, iter_dir: Path, problems: list[dict],
                    manifest_path: Path) -> str:
    return f"""修复以下静态审查问题（verify-agent 报告）。

测试目录：{cfg.test_dir}
问题清单：
{json.dumps(problems, ensure_ascii=False, indent=1)}

逐条修复后更新 manifest → {manifest_path}
（只修复列出的问题，不要大改其他用例。）"""


def _snapshot_manifest_files(cfg: ProjectConfig, manifest: dict) -> dict[str, str]:
    """快照 manifest 声明文件的（相对路径 → 内容 sha1），用于比对 gen 修复是否落盘。

    若 verify 时序上读到旧文件（gen 修复晚于 verify 快照），此指纹可揭示
    "gen 实际改动了文件但 verify 报告基于旧版"的假早停。
    """
    import hashlib
    snap: dict[str, str] = {}
    for f in manifest.get("test_files", []):
        p = cfg.test_dir / f
        try:
            snap[str(f)] = hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            snap[str(f)] = ""
    return snap


def _has_fix_progress(before: dict[str, str], after: dict[str, str],
                      manifest: dict) -> bool:
    """判断 gen 修复是否改动到 verify 关心的问题文件（即修复有实质进展）。

    before/after 为 _snapshot_manifest_files 结果。有任一文件内容变化即视为有进展。
    """
    return any(before.get(f) != after.get(f) for f in before)


def _prompt_verify(run_id: str, iter_n: int, iter_dir: Path,
                   manifest: dict) -> str:
    files = manifest.get("test_files", [])
    return f"""静态审查本轮生成的用例（run_id={run_id} iter={iter_n}）。

测试目录：$AICOV_TEST_DIR = 测试目录见环境变量
manifest 声明的文件：{json.dumps(files, ensure_ascii=False)}
harness：见环境变量 AICOV_TEST_DIR 下 lib/harness.py

逐文件按 V1-V5 清单审查，产出 → {iter_dir / "verify_report.json"}"""


def _prompt_quality(run_id: str, iter_n: int, iter_dir: Path,
                    execution: dict, known_badcases: str = "") -> str:
    return f"""分析本轮执行失败（run_id={run_id} iter={iter_n}）。

执行结果：{json.dumps(execution, ensure_ascii=False, indent=1)}
junit：{iter_dir / "junit.xml"}
pytest 日志：{iter_dir / "pytest.log"}
覆盖率：{iter_dir / "coverage.json"}
测试目录/harness/源码路径：见环境变量 AICOV_TEST_DIR / AICOV_SRC
{known_badcases}
按失败归因分类逐个分析，产出 → {iter_dir / "quality_report.json"}
（含 badcase_candidates 字段：只提议**新的**可泛化失败模式，与上方已知条目
同模式的不重复提议；无新模式输出空数组。）"""


# ── Main loop ────────────────────────────────────────────────────────

async def run_loop(
    cfg: ProjectConfig,
    *,
    requirement: str = "",
    func_target: float | None = None,
    cond_target: float | None = None,
    max_iter: int | None = None,
    skip_analyze: bool = False,
    skip_gap_agent: bool = False,
    skip_build: bool = False,
    target_functions: list[tuple[str, str]] | None = None,
    target_context: str = "",
    quiet: bool = False,
) -> dict:
    """覆盖率闭环主入口。

    Args:
        skip_build: True 时跳过 [1] 插桩构建（多批 MR 闭环复用同一份构建产物时用，
            调用方需自行保证二进制已是最新插桩版本）。
        target_functions: 非空时进入 **scope 收窄模式**（MR 增量闭环用）：
            [(file, bare_func_name), ...]。gap 分析与达标判断的分母全部收窄到
            该集合（函数级增量覆盖率，见 incremental.py），其余行为不变。
        target_context: 注入 gen prompt 的目标上下文（MR 模式下传"调用链+
            改动说明"，帮助 gen-agent 理解触发路径；空字符串时忽略）。
    """
    func_target = func_target if func_target is not None else cfg.func_target
    cond_target = cond_target if cond_target is not None else cfg.cond_target
    max_iter = max_iter or cfg.max_iter

    from .incremental import missing_targets, scope_report

    runs_dir = cfg.runs_dir
    run_id = st.gen_run_id("LOOP")
    run_dir = runs_dir / run_id
    scope_tag = (f" scope={len(target_functions)}funcs"
                 if target_functions else "")
    print(f"▶ 闭环启动 run_id={run_id}（func≥{func_target}% cond≥{cond_target}% "
          f"max_iter={max_iter}{scope_tag}）")

    thresholds = {"func_pct": float(func_target), "cond_pct": float(cond_target)}
    limits = {"max_iter": int(max_iter),
              "max_verify_retry": int(cfg.max_verify_retry),
              "no_progress_iters": cfg.no_progress_stop}
    state = st.init_loop_state(runs_dir, run_id, "manual", thresholds, limits, requirement)
    if target_functions:
        st.update_state(runs_dir, run_id, {
            "scope": {"target_functions": [list(t) for t in target_functions],
                      "mode": "incremental"},
        })
    obs.emit("loop.start", run_id, runs_dir=runs_dir,
             data={"requirement": requirement[:200], "thresholds": thresholds,
                   "target_functions": len(target_functions or [])})
    os.environ.update(cfg.to_env(run_dir=run_dir))

    def _mk_runner(iter_dir: Path | None = None) -> AgentRunner:
        return AgentRunner(cfg, quiet=quiet, run_dir=run_dir, iter_dir=iter_dir)

    async def _call(agent: str, prompt: str, iter_dir: Path, iter_n: int,
                    stage: str, retries: int = 2):
        os.environ.update(cfg.to_env(run_dir=run_dir, iter_dir=iter_dir))
        return await call_agent(
            _mk_runner(iter_dir), run_id, agent, prompt,
            runs_dir=runs_dir, iter_n=iter_n, stage=stage, max_retries=retries,
        )

    def _read_json(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # ── [0] Requirement parsing (fail-soft) ─────────────────────
    plan_summary = ""
    if not skip_analyze:
        print("▶ [0] 需求解析（analyzer-agent）")
        files = cfg.source_files()
        files_preview = "\n".join(
            p.relative_to(cfg.source_path).as_posix() for p in files[:80]
        ) or "（include_globs 未匹配到文件）"
        obs.emit("stage.enter", run_id, stage="analyze", runs_dir=runs_dir)
        res = await _call("analyzer-agent",
                          _prompt_analyze(cfg, run_dir, requirement, files_preview),
                          run_dir, 0, "analyze")
        plan = _read_json(run_dir / "test_plan.json")
        if plan and plan.get("targets"):
            plan_summary = json.dumps(plan["targets"][:30], ensure_ascii=False)
            print(f"  ✅ 分析完成：{len(plan['targets'])} 个测试目标")
        else:
            print("  ⚠️ analyzer 未产出有效计划（降级为纯覆盖率驱动）")
        obs.emit("stage.exit", run_id, stage="analyze", runs_dir=runs_dir,
                 data={"success": res.success, "plan": bool(plan)})

    # ── [1] Instrumented build ──────────────────────────────────
    if skip_build:
        print("▶ [1] 插桩构建（跳过——调用方保证二进制已是最新插桩版本）")
    else:
        print("▶ [1] 插桩构建")
        obs.emit("stage.enter", run_id, stage="build", runs_dir=runs_dir)
        build_res = do_build(cfg, log_dir=run_dir)
        if not build_res.ok:
            obs.emit_diagnostic("BUILD_FAIL" if build_res.gcno_count == 0 else "BUILD_FAIL",
                                run_id, message=build_res.failure_reason,
                                stage="build", runs_dir=runs_dir)
            obs.emit("build.fail", run_id, runs_dir=runs_dir, data=build_res.to_dict())
            st.set_exit(runs_dir, run_id, "early_stop", f"build_failed: {build_res.failure_reason}")
            return _finalize(cfg, runs_dir, run_id)
        obs.emit("build.ok", run_id, runs_dir=runs_dir, data=build_res.to_dict())
        print(f"  ✅ 构建成功（{build_res.gcno_count} 个插桩单元，{build_res.duration_s:.1f}s）")

    # ── [2] Baseline coverage ───────────────────────────────────
    baseline_dir = run_dir / "iter_0"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    existing_tests = list(cfg.test_dir.glob("test_*.py")) if cfg.test_dir.exists() else []
    print(f"▶ [2] 基线覆盖率（已有用例 {len(existing_tests)} 个）")
    if existing_tests:
        exec0 = run_tests(cfg, baseline_dir)
        baseline_cov_path = baseline_dir / "coverage.json"
    else:
        exec0 = None
        baseline_cov = gcov_collect(
            cfg.source_path, cfg.gcov_bin,
            include_filter=cfg.include_globs, exclude_filter=cfg.exclude_globs)
        baseline_cov_path = run_dir / "baseline_coverage.json"
        baseline_cov.save(baseline_cov_path)
    baseline_report = CoverageReport.load(baseline_cov_path)
    previous: CoverageReport | None = None
    print(f"  基线：func={baseline_report.func_pct:.2f}% cond={baseline_report.cond_pct:.2f}%")

    consecutive_gen_empty = 0
    quality_actions: list[dict] = []

    # ── Iteration ───────────────────────────────────────────────
    for iter_n in range(1, max_iter + 1):
        iter_dir = st.iter_dir(runs_dir, run_id, iter_n)
        st.start_iteration(runs_dir, run_id, iter_n)
        print(f"\n▶ 迭代 {iter_n}/{max_iter}")
        gap_source = previous if previous is not None else baseline_report
        # scope mode: gap only looks at uncovered functions in the narrowed view (incremental denominator)
        if target_functions:
            gap_source_view = scope_report(gap_source, target_functions)
        else:
            gap_source_view = gap_source
        uncovered = [f.to_dict() for f in gap_source_view.uncovered_functions()]
        if not uncovered:
            # Function level fully covered. Two cases:
            # a) branches also meet the target (or no testable branch) -> clean threshold_met exit
            # b) some branch unhit -> continue with "functions containing unhit branches" as the
            #    gap (the function-level uncovered list misses this; a bare break can never reach 85%)
            prev_view = (scope_report(previous, target_functions)
                         if target_functions and previous is not None
                         else (previous if previous is not None else baseline_report))
            cond_ok = (gap_source_view.cond_pct >= cond_target
                       or gap_source_view.branch_total == 0)
            if cond_ok:
                # vacuous cond: when no testable branch, the cond display records 100%
                # (cond_vacuous marks the real semantics), avoiding the "met but shows 0%"
                # self-contradiction
                vacuous = gap_source_view.branch_total == 0
                cond_out = 100.0 if vacuous else gap_source_view.cond_pct
                print("  ✅ 无未覆盖函数且分支达标"
                      + ("（scope 内）" if target_functions else "")
                      + ("（cond vacuous：无可测分支）" if vacuous else ""))
                st.update_iteration(runs_dir, run_id, iter_n, {
                    "coverage_after": {"func_pct": gap_source_view.func_pct,
                                       "cond_pct": cond_out,
                                       "func_hit": gap_source_view.func_hit,
                                       "func_total": gap_source_view.func_total,
                                       "branch_hit": gap_source_view.branch_hit,
                                       "branch_total": gap_source_view.branch_total,
                                       **({"cond_vacuous": True} if vacuous else {})},
                    "delta": gap_source_view.delta(prev_view),
                })
                st.set_exit(runs_dir, run_id, "done", "threshold_met",
                            {"func_pct": gap_source_view.func_pct,
                             "cond_pct": cond_out,
                             **({"cond_vacuous": True} if vacuous else {})})
                obs.emit("loop.threshold_met", run_id, runs_dir=runs_dir,
                         data={"iter": iter_n})
                break
            # Branches not met: use functions containing unhit branches as the gap source
            partial = sorted({b.function for fc in gap_source_view.files.values()
                              for b in fc.branches if not b.hit and b.function})
            uncovered = [
                {"file": f.file, "name": f.name, "start_line": f.start_line,
                 "cause": "N4", "priority": "P0",
                 "suggestion": "函数已执行但存在未命中分支，需补充分支覆盖输入"}
                for f in gap_source_view.functions if f.name in partial
            ]
            if not uncovered:
                print("  ⚠️ 函数全覆盖但分支未达标，且无法定位含未命中分支的函数")
                st.update_iteration(runs_dir, run_id, iter_n, {
                    "coverage_after": {"func_pct": gap_source_view.func_pct,
                                       "cond_pct": gap_source_view.cond_pct},
                })
                st.set_exit(runs_dir, run_id, "early_stop", "coverage_ceiling",
                            {"func_pct": gap_source_view.func_pct,
                             "cond_pct": gap_source_view.cond_pct})
                break
            print(f"  ℹ️ 函数已全覆盖，仍有 {len(uncovered)} 个函数存在未命中分支，继续补分支")

        # [a] gap analysis
        cov_path = iter_dir / "coverage_in.json"
        gap_source.save(cov_path)
        if skip_gap_agent:
            gap_items = [{"file": u["file"], "function": u["name"],
                          "start_line": u["start_line"], "cause": "N6",
                          "priority": "P0",
                          "suggestion": "直接触达"} for u in uncovered]
        else:
            print("  [a] 缺口根因分析（coverage-agent）")
            obs.emit("stage.enter", run_id, iter_n=iter_n, stage="gap", runs_dir=runs_dir)
            await _call("coverage-agent",
                        _prompt_gap(cfg, run_id, iter_n, iter_dir, uncovered,
                                    gap_source.to_dict()["summary"]),
                        iter_dir, iter_n, "gap", retries=1)
            gap_data = _read_json(iter_dir / "gap_items.json")
            if gap_data and gap_data.get("items"):
                gap_items = gap_data["items"]
                print(f"      P0 缺口 {len(gap_items)} 个（noise {len(gap_data.get('noise', []))}）")
            else:
                gap_items = [{"file": u["file"], "function": u["name"],
                              "start_line": u["start_line"], "cause": "N6",
                              "priority": "P0"} for u in uncovered]
                print("      ⚠️ gap_items 缺失，降级为裸清单")
            obs.emit("stage.exit", run_id, iter_n=iter_n, stage="gap", runs_dir=runs_dir)

        # [b] case generation
        print("  [b] 用例生成（gen-agent）")
        manifest_path = iter_dir / "manifest.json"
        obs.emit("stage.enter", run_id, iter_n=iter_n, stage="gen", runs_dir=runs_dir)
        gen_result = await _call(
            "gen-agent",
            _prompt_gen(cfg, run_id, iter_n, iter_dir, gap_items, plan_summary,
                        quality_actions or None, manifest_path,
                        target_context=target_context),
            iter_dir, iter_n, "gen", retries=2)
        quality_actions = []  # only reflux one round
        manifest = _read_json(manifest_path)
        if not manifest or not manifest.get("test_files"):
            consecutive_gen_empty += 1
            obs.emit_diagnostic("GEN_NO_OUTPUT", run_id,
                                message=f"iter {iter_n} gen 未产出用例",
                                iter_n=iter_n, stage="gen", runs_dir=runs_dir)
            print("      ⚠️ gen 未产出用例")
            if consecutive_gen_empty >= 2:
                st.set_exit(runs_dir, run_id, "early_stop", "gen_no_output")
                break
            st.update_iteration(runs_dir, run_id, iter_n, {"gen_output": "empty"})
            continue
        consecutive_gen_empty = 0
        obs.emit("artifact.write", run_id, iter_n=iter_n, stage="gen",
                 runs_dir=runs_dir,
                 data={"manifest": str(manifest_path),
                       "files": manifest.get("test_files", [])})
        obs.emit("stage.exit", run_id, iter_n=iter_n, stage="gen", runs_dir=runs_dir,
                 data={"success": gen_result.success})
        print(f"      生成 {len(manifest.get('test_files', []))} 个文件 / "
              f"{len(manifest.get('new_functions', []))} 个用例函数")

        # [c] static review (fail -> gen fix loop)
        # c0: deterministic doc-header gate (zero LLM cost) -- every test_* function's docstring
        # must contain "描述" + "测试点" fields for manual static review (no need to run pytest
        # and read logs to know what a case tests). The result is merged into verify_report.json
        # (EC-07), complementing verify-agent's semantic review (V1-V5) without extra tokens.
        print("  [c] 静态审查（verify-agent + 文档头门禁）")
        verify_ok = False
        for attempt in range(1 + limits["max_verify_retry"]):
            obs.emit("stage.enter", run_id, iter_n=iter_n, stage="verify", runs_dir=runs_dir)
            await _call("verify-agent",
                        _prompt_verify(run_id, iter_n, iter_dir, manifest),
                        iter_dir, iter_n, "verify", retries=1)
            report = _read_json(iter_dir / "verify_report.json") or {
                "verdict": "fail", "problems": [],
                "summary": "verify-agent 未产出报告",
            }
            doc_problems = check_test_docstrings(cfg.test_dir, manifest.get("test_files", []))
            if doc_problems:
                report["problems"] = list(report.get("problems", [])) + doc_problems
                report["verdict"] = "fail"
                (iter_dir / "verify_report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"      ⚠️ 文档头门禁未过：{len(doc_problems)} 个用例缺少"
                      "「描述/测试点」docstring")
            problems = report.get("problems", [])
            errors = [p for p in problems if p.get("severity") == "error"]
            obs.emit("stage.exit", run_id, iter_n=iter_n, stage="verify",
                     runs_dir=runs_dir,
                     data={"verdict": report.get("verdict"),
                           "errors": len(errors), "warns": len(problems) - len(errors),
                           "doc_gate_violations": len(doc_problems)})
            if report.get("verdict") == "pass":
                verify_ok = True
                print(f"      ✅ 审查通过（warn {len(problems)}）")
                break
            if attempt >= limits["max_verify_retry"]:
                break
            print(f"      ⚠️ 审查未过（error {len(errors)}），回环修复（第 {attempt + 1} 次）")
            # timing-guard against false positives: snapshot the manifest files' content
            # fingerprint before gen_fix, compare after. If gen didn't actually change any file
            # (verify reported old issues but gen fixed without persisting / verify read a stale
            # version timing-wise), avoid a pointless next verify round on the "old files".
            before = _snapshot_manifest_files(cfg, manifest)
            await _call("gen-agent",
                        _prompt_gen_fix(cfg, iter_dir, problems, manifest_path),
                        iter_dir, iter_n, "gen_fix")
            manifest = _read_json(manifest_path) or manifest
            after = _snapshot_manifest_files(cfg, manifest)
            changed = {f for f in before if before[f] != after.get(f)}
            if not changed:
                obs.emit_diagnostic(
                    "GEN_FIX_NO_CHANGE", run_id,
                    message=f"iter {iter_n} gen 修复后 manifest 文件内容未变化，"
                            f"verify 可能读到旧版本或 gen 未实际修复",
                    iter_n=iter_n, stage="gen_fix", runs_dir=runs_dir)
                print("      ⚠️ gen 修复后文件未变化——verify 可能读到旧版本；"
                      "已给 verify 完整回环机会（max_verify_retry 提升）")
            elif _has_fix_progress(before, after, manifest):
                print(f"      ✅ gen 已修复 {len(changed)} 个文件，进入下一轮 verify 复核")
            else:
                print(f"      ℹ️ gen 修复改动 {len(changed)} 个文件（可能与问题清单无关），进入下一轮 verify")
        if not verify_ok:
            obs.emit_diagnostic("VERIFY_FAIL_EXCEEDED", run_id,
                                message=f"iter {iter_n} verify 修复回环后仍未通过",
                                iter_n=iter_n, stage="verify", runs_dir=runs_dir)
            st.set_exit(runs_dir, run_id, "early_stop", "verify_fail_exceeded")
            break

        # [d] execution (deterministic)
        print("  [d] 执行 pytest + gcov 采集")
        obs.emit("stage.enter", run_id, iter_n=iter_n, stage="execute", runs_dir=runs_dir)
        execution = run_tests(cfg, iter_dir)
        obs.emit("execute.completed", run_id, iter_n=iter_n, runs_dir=runs_dir,
                 data=execution.to_dict())
        print(f"      verdict={execution.verdict} "
              f"tests={execution.tests} fail={execution.failures} err={execution.errors} "
              f"({execution.duration_s:.1f}s)")
        st.update_iteration(runs_dir, run_id, iter_n, {
            "execute_verdict": execution.verdict,
            "gen_output": "ok",
        })

        # [e] quality analysis (when not PASS)
        if execution.verdict != "PASS":
            print("  [e] 失败分析（quality-agent）")
            obs.emit("stage.enter", run_id, iter_n=iter_n, stage="quality", runs_dir=runs_dir)
            from .badcase import badcase_hint
            await _call("quality-agent",
                        _prompt_quality(run_id, iter_n, iter_dir, execution.to_dict(),
                                        known_badcases=badcase_hint(cfg)),
                        iter_dir, iter_n, "quality", retries=1)
            quality = _read_json(iter_dir / "quality_report.json")
            obs.emit("stage.exit", run_id, iter_n=iter_n, stage="quality",
                     runs_dir=runs_dir,
                     data={"verdict": (quality or {}).get("verdict")})
            if quality:
                quality_actions = quality.get("action_items", [])
                print(f"      verdict={quality.get('verdict')} "
                      f"action_items={len(quality_actions)}")
                # badcase accumulation (LLM proposes -> deterministic code adjudicates into the library)
                candidates = quality.get("badcase_candidates") or []
                if candidates:
                    from .badcase import merge_candidates
                    merged = merge_candidates(cfg.workspace, candidates)
                    if merged["merged"]:
                        print(f"      📥 badcase 沉淀 {len(merged['merged'])} 条"
                              f"（拒绝 {len(merged['rejected'])}）→ {merged['path']}")
                        obs.emit("badcase.merged", run_id, iter_n=iter_n,
                                 runs_dir=runs_dir, data={
                                     "merged": merged["merged"],
                                     "rejected": len(merged["rejected"])})

        # [f] coverage delta and state update
        if execution.coverage_path and execution.coverage_path.exists():
            current_full = CoverageReport.load(execution.coverage_path)
        else:
            current_full = gap_source
        # scope mode: threshold/display metrics all use the narrowed view (function-level
        # incremental coverage); full metrics are stored separately under full_* keys.
        if target_functions:
            current = scope_report(current_full, target_functions)
            prev_view = (scope_report(previous, target_functions) if previous is not None
                         else scope_report(baseline_report, target_functions))
        else:
            current, prev_view = current_full, (previous if previous is not None else baseline_report)
        delta = current.delta(prev_view)
        st.update_iteration(runs_dir, run_id, iter_n, {
            "coverage_after": {"func_pct": current.func_pct, "cond_pct": current.cond_pct,
                               "func_hit": current.func_hit, "func_total": current.func_total,
                               "branch_hit": current.branch_hit,
                               "branch_total": current.branch_total,
                               **({"full_func_pct": current_full.func_pct,
                                   "full_cond_pct": current_full.cond_pct}
                                  if target_functions else {})},
            "delta": {"func_pp": delta["func_pp"], "cond_pp": delta["cond_pp"],
                      "newly_hit": len(delta.get("newly_hit", []))},
        })
        obs.emit("coverage.delta", run_id, iter_n=iter_n, runs_dir=runs_dir,
                 data={"func_pp": delta["func_pp"], "cond_pp": delta["cond_pp"],
                       "newly_hit": len(delta.get("newly_hit", []))})
        obs.emit("coverage.snapshot", run_id, iter_n=iter_n, runs_dir=runs_dir,
                 data=current.to_dict()["summary"])
        label = "增量scope覆盖率" if target_functions else "覆盖率"
        print(f"      {label}：func={current.func_pct:.2f}% "
              f"(Δ{delta['func_pp']:+.2f}pp) cond={current.cond_pct:.2f}% "
              f"(Δ{delta['cond_pp']:+.2f}pp)")
        if target_functions:
            print(f"      （全量参考：func={current_full.func_pct:.2f}% "
                  f"cond={current_full.cond_pct:.2f}%）")
        previous = current_full   # 迭代间比较始终基于全量快照，scope 视图按需现算

        if st.check_threshold(state := st.load_loop_state(runs_dir, run_id), iter_n):
            obs.emit("loop.threshold_met", run_id, runs_dir=runs_dir,
                     data={"iter": iter_n})
            st.set_exit(runs_dir, run_id, "done", "threshold_met",
                        {"func_pct": current.func_pct, "cond_pct": current.cond_pct,
                         **({"scope": True} if target_functions else {})})
            break
        # vacuous cond: when the scope has no testable branch at all, the cond threshold is
        # treated as met (for sequential branchless functions like stats_alloc, cond_pct=0 is
        # a "denominator 0" display artifact, not a failure -- if func already meets, it's overall met)
        if (target_functions and current.branch_total == 0
                and current.func_pct >= func_target):
            print("      ✅ scope 内无可测分支，cond 阈值视为满足（vacuous）")
            st.update_iteration(runs_dir, run_id, iter_n, {
                "coverage_after": {"func_pct": current.func_pct, "cond_pct": 100.0,
                                   "func_hit": current.func_hit,
                                   "func_total": current.func_total,
                                   "branch_hit": 0, "branch_total": 0,
                                   "cond_vacuous": True},
            })
            obs.emit("loop.threshold_met", run_id, runs_dir=runs_dir,
                     data={"iter": iter_n, "cond_vacuous": True})
            st.set_exit(runs_dir, run_id, "done", "threshold_met",
                        {"func_pct": current.func_pct, "cond_pct": 100.0,
                         "cond_vacuous": True, "scope": True})
            break
        early = st.check_early_stop(st.load_loop_state(runs_dir, run_id))
        if early:
            obs.emit("loop.early_stop", run_id, runs_dir=runs_dir, data={"reason": early})
            obs.emit_diagnostic("EARLY_STOP" if early == "coverage_ceiling" else early.upper(),
                                run_id, message=early, iter_n=iter_n, runs_dir=runs_dir)
            st.set_exit(runs_dir, run_id, "early_stop", early,
                        {"func_pct": current.func_pct, "cond_pct": current.cond_pct})
            break
    else:
        st.set_exit(runs_dir, run_id, "early_stop", "max_iter_reached",
                    {"func_pct": previous.func_pct if previous else 0,
                     "cond_pct": previous.cond_pct if previous else 0})

    # scope mode: at the end, verify every target appears in the coverage data; any that don't
    # (spelling mismatch / not instrumented / deleted) must be explicitly reported, never silently ignored.
    if target_functions and previous is not None:
        miss = missing_targets(previous, target_functions)
        if miss:
            print(f"  ⚠️ {len(miss)} 个目标函数不在覆盖率数据中（未插桩/已删除/名称不一致）")
            st.update_state(runs_dir, run_id, {"scope_missing_targets": [list(m) for m in miss]})

    return _finalize(cfg, runs_dir, run_id)


def _finalize(cfg: ProjectConfig, runs_dir: Path, run_id: str) -> dict:
    """Generate the final report (incl. the HTML coverage report) and return the final state."""
    try:
        final_state = st.load_loop_state(runs_dir, run_id)
    except FileNotFoundError:
        final_state = {"run_id": run_id, "status": "error", "exit_reason": "state_missing"}

    # HTML coverage report (always produced at loop end; report generation is not a single
    # point of failure for the loop); a failure must not block the loop's wrap-up.
    html_index = _generate_html_report(cfg, runs_dir, run_id)
    if html_index:
        final_state.setdefault("final_metrics", {})["html_report"] = str(html_index)
        st.set_exit(runs_dir, run_id, final_state.get("status", "unknown"),
                    final_state.get("exit_reason", ""),
                    {"html_report": str(html_index)})

    report_path = runs_dir / run_id / "loop_final_report.md"
    _write_final_report(cfg, runs_dir, run_id, final_state, report_path,
                        html_index=html_index)
    obs.emit("loop.exit", run_id, runs_dir=runs_dir,
             data={"status": final_state.get("status"),
                   "exit_reason": final_state.get("exit_reason"),
                   "report": str(report_path),
                   "html_report": str(html_index) if html_index else None})
    print(f"\n▶ 闭环结束：{final_state.get('status')}（{final_state.get('exit_reason')}）")
    print(f"  最终报告：{report_path}")
    if html_index:
        print(f"  HTML 覆盖率报告：{html_index}")
    final_state["report_path"] = str(report_path)
    if html_index:
        final_state["html_report"] = str(html_index)
    return final_state


def _generate_html_report(cfg: ProjectConfig, runs_dir: Path, run_id: str) -> Path | None:
    """用该 run 最新一轮 coverage.json 生成 HTML 报告，返回 index.html 路径。"""
    run_dir = runs_dir / run_id
    covs = sorted(run_dir.glob("iter_*/coverage.json"),
                  key=lambda p: int(p.parent.name.split("_")[1]))
    cov_path = covs[-1] if covs else (run_dir / "baseline_coverage.json")
    if not cov_path.exists():
        return None
    try:
        from .htmlreport import generate
        report = CoverageReport.load(cov_path)
        out_dir = cfg.reports_dir / f"coverage_{run_id}"
        import os
        links = {}
        md = run_dir / "loop_final_report.md"
        links["闭环报告 (Markdown)"] = os.path.relpath(md, out_dir)
        links["状态机 (loop_state.json)"] = os.path.relpath(run_dir / "loop_state.json", out_dir)
        return generate(report, out_dir, source_root=cfg.source_path,
                        project_name=cfg.display_name, run_id=run_id,
                        extra_links=links)
    except Exception as e:  # noqa: BLE001 — 报告生成失败不阻断闭环
        print(f"  ⚠️ HTML 报告生成失败（忽略）: {e}")
        return None


def _write_final_report(cfg: ProjectConfig, runs_dir: Path, run_id: str,
                        state: dict, path: Path,
                        html_index: Path | None = None) -> None:
    """生成最终 Markdown 报告（实现见 finalreport.py：增量/执行/用例/未覆盖原因/产物索引）。"""
    from .finalreport import write_final_report
    write_final_report(cfg, runs_dir, run_id, state, path, html_index=html_index)
