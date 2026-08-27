"""Scan track: scan-agent's issue list -> repro-case loop -> four-state adjudication.

The four-state adjudication model is validated on a real project. Generalization design:
- no remote-DUT version-alignment concern (AIcoverage builds and runs fully locally)
- the input is the local scan-agent's scan_issues.json (**not** an external-platform scan
  artifact), so it's fully sanitized -- the whole chain touches no code hosting/review
  platform, working for GitHub or any locally cloned source.

Four-state adjudication (key semantics, distinct from two-state):
- confirmed: repro case FAILs and the failure kind is a business defect (program misbehaves)
  -> the issue is confirmed
- false_positive: repro case PASSes (program behaves correctly) -> the issue is likely a false positive
- inconclusive: prerequisite/environment failure (the case didn't actually exercise the point,
  e.g. a prerequisite step failed, or the trigger condition can't be constructed) -> keep for
  manual review; no conclusion can be drawn
- unobservable: gen-agent statically argues it's not observable at runtime (UB no-op /
  architecture assumption etc.) -> no case generated; cite its reasoning

Positive-assertion convention: repro cases assert "the program behaves correctly"
(PASS=false positive / FAIL=confirmed); see prompts/scan_gen_agent.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import observability as obs
from . import state as st
from .agent_call import call_agent
from .agents import load_prompt
from .config import ProjectConfig
from .executor import run_tests
from .runner import AgentRunner

VERDICT_CONFIRMED = "confirmed"
VERDICT_FALSE_POSITIVE = "false_positive"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_UNOBSERVABLE = "unobservable"


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _prompt_scan(cfg: ProjectConfig, run_id: str, scan_issues_path: Path,
                 changed_functions: list[dict], diff_text: str) -> str:
    from .kb import wiki_navigation_hint
    cf_json = json.dumps(changed_functions[:60], ensure_ascii=False, indent=1)
    diff_preview = diff_text[:30000] if diff_text else "（见 changed_functions，diff 原文过大省略）"
    return f"""对本次 MR 的增量代码做聚焦式缺陷扫描（run_id={run_id}，完全本地、零外部平台依赖）。

源码根：$AICOV_SRC = {cfg.source_path}
{wiki_navigation_hint(cfg)}
## 变更函数清单（CodeGraph 行区间归因产物）
{cf_json}

## diff 原文（截断预览）
```
{diff_preview}
```

## 任务
按你的 SOP：对每个变更函数 Read 其函数体与调用链上下文（直接调用方/被调用方），
只报有具体触发条件的问题，产出 → {scan_issues_path}"""


def _prompt_scan_gen(cfg: ProjectConfig, run_id: str, scan_issues: dict,
                     manifest_path: Path) -> str:
    from .badcase import badcase_hint
    issues_json = json.dumps(scan_issues.get("issues", []), ensure_ascii=False, indent=1)
    return f"""为扫描轨的疑似缺陷生成复现/证伪用例（run_id={run_id}）。

被测项目：{cfg.display_name}（{cfg.language}），源码根 $AICOV_SRC = {cfg.source_path}
被测二进制：$AICOV_BINARY = {cfg.binary_path}
测试目录：$AICOV_TEST_DIR = {cfg.test_dir}
harness 原子函数库：{cfg.tests_lib_dir / "harness.py"}（先 Read 它！）
{badcase_hint(cfg)}
## 疑似缺陷清单（scan-agent 产出）
{issues_json}

## 任务
按你的 SOP：逐条做 e2e/unobservable 处置决策，为 e2e 类生成正向断言复现用例
（断言"程序行为正确"），写入 {cfg.test_dir}/，并写 manifest → {manifest_path}

遵守原子函数搭积木铁律。绝不执行 pytest。"""


def _prompt_scan_gen_retry(cfg: ProjectConfig, run_id: str, scan_issues: dict,
                           manifest_path: Path) -> str:
    """Focused second-pass prompt for issues that got no repro case (plan 5.3)."""
    from .badcase import badcase_hint
    issues_json = json.dumps(scan_issues.get("issues", []), ensure_ascii=False, indent=1)
    return f"""为上轮**未能生成复现用例**的疑似缺陷做第二次聚焦尝试（run_id={run_id}）。

被测项目：{cfg.display_name}（{cfg.language}），源码根 $AICOV_SRC = {cfg.source_path}
被测二进制：$AICOV_BINARY = {cfg.binary_path}
测试目录：$AICOV_TEST_DIR = {cfg.test_dir}
harness 原子函数库：{cfg.tests_lib_dir / "harness.py"}（先 Read 它！）
{badcase_hint(cfg)}
## 上轮未复现的疑似缺陷（含 trigger_condition）
{issues_json}

## 任务
上一轮你没能为这些 issue 生成复现用例。本轮**逐条重新尝试**：
1. Read trigger_condition 涉及的源码函数与其调用方，寻找通过被测二进制正常入口
   （CLI 参数/请求输入）构造触发条件的路径——上轮放弃的很多是构造方式没找对
2. 确实黑盒无法构造的，可走单测通道（compile_unit_driver 直调，处置标 unit_confirm
   并写明 reason）；确属运行期不可观测的标 unobservable 并给出静态论证
3. 生成的用例写入 {cfg.test_dir}/，写 manifest → {manifest_path}

与首轮相同的纪律：正向断言、原子函数搭积木、绝不执行 pytest。"""


def _prompt_scan_verify(run_id: str, manifest: dict) -> str:
    files = manifest.get("test_files", [])
    return f"""静态审查扫描轨生成的复现用例（run_id={run_id}）。

manifest 声明的文件：{json.dumps(files, ensure_ascii=False)}
harness：见环境变量 AICOV_TEST_DIR 下 lib/harness.py

重点审查：
- 断言方向必须是"程序行为正确"（正向断言）——发现反向断言（刻意让程序失败/
  断言崩溃发生）→ EC-11 error
- 触发条件是否对齐 issue 的 trigger_condition（引用 issue_id）
- 常规 V1-V5 原子化/独立性/断言质量审查同样适用

（EC-07 文档头 / EC-08 恒真断言 / EC-10 issue_id 绑定由确定性门禁自动检查并
合并进你的报告，你无需重复检查这三类格式问题，专注语义审查。）

产出 verify_report.json（格式与覆盖率轨一致）。"""


def parse_issues(scan_dir: Path) -> list[dict]:
    """读取 scan_issues.json，返回 issues 列表（空产物返回 []，不报错——
    零产出是 scan-agent 的合法结果）。"""
    data = _read_json(scan_dir / "scan_issues.json") or {}
    return data.get("issues", []) or []


async def run_scan_track(
    cfg: ProjectConfig,
    run_id: str,
    run_dir: Path,
    changed_functions: list[dict],
    diff_text: str,
    *, quiet: bool = False, max_verify_retry: int = 2,
    base_ref: str = "", head_ref: str = "",
) -> dict:
    """扫描轨主流程：scan（ocr 优先 / scan-agent 兜底）→ gen（复现用例）→ verify → execute → 裁决。

    Args:
        base_ref/head_ref: 本地 git ref。提供且 ocr 可用已配置时，S1 优先调
            open-code-review（`ocr review --from --to --format json`）；否则
            降级 scan-agent（自研聚焦扫描）。两通道产出统一 issue 格式，
            下游链路无感知。
    """
    scan_dir = run_dir / "scan"
    scan_dir.mkdir(parents=True, exist_ok=True)
    scan_issues_path = scan_dir / "scan_issues.json"

    runner = AgentRunner(cfg, quiet=quiet, run_dir=run_dir)
    runs_dir = cfg.runs_dir
    import os
    os.environ.update(cfg.to_env(run_dir=run_dir))

    # [S1] scan: prefer ocr (backend=off forces agent)
    backend = cfg.scan_backend
    used_backend = "agent"
    if base_ref and head_ref and backend in ("ocr", "auto"):
        try:
            from .ocrscan import run_ocr_review
            print(f"  [S1] 增量代码扫描（open-code-review：{base_ref}..{head_ref}）")
            obs.emit("stage.enter", run_id, stage="scan", runs_dir=runs_dir,
                     data={"backend": "ocr"})
            issues, raw = run_ocr_review(cfg.source_path, base_ref, head_ref,
                                         output_path=scan_dir / "ocr_review.json")
            used_backend = "ocr"
            scan_data = {
                "issues": issues,
                "summary": f"open-code-review 发现 {len(issues)} 个问题",
                "backend": "ocr",
            }
            scan_issues_path.write_text(
                json.dumps(scan_data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"      发现 {len(issues)} 个疑似问题（open-code-review）")
        except (ImportError, RuntimeError) as e:
            if backend == "ocr":
                # explicit ocr failure -> error out rather than silent degrade (explicit config should be respected)
                print(f"  ❌ [scan] backend=ocr 不可用: {e}")
                raise
            print(f"  ⚠️ open-code-review 不可用（{e}），降级 scan-agent")
    if used_backend == "agent":
        print("  [S1] 增量代码扫描（scan-agent，完全本地）")
        obs.emit("stage.enter", run_id, stage="scan", runs_dir=runs_dir,
                 data={"backend": "agent"})
        await call_agent(
            runner, run_id, "scan-agent",
            _prompt_scan(cfg, run_id, scan_issues_path, changed_functions, diff_text),
            runs_dir=runs_dir, stage="scan", max_retries=2,
        )
        scan_data = _read_json(scan_issues_path) or {"issues": [], "summary": "scan-agent 未产出"}
    issues = scan_data.get("issues", []) or []
    if used_backend == "agent":
        print(f"      发现 {len(issues)} 个疑似问题（{scan_data.get('summary', '')[:80]}）")
    obs.emit("stage.exit", run_id, stage="scan", runs_dir=runs_dir,
             data={"issues": len(issues), "backend": used_backend})
    result: dict[str, Any] = {"issues": issues, "scan_summary": scan_data.get("summary", ""),
                              "clean_files": scan_data.get("clean_files", []),
                              "scan_backend": used_backend}
    if not issues:
        result["verdicts"] = {}
        return result

    # [S2] gen-agent (scan_gen variant) generates repro cases
    print("  [S2] 复现用例生成（gen-agent，scan 变体：正向断言）")
    manifest_path = scan_dir / "manifest.json"
    obs.emit("stage.enter", run_id, stage="scan_gen", runs_dir=runs_dir)
    scan_gen_prompt = load_prompt("gen-agent", cfg.prompts_dir)  # default placeholder; fully replaced below
    try:
        scan_gen_prompt = (Path(__file__).parent / "prompts" / "scan_gen_agent.md").read_text(
            encoding="utf-8")
    except OSError:
        pass
    os.environ.update(cfg.to_env(run_dir=run_dir, iter_dir=scan_dir))
    await call_agent(
        runner, run_id, "gen-agent",
        _prompt_scan_gen(cfg, run_id, scan_data, manifest_path),
        runs_dir=runs_dir, stage="scan_gen", max_retries=2,
        prompt_override=scan_gen_prompt,
    )
    manifest = _read_json(manifest_path) or {}
    dispositions = {d.get("issue_id"): d for d in manifest.get("dispositions", []) or []}
    print(f"      生成 {len(manifest.get('test_files', []))} 个文件 / "
          f"{len(manifest.get('new_functions', []))} 个用例")
    obs.emit("stage.exit", run_id, stage="scan_gen", runs_dir=runs_dir,
             data={"files": len(manifest.get("test_files", []))})
    result["manifest"] = manifest

    # [S3] verify-agent static review (reused; includes EC-07 doc-header gate AND the
    # EC-08 assertion-quality / EC-10 issue-binding gates -- whether the caller's loop
    # runs them or not, we run them independently over the scan track's file subset)
    from .assertquality import check_assert_quality
    from .docstyle import check_test_docstrings
    verify_report = {"verdict": "fail", "problems": []}
    if manifest.get("test_files"):
        print("  [S3] 复现用例静态审查（verify-agent）")
        obs.emit("stage.enter", run_id, stage="scan_verify", runs_dir=runs_dir)
        await call_agent(
            runner, run_id, "verify-agent",
            _prompt_scan_verify(run_id, manifest),
            runs_dir=runs_dir, stage="scan_verify", max_retries=1,
        )
        vr = _read_json(scan_dir / "verify_report.json")
        if vr:
            verify_report = vr
        # Deterministic gates apply to the pytest repro files only (a *_test.go file
        # would fail ast.parse and be falsely reported as a syntax problem).
        py_files = [f for f in manifest.get("test_files", []) if str(f).endswith(".py")]
        doc_problems = check_test_docstrings(cfg.test_dir, py_files)
        aq_problems = check_assert_quality(cfg.test_dir, py_files)
        gate_problems = doc_problems + aq_problems
        if gate_problems:
            verify_report["problems"] = list(verify_report.get("problems", [])) + gate_problems
            verify_report["verdict"] = "fail"
            (scan_dir / "verify_report.json").write_text(
                json.dumps(verify_report, ensure_ascii=False, indent=1), encoding="utf-8")
        errors = [p for p in verify_report.get("problems", [])
                  if p.get("severity") == "error"]
        print(f"      verdict={verify_report.get('verdict')}（error {len(errors)}）")
        obs.emit("stage.exit", run_id, stage="scan_verify", runs_dir=runs_dir,
                 data={"verdict": verify_report.get("verdict")})

    # [S4] deterministic execution (run only this round's repro-case files, avoiding mixing in
    # full-case results)
    execution = None
    if manifest.get("test_files") and verify_report.get("verdict") == "pass":
        print("  [S4] 执行复现用例（确定性 executor，仅本轮文件）")
        obs.emit("stage.enter", run_id, stage="scan_execute", runs_dir=runs_dir)
        bug_files = [cfg.test_dir / f for f in manifest.get("test_files", [])
                     if (cfg.test_dir / f).exists()]
        execution = run_tests(cfg, scan_dir, test_files=bug_files or None,
                              collect_coverage=False)
        print(f"      verdict={execution.verdict} tests={execution.tests} "
              f"fail={execution.failures} err={execution.errors}")
        obs.emit("stage.exit", run_id, stage="scan_execute", runs_dir=runs_dir,
                 data=execution.to_dict())
        result["execution"] = execution.to_dict()

    # [S5] four-state adjudication
    verdicts = compute_verdicts(issues, manifest, dispositions, execution,
                                verify_report)
    result["verdicts"] = verdicts
    (scan_dir / "bug_verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for v in verdicts.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    print(f"  [S5] 裁决完成：{counts}")

    # [S5.5] E2E-first disclosure for scan track: dispositions marked unit_confirm
    # (gen judged the repro needs a unit test rather than a black-box e2e trigger)
    # enter the same human-confirmation ledger as the coverage track's
    # unit_confirm_required -- pending until a human accepts them.
    unit_pending = [
        {"issue_id": d.get("issue_id"), "test_function": d.get("test_function"),
         "reason": d.get("reason", "gen 判定需单测通道复现（e2e 无法构造触发条件）")}
        for d in manifest.get("dispositions", []) or []
        if d.get("disposition") == "unit_confirm"
    ]
    if unit_pending:
        (scan_dir / "unit_confirm.json").write_text(
            json.dumps({"pending": unit_pending, "confirmed": [], "declared": unit_pending},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        obs.emit_diagnostic("UNIT_CONFIRM_PENDING", run_id,
                            message=f"扫描轨有 {len(unit_pending)} 个单测复现待人工确认",
                            stage="scan", runs_dir=runs_dir)
        print(f"      🚦 单测复现待人工确认：{len(unit_pending)} 个")

    # [S5.6] one focused retry (plan 5.3): issues left inconclusive because gen
    # produced NO repro case get one more attempt with the trigger_condition spelled
    # out. Some first-pass "couldn't construct" verdicts are just gen giving up too
    # early; a focused second pass converts a slice of 待人工 into real evidence.
    # Retry artifacts are adjudicated independently and merged into the verdicts.
    retry_ids = {iid for iid, v in verdicts.items()
                 if v.get("verdict") == VERDICT_INCONCLUSIVE
                 and "未生成复现用例" in (v.get("evidence") or "")}
    if retry_ids and max_verify_retry > 0:
        retry_issues = [i for i in issues if i.get("issue_id") in retry_ids]
        print(f"  [S5.6] inconclusive 二次尝试（{len(retry_issues)} 个无复现用例的 issue）")
        retry_dir = scan_dir / "retry"
        retry_dir.mkdir(parents=True, exist_ok=True)
        retry_manifest_path = retry_dir / "manifest.json"
        obs.emit("stage.enter", run_id, stage="scan_gen_retry", runs_dir=runs_dir)
        os.environ.update(cfg.to_env(run_dir=run_dir, iter_dir=retry_dir))
        await call_agent(
            runner, run_id, "gen-agent",
            _prompt_scan_gen_retry(cfg, run_id, {"issues": retry_issues}, retry_manifest_path),
            runs_dir=runs_dir, stage="scan_gen_retry", max_retries=1,
            prompt_override=scan_gen_prompt,
        )
        rm = _read_json(retry_manifest_path) or {}
        if rm.get("test_files"):
            # focused verify + execute on the retry files only, then adjudicate
            # the retried issues against the retry execution's per-case results
            retry_disp = {d.get("issue_id"): d for d in rm.get("dispositions", []) or []}
            os.environ.update(cfg.to_env(run_dir=run_dir, iter_dir=retry_dir))
            await call_agent(
                runner, run_id, "verify-agent",
                _prompt_scan_verify(run_id, rm),
                runs_dir=runs_dir, stage="scan_verify_retry", max_retries=1,
            )
            verify2 = _read_json(retry_dir / "verify_report.json") or {"verdict": "fail", "problems": []}
            from .assertquality import check_assert_quality
            from .docstyle import check_test_docstrings
            py_files = [f for f in rm.get("test_files", []) if str(f).endswith(".py")]
            gate2 = (check_test_docstrings(cfg.test_dir, py_files)
                     + check_assert_quality(cfg.test_dir, py_files))
            if gate2:
                verify2["problems"] = list(verify2.get("problems", [])) + gate2
                verify2["verdict"] = "fail"
            execution2 = None
            if verify2.get("verdict") == "pass":
                retry_files = [cfg.test_dir / f for f in rm.get("test_files", [])
                               if (cfg.test_dir / f).exists()]
                execution2 = run_tests(cfg, retry_dir, test_files=retry_files or None,
                                       collect_coverage=False)
            retry_verdicts = compute_verdicts(retry_issues, rm, retry_disp,
                                              execution2, verify2)
            for iid, v in retry_verdicts.items():
                verdicts[iid] = v
            result["verdicts"] = verdicts
            result["retry"] = {"issues": sorted(retry_ids), "files": rm.get("test_files", [])}
            (scan_dir / "bug_verification.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            counts2: dict[str, int] = {}
            for v in verdicts.values():
                counts2[v["verdict"]] = counts2.get(v["verdict"], 0) + 1
            print(f"      二次尝试后裁决分布：{counts2}")
        obs.emit("stage.exit", run_id, stage="scan_gen_retry", runs_dir=runs_dir,
                 data={"issues": len(retry_ids), "files": len(rm.get("test_files", []) or [])})
    return result


def compute_verdicts(
    issues: list[dict],
    manifest: dict,
    dispositions: dict[str, dict],
    execution,
    verify_report: dict,
) -> dict[str, dict]:
    """Four-state adjudication (deterministic rules, no LLM).

    Order of decisions:
    1. disposition=unobservable -> unobservable (cite gen's static-argumentation reason)
    2. no repro case (disposition missing or not e2e) -> inconclusive
    3. verify fail (the case itself failed static review) -> inconclusive (a case-quality issue;
       it does not tell the issue's truth; manual review must be kept)
    4. execution result -- **per issue, via its own test function** (2026-08-27 fix):
       previously every issue reused the whole-run execution.verdict, so one failing
       case confirmed ALL issues (misattribution). Now the disposition's
       test_function is looked up in execution.cases (per-case junit results):
       - fail -> confirmed (positive-assertion convention: program misbehavior)
       - pass -> false_positive (program behaves correctly)
       - error/skipped/not-found -> inconclusive (never guess, never borrow another
         issue's outcome)
    """
    verdicts: dict[str, dict] = {}
    cases = (getattr(execution, "cases", None) or {}) if execution is not None else {}
    for issue in issues:
        issue_id = issue.get("issue_id", "")
        disp = dispositions.get(issue_id, {})
        entry: dict[str, Any] = {
            "issue_id": issue_id, "title": issue.get("title", ""),
            "severity": issue.get("severity", ""), "function": issue.get("function", ""),
        }
        if disp.get("disposition") == "unobservable":
            entry.update({"verdict": VERDICT_UNOBSERVABLE,
                          "evidence": disp.get("reason", "gen-agent 静态论证为不可观测")})
        elif not disp or disp.get("disposition") not in ("e2e", "unit_confirm"):
            entry.update({"verdict": VERDICT_INCONCLUSIVE,
                          "evidence": "未生成复现用例（gen-agent 未声明 e2e 处置或声明缺失）"})
        elif verify_report.get("verdict") != "pass":
            entry.update({"verdict": VERDICT_INCONCLUSIVE,
                          "evidence": "复现用例未通过静态审查（用例质量问题，真伪待人工复核）"})
        elif execution is None:
            entry.update({"verdict": VERDICT_INCONCLUSIVE,
                          "evidence": "复现用例未被执行"})
        else:
            tf = _bare_name(str(disp.get("test_function") or "").strip())
            status = cases.get(tf) if tf else None
            if not cases:
                entry.update({"verdict": VERDICT_INCONCLUSIVE,
                              "evidence": "执行结果无用例明细（junit/日志缺失），无法定位该 issue 的独立用例"})
            elif status is None:
                entry.update({"verdict": VERDICT_INCONCLUSIVE,
                              "evidence": f"执行结果中未找到 test_function={tf!r}"
                                          f"（manifest 声明与实际用例名不一致，待人工核对）"})
            elif status == "fail":
                entry.update({"verdict": VERDICT_CONFIRMED,
                              "evidence": f"复现用例 {tf} FAIL（程序表现异常）——缺陷坐实"})
            elif status == "pass":
                entry.update({"verdict": VERDICT_FALSE_POSITIVE,
                              "evidence": f"复现用例 {tf} PASS（程序行为正常）——疑似误报"})
            elif status == "error":
                entry.update({"verdict": VERDICT_INCONCLUSIVE,
                              "evidence": f"复现用例 {tf} error（setup/框架异常），未形成断言结论"})
            elif status == "skipped":
                entry.update({"verdict": VERDICT_INCONCLUSIVE,
                              "evidence": f"复现用例 {tf} 被跳过（环境前置不满足）"})
            else:
                entry.update({"verdict": VERDICT_INCONCLUSIVE,
                              "evidence": f"执行状态异常 status={status!r}"})
        verdicts[issue_id] = entry
    return verdicts


def _bare_name(name: str) -> str:
    """Bare test-function name (strip file/class prefixes and parametrize brackets)."""
    return name.split("::")[-1].split("[", 1)[0].strip()


def render_scan_markdown(result: dict) -> str:
    """Render the scan-track Markdown snippet (for embedding in the MR final report)."""
    issues = result.get("issues", [])
    verdicts: dict = result.get("verdicts", {})
    if not issues:
        return ("### 扫描轨结果\n\n"
                f"scan-agent 扫描结论：**未发现问题**。\n\n"
                f"> {result.get('scan_summary', '')}\n")
    lines = ["### 扫描轨结果", "",
             f"扫描 {len(issues)} 个疑似问题，裁决分布见下表。", "",
             "| issue | 严重度 | 函数 | 裁决 | 证据 |",
             "|-------|--------|------|------|------|"]
    order = {"confirmed": 0, "inconclusive": 1, "unobservable": 2, "false_positive": 3}
    icon = {"confirmed": "🔴 坐实", "inconclusive": "🟡 待人工",
            "unobservable": "⚪ 不可观测", "false_positive": "🟢 疑似误报"}
    for issue in sorted(issues, key=lambda i: order.get(
            (verdicts.get(i.get("issue_id", ""), {}) or {}).get("verdict", ""), 9)):
        issue_id = issue.get("issue_id", "")
        v = verdicts.get(issue_id, {}) or {}
        verdict = v.get("verdict", VERDICT_INCONCLUSIVE)
        ev = (v.get("evidence", "") or "").replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(
            f"| {issue_id} | {issue.get('severity', '')} | "
            f"`{issue.get('function', '')}` | {icon.get(verdict, verdict)} | {ev} |")
    lines += ["", "**裁决语义**：复现用例为正向断言（断言程序行为正确）——"
              "FAIL=程序异常=缺陷坐实；PASS=程序正常=疑似误报；"
              "inconclusive=用例未测到点子上（质量问题/未执行），保留人工审查；"
              "unobservable=静态论证运行期不可观测。"]
    return "\n".join(lines)
