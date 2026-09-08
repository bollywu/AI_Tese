"""CoverageBackend 契约：把「清零 + 采集」从 gcov 里解耦出来，按语言分发。

目标（见 docs/PLAN_gojava_backend.md）：AIcoverage 的状态机（loop/报告/增量）只消费
语言无关的 CoverageReport；被语言绑死的只有四个触点，backend 抽象覆盖其中
「每轮清零 + 覆盖采集 + 构建校验」三个，让 Go(covdata/coverprofile) 与 Java(JaCoCo)
能以后端形式接入，C/C++ 的 gcov 后端保持既有行为完全不变。
"""
from __future__ import annotations


class BackendNotReady(RuntimeError):
    """后端依赖的工具链未就绪（如本机缺 go/java），携带安装提示。"""

    def __init__(self, language: str, hint: str):
        super().__init__(f"[backend:{language}] 不可用：{hint}")
        self.language = language
        self.hint = hint


class CoverageBackend:
    """一个覆盖率后端的实现面。

    collect 返回 aicoverage.gcov.CoverageReport（同一 schema，loop/htmlreport 零改动）。
    未实现的方法默认抛 BackendNotReady/NotImplementedError，由子类覆写。
    """

    language: str = ""
    name: str = ""

    # ── 构建后校验：返回错误列表（空 = 通过）──────────────────
    def verify_build(self, cfg) -> list[str]:
        return []

    # ── 每轮执行前的计数清零 ──────────────────────────────────
    def clean(self, cfg) -> None:
        raise NotImplementedError(f"{self.name} backend 未实现 clean()")

    # ── 采集：归一化进 CoverageReport ─────────────────────────
    def collect(self, cfg, *, include_filter=None, exclude_filter=None,
                ut_dir=None, out_dir=None):
        raise NotImplementedError(f"{self.name} backend 未实现 collect()")
