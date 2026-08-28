"""P3 单测：变异自检（aicov mutate）+ CodeGraph 可达性富化（plan 4.3）。

mutate 用真实 gcc 编译的二进制 + 真实 pytest 执行验证"失效替身下仍 PASS =
假阳性"语义；reachability 用 monkeypatch 的 fake CodeGraph 结果验证富化逻辑。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig, load_config  # noqa: E402


# ── 变异自检 ─────────────────────────────────────────────────────────

def _gcc_available() -> bool:
    return shutil.which("gcc") is not None and Path("/bin/true").exists()


def _mk_project_with_cases(tmp_path: Path) -> ProjectConfig:
    """真实项目：gcc 编译 app（打印 hello-aicov）+ scaffold 脚手架 + 3 个用例。

    - test_real_assertion: 断言输出含 hello-aicov → 变异后 FAIL（真实验证）
    - test_fake_assertion: assert True → 变异后仍 PASS（假阳性嫌疑）
    - test_unit_channel:   调 compile_unit_driver → 排除（单测通道）
    """
    from aicoverage.templates import scaffold

    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.c").write_text(
        '#include <stdio.h>\n'
        'int main(void) { printf("hello-aicov\\n"); return 0; }\n',
        encoding="utf-8")
    scaffold(src, name="proj", build_cmd="gcc -O0 --coverage -o app app.c",
             binary="app", language="c")
    (src / "tests" / "test_real.py").write_text(
        'from harness import run_binary, assert_stdout_contains\n'
        'def test_real_assertion():\n'
        '    """\n    描述：真实断言\n    测试点：app.c:2\n    """\n'
        '    res = run_binary([])\n'
        '    assert_stdout_contains(res, "hello-aicov")\n', encoding="utf-8")
    (src / "tests" / "test_fake.py").write_text(
        'def test_fake_assertion():\n'
        '    """\n    描述：恒真\n    测试点：x\n    """\n'
        '    assert True\n', encoding="utf-8")
    (src / "tests" / "test_ut.py").write_text(
        'from harness import run_binary, assert_exit_code\n'
        'def test_unit_channel():\n'
        '    """\n    描述：单测通道\n    测试点：x\n    """\n'
        '    res = compile_unit_driver("tests/drivers/d.c", sources=["app.c"],\n'
        '                              out_name="ut_x")\n'
        '    assert True\n', encoding="utf-8")

    # 手工建 run 目录结构（相当于 loop iter_1 的产物）
    iter_dir = src / ".aicoverage" / "runs" / "LOOP_20260828_000000" / "iter_1"
    iter_dir.mkdir(parents=True)
    (iter_dir / "manifest.json").write_text(json.dumps({
        "batch_id": "gen_iter1",
        "test_files": ["test_real.py", "test_fake.py", "test_ut.py"],
        "new_functions": ["test_real_assertion", "test_fake_assertion",
                          "test_unit_channel"],
    }), encoding="utf-8")

    import subprocess
    subprocess.run(["gcc", "-O0", "--coverage", "-o", "app", "app.c"],
                   cwd=str(src), check=True, capture_output=True)
    return load_config(str(src / "aicoverage.toml"))


@pytest.mark.skipif(not _gcc_available(), reason="需要 gcc 与 /bin/true")
class TestMutationCheck:
    def test_dead_binary_exposes_false_positive(self, tmp_path):
        """核心语义：失效二进制下，真断言 FAIL、恒真断言仍 PASS → 被抓出。"""
        from aicoverage.mutate import run_mutation_check
        cfg = _mk_project_with_cases(tmp_path)
        res = run_mutation_check(cfg, run_id="LOOP_20260828_000000", iter_n=1)
        assert res.ok, res.detail
        assert "test_real_assertion" in res.checked
        assert res.suspicious == ["test_fake_assertion"], res.suspicious
        # 单测通道用例被排除（不参与嫌疑判定）
        assert "test_unit_channel" in res.unit_cases
        assert "test_unit_channel" not in res.suspicious
        # 报告落盘
        report = json.loads(
            (cfg.runs_dir / "LOOP_20260828_000000" / "iter_1" / "mutate_report.json")
            .read_text(encoding="utf-8"))
        assert report["suspicious"] == ["test_fake_assertion"]

    def test_binary_restored_after_run(self, tmp_path):
        """原二进制必须被恢复（try/finally 保障）。"""
        from aicoverage.mutate import run_mutation_check
        cfg = _mk_project_with_cases(tmp_path)
        before = cfg.binary_path.read_bytes()
        res = run_mutation_check(cfg, run_id="LOOP_20260828_000000", iter_n=1)
        assert res.ok
        assert cfg.binary_path.read_bytes() == before

    def test_go_unsupported(self, tmp_path):
        from aicoverage.mutate import run_mutation_check
        src = tmp_path / "goproj"
        src.mkdir()
        cfg = ProjectConfig.minimal(src, name="g", language="go")
        res = run_mutation_check(cfg)
        assert not res.ok and "不适用" in res.detail

    def test_missing_run_errors(self, tmp_path):
        from aicoverage.mutate import run_mutation_check
        src = tmp_path / "proj2"
        (src / "tests").mkdir(parents=True)
        cfg = ProjectConfig.minimal(src, name="p", build_cmd="make", binary="app")
        (src / "app").write_text("#!/bin/sh\n", encoding="utf-8")
        res = run_mutation_check(cfg)
        assert not res.ok and "runs" in res.detail


# ── CodeGraph 可达性富化 ─────────────────────────────────────────────

class TestPlanReachability:
    def test_disabled_is_noop(self, tmp_path):
        from aicoverage.loop import _enrich_plan_reachability
        cfg = ProjectConfig.minimal(tmp_path, name="p")
        cfg.codegraph_enabled = False
        plan = {"targets": [{"id": "T-1", "file": "a.c", "functions": ["f"]}]}
        assert _enrich_plan_reachability(cfg, plan) == 0
        assert "reachability" not in plan["targets"][0]

    def test_no_index_is_noop(self, tmp_path):
        from aicoverage.loop import _enrich_plan_reachability
        cfg = ProjectConfig.minimal(tmp_path, name="p")
        cfg.codegraph_enabled = True
        cfg.codegraph_index_dir = ".codegraph"
        plan = {"targets": [{"id": "T-1", "file": "a.c", "functions": ["f"]}]}
        assert _enrich_plan_reachability(cfg, plan) == 0

    def test_enriches_with_fake_codegraph(self, tmp_path, monkeypatch):
        from aicoverage import callgraph
        from aicoverage.loop import _enrich_plan_reachability

        cfg = ProjectConfig.minimal(tmp_path, name="p")
        cfg.codegraph_enabled = True
        cfg.codegraph_index_dir = ".codegraph"
        cfg.codegraph_entrypoints = ["main"]
        monkeypatch.setattr(callgraph, "is_indexed", lambda *a, **k: True)

        def fake_trace(source_path, targets, entrypoints, **kw):
            out = {}
            for t in targets:
                if t == "reachable_fn":
                    out[t] = callgraph.TraceResult(
                        target=t, found=True,
                        paths=[callgraph.CallPath(entry="main",
                                                  path=["main", "serve", t])])
                else:
                    out[t] = callgraph.TraceResult(target=t, found=False)
            return out

        monkeypatch.setattr(callgraph, "trace_batch_to_entrypoints", fake_trace)
        plan = {"targets": [
            {"id": "T-1", "file": "a.c",
             "functions": ["reachable_fn", "dead_fn"]},
        ]}
        n = _enrich_plan_reachability(cfg, plan)
        assert n == 2
        reach = plan["targets"][0]["reachability"]
        assert reach["reachable_fn"]["found"] is True
        assert reach["reachable_fn"]["path"] == "main → serve → reachable_fn"
        assert reach["dead_fn"]["found"] is False
        assert reach["dead_fn"]["path"] == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
