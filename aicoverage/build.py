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
    build_timeout = getattr(cfg, "build_timeout", 3600)

    logs: list[str] = []
    if cfg.clean_cmd and not skip_clean:
        rc, log, dur = run_shell(cfg.clean_cmd, cfg.source_path, timeout=build_timeout)
        logs.append(f"$ {cfg.clean_cmd}\n(rc={rc}, {dur:.1f}s)\n{log[-4000:]}")
        if rc != 0:
            # clean failure is not fatal (first build may have nothing to clean)
            logs.append("⚠ clean 命令非零退出（忽略，继续构建）")

    rc, log, dur = run_shell(cfg.build_cmd, cfg.source_path, timeout=build_timeout)
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

    gcno_files = find_gcno_files(cfg.source_path)
    result.gcno_count = len(gcno_files)
    if result.gcno_count == 0:
        result.failure_reason = (
            "构建成功但未发现 .gcno 文件——build_cmd 大概率没有带 --coverage 插桩，"
            "覆盖率将恒为 0%。请在 [build] build_cmd 中加入 -fprofile-arcs -ftest-coverage"
            "（或 --coverage）并重新构建。"
        )
        _dump_log(log_dir, logs)
        return result

    # gcno freshness/completeness warning (2026-08-27 hardening): a source file
    # without a matching .gcno silently reports 0% forever. Incremental builds
    # commonly miss newly-added files. Warn (not fail -- some files may be
    # intentionally excluded from the build).
    gcno_stems = {p.stem for p in gcno_files}
    try:
        src_files = [p for p in cfg.source_files()
                     if p.suffix not in (".h", ".hpp", ".hxx")]
        missing = [p for p in src_files if p.stem not in gcno_stems]
    except Exception:  # noqa: BLE001 — 清单失败不影响构建判定
        missing = []
    if missing:
        names = ", ".join(p.name for p in missing[:10])
        logs.append(f"⚠ {len(missing)} 个源文件没有对应 .gcno（这些文件覆盖率将恒为 0%）: "
                    f"{names}{'...' if len(missing) > 10 else ''}——"
                    f"若为增量构建请全量重建（clean 后 build）")

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
