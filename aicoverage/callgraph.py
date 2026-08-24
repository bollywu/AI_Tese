"""CodeGraph CLI 封装：调用链反向 BFS + 行区间函数归因 + 调用链聚类分批。

调用链分析能力基于真实项目数据验证过的实现，做了以下
通用化改造：
1. 入口锚点（entrypoints）为项目可配置列表（`aicoverage.toml` 的
   `[codegraph].entrypoints`），而非硬编码特定入口函数。
2. 不假设任何特定项目的函数指针间接调用模式——纯 CodeGraph AST 调用边
   已是"直接调用"的精确来源。若目标项目存在函数指针表/命令注册表等
   间接分发模式，可后续在 `aicoverage.toml` 里加桥接规则扩展点（YAGNI，
   暂不预先设计）。

真实 codegraph CLI 行为已用最小 C 项目验证（2026-08-24）：
- sqlite `nodes` 表字段：id/kind/name/qualified_name/file_path/start_line/
  end_line/signature 等，与本模块假设一致。
- `codegraph callers <symbol> --json` 返回 `{"callers": [{"name","kind",
  "filePath","startLine"}, ...]}`。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


class CodeGraphNotAvailable(RuntimeError):
    """CodeGraph CLI 不可用（未安装 / 索引未建立）。"""


# ── CLI 子进程封装 ─────────────────────────────────────────────

def _resolve_binary() -> str:
    configured = os.environ.get("CODEGRAPH_BIN", "").strip()
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("codegraph")
    if found:
        return found
    raise CodeGraphNotAvailable(
        "未找到 codegraph CLI。请设置环境变量 CODEGRAPH_BIN 指向可执行文件，"
        "或参考 https://github.com/colbymchenry/codegraph 安装。"
    )


def _run_json(args: list[str], cwd: Path, timeout: int = 30) -> dict:
    binary = _resolve_binary()
    try:
        result = subprocess.run(
            [binary, *args, "--json"],
            capture_output=True, text=True, timeout=timeout, cwd=str(cwd),
        )
    except FileNotFoundError as e:
        raise CodeGraphNotAvailable(f"codegraph 二进制不可执行: {binary}") from e
    except subprocess.TimeoutExpired as e:
        raise CodeGraphNotAvailable(f"codegraph 调用超时（{timeout}s）: {' '.join(args)}") from e
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _db_path(source_path: Path, index_dir: str = ".codegraph") -> Path:
    return source_path / index_dir / "codegraph.db"


def is_indexed(source_path: Path, index_dir: str = ".codegraph") -> bool:
    """判断 source_path 下是否已建立 CodeGraph 索引。"""
    return _db_path(source_path, index_dir).exists()


def ensure_index(source_path: Path, *, force: bool = False, timeout: int = 600) -> dict:
    """确保已建索引：未建立则 `codegraph init`，已建立则 `codegraph sync`（增量更新）。

    不在库内自动调用——由调用方（CLI 层）显式触发，避免用户在不知情的情况下
    等一个耗时未知的全量索引过程（对齐计划文档 Q2：默认报错提示手动执行，
    需要自动建索引的场景用本函数显式调用）。
    """
    binary = _resolve_binary()
    if is_indexed(source_path) and not force:
        result = subprocess.run(
            [binary, "sync"], cwd=str(source_path),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"action": "sync", "already_indexed": True,
                "returncode": result.returncode,
                "stdout": result.stdout[-2000:], "stderr": result.stderr[-1000:]}
    cmd = [binary, "index"] if is_indexed(source_path) else [binary, "init"]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, cwd=str(source_path), capture_output=True,
                            text=True, timeout=timeout)
    return {"action": cmd[1], "returncode": result.returncode,
            "stdout": result.stdout[-2000:], "stderr": result.stderr[-1000:]}


def index_sha(source_path: Path, index_dir: str = ".codegraph") -> str:
    """索引指纹（mtime+size，检测索引是否滞后于源码，不做全量哈希）。"""
    db = _db_path(source_path, index_dir)
    if not db.exists():
        return ""
    st = db.stat()
    return f"mtime{int(st.st_mtime)}_size{st.st_size}"


@dataclass
class CGSymbolRef:
    name: str
    kind: str
    file_path: str
    start_line: int


def callers(source_path: Path, symbol: str, limit: int = 50) -> list[CGSymbolRef]:
    """查询谁调用了 symbol（AST 引用消解，非文本正则匹配）。"""
    data = _run_json(["callers", symbol, "--limit", str(limit)], cwd=source_path)
    return [
        CGSymbolRef(name=c.get("name", ""), kind=c.get("kind", ""),
                   file_path=c.get("filePath", ""), start_line=c.get("startLine", 0))
        for c in data.get("callers", []) or []
    ]


def callees(source_path: Path, symbol: str, limit: int = 50) -> list[CGSymbolRef]:
    """查询 symbol 调用了谁。"""
    data = _run_json(["callees", symbol, "--limit", str(limit)], cwd=source_path)
    return [
        CGSymbolRef(name=c.get("name", ""), kind=c.get("kind", ""),
                   file_path=c.get("filePath", ""), start_line=c.get("startLine", 0))
        for c in data.get("callees", []) or []
    ]


# ── 行区间反查（diff 归因的核心，M1 关键能力）───────────────────

@dataclass
class CGFunctionRange:
    """函数的行区间 + 限定名（diff 行号 → 函数的确定性反查结果）。"""

    name: str
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str = ""


def functions_covering_lines(
    source_path: Path, file_path: str, lines: list[int],
    *, index_dir: str = ".codegraph", innermost_only: bool = True,
) -> list[CGFunctionRange]:
    """反查：给定文件的若干改动行，返回行区间命中的函数（带限定名）。

    Args:
        innermost_only: True（默认）时每个改动行只取区间最小（最内层）的函数。
            ⚠️ 必需——CodeGraph 对某些语言/写法的作用域推断会产生嵌套区间
            （如外层匿名包裹范围 + 内层真实宿主函数同时覆盖同一行），已在
            真实项目实测中验证过这个陷阱
            （案例：外层函数区间 [342-1574] 与内层真实宿主 [1230-1366] 都命中
            第 1235 行，若都返回会把同一处改动拆成两个目标）。取最内层
            （区间跨度最小）才是改动真正所属的函数。

    Returns:
        命中的函数列表（按 start_line 排序、去重）。空列表表示这些改动行
        不落在任何已索引函数体内（全局变量/宏/注释/类声明区），调用方应
        据此判定为 unresolved，**不得回退到猜测**。
    """
    if not lines:
        return []
    db_path = _db_path(source_path, index_dir)
    if not db_path.exists():
        raise CodeGraphNotAvailable(
            f"源码尚未建立 CodeGraph 索引: {source_path}\n"
            f"请先执行: cd {source_path} && codegraph init"
        )

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name, qualified_name, kind, file_path, start_line, end_line, "
            "COALESCE(signature, '') FROM nodes "
            "WHERE file_path = ? AND kind IN ('function', 'method') "
            "ORDER BY start_line",
            (file_path,),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT name, qualified_name, kind, file_path, start_line, end_line, "
                "COALESCE(signature, '') FROM nodes "
                "WHERE file_path LIKE ? AND kind IN ('function', 'method') "
                "ORDER BY start_line",
                (f"%{file_path}",),
            ).fetchall()
    finally:
        conn.close()

    ranges: list[CGFunctionRange] = []
    for name, qname, kind, fpath, start, end, sig in rows:
        if not end or end < start:
            continue
        ranges.append(CGFunctionRange(
            name=name, qualified_name=qname or name, kind=kind,
            file_path=fpath, start_line=start, end_line=end, signature=sig,
        ))

    hits: dict[tuple[str, int], CGFunctionRange] = {}
    for ln in set(lines):
        covering = [r for r in ranges if r.start_line <= ln <= r.end_line]
        if not covering:
            continue
        if innermost_only:
            covering = [min(covering, key=lambda r: (r.end_line - r.start_line, -r.start_line))]
        for r in covering:
            hits[(r.qualified_name, r.start_line)] = r
    return sorted(hits.values(), key=lambda f: f.start_line)


def list_functions_in_file(
    source_path: Path, file_path: str, *, index_dir: str = ".codegraph",
) -> list[CGSymbolRef]:
    """枚举某文件下的全部函数/方法（用于 `func_name="*"` 文件级展开场景）。"""
    db_path = _db_path(source_path, index_dir)
    if not db_path.exists():
        raise CodeGraphNotAvailable(
            f"源码尚未建立 CodeGraph 索引: {source_path}\n"
            f"请先执行: cd {source_path} && codegraph init"
        )
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name, kind, file_path, start_line FROM nodes "
            "WHERE file_path = ? AND kind IN ('function', 'method') "
            "ORDER BY start_line",
            (file_path,),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT name, kind, file_path, start_line FROM nodes "
                "WHERE file_path LIKE ? AND kind IN ('function', 'method') "
                "ORDER BY start_line",
                (f"%{file_path}",),
            ).fetchall()
        return [CGSymbolRef(name=r[0], kind=r[1], file_path=r[2], start_line=r[3]) for r in rows]
    finally:
        conn.close()


# ── 反向 BFS：调用链追溯到入口锚点 ───────────────────────────────

@dataclass
class CallPath:
    entry: str
    path: list[str]           # [entry, ..., target]

    def render(self) -> str:
        return " → ".join(self.path)


@dataclass
class TraceResult:
    target: str
    found: bool
    resolved: bool = True     # target 是否存在于索引中
    paths: list[CallPath] = field(default_factory=list)


def _bare_name(qualified: str) -> str:
    return qualified.split("::")[-1]


def _load_reverse_call_graph(source_path: Path, index_dir: str = ".codegraph") -> dict[str, list[str]]:
    """一次性从 CodeGraph sqlite 读取全量反向调用边到内存（一次 sqlite 扫描，
    避免逐函数启动 codegraph 子进程——真实项目实测 244 函数从 ~20min 降到 <5s）。"""
    db_path = _db_path(source_path, index_dir)
    if not db_path.exists():
        raise CodeGraphNotAvailable(
            f"源码尚未建立 CodeGraph 索引: {source_path}\n"
            f"请先执行: cd {source_path} && codegraph init"
        )
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        rows = conn.execute(
            "SELECT n_caller.name, n_callee.name "
            "FROM edges e "
            "JOIN nodes n_caller ON n_caller.id = e.source "
            "JOIN nodes n_callee ON n_callee.id = e.target "
            "WHERE e.kind = 'calls' "
            "AND n_caller.name IS NOT NULL AND n_callee.name IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    reverse: dict[str, list[str]] = {}
    for caller_name, callee_name in rows:
        reverse.setdefault(callee_name, []).append(caller_name)
    for k in reverse:
        reverse[k] = list(set(reverse[k]))
    return reverse


def trace_to_entrypoints(
    source_path: Path, target: str, entrypoints: list[str],
    *, index_dir: str = ".codegraph", max_paths: int = 3, max_depth: int = 12,
    reverse_graph: dict[str, list[str]] | None = None,
) -> TraceResult:
    """反向 BFS：从 target 出发沿"谁调用了我"方向找配置的入口锚点。

    Args:
        entrypoints: 项目配置的入口函数裸名列表（`aicoverage.toml` 的
            `[codegraph].entrypoints`，通常是 `main`；库类项目填驱动程序的
            入口，而非被测库本身的导出函数）。
        reverse_graph: 预加载的反向调用图（批量查询时传入，避免重复扫描
            sqlite；见 `trace_batch_to_entrypoints`）。

    Returns:
        TraceResult。查不到路径**不代表函数不存在**——`resolved=False` 才是
        "函数名在索引里完全找不到"；`resolved=True, found=False` 是"函数存在
        但没有任何调用链能到达配置的入口"，强烈提示疑似死代码/新增未接线的
        函数，调用方不应为其生成 E2E 用例。
    """
    target = target.strip()
    if not target:
        return TraceResult(target=target, found=False, resolved=False)

    entry_set = {e.strip() for e in entrypoints if e.strip()}
    bare_target = _bare_name(target)
    if bare_target in entry_set:
        return TraceResult(target=target, found=True,
                           paths=[CallPath(entry=target, path=[target])])

    rg = reverse_graph if reverse_graph is not None else _load_reverse_call_graph(source_path, index_dir)

    in_graph = bare_target in rg
    appears_as_caller = any(bare_target in callers_ for callers_ in rg.values())
    if not in_graph and not appears_as_caller:
        return TraceResult(target=target, found=False, resolved=False)

    queue: deque = deque([(target, [target])])
    visited: set[str] = {target, bare_target}
    found_paths: list[CallPath] = []
    depth = 0
    while queue and len(found_paths) < max_paths and depth <= max_depth:
        level_size = len(queue)
        for _ in range(level_size):
            node, path = queue.popleft()
            node_bare = _bare_name(node)
            for caller_name in rg.get(node_bare, []):
                caller_bare = _bare_name(caller_name)
                if caller_name in path or caller_bare in [_bare_name(p) for p in path]:
                    continue  # 防环
                new_path = [caller_name] + path
                if caller_bare in entry_set:
                    found_paths.append(CallPath(entry=caller_name, path=new_path))
                    if len(found_paths) >= max_paths:
                        break
                    continue
                if caller_bare not in visited:
                    visited.add(caller_bare)
                    queue.append((caller_name, new_path))
            if len(found_paths) >= max_paths:
                break
        depth += 1
    return TraceResult(target=target, found=bool(found_paths), resolved=True, paths=found_paths)


def trace_batch_to_entrypoints(
    source_path: Path, targets: list[str], entrypoints: list[str],
    *, index_dir: str = ".codegraph", max_paths: int = 1, max_depth: int = 12,
) -> dict[str, TraceResult]:
    """批量反向 BFS：只扫一次 sqlite，逐个 target 复用内存中的反向图。"""
    rg = _load_reverse_call_graph(source_path, index_dir)
    results: dict[str, TraceResult] = {}
    for target in targets:
        target = target.strip()
        if not target:
            continue
        results[target] = trace_to_entrypoints(
            source_path, target, entrypoints, index_dir=index_dir,
            max_paths=max_paths, max_depth=max_depth, reverse_graph=rg,
        )
    return results


# ── 调用链聚类分批 ───────────────────────────────────────────────

def group_by_file(changed: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """按文件分组：同一文件的全部变更函数放同一批（最简单、语义最直观）。"""
    by_file: dict[str, list[tuple[str, str]]] = {}
    for f, fn in changed:
        by_file.setdefault(f, []).append((f, fn))
    return list(by_file.values())


def group_by_size(changed: list[tuple[str, str]], batch_size: int = 5) -> list[list[tuple[str, str]]]:
    """固定数量切分（不考虑语义关联，简单兜底）。"""
    return [changed[i:i + batch_size] for i in range(0, len(changed), batch_size)]


def group_by_call_chain(
    source_path: Path, changed: list[tuple[str, str]], entrypoints: list[str],
    *, index_dir: str = ".codegraph",
) -> tuple[list[list[tuple[str, str]]], list[tuple[str, str]]]:
    """按调用链路聚类：反向查询每个函数到入口的路径，同一入口下"距入口第二近
    的节点"相同的函数聚成一批（同一条链路/同一个具体分发点进来的改动天然
    关联，gen-agent 一次读懂调用链就能给整批函数写好触发条件）。

    Returns:
        (batches, unreachable)
        unreachable: 查不到入口路径的函数（疑似死代码，人工复核，不自动生成用例）。
    """
    targets = [fn for _, fn in changed]
    trace_results = trace_batch_to_entrypoints(source_path, targets, entrypoints, index_dir=index_dir)

    route_groups: dict[str, list[tuple[str, str]]] = {}
    unreachable: list[tuple[str, str]] = []
    for f, fn in changed:
        result = trace_results.get(fn)
        if not result or not result.found or not result.paths:
            unreachable.append((f, fn))
            continue
        path0 = result.paths[0].path
        route_anchor = path0[1] if len(path0) > 1 else path0[0]
        route_key = f"{path0[0]}:{route_anchor}"
        route_groups.setdefault(route_key, []).append((f, fn))
    return list(route_groups.values()), unreachable


def split_batches(
    changed: list[tuple[str, str]], strategy: str = "file", *,
    batch_size: int = 5, source_path: Path | None = None,
    entrypoints: list[str] | None = None, index_dir: str = ".codegraph",
) -> tuple[list[list[tuple[str, str]]], list[tuple[str, str]]]:
    """统一入口：按策略切分变更函数批次。

    Args:
        strategy: file | chain | size
        entrypoints: strategy=chain 时必须提供（非空列表）

    Returns:
        (batches, unreachable)；unreachable 只在 strategy=chain 时可能非空。
    """
    if not changed:
        return [], []
    if strategy == "size":
        return group_by_size(changed, batch_size), []
    if strategy == "chain":
        if source_path is None or not entrypoints:
            raise ValueError("strategy='chain' 需要提供 source_path 与非空 entrypoints")
        return group_by_call_chain(source_path, changed, entrypoints, index_dir=index_dir)
    return group_by_file(changed), []
