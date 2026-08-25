"""CodeGraph CLI wrapper: reverse-BFS call-chain + line-range function attribution + call-chain cluster batching.

The call-chain analysis capability is validated on real project data, with these
generalization changes:
1. Entry anchors (entrypoints) are a project-configurable list (`[codegraph].entrypoints`
   in aicoverage.toml), not hard-coded specific entry functions.
2. It assumes no specific project's function-pointer indirect-call pattern -- pure CodeGraph
   AST call edges are already the precise source of "direct calls". If the target project has
   indirect dispatch patterns such as function-pointer tables / command registries, a bridge
   rule extension point can be added later in aicoverage.toml (YAGNI; not pre-designed now).

Real codegraph CLI behavior verified on a minimal C project (2026-08-24):
- sqlite `nodes` table fields: id/kind/name/qualified_name/file_path/start_line/
  end_line/signature etc., consistent with this module's assumptions.
- `codegraph callers <symbol> --json` returns `{"callers": [{"name","kind",
  "filePath","startLine"}, ...]}`.
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
    """CodeGraph CLI unavailable (not installed / index not built)."""


# ── CLI subprocess wrapper ───────────────────────────────────────

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
    """Whether a CodeGraph index already exists under source_path."""
    return _db_path(source_path, index_dir).exists()


def ensure_index(source_path: Path, *, force: bool = False, timeout: int = 600) -> dict:
    """Ensure an index exists: `codegraph init` if missing, else `codegraph sync` (incremental update).

    Not auto-invoked inside the library -- triggered explicitly by the caller (CLI layer) to
    avoid making the user wait through an unknown-duration full indexing without knowing it
    (aligned with design doc Q2: default error-hint to run manually; use this function for
    explicit auto-indexing scenarios).
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
    """Index fingerprint (mtime+size, detects whether the index lags the source; no full hash)."""
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
    """Query who calls `symbol` (AST reference resolution, not text regex matching)."""
    data = _run_json(["callers", symbol, "--limit", str(limit)], cwd=source_path)
    return [
        CGSymbolRef(name=c.get("name", ""), kind=c.get("kind", ""),
                   file_path=c.get("filePath", ""), start_line=c.get("startLine", 0))
        for c in data.get("callers", []) or []
    ]


def callees(source_path: Path, symbol: str, limit: int = 50) -> list[CGSymbolRef]:
    """Query whom `symbol` calls."""
    data = _run_json(["callees", symbol, "--limit", str(limit)], cwd=source_path)
    return [
        CGSymbolRef(name=c.get("name", ""), kind=c.get("kind", ""),
                   file_path=c.get("filePath", ""), start_line=c.get("startLine", 0))
        for c in data.get("callees", []) or []
    ]


# ── Line-range reverse lookup (the core of diff attribution, M1 key capability) ──

@dataclass
class CGFunctionRange:
    """A function's line range + qualified name (the deterministic reverse-lookup of diff line -> function)."""

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
    """Reverse lookup: for several changed lines of a file, return the functions (qualified)
    whose line ranges cover them.

    Args:
        innermost_only: True (default) keeps only the smallest (innermost) function for each
            changed line. ⚠️ Required -- CodeGraph's scope inference for some languages/styles
            yields nested ranges (e.g. an outer anonymous wrapper plus the inner real host
            function both cover the same line); this trap was verified in real-project testing
            (case: outer [342-1574] and inner real host [1230-1366] both hit line 1235; returning
            both would split one change into two targets). The innermost (smallest span) is the
            function the change truly belongs to.

    Returns:
        The list of hit functions (sorted by start_line, deduped). An empty list means these
        changed lines fall in no indexed function body (globals/macros/comments/class-decl
        regions); the caller should treat that as unresolved and **must not fall back to guessing**.
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
    """Enumerate all functions/methods in a file (for `func_name="*"` file-level expansion)."""
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


# ── Reverse BFS: trace the call chain back to entry anchors ───────

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
    """Load all reverse call edges from the CodeGraph sqlite into memory in one pass (one sqlite
    scan, avoiding per-function codegraph subprocess -- real-project test: 244 functions went
    from ~20min to <5s)."""
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
    """Reverse BFS: from target, walk the "who calls me" direction to find the configured entry anchors.

    Args:
        entrypoints: project-configured entry-function bare-name list (`[codegraph].entrypoints`
            in aicoverage.toml; usually `main`; library projects fill the driver's entry, not
            the lib's own exported functions).
        reverse_graph: pre-loaded reverse call graph (pass when batch querying to avoid
            re-scanning sqlite; see `trace_batch_to_entrypoints`).

    Returns:
        TraceResult. Not finding a path does **not** mean the function doesn't exist --
        `resolved=False` means "the function name isn't in the index at all";
        `resolved=True, found=False` means "the function exists but no call chain reaches the
        configured entry", strongly suggesting dead code / a newly-added un-wired function;
        the caller should not generate E2E cases for it.
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
                    continue  # cycle guard
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
    """Batch reverse BFS: scan sqlite once, reuse the in-memory reverse graph per target."""
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


# ── Call-chain cluster batching ──────────────────────────────────

def group_by_file(changed: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Group by file: all changed functions of the same file go in one batch (simplest, most intuitive)."""
    by_file: dict[str, list[tuple[str, str]]] = {}
    for f, fn in changed:
        by_file.setdefault(f, []).append((f, fn))
    return list(by_file.values())


def group_by_size(changed: list[tuple[str, str]], batch_size: int = 5) -> list[list[tuple[str, str]]]:
    """Split by fixed count (ignores semantic association; simple fallback)."""
    return [changed[i:i + batch_size] for i in range(0, len(changed), batch_size)]


def group_by_call_chain(
    source_path: Path, changed: list[tuple[str, str]], entrypoints: list[str],
    *, index_dir: str = ".codegraph",
) -> tuple[list[list[tuple[str, str]]], list[tuple[str, str]]]:
    """Cluster by call chain: reverse-query each function's path to the entry; functions whose
    "second-closest-to-entry node" under the same entry are the same cluster into one batch
    (changes entering via the same chain / same concrete dispatch point are naturally related,
    so gen-agent can read one call chain and write trigger conditions for the whole batch).

    Returns:
        (batches, unreachable)
        unreachable: functions with no entry path found (likely dead code; manual review, no
        auto-generated cases).
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
    """Unified entry: split changed functions into batches by strategy.

    Args:
        strategy: file | chain | size
        entrypoints: required for strategy=chain (non-empty list)

    Returns:
        (batches, unreachable); unreachable is only possibly non-empty for strategy=chain.
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
