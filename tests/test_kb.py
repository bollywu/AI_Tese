"""知识库（wikirize 适配）模块单测：wiki_ready / 导航提示注入 / hooks 写白名单。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig  # noqa: E402
from aicoverage.kb import REQUIRED_PAGES, wiki_dir, wiki_navigation_hint, wiki_ready  # noqa: E402


def _mk_cfg(tmp_path: Path) -> ProjectConfig:
    cfg = ProjectConfig.__new__(ProjectConfig)
    cfg.config_path = tmp_path / "aicoverage.toml"
    cfg.name = "proj"; cfg.display_name = "proj"
    cfg.source_path = tmp_path
    cfg.build_cmd = "make"; cfg.binary = Path("app")
    cfg.test_dirname = "tests"; cfg.test_timeout = 60
    cfg.extra_blocked_commands = []
    return cfg


def _mk_wiki(tmp_path: Path) -> None:
    d = tmp_path / "wiki"
    d.mkdir(exist_ok=True)
    for p in REQUIRED_PAGES:
        (d / p).write_text(f"# {p}\n", encoding="utf-8")


class TestWikiReady:
    def test_no_wiki(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        assert wiki_ready(cfg) is False

    def test_full_wiki(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        _mk_wiki(tmp_path)
        assert wiki_ready(cfg) is True

    def test_partial_wiki_not_ready(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        d = tmp_path / "wiki"
        d.mkdir()
        for p in REQUIRED_PAGES[:-1]:   # 缺最后一个必备页
            (d / p).write_text("# x\n", encoding="utf-8")
        assert wiki_ready(cfg) is False


class TestNavigationHint:
    def test_no_hint_without_wiki(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        assert wiki_navigation_hint(cfg) == ""

    def test_hint_with_wiki(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        _mk_wiki(tmp_path)
        hint = wiki_navigation_hint(cfg)
        assert "wiki" in hint
        assert "agent-quickstart.md" in hint and "source-map.md" in hint
        assert "源码为准" in hint          # 定位器而非真相的警示必须带上

    def test_partial_wiki_no_hint(self, tmp_path):
        """不完整的 wiki 不给导航提示（防 agent 被引到不存在的页面）。"""
        cfg = _mk_cfg(tmp_path)
        (tmp_path / "wiki").mkdir()
        (tmp_path / "wiki" / "index.md").write_text("# only index\n", encoding="utf-8")
        assert wiki_navigation_hint(cfg) == ""


class TestKbHooks:
    """kb-agent 写白名单：只准写 wiki/** 与根 AGENTS.md。"""

    def _mk_hooks(self, cfg: ProjectConfig):
        import asyncio

        from aicoverage.hooks import make_security_hooks
        hooks = make_security_hooks("kb-agent", cfg)
        return {m.matcher: m.hooks[0] for m in hooks["PreToolUse"]}

    def _run_guard(self, hooks, path: str) -> dict:
        import asyncio

        write_guard = hooks.get("Write|Edit|MultiEdit|replace_in_file|delete_file")
        return asyncio.run(write_guard({"filePath": path}, "Write", None))

    def test_wiki_write_allowed(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        hooks = self._mk_hooks(cfg)
        assert self._run_guard(hooks, str(tmp_path / "wiki" / "index.md")) == {}
        assert self._run_guard(hooks, str(tmp_path / "wiki" / "sub" / "x.md")) == {}

    def test_agents_md_allowed(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        hooks = self._mk_hooks(cfg)
        assert self._run_guard(hooks, str(tmp_path / "AGENTS.md")) == {}

    def test_source_write_blocked(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        (tmp_path / "src").mkdir()
        hooks = self._mk_hooks(cfg)
        blocked = self._run_guard(hooks, str(tmp_path / "src" / "wrk.c"))
        assert blocked.get("decision") == "block"

    def test_fake_agents_md_path_blocked(self, tmp_path):
        """深层目录下的同名 AGENTS.md 不算白名单（只认根目录）。"""
        cfg = _mk_cfg(tmp_path)
        deep = tmp_path / "wiki" / "sub"
        deep.mkdir(parents=True)
        hooks = self._mk_hooks(cfg)
        # wiki 内的 AGENTS.md 属于 wiki/ 前缀 → 允许（仍是 wiki 内容）
        assert self._run_guard(hooks, str(deep / "AGENTS.md")) == {}
        src_fake = tmp_path / "src"
        src_fake.mkdir()
        blocked = self._run_guard(hooks, str(src_fake / "AGENTS.md"))
        assert blocked.get("decision") == "block"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
