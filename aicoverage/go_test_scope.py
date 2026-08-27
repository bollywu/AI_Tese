"""Static classifier for Go test coverage source (E2E vs unit-test).

Requirement (2026-08-27): all coverage must be reached through E2E first; a function
that cannot be E2E-reached may only be covered by a unit test after human confirmation.
Go's `go test` runs all *_test.go together and coverprofile cannot attribute a function
to a specific test, so the source classification is done statically per test function:

  - E2E / integration test: the test function starts an HTTP server / router
    (httptest, http.ListenAndServe, gin.New(), echo, mux), performs real HTTP requests
    against a live server, or wires the full app + a real/external backend. It exercises
    the application through its public entry (HTTP handler).
  - Unit test: the test instantiates the object/service directly, injects an in-memory /
    mocked dependency (e.g. in-memory sqlite), and calls methods directly without a
    server or network path.

A test function is classified E2E if its body references any e2e signal symbol;
otherwise it is unit. This is a heuristic ("looks like") and is intended to guide the
human-confirmation gate and the report disclosure, not to be a precise dataflow proof.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Symbols whose presence in a test function body marks it as exercising the app's
# real E2E path (HTTP server / router / live network / full-stack wiring).
# Route-registration methods (GET/POST/DELETE...) are intentionally NOT included:
# `db.Delete(...)` / `m.Set(...)` in gorm/collections would false-positive on them.
E2E_SIGNAL_RE = re.compile(
    r"httptest\."
    r"|http\.(NewServer|ListenAndServe|Serve|Client|NewRequest|Get|Post|Handle|HandleFunc)\b"
    r"|net/http\b"
    r"|gin\.(New|Default|Engine|NewRouter)\b"
    r"|echo\.New\(\)"
    r"|chi\.NewRouter|gorilla/mux|mux\.NewRouter"
    r"|grpc\.Dial\b"
    r"|localhost|127\.0\.0\.1|0\.0\.0\.0"
    r"|NewRequest\(|PerformRequest|ServeHTTP\(",
    re.IGNORECASE,
)

# Signals that, even with HTTP-ish keywords, only build an in-memory/mocked fake
# (e.g. httptest.NewServer used as a fake peer) — still E2E-ish for our purpose; we
# keep it simple and treat any HTTP/net signal as E2E. Kept as a placeholder for
# future refinement.

_FUNC_DEF = re.compile(
    r"^func\s+(?P<recv>\([^)]*\)\s*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _find_body(lines: list[str], start: int) -> tuple[int, int] | None:
    """Return (start, end) body line range by brace matching from a func line."""
    n = len(lines)
    # find the opening brace line
    brace_line = start
    while brace_line < n and "{" not in lines[brace_line]:
        brace_line += 1
    if brace_line >= n:
        return None
    body_end = brace_line
    depth = lines[brace_line].count("{") - lines[brace_line].count("}")
    while depth > 0:
        body_end += 1
        if body_end >= n:
            return None
        depth += lines[body_end].count("{") - lines[body_end].count("}")
    return brace_line, body_end


@dataclass
class GoTestFunc:
    file: str        # source-root-relative .go path
    name: str        # test function name (TestXxx)
    start_line: int  # 1-based
    end_line: int    # 1-based
    source: str      # "e2e" | "unit"


def _classify_body(lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start:end + 1])
    return "e2e" if E2E_SIGNAL_RE.search(body) else "unit"


def classify_go_test_file(path: Path, source_root: Path) -> list[GoTestFunc]:
    """Classify every TestXxx function in one *_test.go file as e2e or unit."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    try:
        rel = path.relative_to(source_root).as_posix()
    except ValueError:
        rel = path.as_posix()
    funcs: list[GoTestFunc] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("func ") or stripped == "func":
            m = _FUNC_DEF.match(stripped)
            if not m or not (m.group("name") or "").startswith("Test"):
                i += 1
                continue
            span = _find_body(lines, i)
            if span is None:
                i += 1
                continue
            start_line, end_line = span
            funcs.append(GoTestFunc(
                file=rel, name=m.group("name"),
                start_line=start_line + 1, end_line=end_line + 1,
                source=_classify_body(lines, start_line, end_line),
            ))
            i = end_line + 1
            continue
        i += 1
    return funcs


def scan_go_test_sources(source_root: Path) -> dict[str, list[GoTestFunc]]:
    """Scan all *_test.go under source_root, returning {test_func_name: GoTestFunc}.

    Test-function names are globally unique enough in Go (package scope); the map key
    is `name` so the report/gate can look a covered function's test up by name.
    """
    result: dict[str, GoTestFunc] = {}
    for p in source_root.rglob("*_test.go"):
        if p.is_file():
            for tf in classify_go_test_file(p, source_root):
                result.setdefault(tf.name, tf)
    return result
