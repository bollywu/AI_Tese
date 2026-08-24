"""插桩构建驱动：clean → build → 校验（二进制存在 + .gcno 生成）。

构建命令完全由项目配置提供（aicoverage.toml 的 [build]），AIcoverage 不假设
构建系统（make/cmake/自定义脚本均可），只验证两件事：
  1. build_cmd 退出码为 0
  2. 构建后源码树出现 .gcno 文件（证明 --coverage 插桩真的生效了）
     —— 本机版防线：插桩构建后立即校验 gcno 是否真实生成。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .gcov import find_gcno_files


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


def run_shell(cmd: str, cwd: Path, timeout: int = 3600) -> tuple[int, str, float]:
    """执行 shell 命令，返回 (rc, 合并日志, 耗时秒)。"""
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
    """执行插桩构建并校验。"""
    result = BuildResult(ok=False, binary=cfg.binary_path)

    logs: list[str] = []
    if cfg.clean_cmd and not skip_clean:
        rc, log, dur = run_shell(cfg.clean_cmd, cfg.source_path)
        logs.append(f"$ {cfg.clean_cmd}\n(rc={rc}, {dur:.1f}s)\n{log[-4000:]}")
        if rc != 0:
            # clean 失败不致命（首次构建可能本就无产物可清）
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

    result.gcno_count = len(find_gcno_files(cfg.source_path))
    if result.gcno_count == 0:
        result.failure_reason = (
            "构建成功但未发现 .gcno 文件——build_cmd 大概率没有带 --coverage 插桩，"
            "覆盖率将恒为 0%。请在 [build] build_cmd 中加入 -fprofile-arcs -ftest-coverage"
            "（或 --coverage）并重新构建。"
        )
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
