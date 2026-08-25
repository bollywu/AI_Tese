"""gcov 覆盖率后端：解析 `gcov -i -b` 的 JSON 中间格式（.gcov.json / .gcov.json.gz）。

gcc ≥ 9 支持 `-i`（intermediate JSON），gcc 12 默认 gzip 压缩输出。JSON 结构：

    {"gcc_version": "...", "format_version": "1",
     "files": [{
         "file": "src/wrk.c",
         "current_working_directory": "/build/cwd",     # 编译时 cwd（相对路径还原用）
         "functions": [{"name", "demangled_name", "start_line", "end_line",
                        "execution_count", "blocks", "blocks_executed", ...}],
         "lines": [{"line_number", "count", "unexecuted_block",
                    "function_name",
                    "branches": [{"count", "fallthrough", "throw"}, ...]}]
     }]}

指标口径（对齐经典覆盖率工具的口径）：
- 函数覆盖率 = execution_count > 0 的函数 / 全部函数
- 分支覆盖率 = count > 0 的分支（taken at least once）/ 全部分支
- 行覆盖率 = count > 0 的行 / 全部行（辅助指标）
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ── 数据模型 ────────────────────────────────────────────────────────

@dataclass
class FunctionCov:
    file: str                 # 相对源码根（规范化后）
    name: str                 # 函数名（demangled 优先）
    start_line: int
    end_line: int
    execution_count: int
    blocks: int
    blocks_executed: int
    ut_hit: bool = False      # True = 该函数仅被单测 driver 覆盖（E2E 未命中）

    @property
    def hit(self) -> bool:
        return self.execution_count > 0

    def to_dict(self) -> dict:
        return {"file": self.file, "name": self.name,
                "start_line": self.start_line, "end_line": self.end_line,
                "execution_count": self.execution_count, "hit": self.hit,
                "blocks": self.blocks, "blocks_executed": self.blocks_executed,
                "ut_hit": self.ut_hit}


@dataclass
class BranchCov:
    file: str
    line: int
    function: str
    count: int
    fallthrough: bool
    throw: bool

    @property
    def hit(self) -> bool:
        return self.count > 0


@dataclass
class FileCov:
    file: str
    functions: dict[str, FunctionCov] = field(default_factory=dict)   # name -> cov
    branches: list[BranchCov] = field(default_factory=list)
    lines_total: int = 0
    lines_hit: int = 0
    # 行号 → 执行次数（仅 gcov 认定的可执行行；HTML 报告逐行着色用）
    line_counts: dict[int, int] = field(default_factory=dict)


@dataclass
class CoverageReport:
    """一次覆盖率采集的完整快照（可序列化为 coverage.json）。"""
    created_at: str = ""
    files: dict[str, FileCov] = field(default_factory=dict)   # rel file -> FileCov

    # ── 聚合指标 ──
    @property
    def functions(self) -> list[FunctionCov]:
        return [f for fc in self.files.values() for f in fc.functions.values()]

    @property
    def func_total(self) -> int:
        return sum(len(fc.functions) for fc in self.files.values())

    @property
    def func_hit(self) -> int:
        return sum(1 for f in self.functions if f.hit)

    @property
    def func_pct(self) -> float:
        return round(self.func_hit * 100.0 / self.func_total, 2) if self.func_total else 0.0

    @property
    def branch_total(self) -> int:
        return sum(len(fc.branches) for fc in self.files.values())

    @property
    def branch_hit(self) -> int:
        return sum(1 for fc in self.files.values() for b in fc.branches if b.hit)

    @property
    def cond_pct(self) -> float:
        return round(self.branch_hit * 100.0 / self.branch_total, 2) if self.branch_total else 0.0

    @property
    def line_total(self) -> int:
        return sum(fc.lines_total for fc in self.files.values())

    @property
    def line_hit(self) -> int:
        return sum(fc.lines_hit for fc in self.files.values())

    @property
    def line_pct(self) -> float:
        return round(self.line_hit * 100.0 / self.line_total, 2) if self.line_total else 0.0

    def uncovered_functions(self) -> list[FunctionCov]:
        """未覆盖函数（按文件排序、行号排序，执行次数为 0）。"""
        return sorted(
            (f for f in self.functions if not f.hit),
            key=lambda f: (f.file, f.start_line),
        )

    def delta(self, previous: "CoverageReport | None") -> dict:
        """相对上一轮的增量（pp = 百分点）。"""
        if previous is None:
            return {"func_pp": self.func_pct, "cond_pp": self.cond_pct,
                    "newly_hit": [f.to_dict() for f in self.functions if f.hit]}
        prev_hit = {(f.file, f.name) for f in previous.functions if f.hit}
        newly = [f.to_dict() for f in self.functions
                 if f.hit and (f.file, f.name) not in prev_hit]
        return {
            "func_pp": round(self.func_pct - previous.func_pct, 2),
            "cond_pp": round(self.cond_pct - previous.cond_pct, 2),
            "newly_hit": newly,
        }

    # ── 序列化 ──
    def to_dict(self) -> dict:
        return {
            "created_at": self.created_at or datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "func_total": self.func_total, "func_hit": self.func_hit,
                "func_pct": self.func_pct,
                "branch_total": self.branch_total, "branch_hit": self.branch_hit,
                "cond_pct": self.cond_pct,
                "line_total": self.line_total, "line_hit": self.line_hit,
                "line_pct": self.line_pct,
                "uncovered_func_count": len(self.uncovered_functions()),
            },
            "files": {
                rel: {
                    "functions": [f.to_dict() for f in sorted(fc.functions.values(),
                                                              key=lambda x: x.start_line)],
                    "branches": [
                        {"line": b.line, "function": b.function, "count": b.count,
                         "fallthrough": b.fallthrough, "throw": b.throw}
                        for b in sorted(fc.branches, key=lambda x: x.line)
                    ],
                    "branch_total": len(fc.branches),
                    "branch_hit": sum(1 for b in fc.branches if b.hit),
                    "lines_total": fc.lines_total,
                    "lines_hit": fc.lines_hit,
                    # 行号→计数（HTML 报告逐行着色用；key 转字符串以符合 JSON 规范）
                    "line_counts": {str(k): v for k, v in sorted(fc.line_counts.items())},
                }
                for rel, fc in sorted(self.files.items())
            },
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "CoverageReport":
        """从 coverage.json 还原（含 branches/lines，保证跨轮 delta 与阈值判定正确）。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        report = cls(created_at=data.get("created_at", ""))
        for rel, fc_data in data.get("files", {}).items():
            fc = FileCov(file=rel)
            for fd in fc_data.get("functions", []):
                fc.functions[fd["name"]] = FunctionCov(
                    file=rel, name=fd["name"],
                    start_line=fd.get("start_line", 0), end_line=fd.get("end_line", 0),
                    execution_count=fd.get("execution_count", 0),
                    blocks=fd.get("blocks", 0), blocks_executed=fd.get("blocks_executed", 0),
                    ut_hit=bool(fd.get("ut_hit", False)),
                )
            for bd in fc_data.get("branches", []):
                fc.branches.append(BranchCov(
                    file=rel, line=bd.get("line", 0), function=bd.get("function", ""),
                    count=bd.get("count", 0), fallthrough=bd.get("fallthrough", False),
                    throw=bd.get("throw", False),
                ))
            fc.lines_total = fc_data.get("lines_total", 0)
            fc.lines_hit = fc_data.get("lines_hit", 0)
            fc.line_counts = {
                int(k): int(v) for k, v in (fc_data.get("line_counts") or {}).items()
            }
            report.files[rel] = fc
        return report

    def summary_text(self) -> str:
        """人类可读摘要（终端/报告通用）。"""
        lines = [
            f"函数覆盖: {self.func_hit}/{self.func_total} = {self.func_pct:.2f}%",
            f"分支覆盖: {self.branch_hit}/{self.branch_total} = {self.cond_pct:.2f}%",
            f"行覆盖:   {self.line_hit}/{self.line_total} = {self.line_pct:.2f}%",
        ]
        unc = self.uncovered_functions()
        if unc:
            lines.append(f"未覆盖函数: {len(unc)} 个（前 20）:")
            for f in unc[:20]:
                lines.append(f"  - {f.file}:{f.start_line} {f.name}")
        return "\n".join(lines)


# ── 采集 ────────────────────────────────────────────────────────────

def find_gcno_files(source_root: Path, exclude_dir: Path | None = None) -> list[Path]:
    """源码树下的全部 .gcno（插桩编译单元标记文件）。"""
    results: list[Path] = []
    for p in sorted(source_root.rglob("*.gcno")):
        if exclude_dir is not None:
            try:
                p.resolve().relative_to(exclude_dir.resolve())
                continue    # 测试目录内的产物跳过
            except ValueError:
                pass
        results.append(p)
    return results


def clean_gcda(source_root: Path, exclude_dir: Path | None = None) -> int:
    """清除全部 .gcda（运行时计数文件），返回删除数量。"""
    n = 0
    for p in source_root.rglob("*.gcda"):
        if exclude_dir is not None:
            try:
                p.resolve().relative_to(exclude_dir.resolve())
                continue
            except ValueError:
                pass
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _read_gcov_json(path: Path) -> dict | None:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                return json.load(f)
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, gzip.BadGzipFile):
        return None


def _normalize_file(file_field: str, compile_cwd: str, source_root: Path) -> str | None:
    """gcov JSON 里的 file 字段还原为相对 source_root 的规范化路径。"""
    if not file_field:
        return None
    p = Path(file_field)
    if not p.is_absolute() and compile_cwd:
        p = Path(compile_cwd) / p
    p = Path(p.resolve()) if p.is_absolute() else p
    try:
        return p.resolve().relative_to(source_root).as_posix()
    except ValueError:
        return None


def collect(
    source_root: Path,
    gcov_bin: str = "gcov",
    *,
    include_filter=None,
    exclude_filter=None,
    out_dir: Path | None = None,
    timeout_per_file: int = 60,
    ut_dir: Path | None = None,
) -> CoverageReport:
    """执行 gcov 并汇总覆盖率。

    步骤：
    1. 找到全部 .gcno（无 .gcda 时 gcov 仍会输出全 0 计数——即"函数清单基线"）
    2. 逐个 `gcov -i -b <gcno>`，**每个 gcno 用独立子目录**输出 .gcov.json[.gz]
       （2026-08-24 修复事故①：libtool 项目常见"静态 + PIC 共享库"双重编译，
       同一源文件产生两份同 basename 的 .gcno，旧实现把所有输出扁平堆到同一
       目录，同名文件互相覆盖。独立子目录消除了文件名碰撞。）
    3. 按 (file) 聚合**全部**原始记录（同一 rel 路径可能有多份，来自双重编译），
       再按 (file, line/function) 逐项取「计数更大」的一份合并（详见下方合并逻辑）。
       （2026-08-24 修复事故②——ModSecurity 真实闭环 iter6 中被 gen-agent
       自行用 gcov 实测发现：子目录用未补零整数字符串命名（"0","1",...,"122"），
       旧实现按 `sorted(路径字符串)` 决定处理顺序、"先到先得"（seen_files）；
       但字符串序不是数值序（`"122" < "56"`），当"无 .gcda 的静态编译"子目录
       字符串序小于"有 .gcda 的真实份"时，零数据反而先写入并占位，真实覆盖
       被读成 0%——iter6 全部 25 个目标函数命中此 bug，新用例的真实覆盖贡献
       被完全吞掉，导致覆盖率与 iter5 完全相同、看似"未推进"。
       现在的合并策略不依赖任何处理顺序：对每个 (file, line) 取全部重复编译
       记录里 **count 最大**的一份（真实执行数据的 count 天然 ≥ 0 数据，任何
       顺序下都会胜出），从根本上消除排序依赖。）
    """
    report = CoverageReport(created_at=datetime.now().isoformat(timespec="seconds"))
    gcno_files = find_gcno_files(source_root)
    if not gcno_files:
        return report

    # 单测来源判定：ut_dir（如 .aicoverage/ut/）下的 .gcno 属于单测 driver 产物。
    # 命中但 E2E（非 ut 目录）未命中的函数 → ut_hit=True（仅被单测覆盖）。
    ut_root = Path(ut_dir).resolve() if ut_dir else None

    work_dir = out_dir or (source_root / ".aicoverage" / "coverage_raw")
    # 每次全新开始（旧实现的部分 glob 清理无法应对新增的子目录结构）
    if work_dir.exists():
        import shutil as _shutil
        _shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    from .globutil import glob_matches

    # 每个子目录 gcno 的 ut 标记：子目录序号 → 是否单测来源。
    # gcov 执行用线程池并行（每个 .gcno 是独立子进程、写独立子目录，互不干扰），
    # 大项目 .gcno 众多时显著提速（P3 性能优化）。
    gcno_is_ut: dict[int, bool] = {}

    def _run_gcov(i_gcno: tuple[int, Path]) -> bool:
        i, gcno = i_gcno
        sub = work_dir / str(i)
        sub.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [gcov_bin, "-i", "-b", "-c", str(gcno)],
                cwd=sub, capture_output=True, timeout=timeout_per_file,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        try:
            return ut_root is not None and gcno.resolve().is_relative_to(ut_root)
        except ValueError:
            return False

    import concurrent.futures as _cf
    workers = min(8, max(1, (os.cpu_count() or 4)))
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_run_gcov, list(enumerate(gcno_files))))
    for i, is_ut in enumerate(results):
        gcno_is_ut[i] = is_ut

    # 顺序无关：不再 sorted()，处理顺序完全不影响合并结果
    json_paths = list(work_dir.rglob("*.gcov.json")) + list(work_dir.rglob("*.gcov.json.gz"))

    # 按 rel 路径收集全部原始 file_entry（同一 rel 可能有多份，来自双重编译），
    # 附带该记录是否来自单测产物（用于区分 E2E/单测覆盖来源）
    raw_by_rel: dict[str, list[tuple[dict, bool]]] = {}
    for jp in json_paths:
        data = _read_gcov_json(jp)
        if not isinstance(data, dict) or "files" not in data:
            continue
        compile_cwd = data.get("current_working_directory", "")
        # jp 位于 work_dir/<序号>/ 下，反查该 gcno 的 ut 标记
        try:
            idx = int(jp.parent.name)
        except ValueError:
            idx = -1
        is_ut = gcno_is_ut.get(idx, False)
        for file_entry in data["files"]:
            rel = _normalize_file(file_entry.get("file", ""), compile_cwd, source_root)
            if rel is None:
                continue
            if include_filter and not glob_matches(rel, include_filter):
                continue
            if exclude_filter and glob_matches(rel, exclude_filter):
                continue
            raw_by_rel.setdefault(rel, []).append((file_entry, is_ut))

    for rel, entries in raw_by_rel.items():
        fc = FileCov(file=rel)

        # 函数：同名函数在多份重复编译中取 execution_count 更大的一份。
        # 同时维护"仅 E2E（非单测来源）"的最优统计，用于判定 ut_hit。
        func_best: dict[str, dict] = {}
        func_best_e2e: dict[str, dict] = {}
        for entry, is_ut in entries:
            for fn in entry.get("functions", []):
                name = fn.get("demangled_name") or fn.get("name") or ""
                if not name:
                    continue
                prev = func_best.get(name)
                if prev is None or int(fn.get("execution_count", 0)) > int(prev.get("execution_count", 0)):
                    func_best[name] = fn
                if not is_ut:
                    prev_e = func_best_e2e.get(name)
                    if prev_e is None or int(fn.get("execution_count", 0)) > int(prev_e.get("execution_count", 0)):
                        func_best_e2e[name] = fn
        for name, fn in func_best.items():
            e2e_fn = func_best_e2e.get(name)
            e2e_hit = bool(e2e_fn and int(e2e_fn.get("execution_count", 0)) > 0)
            hit = int(fn.get("execution_count", 0)) > 0
            fc.functions[name] = FunctionCov(
                file=rel, name=name,
                start_line=int(fn.get("start_line", 0)),
                end_line=int(fn.get("end_line", 0)),
                execution_count=int(fn.get("execution_count", 0)),
                blocks=int(fn.get("blocks", 0)),
                blocks_executed=int(fn.get("blocks_executed", 0)),
                ut_hit=bool(hit and not e2e_hit),
            )

        # 行 + 分支：按行号取「计数更大」的一份（该行的分支列表整体随之带走，
        # 保持同一来源内 T/F 顺序一致，避免跨份错位配对）
        line_best_count: dict[int, int] = {}
        line_best_branches: dict[int, list[dict]] = {}
        line_best_fname: dict[int, str] = {}
        for entry, _is_ut in entries:
            for ln in entry.get("lines", []):
                line_no = int(ln.get("line_number", 0))
                count = int(ln.get("count", 0))
                if line_no not in line_best_count or count > line_best_count[line_no]:
                    line_best_count[line_no] = count
                    line_best_branches[line_no] = ln.get("branches", []) or []
                    line_best_fname[line_no] = ln.get("function_name", "") or ""

        fc.lines_total = len(line_best_count)
        fc.lines_hit = sum(1 for c in line_best_count.values() if c > 0)
        fc.line_counts = dict(line_best_count)
        for line_no, branches in line_best_branches.items():
            fname = line_best_fname.get(line_no, "")
            for br in branches:
                fc.branches.append(BranchCov(
                    file=rel, line=line_no, function=fname,
                    count=int(br.get("count", 0)),
                    fallthrough=bool(br.get("fallthrough", False)),
                    throw=bool(br.get("throw", False)),
                ))

        report.files[rel] = fc
    return report
