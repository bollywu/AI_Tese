"""脚手架：在目标项目中生成 aicoverage.toml + tests/ harness。

模板以字符串形式内嵌（避免 package-data 安装路径问题）。
harness.py 是"原子函数 → 用例搭积木"方法论的落地载体：
用例只能调 harness 原子函数，新验证维度先扩展 harness。
"""
from __future__ import annotations

from pathlib import Path

CONFIG_TEMPLATE = """\
# AIcoverage 项目配置 — 一个 TOML 描述一个被测 C/C++ 项目
# 文档：https://github.com/yourorg/AIcoverage（示例）
[project]
name = "{name}"
display_name = "{name}"
language = "{language}"
description = ""

[source]
path = "."
# 参与覆盖率统计/函数提取的源文件（glob，相对 source.path）
include_globs = ["src/**/*.c", "src/**/*.cc", "src/**/*.cpp", "src/**/*.cxx"]
exclude_globs = ["deps/**", "third_party/**", "tests/**"]

[build]
# 插桩构建命令：必须让编译器生成 .gcno（即 -fprofile-arcs -ftest-coverage / --coverage）
clean_cmd = ""
build_cmd = "{build_cmd}"
binary = "{binary}"

[test]
dir = "tests"          # pytest 用例目录（相对 source.path）
python = "auto"        # 跑 pytest 的解释器；auto=自动探测
timeout = 600          # 单次 pytest 整体超时（秒，必须 >0）

[coverage]
tool = "gcov"
gcov_bin = "gcov"
func_target = 100.0
cond_target = 85.0

[loop]
max_iter = 6
no_progress_stop = 2

[llm]
model = "your-model-name"  # 必填：所用 Agent SDK 支持的模型名
gen_model = ""         # 留空 = 同 model
max_turns = 80

[knowledge]            # 全部可选
kb_dir = ""            # 项目测试知识库（Markdown）
badcase_dir = ""       # 已废弃：badcase 自动沉淀于 .aicoverage/badcases.md
few_shots_dir = ""
prompts_dir = ""       # 整份覆盖内置 prompts/<agent>.md

[guard]                # 额外命令黑名单（正则，hooks 硬拦截）
blocked_commands = []

[codegraph]             # 可选：MR 增量覆盖闭环用（调用链分析/diff 行归因）
enabled = false
index_dir = ".codegraph"     # `codegraph init` 产物目录（相对 source.path）
entrypoints = ["main"]        # 反向调用链 BFS 的入口锚点（裸函数名）；
                              # 库类项目填驱动程序的 main，而非被测库导出函数

[scan]                  # 可选：MR 扫描轨后端
backend = "auto"             # auto | ocr | agent | off
                              # ocr: open-code-review（需已安装 ocr CLI 并配置 LLM，
                              #      npm i -g @alibaba-group/open-code-review）
                              # agent: 自研 scan-agent（纯本地 LLM 聚焦扫描）
                              # auto: ocr 可用则用之，否则降级 agent
"""

CONFTEST_TEMPLATE = '''\
"""AIcoverage 测试脚手架 conftest（可按项目需要扩展）。"""
import os
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(os.environ.get("AICOV_SRC", Path(__file__).resolve().parent.parent))
TESTS_LIB = Path(__file__).resolve().parent / "lib"
if str(TESTS_LIB) not in sys.path:
    sys.path.insert(0, str(TESTS_LIB))


@pytest.fixture(scope="session")
def target() -> Path:
    """被测插桩二进制路径。"""
    binary = os.environ.get("AICOV_BINARY", "")
    if binary:
        p = Path(binary)
    else:
        # 回退：常见命名约定
        candidates = [SRC_ROOT / name for name in
                      ("wrk", "app", "main", "bin/app")]
        p = next((c for c in candidates if c.exists()), SRC_ROOT)
    if not p.exists():
        pytest.skip(f"被测二进制不存在: {p}（先 aicov build）")
    return p


@pytest.fixture(scope="session")
def src_root() -> Path:
    return SRC_ROOT
'''

HARNESS_TEMPLATE = r'''"""harness — 测试原子函数库（"原子函数 → 用例搭积木"的载体）。

用例铁律：
  用例体只做三件事：构造数据 → 调原子函数 → 把返回值传给断言原子函数。
  需要新的验证维度/打印信息时，**先扩展本文件**，再让用例调用。

每个 test_* 函数的 docstring 必须含"描述"+"测试点"两个字段（供人工静态审查，
不用跑 pytest 看日志就能看懂用例目的；由 aicoverage.docstyle 模块自动校验，
EC-07）。docstring 内容示例（两个字段各占一行）：

    描述：<一句话说明这个用例验证什么行为>
    测试点：<对应源码位置 file:line 与具体分支，与下面 print_test_point_box
            的 what 参数一致>

执行可审计三要素（gen-agent 生成的用例必须遵守）：
  1. print_test_point_box(...)  打印测试点（测什么/输入/预期）
  2. manual_step(...)           打印关键步骤的 call/expected/observed（要打真实观测值）
  3. assert_* 原子函数          打印 expected vs observed 再断言
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SRC_ROOT = Path(os.environ.get("AICOV_SRC", Path(__file__).resolve().parents[2]))


# ── 运行被测目标 ──────────────────────────────────────────────

@dataclass
class ProcResult:
    cmd: list
    rc: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def stdout_lines(self, pattern: str = "") -> list[str]:
        """按可选正则过滤 stdout 行（只做数据提取，断言仍走 assert_*）。"""
        lines = self.stdout.splitlines()
        if pattern:
            rx = re.compile(pattern)
            return [ln for ln in lines if rx.search(ln)]
        return lines


def run_binary(args, *, stdin: str | None = None, timeout: int = 30,
               env_extra: dict | None = None, cwd: str | None = None) -> ProcResult:
    """运行被测插桩二进制（路径取 AICOV_BINARY），返回 ProcResult。"""
    binary = os.environ.get("AICOV_BINARY", "")
    if not binary:
        raise RuntimeError("环境变量 AICOV_BINARY 未设置（应由 aicov 执行器注入）")
    cmd = [binary, *[str(a) for a in args]]
    env = dict(os.environ)
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    start = time.time()
    proc = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True,
        timeout=timeout, env=env, cwd=cwd or str(SRC_ROOT),
    )
    return ProcResult(cmd=cmd, rc=proc.returncode, stdout=proc.stdout,
                      stderr=proc.stderr,
                      duration_ms=int((time.time() - start) * 1000))


# ── 本地测试服务（网络类用例一律自起回环服务，禁止连外网） ────

class _EchoHandler(BaseHTTPRequestHandler):
    delay = 0.0
    status = 200
    body = b"ok"

    def do_GET(self):
        if self.delay:
            time.sleep(self.delay)
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):  # 静默访问日志
        pass


def local_server(port: int = 0, *, delay: float = 0.0, status: int = 200,
                 body: bytes = b"ok") -> tuple[HTTPServer, str]:
    """起一个本地回环 HTTP 服务，返回 (server, "127.0.0.1:port")。

    用例结束后应 server.shutdown()；建议配合 fixture 使用：
        server, addr = local_server()
        yield addr
        server.shutdown()
    """
    handler = type("H", (_EchoHandler,), {"delay": delay, "status": status, "body": body})
    srv = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_address[1]}"


def free_port() -> int:
    """获取一个空闲本地端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── 输入构造 ─────────────────────────────────────────────────

def make_tmp_file(content: str, suffix: str = ".txt") -> Path:
    """把内容写进临时文件，返回路径（会话结束后由系统清理）。"""
    f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                    encoding="utf-8", dir=str(SRC_ROOT / ".aicoverage"))
    f.write(content)
    f.close()
    return Path(f.name)


# ── 可审计打印 ───────────────────────────────────────────────

def print_test_point_box(what: str, input_desc: str, expected: str) -> None:
    """打印测试点方框（测什么/输入/预期）。"""
    line = "─" * 66
    print(f"\n┌{line}┐")
    for label, val in (("测什么", what), ("输入", input_desc), ("预期", expected)):
        text = str(val)
        while text:
            chunk, text = text[:62], text[62:]
            print(f"│ {label}: {chunk:<60s} │")
    print(f"└{line}┘", flush=True)


def manual_step(name: str, *, call: str, side_effect: str, expected: str,
                observed: str) -> None:
    """打印一步关键操作的真实观测（observed 必须是真实输出，不能只打 True/False）。"""
    print(f"  [step] {name}")
    print(f"         call:       {call}")
    print(f"         side_effect:{side_effect}")
    print(f"         expected:   {expected}")
    print(f"         observed:   {observed}", flush=True)


# ── 断言原子函数（打印 expected vs observed 再断言） ─────────

def assert_exit_code(res: ProcResult, expected: int) -> None:
    print(f"  assert_exit_code: expected={expected} observed={res.rc}")
    assert res.rc == expected, f"退出码不符: expected={expected} observed={res.rc}"


def assert_exit_code_ne(res: ProcResult, unexpected: int) -> None:
    print(f"  assert_exit_code_ne: unexpected={unexpected} observed={res.rc}")
    assert res.rc != unexpected, f"退出码不应为 {unexpected}"


def assert_stdout_contains(res: ProcResult, needle: str) -> None:
    hit = needle in res.stdout
    print(f"  assert_stdout_contains: needle={needle!r} hit={hit}")
    assert hit, f"stdout 未包含 {needle!r}；stdout 前 500 字符:\n{res.stdout[:500]}"


def assert_stderr_contains(res: ProcResult, needle: str) -> None:
    hit = needle in res.stderr
    print(f"  assert_stderr_contains: needle={needle!r} hit={hit}")
    assert hit, f"stderr 未包含 {needle!r}；stderr 前 500 字符:\n{res.stderr[:500]}"


def assert_stdout_matches(res: ProcResult, pattern: str) -> None:
    rx = re.compile(pattern)
    hit = rx.search(res.stdout)
    print(f"  assert_stdout_matches: pattern={pattern!r} hit={bool(hit)}")
    assert hit, f"stdout 不匹配 {pattern!r}；stdout 前 500 字符:\n{res.stdout[:500]}"


def assert_eq(actual, expected, *, label: str = "") -> None:
    print(f"  assert_eq{f'[{label}]' if label else ''}: expected={expected!r} observed={actual!r}")
    assert actual == expected, f"{label or '值'}不符: expected={expected!r} observed={actual!r}"


def assert_gt(actual, threshold, *, label: str = "") -> None:
    print(f"  assert_gt{f'[{label}]' if label else ''}: threshold={threshold!r} observed={actual!r}")
    assert actual > threshold, f"{label or '值'}应大于 {threshold!r}，实际 {actual!r}"


def assert_duration_lt(res: ProcResult, seconds: float) -> None:
    actual = res.duration_ms / 1000.0
    print(f"  assert_duration_lt: threshold={seconds}s observed={actual:.2f}s")
    assert actual < seconds, f"耗时 {actual:.2f}s 超过 {seconds}s"
'''


def scaffold(source: Path, *, name: str, build_cmd: str, binary: str,
             language: str = "c") -> None:
    """在目标项目生成配置 + tests/ harness 脚手架。"""
    config = CONFIG_TEMPLATE.format(name=name, language=language,
                                    build_cmd=build_cmd, binary=binary)
    (source / "aicoverage.toml").write_text(config, encoding="utf-8")

    tests = source / "tests"
    (tests / "lib").mkdir(parents=True, exist_ok=True)
    (tests / "conftest.py").write_text(CONFTEST_TEMPLATE, encoding="utf-8")
    (tests / "lib" / "harness.py").write_text(HARNESS_TEMPLATE, encoding="utf-8")
    (tests / "lib" / "__init__.py").write_text("", encoding="utf-8")

    # .aicoverage 工作区 + gitignore
    (source / ".aicoverage").mkdir(exist_ok=True)
    gi = source / ".gitignore"
    entry = ".aicoverage/\n*.gcda\n*.gcno\n*.gcov.json*\n"
    if gi.exists():
        text = gi.read_text(encoding="utf-8")
        if ".aicoverage/" not in text:
            gi.write_text(text.rstrip("\n") + "\n" + entry, encoding="utf-8")
    else:
        gi.write_text(entry, encoding="utf-8")
