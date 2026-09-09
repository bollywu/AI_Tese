"""AIcoverage project configuration: a single aicoverage.toml describes a target C/C++ project.

Core design (this file is the first layer):

| Common coupling point                    | This project's design                    |
|-----------------------------------------|------------------------------------------|
| Multi-file env config + dual-path system | One aicoverage.toml (source/test/build)  |
| Remote creds + remote execution targets  | None -- local build, local execution     |
| Profile system                           | Not needed -- one TOML is one project    |
| Commercial coverage tool (proprietary)   | gcc --coverage (gcov)                    |
| Containers / remote agents / RPC         | subprocess local execution               |

Config precedence: the TOML file is the single source of truth; individual
fields may be overridden by CLI args (func/cond/max-iter etc.).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_ENV = "AICOV_CONFIG"
DEFAULT_CONFIG_NAME = "aicoverage.toml"

DEFAULT_INCLUDE_GLOBS = ["src/**/*.c", "src/**/*.cc", "src/**/*.cpp", "src/**/*.cxx"]
DEFAULT_EXCLUDE_GLOBS = ["deps/**", "third_party/**", "tests/**"]

# 语言 → (源码后缀, include 默认, exclude 默认)。c/cpp 与历史完全一致；
# go/java 为后端扩展（docs/PLAN_gojava_backend.md）放行配置与源码列举。
_LANG_DEFAULTS: dict[str, dict] = {
    "c": {"suffixes": (".c", ".cc", ".cpp", ".cxx"),
          "include_globs": list(DEFAULT_INCLUDE_GLOBS),
          "exclude_globs": list(DEFAULT_EXCLUDE_GLOBS)},
    "cpp": {"suffixes": (".c", ".cc", ".cpp", ".cxx"),
            "include_globs": list(DEFAULT_INCLUDE_GLOBS),
            "exclude_globs": list(DEFAULT_EXCLUDE_GLOBS)},
    "go": {"suffixes": (".go",),
           "include_globs": ["**/*.go"],
           "exclude_globs": ["vendor/**", "third_party/**", "tests/**", "**/*_test.go"]},
    "java": {"suffixes": (".java",),
             "include_globs": ["**/*.java"],
             "exclude_globs": ["**/test/**", "**/target/**", "**/build/**",
                               "**/generated/**", "**/*Test*.java", "**/*Tests*.java"]},
}


def _source_suffixes(language: str) -> tuple:
    return tuple(_LANG_DEFAULTS.get(language, _LANG_DEFAULTS["c"])["suffixes"])


def _default_include_globs(language: str) -> list:
    return list(_LANG_DEFAULTS.get(language, _LANG_DEFAULTS["c"])["include_globs"])


def _default_exclude_globs(language: str) -> list:
    return list(_LANG_DEFAULTS.get(language, _LANG_DEFAULTS["c"])["exclude_globs"])


class ConfigError(SystemExit):
    """Configuration error (fail fast, reported at startup)."""


def find_config(explicit: str | None = None) -> Path:
    """Locate aicoverage.toml: CLI arg > env var > current directory."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_val = os.environ.get(CONFIG_ENV, "").strip()
    if env_val:
        candidates.append(Path(env_val).expanduser())
    candidates.append(Path.cwd() / DEFAULT_CONFIG_NAME)
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise ConfigError(
        f"❌ 未找到项目配置文件。查找顺序：\n"
        f"   1. --config <path>\n"
        f"   2. 环境变量 {CONFIG_ENV}\n"
        f"   3. ./aicoverage.toml\n"
        f"   可用 `aicov init` 在目标项目根目录生成配置模板。"
    )


@dataclass
class ProjectConfig:
    """一个被测 C/C++ 项目的完整接入配置。"""

    config_path: Path
    name: str
    display_name: str
    language: str = "c"                      # c | cpp (affects function extraction & agent hints)
    description: str = ""

    # ── Target source ─────────────────────────────────────────
    source_path: Path = Path(".")            # source/build root (absolute)
    include_globs: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_GLOBS))
    exclude_globs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))

    # ── Build (instrumentation) ───────────────────────────────
    clean_cmd: str = ""                      # optional, run before build
    build_cmd: str = ""                      # required, must include --coverage instrumentation
    binary: Path | None = None               # build artifact (absolute or relative to source_path)

    # ── Tests ─────────────────────────────────────────────────
    test_dirname: str = "tests"              # test dir name (relative to source_path)
    test_python: str = "auto"                # interpreter for pytest: auto | absolute path
    test_timeout: int = 600                  # per-pytest timeout (sec), must be > 0

    # ── Coverage (gcov) ───────────────────────────────────────
    gcov_bin: str = "gcov"
    func_target: float = 100.0
    cond_target: float = 85.0

    # ── Loop ──────────────────────────────────────────────────
    max_iter: int = 6
    no_progress_stop: int = 2

    # ── LLM / Agent ──────────────────────────────────────────
    model: str = ""                        # required: model name supported by the Agent SDK
    gen_model: str = ""                      # empty = same as model
    max_turns: int = 120                    # max tool turns per agent call (complex projects
                                            # hit context_overflow; 80 was too small, bumped
                                            # to 120 on 2026-08-25)
    max_verify_retry: int = 3               # max verify fix-loop rounds (at 2 complex projects
                                            # gen often can't finish in time causing a false
                                            # verify_fail_exceeded early-stop; bumped to 3)
    permission_mode: str = "bypassPermissions"

    # ── Budget gate (0 = unlimited) ─────────────────────────────
    # loop 每轮累计各 agent 调用的 cost/token，超限即 early_stop(budget_exhausted)，
    # 避免"跑满 max_iter × 每个 stage"把预算烧穿（唯一的时间闸门是退避超时，管不住总额）。
    max_cost_usd: float = 0.0               # [llm].max_cost_usd
    max_total_tokens: int = 0               # [llm].max_total_tokens

    # ── Knowledge resources (all optional) ────────────────────
    kb_dir: Path | None = None               # business knowledge-base dir
    badcase_dir: Path | None = None          # deprecated placeholder: badcases auto-managed
                                             # (<source>/.aicoverage/badcases.md,
                                             # see aicoverage/badcase.py), no config needed
    few_shots_dir: Path | None = None        # few-shot test examples
    prompts_dir: Path | None = None          # fully override built-in prompts/<name>.md

    # ── Security (extra command blacklist for hooks, regex) ───
    extra_blocked_commands: list[str] = field(default_factory=list)

    # ── Unit-test channel (E2E-unreachable -> unit test, all optional) ──
    # When a function cannot be reached through the binary's normal E2E flow
    # (gap root causes N1/N3/N5), gen-agent may generate test_driver_*.c that
    # calls the target function directly, using this section's compiler with
    # --coverage to build a "unit-test driver binary" and run it, covering it.
    # gcov scans the source tree for .gcno/.gcda, so this is natively compatible
    # (no changes to collection logic needed).
    ut_compiler: str = ""                     # empty = follow build system; else explicit (gcc/g++/cc)
    ut_flags: list[str] = field(default_factory=lambda: ["-O0", "-g", "-Wall"])  # extra unit-test flags
    ut_link_libs: list[str] = field(default_factory=list)   # extra link libs, e.g. ["-lm", "-lpthread"]
    ut_obj_dir: str = ".aicoverage/ut"        # unit-test intermediate dir (relative to source_path; .gcno/.gcda here)

    # ── Language backend extras (Go / Java, optional; used by aicoverage.backends) ──
    go_bin: str = "go"                               # [go].go_bin
    go_packages: list[str] = field(default_factory=lambda: ["..."])   # [go].packages（go test/coverpkg 的包集合）
    go_tags: str = ""                                # [go].build_tags
    go_coverdir: str = ".aicoverage/coverdata"       # [go].coverdir —— GOCOVERDIR 基准目录（相对 source_path）
    go_coverprofile: str = ".aicoverage/cover.out"   # [go].coverprofile —— coverprofile 落点（外部 go test / covdata textfmt）
    java_agent: str = ""                         # jacocoagent.jar 绝对路径（[java].jacoco_agent）
    java_exec_dir: str = ".aicoverage/jacoco"    # jacoco.exec 落点目录（相对 source_path）

    # ── CodeGraph (for MR incremental loop: call-graph/diff attribution, all optional) ──
    codegraph_enabled: bool = False
    codegraph_index_dir: str = ".codegraph"          # relative to source_path
    codegraph_entrypoints: list[str] = field(default_factory=lambda: ["main"])

    # ── Scan-track backend (open-code-review / built-in scan-agent) ──
    scan_backend: str = "auto"   # auto | ocr | agent | off
                                 # auto: use ocr if available & configured, else fall back to agent
                                 # ocr: force open-code-review (error if unavailable)
                                 # agent: force built-in scan-agent
                                 # off: skip the scan track

    # ── Runtime cache (not part of config) ────────────────────
    _source_files_cache: list | None = field(default=None, repr=False, compare=False)

    # ── Factory methods ───────────────────────────────────────
    @classmethod
    def minimal(cls, source_path, *, name=None, build_cmd="", binary=None,
                test_dirname="tests", language="c") -> "ProjectConfig":
        """Construct a minimal usable config (deterministic-phase defaults filled in).

        For tests/utility scripts that only need to specify key fields, avoiding
        hand-writing ProjectConfig.__new__ + assigning each field (a missing field
        crashes at runtime, see to_env dependency). All optional fields fall back
        to dataclass defaults so every field is present.
        """
        src = Path(source_path).expanduser().resolve()
        return cls(
            config_path=src / "aicoverage.toml",
            name=name or src.name,
            display_name=name or src.name,
            language=language,
            source_path=src,
            build_cmd=build_cmd,
            binary=Path(binary) if binary else None,
            test_dirname=test_dirname,
            test_timeout=600,
            func_target=100.0, cond_target=85.0,
            max_iter=6, no_progress_stop=2,
        )

    # ── Derived paths ─────────────────────────────────────────
    @property
    def test_dir(self) -> Path:
        return self.source_path / self.test_dirname

    @property
    def tests_lib_dir(self) -> Path:
        return self.test_dir / "lib"

    @property
    def conftest_path(self) -> Path:
        return self.test_dir / "conftest.py"

    @property
    def workspace(self) -> Path:
        """Each target project's own AIcoverage workspace (runs/reports live here)."""
        return self.source_path / ".aicoverage"

    @property
    def runs_dir(self) -> Path:
        return self.workspace / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.workspace / "reports"

    @property
    def binary_path(self) -> Path | None:
        if self.binary is None:
            return None
        p = self.binary if self.binary.is_absolute() else self.source_path / self.binary
        return p

    @property
    def ut_obj_path(self) -> Path:
        """Unit-test intermediate dir (.gcno/.gcda/obj all land here)."""
        p = Path(self.ut_obj_dir)
        return p if p.is_absolute() else self.source_path / p

    @property
    def effective_gen_model(self) -> str:
        return self.gen_model or self.model

    def validate(self) -> list[str]:
        """Startup fail-fast validation; returns error list (empty = ok)."""
        errors: list[str] = []
        if not self.source_path.is_dir():
            errors.append(f"source.path 不存在或不是目录: {self.source_path}")
        if not self.build_cmd.strip():
            errors.append("build.build_cmd 为空——必须提供插桩构建命令（含 --coverage）")
        if self.binary is None:
            errors.append("build.binary 为空——必须指定构建产物路径")
        if self.test_timeout <= 0:
            errors.append(f"test.timeout={self.test_timeout} 非法——必须为正数（秒）")
        return errors

    def source_files(self) -> list[Path]:
        """Source files matched by include/exclude globs (absolute; `**` uses gitignore semantics).

        Result is cached per-instance (avoids repeated full rglob scans of large
        projects within the same loop).
        """
        from .globutil import glob_matches

        if self._source_files_cache is not None:
            return list(self._source_files_cache)
        results: list[Path] = []
        if self.source_path.is_dir():
            all_files = [
                p for p in self.source_path.rglob("*")
                if p.is_file() and p.suffix in _source_suffixes(self.language)
            ]
            for p in sorted(all_files):
                rel = p.relative_to(self.source_path).as_posix()
                if glob_matches(rel, self.include_globs) and not glob_matches(rel, self.exclude_globs):
                    results.append(p)
        self._source_files_cache = list(results)
        return list(results)

    def invalidate_source_files(self) -> None:
        """Clear the source_files cache (when project source changes at runtime)."""
        self._source_files_cache = None

    def to_env(self, run_dir: Path | None = None, iter_dir: Path | None = None) -> dict[str, str]:
        """Build the key env vars injected into agent runtimes (AICOV_* series)."""
        env = {
            "AICOV_CONFIG": str(self.config_path),
            "AICOV_SRC": str(self.source_path),
            "AICOV_TEST_DIR": str(self.test_dir),
            "AICOV_WORKSPACE": str(self.workspace),
            "AICOV_PROJECT": self.name,
        }
        if self.binary_path is not None:
            env["AICOV_BINARY"] = str(self.binary_path)
        # Unit-test channel env (getattr fallback: tolerate old test instances built via
        # ProjectConfig.__new__ without these fields)
        env["AICOV_UT_OBJ_DIR"] = str(getattr(self, "ut_obj_path", self.source_path / ".aicoverage" / "ut"))
        env["AICOV_UT_COMPILER"] = getattr(self, "ut_compiler", "") or "gcc"
        env["AICOV_UT_FLAGS"] = " ".join(getattr(self, "ut_flags", ["-O0", "-g", "-Wall"]))
        env["AICOV_UT_LINK_LIBS"] = " ".join(getattr(self, "ut_link_libs", []))
        if run_dir is not None:
            env["AICOV_RUN_DIR"] = str(run_dir)
        if iter_dir is not None:
            env["AICOV_ITER_DIR"] = str(iter_dir)
        return env


def _resolve_dir(base: Path, value: str) -> Path | None:
    """Resolve a knowledge dir: empty -> None; relative resolved against the config's dir; absolute as-is."""
    v = (value or "").strip()
    if not v:
        return None
    p = Path(v).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_config(explicit_path: str | None = None) -> ProjectConfig:
    """Load and validate aicoverage.toml."""
    path = find_config(explicit_path)
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"❌ 配置文件 TOML 语法错误: {path}\n   {e}")

    proj = raw.get("project", {})
    src = raw.get("source", {})
    build = raw.get("build", {})
    test = raw.get("test", {})
    cov = raw.get("coverage", {})
    loop = raw.get("loop", {})
    llm = raw.get("llm", {})
    know = raw.get("knowledge", {})
    guard = raw.get("guard", {})
    codegraph = raw.get("codegraph", {})
    scan = raw.get("scan", {})
    unit = raw.get("unittest", {})

    source_path = Path(src.get("path", ".")).expanduser()
    if not source_path.is_absolute():
        source_path = (path.parent / source_path).resolve()

    binary_raw = (build.get("binary") or "").strip()
    binary = Path(binary_raw).expanduser() if binary_raw else None

    proj_language = str(proj.get("language", "c")).lower()
    go_cfg = raw.get("go", {})
    java_cfg = raw.get("java", {})

    cfg = ProjectConfig(
        config_path=path,
        name=str(proj.get("name") or path.parent.name),
        display_name=str(proj.get("display_name") or proj.get("name") or path.parent.name),
        language=proj_language,
        description=str(proj.get("description", "")),
        source_path=source_path,
        include_globs=list(src.get("include_globs", _default_include_globs(proj_language))),
        exclude_globs=list(src.get("exclude_globs", _default_exclude_globs(proj_language))),
        clean_cmd=str(build.get("clean_cmd", "")).strip(),
        build_cmd=str(build.get("build_cmd", "")).strip(),
        binary=binary,
        test_dirname=str(test.get("dir", "tests")).strip() or "tests",
        test_python=str(test.get("python", "auto")).strip(),
        test_timeout=int(test.get("timeout", 600)),
        gcov_bin=str(cov.get("gcov_bin", "gcov")).strip(),
        func_target=float(cov.get("func_target", 100.0)),
        cond_target=float(cov.get("cond_target", 85.0)),
        max_iter=int(loop.get("max_iter", 6)),
        no_progress_stop=int(loop.get("no_progress_stop", 2)),
        model=str(llm.get("model", "")).strip(),
        gen_model=str(llm.get("gen_model", "")).strip(),
        max_turns=int(llm.get("max_turns", 120)),
        max_verify_retry=int(llm.get("max_verify_retry", 3)),
        permission_mode=str(llm.get("permission_mode", "bypassPermissions")).strip(),
        max_cost_usd=float(llm.get("max_cost_usd", 0.0)),
        max_total_tokens=int(llm.get("max_total_tokens", 0)),
        kb_dir=_resolve_dir(path.parent, know.get("kb_dir", "")),
        badcase_dir=_resolve_dir(path.parent, know.get("badcase_dir", "")),
        few_shots_dir=_resolve_dir(path.parent, know.get("few_shots_dir", "")),
        prompts_dir=_resolve_dir(path.parent, know.get("prompts_dir", "")),
        extra_blocked_commands=[str(x) for x in guard.get("blocked_commands", [])],
        codegraph_enabled=bool(codegraph.get("enabled", False)),
        codegraph_index_dir=str(codegraph.get("index_dir", ".codegraph")).strip() or ".codegraph",
        codegraph_entrypoints=[str(x) for x in codegraph.get("entrypoints", ["main"])] or ["main"],
        scan_backend=str(scan.get("backend", "auto")).strip() or "auto",
        ut_compiler=str(unit.get("compiler", "")).strip(),
        ut_flags=[str(x) for x in unit.get("flags", ["-O0", "-g", "-Wall"])] or ["-O0", "-g", "-Wall"],
        ut_link_libs=[str(x) for x in unit.get("link_libs", [])],
        ut_obj_dir=str(unit.get("obj_dir", ".aicoverage/ut")).strip() or ".aicoverage/ut",
        go_bin=str(go_cfg.get("go_bin", "go")).strip() or "go",
        go_packages=[str(x) for x in go_cfg.get("packages", ["..."])] or ["..."],
        go_tags=str(go_cfg.get("build_tags", "")).strip(),
        go_coverdir=str(go_cfg.get("coverdir", ".aicoverage/coverdata")).strip() or ".aicoverage/coverdata",
        go_coverprofile=str(go_cfg.get("coverprofile", ".aicoverage/cover.out")).strip() or ".aicoverage/cover.out",
        java_agent=str(java_cfg.get("jacoco_agent", "")).strip(),
        java_exec_dir=str(java_cfg.get("exec_dir", ".aicoverage/jacoco")).strip() or ".aicoverage/jacoco",
    )
    if cfg.scan_backend not in ("auto", "ocr", "agent", "off"):
        raise ConfigError(f"❌ scan.backend 必须是 auto/ocr/agent/off，当前: {cfg.scan_backend!r}")

    if cfg.language not in ("c", "cpp", "go", "java"):
        raise ConfigError(
            f"❌ project.language 必须是 c/cpp/go/java，当前: {cfg.language!r}（"
            "c/cpp 走 gcov 后端；go/java 为后端扩展，见 docs/PLAN_gojava_backend.md，"
            "对应 collect 需安装 go/java 工具链）")

    errors = cfg.validate()
    if errors:
        raise ConfigError("❌ 配置校验失败:\n   - " + "\n   - ".join(errors))

    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg
