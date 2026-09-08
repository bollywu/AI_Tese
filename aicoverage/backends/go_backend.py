"""Go 覆盖率后端：GOCOVERDIR/covdata 与 coverprofile → 统一 CoverageReport。

流程（docs/PLAN_gojava_backend.md §2）：
  ① `go build -cover`（或 `go test -coverprofile`）产物；
  ② 运行期计数：E2E 起服务进程时设 GOCOVERDIR；或直接跑 `go test -coverpkg=./...`；
  ③ collect 三源取一（按优先级）：
     a. 显式 coverprofile 已存在（外部 `go test`/`go tool covdata textfmt` 产出）→ 直接解析；
     b. GOCOVERDIR 非空 → `go tool covdata merge` + `covdata textfmt` 转文本 profile → 解析；
     c. 兜底 → 自动 `go test -coverpkg=<packages> -coverprofile=... <packages>`（单测通道）。

解析：coverprofile 基本块 → 逐行 line_counts；函数清单用轻量 Go 源码扫描
（brace 深度，与 C 端 source.py 同思路；不依赖 `go tool cover -func`，规避其
main 包 importcfg 解析缺陷），函数命中 = 体内存在 count>0 的行。
Go 无分支覆盖语义 → CoverageReport.branch_total=0（cond 走 vacuous，见文档 §2.2）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..gcov import CoverageReport, FileCov, FunctionCov
from .base import BackendNotReady, CoverageBackend

# coverprofile 基本块行：<file>:<sl>.<sc>,<el>.<ec> <numStmt> [<count>]
_BLOCK_RE = re.compile(
    r"^(?P<file>.*?):(?P<sl>\d+)\.(?P<sc>\d+),(?P<el>\d+)\.(?P<ec>\d+)"
    r"\s+(?P<num>\d+)(?:\s+(?P<count>\d+))?$")

# Go 函数定义行（单行形态）：func [ (receiver) ] name[generics]( ... 
_FUNC_LINE_RE = re.compile(r"^\s*func\s+(?:(?P<recv>\([^)]*\))\s*)?(?P<name>[A-Za-z_]\w*)")


@dataclass
class _GoFunc:
    file: str            # 相对 source_path
    name: str            # 展示名：method 带接收者类型前缀
    start: int
    end: int


def _resolver(cfg, key: str, default: str) -> Path:
    """把 [go] 段的相对路径字段解析到 source_path 下。"""
    v = getattr(cfg, key, "") or default
    p = Path(v).expanduser()
    return p if p.is_absolute() else Path(cfg.source_path) / p


class GoBackend(CoverageBackend):
    language = "go"
    name = "go"

    def _go_bin(self, cfg) -> str:
        return getattr(cfg, "go_bin", "") or shutil.which("go") or "go"

    def _ensure_go(self, cfg):
        if shutil.which(self._go_bin(cfg)) is None:
            raise BackendNotReady(
                "go",
                "本机未安装 Go 工具链（需 ≥1.20 支持 `go build -cover` / `go tool covdata`）。"
                "请安装 Go 后再运行，或先为 C/C++ 目标使用默认 gcov 后端。")

    def verify_build(self, cfg) -> list[str]:
        if shutil.which(self._go_bin(cfg)) is None:
            return ["本机未安装 Go 工具链，Go 目标的构建/采集需安装 go≥1.20"]
        return []

    # ── 每轮清零：删旧 profile/merge 产物并清空 GOCOVERDIR ─────────
    def clean(self, cfg) -> None:
        d = _resolver(cfg, "go_coverdir", ".aicoverage/coverdata")
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        prof = _resolver(cfg, "go_coverprofile", ".aicoverage/cover.out")
        if prof.exists():
            prof.unlink()
        merged = Path(cfg.source_path) / ".aicoverage/cov_merged"
        if merged.exists():
            shutil.rmtree(merged, ignore_errors=True)

    # ── 采集：确保一份 coverprofile 后解析为 CoverageReport ─────────
    def collect(self, cfg, *, include_filter=None, exclude_filter=None,
                ut_dir=None, out_dir=None):
        self._ensure_go(cfg)
        prof = self._profile(cfg)
        return parse_coverprofile(prof, cfg, include_filter=include_filter,
                                  exclude_filter=exclude_filter)

    def _profile(self, cfg) -> Path:
        """按优先级取得/生成 coverprofile 路径（见模块 docstring）。"""
        prof = _resolver(cfg, "go_coverprofile", ".aicoverage/cover.out")
        if prof.exists():
            return prof

        coverdir = _resolver(cfg, "go_coverdir", ".aicoverage/coverdata")
        entries = list(coverdir.rglob("*")) if coverdir.exists() else []
        if entries:
            # E2E：GOCOVERDIR → covdata merge → textfmt
            merged = Path(cfg.source_path) / ".aicoverage/cov_merged"
            if merged.exists():
                shutil.rmtree(merged, ignore_errors=True)
            prof.parent.mkdir(parents=True, exist_ok=True)
            self._run(cfg, ["tool", "covdata", "merge", "-o", str(merged),
                            *[str(x) for x in coverdir.iterdir() if x.is_file()]])
            self._run(cfg, ["tool", "covdata", "textfmt", "-i", str(merged), "-o", str(prof)])
            return prof

        # 兜底：单测通道 `go test -coverpkg=... -coverprofile=... <pkgs>`
        pkgs = getattr(cfg, "go_packages", None) or ["..."]
        tags = (getattr(cfg, "go_tags", "") or "").strip()
        cmd = ["test", "-count=1",
               "-coverpkg=" + ",".join(pkgs), "-coverprofile=" + str(prof)]
        if tags:
            cmd += ["-tags", tags]
        prof.parent.mkdir(parents=True, exist_ok=True)
        self._run(cfg, [*cmd, *pkgs])
        return prof

    def _run(self, cfg, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [self._go_bin(cfg), *args]
        try:
            proc = subprocess.run(cmd, cwd=str(cfg.source_path), capture_output=True,
                                  text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"go {' '.join(args[:3])}… 超时（1800s）")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-3000:]
            raise RuntimeError(f"go {' '.join(args[:3])}… 失败 rc={proc.returncode}：\n{tail}")
        return proc


# ── profile → CoverageReport ──────────────────────────────────────

def _module_name(cfg) -> str:
    """从 go.mod 读 module 前缀，用于把 import-path 还原为相对源码路径。"""
    try:
        for ln in (cfg.source_path / "go.mod").read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("module "):
                return ln[len("module "):].strip().strip('"')
    except OSError:
        pass
    return ""


def _rel_go_path(file: str, module: str) -> str:
    """port-management-system/internal/a.go → internal/a.go"""
    f = file.replace("\\", "/")
    if module and f.startswith(module + "/"):
        return f[len(module) + 1:]
    if f.startswith("/"):
        return f.lstrip("/")
    return f


def _scan_go_functions(cfg) -> dict[str, list[_GoFunc]]:
    """brace 深度扫描每个 .go 源文件 → 文件内的函数 (start,end)。"""
    out: dict[str, list[_GoFunc]] = {}
    for path in cfg.source_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = path.relative_to(cfg.source_path).as_posix()
        funcs: list[_GoFunc] = []
        n = len(lines)
        i = 0
        while i < n:
            m = _FUNC_LINE_RE.match(lines[i])
            if not m:
                i += 1
                continue
            name = m.group("name")
            if name in ("main", "init") and m.group("recv"):
                name = m.group("recv").split()[-1].strip("*()") + "." + name
            elif m.group("recv"):
                rtype = m.group("recv").split()[-1].strip("*()")
                name = rtype + "." + name
            # 找函数体闭合行
            bal, seen_open, end = 0, False, i
            j = i
            while j < n:
                for ch in lines[j]:
                    if ch == "{":
                        bal += 1
                        seen_open = True
                    elif ch == "}":
                        bal -= 1
                end = j
                j += 1
                if seen_open and bal <= 0:
                    break
            if not seen_open:
                end = i  # 声明式（罕见）；仅算本行
            funcs.append(_GoFunc(file=rel, name=name, start=i + 1, end=end + 1))
            i = max(i + 1, end + 1)
        if funcs:
            out[rel] = funcs
    return out


def parse_coverprofile(profile: Path, cfg, *, include_filter=None,
                       exclude_filter=None) -> CoverageReport:
    from ..globutil import glob_matches
    module = _module_name(cfg)

    # 1) 块级逐行计数：rel_file -> {line: max_count}
    line_max: dict[str, dict[int, int]] = {}
    try:
        text = profile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return CoverageReport()
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("mode:"):
            continue
        m = _BLOCK_RE.match(raw)
        if not m:
            continue
        rel = _rel_go_path(m.group("file"), module)
        if include_filter and not glob_matches(rel, include_filter):
            continue
        if exclude_filter and glob_matches(rel, exclude_filter):
            continue
        sl, el = int(m.group("sl")), int(m.group("el"))
        count = int(m.group("count")) if m.group("count") is not None else 1
        counts = line_max.setdefault(rel, {})
        for ln in range(sl, el + 1):
            counts[ln] = max(counts.get(ln, 0), count)

    # 2) 函数清单（仅统计出现过的文件，避免对未插桩文件产生空分母）
    funcs_by_file = _scan_go_functions(cfg)

    report = CoverageReport()
    for rel, counts in sorted(line_max.items()):
        fc = FileCov(file=rel)
        fc.line_counts = counts
        fc.lines_total = len(counts)
        fc.lines_hit = sum(1 for v in counts.values() if v > 0)
        # 函数归因：落在函数体[start,end]且有 count>0 行 → 命中
        for f in funcs_by_file.get(rel, []):
            body_lines = [ln for ln in range(f.start, f.end + 1) if ln in counts]
            cov_lines = [ln for ln in body_lines if counts[ln] > 0]
            hit = len(cov_lines) > 0
            fc.functions[f.name] = FunctionCov(
                file=rel, name=f.name, start_line=f.start, end_line=f.end,
                execution_count=len(cov_lines) if hit else 0,
                blocks=len(body_lines), blocks_executed=len(cov_lines),
            )
        # 分支：Go 无分支覆盖语义，留空（cond 由上层 vacuous 处理）
        report.files[rel] = fc
    return report
