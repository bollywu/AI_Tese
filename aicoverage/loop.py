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
from .agent_call import call_agent, reset_backoff
from .assertquality import check_assert_quality
from .build import build as do_build
from .config import ProjectConfig
from .docstyle import check_test_docstrings
from .executor import run_go_tests, run_tests
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
    """单测通道引导：默认 e2e 优先；当缺口根因是 e2e 不可达（N1/N3/N5）时才允许
    走单测通道，且该单测覆盖必须声明到 manifest 的 unit_confirm_required 字段等待人工确认。

    Language-aware:
      - C/C++: e2e = run_binary on the instrumented binary; unit = compile_unit_driver.
      - Go: e2e = test that exercises the app's real HTTP/net path (starts a server, issues
        requests); unit = test that instantiates the object and calls methods directly
        (in-memory/mocked deps). All Go tests are *_test.go; the classifier in
        go_test_scope distinguishes source. Pure unit tests must be declared.
    """
    if not cfg.e2e_first:
        return ""  # e2e-first 纪律关闭，不注入单测约束（保持旧行为）
    if getattr(cfg, "language", "c") == "go":
        return _go_e2e_first_hint(cfg)
    cc = cfg.ut_compiler or "（跟随 build 体系，建议 gcc/g++）"
    return f"""
## 覆盖来源铁律：E2E 优先，单测需人工确认（最高优先级，违反必返工）

**默认必须通过被测二进制 $AICOV_BINARY 的黑盒 E2E（run_binary）覆盖目标函数。**
单测（compile_unit_driver/run_driver 直接调函数）**只允许**用于 gap 根因明确为
**N1（特定运行环境/多进程/信号）、N3（错误路径）、N5（死代码/平台相关/无调用点）**
且你读过源码后确认**无法通过任何 E2E 输入构造触达**的函数。

**单测覆盖必须人工确认**：每一个通过单测通道覆盖的函数，都必须写进 manifest 的
`unit_confirm_required` 字段（数组，每项 {{"file","function","evidence"}}），
`evidence` 说明为什么该函数 e2e 不可达（引用源码证据）。未列入该字段的单测视为无效。
能 E2E 触达的（N4/N6）**一律走 run_binary**，不许用单测。

单测通道写法（N1/N3/N5 专用）：
1. 写 `test_driver_<主题>.c`（含 main），`#include` 或 extern 声明目标函数，直接调用并打印返回值/副作用
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
"""


def _go_e2e_first_hint(cfg: ProjectConfig) -> str:
    """Go 专用 E2E-first 纪律：优先写集成测试（启动 HTTP server 走真实链路），
    纯单测（直接调函数、注入 mock/内存依赖）必须声明等待人工确认。"""
    return f"""
## 覆盖来源铁律（Go）：E2E/集成测试优先，单测需人工确认（最高优先级，违反必返工）

**默认必须写 E2E/集成测试覆盖目标函数**：通过应用的真实 HTTP 入口（如 `httptest.NewServer`/
`gin` 路由 + 真实 handler）发起请求走完整链路，验证返回。纯单测（直接实例化对象、
注入内存/mock 依赖、`t *testing.T` 直调方法）**只允许**用于 gap 根因明确为
**N1/N3/N5（需要特定环境/错误路径/死代码等）** 且确认无法通过 HTTP 集成触达的函数。

**单测覆盖必须人工确认**：每一个纯单测覆盖的目标函数，都必须写进 manifest 的
`unit_confirm_required` 字段（数组，每项 {{"file","function","evidence"}}），
`evidence` 说明为什么该函数集成/e2e 不可达（引用源码证据）。未列入该字段的纯单测视为无效。
能通过 HTTP 集成触达的函数（N4/N6）**一律走集成测试**，不许用纯单测。

判定依据：测试函数体内若出现 `httptest.`/`http.NewServer`/`gin.New`/`localhost` 等网络信号
判定为 E2E/集成；否则判定为纯单测。请在 manifest 里如实声明。
"""


def _gen_write_instruction(cfg: ProjectConfig) -> str:
    """Language-aware instruction for where/how gen-agent writes test cases."""
    if getattr(cfg, "language", "c") == "go":
        go_e2e_note = (
            f"【覆盖来源】优先写 E2E/集成测试（httptest/gin 起 server 走真实链路）；"
            f"若某函数必须用纯单测（直接调函数），务必在 manifest.unit_confirm_required 声明并写明证据。"
            if cfg.e2e_first else ""
        )
        return (
            f"生成/修复 Go 测试文件到源码包目录（文件名 *_test.go，与包同目录），"
            f"放在被测源码包旁边（如 src/foo/foo_test.go），用 go test 的标准测试函数"
            f"(func TestXxx(t *testing.T))。不要用 pytest/harness。{go_e2e_note}"
        )
    if cfg.e2e_first and cfg.language != "go":
        return (
            f"生成/修复 pytest 用例到 {cfg.test_dir}/（文件名 test_<主题>_<序号>.py）。"
            f"【覆盖来源】默认走 e2e（run_binary 黑盒触发）；若某函数必须用单测"
            f"(compile_unit_driver)，务必在 manifest.unit_confirm_required 声明并写明证据。"
        )
    return (
        f"生成/修复 pytest 用例到 {cfg.test_dir}/（文件名 test_<主题>_<序号>.py）"
    )


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
{'被测二进制：$AICOV_BINARY = ' + str(cfg.binary_path) if cfg.language != 'go' and cfg.binary_path else '（Go 项目：无需插桩二进制，由 go test -coverprofile 原生采集）'}
测试目录：$AICOV_TEST_DIR = {cfg.test_dir}
harness 原子函数库：{cfg.tests_lib_dir / "harness.py"}（先 Read 它！）
{wiki_navigation_hint(cfg)}{badcase_hint(cfg)}
{_unittest_hint(cfg)}
{plan_part}{fix_part}{ctx_part}## 本轮覆盖缺口（gap_items，按优先级排序）
{gap_json}

## 任务
1. Read harness.py 了解可用原子函数（缺什么先补什么）
2. Read 目标函数源码，断言预期值必须来自源码真实逻辑
3. {_gen_write_instruction(cfg)}
4. 写 manifest → {manifest_path}

遵守原子函数搭积木铁律。绝不执行 pytest / go test。"""


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
    base = cfg.source_path if cfg.language == "go" else cfg.test_dir
    for f in manifest.get("test_files", []):
        p = Path(f)
        if not p.is_absolute():
            p = base / p
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


def _confirm_unit_coverage(cfg: ProjectConfig, manifest: dict,
                           *, interactive: bool = False) -> dict:
    """E2E-first human confirmation gate for unit-test coverage.

    gen-agent declares every unit-test-covered function in manifest.unit_confirm_required
    (list of {file, function, evidence}). This gate turns that declaration into an explicit
    human confirmation:
      - interactive: prompt y/n per function (default n -> strict, unconfirmed stays pending)
      - non-interactive: auto-approve when unit_confirm_auto_yes, else all stay pending

    2026-08-27 hardening (three deterministic sub-gates, all zero-LLM):
      a) auto-detection: unit-channel coverage the gen FORGOT to declare is caught
         statically (Go: *_test.go source classifier; C/C++: AST scan for
         compile_unit_driver/run_driver calls) and enters pending -- silent bypass
         is no longer possible;
      b) evidence gate: a declaration whose evidence cites no concrete source
         location (file:line) is never auto-approved (auto_yes included) --
         "e2e 不可达" claims must be verifiable;
      c) reachability veto: when CodeGraph is enabled and indexed, a declared
         function proven reachable from the entrypoints is rejected outright
         (it should be E2E-covered, not unit-covered).

    Returns:
        {"confirmed": [...], "pending": [...], "declared": [...]} where each item is a
        dict {file, function, evidence, confirmed}.
    """
    declared = manifest.get("unit_confirm_required") or []
    is_go = getattr(cfg, "language", "c") == "go"
    auto_unit = _go_unit_tests(cfg, manifest) if (is_go and cfg.e2e_first) else []
    if not is_go and cfg.e2e_first:
        auto_unit = _undeclared_unit_channel(cfg, manifest)
        if auto_unit:
            print(f"      ⚠️ AST 检测到 {len(auto_unit)} 个未声明的单测通道用例"
                  f"（compile_unit_driver/run_driver）——自动加入待确认")
    if not declared and not auto_unit:
        return {"confirmed": [], "pending": [], "declared": []}
    if not cfg.require_unit_confirm:
        # governance off: treat all declared as confirmed without human review
        return {"confirmed": [dict(d, confirmed=True) for d in declared],
                "pending": [], "declared": declared}

    # Deterministic pre-vetoes applied regardless of confirmation mode:
    #   weak evidence (no source location cited) and CodeGraph-proven e2e-reachability
    #   can never be auto-approved; they land in pending with the veto reason appended.
    veto: dict[int, str] = {}   # index into declared -> reason
    if declared:
        for i, d in enumerate(declared):
            reason = _unit_decl_veto_reason(cfg, d)
            if reason:
                veto[i] = reason
        if veto:
            print(f"      🚫 {len(veto)} 个单测声明被确定性否决"
                  f"（证据无源码定位 / CodeGraph 证明 e2e 可达），强制进入待确认")

    auto_approvable = (not interactive and getattr(cfg, "unit_confirm_auto_yes", False))
    if auto_approvable and not veto and not auto_unit:
        return {"confirmed": [dict(d, confirmed=True) for d in declared],
                "pending": [], "declared": declared}

    confirmed: list[dict] = []
    pending: list[dict] = []
    for i, d in enumerate(declared):
        item = dict(d, confirmed=False)
        loc = f"{d.get('file', '?')}::{d.get('function', '?')}"
        ev = d.get("evidence", "")
        if i in veto:
            item["evidence"] = f"{ev} ｜[否决] {veto[i]}"
        elif auto_approvable:
            item["confirmed"] = True
        elif interactive:
            print(f"\n  ⚠️ [单测人工确认] gen 用单测覆盖了函数 {loc}")
            if ev:
                print(f"     证据：{ev}")
            if not _evidence_cites_source(ev):
                print("     ⚠️ 证据未引用具体源码位置（file:line），请人工重点核查")
            ans = input("     此函数仅被单测覆盖，确认接受该单测覆盖? [y/N] ").strip().lower()
            item["confirmed"] = ans in ("y", "yes")
        # non-interactive default: not confirmed -> pending
        (confirmed if item["confirmed"] else pending).append(item)

    # Auto-detection: even if gen forgot to declare, statically detected unit-channel
    # coverage needs human confirmation under the E2E-first discipline:
    #   - Go: pure-unit *_test.go functions (no HTTP/net signal)
    #   - C/C++: test functions calling compile_unit_driver/run_driver
    for ut in auto_unit:
        item = dict(ut, confirmed=False)
        loc = f"{ut.get('file', '?')}::{ut.get('function', '?')}"
        if interactive:
            print(f"\n  ⚠️ [单测人工确认] 未声明的单测通道覆盖函数 {loc}")
            if ut.get("evidence"):
                print(f"     证据：{ut['evidence']}")
            ans = input("     此函数仅被单测覆盖，确认接受该单测覆盖? [y/N] ").strip().lower()
            item["confirmed"] = ans in ("y", "yes")
        (confirmed if item["confirmed"] else pending).append(item)

    return {"confirmed": confirmed, "pending": pending, "declared": declared}


# Evidence must cite a concrete source location: "src/foo.c:123", "foo.go:45",
# "第123行" or "line 123" -- free-text claims are not verifiable and never
# auto-approved (2026-08-27 hardening, plan 6.3).
_SRC_LOC_RE = None


def _evidence_cites_source(ev: str) -> bool:
    import re
    global _SRC_LOC_RE
    if _SRC_LOC_RE is None:
        _SRC_LOC_RE = re.compile(
            r"[\w./\\-]+\.(?:c|cc|cpp|cxx|h|hpp|hxx|go|py|rs|java)[:：]\s?\d+"
            r"|\b第\s*\d+\s*行\b|\bline\s*\d+",
            re.IGNORECASE)
    return bool(_SRC_LOC_RE.search(ev or ""))


def _unit_decl_veto_reason(cfg: ProjectConfig, d: dict) -> str | None:
    """Deterministic veto reason for a unit-confirm declaration, or None if acceptable.

    1. evidence cites no source location (file:line) -> unverifiable claim;
    2. CodeGraph enabled+indexed and the function is provably reachable from the
       configured entrypoints -> it should be E2E-covered, unit is not allowed.
    """
    ev = d.get("evidence") or ""
    func = str(d.get("function") or "").strip()
    if not _evidence_cites_source(ev):
        return "证据未引用具体源码位置（file:line），不可自动核准"
    if func and getattr(cfg, "codegraph_enabled", False):
        try:
            from . import callgraph
            if callgraph.is_indexed(cfg.source_path, cfg.codegraph_index_dir):
                res = callgraph.trace_batch_to_entrypoints(
                    cfg.source_path, [func], cfg.codegraph_entrypoints,
                    index_dir=cfg.codegraph_index_dir)
                tr = res.get(func)
                if tr is not None and tr.found:
                    path = tr.paths[0].render() if tr.paths else ""
                    return (f"CodeGraph 证明存在从入口到该函数的调用链"
                            f"（{path}），应走 E2E 而非单测")
        except Exception:  # noqa: BLE001 — 可达性核验失败不阻断门禁
            pass
    return None


_UNIT_CHANNEL_FUNCS = ("compile_unit_driver", "run_driver")


def _verify_manifest_claims(manifest: dict, report: CoverageReport) -> list[str]:
    """Zero-LLM anti-hallucination gate: declared-covered functions must actually be hit.

    gen declares coverage in manifest.e2e_functions ([{file, function}]) and
    manifest.targets ([{file, functions: [...]}]). Any declared function that is
    absent from the coverage report or has execution_count == 0 is a claim/fact
    mismatch -- before this check, gen could claim anything and nothing ever
    compared the claim against the deterministic gcov result.
    Returns the mismatch list as "file::function" strings.
    """
    claims: list[tuple[str, str]] = []
    for it in manifest.get("e2e_functions") or []:
        f, name = str(it.get("file") or ""), str(it.get("function") or "")
        if f and name:
            claims.append((f, name))
    for t in manifest.get("targets") or []:
        f = str(t.get("file") or "")
        for name in t.get("functions") or []:
            if f and name:
                claims.append((f, str(name)))
    mismatch: list[str] = []
    for f, name in claims:
        fc = report.files.get(f)
        fn = fc.functions.get(name) if fc else None
        if fn is None or fn.execution_count == 0:
            mismatch.append(f"{f}::{name}")
    return sorted(set(mismatch))


def _plan_ghost_functions(cfg: ProjectConfig, plan: dict) -> list[dict]:
    """Validate analyzer's test_plan targets against the real function inventory.

    A plan target referencing a function that does not exist anywhere in the
    source (analyzer hallucination) would send gen chasing a ghost. Every target
    function is checked against source.function_inventory(); a name missing from
    the whole inventory is reported as {file, function}. Matching is
    name-anywhere (not file-exact) on purpose: a path-formatting difference must
    not produce a false ghost.
    """
    from .source import function_inventory
    try:
        inventory = function_inventory(cfg.source_files(), cfg.source_path)
    except Exception:  # noqa: BLE001 — 清单失败时跳过校验（fail-soft）
        return []
    known = {fi.name for fi in inventory}
    ghosts: list[dict] = []
    for t in plan.get("targets") or []:
        f = str(t.get("file") or "")
        for name in t.get("functions") or []:
            name = str(name)
            if name and name not in known:
                ghosts.append({"file": f, "function": name})
    return ghosts


def _undeclared_unit_channel(cfg: ProjectConfig, manifest: dict) -> list[dict]:
    """AST-detect test functions exercising the unit channel (C/C++).

    Every test_* function whose body calls compile_unit_driver/run_driver is a
    unit-coverage source; before this gate existed, gen could simply omit the
    unit_confirm_required declaration and the coverage entered silently
    (Go got auto-detection via go_test_scope; this restores parity for C/C++).
    Detected entries enter the pending ledger even without any declaration.
    """
    import ast
    out: list[dict] = []
    for f in manifest.get("test_files") or []:
        p = Path(f)
        if not p.is_absolute():
            p = cfg.test_dir / p
        if not (p.is_file() and p.suffix == ".py"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            for n in ast.walk(node):
                if not isinstance(n, ast.Call):
                    continue
                fname = (n.func.id if isinstance(n.func, ast.Name)
                         else (n.func.attr if isinstance(n.func, ast.Attribute) else ""))
                if fname in _UNIT_CHANNEL_FUNCS:
                    out.append({
                        "file": p.name, "function": node.name,
                        "evidence": "AST 检测到单测通道原子函数调用"
                                    f"（{fname}）但未在 unit_confirm_required 声明",
                    })
                    break
    return out


def _go_unit_tests(cfg: ProjectConfig, manifest: dict) -> list[dict]:
    """Auto-classify the manifest's *_test.go files, returning pure-unit test functions
    (no E2E signal). Used by the Go E2E-first gate when gen forgot to declare coverage
    sources in manifest.unit_confirm_required."""
    test_files = manifest.get("test_files") or []
    if not test_files or not any(str(f).endswith("_test.go") for f in test_files):
        return []
    from .go_test_scope import scan_go_test_sources
    # scan is keyed by test-function name across the whole source tree; filter to the
    # manifest's own files so we only gate on newly generated tests.
    own = {str(f) for f in test_files}
    out: list[dict] = []
    for _name, tf in scan_go_test_sources(cfg.source_path).items():
        if tf.file in own and tf.source == "unit":
            out.append({"file": tf.file, "function": tf.name,
                        "evidence": "纯单测：测试函数无 HTTP/网络 e2e 信号，直接调用对象方法"})
    return out


def _prompt_verify(cfg: ProjectConfig, run_id: str, iter_n: int, iter_dir: Path,
                   manifest: dict) -> str:
    files = manifest.get("test_files", [])
    is_go = getattr(cfg, "language", "c") == "go"
    lang_note = (
        "审查对象是 Go *_test.go 测试函数（func TestXxx(t *testing.T)）。"
        "【E2E-first】检查用例是否优先走 e2e/集成（httptest/gin 起 server 走真实链路）；"
        "对 manifest.unit_confirm_required 或纯单测（无 HTTP/网络信号）覆盖的函数，"
        "确认其确属 e2e 不可达（N1/N3/N5）且证据充分，否则提出修复。"
        if is_go else
        "测试目录：$AICOV_TEST_DIR = 测试目录见环境变量\nharness：见环境变量 AICOV_TEST_DIR 下 lib/harness.py"
    )
    return f"""静态审查本轮生成的用例（run_id={run_id} iter={iter_n}）。

manifest 声明的文件：{json.dumps(files, ensure_ascii=False)}
{lang_note}

逐文件按 V1-V5 清单审查，产出 → {iter_dir / "verify_report.json"}"""


def _prompt_quality(run_id: str, iter_n: int, iter_dir: Path,
                    execution: dict, known_badcases: str = "",
                    *, is_go: bool = False, extra_note: str = "") -> str:
    log_ref = (iter_dir / "gotest.log") if is_go else (iter_dir / "pytest.log")
    junit_ref = "" if is_go else f"junit：{iter_dir / 'junit.xml'}\n"
    return f"""分析本轮执行失败（run_id={run_id} iter={iter_n}）。

执行结果：{json.dumps(execution, ensure_ascii=False, indent=1)}
{junit_ref}测试日志：{log_ref}
覆盖率：{iter_dir / "coverage.json"}
测试目录/harness/源码路径：见环境变量 AICOV_TEST_DIR / AICOV_SRC
{known_badcases}{extra_note}
按失败归因分类逐个分析，产出 → {iter_dir / "quality_report.json"}
（含 badcase_candidates 字段：只提议**新的**可泛化失败模式，与上方已知条目
同模式的不重复提议；无新模式输出空数组。）

失败归因判定顺序（强制决策树，防误判）：
1. 先判 env_blocked / infra（环境/框架问题——跳过理由、收集错误、fixture 失败）
2. 再判 flaky（优先用执行结果里的 flaky_cases 事实性证据，勿凭感觉）
3. 再判 case_bug（必须给出"源码行为 vs 用例预期"的具体矛盾点 file:line）
4. 以上都不是、且输入合法但行为与源码逻辑矛盾 → 才允许 product_suspect（附复现命令与证据链）"""


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
    interactive: bool = False,
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

    # Per-run retry budget: the cumulative-backoff ledger is module-level state and
    # must not leak across runs (MR loops invoke run_loop once per batch).
    reset_backoff()

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
            # Zero-LLM ghost-function gate (2026-08-27 hardening): every plan target
            # function must exist in the real function inventory. Hallucinated names
            # are stripped from what gen ever sees and disclosed via diagnostic.
            ghosts = _plan_ghost_functions(cfg, plan)
            if ghosts:
                obs.emit_diagnostic(
                    "PLAN_GHOST_FUNCTION", run_id,
                    message=f"测试计划引用了 {len(ghosts)} 个源码中不存在的函数（已确定性剔除）",
                    stage="analyze", runs_dir=runs_dir,
                    context={"ghosts": ghosts[:20]})
                st.update_state(runs_dir, run_id, {"plan_ghosts": ghosts})
                ghost_set = {(g.get("file"), g.get("function")) for g in ghosts}
                kept = []
                for t in plan["targets"]:
                    t = dict(t)
                    t["functions"] = [fn for fn in (t.get("functions") or [])
                                      if (t.get("file"), str(fn)) not in ghost_set]
                    if t.get("functions"):
                        kept.append(t)
                plan["targets"] = kept
                print(f"  ⚠️ 计划含 {len(ghosts)} 个幽灵函数（源码中不存在），已剔除")
            plan_summary = json.dumps(plan["targets"][:30], ensure_ascii=False)
            print(f"  ✅ 分析完成：{len(plan['targets'])} 个测试目标")
        else:
            print("  ⚠️ analyzer 未产出有效计划（降级为纯覆盖率驱动）")
        obs.emit("stage.exit", run_id, stage="analyze", runs_dir=runs_dir,
                 data={"success": res.success, "plan": bool(plan)})

    # ── [1] Instrumented build ──────────────────────────────────
    # Go is instrumented natively by `go test -coverprofile`; no --coverage build
    # step (or binary) exists, so the build stage is skipped entirely.
    is_go = getattr(cfg, "language", "c") == "go"
    if is_go:
        print("▶ [1] 插桩构建（跳过——Go 由 go test -coverprofile 原生插桩）")
    elif skip_build:
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
    if cfg.language == "go":
        # Go tests are *_test.go colocated with source packages; `go test` discovers
        # them natively (no tests/ dir needed).
        existing_tests = [
            p for p in cfg.source_path.rglob("*_test.go")
            if p.is_file()
        ]
    else:
        existing_tests = list(cfg.test_dir.glob("test_*.py")) if cfg.test_dir.exists() else []
    print(f"▶ [2] 基线覆盖率（已有用例 {len(existing_tests)} 个）")
    if existing_tests:
        exec0 = run_tests(cfg, baseline_dir)
        baseline_cov_path = baseline_dir / "coverage.json"
    else:
        exec0 = None
        baseline_cov_path = run_dir / "baseline_coverage.json"
        if cfg.language == "go":
            # Go has no gcov; run `go test -coverprofile` once to get the empty baseline.
            go_res = run_go_tests(cfg, baseline_dir)
            baseline_cov_path = baseline_dir / "coverage.json"
            if not go_res.coverage_path:
                print("  ⚠️ go test 未产出 coverprofile，基线的函数清单为空")
        else:
            baseline_cov = gcov_collect(
                cfg.source_path, cfg.gcov_bin,
                include_filter=cfg.include_globs, exclude_filter=cfg.exclude_globs)
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

        # [b1] E2E-first: human confirmation gate for unit-test coverage
        # gen declares unit-test-covered functions in manifest.unit_confirm_required.
        # Interactive loop prompts y/n per function; non-interactive keeps them pending
        # unless unit_confirm_auto_yes. Confirmed functions count as accepted coverage;
        # pending ones are flagged in state/report for later human review.
        confirm = _confirm_unit_coverage(cfg, manifest, interactive=interactive)
        if confirm["declared"]:
            print(f"      🚦 单测覆盖需人工确认：声明 {len(confirm['declared'])} 个，"
                  f"已确认 {len(confirm['confirmed'])} 个，待确认 {len(confirm['pending'])} 个")
            if confirm["pending"]:
                obs.emit_diagnostic(
                    "UNIT_CONFIRM_PENDING", run_id,
                    message=f"iter {iter_n} 有 {len(confirm['pending'])} 个单测覆盖待人工确认",
                    iter_n=iter_n, stage="gen", runs_dir=runs_dir)
            # persist per-iteration gate result for the final report
            (iter_dir / "unit_confirm.json").write_text(
                json.dumps(confirm, ensure_ascii=False, indent=1), encoding="utf-8")
            st.update_iteration(runs_dir, run_id, iter_n, {
                "unit_confirm": {
                    "declared": len(confirm["declared"]),
                    "confirmed": [f"{d['file']}::{d['function']}" for d in confirm["confirmed"]],
                    "pending": [f"{d['file']}::{d['function']}" for d in confirm["pending"]],
                },
            })

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
                        _prompt_verify(cfg, run_id, iter_n, iter_dir, manifest),
                        iter_dir, iter_n, "verify", retries=1)
            report = _read_json(iter_dir / "verify_report.json") or {
                "verdict": "fail", "problems": [],
                "summary": "verify-agent 未产出报告",
            }
            # Deterministic zero-LLM gates (merged into verify_report.json):
            #   - docstyle EC-07: docstring 描述/测试点 header fields
            #   - assertquality EC-08: tautological/weak/missing assertions
            # (both are pytest-specific; Go *_test.go has neither docstrings nor
            #  harness atomic functions, so both are skipped for Go projects)
            doc_problems = ([] if cfg.language == "go"
                            else check_test_docstrings(cfg.test_dir, manifest.get("test_files", [])))
            aq_problems = ([] if cfg.language == "go"
                           else check_assert_quality(cfg.test_dir, manifest.get("test_files", [])))
            gate_problems = doc_problems + aq_problems
            if gate_problems:
                report["problems"] = list(report.get("problems", [])) + gate_problems
                report["verdict"] = "fail"
                (iter_dir / "verify_report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"      ⚠️ 确定性门禁未过：文档头 {len(doc_problems)} 处 /"
                      f" 断言质量 {len(aq_problems)} 处")
            problems = report.get("problems", [])
            errors = [p for p in problems if p.get("severity") == "error"]
            obs.emit("stage.exit", run_id, iter_n=iter_n, stage="verify",
                     runs_dir=runs_dir,
                     data={"verdict": report.get("verdict"),
                           "errors": len(errors), "warns": len(problems) - len(errors),
                           "doc_gate_violations": len(doc_problems),
                           "assert_gate_violations": len(aq_problems)})
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
              f"skip={execution.skipped}"
              + (f" flaky={len(execution.flaky_cases)}" if execution.flaky_cases else "")
              + f" ({execution.duration_s:.1f}s)")
        # High skip rate is a partial "nothing verified" signal: skipped cases
        # produce no evidence. Diagnose it and force quality analysis even when
        # the overall verdict is PASS (2026-08-27 hardening).
        skip_rate = (execution.skipped / execution.tests) if execution.tests else 0.0
        if execution.tests and skip_rate > 0.30:
            obs.emit_diagnostic(
                "HIGH_SKIP_RATE", run_id,
                message=f"iter {iter_n} 用例跳过率 {skip_rate:.0%}"
                        f"（{execution.skipped}/{execution.tests}）——被跳过的用例未产生任何验证",
                iter_n=iter_n, stage="execute", runs_dir=runs_dir,
                context={"skip_rate": round(skip_rate, 3),
                         "skipped": execution.skipped, "tests": execution.tests})
        st.update_iteration(runs_dir, run_id, iter_n, {
            "execute_verdict": execution.verdict,
            "gen_output": "ok",
            **({"skip_rate": round(skip_rate, 3)} if execution.tests else {}),
        })

        # [e] quality analysis (when not PASS, or skip rate suspiciously high)
        if execution.verdict != "PASS" or (execution.tests and skip_rate > 0.30):
            print("  [e] 失败分析（quality-agent）")
            obs.emit("stage.enter", run_id, iter_n=iter_n, stage="quality", runs_dir=runs_dir)
            from .badcase import badcase_hint
            skip_note = (f"\n⚠️ 本轮跳过率 {skip_rate:.0%}"
                         f"（{execution.skipped}/{execution.tests}）：请逐个查看 pytest.log 中"
                         f"被跳过用例的 skip 理由，归因到 env_blocked（二进制缺失/环境不满足）"
                         f"或 case 问题，并给出修复建议。\n" if execution.tests and skip_rate > 0.30 else "")
            flaky_note = (f"\n⚠️ 确定性 flaky 复检：以下用例两次运行结果不一致（事实性 flaky 证据，"
                          f"直接标 flaky 勿猜）：{execution.flaky_cases}\n"
                          if execution.flaky_cases else "")
            await _call("quality-agent",
                        _prompt_quality(run_id, iter_n, iter_dir, execution.to_dict(),
                                        known_badcases=badcase_hint(cfg),
                                        is_go=(cfg.language == "go"),
                                        extra_note=skip_note + flaky_note),
                        iter_dir, iter_n, "quality", retries=1)
            quality = _read_json(iter_dir / "quality_report.json")
            obs.emit("stage.exit", run_id, iter_n=iter_n, stage="quality",
                     runs_dir=runs_dir,
                     data={"verdict": (quality or {}).get("verdict")})
            if quality:
                quality_actions = quality.get("action_items", [])
                print(f"      verdict={quality.get('verdict')} "
                      f"action_items={len(quality_actions)}")
                # Bug cross-validation (plan 3.1, zero LLM): every report_bug item is
                # checked against hard facts (cited file exists; referenced case
                # actually failed). Invalid ones are downgraded out of the final
                # report's "suspected defects" section -- hallucinated bugs must
                # not reach the reader.
                from .bugcheck import validate_bug_reports
                bv = validate_bug_reports(cfg, quality, execution.cases)
                if bv["valid"] or bv["invalid"]:
                    (iter_dir / "bug_validation.json").write_text(
                        json.dumps(bv, ensure_ascii=False, indent=1), encoding="utf-8")
                if bv["invalid"]:
                    print(f"      🚫 {len(bv['invalid'])} 个 report_bug 证据不足已降级"
                          f"（{len(bv['valid'])} 个有效保留）")
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

        # Claim-vs-fact gate (zero LLM, 2026-08-27 hardening): every function gen
        # declared covered (e2e_functions / targets) must actually have
        # execution_count > 0 in the gcov report. A mismatch is a declaration/fact
        # divergence -> state + diagnostic + reflux to next round's gen.
        if execution.coverage_path and execution.coverage_path.exists():
            claim_miss = _verify_manifest_claims(manifest, current_full)
            if claim_miss:
                print(f"      ⚠️ 声明与事实不符：{len(claim_miss)} 个函数声明已覆盖但 gcov 未命中")
                st.update_iteration(runs_dir, run_id, iter_n,
                                    {"claim_mismatch": claim_miss})
                obs.emit_diagnostic(
                    "CLAIM_MISMATCH", run_id,
                    message=f"iter {iter_n} manifest 声明覆盖但实际未命中 {len(claim_miss)} 个函数",
                    iter_n=iter_n, stage="update", runs_dir=runs_dir,
                    context={"functions": claim_miss[:20]})
                quality_actions.append({
                    "type": "claim_mismatch", "functions": claim_miss,
                    "suggestion": "以下函数被声明为已覆盖但 gcov 显示未命中："
                                  "要么修复用例使其真正触达，要么在 manifest 中如实更正声明"})

        # E2E-first unit-ratio quota (2026-08-27 hardening): unit-covered share of
        # this round's newly-hit functions above max_unit_ratio -> diagnostic +
        # hard e2e-first instruction refluxed into the next gen round.
        if cfg.e2e_first and delta.get("newly_hit"):
            newly = {(d["file"], d["name"]) for d in delta["newly_hit"]}
            unit_claims = {(str(u.get("file")), str(u.get("function")))
                           for u in manifest.get("unit_confirm_required") or []}
            unit_new = newly & unit_claims
            if newly and unit_new:
                ratio = len(unit_new) / len(newly)
                quota = getattr(cfg, "max_unit_ratio", 0.15)
                if ratio > quota:
                    print(f"      ⚠️ 单测覆盖占比 {ratio:.0%} 超过配额 {quota:.0%}"
                          f"（{len(unit_new)}/{len(newly)}）")
                    st.update_iteration(runs_dir, run_id, iter_n,
                                        {"unit_ratio": round(ratio, 3)})
                    obs.emit_diagnostic(
                        "UNIT_RATIO_EXCEEDED", run_id,
                        message=f"iter {iter_n} 新增命中中单测覆盖占比 {ratio:.0%} 超过配额 {quota:.0%}",
                        iter_n=iter_n, stage="update", runs_dir=runs_dir,
                        context={"ratio": round(ratio, 3), "quota": quota,
                                 "unit_functions": sorted(f"{f}::{n}" for f, n in unit_new)[:20]})
                    quality_actions.append({
                        "type": "unit_ratio_exceeded", "ratio": round(ratio, 3),
                        "functions": sorted(f"{f}::{n}" for f, n in unit_new),
                        "suggestion": f"本轮新增命中中单测覆盖占比 {ratio:.0%} 超过配额 {quota:.0%}："
                                      f"下一轮必须优先尝试 e2e 触发路径（run_binary），"
                                      f"把上述函数从单测改为 e2e 覆盖"})

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
