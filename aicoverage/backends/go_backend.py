"""Go 覆盖率后端（占位 + 工具链就绪检查，功能实现在 M1 里程碑）。

流程（见 docs/PLAN_gojava_backend.md §2）：
  go build -cover 构建 → 运行服务时设 GOCOVERDIR → 关停后
  go tool covdata merge → coverprofile → 解析归一化为 CoverageReport。
本文件当前只提供 clean（清空 GOCOVERDIR）与就绪检查；collect 的实现需要 Go
工具链与 go/parser AST 归因，作为 M1 里程碑独立落地。
"""
from __future__ import annotations

import shutil

from .base import BackendNotReady, CoverageBackend


def _coverdir(cfg):
    import os
    from pathlib import Path
    v = getattr(cfg, "go_coverdir", "") or ".aicoverage/coverdata"
    p = Path(v).expanduser()
    return p if p.is_absolute() else Path(cfg.source_path) / p


class GoBackend(CoverageBackend):
    language = "go"
    name = "go"

    def _ensure_go(self):
        if shutil.which("go") is None:
            raise BackendNotReady(
                "go",
                "本机未安装 Go 工具链（需 ≥1.20 支持 `go build -cover` / `go tool covdata`）。"
                "请安装 Go 后再运行，或先为 C/C++ 目标使用默认 gcov 后端。")

    def verify_build(self, cfg) -> list[str]:
        # 无 go 时给出明确提示（不阻断 config 加载与源码列举冒烟）
        if shutil.which("go") is None:
            return ["本机未安装 Go 工具链，Go 目标的构建/采集需安装 go≥1.20"]
        return []

    def clean(self, cfg) -> None:
        import shutil as _sh
        d = _coverdir(cfg)
        if d.exists():
            _sh.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)

    def collect(self, cfg, *, include_filter=None, exclude_filter=None,
                ut_dir=None, out_dir=None):
        self._ensure_go()
        raise NotImplementedError(
            "GoBackend.collect 尚未实现（M1 里程碑）：需 `go tool covdata merge` 产出 "
            "coverprofile 后，用 go/parser AST 做函数归因并归一化为 CoverageReport。")
