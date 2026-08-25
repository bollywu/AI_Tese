"""单测通道验证：e2e 不可达函数可通过 harness 单测原子函数直接调用覆盖。

用真实 gcc --coverage 走完整链路，验证：
  1. config 能解析 [unittest] 段并注入 AICOV_UT_* 环境变量
  2. harness.compile_unit_driver 能以 --coverage 插桩编译 driver + 被测源
  3. run_driver 运行后 gcov 能采集到目标函数命中（gcov 按源码树扫 .gcno/.gcda，天然兼容）
"""
from __future__ import annotations

import os
import shutil
import subprocess as sp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import load_config  # noqa: E402


# ── 真实编译链路（无 gcc 则跳过）────────────────────────────────

@pytest.fixture()
def ut_project(tmp_path, monkeypatch):
    """构造一个最小 C 项目 + aicoverage.toml + tests/lib/harness.py + driver。"""
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("本机无 gcc，跳过单测通道真实编译验证")

    src_root = tmp_path / "proj"
    (src_root / "src").mkdir(parents=True)
    (src_root / "tests" / "drivers").mkdir(parents=True)
    (src_root / "tests" / "lib").mkdir(parents=True)

    # 被测源：parse_opt 有正常 + 错误路径，错误路径 E2E 难触达
    (src_root / "src" / "parse.c").write_text(
        "int parse_opt(const char *s){\n"
        "  if(!s) return -1;          /* 错误路径 N3，E2E 难触达 */\n"
        "  if(s[0]=='h') return 1;    /* 正常路径 */\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8")

    # driver：直接调用 parse_opt，argv[1] 决定走哪个分支
    (src_root / "tests" / "drivers" / "test_driver_parse.c").write_text(
        '#include <stdio.h>\n'
        'int parse_opt(const char *s);\n'
        'int main(int argc, char **argv){\n'
        '  const char *s = (argc > 1) ? argv[1] : NULL;\n'
        '  int r = parse_opt(s);\n'
        '  printf("err=%d\\n", r);\n'
        '  return 0;\n'
        '}\n',
        encoding="utf-8")

    # harness.py（复用项目真实模板，保证原子函数一致）
    from aicoverage.templates import HARNESS_TEMPLATE
    (src_root / "tests" / "lib" / "harness.py").write_text(HARNESS_TEMPLATE, encoding="utf-8")
    (src_root / "tests" / "lib" / "__init__.py").write_text("", encoding="utf-8")

    # aicoverage.toml（含 [unittest]）
    (src_root / "aicoverage.toml").write_text(
        f'[project]\nname="utdemo"\nlanguage="c"\n'
        f'[source]\npath="."\ninclude_globs=["src/**/*.c"]\n'
        f'[build]\nbuild_cmd="gcc --coverage -O0 -g src/parse.c -o app"\nbinary="app"\n'
        f'[test]\ndir="tests"\n'
        f'[unittest]\ncompiler="gcc"\nflags=["-O0","-g","-Wall"]\nlink_libs=[]\n'
        f'obj_dir=".aicoverage/ut"\n',
        encoding="utf-8")

    cfg = load_config(str(src_root / "aicoverage.toml"))
    return cfg


class TestUnittestConfig:
    def test_parses_unittest_section(self, ut_project):
        cfg = ut_project
        assert cfg.ut_compiler == "gcc"
        assert cfg.ut_flags == ["-O0", "-g", "-Wall"]
        assert cfg.ut_link_libs == []
        assert str(cfg.ut_obj_dir).endswith(".aicoverage/ut")

    def test_to_env_injects_ut_vars(self, ut_project):
        cfg = ut_project
        env = cfg.to_env()
        assert env["AICOV_UT_OBJ_DIR"] == str(cfg.ut_obj_path)
        assert env["AICOV_UT_COMPILER"] == "gcc"
        assert "Wall" in env["AICOV_UT_FLAGS"]


class TestUnittestChannel:
    def test_driver_compile_run_and_gcov_hit(self, ut_project, monkeypatch):
        """端到端：编译 driver → 运行 → gcov 采集命中 parse_opt（含错误路径）。"""
        cfg = ut_project
        monkeypatch.chdir(cfg.source_path)
        # 注入 harness 需要的环境变量（loop.py 会这么做）
        env = cfg.to_env()
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        sys.path.insert(0, str(cfg.tests_lib_dir))
        import harness

        # 1. 编译 driver + 被测源
        cres = harness.compile_unit_driver(
            "tests/drivers/test_driver_parse.c",
            sources=["src/parse.c"], out_name="ut_parse", include_dirs=["src"],
        )
        harness.assert_ut_compiled(cres)
        ut_bin = cfg.ut_obj_path / "ut_parse"
        assert ut_bin.exists(), f"单测二进制未生成: {ut_bin}"
        assert len(list(cfg.ut_obj_path.glob("*.gcno"))) >= 1, "未生成 .gcno（插桩没生效）"

        # 2. 运行 driver（argv[1]=NULL 走错误路径 N3）
        r = harness.run_driver("ut_parse", args=[])
        harness.assert_exit_code(r, 0)
        harness.assert_stdout_contains(r, "err=-1")
        # .gcda 名跟源文件（parse.gcda），不跟 -o 产物名
        assert list(cfg.ut_obj_path.glob("*.gcda")), "运行后未生成 .gcda"

        # 3. gcov 采集（clean 掉主程序产物，避免干扰；只统计 src/parse.c）
        from aicoverage.gcov import clean_gcda, collect
        clean_gcda(cfg.source_path)
        # 单测刚产生的 .gcda 在 ut 目录，重新跑一次单测以产生计数
        harness.run_driver("ut_parse", args=[])
        report = collect(cfg.source_path, "gcov", include_filter=["src/**/*.c"],
                         exclude_filter=["tests/**"])
        parse_opt = next((f for f in report.functions if f.name == "parse_opt"), None)
        assert parse_opt is not None, f"未找到 parse_opt；files={list(report.files)}"
        assert parse_opt.hit, (
            f"parse_opt 被单测 driver 直接调用过，但 gcov 报告未命中——"
            f"单测通道未真正写入覆盖")
        assert parse_opt.execution_count > 0
        assert report.func_pct == 100.0, f"parse_opt 应 100% 命中，实际 {report.func_pct}%"

    def test_missing_driver_binary_returns_rc127(self, ut_project, monkeypatch):
        """未先编译就 run_driver 应明确报错（而不是静默）。"""
        cfg = ut_project
        monkeypatch.setenv("AICOV_UT_OBJ_DIR", str(cfg.ut_obj_path))
        sys.path.insert(0, str(cfg.tests_lib_dir))
        import harness
        r = harness.run_driver("ut_nonexistent")
        assert r.rc == 127
        assert "不存在" in r.stderr


# ── P0/P2：编译失败自愈 + driver 崩溃处理 ─────────────────────────

class TestUnittestRobustness:
    def test_link_lib_auto_probe_recovers_math_symbols(self, tmp_path, monkeypatch):
        """编译失败且 undefined reference 时自动补 -lm 等常见库（wrk 冒烟真实场景）。"""
        gcc = shutil.which("gcc")
        if not gcc:
            pytest.skip("本机无 gcc")
        from aicoverage.templates import HARNESS_TEMPLATE
        src = tmp_path / "proj"
        (src / "src").mkdir(parents=True)
        (src / "tests" / "drivers").mkdir(parents=True)
        (src / "tests" / "lib").mkdir(parents=True)
        # 被测函数用 sqrtl（数学库符号），不配 link_libs → 靠自动探测 -lm 恢复
        (src / "src" / "calc.c").write_text(
            '#include <math.h>\nlong double dist2(long double x){\n'
            '  return x * sqrtl(x) + 1.0L;\n}\n', encoding="utf-8")
        (src / "tests" / "drivers" / "d.c").write_text(
            '#include <stdio.h>\nlong double dist2(long double);\n'
            'int main(void){ printf("%Lf\\n", dist2(4.0L)); return 0; }\n',
            encoding="utf-8")
        (src / "tests" / "lib" / "harness.py").write_text(HARNESS_TEMPLATE, encoding="utf-8")
        monkeypatch.setenv("AICOV_SRC", str(src))
        monkeypatch.setenv("AICOV_UT_OBJ_DIR", str(src / ".aicoverage" / "ut"))
        monkeypatch.setenv("AICOV_UT_COMPILER", "gcc")
        monkeypatch.setenv("AICOV_UT_FLAGS", "-O0 -g -Wall")
        monkeypatch.setenv("AICOV_UT_LINK_LIBS", "")   # 不配 → 触发自动探测
        monkeypatch.chdir(src)
        sys.path.insert(0, str(src / "tests" / "lib"))
        import harness
        import importlib
        importlib.reload(harness)   # SRC_ROOT 是模块级常量，按新 AICOV_SRC 重载
        res = harness.compile_unit_driver("tests/drivers/d.c", sources=["src/calc.c"],
                                          out_name="ut_calc")
        harness.assert_ut_compiled(res)
        assert "-lm" in res.cmd or any(a.endswith("lm") for a in res.cmd), \
            f"自动探测应追加 -lm，实际 cmd={res.cmd}"
        r = harness.run_driver("ut_calc")
        harness.assert_exit_code(r, 0)
        harness.assert_stdout_contains(r, "9.000000")

    def test_driver_crash_detected_with_signal_hint(self, tmp_path, monkeypatch):
        """driver 崩溃（段错误）时 run_driver 给出信号提示，而不是裸负 rc。"""
        gcc = shutil.which("gcc")
        if not gcc:
            pytest.skip("本机无 gcc")
        from aicoverage.templates import HARNESS_TEMPLATE
        src = tmp_path / "proj"
        (src / "src").mkdir(parents=True)
        (src / "tests" / "drivers").mkdir(parents=True)
        (src / "tests" / "lib").mkdir(parents=True)
        (src / "src" / "s.c").write_text(
            'int get(int *p){ return *p; }\n', encoding="utf-8")
        # driver 传 NULL → 段错误
        (src / "tests" / "drivers" / "crash.c").write_text(
            '#include <stdio.h>\nint get(int*);\n'
            'int main(void){ int *p = 0; printf("%d\\n", get(p)); return 0; }\n',
            encoding="utf-8")
        (src / "tests" / "lib" / "harness.py").write_text(HARNESS_TEMPLATE, encoding="utf-8")
        monkeypatch.setenv("AICOV_SRC", str(src))
        monkeypatch.setenv("AICOV_UT_OBJ_DIR", str(src / ".aicoverage" / "ut"))
        monkeypatch.setenv("AICOV_UT_COMPILER", "gcc")
        monkeypatch.setenv("AICOV_UT_FLAGS", "-O0 -g -Wall")
        monkeypatch.setenv("AICOV_UT_LINK_LIBS", "")
        monkeypatch.chdir(src)
        sys.path.insert(0, str(src / "tests" / "lib"))
        import harness
        import importlib
        importlib.reload(harness)   # SRC_ROOT 是模块级常量，按新 AICOV_SRC 重载
        res = harness.compile_unit_driver("tests/drivers/crash.c", sources=["src/s.c"],
                                          out_name="ut_crash")
        harness.assert_ut_compiled(res)
        r = harness.run_driver("ut_crash")
        assert r.rc < 0, f"driver 应崩溃（负 rc），实际 rc={r.rc}"
        assert "SIGSEGV" in r.stderr or "signal" in r.stderr.lower(), \
            f"崩溃提示应含信号名，stderr={r.stderr}"


# ── P1：配置健壮性（minimal 工厂） ───────────────────────────────

class TestUnittestConfigRobustness:
    def test_minimal_factory_fills_all_fields(self, tmp_path):
        from aicoverage.config import ProjectConfig
        cfg = ProjectConfig.minimal(tmp_path / "proj", name="demo",
                                    build_cmd="make", binary="app")
        assert cfg.source_path == (tmp_path / "proj").resolve()
        assert cfg.name == "demo"
        assert cfg.build_cmd == "make"
        assert str(cfg.binary_path).endswith("app")
        # 确定性阶段字段全部有默认值（to_env 不依赖 getattr 兜底也能工作）
        env = cfg.to_env()
        assert env["AICOV_UT_OBJ_DIR"] == str(cfg.ut_obj_path)
        assert env["AICOV_UT_FLAGS"] == "-O0 -g -Wall"
        assert cfg.include_globs  # 默认 include 非空
        assert cfg.func_target == 100.0
        assert cfg.max_iter == 6

    def test_source_files_cached(self, tmp_path):
        from aicoverage.config import ProjectConfig
        src = tmp_path / "proj"
        (src / "src").mkdir(parents=True)
        (src / "src" / "a.c").write_text("int a(){return 1;}\n", encoding="utf-8")
        cfg = ProjectConfig.minimal(src, build_cmd="make", binary="app")
        first = cfg.source_files()
        assert len(first) == 1
        # 第二次调用应命中缓存（同一实例）
        assert cfg.source_files() == first
        # 新增文件后 invalidate 应反映变化
        (src / "src" / "b.c").write_text("int b(){return 2;}\n", encoding="utf-8")
        assert cfg.source_files() == first, "未失效前缓存应保持"
        cfg.invalidate_source_files()
        assert len(cfg.source_files()) == 2


# ── P2：报告区分 E2E / 单测来源（ut_hit 标记） ───────────────────

class TestUtHitMarking:
    def test_collect_marks_ut_only_functions(self, ut_project, monkeypatch):
        """collect(ut_dir=...) 应把"仅单测覆盖、E2E 未命中"的函数标记 ut_hit。"""
        cfg = ut_project
        monkeypatch.chdir(cfg.source_path)
        env = cfg.to_env()
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sys.path.insert(0, str(cfg.tests_lib_dir))
        import harness
        import importlib
        importlib.reload(harness)   # SRC_ROOT 是模块级常量，按新 AICOV_SRC 重载
        from aicoverage.gcov import clean_gcda, collect

        # E2E：先编译一个带 main 的主程序并运行（覆盖 parse_opt 的正常路径）
        (cfg.source_path / "src" / "main.c").write_text(
            '#include <stdio.h>\nint parse_opt(const char *s);\n'
            'int main(void){ printf("r=%d\\n", parse_opt("h")); return 0; }\n',
            encoding="utf-8")
        sp.run([shutil.which("gcc"), "--coverage", "-O0", "-g",
                "src/parse.c", "src/main.c", "-o", "app"],
               check=True, capture_output=True, cwd=cfg.source_path)
        sp.run(["./app"], cwd=cfg.source_path, capture_output=True)

        # 单测：driver 覆盖 parse_opt 的 NULL 错误路径
        cres = harness.compile_unit_driver(
            "tests/drivers/test_driver_parse.c", sources=["src/parse.c"],
            out_name="ut_parse", include_dirs=["src"])
        harness.assert_ut_compiled(cres)
        harness.run_driver("ut_parse", args=[])

        # 采集时传 ut_dir → parse_opt 同时被 E2E+单测命中，不应标 ut_hit（E2E 也命中）
        report = collect(cfg.source_path, "gcov", include_filter=["src/**/*.c"],
                         exclude_filter=["tests/**"], ut_dir=cfg.ut_obj_path)
        parse_opt = next(f for f in report.functions if f.name == "parse_opt")
        assert parse_opt.hit
        assert not parse_opt.ut_hit, "E2E 也命中时不应标 ut_hit"

    def test_ut_hit_roundtrip_serialization(self, tmp_path):
        """ut_hit 字段应能序列化/反序列化往返。"""
        from aicoverage.gcov import CoverageReport, FileCov, FunctionCov
        r = CoverageReport(created_at="t")
        fc = FileCov(file="src/a.c")
        fc.functions["only_ut"] = FunctionCov("src/a.c", "only_ut", 1, 3, 5, 2, 2,
                                              ut_hit=True)
        fc.functions["both"] = FunctionCov("src/a.c", "both", 5, 7, 3, 2, 2,
                                           ut_hit=False)
        r.files["src/a.c"] = fc
        p = tmp_path / "cov.json"
        r.save(p)
        r2 = CoverageReport.load(p)
        fns = r2.files["src/a.c"].functions
        assert fns["only_ut"].ut_hit is True
        assert fns["both"].ut_hit is False
        # 旧数据（无 ut_hit 字段）也应能加载（默认 False）
        import json
        old = tmp_path / "old.json"
        old.write_text(json.dumps({
            "created_at": "t", "summary": {},
            "files": {"src/a.c": {"functions": [
                {"file": "src/a.c", "name": "legacy", "start_line": 1, "end_line": 3,
                 "execution_count": 1, "hit": True, "blocks": 2, "blocks_executed": 2}],
                "branches": [], "branch_total": 0, "branch_hit": 0,
                "lines_total": 0, "lines_hit": 0, "line_counts": {}}},
        }), encoding="utf-8")
        r3 = CoverageReport.load(old)
        assert r3.files["src/a.c"].functions["legacy"].ut_hit is False
