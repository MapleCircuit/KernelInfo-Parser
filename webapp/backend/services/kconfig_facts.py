"""webapp/backend/services/kconfig_facts.py - Kconfig definitions of a blob, parsed once per blob SHA.

Kconfig history (history_service) and Compare's Kconfig diff (diff_service) both need the definitions of
the same Kconfig blobs. `definitions()` parses a blob once for all its symbols
(parser/kconfig_ast/kconfig_definitions.py, with the raw prompt / first default fields) and keeps the
result in one LRU keyed by the Git blob SHA: content addressed, so valid for every revision and repository.
On a miss the parser's stored definitions are read first (`m_blob_facts` family 4, written by Step 6.9 and
`--backfill-facts`, the same parse with help texts), one query per 500 blobs (`load_stored()`, also run by
`read_blobs()` before Git); only blobs the database lacks are read from Git and parsed. Parsed results go
through JSON like the stored ones, so both compare equal.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections import OrderedDict
from typing import Any, Callable, Iterable

from parser.kconfig_ast.kconfig_definitions import kconfig_definitions
from webapp.backend.config import get_repo_dir

logger = logging.getLogger(__name__)

CACHE_SIZE = 4096
# blob SHA -> {symbol: [facts, ...]} (facts with RAW_FIELDS)
_DEFS_CACHE: OrderedDict[str, dict[str, list[dict[str, Any]]]] = OrderedDict()
_LOCK = threading.Lock()
# Number of parses (tests check a blob is parsed once for all its symbols) and of blobs read from the database
STATS = {"parsed": 0, "stored": 0}


def cached(sha: str) -> dict[str, list[dict[str, Any]]] | None:
    with _LOCK:
        hit = _DEFS_CACHE.get(sha)
        if hit is not None:
            _DEFS_CACHE.move_to_end(sha)
        return hit


def store(sha: str, defs: dict[str, list[dict[str, Any]]]) -> None:
    with _LOCK:
        _DEFS_CACHE[sha] = defs
        _DEFS_CACHE.move_to_end(sha)
        while len(_DEFS_CACHE) > CACHE_SIZE:
            _DEFS_CACHE.popitem(last=False)


def load_stored(shas: Iterable[str]) -> set[str]:
    """Fill the cache with the parser's stored definitions (m_blob_facts family 4) of the blobs not cached;
    returns the SHAs found."""
    from parser.kconfig_ast import kconfig_definitions as kd
    from webapp.backend.services.source_index import db_facts
    want = sorted({s for s in shas if s and len(s) == 40 and cached(s) is None})
    if not want:
        return set()
    found = db_facts(want, kd.FAMILY, kd.FORMAT, kd.decode)
    for sha, defs in found.items():
        store(sha, defs)
    STATS["stored"] += len(found)
    return set(found)


def definitions(sha: str, load_text: Callable[[], str | None]) -> dict[str, list[dict[str, Any]]]:
    """{symbol: [facts, ...]} of the Kconfig blob `sha`: cache, then the parser's stored definitions, then
    load_text() parsed.

    The returned structure is shared: callers must not modify it."""
    hit = cached(sha)
    if hit is not None:
        return hit
    if load_stored([sha]):
        hit = cached(sha)
        if hit is not None:
            return hit
    text = load_text() or ""
    defs = json.loads(json.dumps(kconfig_definitions(text, raw_fields=True)))
    STATS["parsed"] += 1
    store(sha, defs)
    return defs


def read_blobs(shas: Iterable[str], skip_cached: bool = True) -> dict[str, str]:
    """Text of Git blobs (one `git cat-file --batch` process); blobs already parsed, or whose definitions the
    database holds (loaded into the cache here), are skipped."""
    shas = [s for s in shas if s]
    if skip_cached:
        load_stored(shas)
    missing = sorted({s for s in shas if not (skip_cached and cached(s) is not None)})
    if not missing:
        return {}
    try:
        out = subprocess.run(
            ["git", "-C", str(get_repo_dir()), "cat-file", "--batch"],
            input="".join(f"{sha}\n" for sha in missing).encode(),
            capture_output=True,
            timeout=60,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("git cat-file for Kconfig blobs failed: %s", e)
        return {}
    texts: dict[str, str] = {}
    pos = 0
    for sha in missing:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl].split()
        if len(header) < 3 or header[1] != b"blob":
            pos = nl + 1
            continue
        size = int(header[2])
        texts[sha] = out[nl + 1:nl + 1 + size].decode("utf-8", errors="replace")
        pos = nl + 1 + size + 1
    return texts


def clear() -> None:
    with _LOCK:
        _DEFS_CACHE.clear()


def reverse_dependencies(symbol: str, shas: Iterable[str] | None = None) -> tuple[list[str], list[str]]:
    """Return ([selected_by], [implied_by]) for `symbol` across cached or provided Kconfig blobs."""
    clean = symbol.strip()
    if clean.startswith("CONFIG_"):
        clean = clean[7:]
    selected_by: set[str] = set()
    implied_by: set[str] = set()
    with _LOCK:
        blob_defs = list(_DEFS_CACHE.values())
    for d in blob_defs:
        for sym_name, facts_list in d.items():
            for f in facts_list:
                for sel in f.get("selects", []):
                    target = sel.split()[0] if isinstance(sel, str) else ""
                    if target == clean:
                        selected_by.add(sym_name)
                for imp in f.get("implies", []):
                    target = imp.split()[0] if isinstance(imp, str) else ""
                    if target == clean:
                        implied_by.add(sym_name)
    return sorted(selected_by), sorted(implied_by)
