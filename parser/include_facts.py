"""parser/include_facts.py - `#include` directives of a source blob (`m_blob_facts` family 3, pure).

`[[name, quoted, conds], ...]` of every `#include` / `#include_next` with a literal name, in file order
(`parser/include_graph.include_directives_with_conditions()`), for `.c`, `.h`, `.S` and `.s` blobs. `conds` is
the list of `#if` conditions (strings) enclosing the directive, empty when it is unconditional; a stored
entry without `conds` decodes with an empty list. The webapp's include walks
(`ArchLayoutService.reachable()`) and its list of `.c`/`.S` files included by other files
(`KconfigService._included_sources()`) read them instead of opening every file from Git.
"""
from __future__ import annotations

import json
import zlib
from typing import Any

from parser.include_graph import include_directives_with_conditions

FAMILY = 3
FORMAT = 2
SUFFIXES = (".c", ".h", ".S", ".s")


def applies(path: str) -> bool:
    return path.endswith(SUFFIXES)


def extract(text: str) -> list[list[Any]]:
    return [[name, quoted, conds] for name, quoted, conds in include_directives_with_conditions(text or "")]


def encode(record: list[list[Any]] | None) -> bytes:
    """Stored form: empty for a file without includes, else zlib-compressed JSON."""
    if not record:
        return b""
    return zlib.compress(json.dumps(record, separators=(",", ":")).encode(), 3)


def decode(data: bytes | bytearray | memoryview | None) -> list[tuple[str, bool, list[str]]] | None:
    """[(name, quoted, conds)] of a stored record (None when the bytes are not one)."""
    if data is None:
        return None
    raw = bytes(data)
    if not raw:
        return []
    try:
        return [(str(e[0]), bool(e[1]), [str(c) for c in (e[2] if len(e) > 2 else ())]) for e in json.loads(zlib.decompress(raw))]
    except (zlib.error, ValueError, TypeError, IndexError):
        return None


def included_sources(directives: list[tuple[Any, ...]]) -> set[str]:
    """Basenames of the `.c` / `.S` files a blob includes (compiled inside it, not on their own)."""
    out = set()
    for item in directives:
        name = item[0]
        if name.endswith((".c", ".S")):
            out.add(name.rsplit("/", 1)[-1])
    return out
