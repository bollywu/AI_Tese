"""覆盖率后端门面：executor/loop/cli 通过 get_backend(cfg) 清零与采集，不再直连 gcov。

语言 → 后端：c/cpp → gcov（既有行为原样保留）；go → GoBackend（占位，待 M1）；
java → JavaBackend（占位，待 M2）。见 docs/PLAN_gojava_backend.md。
"""
from __future__ import annotations

from .base import BackendNotReady, CoverageBackend
from .gcov_backend import GcovBackend
from .go_backend import GoBackend
from .java_backend import JavaBackend

_BACKENDS: dict[str, CoverageBackend] = {
    "c": GcovBackend("c"),
    "cpp": GcovBackend("cpp"),
    "go": GoBackend(),
    "java": JavaBackend(),
}


def get_backend(cfg) -> CoverageBackend:
    """按 cfg.language 取后端；未知语言回退 gcov（fail-fast 交给 config 校验）。"""
    return _BACKENDS.get(getattr(cfg, "language", "c"), _BACKENDS["c"])


__all__ = ["CoverageBackend", "BackendNotReady", "GcovBackend",
           "GoBackend", "JavaBackend", "get_backend"]
