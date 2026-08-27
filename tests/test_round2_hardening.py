"""2026-08-27 第二轮加固的配套单测。

覆盖：
  - hooks：TEST_BLOCKED 全 agent 生效（pytest/go test）、Go 写白名单、gen git 禁令
  - agents：verify-agent 工具收紧（无 Bash、有 Grep/Glob）
  - agent_call.reset_backoff：模块级退避台账清零
  - gcov.collect 增量缓存：跳过重跑 / gcda 变化重跑 / stale 目录清理
  - finalreport Go 用例清单：*_test.go 入列 + e2e/unit 标注
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig  # noqa: E402


# ── hooks ────────────────────────────────────────────────────────────

def _hooks_for(agent: str, cfg: ProjectConfig):
    from aicoverage.hooks import make_security_hooks
    return make_security_hooks(agent, cfg)


def _bash_guard(hooks):
    matchers = hooks["PreToolUse"]
    for m in matchers:
        for h in m.hooks:
            return h  # first hook is bash_guard by construction
    raise AssertionError("no bash_guard")


def _write_guard(hooks):
    matchers = hooks["PreToolUse"]
    hooks_list = matchers[1].hooks
    return hooks_list[0]


def _run(coro):
    return asyncio.run(coro)


class TestTestBlockedAllAgents:
    """执行铁律对全 agent 生效（原 GEN_BLOCKED 只拦 gen-agent）。"""

    @pytest.mark.parametrize("agent", ["gen-agent", "verify-agent", "quality-agent",
                                       "analyzer-agent", "coverage-agent", "scan-agent"])
    def test_pytest_blocked_for_every_agent(self, tmp_path, agent):
        cfg = ProjectConfig.minimal(tmp_path, name="p", build_cmd="make", binary="app")
        guard = _bash_guard(_hooks_for(agent, cfg))
        res = _run(guard({"command": "uv run pytest tests/ -q"}, "Bash", None))
        assert res.get("decision") == "block"

    def test_go_test_blocked(self, tmp_path):
        """Go 项目的 go test 同样被拦（原 GEN_BLOCKED 无 go test）。"""
        cfg = ProjectConfig.minimal(tmp_path, name="p", language="go")
        for agent in ("gen-agent", "verify-agent"):
            guard = _bash_guard(_hooks_for(agent, cfg))
            res = _run(guard({"command": "go test -v ./..."}, "Bash", None))
            assert res.get("decision") == "block", f"{agent} 未拦截 go test"

    def test_normal_commands_pass(self, tmp_path):
        """普通读命令不误伤（go build / grep / ls）。"""
        cfg = ProjectConfig.minimal(tmp_path, name="p", build_cmd="make", binary="app")
        guard = _bash_guard(_hooks_for("coverage-agent", cfg))
        for cmd in ("go build ./...", "grep -rn foo src/", "ls -la", "gcc --version"):
            res = _run(guard({"command": cmd}, "Bash", None))
            assert not res.get("decision"), f"误伤: {cmd}"

    def test_gen_git_still_blocked(self, tmp_path):
        cfg = ProjectConfig.minimal(tmp_path, name="p", build_cmd="make", binary="app")
        guard = _bash_guard(_hooks_for("gen-agent", cfg))
        assert _run(guard({"command": "git push origin master"}, "Bash", None)).get("decision") == "block"
        # 其他 agent 不做 git 操作限制（analyzer 读 git log 是合法的）
        guard2 = _bash_guard(_hooks_for("analyzer-agent", cfg))
        assert not _run(guard2({"command": "git log --oneline -5"}, "Bash", None)).get("decision")


class TestGoWriteWhitelist:
    def test_go_gen_may_write_test_go_next_to_source(self, tmp_path):
        """Go 用例与源码同目录（internal/router/router_test.go）→ 放行（缺陷A修复）。"""
        src = tmp_path / "proj"
        (src / "internal" / "router").mkdir(parents=True)
        cfg = ProjectConfig.minimal(src, name="p", language="go")
        guard = _write_guard(_hooks_for("gen-agent", cfg))
        res = _run(guard({"filePath": str(src / "internal" / "router" / "router_test.go")},
                         "Write", None))
        assert not res.get("decision")

    def test_go_gen_cannot_write_non_test_go(self, tmp_path):
        """Go gen 写非 _test.go 源码仍被拦。"""
        src = tmp_path / "proj"
        (src / "internal").mkdir(parents=True)
        cfg = ProjectConfig.minimal(src, name="p", language="go")
        guard = _write_guard(_hooks_for("gen-agent", cfg))
        res = _run(guard({"filePath": str(src / "internal" / "router.go")}, "Write", None))
        assert res.get("decision") == "block"

    def test_c_gen_cannot_write_source_but_can_write_tests(self, tmp_path):
        src = tmp_path / "proj"
        (src / "src").mkdir(parents=True)
        (src / "tests").mkdir(parents=True)
        cfg = ProjectConfig.minimal(src, name="p", build_cmd="make", binary="app")
        guard = _write_guard(_hooks_for("gen-agent", cfg))
        assert _run(guard({"filePath": str(src / "src" / "a.c")}, "Write", None)).get("decision") == "block"
        assert not _run(guard({"filePath": str(src / "tests" / "test_a.py")}, "Write", None)).get("decision")


class TestVerifyTools:
    def test_verify_has_no_bash(self):
        from aicoverage.agents import AGENT_TOOLS
        tools = AGENT_TOOLS["verify-agent"]
        assert "Bash" not in tools
        assert "Read" in tools and "Write" in tools
        assert "Grep" in tools and "Glob" in tools


# ── agent_call.reset_backoff ─────────────────────────────────────────

class TestResetBackoff:
    def test_reset_clears_ledger(self):
        from aicoverage import agent_call
        agent_call._backoff_elapsed["gen-agent"] = 500.0
        agent_call._backoff_elapsed["verify-agent"] = 300.0
        agent_call.reset_backoff()
        assert agent_call._backoff_elapsed == {}


# ── gcov.collect 增量缓存 ─────────────────────────────────────────────

def _gcc_available() -> bool:
    import shutil
    return shutil.which("gcc") is not None and shutil.which("gcov") is not None


@pytest.mark.skipif(not _gcc_available(), reason="需要 gcc+gcov 环境")
class TestGcovIncrementalCache:
    def _mk_project(self, tmp_path: Path) -> Path:
        src = tmp_path / "proj"
        src.mkdir()
        (src / "a.c").write_text(
            "int hit_fn(void) { return 1; }\n"
            "int miss_fn(void) { return 2; }\n"
            "int main(void) { hit_fn(); return 0; }\n", encoding="utf-8")
        return src

    def _compile(self, src: Path):
        import subprocess
        subprocess.run(["gcc", "--coverage", "-O0", "-o", str(src / "app"),
                        str(src / "a.c")], check=True, cwd=str(src),
                       capture_output=True)

    def test_second_collect_skips_rerun(self, tmp_path):
        from aicoverage.gcov import collect
        src = self._mk_project(tmp_path)
        self._compile(src)
        r1 = collect(src)
        assert r1.func_total >= 2
        work = src / ".aicoverage" / "coverage_raw"
        assert (work / "_index_map.json").exists()
        # 记录首轮产物 mtime
        outs1 = {p: p.stat().st_mtime for p in work.rglob("*.gcov.json*")}
        time.sleep(0.02)
        # 第二轮：无 .gcda 变化 → 增量跳过，产物文件 mtime 不变
        r2 = collect(src)
        outs2 = {p: p.stat().st_mtime for p in work.rglob("*.gcov.json*")}
        assert outs1 == outs2, "增量缓存未生效：第二轮重建了 gcov 产物"
        assert r2.func_total == r1.func_total

    def test_gcda_change_triggers_rerun(self, tmp_path):
        import subprocess
        from aicoverage.gcov import collect
        src = self._mk_project(tmp_path)
        self._compile(src)
        r1 = collect(src)
        assert r1.files["a.c"].functions["miss_fn"].execution_count == 0
        # 跑一遍被测程序产生 .gcda
        subprocess.run([str(src / "app")], cwd=str(src), check=True, capture_output=True)
        time.sleep(0.02)
        r2 = collect(src)
        # hit_fn 被执行 → 计数 > 0（增量正确性：重跑后读到新数据）
        assert r2.files["a.c"].functions["hit_fn"].execution_count >= 1

    def test_stale_subdir_removed(self, tmp_path):
        from aicoverage.gcov import collect
        src = self._mk_project(tmp_path)
        self._compile(src)
        collect(src)
        work = src / ".aicoverage" / "coverage_raw"
        # 伪造一个不在 index_map 里的陈旧子目录 → 下轮应被清理
        # 先删掉 gcno（模拟文件被移除），stale 逻辑按 map 对比清理
        stale = work / "999"
        stale.mkdir()
        (stale / "junk.gcov.json").write_text("{}", encoding="utf-8")
        # index_map 中没有 999 → prev_map.get("999") 为 None → cur_map 也无 → 删除
        collect(src)
        assert not stale.exists()


# ── finalreport Go 用例清单 ──────────────────────────────────────────

class TestGoCaseInventory:
    def test_go_tests_listed_with_source_class(self, tmp_path):
        from aicoverage.finalreport import _collect_go_test_functions
        src = tmp_path / "proj"
        (src / "internal" / "router").mkdir(parents=True)
        (src / "pkg" / "calc").mkdir(parents=True)
        # 含 httptest 的文件 → file-level fallback 下全文件判 e2e
        (src / "internal" / "router" / "router_test.go").write_text(
            "package router\n\n"
            "import (\n\t\"net/http/httptest\"\n\t\"testing\"\n)\n\n"
            "func TestHealth(t *testing.T) {\n"
            "\tsrv := httptest.NewServer(nil)\n\tdefer srv.Close()\n}\n", encoding="utf-8")
        # 无网络信号的独立文件 → unit
        (src / "pkg" / "calc" / "calc_test.go").write_text(
            "package calc\n\nimport \"testing\"\n\n"
            "func TestHelper(t *testing.T) {\n"
            "\t_ = compute(1)\n}\n", encoding="utf-8")
        cases = _collect_go_test_functions(src)
        assert "router_test.go" in cases
        assert "calc_test.go" in cases
        assert "TestHealth(e2e)" in " ".join(cases["router_test.go"])
        assert "TestHelper(unit)" in " ".join(cases["calc_test.go"])

    def test_empty_source_returns_empty(self, tmp_path):
        from aicoverage.finalreport import _collect_go_test_functions
        assert _collect_go_test_functions(tmp_path) == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
