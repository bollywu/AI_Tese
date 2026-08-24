"""环境变量加载：.env 文件（认证等环境级配置，非项目配置）。

与项目配置（aicoverage.toml）分离——认证是「环境」问题，不是「项目」问题。
查找顺序：$AICOV_ENV 指定文件 > AIcoverage/.env。已有环境变量不覆盖
（命令行 export 优先）。AIcoverage/.env 不存在时静默跳过。
"""
from __future__ import annotations

import os
from pathlib import Path

CLI_HOME = Path(__file__).resolve().parent.parent


def load_env_file() -> None:
    path = Path(os.environ.get("AICOV_ENV", "")).expanduser() if os.environ.get("AICOV_ENV") \
        else CLI_HOME / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
