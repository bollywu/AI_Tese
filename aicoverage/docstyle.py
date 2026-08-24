"""确定性用例文档头检查（静态 AST 解析，零 LLM token 成本）。

要求每个 `test_*` 函数的 docstring 必须包含两个字段：
  - 描述：一句话说明这个用例在验证什么行为（面向不熟悉源码的审查者）
  - 测试点：对应的源码位置/分支条件（与 harness.print_test_point_box() 的
    `what` 参数保持一致，便于静态审查时对照运行日志核实）

这是一道**确定性前置门禁**，与 verify-agent 的语义审查（V1-V5，判断断言是否
正确、是否遵守原子化等）互补：本检查纯格式校验、不涉及任何语义判断，不消耗
LLM token，由 `loop.py` 在静态审查阶段自动运行并把结果合并进
`verify_report.json`（问题编码 EC-07）。

设计动机（2026-08-24）：此前用例的"可审查性"完全依赖 print_test_point_box()
的**运行时输出**——审查者必须先跑一遍 pytest 看日志才知道每个用例在测什么。
本模块把这个信息前移到**源码 docstring**，静态读代码（不运行）就能审查。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# 允许中文全角/半角冒号，及可选的英文别名
_DESC_RE = re.compile(r"(描述|Description)\s*[:：]\s*\S")
_POINT_RE = re.compile(r"(测试点|Test\s*Point)\s*[:：]\s*\S")

REQUIRED_FIELDS = ("描述", "测试点")

EC_MISSING_DOC = "EC-07"


def check_file(path: Path) -> list[dict]:
    """检查单个测试文件里每个 `test_*` 函数的 docstring 是否含 描述/测试点。

    返回 verify_report.json 兼容的 problem 字典列表（可直接 extend 进
    `problems` 数组）。文件解析失败（语法错误）也报告为一条 error，交给
    gen-agent 修复——不静默跳过，避免"文件坏了但检查器没发现"。
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
    """检查 `test_dir` 下（或 `filenames` 指定的）全部 `test_*.py` 用例文档头。

    `filenames` 为 None 时扫描整个目录（用于存量回归/健康检查）；传入具体
    文件名列表时只检查这些文件（用于闭环里"只查本轮新增/修改的文件"，
    不因历史遗留文件的问题阻塞当前轮）。
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
