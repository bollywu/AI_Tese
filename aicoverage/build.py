"""Instrumented-build driver: clean -> build -> verify (binary exists + .gcno generated).

The build command is entirely project-provided (aicoverage.toml's [build]).
AIcoverage does not assume any build system (make/cmake/custom scripts all fine);
it only verifies two things:
  1. build_cmd exits with code 0
  2. .gcno files appear in the source tree after build (proving --coverage
     instrumentation actually took effect)
     -- local version's defense: verify gcno really generated right after build.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .backends import get_backend
from .config import ProjectConfig


@dataclass
class BuildResult:
    ok: bool
    log: str = ""
    binary: Path | None = None
    gcno_count: int = 0
    duration_s: float = 0.0
    failure_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "binary": str(self.binary) if self.binary else None,
            "gcno_count": self.gcno_count,
            "duration_s": round(self.duration_s, 1),
            "failure_reason": self.failure_reason,
        }


def is_fresh(cfg: ProjectConfig) -> tuple[bool, str]:
    """Whether the instrumented artifact can be reused as-is (skip-build / resume 前必校验)。

    复用场景（MR 多批复用首批产物、`--resume` 续跑）必须确认产物没过期，否则
    覆盖率会建立在"源码已改、插桩产物还是旧的"之上，得到假数据。判定：
      1. 二进制存在
      2. 插桩确实生效（gcov 后端看 .gcno；其它后端交 backend.verify_build）
      3. 没有源码文件比二进制更新（留 1s 容差，规避同秒写入的抖动）

    Returns:
        (fresh, reason)；fresh=False 时 reason 为空串以外的原因说明。
    """
    binary = cfg.binary_path
    if binary is None or not binary.exists():
        return False, f"构建产物不存在: {binary}"

    backend = get_backend(cfg)
    if backend.name == "gcov":
        from .gcov import find_gcno_files
        if not find_gcno_files(cfg.source_path):
            return False, "源码树中没有 .gcno（插桩未生效）"
    else:
        errs = backend.verify_build(cfg)
        if errs:
            return False, "后端校验未通过：" + "；".join(errs)

    try:
        bin_mtime = binary.stat().st_mtime
    except OSError:
        return False, f"构建产物不可读: {binary}"

    newest: Path | None = None
    newest_mtime = 0.0
    for p in cfg.source_files():
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest_mtime:
            newest, newest_mtime = p, m
    if newest is not None and newest_mtime > bin_mtime + 1.0:
        return False, (f"源码比产物新：{newest.name} 晚于 {binary.name} "
                       f"{newest_mtime - bin_mtime:.0f}s")
    return True, ""


def run_shell(cmd: str, cwd: Path, timeout: int = 3600) -> tuple[int, str, float]:
    """Run a shell command, return (rc, merged log, elapsed seconds)."""
    import time

    start = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout,
        )
        log = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        return proc.returncode, log, time.time() - start
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, f"TIMEOUT after {timeout}s\n{out}", time.time() - start
    except OSError as e:
        return 127, f"OSERROR: {e}", time.time() - start


def build(cfg: ProjectConfig, *, skip_clean: bool = False, log_dir: Path | None = None) -> BuildResult:
    """Run the instrumented build and verify it."""
    result = BuildResult(ok=False, binary=cfg.binary_path)

    logs: list[str] = []
    if cfg.clean_cmd and not skip_clean:
        rc, log, dur = run_shell(cfg.clean_cmd, cfg.source_path)
        logs.append(f"$ {cfg.clean_cmd}\n(rc={rc}, {dur:.1f}s)\n{log[-4000:]}")
        if rc != 0:
            # clean failure is not fatal (first build may have nothing to clean)
            logs.append("⚠ clean 命令非零退出（忽略，继续构建）")

    rc, log, dur = run_shell(cfg.build_cmd, cfg.source_path)
    result.duration_s = dur
    logs.append(f"$ {cfg.build_cmd}\n(rc={rc}, {dur:.1f}s)\n{log[-8000:]}")
    result.log = "\n\n".join(logs)

    if rc != 0:
        result.failure_reason = f"build_cmd 退出码 {rc}"
        _dump_log(log_dir, logs)
        return result

    if result.binary is not None and not result.binary.exists():
        result.failure_reason = f"构建成功但产物不存在: {result.binary}"
        _dump_log(log_dir, logs)
        return result

    backend = get_backend(cfg)
    if backend.name == "gcov":
        from .gcov import find_gcno_files
        result.gcno_count = len(find_gcno_files(cfg.source_path))
        if result.gcno_count == 0:
            result.failure_reason = (
                "构建成功但未发现 .gcno 文件——build_cmd 大概率没有带 --coverage 插桩，"
                "覆盖率将恒为 0%。请在 [build] build_cmd 中加入 -fprofile-arcs -ftest-coverage"
                "（或 --coverage）并重新构建。"
            )
            _dump_log(log_dir, logs)
            return result
    else:
        # 非 gcov 后端（go/java）：插桩生效校验交给对应 backend
        verify_errs = backend.verify_build(cfg)
        if verify_errs:
            result.failure_reason = "构建产物存在但后端校验未通过：" + "；".join(verify_errs)
            _dump_log(log_dir, logs)
            return result

    result.ok = True
    _dump_log(log_dir, logs)
    return result


def _dump_log(log_dir: Path | None, logs: list[str]) -> None:
    if log_dir is None:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "build.log").write_text("\n\n".join(logs), encoding="utf-8")
    except OSError:
        pass
