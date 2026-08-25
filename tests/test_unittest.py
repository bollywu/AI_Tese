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
