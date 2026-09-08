"""Java（JaCoCo）覆盖率后端（占位 + 工具链就绪检查，功能实现在 M2 里程碑）。

流程（见 docs/PLAN_gojava_backend.md §3）：
  构建产出 class/jar → 运行期 `-javaagent:jacocoagent.jar` 插桩 → 停服落盘 jacoco.exec →
  `jacococli report --xml` → 解析 XML（method/branch/line）归一化为 CoverageReport。
当前只提供 clean（删除上次 exec）与就绪检查；XML 解析作为 M2 里程碑独立落地。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import BackendNotReady, CoverageBackend


def _exec_file(cfg) -> Path:
    d = getattr(cfg, "java_exec_dir", "") or ".aicoverage/jacoco"
    p = Path(d).expanduser()
    base = p if p.is_absolute() else Path(cfg.source_path) / p
    return base / "jacoco.exec"


class JavaBackend(CoverageBackend):
    language = "java"
    name = "jacoco"

    def _ensure(self, cfg):
        msgs = []
        if not getattr(cfg, "java_agent", ""):
            msgs.append("aicoverage.toml 缺少 [java] jacoco_agent（jacocoagent.jar 路径）")
        if shutil.which("java") is None:
            msgs.append("本机未安装 java 运行环境")
        if msgs:
            raise BackendNotReady("java", "；".join(msgs))

    def verify_build(self, cfg) -> list[str]:
        errs = []
        if not getattr(cfg, "java_agent", ""):
            errs.append("[java] jacoco_agent 未配置（jacocoagent.jar 路径）")
        if shutil.which("java") is None:
            errs.append("本机未安装 java 运行环境")
        return errs

    def clean(self, cfg) -> None:
        f = _exec_file(cfg)
        if f.exists():
            f.unlink()

    def collect(self, cfg, *, include_filter=None, exclude_filter=None,
                ut_dir=None, out_dir=None):
        self._ensure(cfg)
        raise NotImplementedError(
            "JavaBackend.collect 尚未实现（M2 里程碑）：需运行 jacococli report --xml "
            "并解析 method/branch/line 归一化为 CoverageReport。")
