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
        unit = [{"file": "src/url.c", "function": "parse_url_invalid", "evidence": "N3"}]
        res = _confirm_unit_coverage(cfg, _manifest(unit=unit), interactive=False)
        assert len(res["confirmed"]) == 1 and len(res["pending"]) == 0
        assert res["confirmed"][0]["confirmed"] is True

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
            {"file": "a.c", "function": "f1", "evidence": "N3"},
            {"file": "b.c", "function": "f2", "evidence": "N5"},
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
