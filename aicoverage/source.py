"""C/C++ source static parsing: function-inventory extraction (for analyzer phase / reports).

Note: the authoritative function list for the coverage loop comes from gcov JSON
(gcov -i enumerates every function in each instrumented compilation unit,
including unexecuted ones, precise to line numbers). This module is only a light
static scan before the build, giving the analyzer-agent a panorama of "what
functions exist" during requirement parsing.

Implementation: brace-depth scan + line-start heuristic matching, no ctags
dependency (keeps zero external-tool dependency; uses ctags for better accuracy
when available).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Function-def heuristic: [modifier/return-type] name(args...) { start
# Exclude control-flow keywords; C++ :: (out-of-class definition) and operator
# overloads are passed through simply.
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "else", "do",
    "sizeof", "defined", "case", "default",
}

_FUNC_HEAD = re.compile(
    r"^(?P<sig>(?:[A-Za-z_][\w:]*(?:<[^;{}]*>)?\s+)+"  # return type (incl. templates)
    r"|(?:static|inline|extern|virtual|explicit)\s+)?"   # or a bare modifier start
    r"(?P<name>[A-Za-z_~][\w:<>~]*)\s*"                  # function name (incl. Class::method)
    r"\((?:[^;{}()]|\([^()]*\))*\)\s*"                    # parameter list (one nesting level)
    r"(?:const\s*)?(?:noexcept\s*)?(?:->\s*[\w:<>&*\s]+)?$"  # trailing return/noexcept
)

_LINE_COMMENT = re.compile(r"//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass
class FunctionInfo:
    file: str          # path relative to source root
    name: str          # function name (C++ includes class qualification)
    line: int          # definition start line
    signature: str     # single-line signature summary

    def to_dict(self) -> dict:
        return {"file": self.file, "name": self.name, "line": self.line,
                "signature": self.signature}


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT.sub(" ", text)
    text = "\n".join(_LINE_COMMENT.sub("", ln) for ln in text.splitlines())
    return text


def extract_functions_source(path: Path, source_root: Path) -> list[FunctionInfo]:
    """Function extraction for a single .c/.cpp file (brace-depth heuristic)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = _strip_comments(text)
    rel = path.relative_to(source_root).as_posix()
    results: list[FunctionInfo] = []
    depth = 0
    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if depth == 0 and stripped and not stripped.startswith("#"):
            m = _FUNC_HEAD.match(stripped)
            if m:
                name = m.group("name")
                # The function body must contain "{" within the next few lines
                # (avoids treating declarations/calls as definitions)
                lookahead = "\n".join(lines[i - 1: i + 4])
                if "{" in lookahead and name not in _KEYWORDS and len(name) > 1:
                    results.append(FunctionInfo(
                        file=rel, name=name, line=i,
                        signature=stripped[:120],
                    ))
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
    return results


def extract_functions_ctags(source_root: Path, files: list[Path]) -> list[FunctionInfo] | None:
    """Prefer ctags when available (more accurate); return None to fall back to regex."""
    ctags = shutil.which("ctags")
    if not ctags:
        return None
    try:
        proc = subprocess.run(
            [ctags, "--output-format=json", "-f", "-",
             "--kinds-c=f", "--kinds-c++=f", "--fields=+n",
             *[str(p) for p in files]],
            capture_output=True, text=True, timeout=120, cwd=source_root,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    results: list[FunctionInfo] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            import json
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = entry.get("name", "")
        path = entry.get("path", "")
        line_no = entry.get("line", 0)
        if not name or not path or not line_no:
            continue
        try:
            rel = Path(path).resolve().relative_to(source_root).as_posix()
        except ValueError:
            continue
        signature = (entry.get("signature") or name)[:120]
        results.append(FunctionInfo(file=rel, name=name, line=int(line_no),
                                    signature=signature))
    return results or None


def function_inventory(files: list[Path], source_root: Path) -> list[FunctionInfo]:
    """Full function inventory: ctags first, regex as fallback."""
    if not files:
        return []
    via_ctags = extract_functions_ctags(source_root, files)
    if via_ctags is not None:
        return via_ctags
    results: list[FunctionInfo] = []
    for p in files:
        results.extend(extract_functions_source(p, source_root))
    return results
