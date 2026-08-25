"""Environment variable loading: .env files (auth and other environment-level config, not project config).

Separated from project config (aicoverage.toml) -- auth is an "environment" concern,
not a "project" one. Lookup order: $AICOV_ENV-specified file > AIcoverage/.env.
Existing env vars are not overwritten (command-line export wins). Silently skipped
when AIcoverage/.env does not exist.
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
