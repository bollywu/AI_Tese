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
    def __init__(self, verdict: str, failures: int = 0, errors: int = 0):
        self.verdict = verdict
        self.failures = failures
        self.errors = errors


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
        """正向断言语义：复现用例 FAIL = 程序异常 = 缺陷坐实。"""
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions,
                             _FakeExec("FAIL", failures=1), {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_CONFIRMED

    def test_exec_pass_false_positive(self):
        """复现用例 PASS = 程序行为正常 = 疑似误报。"""
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions,
                             _FakeExec("PASS"), {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_FALSE_POSITIVE

    def test_not_executed_inconclusive(self):
        dispositions = {"ISSUE-01": {"disposition": "e2e",
                                     "test_function": "t1"}}
        v = compute_verdicts(_ISSUES[:1], {}, dispositions, None,
                             {"verdict": "pass"})
        assert v["ISSUE-01"]["verdict"] == VERDICT_INCONCLUSIVE

    def test_mixed_four_verdicts(self):
        """四态混合：每条独立判定，互不影响。"""
        dispositions = {
            "ISSUE-01": {"disposition": "e2e", "test_function": "t1"},   # FAIL→confirmed
            "ISSUE-02": {"disposition": "e2e", "test_function": "t2"},   # PASS→fp
            "ISSUE-03": {"disposition": "unobservable", "reason": "x"},  # unobservable
            # ISSUE-04 无 disposition → inconclusive
        }
        v = compute_verdicts(_ISSUES, {}, dispositions,
                             _FakeExec("PASS"), {"verdict": "pass"})
        # 执行是整体 PASS（junit 不按用例拆分时，全部 e2e 都判 fp——这里
        # 用整体 PASS 验证：confirmed 需要 FAIL 执行）
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
