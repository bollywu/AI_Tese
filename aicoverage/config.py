"""AIcoverage 项目配置：单个 aicoverage.toml 描述一个被测 C/C++ 项目。

核心设计（本文件是第一层）：

| 常见耦合点                               | 本项目设计                                |
|-----------------------------------------|------------------------------------------|
| 多文件环境配置 + 双路径体系               | 一份 aicoverage.toml（源码/测试/构建一体）|
| 远程凭据 + 远程执行目标配置               | 不存在——本机构建、本地执行               |
| profile 体系                             | 不需要——一个 TOML 即一个项目             |
| 商业覆盖率工具（专有格式）                | gcc --coverage（gcov）                   |
| 容器 / 远程 agent / RPC                  | subprocess 本地执行                      |

配置优先级：TOML 文件为唯一真源；个别字段可被 CLI 参数覆盖（func/cond/max-iter 等）。
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


class ConfigError(SystemExit):
    """配置错误（fail fast，启动阶段即报）。"""


def find_config(explicit: str | None = None) -> Path:
    """定位 aicoverage.toml：CLI 参数 > 环境变量 > 当前目录。"""
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
    language: str = "c"                      # c | cpp（影响函数提取与 agent 提示）
    description: str = ""

    # ── 被测源码 ──────────────────────────────────────────────
    source_path: Path = Path(".")            # 源码/构建根目录（绝对路径）
    include_globs: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_GLOBS))
    exclude_globs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))

    # ── 构建（插桩） ─────────────────────────────────────────
    clean_cmd: str = ""                      # 可选，构建前清理
    build_cmd: str = ""                      # 必填，需自带 --coverage 插桩
    binary: Path | None = None               # 构建产物（绝对或相对 source_path）

    # ── 测试 ─────────────────────────────────────────────────
    test_dirname: str = "tests"              # 测试目录名（相对 source_path）
    test_python: str = "auto"                # 跑 pytest 的解释器：auto | 绝对路径
    test_timeout: int = 600                  # 单次 pytest 整体超时（秒），禁止 0

    # ── 覆盖率（gcov） ───────────────────────────────────────
    gcov_bin: str = "gcov"
    func_target: float = 100.0
    cond_target: float = 85.0

    # ── 闭环 ─────────────────────────────────────────────────
    max_iter: int = 6
    no_progress_stop: int = 2

    # ── LLM / Agent ──────────────────────────────────────────
    model: str = ""                        # 必填：所用 Agent SDK 支持的模型名
    gen_model: str = ""                      # 留空 = 同 model
    max_turns: int = 120                    # 单次 agent 调用最大工具轮次（复杂项目易触发
                                            # context_overflow，80 偏小，2026-08-25 调至 120）
    max_verify_retry: int = 3               # verify 失败修复回环最大次数（2 时复杂项目
                                            # gen 修不完易假早停 verify_fail_exceeded，
                                            # 2026-08-25 调至 3）
    permission_mode: str = "bypassPermissions"

    # ── 知识资源（全部可选） ─────────────────────────────────
    kb_dir: Path | None = None               # 业务知识库目录
    badcase_dir: Path | None = None          # 已废弃占位：badcase 由自动机制接管
                                             # （<source>/.aicoverage/badcases.md，
                                             # 见 aicoverage/badcase.py），无需配置
    few_shots_dir: Path | None = None        # few-shot 用例示例
    prompts_dir: Path | None = None          # 整份覆盖内置 prompts/<name>.md

    # ── 安全（hooks 额外命令黑名单，正则） ───────────────────
    extra_blocked_commands: list[str] = field(default_factory=list)

    # ── 单元测试通道（e2e 不可达函数转单测，全部可选） ───────────
    # 当某函数无法通过被测二进制的正常 E2E 流程触达（gap 根因 N1/N3/N5）时，
    # gen-agent 可生成 test_driver_*.c 直接调用目标函数，用本段配置的编译器
    # 以 --coverage 插桩编译出"单测 driver 二进制"并运行，从而覆盖该函数。
    # gcov 按源码树扫 .gcno/.gcda，天然兼容（无需改采集逻辑）。
    ut_compiler: str = ""                     # 空 = 跟随 build 体系；否则显式指定（gcc/g++/cc）
    ut_flags: list[str] = field(default_factory=lambda: ["-O0", "-g", "-Wall"])  # 单测编译附加 flag
    ut_link_libs: list[str] = field(default_factory=list)   # 额外链接库，如 ["-lm", "-lpthread"]
    ut_obj_dir: str = ".aicoverage/ut"        # 单测中间产物目录（相对 source_path，.gcno/.gcda 落此）

    # ── CodeGraph（MR 增量闭环用，调用链分析/diff 行归因，全部可选）───
    codegraph_enabled: bool = False
    codegraph_index_dir: str = ".codegraph"          # 相对 source_path
    codegraph_entrypoints: list[str] = field(default_factory=lambda: ["main"])

    # ── 扫描轨后端（open-code-review / 自研 scan-agent） ──────
    scan_backend: str = "auto"   # auto | ocr | agent | off
                                 # auto: ocr 可用且已配置则用之，否则降级 agent
                                 # ocr: 强制 open-code-review（不可用则报错）
                                 # agent: 强制自研 scan-agent
                                 # off: 跳过扫描轨

    # ── 运行期缓存（不入配置） ────────────────────────────────
    _source_files_cache: list | None = field(default=None, repr=False, compare=False)

    # ── 工厂方法 ─────────────────────────────────────────────
    @classmethod
    def minimal(cls, source_path, *, name=None, build_cmd="", binary=None,
                test_dirname="tests", language="c") -> "ProjectConfig":
        """构造一个最小可用配置（确定性阶段的默认值自动填全）。

        用于测试/工具脚本里需要"只指定关键字段"的场景，避免手写
        ProjectConfig.__new__ + 逐个赋值（缺字段会在运行时崩，见 to_env 依赖）。
        所有可选字段走 dataclass 默认值，保证字段完整。
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

    # ── 派生路径 ─────────────────────────────────────────────
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
        """每个目标项目自己的 AIcoverage 工作区（runs/reports 全在这里）。"""
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
        """单测中间产物目录（.gcno/.gcda/obj 都落这里）。"""
        p = Path(self.ut_obj_dir)
        return p if p.is_absolute() else self.source_path / p

    @property
    def effective_gen_model(self) -> str:
        return self.gen_model or self.model

    def validate(self) -> list[str]:
        """启动阶段 fail-fast 校验，返回错误列表（空 = 通过）。"""
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
        """按 include/exclude glob 匹配的源文件（绝对路径；`**` 为 gitignore 语义）。

        结果做实例级缓存（同一闭环内多次调用避免重复全量 rglob 扫描大项目）。
        """
        from .globutil import glob_matches

        if self._source_files_cache is not None:
            return list(self._source_files_cache)
        results: list[Path] = []
        if self.source_path.is_dir():
            all_files = [
                p for p in self.source_path.rglob("*")
                if p.is_file() and p.suffix in (".c", ".cc", ".cpp", ".cxx")
            ]
            for p in sorted(all_files):
                rel = p.relative_to(self.source_path).as_posix()
                if glob_matches(rel, self.include_globs) and not glob_matches(rel, self.exclude_globs):
                    results.append(p)
        self._source_files_cache = list(results)
        return list(results)

    def invalidate_source_files(self) -> None:
        """清空 source_files 缓存（若项目源码在运行期发生变化）。"""
        self._source_files_cache = None

    def to_env(self, run_dir: Path | None = None, iter_dir: Path | None = None) -> dict[str, str]:
        """构造注入 agent 运行环境的关键变量（AICOV_* 系列）。"""
        env = {
            "AICOV_CONFIG": str(self.config_path),
            "AICOV_SRC": str(self.source_path),
            "AICOV_TEST_DIR": str(self.test_dir),
            "AICOV_WORKSPACE": str(self.workspace),
            "AICOV_PROJECT": self.name,
        }
        if self.binary_path is not None:
            env["AICOV_BINARY"] = str(self.binary_path)
        # 单测通道环境（getattr 兜底：兼容 ProjectConfig.__new__ 直构的旧测试实例）
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
    """知识目录解析：空 → None；相对路径相对 config 所在目录；绝对路径原样。"""
    v = (value or "").strip()
    if not v:
        return None
    p = Path(v).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_config(explicit_path: str | None = None) -> ProjectConfig:
    """加载并校验 aicoverage.toml。"""
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

    cfg = ProjectConfig(
        config_path=path,
        name=str(proj.get("name") or path.parent.name),
        display_name=str(proj.get("display_name") or proj.get("name") or path.parent.name),
        language=str(proj.get("language", "c")).lower(),
        description=str(proj.get("description", "")),
        source_path=source_path,
        include_globs=list(src.get("include_globs", DEFAULT_INCLUDE_GLOBS)),
        exclude_globs=list(src.get("exclude_globs", DEFAULT_EXCLUDE_GLOBS)),
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
    )
    if cfg.scan_backend not in ("auto", "ocr", "agent", "off"):
        raise ConfigError(f"❌ scan.backend 必须是 auto/ocr/agent/off，当前: {cfg.scan_backend!r}")

    if cfg.language not in ("c", "cpp"):
        raise ConfigError(f"❌ project.language 必须是 c 或 cpp，当前: {cfg.language!r}")

    errors = cfg.validate()
    if errors:
        raise ConfigError("❌ 配置校验失败:\n   - " + "\n   - ".join(errors))

    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg
