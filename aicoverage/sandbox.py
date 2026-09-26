"""Execution sandbox: 把「确定性执行面」（插桩构建 / pytest+gcov 采集）关进隔离环境。

Why only two entry points:
    harness 的 run_binary / compile_unit_driver / run_driver / local_server 全部跑在
    pytest 进程内部，所以沙箱化 pytest 即等于沙箱化被测二进制、单测 driver 编译与
    本地回环服务——不需要逐个改。Agent 的 Bash（读源码/分析类）不在本模块管辖范围。

Design:
    - HostSandbox: 现状直通（subprocess shell=True），默认后端，行为与旧版完全一致
    - DockerSandbox: docker/podman run --rm -i，命令经 **stdin** 传给 `bash -s`
      （规避 shell 引号地狱）；**同路径 bind mount** 源码树——gcov 的 .gcno/.gcda
      落点在编译期固化为绝对路径，路径不一致会导致覆盖率全 0
    - 采集（gcov）与 gcc 版本强耦合，启用沙箱时默认也在容器内执行
      （executor.collect_coverage 同一份逻辑，宿主机/容器共用）

Degradation:
    enabled 但 runtime 不可用（无 docker/daemon）→ 降级 HostSandbox 并显式告警，
    不静默、不抛异常（闭环不因沙箱成为单点故障）。

Env contract: 容器内注入 AICOV_* 系列环境变量（cfg.to_env 的产物），并挂载
AIcoverage 包目录（只读）+ PYTHONPATH，使容器内可以 import aicoverage 做采集。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .observability import emit

#: 通用基础镜像（`aicov sandbox` 一次性构建，所有被测项目复用）
DEFAULT_SANDBOX_IMAGE = "aicoverage-sandbox:latest"

#: agent Bash 默认接管的命令（编译/构建类高风险动作；python/pytest 不在其中——
#: pytest 本就被 GEN_BLOCKED hooks 拦截，python 常用于只读分析，接管徒增容器开销）
DEFAULT_SHIM_COMMANDS = [
    "gcc", "g++", "cc", "c++", "clang", "clang++",
    "ld", "ar", "as", "nm", "objdump", "strip",
    "make", "cmake", "ninja",
    "autoconf", "automake", "autoreconf", "libtoolize", "pkg-config",
    "gcov", "gcov-dump", "gcov-tool",
]

#: runtime 探测结果的磁盘缓存 TTL（shim 每条命令都起一个新 Python 进程，
#  模块级缓存跨进程失效；不落盘的话每次 gcc 都要付一次 `docker version` 的代价）
_RUNTIME_CACHE_TTL_S = 300.0

#: 除 AICOV_* 外允许透传进容器的宿主机环境变量
_PASSTHROUGH_ENV = {
    "PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED", "SSL_CERT_FILE", "SSL_CERT_DIR",
}


@dataclass
class SandboxResult:
    rc: int
    log: str
    duration_s: float
    backend: str           # "host" | "docker" | "podman"
    network: bool = False


class Sandbox:
    """执行沙箱协议：run() 语义与 build.run_shell 完全对齐（rc/log/耗时）。"""

    name = "host"

    def run(self, cmd: str, *, cwd: Path, env: dict[str, str] | None = None,
            timeout: int = 3600, network: bool = True) -> SandboxResult:
        raise NotImplementedError


class HostSandbox(Sandbox):
    """现状直通：subprocess shell=True，无隔离（默认，行为与旧版完全一致）。"""

    name = "host"

    def run(self, cmd: str, *, cwd: Path, env: dict[str, str] | None = None,
            timeout: int = 3600, network: bool = True) -> SandboxResult:
        start = time.time()
        run_env = dict(env) if env is not None else None
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
                timeout=timeout, env=run_env,
            )
            log = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
            rc: int = proc.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc, log = 124, f"TIMEOUT after {timeout}s\n{out}"
        except OSError as e:
            rc, log = 127, f"OSERROR: {e}"
        return SandboxResult(rc=rc, log=log, duration_s=time.time() - start,
                             backend=self.name, network=network)


class DockerSandbox(Sandbox):
    """docker/podman 隔离执行：同路径挂载 + stdin 传令 + 资源/网络限制。"""

    def __init__(self, cfg: ProjectConfig, *, runtime: str):
        self.cfg = cfg
        self.runtime = runtime
        self.name = runtime
        self.image = (getattr(cfg, "sandbox_image", "") or DEFAULT_SANDBOX_IMAGE)
        self.src = Path(cfg.source_path).resolve()
        self.aicov_home = Path(__file__).resolve().parent.parent

    # ── argv 构造（独立成方法，便于不依赖 docker 的单测）─────────────
    def build_argv(self, *, cwd: Path, env: dict[str, str] | None,
                   network: bool, cmd: str | None = None) -> list[str]:
        """组装 `docker run` 命令行。

        cmd=None：命令稍后经 **stdin** 传给 `<shell> -s`（闭环确定性阶段用，
            用户 build_cmd 是任意 shell 串，走 -c 拼接会陷入引号地狱）。
        cmd 非 None：命令经环境变量传给 `<shell> -c 'eval "$AICOV_SHIM_CMD"'`
            （agent-bash shim 用；stdin 留给被转发命令的真实输入，如 `gcc -x c -`）。
        """
        cfg = self.cfg
        argv: list[str] = [self.runtime, "run", "--rm", "-i",
                           "--network", "bridge" if network else "none"]
        if hasattr(os, "getuid"):                     # Windows 无 uid/gid
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        argv += ["--memory", str(getattr(cfg, "sandbox_memory", "2g") or "2g"),
                 "--cpus", str(getattr(cfg, "sandbox_cpus", "2") or "2"),
                 "--pids-limit", str(getattr(cfg, "sandbox_pids_limit", 256) or 256),
                 "-w", str(cwd),
                 # 同路径挂载：gcov 编译期固化的是绝对路径，必须原路可见
                 "-v", f"{self.src}:{self.src}"]
        # AIcoverage 包目录（只读）——容器内采集要 import aicoverage；
        # 包目录就在源码树内时无需重复挂载
        try:
            home_in_src = self.aicov_home == self.src or self.src in self.aicov_home.parents
        except OSError:
            home_in_src = False
        if not home_in_src:
            argv += ["-v", f"{self.aicov_home}:{self.aicov_home}:ro"]
        for k, v in (env or {}).items():
            if k.startswith("AICOV_") or k in _PASSTHROUGH_ENV:
                argv += ["-e", f"{k}={v}"]
        argv += ["-e", "HOME=/tmp",                      # 非 root 用户映射后保证可写
                 "-e", f"PYTHONPATH={self.aicov_home}"]
        argv += [str(a) for a in (getattr(cfg, "sandbox_extra_args", None) or [])]
        shell = str(getattr(cfg, "sandbox_shell", "bash") or "bash")
        if cmd is None:
            argv += [self.image, shell, "-s"]
        else:
            argv += ["-e", f"AICOV_SHIM_CMD={cmd}", self.image, shell, "-c",
                     'eval "$AICOV_SHIM_CMD"']
        return argv

    def run(self, cmd: str, *, cwd: Path, env: dict[str, str] | None = None,
            timeout: int = 3600, network: bool = True) -> SandboxResult:
        cwd = Path(cwd).resolve()
        argv = self.build_argv(cwd=cwd, env=env, network=network)
        start = time.time()
        try:
            # 命令经 stdin 传给 `bash -s`：用户 build_cmd 是任意 shell 串，
            # 走 -c 拼接会陷入引号地狱；stdin 方式零转义
            proc = subprocess.run(
                argv, input=cmd, capture_output=True, text=True,
                timeout=timeout + 60,          # 容器启动开销宽限
            )
            log = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
            rc: int = proc.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc, log = 124, f"TIMEOUT after {timeout + 60}s\n{out}"
        except OSError as e:
            rc, log = 127, f"OSERROR: {e}"
        result = SandboxResult(rc=rc, log=log, duration_s=time.time() - start,
                               backend=self.name, network=network)
        # 事件落 run 自己的 events.jsonl（run_id 从 AICOV_RUN_DIR 反推，避免侵入 to_env）
        run_dir_env = str((env or {}).get("AICOV_RUN_DIR", "") or "")
        if run_dir_env:
            emit("sandbox.run", Path(run_dir_env).name,
                 runs_dir=Path(run_dir_env).parent,
                 data={"backend": self.name, "image": self.image, "rc": rc,
                       "network": network, "duration_s": round(result.duration_s, 1),
                       "cwd": str(cwd), "cmd": cmd[:200]})
        return result

    def run_pipe(self, cmd: str, *, cwd: Path, env: dict[str, str] | None = None,
                 timeout: int = 1800, network: bool = False) -> int:
        """转发执行：stdio 直通（agent 需要实时看到输出/交互），返回退出码。

        与 run() 的区别：run() 捕获输出（闭环确定性阶段要落盘日志）；
        run_pipe() 继承 stdio（agent Bash 的 shim 转发要"像本地命令一样"），
        stdin 留给被转发命令的真实输入（命令本体经 AICOV_SHIM_CMD 环境变量传入）。
        """
        cwd = Path(cwd).resolve()
        argv = self.build_argv(cwd=cwd, env=env, network=network, cmd=cmd)
        run_dir_env = str((env or {}).get("AICOV_RUN_DIR", "") or "")
        start = time.time()
        try:
            proc = subprocess.run(argv, timeout=timeout or None)   # stdio 继承
            rc: int = proc.returncode
        except subprocess.TimeoutExpired:
            print(f"⛔ [sandbox] 命令超过 {timeout}s 被终止: {cmd[:120]}",
                  file=sys.stderr)
            rc = 124
        except OSError as e:
            print(f"⛔ [sandbox] 启动失败: {e}", file=sys.stderr)
            rc = 127
        if run_dir_env:
            emit("sandbox.run", Path(run_dir_env).name,
                 runs_dir=Path(run_dir_env).parent,
                 data={"backend": self.name, "image": self.image, "rc": rc,
                       "network": network, "mode": "pipe",
                       "duration_s": round(time.time() - start, 1),
                       "cwd": str(cwd), "cmd": cmd[:200]})
        return rc


# ── runtime 探测与工厂 ────────────────────────────────────────────────

_runtime_cache: dict[str, bool] = {}


def _disk_cache_path(cache_dir: Path | None) -> Path | None:
    if cache_dir is None:
        return None
    try:
        return Path(cache_dir) / "sandbox_runtime_cache.json"
    except OSError:
        return None


def runtime_available(runtime: str, cache_dir: Path | None = None) -> bool:
    """docker/podman CLI 存在且 daemon 可达。

    结果做两级缓存：进程内 dict（闭环同进程多次构建）+ 磁盘 JSON（TTL 5 分钟，
    agent-bash shim 每条命令一个新进程，没有磁盘缓存每次 gcc 都要付一次探测代价）。
    """
    if runtime in _runtime_cache:
        return _runtime_cache[runtime]
    cache_file = _disk_cache_path(cache_dir)
    if cache_file is not None:
        try:
            import json
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            entry = data.get(runtime) or {}
            if time.time() - float(entry.get("ts", 0)) < _RUNTIME_CACHE_TTL_S:
                _runtime_cache[runtime] = bool(entry.get("ok"))
                return _runtime_cache[runtime]
        except (OSError, ValueError, TypeError):
            pass
    ok = False
    if shutil.which(runtime):
        try:
            proc = subprocess.run(
                [runtime, "version", "--format", "{{.Server.Version}}"],
                capture_output=True, text=True, timeout=10,
            )
            ok = proc.returncode == 0 and bool((proc.stdout or "").strip())
        except (subprocess.TimeoutExpired, OSError):
            ok = False
    _runtime_cache[runtime] = ok
    if cache_file is not None:
        try:
            import json
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            merged: dict = {}
            try:
                merged = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            merged[runtime] = {"ok": ok, "ts": time.time()}
            cache_file.write_text(json.dumps(merged), encoding="utf-8")
        except OSError:
            pass
    return ok


def detect_runtime(cache_dir: Path | None = None) -> str | None:
    for rt in ("docker", "podman"):
        if runtime_available(rt, cache_dir):
            return rt
    return None


def get_sandbox(cfg: ProjectConfig) -> Sandbox:
    """按配置返回沙箱后端；enabled 但不可用时降级 HostSandbox 并显式告警。"""
    if not getattr(cfg, "sandbox_enabled", False):
        return HostSandbox()
    pref = str(getattr(cfg, "sandbox_runtime", "auto") or "auto").strip().lower()
    if pref == "host":
        return HostSandbox()
    cache_dir = getattr(cfg, "workspace", None)
    if pref in ("docker", "podman"):
        runtime = pref if runtime_available(pref, cache_dir) else None
    else:                                   # auto
        runtime = detect_runtime(cache_dir)
    if runtime is None:
        print("⚠️ [sandbox] 已启用但未检测到可用的 docker/podman（或 daemon 不可达）"
              "→ 降级为宿主机直跑，本轮无隔离")
        try:
            from .observability import emit_diagnostic
            run_dir_env = os.environ.get("AICOV_RUN_DIR", "")
            emit_diagnostic(
                "SANDBOX_UNAVAILABLE",
                Path(run_dir_env).name or "sandbox",
                runs_dir=Path(run_dir_env).parent if run_dir_env else None,
                message="沙箱已启用但 runtime 不可用，降级为宿主机直跑（agent Bash 的"
                        "编译类命令同样回退宿主机执行）",
            )
        except Exception:  # noqa: BLE001 —— 降级告警绝不能反噬降级本身
            pass
        return HostSandbox()
    return DockerSandbox(cfg, runtime=runtime)


# ── agent-bash PATH shim ─────────────────────────────────────────────

_shims_cache: dict[tuple, Path] = {}


def agent_shim_commands(cfg: ProjectConfig) -> list[str]:
    raw = getattr(cfg, "sandbox_agent_shim_commands", None)
    return [str(c) for c in raw] if raw else list(DEFAULT_SHIM_COMMANDS)


def ensure_agent_shims(cfg: ProjectConfig) -> Path | None:
    """生成 agent Bash 的 PATH shim 目录（沙箱启用时由 AgentRunner 调用）。

    每个被接管的命令名一个可执行脚本：`exec <python> -m aicoverage.sandbox_shim
    <name> "$@"`，由 sandbox_shim 转发进容器。返回目录路径（前置进 agent 的
    PATH）；未启用/无命令表时返回 None。生成结果按 (项目, 镜像, 命令表) 缓存。
    """
    if not getattr(cfg, "sandbox_enabled", False):
        return None
    if not getattr(cfg, "sandbox_agent_shims", True):
        return None
    commands = agent_shim_commands(cfg)
    if not commands:
        return None
    image = getattr(cfg, "sandbox_image", "") or DEFAULT_SANDBOX_IMAGE
    key = (str(cfg.source_path), image, tuple(commands), sys.executable)
    if key in _shims_cache:
        return _shims_cache[key]

    shims_dir = cfg.workspace / "shims"
    shims_dir.mkdir(parents=True, exist_ok=True)
    interpreter = shlex.quote(sys.executable)
    for name in commands:
        script = shims_dir / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            "# aicoverage agent-bash sandbox shim（自动生成，勿手改）\n"
            f"exec {interpreter} -m aicoverage.sandbox_shim {shlex.quote(name)} \"$@\"\n",
            encoding="utf-8",
        )
        try:
            script.chmod(0o755)
        except OSError:
            pass
    _shims_cache[key] = shims_dir
    return shims_dir


def is_isolated(sandbox: Sandbox | None) -> bool:
    return sandbox is not None and sandbox.name != "host"


def sandbox_collect_command(cfg: ProjectConfig, coverage_path: Path) -> str:
    """容器内执行 gcov 采集的命令（gcc/gcov 必须同源，版本不匹配会解析失败）。

    复用 executor.collect_coverage_artifact —— 宿主机与容器内是同一份采集逻辑，
    保证 include/exclude 过滤与 ut_dir 标记行为完全一致。
    """
    home = Path(__file__).resolve().parent.parent
    code = (
        f"import sys; sys.path.insert(0, {str(home)!r}); "
        f"from aicoverage.config import load_config; "
        f"from aicoverage.executor import collect_coverage_artifact; "
        f"collect_coverage_artifact(load_config({str(cfg.config_path)!r}), "
        f"__import__('pathlib').Path({str(coverage_path)!r}))"
    )
    py = str(getattr(cfg, "sandbox_python", "python3") or "python3")
    return f"{py} -c {shlex.quote(code)}"
