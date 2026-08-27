"""Deterministic assertion-quality gate (static AST parsing, zero LLM token cost).

Complements docstyle.py's doc-header gate (EC-07) by checking the *assertions
themselves*: a case whose assertions are tautological or missing verifies nothing
yet shows up green -- the highest-priority false-positive source (real incident:
ModSecurity SecLang cases asserted log lines with unescaped regex, hitting every
run; wrk cases sliced stdout at fixed columns). All checks are pure AST, no LLM.

Problem codes:
  EC-08  weak/tautological assertion (severity: error) -- one of:
    1. no assert_* atomic call and no bare `assert` in the test body
    2. assert_stdout_contains / assert_stderr_contains with a needle < 3 chars
       or punctuation/whitespace-only (substring match -> near-always true)
    3. assert_exit_code_ne(res, <nonzero literal>) -- rc almost never equals an
       arbitrary nonzero constant, so the assertion carries no information
    4. assert_gt(x, <negative literal>) -- always true for non-negative values
    5. assert_stdout_matches pattern that matches anything (wildcards/anchors only)
    6. assert_eq(a, b) where both args are the identical expression (tautology)
    7. bare `assert <constant truthy>` statements (assert True / assert 1)
  EC-10  scan-track binding: every test_* function in a test_bug_*.py file must
         carry an `issue_id:` docstring field so the executor's per-case results
         can be attributed to the exact issue during four-state adjudication
         (without it, adjudication falls back to inconclusive instead of guessing).

Integration: loop.py (coverage track, verify phase) and scanverify.py (scan track,
S3) run check_assert_quality over this round's manifest files and merge the
problems into verify_report.json -- the existing gen fix-loop then repairs them.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

EC_WEAK_ASSERT = "EC-08"
EC_ISSUE_UNBOUND = "EC-10"

# needle consisting only of non-alphanumeric chars (punctuation/whitespace) is
# near-always present in any non-empty output
_PUNCT_ONLY = re.compile(r"^[\W_]+$")
# docstring field "issue_id: ISSUE-XX" (half/full-width colon, EN aliases allowed)
_ISSUE_ID_RE = re.compile(r"issue[_\s]?id\s*[:：]\s*\S+", re.IGNORECASE)

# minimum meaningful needle length for substring assertions
_MIN_NEEDLE_LEN = 3

# assertion atomic functions whose needle/pattern/threshold args are checkable
_CONTAINS_FUNCS = {"assert_stdout_contains", "assert_stderr_contains"}
_MATCHES_FUNCS = {"assert_stdout_matches", "assert_stderr_matches"}


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _const(node: ast.AST):
    """Return the constant value when the node is a literal (incl. negative
    numbers, which parse as UnaryOp(USub, Constant)), else None."""
    if isinstance(node, ast.Constant):
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, (int, float))
            and not isinstance(node.operand.value, bool)):
        return -node.operand.value
    return None


def _matches_anything(pattern: str) -> bool:
    """Whether a regex pattern matches every string (wildcards/anchors/flags only).

    e.g. ".*", "^.*$", "(?s).*" match anything; "Latency.*" does not (residual
    "Latency" left after stripping wildcard tokens).
    """
    s = pattern.strip()
    if not s:
        return False
    residual = s
    for token in ("(?s)", "(?m)", "(?i)", "(?x)", ".*?", ".*", "."):
        residual = residual.replace(token, "")
    residual = residual.strip("^").strip("$")
    return residual == ""


def _problem(ec: str, func_node: ast.FunctionDef, filename: str,
             detail: str, fix: str) -> dict:
    return {
        "ec": ec, "severity": "error", "file": filename,
        "function": func_node.name, "line_hint": func_node.lineno,
        "detail": detail, "fix_suggestion": fix,
    }


def _check_assert_call(node: ast.Call, name: str, fn: ast.FunctionDef,
                       filename: str) -> list[dict]:
    """Check one assert_* atomic call for tautological/weak argument patterns."""
    problems: list[dict] = []
    args = node.args

    # 2. substring assertions with a trivially-present needle
    if name in _CONTAINS_FUNCS and len(args) >= 2:
        needle = _const(args[1])
        if isinstance(needle, str):
            if len(needle.strip()) < _MIN_NEEDLE_LEN or _PUNCT_ONLY.match(needle.strip()):
                problems.append(_problem(
                    EC_WEAK_ASSERT, fn, filename,
                    f"{name} 的匹配串过短/无信息量: {needle!r}——子串匹配下几乎恒真",
                    f"改为断言一段有区分度的输出片段（≥{_MIN_NEEDLE_LEN} 个非标点字符）"))

    # 3. assert_exit_code_ne(res, <nonzero literal>) carries no information
    if name == "assert_exit_code_ne" and len(args) >= 2:
        unexpected = _const(args[1])
        if isinstance(unexpected, int) and not isinstance(unexpected, bool) and unexpected != 0:
            problems.append(_problem(
                EC_WEAK_ASSERT, fn, filename,
                f"assert_exit_code_ne(res, {unexpected})——退出码几乎不可能恰好等于该值，断言恒真",
                "改为 assert_exit_code(res, <预期值>) 或 assert_exit_code_ne(res, 0)"))

    # 4. assert_gt(x, <negative literal>) is always true for non-negative values
    if name == "assert_gt" and len(args) >= 2:
        threshold = _const(args[1])
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and threshold < 0:
            problems.append(_problem(
                EC_WEAK_ASSERT, fn, filename,
                f"assert_gt(x, {threshold})——阈值为负，对非负值恒真",
                "改为断言一个有业务含义的非负阈值（如 assert_gt(n, 0) 表示至少命中一次）"))

    # 5. regex assertions whose pattern matches anything
    if name in _MATCHES_FUNCS and len(args) >= 2:
        pattern = _const(args[1])
        if isinstance(pattern, str) and _matches_anything(pattern):
            problems.append(_problem(
                EC_WEAK_ASSERT, fn, filename,
                f"{name} 的正则 {pattern!r} 匹配任意字符串，断言恒真",
                "在 pattern 中锚定具体的关键字/字段（如 'Latency\\s+[0-9.]+'）"))

    # 6. assert_eq(a, a) -- identical expressions on both sides
    if name == "assert_eq" and len(args) >= 2:
        if ast.dump(args[0]) == ast.dump(args[1]):
            problems.append(_problem(
                EC_WEAK_ASSERT, fn, filename,
                "assert_eq 的两个参数是完全相同的表达式，断言恒真",
                "第二个参数应改为来自源码逻辑的预期值"))

    return problems


def check_file(path: Path) -> list[dict]:
    """Check one test file's assertions; returns verify_report-compatible problems.

    For files named test_bug_*.py (the scan-track repro-case convention) each
    test function's docstring must additionally carry an `issue_id:` field (EC-10).
    Parse failures surface as errors (never silently skipped), same as docstyle.
    """
    problems: list[dict] = []
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [{
            "ec": EC_WEAK_ASSERT, "severity": "error", "file": path.name,
            "function": "", "line_hint": e.lineno or 1,
            "detail": f"文件语法错误，无法做断言质量检查: {e.msg}",
            "fix_suggestion": "先修复语法错误",
        }]
    except OSError:
        return []

    require_issue_id = path.name.startswith("test_bug_")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue

        # EC-10: scan-track binding
        if require_issue_id:
            doc = ast.get_docstring(node) or ""
            if not _ISSUE_ID_RE.search(doc):
                problems.append(_problem(
                    EC_ISSUE_UNBOUND, node, path.name,
                    "复现用例 docstring 缺少 issue_id 字段——裁决无法把执行结果归因到具体 issue",
                    'docstring 加一行：issue_id: ISSUE-XX（与 scan_issues.json 的 issue_id 一致）'))

        # EC-08: assertion quality
        assert_calls = 0
        bare_asserts = 0
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                cname = _call_name(n)
                if cname.startswith("assert_"):
                    assert_calls += 1
                    problems.extend(_check_assert_call(n, cname, node, path.name))
            elif isinstance(n, ast.Assert):
                bare_asserts += 1
                # 7. bare assert on a constant truthy value
                if isinstance(n.test, ast.Constant) and bool(n.test.value):
                    problems.append(_problem(
                        EC_WEAK_ASSERT, node, path.name,
                        f"第 {n.lineno} 行出现裸 assert 常量真值（assert {n.test.value!r}），恒真",
                        "改为有信息量的断言原子函数（如 assert_exit_code / assert_stdout_contains）"))
        if assert_calls == 0 and bare_asserts == 0:
            problems.append(_problem(
                EC_WEAK_ASSERT, node, path.name,
                "用例体内没有任何断言（无 assert_* 原子函数也无裸 assert）——只打印不验证",
                "至少补一个断言原子函数（如 assert_exit_code(res, 0)）"))
    return problems


def check_assert_quality(test_dir: Path, filenames: list[str] | None = None) -> list[dict]:
    """Check assertions of all test_*.py under test_dir (or only the given filenames).

    Mirrors docstyle.check_test_docstrings' contract: filename list = check only
    this round's files; None = whole dir (legacy health check).
    """
    problems: list[dict] = []
    if filenames:
        files = [test_dir / f for f in filenames]
    else:
        files = sorted(test_dir.glob("test_*.py"))
    for f in files:
        if f.exists() and f.is_file():
            problems.extend(check_file(f))
    return problems
