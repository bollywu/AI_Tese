"""三项立项的配套单测：
  - rust_cover：lcov 解析 → CoverageReport（函数/行/分支）
  - java_cover：jacoco.xml 解析 → CoverageReport（方法/行/分支）
  - config：rust/java 语言支持（后缀、validate 豁免、TOML 解析、新字段）
  - executor：_parse_cargo_test_output / _parse_java_test_output / _rust_env_blocked
  - history：append/load/render（JSONL 容错）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig, load_config  # noqa: E402


# ── rust_cover（lcov 解析）────────────────────────────────────────────

_LCOV_SAMPLE = """TN:
SF:src/main.rs
FN:3,main
FN:10,helper
FNDA:1,main
FNDA:0,helper
FNF:2
FNH:1
DA:3,1
DA:4,1
DA:10,0
BRDA:4,0,0,1
BRDA:4,0,1,0
end_of_record
SF:src/lib.rs
FN:5,compute
FNDA:7,compute
DA:5,7
DA:6,7
end_of_record
"""


class TestRustCover:
    def _write(self, tmp_path: Path) -> Path:
        src = tmp_path / "proj"
        (src / "src").mkdir(parents=True)
        (src / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        (src / "src" / "lib.rs").write_text("pub fn compute() {}\n", encoding="utf-8")
        p = src / "lcov.info"
        p.write_text(_LCOV_SAMPLE, encoding="utf-8")
        return src

    def test_parse_lcov(self, tmp_path):
        from aicoverage.rust_cover import parse_lcov
        files = parse_lcov(self._write(tmp_path) / "lcov.info")
        assert len(files) == 2
        assert files[0].fn_defs == {"main": 3, "helper": 10}
        assert files[0].fn_hits == {"main": 1, "helper": 0}
        assert files[0].branches == {(4, 0, 0): 1, (4, 0, 1): 0}

    def test_collect_rust_functions_lines_branches(self, tmp_path):
        from aicoverage.rust_cover import collect_rust
        src = self._write(tmp_path)
        rep = collect_rust(src, src / "lcov.info")
        assert set(rep.files) == {"src/main.rs", "src/lib.rs"}
        main = rep.files["src/main.rs"]
        assert main.functions["main"].execution_count == 1
        assert main.functions["main"].hit
        assert not main.functions["helper"].hit
        # 函数覆盖 2/3（main+compute 命中，helper 未命中）
        assert rep.func_total == 3 and rep.func_hit == 2
        assert rep.func_pct == pytest.approx(66.67)
        # 行覆盖
        assert main.lines_total == 3 and main.lines_hit == 2
        # 分支：BRDA taken=1 命中、taken=0 未命中
        assert rep.branch_total == 2 and rep.branch_hit == 1
        assert rep.cond_pct == 50.0

    def test_exclude_filter(self, tmp_path):
        from aicoverage.rust_cover import collect_rust
        src = self._write(tmp_path)
        rep = collect_rust(src, src / "lcov.info", exclude_filter=["**/lib.rs"])
        assert set(rep.files) == {"src/main.rs"}

    def test_missing_file_empty(self, tmp_path):
        from aicoverage.rust_cover import parse_lcov
        assert parse_lcov(tmp_path / "ghost.info") == []


# ── java_cover（jacoco XML 解析）──────────────────────────────────────

_JACOCO_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<report name="app">
  <package name="com/example">
    <sourcefile name="App.java">
      <line nr="10" ci="2" mi="0" cb="1" mb="0"/>
      <line nr="11" ci="0" mi="3" cb="0" mb="1"/>
      <method name="main" desc="([Ljava/lang/String;)V" line="10">
        <counter type="INSTRUCTION" covered="5" missed="0"/>
      </method>
      <method name="helper" desc="()V" line="11">
        <counter type="INSTRUCTION" covered="0" missed="3"/>
      </method>
      <counter type="METHOD" covered="1" missed="1"/>
    </sourcefile>
  </package>
</report>
"""


class TestJavaCover:
    def _write(self, tmp_path: Path) -> Path:
        src = tmp_path / "proj"
        (src / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
        (src / "src" / "main" / "java" / "com" / "example" / "App.java").write_text(
            "public class App {}\n", encoding="utf-8")
        p = src / "target" / "site" / "jacoco" / "jacoco.xml"
        p.parent.mkdir(parents=True)
        p.write_text(_JACOCO_SAMPLE, encoding="utf-8")
        return src

    def test_parse_jacoco(self, tmp_path):
        from aicoverage.java_cover import parse_jacoco_xml
        files = parse_jacoco_xml(self._write(tmp_path) / "target/site/jacoco/jacoco.xml")
        assert len(files) == 1
        assert files[0].path == "com/example/App.java"
        assert len(files[0].methods) == 2
        assert files[0].lines == {10: (2, 0, 1, 0), 11: (0, 3, 0, 1)}

    def test_collect_java_methods_lines_branches(self, tmp_path):
        from aicoverage.java_cover import collect_java
        src = self._write(tmp_path)
        rep = collect_java(src, src / "target/site/jacoco/jacoco.xml")
        fc = rep.files["com/example/App.java"]
        assert fc.functions["main"].hit          # covered instr > 0
        assert not fc.functions["helper"].hit
        assert rep.func_pct == 50.0
        assert fc.lines_hit == 1 and fc.lines_total == 2
        # cb=1 covered + mb=1 missed on the two lines → 2 branches, 1 hit
        assert rep.branch_total == 2 and rep.branch_hit == 1

    def test_corrupt_xml_empty(self, tmp_path):
        from aicoverage.java_cover import parse_jacoco_xml
        p = tmp_path / "bad.xml"
        p.write_text("not xml", encoding="utf-8")
        assert parse_jacoco_xml(p) == []


# ── config：rust/java 语言支持 ────────────────────────────────────────

class TestConfigMultilang:
    def test_rust_config_roundtrip(self, tmp_path):
        root = tmp_path / "rproj"
        root.mkdir()
        (root / "aicoverage.toml").write_text(
            '[project]\nname="r"\nlanguage="rust"\n[source]\npath="."\n'
            '[rust]\ncov_tool="tarpaulin"\nlcov=".aicoverage/out.info"\n',
            encoding="utf-8")
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.language == "rust"
        assert cfg.rust_cov_tool == "tarpaulin"
        assert cfg.lcov.name == "out.info"
        assert cfg.validate() == []          # 无需 build_cmd/binary
        assert cfg.include_globs == ["**/*.rs"]

    def test_java_config_roundtrip(self, tmp_path):
        root = tmp_path / "jproj"
        root.mkdir()
        (root / "aicoverage.toml").write_text(
            '[project]\nname="j"\nlanguage="java"\n[source]\npath="."\n'
            '[java]\nbuild_tool="gradle"\n',
            encoding="utf-8")
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.language == "java"
        assert cfg.java_build_tool == "gradle"
        assert cfg.validate() == []
        assert cfg.include_globs == ["**/*.java"]

    def test_invalid_language_rejected(self, tmp_path):
        root = tmp_path / "xproj"
        root.mkdir()
        (root / "aicoverage.toml").write_text(
            '[project]\nname="x"\nlanguage="python"\n[source]\npath="."\n',
            encoding="utf-8")
        with pytest.raises(SystemExit):
            load_config(str(root / "aicoverage.toml"))

    def test_invalid_rust_cov_tool_rejected(self, tmp_path):
        root = tmp_path / "rproj2"
        root.mkdir()
        (root / "aicoverage.toml").write_text(
            '[project]\nname="r"\nlanguage="rust"\n[source]\npath="."\n'
            '[rust]\ncov_tool="bogus"\n', encoding="utf-8")
        with pytest.raises(SystemExit):
            load_config(str(root / "aicoverage.toml"))

    def test_source_files_suffix_dispatch(self, tmp_path):
        from aicoverage.config import suffixes_for
        assert suffixes_for("rust") == {".rs"}
        assert suffixes_for("java") == {".java"}
        assert suffixes_for("go") == {".go"}
        assert ".c" in suffixes_for("cpp")

    def test_env_injection(self, tmp_path):
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="rust")
        env = cfg.to_env()
        assert env["AICOV_RUST_COV_TOOL"] == "llvm-cov"
        assert "lcov.info" in env["AICOV_LCOV"]


# ── executor 输出解析 ────────────────────────────────────────────────

class TestCargoJavaOutputParsers:
    def test_parse_cargo_multi_target(self):
        from aicoverage.executor import _parse_cargo_test_output
        log = ("running 3 tests\n...\ntest result: ok. 3 passed; 0 failed; "
               "0 ignored; 0 measured; 0 filtered out\n"
               "running 2 tests\ntest result: FAILED. 1 passed; 1 failed\n")
        assert _parse_cargo_test_output(log) == (4, 1)

    def test_parse_java_surefire_summary(self):
        from aicoverage.executor import _parse_java_test_output
        log = ("Tests run: 2, Failures: 0, Errors: 0\n"   # per-class
               "[INFO] Results:\nTests run: 5, Failures: 1, Errors: 1, Skipped: 0\n")
        assert _parse_java_test_output(log) == (3, 2)      # 取最后一条汇总

    def test_rust_env_blocked_markers(self):
        from aicoverage.executor import _rust_env_blocked
        assert _rust_env_blocked("error[E0433]: failed to resolve")
        assert _rust_env_blocked("error: could not compile `app`")
        assert not _rust_env_blocked("test result: ok. 3 passed")


# ── history：跨 run 覆盖历史 ──────────────────────────────────────────

class TestHistory:
    def test_append_load_roundtrip(self, tmp_path):
        from aicoverage.history import append_history, load_history
        append_history(tmp_path, {"run_id": "LOOP_1", "trigger": "manual",
                                  "status": "done", "exit_reason": "threshold_met",
                                  "func_pct": 45.0, "cond_pct": 20.0})
        append_history(tmp_path, {"run_id": "LOOP_2", "trigger": "manual",
                                  "status": "done", "exit_reason": "threshold_met",
                                  "func_pct": 80.0, "cond_pct": 55.0})
        entries = load_history(tmp_path)
        assert len(entries) == 2
        assert entries[0]["run_id"] == "LOOP_1"
        assert entries[1]["func_pct"] == 80.0
        assert "ts" in entries[0]

    def test_corrupt_lines_skipped(self, tmp_path):
        from aicoverage.history import load_history
        p = tmp_path / "history.jsonl"
        p.write_text('{"run_id": "A"}\nnot-json\n{"run_id": "B"}\n', encoding="utf-8")
        entries = load_history(tmp_path)
        assert [e["run_id"] for e in entries] == ["A", "B"]

    def test_render_trend(self, tmp_path):
        from aicoverage.history import load_history, render_history
        from aicoverage.history import append_history
        append_history(tmp_path, {"run_id": "LOOP_1", "trigger": "manual",
                                  "status": "done", "exit_reason": "threshold_met",
                                  "func_pct": 45.0, "cond_pct": 20.0})
        append_history(tmp_path, {"run_id": "LOOP_2", "trigger": "manual",
                                  "status": "done", "exit_reason": "threshold_met",
                                  "func_pct": 80.0, "cond_pct": 55.0})
        md = render_history(load_history(tmp_path))
        assert "LOOP_1" in md and "80.00%" in md
        assert "+35.00pp" in md                    # 演进摘要
        assert "共 2 次 run" in md

    def test_empty_history(self, tmp_path):
        from aicoverage.history import load_history, render_history
        assert load_history(tmp_path) == []
        assert "无历史记录" in render_history([])


# ── xdist / workers 配置 ─────────────────────────────────────────────

class TestWorkersConfig:
    def test_workers_parsed(self, tmp_path):
        root = tmp_path / "wproj"
        root.mkdir()
        (root / "aicoverage.toml").write_text(
            '[project]\nname="w"\nlanguage="c"\n[source]\npath="."\n'
            '[build]\nbuild_cmd="make --coverage"\nbinary="./app"\n'
            '[test]\nworkers=-1\n', encoding="utf-8")
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.workers == -1

    def test_workers_default_off(self, tmp_path):
        cfg = ProjectConfig.minimal(tmp_path, name="p")
        assert cfg.workers == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
