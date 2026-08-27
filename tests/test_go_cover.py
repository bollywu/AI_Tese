"""Go coverage backend tests: coverprofile parsing, collect_go, config, execution.

Covers the pure-standard-library Go backend (no cclog_agent-style internal module
dependency). Real `go test -coverprofile` runs are gated on a working Go toolchain
and skipped otherwise, so the suite still passes in environments without Go.
"""
from __future__ import annotations

import shutil
import subprocess as sp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig, load_config  # noqa: E402
from aicoverage.go_cover import (  # noqa: E402
    extract_go_functions, parse_coverprofile, collect_go,
)
from aicoverage.gcov import CoverageReport  # noqa: E402


# ── coverprofile parsing ─────────────────────────────────────────────

class TestParseCoverprofile:
    def test_parses_mode_and_blocks(self, tmp_path):
        cov = tmp_path / "cover.out"
        cov.write_text(
            "mode: set\n"
            "mathx/calc.go:3.24,4.11 1 1\n"
            "mathx/calc.go:7.2,7.14 1 1\n"
            "mathx/calc.go:10.24,11.12 1 0\n",
            encoding="utf-8",
        )
        blocks = parse_coverprofile(cov)
        assert len(blocks) == 3
        assert blocks[0].file == "mathx/calc.go"
        assert blocks[0].start_line == 3 and blocks[0].end_line == 4
        assert blocks[0].count == 1 and blocks[0].hit
        assert blocks[2].count == 0 and not blocks[2].hit

    def test_skips_invalid_lines(self, tmp_path):
        cov = tmp_path / "cover.out"
        cov.write_text("mode: atomic\nbogus-line\nnot-a-block\n", encoding="utf-8")
        assert parse_coverprofile(cov) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_coverprofile(tmp_path / "ghost.out") == []


# ── Go function extraction ───────────────────────────────────────────

class TestExtractGoFunctions:
    def test_plain_and_receiver(self, tmp_path):
        src = tmp_path / "srv.go"
        src.write_text(
            "package srv\n"
            "func Add(a, b int) int {\n"
            "    return a + b\n"
            "}\n"
            "func (s *Server) Start(ctx string) error {\n"
            "    if ctx == \"\" {\n"
            "        return nil\n"
            "    }\n"
            "    return nil\n"
            "}\n",
            encoding="utf-8",
        )
        funcs = extract_go_functions(src, tmp_path)
        by_name = {f.name: f for f in funcs}
        assert set(by_name) == {"Add", "(s *Server) Start"}
        assert by_name["Add"].start_line == 2
        assert by_name["Add"].end_line == 4
        assert by_name["(s *Server) Start"].start_line == 5
        assert by_name["(s *Server) Start"].end_line == 10

    def test_skips_interface_methods_and_non_func(self, tmp_path):
        src = tmp_path / "types.go"
        src.write_text(
            "package t\n"
            "type Greeter interface {\n"
            "    Hello() string\n"
            "}\n"
            "var add = func(a int) int { return a }\n"
            "func real() int { return 1 }\n",
            encoding="utf-8",
        )
        funcs = extract_go_functions(src, tmp_path)
        names = {f.name for f in funcs}
        assert names == {"real"}, f"should only find real(), got {names}"

    def test_signature_spans_lines(self, tmp_path):
        src = tmp_path / "multi.go"
        src.write_text(
            "package m\n"
            "func Long(\n"
            "    a int,\n"
            "    b string,\n"
            ") error {\n"
            "    return nil\n"
            "}\n",
            encoding="utf-8",
        )
        funcs = extract_go_functions(src, tmp_path)
        assert len(funcs) == 1
        assert funcs[0].name == "Long"
        assert funcs[0].start_line == 2
        assert funcs[0].end_line == 7


# ── collect_go (function + line + branch aggregation) ────────────────

def _mk_go_project(tmp_path) -> Path:
    """A minimal Go module with two functions; only Add is covered."""
    root = tmp_path / "proj"
    (root / "mathx").mkdir(parents=True)
    (root / "go.mod").write_text("module verify\n\ngo 1.20\n", encoding="utf-8")
    (root / "mathx" / "calc.go").write_text(
        "package mathx\n"
        "\n"
        "func Add(a, b int) int {\n"
        "\tif a < 0 {\n"
        "\t\treturn b\n"
        "\t}\n"
        "\treturn a + b\n"
        "}\n"
        "\n"
        "func Div(a, b int) int {\n"
        "\tif b == 0 {\n"
        "\t\treturn 0\n"
        "\t}\n"
        "\treturn a / b\n"
        "}\n",
        encoding="utf-8",
    )
    return root


class TestCollectGo:
    def test_function_and_line_coverage(self, tmp_path):
        root = _mk_go_project(tmp_path)
        # Statement profile: Add fully covered, Div fully uncovered (mirrors the
        # real coverprofile layout observed from `go test -coverprofile`).
        cov = tmp_path / "cover.out"
        cov.write_text(
            "mode: set\n"
            "verify/mathx/calc.go:3.24,4.11 1 1\n"
            "verify/mathx/calc.go:4.11,6.3 1 0\n"
            "verify/mathx/calc.go:7.2,7.14 1 1\n"
            "verify/mathx/calc.go:10.24,11.12 1 0\n"
            "verify/mathx/calc.go:11.12,13.3 1 0\n"
            "verify/mathx/calc.go:14.2,14.14 1 0\n",
            encoding="utf-8",
        )
        report = collect_go(root, cov, include_filter=["**/*.go"])
        assert isinstance(report, CoverageReport)
        assert report.func_total == 2, f"expected 2 funcs, got {report.func_total}"
        add = report.files["mathx/calc.go"].functions["Add"]
        div = report.files["mathx/calc.go"].functions["Div"]
        assert add.hit, "Add covered by profile should be HIT"
        assert add.execution_count > 0
        assert not div.hit, "Div uncovered by profile should be MISS"
        assert div.execution_count == 0
        assert report.func_pct == 50.0
        assert report.line_total == 10
        assert report.line_hit == 3
        assert report.line_pct == 30.0
        # Go's statement coverage carries no reliable branch data → branch_total=0
        # (loop skips the cond threshold; we don't fabricate pseudo-branches).
        assert report.branch_total == 0

    def test_module_prefix_stripped_and_glob_filter(self, tmp_path):
        """Coverprofile paths carry the module prefix (verify/mathx/...); the report
        must normalize to source-relative (mathx/...) and honor include/exclude."""
        root = _mk_go_project(tmp_path)
        cov = tmp_path / "cover.out"
        cov.write_text(
            "mode: set\n"
            "verify/mathx/calc.go:3.24,4.11 1 1\n"
            "verify/mathx/calc.go:7.2,7.14 1 1\n"
            "verify/mathx/calc.go:10.24,11.12 1 0\n",
            encoding="utf-8",
        )
        # include only the mathx package
        report = collect_go(root, cov, include_filter=["mathx/**/*.go"])
        assert list(report.files) == ["mathx/calc.go"]
        # exclude should drop it entirely
        report2 = collect_go(root, cov, exclude_filter=["**/calc.go"])
        assert list(report2.files) == []


# ── config: language=go ──────────────────────────────────────────────

class TestGoConfig:
    def test_load_go_config(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir(parents=True)
        (root / "aicoverage.toml").write_text(
            '[project]\nname="godemo"\nlanguage="go"\n'
            '[source]\npath="."\n'
            '[go]\ngo_bin="go"\npackages=["./..."]\nbuild_tags="integration"\n'
            'coverprofile=".aicoverage/cover.out"\n'
            '[test]\ndir="tests"\n',
            encoding="utf-8",
        )
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.language == "go"
        assert cfg.go_bin == "go"
        assert cfg.go_packages == ["./..."]
        assert cfg.go_build_tags == "integration"
        assert str(cfg.coverprofile).endswith(".aicoverage/cover.out")
        # Go default include globs are .go files
        assert cfg.include_globs == ["**/*.go"]

    def test_go_validation_skips_build_binary(self, tmp_path):
        """Go projects don't require build_cmd/binary (native go test instrumentation)."""
        root = tmp_path / "proj"
        root.mkdir(parents=True)
        cfg = ProjectConfig.minimal(root, name="go", language="go")
        assert cfg.validate() == []
        cfg2 = ProjectConfig.minimal(root, name="c", language="c")
        assert len(cfg2.validate()) == 2  # missing build_cmd + binary

    def test_source_files_uses_go_suffix(self, tmp_path):
        root = tmp_path / "proj"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "a.go").write_text("package p\n", encoding="utf-8")
        (root / "pkg" / "a.c").write_text("int x;\n", encoding="utf-8")
        (root / "pkg" / "a_test.go").write_text("package p\n", encoding="utf-8")
        cfg = ProjectConfig.minimal(root, name="go", language="go")
        # default exclude only filters tests/ dir, so a_test.go still matches *.go glob
        files = cfg.source_files()
        suffixes = {p.suffix for p in files}
        assert suffixes == {".go"}, f"Go source_files should only pick .go, got {suffixes}"
        assert all(p.suffix == ".go" for p in files)

    def test_to_env_injects_go_vars(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir(parents=True)
        cfg = ProjectConfig.minimal(root, name="go", language="go")
        env = cfg.to_env()
        assert env["AICOV_GO_BIN"] == "go"
        assert env["AICOV_GO_PACKAGES"] == "./..."
        assert env["AICOV_GO_COVERPROFILE"] == str(cfg.coverprofile)


# ── Real end-to-end: go test -coverprofile (skipped without Go) ─────

_GO_CANDIDATES = [
    shutil.which("go"),
    str(Path.home() / "go-toolchain" / "go" / "bin" / "go"),
]


@pytest.fixture()
def go_toolchain():
    for cand in _GO_CANDIDATES:
        if cand and Path(cand).is_file():
            return str(Path(cand).resolve())
    pytest.skip("本机无 go 工具链，跳过真实 Go 覆盖率端到端")


class TestGoEndToEnd:
    def test_real_go_test_coverage(self, tmp_path, go_toolchain):
        """Run `go test -coverprofile` on a real pure-stdlib module and collect."""
        root = _mk_go_project(tmp_path)
        (root / "mathx" / "calc_test.go").write_text(
            "package mathx\n"
            "import \"testing\"\n"
            "func TestAdd(t *testing.T) {\n"
            "\tif Add(1, 2) != 3 {\n"
            "\t\tt.Fatal(\"bad\")\n"
            "\t}\n"
            "}\n",
            encoding="utf-8",
        )
        cov = tmp_path / "cover.out"
        import os
        penv = dict(os.environ)
        # ensure go on PATH (go_toolchain may live outside the default PATH)
        go_dir = str(Path(go_toolchain).parent)
        penv["PATH"] = go_dir + os.pathsep + penv.get("PATH", "")
        proc = sp.run(
            [go_toolchain, "test", "-coverprofile", str(cov), "./mathx/"],
            cwd=str(root), capture_output=True, text=True, timeout=120, env=penv,
        )
        assert proc.returncode == 0, f"go test failed: {proc.stdout}{proc.stderr}"
        assert cov.exists()

        report = collect_go(root, cov, include_filter=["**/*.go"],
                            exclude_filter=["**/*_test.go"])
        assert report.func_total == 2
        assert report.files["mathx/calc.go"].functions["Add"].hit
        assert not report.files["mathx/calc.go"].functions["Div"].hit
        assert report.func_pct == 50.0

    def test_executor_run_go_tests(self, tmp_path, go_toolchain, monkeypatch):
        """The executor's Go branch (`run_go_tests`) runs `go test -coverprofile`,
        writes coverage.json, and dispatches correctly from run_tests."""
        root = _mk_go_project(tmp_path)
        (root / "mathx" / "calc_test.go").write_text(
            "package mathx\n"
            "import \"testing\"\n"
            "func TestAdd(t *testing.T) {\n"
            "\tif Add(1, 2) != 3 {\n"
            "\t\tt.Fatal(\"bad\")\n"
            "\t}\n"
            "}\n",
            encoding="utf-8",
        )
        from aicoverage.config import ProjectConfig
        from aicoverage.executor import run_go_tests, run_tests

        cfg = ProjectConfig.minimal(root, name="go", language="go")
        cfg.go_bin = go_toolchain
        # put go-toolchain on PATH for `go` subprocess resolution
        import os
        go_dir = str(Path(go_toolchain).parent)
        monkeypatch.setenv("PATH", go_dir + ":" + os.environ.get("PATH", ""))

        run_dir = tmp_path / "iter"
        # direct Go executor
        res = run_go_tests(cfg, run_dir)
        assert res.verdict == "PASS", f"go test should pass, got {res.verdict}: {res.detail}"
        assert res.tests == 1
        assert res.coverage_path is not None
        report = CoverageReport.load(res.coverage_path)
        assert report.func_pct == 50.0

        # run_tests must dispatch to the Go path when language == "go"
        res2 = run_tests(cfg, tmp_path / "iter2")
        assert res2.verdict == "PASS"
        assert res2.coverage_path is not None
