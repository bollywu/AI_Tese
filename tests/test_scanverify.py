"""扫描轨单测：四态裁决规则 + 报告渲染 + MR 编排的确定性前置分支。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.scanverify import (  # noqa: E402
    VERDICT_CONFIRMED, VERDICT_FALSE_POSITIVE, VERDICT_INCONCLUSIVE,
    VERDICT_UNOBSERVABLE, compute_verdicts, render_scan_markdown,
)


class _FakeExec:
    def __init__(self, verdict: str, failures: int = 0, errors: int = 0,
                 cases: dict[str, str] | None = None):
        self.verdict = verdict
        self.failures = failures
        self.errors = errors
        # per-case results keyed by bare test-function name (new contract:
        # adjudication looks up the issue's own test function, never the
        # whole-run verdict)
        self.cases = cases if cases is not None else {}


_ISSUES = [
    {"issue_id": "ISSUE-01", "title": "realloc 失败泄漏", "severity": "high",
     "function": "grow_buffer"},
    {"issue_id": "ISSUE-02", "title": "未检查返回值", "severity": "medium",
     "function": "parse_hdr"},
    {"issue_id": "ISSUE-03", "title": "TOCTOU", "severity": "low",
     "function": "check_file"},
    {"issue_id": "ISSUE-04", "title": "有符号比较", "severity": "low",
     "function": "scan_args"},
]


class TestVerdicts:
    def test_unobservable_wins_first(self):
        """disposition=unobservable 优先于一切——gen 静态论证为不可观测时不看执行。"""
        dispositions = {"ISSUE-01": {"disposition": "unobservable",
                                     "reason": "UB 在当前架构无副作用"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions,
                             _FakeExec("FAIL", failures=1), {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_UNOBSERVABLE
        assert "当前架构" in v["ISSUE-01"]["evidence"]

    def test_no_disposition_inconclusive(self):
        v = compute_verdicts(_ISSUES, {}, {}, None, {"verdict": "pass"})
        assert all(x["verdict"] == VERDICT_INCONCLUSIVE for x in v.values())

    def test_verify_fail_inconclusive_not_confirmed(self):
        """用例没过静态审查 → inconclusive（用例质量问题≠缺陷真伪）。"""
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions, _FakeExec("FAIL", 1),
                             {"verdict": "fail"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_INCONCLUSIVE

    def test_exec_fail_confirmed(self):
        """正向断言语义：该 issue 自己的复现用例 FAIL = 程序异常 = 缺陷坐实。"""
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions,
                             _FakeExec("FAIL", failures=1, cases={"t1": "fail"}),
                             {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_CONFIRMED

    def test_exec_pass_false_positive(self):
        """复现用例 PASS = 程序行为正常 = 疑似误报。"""
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions,
                             _FakeExec("PASS", cases={"t1": "pass"}),
                             {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_FALSE_POSITIVE

    def test_no_case_detail_never_borrows_global_verdict(self):
        """防张冠李戴回归（2026-08-27 修复核心）：junit 无用例明细时，
        即使整轮 FAIL 也不把全局结论借给任何 issue——一律 inconclusive，不猜。"""
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions,
                             _FakeExec("FAIL", failures=1),  # cases 为空
                             {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_INCONCLUSIVE

    def test_per_issue_attribution_no_cross_contamination(self):
        """逐 issue 归因（修复前缺陷：3 个 issue 复用同一全局 verdict）：
        同一轮执行中 t1 FAIL、t2 PASS → ISSUE-01 坐实、ISSUE-02 误报，
        互不污染；test_function 与实际用例名不一致 → inconclusive。"""
        dispositions = {
            "ISSUE-01": {"disposition": "e2e", "test_function": "t1"},
            "ISSUE-02": {"disposition": "e2e", "test_function": "t2"},
            "ISSUE-03": {"disposition": "e2e", "test_function": "t_typo"},
        }
        v = compute_verdicts(
            _ISSUES[:3], {}, dispositions,
            _FakeExec("FAIL", failures=1, cases={"t1": "fail", "t2": "pass"}),
            {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_CONFIRMED
        assert v["ISSUE-02"]["verdict"] == VERDICT_FALSE_POSITIVE
        assert v["ISSUE-03"]["verdict"] == VERDICT_INCONCLUSIVE

    def test_case_error_or_skipped_is_inconclusive(self):
        """用例 error（setup/框架异常）或 skipped 都不构成断言结论 → inconclusive。"""
        dispositions = {
            "ISSUE-01": {"disposition": "e2e", "test_function": "t1"},
            "ISSUE-02": {"disposition": "e2e", "test_function": "t2"},
        }
        v = compute_verdicts(
            _ISSUES[:2], {}, dispositions,
            _FakeExec("FAIL", failures=1, cases={"t1": "error", "t2": "skipped"}),
            {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_INCONCLUSIVE
        assert v["ISSUE-02"]["verdict"] == VERDICT_INCONCLUSIVE

    def test_not_executed_inconclusive(self):
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions, None,
                             {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_INCONCLUSIVE

    def test_mixed_four_verdicts(self):
        """四态混合：每条独立判定，互不影响（按各自 test_function 的执行状态）。"""
        dispositions = {
            "ISSUE-01": {"disposition": "e2e", "test_function": "t1"},   # fail→confirmed
            "ISSUE-02": {"disposition": "e2e", "test_function": "t2"},   # pass→fp
            "ISSUE-03": {"disposition": "unobservable", "reason": "x"},  # unobservable
            # ISSUE-04 无 disposition → inconclusive
        }
        v = compute_verdicts(
            _ISSUES, {}, dispositions,
            _FakeExec("FAIL", failures=1, cases={"t1": "fail", "t2": "pass"}),
            {"verdict": "pass"})
        # 同一轮执行：t1 FAIL、t2 PASS —— 按 issue 自己的用例分别裁决
        assert v["ISSUE-01"]["verdict"] == VERDICT_CONFIRMED
        assert v["ISSUE-02"]["verdict"] == VERDICT_FALSE_POSITIVE
        assert v["ISSUE-03"]["verdict"] == VERDICT_UNOBSERVABLE
        assert v["ISSUE-04"]["verdict"] == VERDICT_INCONCLUSIVE


class TestRenderMarkdown:
    def test_no_issues_renders_clean(self):
        md = render_scan_markdown({"issues": [], "scan_summary": "无问题"})
        assert "未发现问题" in md

    def test_issues_table_with_icons(self):
        result = {
            "issues": _ISSUES,
            "verdicts": {
                "ISSUE-01": {"verdict": "confirmed", "evidence": "复现用例 FAIL"},
                "ISSUE-02": {"verdict": "false_positive", "evidence": "PASS"},
                "ISSUE-03": {"verdict": "unobservable", "evidence": "x"},
                "ISSUE-04": {"verdict": "inconclusive", "evidence": "y"},
            },
        }
        md = render_scan_markdown(result)
        assert "🔴 坐实" in md and "🟢 疑似误报" in md
        assert "⚪ 不可观测" in md and "🟡 待人工" in md
        # 人工优先看坐实的：confirmed → inconclusive → unobservable → false_positive
        assert (md.index("ISSUE-01") < md.index("ISSUE-04")
                < md.index("ISSUE-03") < md.index("ISSUE-02"))
        assert "正向断言" in md  # 裁决语义说明


# ── MR 编排：确定性前置分支（不调 LLM）──────────────────────────

class TestMrLoopGuards:
    def test_codegraph_disabled_errors_out(self, tmp_path, monkeypatch):
        """[codegraph].enabled=false → 明确报错而非静默退化（Q2 决策）。"""
        import asyncio

        from aicoverage.config import ProjectConfig
        from aicoverage.mr_loop import run_mr_loop

        src = tmp_path / "proj"
        (src / "tests").mkdir(parents=True)
        cfg = ProjectConfig.minimal(src, name="proj", build_cmd="make", binary="app")
        cfg.codegraph_enabled = False
        # workspace/runs_dir 是 property（由 source_path 派生），无需也无法赋值

        summary = asyncio.run(run_mr_loop(cfg, base_ref="HEAD~1", head_ref="HEAD"))
        assert summary["status"] == "error"
        assert summary["exit_reason"] == "codegraph_disabled"

    def test_codegraph_not_indexed_errors_out(self, tmp_path):
        """enabled=true 但索引不存在 → 报错并给出建索引命令。"""
        import asyncio

        from aicoverage.config import ProjectConfig
        from aicoverage.mr_loop import run_mr_loop

        src = tmp_path / "proj2"
        (src / "tests").mkdir(parents=True)
        cfg = ProjectConfig.minimal(src, name="proj", build_cmd="make", binary="app")
        cfg.codegraph_enabled = True
        cfg.codegraph_index_dir = ".codegraph"
        cfg.codegraph_entrypoints = ["main"]

        summary = asyncio.run(run_mr_loop(cfg, base_ref="HEAD~1", head_ref="HEAD"))
        assert summary["status"] == "error"
        assert summary["exit_reason"] == "codegraph_not_indexed"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
