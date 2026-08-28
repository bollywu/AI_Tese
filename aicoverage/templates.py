"""Scaffolding: generate aicoverage.toml + tests/ harness in the target project.

Templates are embedded as strings (avoids package-data install-path issues).
harness.py is the concrete carrier of the "atomic functions -> case building blocks"
methodology: cases may only call harness atomic functions; new verification dimensions
are added by extending harness first.
"""
from __future__ import annotations

from pathlib import Path

CONFIG_TEMPLATE = """\
# AIcoverage project config -- one TOML describes one target C/C++ project
# docs: https://github.com/yourorg/AIcoverage (example)
[project]
name = "{name}"
display_name = "{name}"
language = "{language}"
description = ""

[source]
path = "."
# source files included in coverage stats / function extraction (glob, relative to source.path)
include_globs = ["src/**/*.c", "src/**/*.cc", "src/**/*.cpp", "src/**/*.cxx"]
exclude_globs = ["deps/**", "third_party/**", "tests/**"]

[build]
# instrumented build command: must make the compiler emit .gcno
# (i.e. -fprofile-arcs -ftest-coverage / --coverage)
clean_cmd = ""
build_cmd = "{build_cmd}"
binary = "{binary}"

[test]
dir = "tests"          # pytest case dir (relative to source.path)
python = "auto"        # interpreter for pytest; auto=auto-detect
timeout = 600          # per-pytest timeout (sec, must be >0)
flaky_rerun = true     # on case failure, re-run once and diff per-case status
                       # -> deterministic flaky evidence (execution.json: flaky_cases)

[coverage]
tool = "gcov"
gcov_bin = "gcov"
func_target = 100.0
cond_target = 85.0
max_unit_ratio = 0.15  # E2E-first quota: unit-covered share of newly-hit functions
                       # above this emits UNIT_RATIO_EXCEEDED + e2e-first hint to gen
bug_base_compare = false  # MR loop: re-run failing cases against base_ref in an
                          # isolated git worktree (pass@base+fail@head = regression
                          # introduced by the change). Costs one extra build, opt-in.

[unittest]              # optional: E2E-unreachable -> unit test (gap causes N1/N3/N5)
compiler = ""           # unit-test compiler (empty=follow build system; recommended gcc / g++)
flags = ["-O0", "-g", "-Wall"]   # extra unit-test compile flags (--coverage auto-appended)
link_libs = []          # extra link libs, e.g. ["-lm", "-lpthread"]
obj_dir = ".aicoverage/ut"       # unit-test intermediate dir (.gcno/.gcda land here)

[loop]
max_iter = 6
no_progress_stop = 2

[llm]
model = "your-model-name"  # required: model name supported by the Agent SDK
gen_model = ""         # empty = same as model
max_turns = 120        # max tool turns per agent call (80 was too small for complex C/C++ projects)
max_verify_retry = 3   # max verify fix-loop rounds (at 2 complex projects gen often fails in time)

[knowledge]            # all optional
kb_dir = ""            # project test knowledge base (Markdown)
badcase_dir = ""       # deprecated: badcases auto-accumulate into .aicoverage/badcases.md
few_shots_dir = ""
prompts_dir = ""       # fully override built-in prompts/<agent>.md

[guard]                # extra command blacklist (regex, hard-intercepted by hooks)
blocked_commands = []

[codegraph]             # optional: MR incremental coverage loop (call-graph/diff attribution)
enabled = false
index_dir = ".codegraph"     # `codegraph init` artifact dir (relative to source.path)
entrypoints = ["main"]        # reverse call-graph BFS entry anchors (bare function names);
                              # for library projects use the driver's main, not the lib's exports

[scan]                  # optional: MR scan-track backend
backend = "auto"             # auto | ocr | agent | off
                              # ocr: open-code-review (needs ocr CLI installed & LLM configured,
                              #      npm i -g @alibaba-group/open-code-review)
                              # agent: built-in scan-agent (pure local LLM focused scan)
                              # auto: use ocr if available, else fall back to agent
"""

# Go project config: no --coverage build / binary needed. `go test -coverprofile`
# instruments and reports statement coverage natively, so the [build] section is
# omitted and the [go] section drives the backend.
CONFIG_TEMPLATE_GO = """\
# AIcoverage project config -- Go coverage via `go test -coverprofile`
# docs: https://github.com/yourorg/AIcoverage (example)
[project]
name = "{name}"
display_name = "{name}"
language = "go"
description = ""

[source]
path = "."
# Go source files included in coverage stats / function extraction
include_globs = ["**/*.go"]
exclude_globs = ["vendor/**", "third_party/**", "tests/**", "**/*_test.go"]

[go]
go_bin = "go"
packages = ["./..."]
build_tags = ""
coverprofile = ".aicoverage/cover.out"

[test]
dir = "tests"          # Go test dir; pytest-style test_*.py not required
timeout = 600

[coverage]
func_target = 100.0
cond_target = 85.0

[loop]
max_iter = 6
no_progress_stop = 2

[llm]
model = "your-model-name"  # required: model name supported by the Agent SDK
gen_model = ""         # empty = same as model
max_turns = 120
max_verify_retry = 3

[knowledge]            # all optional
kb_dir = ""
badcase_dir = ""
few_shots_dir = ""
prompts_dir = ""

[guard]
blocked_commands = []

[codegraph]
enabled = false
index_dir = ".codegraph"
entrypoints = ["main"]

[scan]
backend = "auto"
"""

# Rust project config: no --coverage build / binary needed. `cargo llvm-cov`
# (preferred) or `cargo tarpaulin` instruments at test time and emits lcov,
# so the [build] section is omitted and the [rust] section drives the backend.
CONFIG_TEMPLATE_RUST = """\
# AIcoverage project config -- Rust coverage via `cargo llvm-cov` / tarpaulin
# docs: https://github.com/yourorg/AIcoverage (example)
[project]
name = "{name}"
display_name = "{name}"
language = "rust"
description = ""

[source]
path = "."
include_globs = ["**/*.rs"]
exclude_globs = ["target/**", "vendor/**", "tests/**"]

[rust]
cargo_bin = "cargo"
cov_tool = "llvm-cov"          # llvm-cov | tarpaulin
lcov = ".aicoverage/lcov.info"

[test]
dir = "tests"          # cargo integration-test dir
timeout = 600

[coverage]
func_target = 100.0
cond_target = 85.0

[loop]
max_iter = 6
no_progress_stop = 2

[llm]
model = "your-model-name"  # required: model name supported by the Agent SDK
gen_model = ""         # empty = same as model
max_turns = 120
max_verify_retry = 3

[knowledge]            # all optional
kb_dir = ""
badcase_dir = ""
few_shots_dir = ""
prompts_dir = ""

[guard]
blocked_commands = []

[codegraph]
enabled = false
index_dir = ".codegraph"
entrypoints = ["main"]

[scan]
backend = "auto"
"""

# Java project config: JaCoCo agent instruments at test time (maven/gradle);
# jacoco.xml is the report. No --coverage build step exists.
CONFIG_TEMPLATE_JAVA = """\
# AIcoverage project config -- Java coverage via JaCoCo (jacoco.xml)
# docs: https://github.com/yourorg/AIcoverage (example)
[project]
name = "{name}"
display_name = "{name}"
language = "java"
description = ""

[source]
path = "."
include_globs = ["**/*.java"]
exclude_globs = ["target/**", "build/**", "**/test/**"]

[java]
build_tool = "auto"          # auto | maven | gradle
mvn_bin = "mvn"
gradle_bin = "gradle"
jacoco_xml = "target/site/jacoco/jacoco.xml"   # maven 默认；gradle: build/reports/jacoco/test/jacocoTestReport.xml

[test]
dir = "tests"          # 未用（Java 测试在 src/test/java），占位
timeout = 900

[coverage]
func_target = 100.0
cond_target = 85.0

[loop]
max_iter = 6
no_progress_stop = 2

[llm]
model = "your-model-name"  # required: model name supported by the Agent SDK
gen_model = ""         # empty = same as model
max_turns = 120
max_verify_retry = 3

[knowledge]            # all optional
kb_dir = ""
badcase_dir = ""
few_shots_dir = ""
prompts_dir = ""

[guard]
blocked_commands = []

[codegraph]
enabled = false
index_dir = ".codegraph"
entrypoints = ["main"]

[scan]
backend = "auto"
"""

CONFTEST_TEMPLATE = '''\
"""AIcoverage test scaffolding conftest (extendable per project)."""
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
    """Path to the instrumented binary under test."""
    binary = os.environ.get("AICOV_BINARY", "")
    if binary:
        p = Path(binary)
    else:
        # fallback: common naming conventions
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

HARNESS_TEMPLATE = r'''"""harness -- test atomic-function library (the carrier of "atomic functions -> case building blocks").

Case iron rules:
  A case body does only three things: construct data -> call an atomic function -> pass the
  return value to an assertion atomic function.
  For a new verification dimension/print info, **extend this file first**, then let cases call it.

Every test_* function's docstring must contain the two fields 描述 + 测试点 (for manual
static review -- understand the case purpose without running pytest/logs; auto-validated by
the aicoverage.docstyle module, EC-07). Docstring example (one field per line):

    描述：<one sentence on what behavior this case verifies>
    测试点：<corresponding source location file:line and branch, kept consistent with the
            `what` arg of print_test_point_box below>

Three auditability elements (required for gen-agent-generated cases):
  1. print_test_point_box(...)  print the test point (what/input/expected)
  2. manual_step(...)           print key steps' call/expected/observed (real observed values)
  3. assert_* atomic functions  print expected vs observed, then assert
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


# ── Running the target ─────────────────────────────────────────

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
        """Filter stdout lines by an optional regex (data extraction only; assertions still go through assert_*)."""
        lines = self.stdout.splitlines()
        if pattern:
            rx = re.compile(pattern)
            return [ln for ln in lines if rx.search(ln)]
        return lines


def run_binary(args, *, stdin: str | None = None, timeout: int = 30,
               env_extra: dict | None = None, cwd: str | None = None) -> ProcResult:
    """Run the instrumented binary under test (path from AICOV_BINARY), returning ProcResult."""
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


# ── Unit-test channel (E2E-unreachable -> unit test) ─────────────
# Background: some functions cannot be reached through the binary's normal E2E flow
# (error-handling paths N3, static init, platform-specific N1/N5, etc.). Here you can
# write a test_driver_*.c that calls the target function directly, compile it into a
# unit-test binary with --coverage and run it, so gcov collects that function. Since gcov
# scans the source tree for .gcno/.gcda, this channel is fully compatible with existing
# collection.

# Common link libs (auto-tried one by one on undefined reference, saving manual link_libs config)
_COMMON_LINK_LIBS = ("-lm", "-lpthread", "-lrt", "-ldl", "-lz")


def _run_cc(cmd: list, *, timeout: int, cwd: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd)
        log = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        return proc.returncode, log
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"


def compile_unit_driver(driver_c: Path | str, sources: list[Path | str],
                        out_name: str = "ut_main",
                        include_dirs: list[Path | str] | None = None,
                        *, timeout: int = 120) -> ProcResult:
    """Compile the target source files + driver with --coverage and link into a unit-test binary.

    Args:
        driver_c: test driver source path (contains main, calls the target function directly).
        sources: target source file list (.c/.cc/.cpp where the target functions live). These
            must already be compiled with --coverage (or compiled here with
            -fprofile-arcs -ftest-coverage) -- this function always appends --coverage so
            .gcno is generated.
        out_name: output binary name (placed under AICOV_UT_OBJ_DIR).
        include_dirs: extra header search dirs (relative or absolute).

    Returns:
        ProcResult (cmd = compile command; rc=0 and the artifact existing count as success).
    """
    cc = os.environ.get("AICOV_UT_COMPILER", "gcc")
    flags = os.environ.get("AICOV_UT_FLAGS", "-O0 -g -Wall").split()
    link_libs = [x for x in os.environ.get("AICOV_UT_LINK_LIBS", "").split() if x]
    ut_dir = Path(os.environ.get("AICOV_UT_OBJ_DIR", SRC_ROOT / ".aicoverage" / "ut"))
    ut_dir.mkdir(parents=True, exist_ok=True)
    out_bin = ut_dir / out_name

    driver_p = Path(driver_c)
    if not driver_p.is_absolute():
        driver_p = SRC_ROOT / driver_p
    src_list = [str(s if Path(s).is_absolute() else SRC_ROOT / s) for s in sources]
    inc = []
    for d in (include_dirs or []):
        p = Path(d)
        inc.append(str(p if p.is_absolute() else SRC_ROOT / p))

    base_cmd = [cc, "--coverage", *flags]
    if inc:
        base_cmd += ["-I" + d for d in inc]
    base_cmd += src_list + [str(driver_p), "-o", str(out_bin)]

    start = time.time()
    cmd = [*base_cmd, *link_libs]
    rc, log = _run_cc(cmd, timeout=timeout, cwd=str(SRC_ROOT))

    # Link-failure self-healing: on "undefined reference" (missing library) in stderr, try
    # common libraries one by one (-lm/-lpthread/-lrt/-ldl/-lz); use whichever succeeds; if
    # all fail, give an explicit hint to fill [unittest] link_libs in aicoverage.toml.
    if rc != 0 and "undefined reference" in log:
        for lib in _COMMON_LINK_LIBS:
            if lib in link_libs:
                continue
            try_cmd = [*base_cmd, *link_libs, lib]
            try_rc, try_log = _run_cc(try_cmd, timeout=timeout, cwd=str(SRC_ROOT))
            if try_rc == 0:
                cmd, rc, log = try_cmd, 0, try_log
                break
    if rc != 0 and "undefined reference" in log:
        log += ("\n[提示] 链接失败通常是目标函数依赖了额外库。请在 aicoverage.toml "
                "的 [unittest] link_libs 中补齐，例如数学库加 [\"-lm\"]、线程库加 "
                "[\"-lpthread\"]（本函数已自动尝试 -lm/-lpthread/-lrt/-ldl/-lz，"
                "若仍未解决，可能是自定义/第三方静态库）。")

    if rc == 0 and not out_bin.exists():
        rc, log = 127, log + f"\n编译成功但产物不存在: {out_bin}"
    manual_step("compile_unit_driver",
                call=" ".join(cmd), side_effect=f"产物 {out_bin}",
                expected="rc=0 且产物存在", observed=f"rc={rc} 产物={'存在' if out_bin.exists() else '缺失'}")
    return ProcResult(cmd=cmd, rc=rc, stdout=log, stderr="",
                      duration_ms=int((time.time() - start) * 1000))


def run_driver(out_name: str = "ut_main", args: list | None = None, *,
               timeout: int = 60, env_extra: dict | None = None) -> ProcResult:
    """Run a compiled unit-test driver binary (see compile_unit_driver), returning ProcResult.

    The unit-test binary must be compiled by compile_unit_driver (lands under AICOV_UT_OBJ_DIR).
    Running writes .gcda, so gcov collection can hit the target functions.
    """
    ut_dir = Path(os.environ.get("AICOV_UT_OBJ_DIR", SRC_ROOT / ".aicoverage" / "ut"))
    binary = ut_dir / out_name
    if not binary.exists():
        return ProcResult(cmd=[str(binary)], rc=127, stdout="",
                          stderr=f"单测二进制不存在: {binary}（先调 compile_unit_driver）",
                          duration_ms=0)
    cmd = [str(binary), *[str(a) for a in (args or [])]]
    env = dict(os.environ)
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=env, cwd=str(SRC_ROOT))
        rc = proc.returncode
        stderr = proc.stderr or ""
        # Crash detection: a negative rc means termination by a signal (e.g. -11=SIGSEGV).
        # A driver directly calling the target function may crash on a dangling pointer /
        # out-of-bounds; give an explicit hint (driver-construction bug vs real defect in the
        # target function, judged with stderr).
        if rc < 0:
            sig = -rc
            sig_name = _SIGNAL_NAMES.get(sig, f"signal {sig}")
            stderr += (f"\n[崩溃] 单测 driver 被信号 {sig_name}({sig}) 终止。"
                       f"常见原因：driver 参数构造越界/野指针（先查 driver 的输入构造），"
                       f"或被测函数在特定输入下真实崩溃（可作为疑似产品缺陷上报）。")
        return ProcResult(cmd=cmd, rc=rc, stdout=proc.stdout,
                          stderr=stderr,
                          duration_ms=int((time.time() - start) * 1000))
    except subprocess.TimeoutExpired:
        return ProcResult(cmd=cmd, rc=124, stdout="", stderr=f"TIMEOUT after {timeout}s",
                          duration_ms=int((time.time() - start) * 1000))


_SIGNAL_NAMES = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 6: "SIGABRT",
    8: "SIGFPE", 9: "SIGKILL", 11: "SIGSEGV", 13: "SIGPIPE", 14: "SIGALRM",
    15: "SIGTERM", 24: "SIGXCPU", 25: "SIGXFSZ",
}


def assert_ut_compiled(res: ProcResult) -> None:
    """Assert the unit-test driver compiled successfully (rc=0)."""
    print(f"  assert_ut_compiled: rc={res.rc}")
    assert res.rc == 0, f"单测编译失败，编译输出:\n{res.stdout}"


def assert_driver_ok(res: ProcResult) -> None:
    """Assert the unit-test driver ran normally (rc=0, not signal-terminated). On crash (rc<0)
    it gives a clear distinguishing hint, more readable than a bare exit-code assert."""
    print(f"  assert_driver_ok: rc={res.rc}")
    assert res.rc == 0, (
        f"单测 driver 未正常返回 0（rc={res.rc}）。stderr:\n{res.stderr}"
        if res.rc != 0 else "")


# ── Local test service (network cases always self-start a loopback server; no external network) ──

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

    def log_message(self, *a):  # silence access logs
        pass


def local_server(port: int = 0, *, delay: float = 0.0, status: int = 200,
                 body: bytes = b"ok") -> tuple[HTTPServer, str]:
    """Start a local loopback HTTP service; returns (server, "127.0.0.1:port").

    Call server.shutdown() after the case; recommended with a fixture:
        server, addr = local_server()
        yield addr
        server.shutdown()
    """
    handler = type("H", (_EchoHandler,), {"delay": delay, "status": status, "body": body})
    srv = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_address[1]}"


def free_port() -> int:
    """Get an available local port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Input construction ──────────────────────────────────────────

def make_tmp_file(content: str, suffix: str = ".txt") -> Path:
    """Write content into a temp file; returns its path (cleaned up by the system after the session)."""
    f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                    encoding="utf-8", dir=str(SRC_ROOT / ".aicoverage"))
    f.write(content)
    f.close()
    return Path(f.name)


# ── Auditable printing ───────────────────────────────────────────

def print_test_point_box(what: str, input_desc: str, expected: str) -> None:
    """Print the test-point box (what/input/expected)."""
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
    """Print one key step's real observation (observed must be real output, not just True/False)."""
    print(f"  [step] {name}")
    print(f"         call:       {call}")
    print(f"         side_effect:{side_effect}")
    print(f"         expected:   {expected}")
    print(f"         observed:   {observed}", flush=True)


# ── Assertion atomic functions (print expected vs observed, then assert) ──

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
    """Generate the config + tests/ harness scaffold in the target project."""
    if language == "go":
        config = CONFIG_TEMPLATE_GO.format(name=name)
    elif language == "rust":
        config = CONFIG_TEMPLATE_RUST.format(name=name)
    elif language == "java":
        config = CONFIG_TEMPLATE_JAVA.format(name=name)
    else:
        config = CONFIG_TEMPLATE.format(name=name, language=language,
                                        build_cmd=build_cmd, binary=binary)
    (source / "aicoverage.toml").write_text(config, encoding="utf-8")

    # pytest harness scaffold is C/C++-only: Go/Rust/Java tests are written in
    # the project's own framework (go test / cargo test / JUnit), no harness.py
    if language in ("c", "cpp"):
        tests = source / "tests"
        (tests / "lib").mkdir(parents=True, exist_ok=True)
        (tests / "conftest.py").write_text(CONFTEST_TEMPLATE, encoding="utf-8")
        (tests / "lib" / "harness.py").write_text(HARNESS_TEMPLATE, encoding="utf-8")
        (tests / "lib" / "__init__.py").write_text("", encoding="utf-8")

    # .aicoverage workspace + gitignore
    (source / ".aicoverage").mkdir(exist_ok=True)
    gi = source / ".gitignore"
    entry = ".aicoverage/\n*.gcda\n*.gcno\n*.gcov.json*\n"
    if gi.exists():
        text = gi.read_text(encoding="utf-8")
        if ".aicoverage/" not in text:
            gi.write_text(text.rstrip("\n") + "\n" + entry, encoding="utf-8")
    else:
        gi.write_text(entry, encoding="utf-8")
