"""gitignore 风格的路径 glob 匹配（`**` 语义修正）。

为什么不用 fnmatch：fnmatch 的 `*` 会跨 `/` 匹配，导致
`src/**/*.c` 匹配不到 `src/wrk.c`（缺一层目录）、`*.c` 却能匹配
`a/b.c`——两者都与直觉相反。本模块实现 gitignore 语义：

- `**` 段匹配零个或多个路径段（`src/**/*.c` 同时命中 `src/a.c` 与 `src/x/y/a.c`）
- 普通段内 `*`/`?` 不跨 `/`
"""
from __future__ import annotations

import fnmatch

_PART_CACHE: dict[tuple[str, str], bool] = {}


def _match_parts(r_parts: tuple[str, ...], p_parts: tuple[str, ...]) -> bool:
    if not p_parts:
        return not r_parts
    if p_parts[0] == "**":
        # '**' 匹配零个或多个段
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
    """rel_path 是否命中任一 pattern（patterns 为空时返回 False）。"""
    return any(match_one(rel_path, p) for p in patterns)
