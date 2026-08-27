"""2026-08-27 测试质量加固的配套单测。

覆盖：
  - executor：逐用例结果解析（junit/Go）、all_skipped 门禁、前置自检、flaky 确定性复检
  - loop：manifest 声明校验（claim mismatch）、C/C++ 单测通道自动检测、plan 幽灵函数
  - bugcheck：report_bug 交叉校验、base/head 回归裁决
  - config：flaky_rerun / max_unit_ratio 解析
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig, load_config  # noqa: E402
from aicoverage.executor import (  # noqa: E402
    _parse_go_cases, _parse_junit_cases, run_tests,
)
from aicoverage.gcov import CoverageReport, FileCov, FunctionCov  # noqa: E402


# ── executor：逐用例结果解析 ─────────────────────────────────────────

class TestParseJunitCases:
    def test_status_classification_and_bare_names(self, tmp_path):
        xml = """<?xml version="1.0"?>
<testsuites><testsuite name="t" tests="5" failures="1" errors="1" skipped="1">
<testcase classname="tests.test_x" name="test_a"/>
<testcase classname="tests.test_x" name="test_b"><failure/></testcase>
<testcase classname="tests.test_x" name="test_c"><error/></testcase>
<testcase classname="tests.test_x" name="test_d"><skipped/></testcase>
<testcase classname="tests.test_x" name="test_e[param0]"><failure/></testcase>
</testsuite></testsuites>"""
        p = tmp_path / "junit.xml"
        p.write_text(xml, encoding="utf-8")
        cases = _parse_junit_cases(p)
        assert cases == {"test_a": "pass", "test_b": "fail", "test_c": "error",
                         "test_d": "skipped", "test_e": "fail"}

    def test_parametrized_worst_of_merge(self, tmp_path):
        """参数化用例多实例合并取最差：一个 fail → 函数级 fail。"""
        xml = """<?xml version="1.0"?>
<testsuites><testsuite name="t" tests="3" failures="1">
<testcase name="test_p[case0]"/>
<testcase name="test_p[case1]"><failure/></testcase>
<testcase name="test_p[case2]"/>
</testsuite></testsuites>"""
        p = tmp_path / "junit.xml"
        p.write_text(xml, encoding="utf-8")
        assert _parse_junit_cases(p) == {"test_p": "fail"}

    def test_broken_xml_returns_empty(self, tmp_path):
        p = tmp_path / "junk.xml"
        p.write_text("not xml", encoding="utf-8")
        assert _parse_junit_cases(p) == {}


class TestParseGoCases:
    def test_go_verbose_lines(self):
        log = ("=== RUN   TestA\n--- PASS: TestA (0.00s)\n"
               "=== RUN   TestB\n--- FAIL: TestB (0.01s)\n"
               "--- SKIP: TestC (0.00s)\n"
               "=== RUN   TestD/sub\n--- FAIL: TestD/sub (0.00s)\n"
               "PASS\nok  pkg 0.1s\n")
        cases = _parse_go_cases(log)
        assert cases["TestA"] == "pass"
        assert cases["TestB"] == "fail"
        assert cases["TestC"] == "skipped"
        # 子测试失败合并到父函数（最差）
        assert cases["TestD"] == "fail"


# ── executor：all_skipped / preflight / flaky（真实 pytest 执行）────────

def _mk_project(tmp_path: Path, test_body: str, *, binary_exists: bool = True) -> ProjectConfig:
    src = tmp_path / "proj"
    (src / "tests").mkdir(parents=True, exist_ok=True)
    (src / "tests" / "test_case.py").write_text(test_body, encoding="utf-8")
    binary = src / "app"
    if binary_exists:
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return ProjectConfig.minimal(src, name="proj", build_cmd="true",
                                 binary="app" if binary_exists else "missing_app")


class TestAllSkippedGuard:
    def test_all_skipped_is_blocked_not_pass(self, tmp_path):
        """全部用例被跳过（rc=0）→ BLOCKED/all_skipped，不再是假 PASS（缺陷2）。"""
        cfg = _mk_project(tmp_path, (
            "import pytest\n"
            "def test_skipped():\n"
            "    pytest.skip('no binary')\n"
        ))
        res = run_tests(cfg, tmp_path / "iter", collect_coverage=False)
        assert res.verdict == "BLOCKED"
        assert res.failure_kind == "all_skipped"
        assert "被跳过" in res.detail
        # execution.json 落盘含 cases
        data = json.loads((tmp_path / "iter" / "execution.json").read_text(encoding="utf-8"))
        assert data["failure_kind"] == "all_skipped"
        assert data["cases"].get("test_skipped") == "skipped"

    def test_partial_skip_still_pass(self, tmp_path):
        """部分跳过（有真跑的用例）→ 正常 PASS。"""
        cfg = _mk_project(tmp_path, (
            "import pytest\n"
            "def test_skipped():\n"
            "    pytest.skip('x')\n"
            "def test_real():\n"
            "    assert 1 == 1\n"
        ))
        res = run_tests(cfg, tmp_path / "iter", collect_coverage=False)
        assert res.verdict == "PASS"
        assert res.cases["test_real"] == "pass"


class TestPreflight:
    def test_missing_binary_blocks_before_pytest(self, tmp_path):
        """被测二进制缺失 → 前置自检直接 BLOCKED（不浪费一次 pytest）。"""
        cfg = _mk_project(tmp_path, "def test_x():\n    assert True\n",
                          binary_exists=False)
        res = run_tests(cfg, tmp_path / "iter", collect_coverage=False)
        assert res.verdict == "BLOCKED"
        assert res.failure_kind == "env_blocked"
        assert "被测二进制不存在" in res.detail

    def test_syntax_error_blocks(self, tmp_path):
        """gen 产出语法坏文件 → BLOCKED 且指出文件与行号。"""
        cfg = _mk_project(tmp_path, "def test_bad(:\n")
        res = run_tests(cfg, tmp_path / "iter", collect_coverage=False)
        assert res.verdict == "BLOCKED"
        assert "语法错误" in res.detail


class TestFlakyRerun:
    def test_flaky_detected_by_deterministic_rerun(self, tmp_path):
        """两次运行结果不一致 → flaky_cases 记录（事实性证据，非 LLM 猜测）。

        构造：用例首次运行 FAIL 并落 marker 文件，第二次运行 PASS。
        """
        cfg = _mk_project(tmp_path, (
            "from pathlib import Path\n"
            "def test_flip():\n"
            "    m = Path(__file__).parent / 'flip_marker'\n"
            "    if m.exists():\n"
            "        assert True\n"
            "    else:\n"
            "        m.write_text('x')\n"
            "        assert False, 'first run fails'\n"
        ))
        marker = cfg.test_dir / "flip_marker"
        if marker.exists():
            marker.unlink()
        res = run_tests(cfg, tmp_path / "iter", collect_coverage=False)
        assert res.verdict == "FAIL"
        assert "test_flip" in res.flaky_cases
        marker.unlink(missing_ok=True)

    def test_stable_failure_not_flaky(self, tmp_path):
        """两次都失败 → 稳定失败，不进 flaky_cases。"""
        cfg = _mk_project(tmp_path, "def test_always_fail():\n    assert False\n")
        res = run_tests(cfg, tmp_path / "iter", collect_coverage=False)
        assert res.verdict == "FAIL"
        assert res.flaky_cases == []

    def test_flaky_rerun_disabled(self, tmp_path):
        """flaky_rerun=false 时不重跑。"""
        cfg = _mk_project(tmp_path, (
            "from pathlib import Path\n"
            "def test_flip():\n"
            "    m = Path(__file__).parent / 'flip2'\n"
            "    if m.exists():\n"
            "        assert True\n"
            "    else:\n"
            "        m.write_text('x')\n"
            "        assert False\n"
        ))
        cfg.flaky_rerun = False
        marker = cfg.test_dir / "flip2"
        if marker.exists():
            marker.unlink()
        res = run_tests(cfg, tmp_path / "iter", collect_coverage=False)
        assert res.verdict == "FAIL"
        assert res.flaky_cases == []
        marker.unlink(missing_ok=True)


# ── loop：声明校验 / 单测通道检测 / 幽灵函数 ──────────────────────────

class TestManifestClaimCheck:
    def _report(self) -> CoverageReport:
        rep = CoverageReport()
        fc = FileCov(file="src/a.c")
        fc.functions["hit_fn"] = FunctionCov("src/a.c", "hit_fn", 1, 5, 3, 2, 1)
        fc.functions["miss_fn"] = FunctionCov("src/a.c", "miss_fn", 10, 15, 0, 2, 0)
        rep.files["src/a.c"] = fc
        return rep

    def test_declared_but_unhit_is_mismatch(self):
        from aicoverage.loop import _verify_manifest_claims
        manifest = {
            "e2e_functions": [{"file": "src/a.c", "function": "hit_fn"},
                              {"file": "src/a.c", "function": "miss_fn"}],
            "targets": [{"file": "src/a.c", "functions": ["ghost_fn"]}],
        }
        miss = _verify_manifest_claims(manifest, self._report())
        assert miss == ["src/a.c::ghost_fn", "src/a.c::miss_fn"]

    def test_all_hit_no_mismatch(self):
        from aicoverage.loop import _verify_manifest_claims
        manifest = {"e2e_functions": [{"file": "src/a.c", "function": "hit_fn"}],
                    "targets": []}
        assert _verify_manifest_claims(manifest, self._report()) == []


class TestUnitChannelDetection:
    def test_undeclared_unit_channel_detected(self, tmp_path):
        """用例调 compile_unit_driver 但 manifest 未声明 → 自动检测进待确认（6.1）。"""
        from aicoverage.loop import _undeclared_unit_channel
        cfg = ProjectConfig.minimal(tmp_path, name="p")
        (cfg.test_dir).mkdir(parents=True, exist_ok=True)
        (cfg.test_dir / "test_ut.py").write_text(
            "def test_ut_parse():\n"
            "    res = compile_unit_driver('d.c', sources=['s.c'], out_name='ut')\n"
            "    assert_ut_compiled(res)\n"
            "    r = run_driver('ut', args=['x'])\n"
            "    assert_exit_code(r, 0)\n", encoding="utf-8")
        (cfg.test_dir / "test_e2e.py").write_text(
            "def test_e2e():\n"
            "    res = run_binary(['--flag'])\n"
            "    assert_exit_code(res, 0)\n", encoding="utf-8")
        manifest = {"test_files": ["test_ut.py", "test_e2e.py"]}
        detected = _undeclared_unit_channel(cfg, manifest)
        assert len(detected) == 1
        assert detected[0]["function"] == "test_ut_parse"
        assert detected[0]["file"] == "test_ut.py"
        assert "compile_unit_driver" in detected[0]["evidence"]

    def test_declared_still_detected_for_pending(self, tmp_path):
        """声明与否都会被检出（与 Go _go_unit_tests 对等：都进待确认台账）。"""
        from aicoverage.loop import _undeclared_unit_channel
        cfg = ProjectConfig.minimal(tmp_path, name="p")
        cfg.test_dir.mkdir(parents=True, exist_ok=True)
        (cfg.test_dir / "test_ut.py").write_text(
            "def test_ut_x():\n    r = run_driver('ut')\n    assert_exit_code(r, 0)\n",
            encoding="utf-8")
        manifest = {"test_files": ["test_ut.py"],
                    "unit_confirm_required": [{"file": "src/x.c", "function": "x"}]}
        assert len(_undeclared_unit_channel(cfg, manifest)) == 1


class TestPlanGhostFunctions:
    def test_ghost_detected_and_real_kept(self, tmp_path):
        from aicoverage.loop import _plan_ghost_functions
        src = tmp_path / "proj"
        (src / "src").mkdir(parents=True)
        (src / "src" / "a.c").write_text(
            "int real_fn(int x) {\n  return x + 1;\n}\n", encoding="utf-8")
        cfg = ProjectConfig.minimal(src, name="p")
        plan = {"targets": [
            {"id": "T-1", "file": "src/a.c", "functions": ["real_fn", "ghost_fn"]},
        ]}
        ghosts = _plan_ghost_functions(cfg, plan)
        assert ghosts == [{"file": "src/a.c", "function": "ghost_fn"}]


# ── bugcheck：report_bug 交叉校验 / 回归裁决 ─────────────────────────

class TestBugValidation:
    def _cfg(self, tmp_path):
        src = tmp_path / "proj"
        src.mkdir()
        (src / "a.c").write_text("int f(void){return 0;}\n", encoding="utf-8")
        return ProjectConfig.minimal(src, name="p")

    def test_valid_bug_kept(self, tmp_path):
        from aicoverage.bugcheck import validate_bug_reports
        cfg = self._cfg(tmp_path)
        quality = {"failures": [
            {"test": "tests/test_x.py::test_a", "action": "report_bug",
             "evidence": "a.c:1 返回值与文档矛盾", "suggestion": "疑似缺陷"}]}
        out = validate_bug_reports(cfg, quality, {"test_a": "fail"})
        assert len(out["valid"]) == 1 and not out["invalid"]

    def test_hallucinated_file_downgraded(self, tmp_path):
        """证据引用不存在的文件 → 降级。"""
        from aicoverage.bugcheck import validate_bug_reports
        cfg = self._cfg(tmp_path)
        quality = {"failures": [
            {"test": "tests/test_x.py::test_a", "action": "report_bug",
             "evidence": "nonexistent.c:42 矛盾", "suggestion": "x"}]}
        out = validate_bug_reports(cfg, quality, {"test_a": "fail"})
        assert not out["valid"] and len(out["invalid"]) == 1
        assert "不存在" in out["invalid"][0]["reason"]

    def test_no_source_location_downgraded(self, tmp_path):
        """证据无 file:line → 不可核实，降级。"""
        from aicoverage.bugcheck import validate_bug_reports
        cfg = self._cfg(tmp_path)
        quality = {"action_items": [
            {"type": "report_bug", "suggestion": "这里好像有个 bug"}]}
        out = validate_bug_reports(cfg, quality, {})
        assert not out["valid"] and len(out["invalid"]) == 1
        assert "file:line" in out["invalid"][0]["reason"]

    def test_passed_case_claim_downgraded(self, tmp_path):
        """引用的用例实际 PASS → 疑似臆测，降级。"""
        from aicoverage.bugcheck import validate_bug_reports
        cfg = self._cfg(tmp_path)
        quality = {"failures": [
            {"test": "tests/test_x.py::test_a", "action": "report_bug",
             "evidence": "a.c:1 矛盾", "suggestion": "x"}]}
        out = validate_bug_reports(cfg, quality, {"test_a": "pass"})
        assert not out["valid"] and len(out["invalid"]) == 1
        assert "非失败" in out["invalid"][0]["reason"]


class TestRegressionVerdicts:
    def test_base_pass_head_fail_is_regression(self):
        from aicoverage.bugcheck import regression_verdicts
        head = {"t1": "fail", "t2": "fail", "t3": "pass"}
        base = {"t1": "pass", "t2": "fail"}
        v = regression_verdicts(head, base)
        assert v["t1"] == "regression_confirmed"
        assert v["t2"] == "preexisting"
        assert "t3" not in v  # 非失败用例不参与

    def test_missing_on_base_is_unknown(self):
        from aicoverage.bugcheck import regression_verdicts
        v = regression_verdicts({"t1": "fail"}, {})
        assert v["t1"] == "unknown"


# ── config：新增字段解析 ─────────────────────────────────────────────

class TestNewConfigFields:
    def test_flaky_rerun_and_unit_ratio_parsed(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "aicoverage.toml").write_text(
            '[project]\nname="p"\nlanguage="c"\n[source]\npath="."\n'
            '[build]\nbuild_cmd="make --coverage"\nbinary="./app"\n'
            '[test]\nflaky_rerun=false\n'
            '[coverage]\nmax_unit_ratio=0.3\n', encoding="utf-8")
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.flaky_rerun is False
        assert cfg.max_unit_ratio == 0.3

    def test_defaults(self, tmp_path):
        root = tmp_path / "proj2"
        root.mkdir()
        (root / "aicoverage.toml").write_text(
            '[project]\nname="p"\nlanguage="c"\n[source]\npath="."\n'
            '[build]\nbuild_cmd="make --coverage"\nbinary="./app"\n', encoding="utf-8")
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.flaky_rerun is True
        assert cfg.max_unit_ratio == 0.15


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
