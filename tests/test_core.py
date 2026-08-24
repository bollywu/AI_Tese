"""AIcoverage 核心模块单测（不依赖 SDK / LLM / 真实项目）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.globutil import glob_matches, match_one  # noqa: E402
from aicoverage.gcov import (  # noqa: E402
    BranchCov, CoverageReport, FileCov, FunctionCov,
)
from aicoverage.config import ProjectConfig  # noqa: E402
from aicoverage.executor import _parse_junit  # noqa: E402


# ── globutil：`**` 的 gitignore 语义 ─────────────────────────────

class TestGlob:
    def test_double_star_matches_zero_segment(self):
        # 修复回归：fnmatch 语义下 src/**/*.c 匹配不到 src/wrk.c（曾导致 wrk 覆盖率 0/0）
        assert match_one("src/wrk.c", "src/**/*.c")

    def test_double_star_matches_nested(self):
        assert match_one("src/a/b/c.c", "src/**/*.c")
        assert glob_matches("src/a/b/c.c", ["src/**/*.c"])

    def test_single_star_not_cross_slash(self):
        assert not match_one("a/b.c", "*.c")
        assert match_one("a/b.c", "**/*.c")

    def test_empty_patterns_no_match(self):
        assert not glob_matches("a.c", [])

    def test_exclude_semantics(self):
        assert glob_matches("deps/LuaJIT-2.1/x.c", ["**/*.c", "deps/**"])


# ── CoverageReport：聚合 / 序列化 / 增量 ──────────────────────────

def _mk_report(hit_add: bool) -> CoverageReport:
    r = CoverageReport(created_at="t")
    fc = FileCov(file="src/a.c")
    fc.functions["main"] = FunctionCov("src/a.c", "main", 1, 5, 1, 4, 4)
    fc.functions["add"] = FunctionCov("src/a.c", "add", 7, 9, 1 if hit_add else 0, 2, 1)
    fc.branches.append(BranchCov("src/a.c", 10, "main", 1, False, False))
    fc.branches.append(BranchCov("src/a.c", 10, "main", 0 if not hit_add else 1, True, False))
    fc.lines_total, fc.lines_hit = 4, 3 if hit_add else 2
    fc.line_counts = {1: 1, 3: 1, 8: 1 if hit_add else 0, 10: 1}
    r.files["src/a.c"] = fc
    return r


class TestCoverageReport:
    def test_collect_survives_duplicate_basename_gcno(self, tmp_path):
        """回归守卫（2026-08-24 真实事故）：libtool 类项目常见"同一源文件产出两份
        同 basename 的 .gcno"（静态编译 + PIC 共享库编译），其中只有一份被真正
        执行（有 .gcda）。collect() 必须能正确识别出真实数据，不能被"从未执行、
        无 .gcda"的另一份用全 0 覆盖（真实 bug：旧实现把所有 gcov 输出堆到同一
        平铺目录，同名文件互相覆盖，谁后处理谁赢，与是否有真实数据无关）。

        用真实 gcc --coverage 编译同一份源码两次到不同目录（模拟静态/PIC 双重
        编译），只让其中一次真正执行产生 .gcda，另一份保留零执行状态。
        """
        import shutil
        import subprocess as sp

        from aicoverage.gcov import collect

        gcc = shutil.which("gcc")
        if not gcc:
            pytest.skip("本机无 gcc，跳过真实编译回归测试")

        src_root = tmp_path / "proj"
        (src_root / "lib" / ".libs").mkdir(parents=True)
        src_file = src_root / "lib" / "foo.c"
        src_file.write_text(
            "int add(int a,int b){return a+b;}\n"
            "int main(int c,char**v){ if(c>1) return add(1,2); return 0; }\n",
            encoding="utf-8")

        # 变体 A（.libs/ 子目录，basename 相同）：真正编译+执行 → 有 .gcda
        # 用 -c 分步编译（.gcno 命名跟随 -o 的对象文件名，与真实 libtool 行为一致）
        exe_a = src_root / "lib" / ".libs" / "foo_a"
        sp.run([gcc, "--coverage", "-O0", "-g", "-c", "../foo.c", "-o", "foo.o"],
               check=True, capture_output=True, cwd=exe_a.parent)
        sp.run([gcc, "--coverage", "foo.o", "-o", "foo_a"],
               check=True, capture_output=True, cwd=exe_a.parent)
        sp.run(["./foo_a", "run"], cwd=exe_a.parent, capture_output=True)
        assert (exe_a.parent / "foo.gcno").exists()
        assert (exe_a.parent / "foo.gcda").exists()

        # 变体 B（lib/ 顶层，同 basename foo.gcno）：只编译不执行 → 无 .gcda
        exe_b = src_root / "lib" / "foo_b"
        sp.run([gcc, "--coverage", "-O0", "-g", "-c", "foo.c", "-o", "foo.o"],
               check=True, capture_output=True, cwd=exe_b.parent)
        sp.run([gcc, "--coverage", "foo.o", "-o", "foo_b"],
               check=True, capture_output=True, cwd=exe_b.parent)
        assert (src_root / "lib" / "foo.gcno").exists()
        assert not (src_root / "lib" / "foo.gcda").exists()

        report = collect(src_root, "gcov", include_filter=["lib/**/*.c"])
        add_fn = next((f for f in report.functions if f.name == "add"), None)
        assert add_fn is not None, f"未找到 add 函数；收集到的文件: {list(report.files)}"
        assert add_fn.hit, (
            f"add() 真实被执行过（变体A产生了.gcda），但 collect() 报告未命中——"
            f"说明真实数据被同名的未执行变体覆盖了（真实事故复现）")
        assert add_fn.execution_count > 0

    def test_collect_immune_to_subdir_string_sort_order(self, tmp_path):
        """回归守卫（2026-08-24 第二次真实事故，ModSecurity 闭环 iter6 中被
        gen-agent 自行用 gcov 实测发现）：修复事故①后，`collect()` 用未补零的
        整数字符串给每个 .gcno 命名独立子目录（"0","1",...），再靠
        `sorted(路径字符串)` 决定"先到先得"（seen_files）处理顺序——但字符串
        序不等于数值序（`"10" < "2"`，更极端地 `"122" < "56"`）。当"无 .gcda
        的重复编译份"恰好落在字符串序更靠前的子目录时，零数据会先写入并占位，
        真实覆盖被读成 0%。ModSecurity 闭环 iter6 全部 25 个目标函数命中此
        bug：gen-agent 新写的用例真实跑通、`.gcda` 里确有非零执行计数，但
        `coverage.json` 与 iter5 完全相同——新增的真实覆盖贡献被完全吞掉。

        本测试制造 12 份同 basename 的重复编译（index 0..11，天然产生
        "10"/"11" 与 "2".."9" 混排的字符串序陷阱），只让 index 最大（数值上
        最后处理、但字符串序排在中间）的那份真正执行。修复后的合并逻辑按
        (file, line) 取 count 更大的一份，与处理顺序完全无关，必须稳定通过。
        """
        import shutil
        import subprocess as sp

        from aicoverage.gcov import collect

        gcc = shutil.which("gcc")
        if not gcc:
            pytest.skip("本机无 gcc，跳过真实编译回归测试")

        src_root = tmp_path / "proj2"
        lib = src_root / "lib"
        lib.mkdir(parents=True)
        src_file = lib / "foo.c"
        src_file.write_text(
            "int add(int a,int b){return a+b;}\n"
            "int main(int c,char**v){ if(c>1) return add(1,2); return 0; }\n",
            encoding="utf-8")

        # 12 份重复编译（v00..v11，源路径本身零补齐，不制造路径层面的排序问题——
        # 陷阱完全来自 collect() 内部子目录的未补零命名），只让最后一份真正执行。
        n = 12
        for i in range(n):
            d = lib / f"v{i:02d}"
            d.mkdir()
            sp.run([gcc, "--coverage", "-O0", "-g", "-c", "../foo.c", "-o", "foo.o"],
                   check=True, capture_output=True, cwd=d)
            sp.run([gcc, "--coverage", "foo.o", "-o", "foo_bin"],
                   check=True, capture_output=True, cwd=d)
            assert (d / "foo.gcno").exists()
            if i == n - 1:
                sp.run(["./foo_bin", "run"], cwd=d, capture_output=True)
                assert (d / "foo.gcda").exists()
            else:
                assert not (d / "foo.gcda").exists()

        report = collect(src_root, "gcov", include_filter=["lib/**/*.c"])
        add_fn = next((f for f in report.functions if f.name == "add"), None)
        assert add_fn is not None, f"未找到 add 函数；收集到的文件: {list(report.files)}"
        assert add_fn.hit and add_fn.execution_count > 0, (
            "12 份重复编译中唯一有 .gcda 的真实数据被字符串排序陷阱吞掉了"
            "（真实事故复现：ModSecurity iter6 全部 25 个目标函数命中此路径）")


    def test_aggregates(self):
        r = _mk_report(hit_add=False)
        assert r.func_total == 2 and r.func_hit == 1
        assert r.func_pct == 50.0
        assert r.branch_total == 2 and r.branch_hit == 1
        assert r.cond_pct == 50.0
        assert [f.name for f in r.uncovered_functions()] == ["add"]

    def test_roundtrip_keeps_branches(self):
        # 修复回归：旧 load 不还原 branches，跨轮 cond_pct 归零
        r = _mk_report(hit_add=True)
        tmp = Path("/tmp/aicov_test_roundtrip.json")
        r.save(tmp)
        r2 = CoverageReport.load(tmp)
        assert r2.func_pct == 100.0
        assert r2.cond_pct == 100.0 and r2.branch_total == 2
        # line_counts 也必须 round-trip（HTML 逐行着色依赖它）
        assert r2.files["src/a.c"].line_counts == r.files["src/a.c"].line_counts
        tmp.unlink()

    def test_delta(self):
        before = _mk_report(hit_add=False)
        after = _mk_report(hit_add=True)
        d = after.delta(before)
        assert d["func_pp"] == 50.0 and d["cond_pp"] == 50.0
        assert len(d["newly_hit"]) == 1 and d["newly_hit"][0]["name"] == "add"


# ── incremental：MR 增量覆盖率 scope 收窄视图 ─────────────────────

class TestIncremental:
    def test_scope_report_narrows_functions_branches_lines(self):
        """收窄到 [("src/a.c","add")] 后：main 消失，line_counts 只保留 add
        的区间 [7,9]（main 的行 1/3 应被剔除，add 的行 8 保留）。"""
        from aicoverage.incremental import scope_report

        full = _mk_report(hit_add=False)
        scoped = scope_report(full, [("src/a.c", "add")])

        assert list(scoped.files["src/a.c"].functions.keys()) == ["add"]
        assert scoped.func_total == 1 and scoped.func_hit == 0
        # main 的分支（第10行，owner=main）应被剔除，add 没有分支 → 0/0
        assert scoped.branch_total == 0
        # main 的行 1/3/10 剔除，只留 add 区间 [7,9] 内的行（8）
        assert set(scoped.files["src/a.c"].line_counts.keys()) == {8}

    def test_scope_report_ignores_files_with_no_targets(self):
        from aicoverage.incremental import scope_report

        full = _mk_report(hit_add=True)
        scoped = scope_report(full, [("src/other.c", "whatever")])
        assert scoped.files == {}
        assert scoped.func_total == 0

    def test_missing_targets_reported_not_silently_dropped(self):
        from aicoverage.incremental import missing_targets

        full = _mk_report(hit_add=True)
        miss = missing_targets(full, [("src/a.c", "add"), ("src/a.c", "not_exist_fn")])
        assert miss == [("src/a.c", "not_exist_fn")]

    def test_incremental_delta_scoped_to_target(self):
        """全量报告里 main 一直是 100%，只有 add 从未覆盖变已覆盖；scope 收窄
        到只含 add 后，增量应该是 0%→100%（+100pp），不受 main 稀释。"""
        from aicoverage.incremental import incremental_delta

        before = _mk_report(hit_add=False)
        after = _mk_report(hit_add=True)
        d = incremental_delta(before, after, [("src/a.c", "add")])
        assert d["scope_func_pct"] == 100.0
        assert d["func_pp"] == 100.0
        assert d["scope_func_total"] == 1 and d["scope_func_hit"] == 1

    def test_scope_threshold_met(self):
        from aicoverage.incremental import scope_threshold_met

        after = _mk_report(hit_add=True)
        met, scoped = scope_threshold_met(after, [("src/a.c", "add")],
                                          func_target=100.0, cond_target=0.0)
        assert met is True and scoped.func_pct == 100.0

        before = _mk_report(hit_add=False)
        met2, _ = scope_threshold_met(before, [("src/a.c", "add")],
                                      func_target=100.0, cond_target=0.0)
        assert met2 is False


# ── config：加载与校验 ───────────────────────────────────────────

class TestConfig:
    def _write(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "aicoverage.toml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_minimal_valid(self, tmp_path):
        p = self._write(tmp_path, f"""
[project]
name = "demo"
[source]
path = "{tmp_path}"
[build]
build_cmd = "make"
binary = "app"
""")
        cfg = ProjectConfig.__new__(ProjectConfig)  # 不走 load_config 的副作用，直接测 validate
        cfg.config_path = p
        cfg.name = "demo"
        cfg.display_name = "demo"
        cfg.source_path = tmp_path
        cfg.build_cmd = "make"
        cfg.binary = Path("app")
        cfg.test_timeout = 600
        assert cfg.validate() == []
        assert cfg.binary_path == tmp_path / "app"

    def test_load_config(self, tmp_path):
        p = self._write(tmp_path, f"""
[project]
name = "demo"
language = "cpp"
[source]
path = "."
include_globs = ["src/**/*.cpp"]
[build]
build_cmd = "cmake --build ."
binary = "./bin/app"
[test]
timeout = 300
[coverage]
func_target = 90.0
""")
        import os
        old = os.environ.get("AICOV_CONFIG")
        os.environ["AICOV_CONFIG"] = str(p)
        try:
            os.chdir(tmp_path)
            from aicoverage.config import load_config
            cfg = load_config()
            assert cfg.language == "cpp"
            assert cfg.func_target == 90.0
            assert cfg.test_timeout == 300
            assert cfg.effective_gen_model == cfg.model
        finally:
            if old is None:
                os.environ.pop("AICOV_CONFIG", None)
            else:
                os.environ["AICOV_CONFIG"] = old

    def test_validate_catches_errors(self, tmp_path):
        cfg = ProjectConfig.__new__(ProjectConfig)
        cfg.config_path = tmp_path / "x.toml"
        cfg.name = "demo"; cfg.display_name = "demo"
        cfg.source_path = tmp_path / "nope"
        cfg.build_cmd = ""
        cfg.binary = None
        cfg.test_timeout = 0
        errors = cfg.validate()
        assert any("source.path" in e for e in errors)
        assert any("build_cmd" in e for e in errors)
        assert any("binary" in e for e in errors)
        assert any("timeout" in e for e in errors)


# ── executor：junit 解析与 verdict 语义 ──────────────────────────

class TestExecutor:
    def test_parse_junit_suites(self, tmp_path):
        xml = """<?xml version="1.0"?>
<testsuites><testsuite name="a" tests="3" failures="1" errors="0" skipped="1">
<testcase name="t1"/><testcase name="t2"><failure/></testcase>
</testsuite></testsuites>"""
        p = tmp_path / "junit.xml"
        p.write_text(xml, encoding="utf-8")
        assert _parse_junit(p) == (3, 1, 0, 1)

    def test_parse_junit_broken(self, tmp_path):
        p = tmp_path / "junk.xml"
        p.write_text("not xml", encoding="utf-8")
        assert _parse_junit(p) == (0, 0, 0, 0)


class TestHtmlReport:
    """层级下钻式 HTML 报告（iframe 框架 + 四列指标 + 函数级行 + 源码锚点）。"""

    def _mk_src(self, tmp_path: Path) -> Path:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.c").write_text(
            "int main(void){\n  int x=1;\n  if(x){return 0;}\n  return 1;\n}\n"
            "int add(int a,int b){\n  return a+b;\n}\n",
            encoding="utf-8")
        return tmp_path

    def test_hierarchical_structure(self, tmp_path):
        from aicoverage.htmlreport import generate

        root = self._mk_src(tmp_path)
        out = tmp_path / "report"
        index = generate(_mk_report(hit_add=False), out, source_root=root,
                         project_name="demo", run_id="RUN_X",
                         extra_links={"闭环报告": "../loop_final_report.md"})

        assert index.exists() and (out / "style.css").exists()
        assert (out / "nav.html").exists()
        assert (out / "d_coverage.html").exists()
        assert (out / "d_src.html").exists()
        assert (out / "f_src_a.c.html").exists()
        assert (out / "s_src_a.c.html").exists()

        frame = index.read_text(encoding="utf-8")
        assert "nav.html" in frame and "splitter" in frame and 'name="right"' in frame

    def test_four_metric_columns(self, tmp_path):
        from aicoverage.htmlreport import generate

        root = self._mk_src(tmp_path)
        out = tmp_path / "r"
        generate(_mk_report(hit_add=False), out, source_root=root)
        page = (out / "d_coverage.html").read_text(encoding="utf-8")
        for col in ("Function<br>coverage", "Uncovered<br>functions",
                    "Condition/decision<br>coverage",
                    "Uncovered<br>conditions/decisions"):
            assert col in page, f"缺少列: {col}"
        assert "50%" in page

    def test_function_level_rows(self, tmp_path):
        from aicoverage.htmlreport import generate

        root = self._mk_src(tmp_path)
        out = tmp_path / "r"
        generate(_mk_report(hit_add=False), out, source_root=root)
        page = (out / "f_src_a.c.html").read_text(encoding="utf-8")

        assert "s_src_a.c.html#fn_1" in page and "main" in page
        assert "s_src_a.c.html#fn_7" in page and "add" in page
        assert "&#10004;" in page and "&#10008;" in page
        assert "exec 1" in page

    def test_source_page_marks(self, tmp_path):
        from aicoverage.htmlreport import generate

        root = self._mk_src(tmp_path)
        out = tmp_path / "r"
        generate(_mk_report(hit_add=False), out, source_root=root)
        page = (out / "s_src_a.c.html").read_text(encoding="utf-8")

        assert 'id="fn_1"' in page and 'id="fn_7"' in page
        assert 'class="fnhit"' in page and 'class="fnmiss"' in page
        assert 'class="tf"' in page and 'class="tfmiss"' in page
        assert "row hit" in page and "row miss" in page

    def test_html_escapes_source(self, tmp_path):
        from aicoverage.htmlreport import generate

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.c").write_text('char *s = "<script>alert(1)</script>";\n',
                                 encoding="utf-8")
        out = tmp_path / "r2"
        generate(_mk_report(hit_add=True), out, source_root=tmp_path)
        page = (out / "s_src_a.c.html").read_text(encoding="utf-8")
        assert "&lt;script&gt;" in page
        assert "<script>alert(1)</script>" not in page

    def test_missing_source_file_is_tolerated(self, tmp_path):
        from aicoverage.htmlreport import generate

        out = tmp_path / "r3"
        index = generate(_mk_report(hit_add=True), out, source_root=tmp_path)
        assert index.exists()
        page = (out / "s_src_a.c.html").read_text(encoding="utf-8")
        assert "源文件不可读" in page


class TestFinalReport:
    def _setup_run(self, tmp_path: Path):
        """构造一个最小但完整的 run 目录 + 项目配置。"""
        src = tmp_path / "proj"
        (src / "src").mkdir(parents=True)
        (src / "src" / "a.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
        tests = src / "tests"
        (tests / "lib").mkdir(parents=True)
        (tests / "test_demo.py").write_text(
            "def test_alpha():\n    pass\n\ndef test_beta():\n    pass\n", encoding="utf-8")

        cfg = ProjectConfig.__new__(ProjectConfig)
        cfg.config_path = src / "aicoverage.toml"
        cfg.name = "proj"; cfg.display_name = "proj"
        cfg.source_path = src
        cfg.build_cmd = "make"; cfg.binary = Path("app")
        cfg.test_dirname = "tests"; cfg.test_timeout = 60

        runs = src / ".aicoverage" / "runs"
        run_id = "LOOP_TEST"
        run_dir = runs / run_id
        it1 = run_dir / "iter_1"
        it1.mkdir(parents=True)

        cov_before = _mk_report(hit_add=False)
        cov_before.save(run_dir / "baseline_coverage.json")
        # iter_1 覆盖率提升但 add 仍未覆盖（保证"未覆盖原因"章节有内容可报）
        cov_after = _mk_report(hit_add=False)
        cov_after.files["src/a.c"].branches[1].count = 1   # 分支覆盖提升
        cov_after.save(it1 / "coverage.json")

        (it1 / "junit.xml").write_text(
            '<testsuites><testsuite name="t" tests="2" failures="1" errors="0" '
            'skipped="0" time="1.5">'
            '<testcase classname="tests.test_demo" name="test_alpha"/>'
            '<testcase classname="tests.test_demo" name="test_beta">'
            '<failure message="AssertionError: boom"/></testcase>'
            "</testsuite></testsuites>", encoding="utf-8")
        (it1 / "execution.json").write_text(json.dumps(
            {"verdict": "FAIL", "tests": 2, "failures": 1, "errors": 0,
             "skipped": 0, "duration_s": 1.5}), encoding="utf-8")
        (it1 / "manifest.json").write_text(json.dumps(
            {"batch_id": "gen_iter1", "test_files": ["test_demo.py"],
             "new_functions": ["test_alpha", "test_beta"], "modified_files": [],
             "summary": "s"}), encoding="utf-8")
        (it1 / "verify_report.json").write_text(json.dumps(
            {"verdict": "pass", "problems": [{"severity": "warn", "detail": "x"}]}),
            encoding="utf-8")
        (it1 / "gap_items.json").write_text(json.dumps(
            {"total_uncovered": 1,
             "items": [{"file": "src/a.c", "function": "add", "start_line": 7,
                        "cause": "N4", "priority": "P0",
                        "evidence": "需要构造 x>0 输入", "suggestion": "传 -t1"}],
             "noise": []}), encoding="utf-8")
        (it1 / "quality_report.json").write_text(json.dumps(
            {"verdict": "fail", "metrics": {"tests": 2},
             "failures": [{"test": "tests.test_demo::test_beta", "kind": "case_bug",
                           "evidence": "src/a.c:3 恒返回 0", "action": "modify_case",
                           "suggestion": "改断言"}],
             "action_items": [{"type": "report_bug", "file": "src/a.c",
                               "suggestion": "疑似缺陷 X"}]}), encoding="utf-8")

        state = {
            "run_id": run_id, "status": "early_stop", "exit_reason": "max_iter_reached",
            "requirement": "覆盖 add 分支",
            "thresholds": {"func_pct": 100.0, "cond_pct": 85.0},
            "iterations": [{
                "iter": 1, "gen_output": "ok", "execute_verdict": "FAIL",
                "coverage_after": {"func_pct": 50.0, "cond_pct": 100.0,
                                   "func_hit": 1, "func_total": 2,
                                   "branch_hit": 2, "branch_total": 2},
                "delta": {"func_pp": 0.0, "cond_pp": 50.0, "newly_hit": 0},
            }],
            "final_metrics": {},
        }
        return cfg, runs, run_id, state

    def test_report_has_all_sections(self, tmp_path):
        from aicoverage.finalreport import write_final_report

        cfg, runs, run_id, state = self._setup_run(tmp_path)
        out = runs / run_id / "loop_final_report.md"
        html = cfg.source_path / ".aicoverage" / "reports" / f"coverage_{run_id}" / "index.html"
        write_final_report(cfg, runs, run_id, state, out, html_index=html)
        md = out.read_text(encoding="utf-8")

        # 六大章节齐备
        for section in ("## 1. 每轮覆盖率增量", "## 2. 用例执行结果", "## 3. 用例清单",
                       "## 4. 未覆盖函数与原因", "## 5. 疑似产品缺陷", "## 6. 产物索引"):
            assert section in md, f"缺少章节: {section}"

        # 增量：Δ 与新命中函数数
        assert "+50.00pp" in md
        assert "| 1 | 有新用例 | FAIL |" in md
        # 执行结果：统计 + 失败归因（来自 junit + quality_report）
        assert "test_beta" in md and "AssertionError: boom" in md
        assert "case_bug" in md and "改断言" in md

        # 用例清单：磁盘实测函数 + 溯源轮次
        assert "test_demo.py" in md and "test_alpha" in md
        assert "iter 1 新建" in md

        # 未覆盖原因：根因编码 + 证据 + 建议
        assert "N4" in md and "需要构造 x>0 输入" in md and "传 -t1" in md

        # 疑似缺陷 + HTML 地址
        assert "疑似缺陷 X" in md
        assert str(html) in md
        assert "http.server" in md

    def test_report_handles_missing_artifacts(self, tmp_path):
        """产物缺失（无 junit/gap/quality）时不抛异常，仍生成可读报告。"""
        from aicoverage.finalreport import write_final_report

        cfg, runs, run_id, state = self._setup_run(tmp_path)
        for name in ("junit.xml", "gap_items.json", "quality_report.json",
                     "execution.json", "verify_report.json"):
            (runs / run_id / "iter_1" / name).unlink()
        out = runs / run_id / "report.md"
        write_final_report(cfg, runs, run_id, state, out, html_index=None)
        md = out.read_text(encoding="utf-8")
        assert "## 1. 每轮覆盖率增量" in md
        assert "## 4. 未覆盖函数与原因" in md
        # 没有根因数据时给出明确占位，而不是编造
        assert "本轮未产出根因分析" in md

    def test_five_required_elements_always_present(self, tmp_path):
        """守卫：五项必备内容（每轮增量/执行结果/用例清单/未覆盖原因/HTML地址）
        必须始终出现在报告中，且章节编号连续不跳号。
        """
        from aicoverage.finalreport import write_final_report

        cfg, runs, run_id, state = self._setup_run(tmp_path)
        out = runs / run_id / "r.md"
        html = cfg.source_path / ".aicoverage" / "reports" / "c" / "index.html"
        write_final_report(cfg, runs, run_id, state, out, html_index=html)
        md = out.read_text(encoding="utf-8")

        # ① 每轮增量：表头 + Δpp + 新命中函数列
        assert "每轮覆盖率增量" in md
        assert "Δ函数" in md and "Δ分支" in md and "本轮新命中函数" in md
        assert "pp" in md
        # ② 执行结果：统计表头
        assert "用例执行结果" in md and "verdict" in md and "耗时(s)" in md
        # ③ 用例清单
        assert "用例清单" in md and "test_demo.py" in md
        # ④ 未覆盖原因：根因表头
        assert "未覆盖函数与原因" in md and "原因/证据" in md
        # ⑤ HTML 地址 + 打开方式
        assert str(html) in md and "http.server" in md

        # 章节编号必须连续（1,2,3,...）
        import re
        nums = [int(n) for n in re.findall(r"^## (\d+)\. ", md, re.MULTILINE)]
        assert nums == list(range(1, len(nums) + 1)), f"章节编号不连续: {nums}"

    def test_five_elements_survive_empty_artifacts(self, tmp_path):
        """守卫：即使执行/覆盖率/根因产物全部缺失，五项章节仍在（给出占位说明）。"""
        from aicoverage.finalreport import write_final_report

        cfg, runs, run_id, state = self._setup_run(tmp_path)
        it1 = runs / run_id / "iter_1"
        for name in ("junit.xml", "execution.json", "coverage.json",
                     "gap_items.json", "quality_report.json", "verify_report.json"):
            f = it1 / name
            if f.exists():
                f.unlink()
        (runs / run_id / "baseline_coverage.json").unlink()
        out = runs / run_id / "r2.md"
        write_final_report(cfg, runs, run_id, state, out, html_index=None)
        md = out.read_text(encoding="utf-8")

        assert "每轮覆盖率增量" in md
        assert "用例执行结果" in md and "未执行" in md
        assert "用例清单" in md
        assert "未覆盖函数与原因" in md and "未采集到覆盖率数据" in md
        # HTML 缺失时也要明确说明，而不是静默消失
        assert "HTML 覆盖率报告" in md and "本次未生成" in md

        import re
        nums = [int(n) for n in re.findall(r"^## (\d+)\. ", md, re.MULTILINE)]
        assert nums == list(range(1, len(nums) + 1)), f"章节编号不连续: {nums}"

    def test_table_cell_escaping(self):
        from aicoverage.finalreport import _cell

        assert _cell("a|b") == "a\\|b"
        assert _cell("x\n  y\tz") == "x y z"
        assert _cell("x" * 300).endswith("…")


# ── docstyle：用例文档头确定性门禁（描述 + 测试点）────────────────

class TestDocstyle:
    def test_full_fields_pass(self, tmp_path):
        """两个字段齐全 → 无违规。"""
        from aicoverage.docstyle import check_file

        f = tmp_path / "test_ok.py"
        f.write_text(
            'def test_alpha(target):\n'
            '    """\n'
            '    描述：验证非法参数被拒绝\n'
            '    测试点：main.c:10 parse_args 校验失败分支\n'
            '    """\n'
            '    pass\n',
            encoding="utf-8")
        assert check_file(f) == []

    def test_missing_both_fields(self, tmp_path):
        """完全没有 docstring → 报告缺失两个字段。"""
        from aicoverage.docstyle import check_file, EC_MISSING_DOC

        f = tmp_path / "test_bad.py"
        f.write_text("def test_alpha(target):\n    pass\n", encoding="utf-8")
        problems = check_file(f)
        assert len(problems) == 1
        p = problems[0]
        assert p["ec"] == EC_MISSING_DOC and p["severity"] == "error"
        assert p["function"] == "test_alpha"
        assert "描述" in p["detail"] and "测试点" in p["detail"]
        assert "完全没有 docstring" in p["detail"]

    def test_missing_one_field(self, tmp_path):
        """只有笼统一句话，既不是"描述："也不是"测试点：" → 两个字段都算缺失。"""
        from aicoverage.docstyle import check_file

        f = tmp_path / "test_partial.py"
        f.write_text(
            'def test_alpha(target):\n'
            '    """测试非法参数"""\n'
            '    pass\n',
            encoding="utf-8")
        problems = check_file(f)
        assert len(problems) == 1
        assert "描述" in problems[0]["detail"] and "测试点" in problems[0]["detail"]

        f2 = tmp_path / "test_partial2.py"
        f2.write_text(
            'def test_beta(target):\n'
            '    """\n'
            '    描述：验证非法参数被拒绝\n'
            '    """\n'
            '    pass\n',
            encoding="utf-8")
        problems2 = check_file(f2)
        assert len(problems2) == 1
        assert "测试点" in problems2[0]["detail"]
        assert "描述" not in problems2[0]["detail"]  # 描述字段已有，不应被误判缺失

    def test_half_width_colon_and_english_alias_accepted(self, tmp_path):
        """半角冒号、英文别名（Description/Test Point）也应被接受。"""
        from aicoverage.docstyle import check_file

        f = tmp_path / "test_alias.py"
        f.write_text(
            'def test_alpha(target):\n'
            '    """\n'
            '    Description: verify invalid arg rejected\n'
            '    Test Point: main.c:10 branch\n'
            '    """\n'
            '    pass\n',
            encoding="utf-8")
        assert check_file(f) == []

        f2 = tmp_path / "test_halfwidth.py"
        f2.write_text(
            'def test_beta(target):\n'
            '    """\n'
            '    描述:验证非法参数被拒绝\n'
            '    测试点:main.c:10\n'
            '    """\n'
            '    pass\n',
            encoding="utf-8")
        assert check_file(f2) == []

    def test_ignores_non_test_functions(self, tmp_path):
        """普通函数/私有辅助函数（不以 test_ 开头）不受此检查约束。"""
        from aicoverage.docstyle import check_file

        f = tmp_path / "test_helper.py"
        f.write_text(
            'def _make_case(x):\n'
            '    pass\n\n'
            'def test_ok(target):\n'
            '    """\n'
            '    描述：d\n'
            '    测试点：p\n'
            '    """\n'
            '    _make_case(1)\n',
            encoding="utf-8")
        assert check_file(f) == []

    def test_syntax_error_reported_not_silently_skipped(self, tmp_path):
        """文件语法错误也要报出来，不能悄悄跳过（否则坏文件会绕过审查）。"""
        from aicoverage.docstyle import check_file, EC_MISSING_DOC

        f = tmp_path / "test_broken.py"
        f.write_text("def test_alpha(target)\n    pass\n", encoding="utf-8")  # 缺冒号
        problems = check_file(f)
        assert len(problems) == 1
        assert problems[0]["ec"] == EC_MISSING_DOC and problems[0]["severity"] == "error"

    def test_check_test_docstrings_scans_dir_or_filenames(self, tmp_path):
        """check_test_docstrings：不传 filenames 时扫全目录；传了只查指定文件。"""
        from aicoverage.docstyle import check_test_docstrings

        good = (
            'def test_ok(target):\n'
            '    """\n    描述：d\n    测试点：p\n    """\n    pass\n')
        bad = 'def test_bad(target):\n    pass\n'
        (tmp_path / "test_a.py").write_text(good, encoding="utf-8")
        (tmp_path / "test_b.py").write_text(bad, encoding="utf-8")

        all_problems = check_test_docstrings(tmp_path)
        assert len(all_problems) == 1 and all_problems[0]["file"] == "test_b.py"

        only_good = check_test_docstrings(tmp_path, ["test_a.py"])
        assert only_good == []

    def test_generated_harness_template_files_are_exempt_by_naming(self, tmp_path):
        """harness.py/conftest.py 不叫 test_*.py，天然不受此检查约束（无需特殊豁免逻辑）。"""
        from aicoverage.docstyle import check_test_docstrings

        (tmp_path / "conftest.py").write_text("def test_fixture_helper():\n    pass\n",
                                               encoding="utf-8")
        assert check_test_docstrings(tmp_path) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
