"""Agent-bash 命令的沙箱转发入口（PATH shim 的后端，见 sandbox.ensure_agent_shims）。

调用链：
    agent 在 Bash 里敲 `gcc -c foo.c`
      → PATH 前置的 <workspace>/shims/gcc 截获
        → exec <python> -m aicoverage.sandbox_shim gcc -c foo.c
          → 沙箱可用：docker run ... bash -c 'eval "$AICOV_SHIM_CMD"'（stdio 直通）
          → 沙箱不可用：execvp 原样回退宿主机（与未启用时行为一致）

stdin 语义：命令本体经 AICOV_SHIM_CMD 环境变量传入，stdin 留给被转发命令的
真实输入（`gcc -x c -`、管道喂参数等场景不受影响）。
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from .config import load_config
from .sandbox import get_sandbox, is_isolated


def _exec_host(argv: list[str]) -> int:
    """沙箱不可用时的回退：execvp 原样执行（进程替换，退出码即宿主机命令的）。"""
    try:
        os.execvp(argv[0], argv)          # 正常不返回
    except FileNotFoundError:
        print(f"aicov-sandbox-shim: 找不到命令 {argv[0]}", file=sys.stderr)
        return 127
    except OSError as e:
        print(f"aicov-sandbox-shim: 执行失败: {e}", file=sys.stderr)
        return 126


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m aicoverage.sandbox_shim <command> [args...]",
              file=sys.stderr)
        return 2

    # 快速短路：沙箱未启用（shim 目录只会在启用时进 PATH，但用户可能手动调用）
    cfg = load_config(os.environ.get("AICOV_CONFIG") or None)
    sandbox = get_sandbox(cfg)
    if not is_isolated(sandbox):
        return _exec_host(argv)

    cmd = shlex.join(argv)
    timeout = int(getattr(cfg, "sandbox_agent_timeout", 1800) or 1800)
    network = bool(getattr(cfg, "sandbox_network_agent", False))
    return sandbox.run_pipe(cmd, cwd=Path.cwd(), env=dict(os.environ),
                            timeout=timeout, network=network)


if __name__ == "__main__":
    sys.exit(main())
