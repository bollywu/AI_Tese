"""扫描轨：scan-agent 产出的问题清单 → 复现用例闭环 → 四态裁决。

四态裁决模型经真实项目验证。通用化设计：
- 无远程 DUT 版本对齐问题（AIcoverage 全部本机构建本机执行）
- 输入是本地 scan-agent 的 scan_issues.json（**不是**外部平台扫描产物），
  因此完全脱敏——整条链路不访问任何代码托管/评审平台，适用于 GitHub 或
  任意来源的本地 clone。

四态裁决（关键语义，与二态不同）：
- confirmed：复现用例 FAIL 且失败类型是业务缺陷（程序表现异常）→ 问题坐实
- false_positive：复现用例 PASS（程序行为正常）→ 问题疑似误报
- inconclusive：前置/环境失败（用例没测到点子上，如前置步骤失败、无法构造
  触发条件）→ 保留人工审查，不能下结论
- unobservable：gen-agent 静态论证为运行期不可观测（UB no-op/架构假设等）→
  不生成用例，引用其论证理由

正向断言约定：复现用例断言"程序行为正确"（PASS=误报 / FAIL=坐实），
详见 prompts/scan_gen_agent.md。
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


def _prompt_scan_verify(run_id: str, manifest: dict) -> str:
    files = manifest.get("test_files", [])
    return f"""静态审查扫描轨生成的复现用例（run_id={run_id}）。

manifest 声明的文件：{json.dumps(files, ensure_ascii=False)}
harness：见环境变量 AICOV_TEST_DIR 下 lib/harness.py

重点审查：
- 断言方向必须是"程序行为正确"（正向断言）——发现反向断言（刻意让程序失败/
  断言崩溃发生）→ EC-08 error
- 触发条件是否对齐 issue 的 trigger_condition（引用 issue_id）
- 常规 V1-V5 原子化/独立性/断言质量审查同样适用

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

    # [S1] 扫描：ocr 优先（backend=off 时强制走 agent）
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
                # 显式指定 ocr 失败 → 报错退出而非静默降级（显式配置应当被尊重）
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

    # [S2] gen-agent（scan_gen 变体）生成复现用例
    print("  [S2] 复现用例生成（gen-agent，scan 变体：正向断言）")
    manifest_path = scan_dir / "manifest.json"
    obs.emit("stage.enter", run_id, stage="scan_gen", runs_dir=runs_dir)
    scan_gen_prompt = load_prompt("gen-agent", cfg.prompts_dir)  # 默认占位，下面整份替换
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

    # [S3] verify-agent 静态审查（复用，含 EC-07 文档头门禁——由调用方 loop 统一
    # 跑还是这里跑？这里独立跑一遍扫描轨文件子集）
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
        doc_problems = check_test_docstrings(cfg.test_dir, manifest.get("test_files", []))
        if doc_problems:
            verify_report["problems"] = list(verify_report.get("problems", [])) + doc_problems
            verify_report["verdict"] = "fail"
            (scan_dir / "verify_report.json").write_text(
                json.dumps(verify_report, ensure_ascii=False, indent=1), encoding="utf-8")
        errors = [p for p in verify_report.get("problems", [])
                  if p.get("severity") == "error"]
        print(f"      verdict={verify_report.get('verdict')}（error {len(errors)}）")
        obs.emit("stage.exit", run_id, stage="scan_verify", runs_dir=runs_dir,
                 data={"verdict": verify_report.get("verdict")})

    # [S4] 确定性执行（只跑本轮复现用例文件，避免混入全量用例结果）
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

    # [S5] 四态裁决
    verdicts = compute_verdicts(issues, manifest, dispositions, execution,
                                verify_report)
    result["verdicts"] = verdicts
    (scan_dir / "bug_verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for v in verdicts.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    print(f"  [S5] 裁决完成：{counts}")
    return result


def compute_verdicts(
    issues: list[dict],
    manifest: dict,
    dispositions: dict[str, dict],
    execution,
    verify_report: dict,
) -> dict[str, dict]:
    """四态裁决（确定性规则，无 LLM）。

    判定顺序：
    1. disposition=unobservable → unobservable（引用 gen 静态论证理由）
    2. 无复现用例（disposition 缺失或非 e2e）→ inconclusive
    3. verify fail（用例本身没通过静态审查）→ inconclusive（用例质量问题，
       不代表问题真伪，必须保留人工审查）
    4. 执行结果：
       - FAIL → confirmed（正向断言约定下，程序表现异常 = 缺陷坐实）
       - PASS → false_positive（程序行为正常 = 缺陷疑似误报）
       - 未执行 → inconclusive
    """
    verdicts: dict[str, dict] = {}
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
        elif not disp or disp.get("disposition") != "e2e":
            entry.update({"verdict": VERDICT_INCONCLUSIVE,
                          "evidence": "未生成复现用例（gen-agent 未声明 e2e 处置或声明缺失）"})
        elif verify_report.get("verdict") != "pass":
            entry.update({"verdict": VERDICT_INCONCLUSIVE,
                          "evidence": "复现用例未通过静态审查（用例质量问题，真伪待人工复核）"})
        elif execution is None:
            entry.update({"verdict": VERDICT_INCONCLUSIVE,
                          "evidence": "复现用例未被执行"})
        elif execution.verdict == "FAIL":
            entry.update({"verdict": VERDICT_CONFIRMED,
                          "evidence": f"复现用例 FAIL（程序表现异常）："
                                      f"failures={execution.failures} errors={execution.errors}"})
        elif execution.verdict == "PASS":
            entry.update({"verdict": VERDICT_FALSE_POSITIVE,
                          "evidence": "复现用例 PASS（程序行为正常）"})
        else:
            entry.update({"verdict": VERDICT_INCONCLUSIVE,
                          "evidence": f"执行异常 verdict={execution.verdict}"})
        verdicts[issue_id] = entry
    return verdicts


def render_scan_markdown(result: dict) -> str:
    """渲染扫描轨 Markdown 片段（嵌入 MR 最终报告用）。"""
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
