"""C/C++ 的 gcov 后端：直接复用既有 aicoverage.gcov（clean_gcda / collect），零行为变化。

历史实现（executor/loop/cli 里裸调 clean_gcda + gcov_collect）被挪到这里，
对外接口保持原参数语义，确保 C/C++ 冒烟回归与旧结果完全一致。
"""
from __future__ import annotations

from .base import CoverageBackend


class GcovBackend(CoverageBackend):
    def __init__(self, language: str):
        self.language = language          # "c" | "cpp"
        self.name = "gcov"

    def verify_build(self, cfg) -> list[str]:
        from ..gcov import find_gcno_files
        if not find_gcno_files(cfg.source_path):
            return ["构建后未发现 .gcno——build_cmd 大概率没带 --coverage 插桩，覆盖率将恒为 0%"]
        return []

    def clean(self, cfg) -> None:
        from ..gcov import clean_gcda
        clean_gcda(cfg.source_path)

    def collect(self, cfg, *, include_filter=None, exclude_filter=None,
                ut_dir=None, out_dir=None):
        from ..gcov import collect as gcov_collect
        return gcov_collect(
            cfg.source_path, cfg.gcov_bin,
            include_filter=include_filter, exclude_filter=exclude_filter,
            ut_dir=ut_dir,
        )
