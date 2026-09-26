"""全局测试夹具。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _silent_observability(monkeypatch):
    """静默观测事件：单测不往生产 runs/ 目录（或 cwd 回退路径）写 events.jsonl。

    observability 的设计契约是"不得成为闭环单点故障"，静默开关
    （AICOV_OBS_SILENT）本来就是为单测准备的——这里 autouse 全局生效。
    """
    monkeypatch.setenv("AICOV_OBS_SILENT", "1")
