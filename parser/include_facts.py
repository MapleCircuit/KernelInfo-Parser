"""parser/include_facts.py - `#include` directives of a source blob (`m_blob_facts` family 3, pure).

`[[name, quoted], ...]` of every `#include` / `#include_next` with a literal name, in file order
(`parser/include_graph.include_directives()`), for `.c`, `.h` and `.S` blobs. The webapp's include walks
(`ArchLayoutService.reachable()`) and its list of `.c`/`.S` files included by other files
(`KconfigService._included_sources()`) read them instead of opening every file from Git.
"""
from __future__ import annotations

import json
import zlib
from typing import Any

from parser.include_graph import include_directives

FAMILY = 3
FORMAT = 1
SUFFIXES = (".c", ".h", ".S")


def applies(path: str) -> bool:
    return path.endswith(SUFFIXES)


def extract(text: str) -> list[list[Any]]:
    return [[name, quoted] for name, quoted in include_directives(text or "")]


def encode(record: list[list[Any]] | None) -> bytes:
    """Stored form: empty for a file without includes, else zlib-compressed JSON."""
    if not record:
        return b""
    return zlib.compress(json.dumps(record, separators=(",", ":")).encode(), 3)


def decode(data: bytes | bytearray | memoryview | None) -> list[tuple[str, bool]] | None:
    """[(name, quoted)] of a stored record (None when the bytes are not one)."""
    if data is None:
        return None
    raw = bytes(data)
    if not raw:
        return []
    try:
        return [(str(n), bool(q)) for n, q in json.loads(zlib.decompress(raw))]
    except (zlib.error, ValueError, TypeError):
        return None


def included_sources(directives: list[tuple[str, bool]]) -> set[str]:
    """Basenames of the `.c` / `.S` files a blob includes (compiled inside it, not on their own)."""
    out = set()
    for name, _quoted in directives:
        if name.endswith((".c", ".S")):
            out.add(name.rsplit("/", 1)[-1])
    return out
