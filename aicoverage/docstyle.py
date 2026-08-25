"""Deterministic test doc-header gate (static AST parsing, zero LLM token cost).

Requires every `test_*` function's docstring to contain two fields:
  - 描述 (description): one sentence on what behavior this case verifies (for
    reviewers unfamiliar with the source)
  - 测试点 (test point): the corresponding source location/branch condition (kept
    consistent with harness.print_test_point_box()'s `what` arg, so static review
    can cross-check against runtime logs)

This is a deterministic pre-gate, complementary to verify-agent's semantic review
(V1-V5: whether assertions are correct, whether atomicity is respected, etc.):
this check is pure format validation with no semantic judgment and no LLM token
cost. It runs automatically in loop.py's static-review phase and is merged into
`verify_report.json` (problem code EC-07).

Design rationale (2026-08-24): previously a case's "auditability" depended entirely
on print_test_point_box()'s runtime output -- reviewers had to run pytest and read
logs to know what each case tested. This module moves that info forward into the
source docstring so review can happen statically (no execution).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# Allow full/half-width Chinese colons, plus optional English aliases
_DESC_RE = re.compile(r"(描述|Description)\s*[:：]\s*\S")
_POINT_RE = re.compile(r"(测试点|Test\s*Point)\s*[:：]\s*\S")

REQUIRED_FIELDS = ("描述", "测试点")

EC_MISSING_DOC = "EC-07"


def check_file(path: Path) -> list[dict]:
    """Check each `test_*` function's docstring in one test file for 描述/测试点.

    Returns verify_report.json-compatible problem dicts (can be directly extended
    into the `problems` array). Parse failures (syntax errors) are also reported as
    an error for gen-agent to fix -- not silently skipped, so a broken file never
    escapes the checker undetected.
    """
    problems: list[dict] = []
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [{
            "ec": EC_MISSING_DOC, "severity": "error", "file": path.name,
            "function": "", "line_hint": e.lineno or 1,
            "detail": f"文件语法错误，无法做文档头检查: {e.msg}",
            "fix_suggestion": "先修复语法错误",
        }]
    except OSError:
        return []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        doc = ast.get_docstring(node) or ""
        field_checks = (("描述", _DESC_RE), ("测试点", _POINT_RE))
        missing = [field for field, rx in field_checks if not rx.search(doc)]
        if missing:
            no_doc_note = "（完全没有 docstring）" if not doc.strip() else ""
            problems.append({
                "ec": EC_MISSING_DOC,
                "severity": "error",
                "file": path.name,
                "function": node.name,
                "line_hint": node.lineno,
                "detail": f"用例 docstring 缺少必需字段: {', '.join(missing)}{no_doc_note}",
                "fix_suggestion": (
                    '函数体首行加 docstring，格式（描述 + 测试点缺一不可）：\n'
                    '    """\n'
                    '    描述：<一句话说明这个用例验证什么行为>\n'
                    '    测试点：<对应源码位置 file:line 与具体分支/条件，'
                    '与 print_test_point_box() 的 what 参数一致>\n'
                    '    """'
                ),
            })
    return problems


def check_test_docstrings(test_dir: Path, filenames: list[str] | None = None) -> list[dict]:
    """Check doc headers of all `test_*.py` cases under `test_dir` (or the given `filenames`).

    When `filenames` is None, scan the whole dir (for legacy regression/health checks);
    when a specific filename list is passed, only those files are checked (for the loop's
    "only review this round's new/modified files", so legacy-file problems don't block the
    current round).
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
