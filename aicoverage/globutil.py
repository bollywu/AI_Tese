"""gitignore-style path glob matching (`**` semantics corrected).

Why not fnmatch: fnmatch's `*` crosses `/`, so `src/**/*.c` fails to match
`src/wrk.c` (missing a directory level) while `*.c` matches `a/b.c` -- both are
counter-intuitive. This module implements gitignore semantics:

- a `**` segment matches zero or more path segments (`src/**/*.c` hits both
  `src/a.c` and `src/x/y/a.c`)
- within a normal segment `*`/`?` do not cross `/`
"""
from __future__ import annotations

import fnmatch

_PART_CACHE: dict[tuple[str, str], bool] = {}


def _match_parts(r_parts: tuple[str, ...], p_parts: tuple[str, ...]) -> bool:
    if not p_parts:
        return not r_parts
    if p_parts[0] == "**":
        # '**' matches zero or more segments
        for skip in range(len(r_parts) + 1):
            if _match_parts(r_parts[skip:], p_parts[1:]):
                return True
        return False
    if not r_parts:
        return False
    key = (r_parts[0], p_parts[0])
    hit = _PART_CACHE.get(key)
    if hit is None:
        hit = fnmatch.fnmatchcase(r_parts[0], p_parts[0])
        _PART_CACHE[key] = hit
    return hit and _match_parts(r_parts[1:], p_parts[1:])


def match_one(rel_path: str, pattern: str) -> bool:
    return _match_parts(tuple(rel_path.split("/")), tuple(pattern.split("/")))


def glob_matches(rel_path: str, patterns: list[str]) -> bool:
    """Whether rel_path matches any pattern (False when patterns is empty)."""
    return any(match_one(rel_path, p) for p in patterns)
