"""MR 增量覆盖闭环 M1：mrdiff / callgraph / diffextract 单测。

callgraph.py 依赖真实 `codegraph` CLI 二进制，测试用 `shutil.which` 检测，
不可用时 `pytest.skip`（与项目里 gcc 依赖测试的既有约定一致）。
"""
from __future__ import annotations

import shutil
import subprocess as sp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage import callgraph, diffextract, mrdiff  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    sp.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def _codegraph_available() -> bool:
    return shutil.which("codegraph") is not None


CODEGRAPH_SKIP = pytest.mark.skipif(
    not _codegraph_available(), reason="本机无 codegraph CLI，跳过依赖它的测试")


# ── mrdiff：git diff 提取（不依赖 codegraph）───────────────────

class TestMrDiff:
    def test_collect_modify_hunk(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        src = repo / "src"
        src.mkdir()
        f = src / "foo.c"
        f.write_text(
            "int helper(int x) {\n    return x + 1;\n}\n\n"
            "int add(int a, int b) {\n    int r = helper(a);\n    return r + b;\n}\n",
            encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")

        f.write_text(
            "int helper(int x) {\n    return x + 1;\n}\n\n"
            "int add(int a, int b) {\n    int r = helper(a);\n    return r + b + 1;\n}\n",
            encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "modify add")

        diffs, diff_text = mrdiff.collect_file_diffs(repo, "HEAD~1", "HEAD")
        assert len(diffs) == 1
        fd = diffs[0]
        assert fd.file == "src/foo.c"
        assert 7 in fd.changed_lines   # `return r + b + 1;` 落在第 7 行
        assert diff_text.strip() != ""

    def test_collect_no_changes_returns_empty(self, tmp_path):
        repo = tmp_path / "repo2"
        _init_repo(repo)
        (repo / "a.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        diffs, diff_text = mrdiff.collect_file_diffs(repo, "HEAD", "HEAD")
        assert diffs == [] and diff_text == ""

    def test_pure_deletion_hunk_keeps_affected_line(self, tmp_path):
        """纯删除 hunk（new_count=0）应取删除位置那一行作为受影响行，不整体丢弃。"""
        repo = tmp_path / "repo3"
        _init_repo(repo)
        f = repo / "b.c"
        f.write_text("int a(void){return 1;}\nint b(void){return 2;}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        f.write_text("int a(void){return 1;}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "delete b")
        diffs, _ = mrdiff.collect_file_diffs(repo, "HEAD~1", "HEAD")
        assert len(diffs) == 1
        assert diffs[0].changed_lines  # 非空，纯删除也要记一行


# ── callgraph：真实 CodeGraph CLI（行区间反查 + BFS + 分批）────

@CODEGRAPH_SKIP
class TestCallGraph:
    def _mk_indexed_project(self, tmp_path: Path) -> Path:
        root = tmp_path / "proj"
        src = root / "src"
        src.mkdir(parents=True)
        (src / "foo.c").write_text(
            "int helper(int x) {\n"          # 1
            "    return x + 1;\n"            # 2
            "}\n"                            # 3
            "\n"                             # 4
            "int add(int a, int b) {\n"      # 5
            "    int r = helper(a);\n"       # 6
            "    return r + b;\n"            # 7
            "}\n"                            # 8
            "\n"                             # 9
            "int main(int argc, char **argv) {\n"  # 10
            "    int r = add(1, 2);\n"       # 11
            "    return r;\n"                # 12
            "}\n",                           # 13
            encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
        result = sp.run(["codegraph", "init"], cwd=str(root), capture_output=True,
                        text=True, timeout=60)
        assert result.returncode == 0, f"codegraph init 失败: {result.stderr}"
        assert callgraph.is_indexed(root)
        return root

    def test_functions_covering_lines(self, tmp_path):
        root = self._mk_indexed_project(tmp_path)
        ranges = callgraph.functions_covering_lines(root, "src/foo.c", [7])
        assert len(ranges) == 1
        assert ranges[0].name == "add"
        assert ranges[0].start_line == 5 and ranges[0].end_line == 8

    def test_functions_covering_lines_no_hit(self, tmp_path):
        """全局作用域外的行（比如文件末尾空行之外，或不存在的行号）应返回空。"""
        root = self._mk_indexed_project(tmp_path)
        ranges = callgraph.functions_covering_lines(root, "src/foo.c", [9999])
        assert ranges == []

    def test_not_indexed_raises(self, tmp_path):
        root = tmp_path / "not_indexed"
        root.mkdir()
        assert not callgraph.is_indexed(root)
        with pytest.raises(callgraph.CodeGraphNotAvailable):
            callgraph.functions_covering_lines(root, "src/foo.c", [1])

    def test_trace_to_entrypoints_found(self, tmp_path):
        root = self._mk_indexed_project(tmp_path)
        result = callgraph.trace_to_entrypoints(root, "helper", entrypoints=["main"])
        assert result.resolved and result.found
        assert result.paths[0].path[0] == "main"
        assert result.paths[0].path[-1] == "helper"

    def test_trace_to_entrypoints_target_is_entry(self, tmp_path):
        root = self._mk_indexed_project(tmp_path)
        result = callgraph.trace_to_entrypoints(root, "main", entrypoints=["main"])
        assert result.found and result.paths[0].path == ["main"]

    def test_trace_to_entrypoints_unknown_symbol(self, tmp_path):
        root = self._mk_indexed_project(tmp_path)
        result = callgraph.trace_to_entrypoints(root, "totally_unknown_fn_xyz", entrypoints=["main"])
        assert result.resolved is False and result.found is False

    def test_split_batches_file_strategy(self):
        changed = [("a.c", "f1"), ("a.c", "f2"), ("b.c", "f3")]
        batches, unreachable = callgraph.split_batches(changed, strategy="file")
        assert unreachable == []
        assert len(batches) == 2
        assert sorted(len(b) for b in batches) == [1, 2]

    def test_split_batches_size_strategy(self):
        changed = [("a.c", f"f{i}") for i in range(7)]
        batches, unreachable = callgraph.split_batches(changed, strategy="size", batch_size=3)
        assert unreachable == []
        assert [len(b) for b in batches] == [3, 3, 1]

    def test_split_batches_chain_strategy(self, tmp_path):
        root = self._mk_indexed_project(tmp_path)
        changed = [("src/foo.c", "helper"), ("src/foo.c", "add")]
        batches, unreachable = callgraph.split_batches(
            changed, strategy="chain", source_path=root, entrypoints=["main"])
        assert unreachable == []
        # helper 和 add 同在一条从 main 出发的链路上（add 是路径中第二近节点），
        # 应聚成一批
        assert len(batches) == 1 and len(batches[0]) == 2

    def test_split_batches_chain_requires_entrypoints(self):
        with pytest.raises(ValueError):
            callgraph.split_batches([("a.c", "f1")], strategy="chain")


# ── diffextract：组合 mrdiff + callgraph 归因 ──────────────────

@CODEGRAPH_SKIP
class TestDiffExtract:
    def _mk_repo_with_change(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        src = root / "src"
        src.mkdir(parents=True)
        f = src / "foo.c"
        f.write_text(
            "int helper(int x) {\n    return x + 1;\n}\n\n"
            "int add(int a, int b) {\n    int r = helper(a);\n    return r + b;\n}\n\n"
            "int main(int argc, char **argv) {\n    int r = add(1, 2);\n    return r;\n}\n",
            encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
        r = sp.run(["codegraph", "init"], cwd=str(root), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0

        f.write_text(
            "int helper(int x) {\n    return x + 1;\n}\n\n"
            "int add(int a, int b) {\n    int r = helper(a);\n    return r + b + 1;\n}\n\n"
            "int main(int argc, char **argv) {\n    int r = add(1, 2);\n    return r;\n}\n",
            encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "modify add")
        return root

    def test_extract_happy_path(self, tmp_path):
        root = self._mk_repo_with_change(tmp_path)
        ex = diffextract.extract(root, "HEAD~1", "HEAD")
        assert len(ex.functions) == 1
        fn = ex.functions[0]
        assert fn.bare_name == "add"
        assert fn.resolution == diffextract.RESOLUTION_CODEGRAPH
        assert fn.as_target() == ("src/foo.c", "add")
        assert ex.unresolved_files == []
        d = ex.to_dict()
        assert d["counts"]["trusted"] == 1 and d["counts"]["conflict"] == 0

    def test_extract_no_diff(self, tmp_path):
        root = self._mk_repo_with_change(tmp_path)
        ex = diffextract.extract(root, "HEAD", "HEAD")
        assert ex.functions == [] and ex.file_diffs == []

    def test_extract_unresolved_when_line_outside_any_function(self, tmp_path, monkeypatch):
        """改动行落在全局作用域（不在任何函数体内）→ 归入 unresolved_files，
        不猜测函数名。"""
        root = self._mk_repo_with_change(tmp_path)

        def fake_collect(_root, _base, _head, **kw):
            from aicoverage.mrdiff import FileDiff
            return [FileDiff(file="src/foo.c", changed_lines=[9999], hunk_hints=[])], "fake diff"

        monkeypatch.setattr(diffextract, "collect_file_diffs", fake_collect)
        ex = diffextract.extract(root, "HEAD~1", "HEAD")
        assert ex.functions == []
        assert ex.unresolved_files == ["src/foo.c"]

    def test_extract_conflict_when_hint_mismatches(self, tmp_path, monkeypatch):
        """CodeGraph 行区间反查结果与 hunk header 提示的函数名完全不交叉
        → 标记 conflict，不进 trusted 分母。"""
        root = self._mk_repo_with_change(tmp_path)

        def fake_collect(_root, _base, _head, **kw):
            from aicoverage.mrdiff import FileDiff
            # 行 7 真实属于 add()，但伪造一个不相关的 hunk 提示 "totally_other_fn"
            return ([FileDiff(file="src/foo.c", changed_lines=[7],
                              hunk_hints=["int totally_other_fn(void) {"])],
                    "fake diff")

        monkeypatch.setattr(diffextract, "collect_file_diffs", fake_collect)
        ex = diffextract.extract(root, "HEAD~1", "HEAD")
        assert len(ex.functions) == 1
        fn = ex.functions[0]
        assert fn.bare_name == "add"
        assert fn.resolution == diffextract.RESOLUTION_CONFLICT
        assert "totally_other_fn" in fn.note
        assert ex.conflict_functions == [fn]
        assert ex.trusted_functions == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
