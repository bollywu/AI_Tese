"""assertquality 确定性门禁单测：EC-08 恒真/弱断言 + EC-10 issue 绑定。

每条检测规则对应一个用例；同时验证与 docstyle 相同的接入契约
（check_assert_quality(test_dir, filenames) → verify_report 兼容的 problems）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.assertquality import (  # noqa: E402
    EC_ISSUE_UNBOUND, EC_WEAK_ASSERT, check_assert_quality, check_file,
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _probs(path: Path) -> list[dict]:
    return check_file(path)


class TestWeakAssertions:
    def test_no_assertion_at_all(self, tmp_path):
        """只打印不断言 → EC-08。"""
        p = _write(tmp_path, "test_a.py", '''
def test_only_prints():
    """
    描述：x
    测试点：y
    """
    print("running")
    run_binary(["--flag"])
''')
        probs = _probs(p)
        assert any(x["ec"] == EC_WEAK_ASSERT and "没有任何断言" in x["detail"] for x in probs)

    def test_tiny_needle_contains(self, tmp_path):
        """assert_stdout_contains(res, "e") 子串过短 → EC-08。"""
        p = _write(tmp_path, "test_b.py", '''
def test_tiny():
    res = run_binary(["--flag"])
    assert_stdout_contains(res, "e")
    assert_stdout_contains(res, "---")
''')
        probs = _probs(p)
        details = " ".join(x["detail"] for x in probs if x["ec"] == EC_WEAK_ASSERT)
        assert "过短" in details and "'e'" in details
        assert "'---'" in details  # 纯标点同样命中

    def test_meaningful_needle_passes(self, tmp_path):
        """有区分度的匹配串不报。"""
        p = _write(tmp_path, "test_c.py", '''
def test_ok():
    res = run_binary(["--flag"])
    assert_stdout_contains(res, "Latency Distribution")
''')
        assert _probs(p) == []

    def test_exit_code_ne_nonzero(self, tmp_path):
        """assert_exit_code_ne(res, 1) 几乎恒真 → EC-08；ne 0 是有信息量的，不报。"""
        p = _write(tmp_path, "test_d.py", '''
def test_ne():
    res = run_binary(["--flag"])
    assert_exit_code_ne(res, 1)
    assert_exit_code_ne(res, 0)
''')
        probs = _probs(p)
        assert any("exit_code_ne" in x["detail"] for x in probs if x["ec"] == EC_WEAK_ASSERT)
        assert len([x for x in probs if "exit_code_ne" in x["detail"]]) == 1

    def test_gt_negative_threshold(self, tmp_path):
        """assert_gt(x, -1) 恒真 → EC-08；gt(x, 0) 有信息量不报。"""
        p = _write(tmp_path, "test_e.py", '''
def test_gt():
    res = run_binary(["--flag"])
    assert_gt(res.rc, -1)
    assert_gt(res.rc, 0)
''')
        probs = _probs(p)
        assert any("阈值为负" in x["detail"] for x in probs if x["ec"] == EC_WEAK_ASSERT)
        assert len(probs) == 1

    def test_matches_anything_pattern(self, tmp_path):
        """assert_stdout_matches(res, ".*") 匹配任意串 → EC-08；锚定关键字的正则不报。"""
        p = _write(tmp_path, "test_f.py", '''
def test_rx():
    res = run_binary(["--flag"])
    assert_stdout_matches(res, "^.*$")
    assert_stdout_matches(res, "Latency\\\\s+[0-9.]+")
''')
        probs = _probs(p)
        assert any("匹配任意字符串" in x["detail"] for x in probs if x["ec"] == EC_WEAK_ASSERT)
        assert len(probs) == 1

    def test_assert_eq_identical_exprs(self, tmp_path):
        """assert_eq(a, a) 两侧同表达式恒真 → EC-08。"""
        p = _write(tmp_path, "test_g.py", '''
def test_eq():
    n = 3
    assert_eq(n, n)
''')
        probs = _probs(p)
        assert any("完全相同" in x["detail"] for x in probs if x["ec"] == EC_WEAK_ASSERT)

    def test_bare_assert_true(self, tmp_path):
        """裸 assert True 恒真 → EC-08。"""
        p = _write(tmp_path, "test_h.py", '''
def test_assert_true():
    assert True
''')
        probs = _probs(p)
        assert any("常量真值" in x["detail"] for x in probs if x["ec"] == EC_WEAK_ASSERT)

    def test_non_test_functions_ignored(self, tmp_path):
        """helper 函数（非 test_ 前缀）内的弱断言不报。"""
        p = _write(tmp_path, "test_i.py", '''
def _helper():
    assert True
    return 1

def test_ok():
    res = run_binary(["--flag"])
    assert_exit_code(res, 0)
''')
        assert _probs(p) == []

    def test_syntax_error_reported(self, tmp_path):
        """语法错误 → 报 EC-08 error（不静默跳过）。"""
        p = _write(tmp_path, "test_j.py", "def test_bad(:\n")
        probs = _probs(p)
        assert probs and probs[0]["ec"] == EC_WEAK_ASSERT
        assert "语法错误" in probs[0]["detail"]


class TestIssueBinding:
    def test_bug_file_requires_issue_id(self, tmp_path):
        """test_bug_*.py 的用例 docstring 缺 issue_id 字段 → EC-10。"""
        p = _write(tmp_path, "test_bug_issue01.py", '''
def test_issue01_repro():
    """
    描述：复现
    测试点：x
    """
    res = run_binary(["--flag"])
    assert_exit_code(res, 0)
''')
        probs = _probs(p)
        assert any(x["ec"] == EC_ISSUE_UNBOUND for x in probs)

    def test_bug_file_with_issue_id_passes(self, tmp_path):
        p = _write(tmp_path, "test_bug_issue01.py", '''
def test_issue01_repro():
    """
    描述：复现
    测试点：x
    issue_id: ISSUE-01
    """
    res = run_binary(["--flag"])
    assert_exit_code(res, 0)
''')
        assert _probs(p) == []

    def test_normal_file_no_issue_id_required(self, tmp_path):
        """普通用例文件不要求 issue_id（仅扫描轨复现用例要求）。"""
        p = _write(tmp_path, "test_normal.py", '''
def test_ok():
    res = run_binary(["--flag"])
    assert_exit_code(res, 0)
''')
        assert _probs(p) == []


class TestDirectoryContract:
    def test_filenames_filter(self, tmp_path):
        """传 filenames 只查指定文件（与 docstyle.check_test_docstrings 契约一致）。"""
        _write(tmp_path, "test_bad.py", "def test_x():\n    print('a')\n")
        _write(tmp_path, "test_good.py",
               "def test_y():\n    res = run_binary([])\n    assert_exit_code(res, 0)\n")
        probs = check_assert_quality(tmp_path, ["test_good.py"])
        assert probs == []
        probs = check_assert_quality(tmp_path, ["test_bad.py"])
        assert any("没有任何断言" in x["detail"] for x in probs)

    def test_missing_file_skipped(self, tmp_path):
        assert check_assert_quality(tmp_path, ["ghost.py"]) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
