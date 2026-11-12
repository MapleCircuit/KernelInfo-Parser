"""parser/facts_ingest.py - Step 6.9: the per-blob facts of a version's files (`m_blob_facts`).

Families of facts are stored per Git blob SHA, one row each (`(blob_hash, family)`), each with its own format:
- family 1 (`source_facts.FAMILY`): the functions, calls and ops tables of a `.c` / `.h` blob (`parser/source_facts.py`);
  the webapp's source index assembles a revision from them (call paths, ops implementations; `webapp/API.md`
  section 2.19);
- family 2 (`kapi_facts.FAMILY`): the Kernel API macros of a `.c` / `.h` / `.S` blob (`parser/kapi_facts.py`); the
  webapp assembles the Kernel API catalogs of a revision from them (`webapp/API.md` section 4.9). Most blobs have
  none: their row holds empty data;
- family 3 (`include_facts.FAMILY`): the `#include` directives of a `.c` / `.h` / `.S` blob (`parser/include_facts.py`):
  the webapp's include walks and its list of included `.c` / `.S` files;
- family 4 (`kconfig_definitions.FAMILY`): the definitions of a Kconfig blob (`parser/kconfig_ast/kconfig_definitions.py`):
  the webapp's Kconfig history and Compare;
- family 5 (`doc_labels.FAMILY`): the `.. _label:` targets of a `Documentation/**/*.rst` blob (`parser/doc_labels.py`):
  the webapp's documentation label index;
- family 6 (`man_facts.FAMILY`): the NAME, `.TH` header, `.so` alias, cross references and SYNOPSIS declarations of
  a man page blob (`man/manS/name.S` or `manS/name.S`, `parser/man_ast/man_facts.py`): the webapp's man page index.

All of a blob's missing families are read from one text in one pass; the comment/string mask (families 1 and 2) is
computed once, and only when one of them is requested. A version only extracts the blobs (and families) no earlier
version stored (a release changes ~10% of its files; the first version extracts its whole tree, spread over
`parser.facts_workers` low-priority forked processes). Rows of an older format are replaced. `--backfill-facts`
(core/backfills.py) fills them for versions ingested before a family existed.
"""
from __future__ import annotations

import contextlib
import logging
import multiprocessing
import re
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any

from parser import doc_labels as dl
from parser import include_facts as inc
from parser import kapi_facts as kf
from parser import source_facts as sf
from parser.kconfig_ast import kconfig_definitions as kd
from parser.man_ast import man_facts as mf
from parser.source_facts import init_worker

logger = logging.getLogger(__name__)


# family -> current format
FAMILIES: dict[int, int] = {sf.FAMILY: sf.FACTS_FORMAT, kf.FAMILY: kf.KAPI_FORMAT, inc.FAMILY: inc.FORMAT,
                            kd.FAMILY: kd.FORMAT, dl.FAMILY: dl.FORMAT, mf.FAMILY: mf.FORMAT}
_MASKED = {sf.FAMILY, kf.FAMILY}
SUFFIXES = (".c", ".h", ".S", ".s", ".rs")
MAX_TEXT = 4 << 20   # larger files are stored without facts (generated tables, firmware)
_CHUNK = 48
_IN = 1000
_INSERT = 1000

# -----------------------------------------------------------------------------
# Multi-language fact extractors (Rust, ASM, C)
# -----------------------------------------------------------------------------

_ASM_START_MACRO = re.compile(
    r"^[ \t]*(?:SYM_FUNC_START\w*|SYM_CODE_START\w*|SYM_TYPED_FUNC_START|ENTRY|GLOBAL|KENTRY)\s*\(\s*([A-Za-z0-9_]+)\s*\)"
)
_ASM_END_MACRO = re.compile(
    r"^[ \t]*(?:SYM_FUNC_END\w*|SYM_CODE_END\w*|SYM_DATA_END\w*|ENDPROC|END)\s*\(\s*([A-Za-z0-9_]+)\s*\)"
)
_ASM_LABEL = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*):")
_ASM_CALL = re.compile(r"^[ \t]*(?:callq?|bl|b\.w|jal[rx]?|blx)\s+([A-Za-z_][A-Za-z0-9_]*)")

_RUST_NO_MANGLE = re.compile(
    r"#\[\s*(?:unsafe\s*\(\s*)?(?:no_mangle|export)\s*\)?\s*\]\s*(?:(?:pub(?:\([^)]*\))?|extern\s+\"[^\"]+\"|unsafe)\s+)*fn\s+([A-Za-z0-9_]+)",
    re.MULTILINE,
)
_RUST_EXPORT_MACRO = re.compile(
    r"\b(?:EXPORT_SYMBOL_RUST_GPL|export_symbol_rust_gpl)\s*!\s*\(\s*([A-Za-z0-9_]+)\s*\)"
)
_RUST_MOD = re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?mod[ \t]+([A-Za-z0-9_]+)[ \t]*;", re.MULTILINE)
_RUST_EXTERN_CRATE = re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?extern[ \t]+crate[ \t]+([A-Za-z0-9_]+)[ \t]*;", re.MULTILINE)
_ASM_INCLUDE = re.compile(r'^[ \t]*\.include[ \t]+([<"])([^>"\n]+)[>"]', re.MULTILINE)


def extract_asm_calls(text: str) -> dict[str, list[Any]]:
    """Extract function definitions and call references from assembly source text."""
    if not text:
        return {"d": [], "c": [], "m": [], "t": [], "a": []}
    d_items: list[list[Any]] = []
    c_items: list[list[Any]] = []
    cur_func_idx: int | None = None
    lines = text.replace("\r\n", "\n").split("\n")
    for idx, raw_line in enumerate(lines):
        ln = idx + 1
        line = raw_line.split("/*", 1)[0].split("//", 1)[0]
        if "#" in line:
            stripped = line.strip()
            if not (stripped.startswith("#include") or stripped.startswith("#define") or stripped.startswith("#if")):
                line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue

        m_start = _ASM_START_MACRO.match(line)
        if m_start:
            sym_name = m_start.group(1)
            cur_func_idx = len(d_items)
            d_items.append([sym_name, ln, ln, 0])
            continue

        m_end = _ASM_END_MACRO.match(line)
        if m_end:
            if cur_func_idx is not None:
                d_items[cur_func_idx][2] = ln
                cur_func_idx = None
            continue

        m_lbl = _ASM_LABEL.match(line)
        if m_lbl:
            sym_name = m_lbl.group(1)
            if not sym_name.isdigit() and not sym_name.startswith((".L", "L")):
                if cur_func_idx is None:
                    cur_func_idx = len(d_items)
                    d_items.append([sym_name, ln, ln, 0])
            continue

        m_call = _ASM_CALL.match(line)
        if m_call and cur_func_idx is not None:
            target = m_call.group(1)
            c_items.append([cur_func_idx, target, ln])

    return {"d": d_items, "c": c_items, "m": [], "t": [], "a": []}


def extract_rust_exports(text: str) -> dict[str, Any]:
    """Extract #[export], #[no_mangle], and EXPORT_SYMBOL_RUST_GPL exports from Rust source text."""
    if not text:
        return {}
    x: list[list[Any]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    for idx, raw_line in enumerate(lines):
        ln = idx + 1
        m_macro = _RUST_EXPORT_MACRO.search(raw_line)
        if m_macro:
            sym = m_macro.group(1)
            x.append([sym, "gpl", "", ln])

    for m in _RUST_NO_MANGLE.finditer(text):
        sym = m.group(1)
        ln = text.count("\n", 0, m.start()) + 1
        x.append([sym, "plain", "", ln])

    return {"x": sorted(x, key=lambda it: it[3])} if x else {}


def extract_rust_includes(text: str) -> list[list[Any]]:
    """Extract mod and extern crate declarations from Rust source text."""
    if not text:
        return []
    out: list[list[Any]] = []
    for m in _RUST_MOD.finditer(text):
        out.append([m.group(1), False])
    for m in _RUST_EXTERN_CRATE.finditer(text):
        out.append([m.group(1), False])
    return out


def extract_asm_includes(text: str) -> list[list[Any]]:
    """Extract .include and #include directives from assembly source text."""
    directives = inc.extract(text)
    for m in _ASM_INCLUDE.finditer(text or ""):
        directives.append([m.group(2).strip(), m.group(1) == '"'])
    return directives


def _is_rust(path: str, text: str) -> bool:
    if path:
        return path.endswith(".rs")
    return "fn " in text or "impl " in text or "pub " in text


def _is_asm(path: str, text: str) -> bool:
    if path:
        return path.endswith((".S", ".s"))
    return ".globl" in text or ".section" in text or "ENTRY(" in text or "SYM_FUNC_" in text


def families_of(path: str) -> tuple[int, ...]:
    """The fact families of a file: calls, Kernel API and includes for C and Rust sources and headers;
    Kernel API and includes for assembly; definitions for Kconfig files; labels for Documentation `.rst` files;
    man facts for man pages."""
    if path.endswith((".c", ".h")):
        return (sf.FAMILY, kf.FAMILY, inc.FAMILY)
    if path.endswith(".rs"):
        return (sf.FAMILY, kf.FAMILY, inc.FAMILY)
    if path.endswith((".S", ".s")):
        return (kf.FAMILY, inc.FAMILY)
    if kd.applies(path):
        return (kd.FAMILY,)
    if dl.applies(path):
        return (dl.FAMILY,)
    if mf.applies(path):
        return (mf.FAMILY,)
    return ()


def fact_blobs(file_blob_hashes: dict[str, bytes]) -> dict[bytes, str]:
    """blob SHA -> one path holding it, for the files of a tree listing (path -> SHA) with at least one family."""
    out: dict[bytes, str] = {}
    for path, sha in file_blob_hashes.items():
        if sha not in out and families_of(path):
            out[sha] = path
    return out


def missing(db: Any, shas: Iterable[bytes], family: int) -> list[bytes]:
    """The SHAs without a row of the family in its current format."""
    todo = sorted(set(shas))
    have: set[bytes] = set()
    for i in range(0, len(todo), _IN):
        part = todo[i:i + _IN]
        placeholders = ",".join(["%s"] * len(part))
        rows = db.query(
            "SELECT blob_hash FROM m_blob_facts WHERE family = %s AND format = %s "  # noqa: S608
            f"AND blob_hash IN ({placeholders})",
            (family, FAMILIES[family], *part),
            dictionary=False,
        )
        have.update(bytes(r[0]) for r in rows)
    return [s for s in todo if s not in have]


def _encoded(family: int, text: str, masked: str | None, path: str = "") -> bytes:
    """One family's stored data; a file that defeats a scanner is stored empty for that family."""
    try:
        if family == sf.FAMILY:
            if _is_rust(path, text):
                from parser.rust_ast import rust_facts
                return rust_facts.encode(rust_facts.extract(text, masked))
            if _is_asm(path, text):
                return sf.encode(extract_asm_calls(text))
            return sf.encode(sf.extract(text, masked))
        if family == kf.FAMILY:
            if _is_rust(path, text):
                return kf.encode(extract_rust_exports(text))
            return kf.encode(kf.extract(text, masked))
        if family == inc.FAMILY:
            if _is_rust(path, text):
                return inc.encode(extract_rust_includes(text))
            if _is_asm(path, text):
                return inc.encode(extract_asm_includes(text))
            return inc.encode(inc.extract(text))
        if family == kd.FAMILY:
            return kd.encode(kd.extract(text))
        if family == dl.FAMILY:
            return dl.encode(dl.extract(text))
        if family == mf.FAMILY:
            return mf.encode(mf.extract(text))
    except (RecursionError, ValueError, IndexError):
        pass
    module = {sf.FAMILY: sf, kf.FAMILY: kf, inc.FAMILY: inc, kd.FAMILY: kd, dl.FAMILY: dl, mf.FAMILY: mf}[family]
    return module.encode(None)


def extract_families(items: list[tuple[Any, ...]]) -> list[tuple[str, dict[int, bytes]]]:
    """[(hex SHA, text, families[, path])] -> [(hex SHA, {family: encoded data})]."""
    out: list[tuple[str, dict[int, bytes]]] = []
    for item in items:
        if len(item) >= 4:
            sha, text, families, path = item[0], item[1], item[2], item[3]
        else:
            sha, text, families = item[0], item[1], item[2]
            path = ""
        masked: str | None = None
        if _MASKED.intersection(families):
            try:
                if _is_rust(path, text):
                    from parser.rust_ast.rust_text import mask_code
                    masked = mask_code(text)
                else:
                    masked = sf.mask(text)
            except (RecursionError, ValueError):
                masked = None
        out.append((sha, {family: _encoded(family, text, masked, path) for family in families}))
    return out


def extract(items: Iterable[tuple[Any, ...]], workers: int = 1,
            total: int = 0) -> Iterator[tuple[str, dict[int, bytes]]]:
    """(hex SHA, {family: data}) of (hex SHA, text, families[, path]) items, in `workers` low-priority processes when worth it.

    The processes are forked, like the parser's other workers: a spawned one would import `main.py` (the parser's
    `__main__`) again before running a pure function. They only receive texts and return encoded facts."""
    chunk: list[tuple[Any, ...]] = []
    if workers <= 1 or total < 200:
        for item in items:
            chunk.append(item)
            if len(chunk) >= _CHUNK:
                yield from extract_families(chunk)
                chunk = []
        yield from extract_families(chunk)
        return
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("fork"),
                             initializer=init_worker) as pool:
        inflight: list[Future] = []
        for item in items:
            chunk.append(item)
            if len(chunk) >= _CHUNK:
                inflight.append(pool.submit(extract_families, chunk))
                chunk = []
            while len(inflight) > 2 * workers:
                yield from inflight.pop(0).result()
        if chunk:
            inflight.append(pool.submit(extract_families, chunk))
        for fut in inflight:
            yield from fut.result()


def store(db: Any, rows: list[tuple[bytes, int, bytes]]) -> None:
    """Write (SHA, family, encoded data) rows (replacing rows of another format)."""
    for i in range(0, len(rows), _INSERT):
        db.executemany(
            "INSERT INTO m_blob_facts (blob_hash, family, format, data) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE format = VALUES(format), data = VALUES(data)",
            [(sha, family, FAMILIES[family], data) for sha, family, data in rows[i:i + _INSERT]],
        )


def ingest(db: Any, blobs: dict[bytes, str], read_text: Callable[[str], str | None], workers: int = 1,
           families: Iterable[int] | None = None, read_sha: Callable[[bytes], str | None] | None = None,
           progress: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Extract and store the facts the database lacks of the blobs (SHA -> a path to read it from), each blob read
    once for all its missing families (`families`: only those).

    `read_text(path)` returns the file's text (None: unreadable; stored as a blob without facts); `read_sha(sha)`,
    when given, reads it by blob SHA instead (the backfill: no checkout). `progress(n)` is called per extracted blob."""
    started = time.perf_counter()
    if db is None or not blobs:
        return {"blobs": len(blobs), "extracted": 0, "rows": 0, "seconds": 0.0}
    wanted = [f for f in FAMILIES if families is None or f in set(families)]
    need: dict[bytes, list[int]] = {}
    for family in wanted:
        for sha in missing(db, (s for s, p in blobs.items() if family in families_of(p)), family):
            need.setdefault(sha, []).append(family)
    if not need:
        return {"blobs": len(blobs), "extracted": 0, "rows": 0, "seconds": round(time.perf_counter() - started, 1)}
    todo = sorted(need)

    def items() -> Iterator[tuple[str, str, tuple[int, ...], str]]:
        for sha in todo:
            path = blobs[sha]
            text = read_sha(sha) if read_sha is not None else read_text(path)
            yield sha.hex(), (text if text is not None and len(text) <= MAX_TEXT else ""), tuple(need[sha]), path

    pending: list[tuple[bytes, int, bytes]] = []
    done = rows = 0
    try:
        for sha_hex, data in extract(items(), workers, len(todo)):
            sha = bytes.fromhex(sha_hex)
            pending.extend((sha, family, payload) for family, payload in data.items())
            done += 1
            rows += len(data)
            if progress is not None:
                progress(1)
            if len(pending) >= 2000:
                store(db, pending)
                db.commit()
                pending.clear()
        store(db, pending)
        db.commit()
    except Exception:
        if hasattr(db, "rollback"):
            db.rollback()
        raise
    return {"blobs": len(blobs), "extracted": done, "rows": rows, "seconds": round(time.perf_counter() - started, 1)}
