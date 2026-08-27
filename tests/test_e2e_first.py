"""E2E-first coverage-governance tests: unit-test human confirmation gate.

Requirement (2026-08-27): all coverage must be reached through E2E first; a function
that genuinely cannot be E2E-reached may only be covered by a unit test after explicit
human confirmation. These tests cover:
  - config parsing for e2e_first / require_unit_confirm / unit_confirm_auto_yes
  - the loop's _confirm_unit_coverage gate (interactive y/n / non-interactive pending /
    auto_yes / governance off)
  - the gen prompt's E2E-first discipline hint (_unittest_hint)
  - the final report's pending-confirmation section
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig, load_config  # noqa: E402


# ── config parsing ───────────────────────────────────────────────────

class TestGovernanceConfig:
    def test_defaults(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir(parents=True)
        (root / "aicoverage.toml").write_text(
            '[project]\nname="p"\nlanguage="c"\n[source]\npath="."\n'
            '[build]\nbuild_cmd="make --coverage"\nbinary="./app"\n', encoding="utf-8")
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.e2e_first is True
        assert cfg.require_unit_confirm is True
        assert cfg.unit_confirm_auto_yes is False

    def test_overrides(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir(parents=True)
        (root / "aicoverage.toml").write_text(
            '[project]\nname="p"\nlanguage="c"\n[source]\npath="."\n'
            '[build]\nbuild_cmd="make --coverage"\nbinary="./app"\n'
            '[coverage]\ne2e_first=false\nrequire_unit_confirm=false\n'
            'unit_confirm_auto_yes=true\n', encoding="utf-8")
        cfg = load_config(str(root / "aicoverage.toml"))
        assert cfg.e2e_first is False
        assert cfg.require_unit_confirm is False
        assert cfg.unit_confirm_auto_yes is True

    def test_to_env_injects_governance(self, tmp_path):
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="c")
        env = cfg.to_env()
        assert env["AICOV_E2E_FIRST"] == "1"
        assert env["AICOV_REQUIRE_UNIT_CONFIRM"] == "1"
        assert env["AICOV_UNIT_CONFIRM_AUTO_YES"] == "0"


# ── loop confirmation gate ───────────────────────────────────────────

def _mk_cfg(tmp_path, **kw) -> ProjectConfig:
    cfg = ProjectConfig.minimal(tmp_path, name="p", language="c")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _manifest(unit=None, e2e=None) -> dict:
    m = {"test_files": ["test_x.py"], "new_functions": ["test_x"],
         "targets": [{"file": "src/a.c", "functions": ["a"]}]}
    if e2e is not None:
        m["e2e_functions"] = e2e
    if unit is not None:
        m["unit_confirm_required"] = unit
    return m


class TestConfirmUnitCoverage:
    def test_no_declaration(self, tmp_path):
        from aicoverage.loop import _confirm_unit_coverage
        cfg = _mk_cfg(tmp_path)
        res = _confirm_unit_coverage(cfg, _manifest(unit=None))
        assert res == {"confirmed": [], "pending": [], "declared": []}

    def test_non_interactive_default_pending(self, tmp_path, monkeypatch):
        from aicoverage.loop import _confirm_unit_coverage
        cfg = _mk_cfg(tmp_path)  # require_unit_confirm=True, auto_yes=False
        unit = [{"file": "src/url.c", "function": "parse_url_invalid",
                 "evidence": "错误路径 N3"}]
        res = _confirm_unit_coverage(cfg, _manifest(unit=unit), interactive=False)
        assert len(res["declared"]) == 1
        assert len(res["pending"]) == 1 and len(res["confirmed"]) == 0
        assert res["pending"][0]["function"] == "parse_url_invalid"
        assert res["pending"][0]["confirmed"] is False

    def test_auto_yes_confirms(self, tmp_path):
        from aicoverage.loop import _confirm_unit_coverage
        cfg = _mk_cfg(tmp_path, unit_confirm_auto_yes=True)
        unit = [{"file": "src/url.c", "function": "parse_url_invalid",
                 "evidence": "N3 错误路径，src/url.c:120 无入口可触达"}]
        res = _confirm_unit_coverage(cfg, _manifest(unit=unit), interactive=False)
        assert len(res["confirmed"]) == 1 and len(res["pending"]) == 0
        assert res["confirmed"][0]["confirmed"] is True

    def test_auto_yes_never_approves_weak_evidence(self, tmp_path):
        """证据未引用源码位置（file:line）→ 即使 auto_yes 也不核准（新门禁 6.3）。"""
        from aicoverage.loop import _confirm_unit_coverage
        cfg = _mk_cfg(tmp_path, unit_confirm_auto_yes=True)
        unit = [{"file": "src/url.c", "function": "parse_url_invalid", "evidence": "N3"}]
        res = _confirm_unit_coverage(cfg, _manifest(unit=unit), interactive=False)
        assert len(res["confirmed"]) == 0
        assert len(res["pending"]) == 1
        assert "否决" in res["pending"][0]["evidence"]

    def test_interactive_vetoed_never_prompted(self, tmp_path, monkeypatch):
        """被否决的声明（弱证据）在交互模式下也不询问，直接待确认。"""
        from aicoverage.loop import _confirm_unit_coverage
        cfg = _mk_cfg(tmp_path)
        unit = [{"file": "a.c", "function": "f1", "evidence": "N3"}]
        called = []
        monkeypatch.setattr("builtins.input", lambda *a, **k: called.append(1) or "y")
        res = _confirm_unit_coverage(cfg, _manifest(unit=unit), interactive=True)
        assert not called  # 未触发任何交互询问
        assert len(res["pending"]) == 1

    def test_governance_off_confirms(self, tmp_path):
        from aicoverage.loop import _confirm_unit_coverage
        cfg = _mk_cfg(tmp_path, require_unit_confirm=False)
        unit = [{"file": "src/url.c", "function": "parse_url_invalid", "evidence": "N3"}]
        res = _confirm_unit_coverage(cfg, _manifest(unit=unit), interactive=False)
        assert len(res["confirmed"]) == 1 and len(res["pending"]) == 0

    def test_interactive_yes_and_no(self, tmp_path, monkeypatch):
        from aicoverage.loop import _confirm_unit_coverage
        cfg = _mk_cfg(tmp_path)
        unit = [
            {"file": "a.c", "function": "f1", "evidence": "N3 错误路径，a.c:12 无入口"},
            {"file": "b.c", "function": "f2", "evidence": "N5 死代码，b.c:34 无调用点"},
        ]
        # y for first, N for second
        inputs = iter(["y", "N"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(inputs))
        res = _confirm_unit_coverage(cfg, _manifest(unit=unit), interactive=True)
        assert len(res["confirmed"]) == 1 and res["confirmed"][0]["function"] == "f1"
        assert len(res["pending"]) == 1 and res["pending"][0]["function"] == "f2"


# ── gen prompt E2E-first hint ────────────────────────────────────────

class TestUnittestHint:
    def test_e2e_first_mentions_confirm_and_discipline(self, tmp_path):
        from aicoverage.loop import _unittest_hint
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="c")
        hint = _unittest_hint(cfg)
        assert "E2E 优先" in hint
        assert "unit_confirm_required" in hint
        assert "run_binary" in hint
        # N4/N6 must stay E2E
        assert "N4/N6" in hint or "能 E2E 触达" in hint

    def test_e2e_first_disabled_returns_empty(self, tmp_path):
        from aicoverage.loop import _unittest_hint
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="c")
        cfg.e2e_first = False
        assert _unittest_hint(cfg) == ""

    def test_go_project_uses_go_instruction(self, tmp_path):
        from aicoverage.loop import _gen_write_instruction
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="go")
        assert "_test.go" in _gen_write_instruction(cfg)


# ── final report pending-confirmation section ────────────────────────

def _write_report_inputs(tmp_path, run_dir, *, pending=None, confirmed=None):
    """Assemble the minimal on-disk artifacts `finalreport.build` needs to render the
    unit-confirm section. We call build() and assert the section text appears."""
    from aicoverage.finalreport import _iter_dirs, _load_json  # noqa: F401
    (run_dir / "iter_1").mkdir(parents=True, exist_ok=True)
    uc = {"declared": [], "confirmed": confirmed or [],
          "pending": pending or []}
    (run_dir / "iter_1" / "unit_confirm.json").write_text(
        __import__("json").dumps(uc, ensure_ascii=False), encoding="utf-8")
    return run_dir


class TestFinalReportUnitConfirm:
    def test_pending_section_rendered(self, tmp_path):
        from aicoverage.finalreport import write_final_report
        from aicoverage.config import ProjectConfig
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "RUN_TEST"
        run_dir.mkdir(parents=True, exist_ok=True)
        pending = [{"file": "src/url.c", "function": "parse_url_invalid",
                    "evidence": "错误路径 N3，无入口"}]
        _write_report_inputs(tmp_path, run_dir, pending=pending)
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="c")
        out_path = tmp_path / "report.md"
        write_final_report(cfg, runs_dir, "RUN_TEST", {}, out_path)
        md = out_path.read_text(encoding="utf-8")
        assert "单测覆盖待人工确认" in md
        assert "parse_url_invalid" in md
        assert "无入口" in md


# ── Go E2E-first governance ───────────────────────────────────────────

def _write_go_test(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestGoTestScope:
    def test_unit_classification(self, tmp_path):
        from aicoverage.go_test_scope import classify_go_test_file
        root = tmp_path
        f = _write_go_test(root, "srv_test.go",
                           "package srv\n"
                           "func TestAdd(t *testing.T) {\n"
                           "\tif Add(1,2) != 3 { t.Fatal(\"bad\") }\n"
                           "}\n")
        funcs = classify_go_test_file(f, root)
        assert len(funcs) == 1
        assert funcs[0].name == "TestAdd"
        assert funcs[0].source == "unit"  # no HTTP/net signal

    def test_e2e_classification_http(self, tmp_path):
        from aicoverage.go_test_scope import classify_go_test_file
        root = tmp_path
        f = _write_go_test(root, "api_test.go",
                           "package api\n"
                           "import \"net/http/httptest\"\n"
                           "func TestHandler(t *testing.T) {\n"
                           "\trec := httptest.NewRecorder()\n"
                           "\t_ = rec\n"
                           "}\n")
        funcs = classify_go_test_file(f, root)
        assert funcs[0].source == "e2e"  # httptest signal

    def test_e2e_classification_gin(self, tmp_path):
        from aicoverage.go_test_scope import classify_go_test_file
        root = tmp_path
        f = _write_go_test(root, "router_test.go",
                           "package r\n"
                           "import \"github.com/gin-gonic/gin\"\n"
                           "func TestRoute(t *testing.T) {\n"
                           "\tr := gin.New()\n"
                           "\t_ = r\n"
                           "}\n")
        funcs = classify_go_test_file(f, root)
        assert funcs[0].source == "e2e"

    def test_gorm_delete_not_false_positive(self, tmp_path):
        """`db.Delete(...)` (gorm) must NOT be classified e2e."""
        from aicoverage.go_test_scope import classify_go_test_file
        root = tmp_path
        f = _write_go_test(root, "svc_test.go",
                           "package s\n"
                           "func TestDelete(t *testing.T) {\n"
                           "\tdb.Delete(&v)\n"
                           "}\n")
        funcs = classify_go_test_file(f, root)
        assert funcs[0].source == "unit"

    def test_file_level_helper_fallback(self, tmp_path):
        """A test driving HTTP via a shared helper (doJSON) with no inline httptest must
        still be e2e because the file carries an httptest helper signal."""
        from aicoverage.go_test_scope import classify_go_test_file
        root = tmp_path
        f = _write_go_test(root, "api_test.go",
                           "package api\n"
                           "import \"net/http/httptest\"\n"
                           "func doJSON(r any, p string) *httptest.ResponseRecorder {\n"
                           "\treq := httptest.NewRequest(\"GET\", p, nil)\n"
                           "\tw := httptest.NewRecorder()\n"
                           "\treturn w\n"
                           "}\n"
                           "func TestList(t *testing.T) {\n"
                           "\tw := doJSON(nil, \"/api/v1/vessels\")\n"
                           "\t_ = w\n"
                           "}\n")
        funcs = classify_go_test_file(f, root)
        assert funcs[0].name == "TestList"
        assert funcs[0].source == "e2e"  # file-level httptest helper signal

    def test_mixed_file_marks_all_e2e_when_httptest_present(self, tmp_path):
        """A file with an httptest helper classifies all its tests as e2e (heuristic for
        shared-HTTP-helper test files)."""
        from aicoverage.go_test_scope import classify_go_test_file
        root = tmp_path
        f = _write_go_test(root, "mix_test.go",
                           "package m\n"
                           "import \"net/http/httptest\"\n"
                           "func TestPure(t *testing.T) {\n"
                           "\tDoWork()\n"
                           "}\n"
                           "func TestHTTP(t *testing.T) {\n"
                           "\thttptest.NewRecorder()\n"
                           "}\n")
        funcs = classify_go_test_file(f, root)
        by = {x.name: x.source for x in funcs}
        assert by["TestHTTP"] == "e2e"
        assert by["TestPure"] == "e2e"  # file-level fallback


class TestGoE2EFirstGate:
    def test_go_unittest_hint(self, tmp_path):
        from aicoverage.loop import _unittest_hint
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="go")
        hint = _unittest_hint(cfg)
        assert "E2E/集成测试优先" in hint
        assert "unit_confirm_required" in hint
        assert "httptest" in hint

    def test_go_write_instruction_mentions_e2e(self, tmp_path):
        from aicoverage.loop import _gen_write_instruction
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="go")
        inst = _gen_write_instruction(cfg)
        assert "_test.go" in inst
        assert "unit_confirm_required" in inst

    def test_go_auto_detect_unit_tests(self, tmp_path):
        """A generated *_test.go with a pure unit test should surface as pending via the
        gate even if gen didn't declare unit_confirm_required."""
        from aicoverage.loop import _confirm_unit_coverage
        root = tmp_path / "proj"
        _write_go_test(root, "internal/svc/svc_test.go",
                       "package svc\n"
                       "func TestDoWork(t *testing.T) {\n"
                       "\tDoWork()\n"
                       "}\n")
        cfg = ProjectConfig.minimal(root, name="p", language="go")
        manifest = {"test_files": ["internal/svc/svc_test.go"],
                    "new_functions": ["TestDoWork"]}
        res = _confirm_unit_coverage(cfg, manifest, interactive=False)
        # 1 pure-unit test auto-detected -> pending (require_unit_confirm=True, no auto_yes)
        assert len(res["pending"]) == 1
        assert res["pending"][0]["function"] == "TestDoWork"
        assert len(res["confirmed"]) == 0

    def test_go_auto_detect_skips_e2e(self, tmp_path):
        """A generated *_test.go with an e2e/HTTP test should NOT be flagged pending."""
        from aicoverage.loop import _confirm_unit_coverage
        root = tmp_path / "proj"
        _write_go_test(root, "internal/api/api_test.go",
                       "package api\n"
                       "import \"net/http/httptest\"\n"
                       "func TestHTTPHandler(t *testing.T) {\n"
                       "\thttptest.NewServer(nil)\n"
                       "}\n")
        cfg = ProjectConfig.minimal(root, name="p", language="go")
        manifest = {"test_files": ["internal/api/api_test.go"],
                    "new_functions": ["TestHTTPHandler"]}
        res = _confirm_unit_coverage(cfg, manifest, interactive=False)
        assert res["pending"] == [] and res["confirmed"] == []
