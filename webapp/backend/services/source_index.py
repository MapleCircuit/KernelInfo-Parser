"""webapp/backend/services/source_index.py - Source index of a revision: its functions, who calls whom, and the ops
tables, read from the revision's Git blobs (webapp/API.md section 2.19).

The call path finder (N2, `callpath_service.py`) and the ops implementations (N1, `ops_service.py`) answer from it in
memory. It is content-addressed, so it covers every revision, git-only ones included:

- **Facts** - `parser/source_facts.py` `extract()` of every `.c` / `.h` blob, stored once per blob SHA. The parser
  writes them for every blob of the versions it ingests (`m_blob_facts`, Step 6.9), so a revision of an ingested
  version is assembled without reading a single blob. Blobs the database lacks (revisions the parser never ingested)
  are extracted here, in a background job (section 2.15) spread over a few low-priority worker processes
  (`webapp.source_index.workers`, default 2), into a SQLite file under the disk cache (section 2.14), capped at
  `webapp.source_index.max_mb` (default 1024, least recently used blobs dropped).
- **Revision** - `git ls-tree` of the commit, then the facts of its blobs assembled into a call graph (compressed sparse
  rows, both directions, each edge with its call site) and the ops tables. The assembled revision is written to the
  disk cache (`source-index-rev`, ~25 MB for v3.0, at most `webapp.source_index.revisions`, default 8) and read back
  in about a second, so a restart does not assemble it again; the two most recently used stay in memory.

Nodes are functions and macros, by name. A static function or a macro defined in a `.c` file is a node of its own per
file, and the calls made in that file bind to it; every other definition of a name (global functions, header inlines and
macros, the per-architecture variants of a function) shares one node, so a node can have several definitions. Callees
with no definition (assembly, generated code) are nodes without definitions. Calls through struct members
(`->field(`) are kept apart: with the ops tables they give the optional indirect edges (a call of `->read(` reaches the
functions assigned to `.read`).
"""
from __future__ import annotations

import bisect
import json
import logging
import subprocess
import sys
import threading
import time
import zlib
from array import array
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any, Callable, Iterable, Iterator

from parser.source_facts import (
    EMPTY,
    FACTS_FORMAT,
    FAMILY,
    F_MACRO_FN,
    F_MACRO_OBJ,
    F_STATIC,
    blob_text,
    decode,
    encode,
    extract_many,
    init_worker,
)
from cache_engine import BYTES, INT, CacheError, CacheSpace, In, Range, StoreSpec, get_cache_engine
from webapp.backend.config import get_active_repo_name, get_repo_dir
from webapp.backend.database.pool import get_reader
from webapp.backend.services import disk_cache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

FORMAT = 2
SUFFIXES = (".c", ".h")
MAX_REVISIONS = 2
CHUNK = 48
MAX_BLOB = 4 << 20
_MACRO = F_MACRO_FN | F_MACRO_OBJ
NAMESPACE = "source-index"
STORES = (
    StoreSpec("facts", (BYTES("sha"), INT("used"), INT("size"), BYTES("data")), key=("sha",), indexes=(("used",),)),
)


def _settings() -> dict[str, Any]:
    try:
        from core.config import get_webapp_config
        cfg = get_webapp_config().get("source_index") or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    try:
        workers = max(1, min(16, int(cfg.get("workers", 2))))
    except (TypeError, ValueError):
        workers = 2
    try:
        max_mb = max(64, int(cfg.get("max_mb", 1024)))
    except (TypeError, ValueError):
        max_mb = 1024
    try:
        revisions = max(0, min(64, int(cfg.get("revisions", 8))))
    except (TypeError, ValueError):
        revisions = 8
    return {"workers": workers, "max_mb": max_mb, "revisions": revisions}


# ------------------------------------------------------------------------------------- facts store

class FactStore:
    """blob SHA -> facts of one family, in a cache_engine space (one per family and format, for every repository: blob
    SHAs are content addresses). The default is the call facts (`parser/source_facts.py`); the Kernel API catalogs keep
    their records (`parser/kapi_facts.py`) in a store of their own (`kapi_service.kapi_store`)."""

    def __init__(self, name: str = "facts", fmt: int = FACTS_FORMAT, encoder: Callable[[Any], bytes] = encode,
                 decoder: Callable[[Any], Any] = decode) -> None:
        self.name, self.fmt, self.encoder, self.decoder = name, fmt, encoder, decoder

    def space(self) -> CacheSpace:
        return get_cache_engine().open(NAMESPACE, f"{self.name}-v{self.fmt}", STORES)

    def _facts(self):
        return self.space().store("facts")

    def get_many(self, shas: list[str]) -> dict[str, dict[str, Any]]:
        """Facts of the blobs already extracted (and marks them used now)."""
        out: dict[str, dict[str, Any]] = {}
        facts_store = self._facts()
        now = int(time.time() // 3600)
        for i in range(0, len(shas), 500):
            part = [bytes.fromhex(s) for s in shas[i:i + 500]]
            rows = facts_store.get_many(part)
            for sha, (_s, _used, _size, data) in rows.items():
                facts = self.decoder(data)
                if facts is not None:
                    out[sha.hex()] = facts
            if rows:
                facts_store.update_where([In("sha", part), Range("used", lt=now)], {"used": now})
        return out

    def missing(self, shas: list[str]) -> list[str]:
        """The blobs of `shas` not in the store."""
        have: set[bytes] = set()
        facts_store = self._facts()
        for i in range(0, len(shas), 500):
            have.update(r[0] for r in facts_store.scan(In("sha", [bytes.fromhex(s) for s in shas[i:i + 500]]), columns=("sha",)))
        return [s for s in shas if bytes.fromhex(s) not in have]

    def put_many(self, items: list[tuple[str, dict[str, Any]]]) -> None:
        if not items:
            return
        now = int(time.time() // 3600)
        rows = []
        for sha, facts in items:
            data = self.encoder(facts)
            rows.append((bytes.fromhex(sha), now, len(data), data))
        self._facts().put_many(rows)

    def size(self) -> int:
        return int(self._facts().sum("size"))

    def prune(self, max_bytes: int) -> int:
        """Drop the least recently used blobs until the store holds at most 3/4 of `max_bytes`; returns rows dropped."""
        facts_store = self._facts()
        total = self.size()
        dropped = 0
        while total > max_bytes * 3 // 4:
            rows = list(facts_store.scan(columns=("sha", "size"), order_by=("used",), limit=2000))
            if not rows:
                break
            facts_store.delete_many(r[0] for r in rows)
            total -= sum(r[1] for r in rows)
            dropped += len(rows)
        if dropped:
            self.space().compact()
        return dropped

    def count(self) -> int:
        return self._facts().count()


fact_store = FactStore()


# ------------------------------------------------------------------------------------- the parser's facts

def _db_rows(shas: list[str], with_data: bool, family: int = FAMILY, fmt: int = FACTS_FORMAT) -> Iterator[dict[str, Any]]:
    cols = ["blob_hash", "data"] if with_data else ["blob_hash"]
    with get_reader() as reader:
        for i in range(0, len(shas), 500):
            part = [bytes.fromhex(s) for s in shas[i:i + 500]]
            yield from reader.select_rows("m_blob_facts", cols, {"family": family, "format": fmt, "blob_hash": part})


def db_has(shas: list[str], family: int = FAMILY, fmt: int = FACTS_FORMAT) -> set[str]:
    """The blobs whose facts of the family the parser stored (m_blob_facts, current format); empty without a database."""
    try:
        return {bytes(r["blob_hash"]).hex() for r in _db_rows(shas, False, family, fmt)}
    except Exception as exc:  # noqa: BLE001 - no database, or a database without the table
        logger.debug("m_blob_facts unavailable: %s", exc)
        return set()


def db_facts(shas: list[str], family: int = FAMILY, fmt: int = FACTS_FORMAT,
             decoder: Callable[[Any], Any] = decode) -> dict[str, Any]:
    """Facts of the family the parser stored for these blobs."""
    out: dict[str, Any] = {}
    try:
        for r in _db_rows(shas, True, family, fmt):
            facts = decoder(r["data"])
            if facts is not None:
                out[bytes(r["blob_hash"]).hex()] = facts
    except Exception as exc:  # noqa: BLE001
        logger.debug("m_blob_facts unavailable: %s", exc)
    return out


def load_facts(shas: list[str]) -> dict[str, dict[str, Any]]:
    """Facts of blobs: the database's, then the local store's for the others."""
    found = db_facts(shas)
    rest = [s for s in shas if s not in found]
    if rest:
        found.update(fact_store.get_many(rest))
    return found


# ------------------------------------------------------------------------------------- Git

def _git(args: list[str], timeout: float = 120.0) -> bytes | None:
    try:
        out = subprocess.run(["git", "-C", str(get_repo_dir()), *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def revision_files(sha: str, suffixes: tuple[str, ...] = SUFFIXES) -> list[tuple[str, str]]:
    """(path, blob SHA) of the files of a commit (validated full SHA) with these suffixes (C sources and headers by
    default), in tree order."""
    wanted = tuple(x.encode() for x in suffixes)
    out = _git(["ls-tree", "-r", "-z", "--full-tree", sha], timeout=300)
    if out is None:
        raise RuntimeError(f"git ls-tree failed for {sha}")
    files = []
    for entry in out.split(b"\0"):
        if not entry or b"\t" not in entry:
            continue
        meta, path = entry.split(b"\t", 1)
        parts = meta.split()
        if len(parts) == 3 and parts[1] == b"blob" and path.endswith(wanted):
            files.append((path.decode("utf-8", "replace"), parts[2].decode()))
    return files


def read_blobs(shas: list[str]) -> Iterator[tuple[str, str | None]]:
    """(blob SHA, text) through one `git cat-file --batch` (None: missing or over 4 MB)."""
    proc = subprocess.Popen(["git", "-C", str(get_repo_dir()), "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha in shas:
            proc.stdin.write(sha.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) < 3 or header[1] != b"blob":
                yield sha, None
                continue
            size = int(header[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)
            yield sha, (blob_text(body) if size <= MAX_BLOB else None)
    finally:
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def extract_blobs(shas: list[str], progress: Callable | None = None, workers: int = 1, store: FactStore | None = None,
                  extractor: Callable | None = None, empty: Any = None) -> int:
    """Extract the facts of blobs not in the store yet (in `workers` processes when more than one) and store them;
    returns how many were extracted. `store`, `extractor` (a picklable `[(sha, text)] -> [(sha, facts)]`) and `empty`
    (stored for a file that defeats it) default to the call facts'."""
    store = store if store is not None else fact_store
    extractor = extractor if extractor is not None else extract_many
    empty = empty if empty is not None else EMPTY
    done = [0]
    pending: list[tuple[str, Any]] = []

    def keep(results: list[tuple[str, Any]]) -> None:
        for sha, facts in results:
            pending.append((sha, facts if facts is not None else empty))
        done[0] += len(results)
        if len(pending) >= 500:
            store.put_many(pending)
            pending.clear()
        if progress:
            progress(done[0], len(shas), "reading the sources")

    chunk: list[tuple[str, str]] = []
    if workers <= 1 or len(shas) < 200:
        for sha, text in read_blobs(shas):
            chunk.append((sha, text or ""))
            if len(chunk) >= CHUNK:
                keep(extractor(chunk))
                chunk = []
        keep(extractor(chunk))
    else:
        import multiprocessing
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=init_worker) as pool:
            inflight: list[Future] = []
            for sha, text in read_blobs(shas):
                chunk.append((sha, text or ""))
                if len(chunk) >= CHUNK:
                    inflight.append(pool.submit(extractor, chunk))
                    chunk = []
                while len(inflight) > 2 * workers:
                    keep(inflight.pop(0).result())
            if chunk:
                inflight.append(pool.submit(extractor, chunk))
            for fut in inflight:
                keep(fut.result())
    store.put_many(pending)
    return done[0]


# ------------------------------------------------------------------------------------- the revision

def _csr(n: int, keys: array, *columns: array) -> tuple[array, ...]:
    """Rows grouped by key (0 <= key < n): the offsets (n + 1 entries), then each column reordered by key."""
    off = array("i", bytes(4 * (n + 1)))
    for k in keys:
        off[k + 1] += 1
    for i in range(n):
        off[i + 1] += off[i]
    pos = array("i", off)
    m = len(keys)
    outs = [array("i", bytes(4 * m)) for _ in columns]
    for i in range(m):
        k = keys[i]
        j = pos[k]
        pos[k] = j + 1
        for out, col in zip(outs, columns):
            out[j] = col[i]
    return (off, *outs)


FILE_SPAN = 1 << 20  # file ids per name in the key of a file-local node


class RevisionIndex:
    """The assembled index of one commit (see the module docstring). Large parts are arrays (compressed sparse rows):
    a v3.0 tree (30k files, 400k nodes, 1.9M calls) holds ~100 MB."""

    def __init__(self, sha: str, files: list[tuple[str, str]], load: Callable[[list[str]], dict[str, dict[str, Any]]],
                 progress: Callable | None = None) -> None:
        """`load(blob SHAs)` returns their facts; it is called in batches, twice per file (memory stays bounded)."""
        self.sha = sha
        self.built_at = time.time()
        self.paths = [p for p, _ in files]
        self.names: list[str] = []
        self.name_ids: dict[str, int] = {}
        self.node_name = array("i")
        self.node_file = array("i")
        self._global = array("i")          # name id -> its shared node, or -1
        self._local: dict[int, int] = {}   # name id * FILE_SPAN + file id -> node of a file-local definition
        self._allowed: dict[str, bytearray] = {}
        self._table_entries: dict[int, list[int]] | None = None
        per_file: list[array | None] = [None] * len(files)
        d_node, d_file, d_line_s, d_line_e, d_flags = array("i"), array("i"), array("i"), array("i"), array("i")

        for fid, path, f in self._batches(files, load):
            if not f:
                continue
            local_scope = path.endswith(".c")
            nodes = array("i")
            for name, line_s, line_e, flags in f["d"]:
                local = local_scope and bool(flags & (F_STATIC | _MACRO))
                node = self._node(name, fid if local else -1)
                d_node.append(node)
                d_file.append(fid)
                d_line_s.append(line_s)
                d_line_e.append(line_e)
                d_flags.append(flags)
                nodes.append(node)
            per_file[fid] = nodes
            if progress and fid % 2000 == 0:
                progress(fid, 2 * len(files), "definitions")
        self.def_off, self.def_file, self.def_line_s, self.def_line_e, self.def_flags = _csr(
            len(self.node_name), d_node, d_file, d_line_s, d_line_e, d_flags)
        del d_node, d_file, d_line_s, d_line_e, d_flags

        src, dst, site_path, site_line = array("i"), array("i"), array("i"), array("i")
        self.member_calls: dict[int, list[tuple[int, int, int]]] = {}       # caller -> [(field id, path id, line)]
        self.field_callers: dict[int, list[tuple[int, int, int]]] = {}     # field id -> [(caller, path id, line)]
        tables: list[tuple[int, int, int, int, int]] = []                   # (struct id, table id, path id, line, positional)
        entries: list[tuple[int, int, int, int, int]] = []                  # (table idx, field id, leaf id, node, line)
        for fid, path, f in self._batches(files, load):
            nodes = per_file[fid]
            if nodes is None or not f:
                continue
            for def_idx, callee, line in f["c"]:
                src.append(nodes[def_idx])
                dst.append(self._resolve(callee, fid))
                site_path.append(fid)
                site_line.append(line)
            for def_idx, field, line in f["m"]:
                caller = nodes[def_idx]
                fld = self._name(field)
                self.member_calls.setdefault(caller, []).append((fld, fid, line))
                self.field_callers.setdefault(fld, []).append((caller, fid, line))
            for struct, table, line, positional, items in f["t"]:
                t_idx = len(tables)
                tables.append((self._name(struct), self._name(table), fid, line, positional))
                for field, value, vline in items:
                    node = self._resolve(value, fid, create=False)
                    if node is None or not self.is_function(node):
                        continue
                    entries.append((t_idx, self._name(field), self._name(field.rsplit(".", 1)[-1]), node, vline))
            for var, field, val, aline in f.get("a", []):
                node = self._resolve(val, fid, create=False)
                if node is None or not self.is_function(node):
                    continue
                t_idx = len(tables)
                tables.append((self._name("<dynamic>"), self._name(var), fid, aline, 0))
                entries.append((t_idx, self._name(field), self._name(field.rsplit(".", 1)[-1]), node, aline))
            per_file[fid] = None
            if progress and fid % 2000 == 0:
                progress(len(files) + fid, 2 * len(files), "calls")

        n = len(self.node_name)
        self.out_off, self.out_dst, self.out_path, self.out_line = _csr(n, src, dst, site_path, site_line)
        self.in_off, self.in_src, self.in_path, self.in_line = _csr(n, dst, src, site_path, site_line)
        self.calls = len(src)
        del src, dst, site_path, site_line
        self.tables = tables
        self.entries = entries
        self.struct_tables: dict[int, list[int]] = {}
        for i, t in enumerate(tables):
            self.struct_tables.setdefault(t[0], []).append(i)
        self.leaf_entries: dict[int, list[int]] = {}
        self.node_entries: dict[int, list[int]] = {}
        for i, e in enumerate(entries):
            self.leaf_entries.setdefault(e[2], []).append(i)
            self.node_entries.setdefault(e[3], []).append(i)
        # Lookups by name from now on: a sorted array of name ids instead of the dict (~40 MB on v3.0); the
        # file-local nodes are only needed to resolve calls while assembling
        self._sorted = array("i", sorted(range(len(self.names)), key=self.names.__getitem__))
        self.name_ids = None
        self._local = None

    @staticmethod
    def _batches(files: list[tuple[str, str]], load: Callable) -> Iterator[tuple[int, str, dict[str, Any] | None]]:
        for start in range(0, len(files), 1000):
            part = files[start:start + 1000]
            facts = load(list(dict.fromkeys(b for _p, b in part)))
            for i, (path, blob) in enumerate(part):
                yield start + i, path, facts.get(blob)

    # -- names and nodes
    def _name(self, name: str) -> int:
        i = self.name_ids.get(name)
        if i is None:
            i = self.name_ids[name] = len(self.names)
            self.names.append(name)
            self._global.append(-1)
        return i

    def _node(self, name: str, fid: int) -> int:
        nid = self._name(name)
        node = self._global[nid] if fid < 0 else self._local.get(nid * FILE_SPAN + fid, -1)
        if node < 0:
            node = len(self.node_name)
            self.node_name.append(nid)
            self.node_file.append(fid)
            if fid < 0:
                self._global[nid] = node
            else:
                self._local[nid * FILE_SPAN + fid] = node
        return node

    def _resolve(self, name: str, fid: int, create: bool = True) -> int | None:
        """The node a name used in file `fid` binds to: the file's own static function or macro, else the shared one."""
        nid = self.name_ids.get(name)
        if nid is not None:
            node = self._local.get(nid * FILE_SPAN + fid, -1)
            if node < 0:
                node = self._global[nid]
            if node >= 0:
                return node
        return self._node(name, -1) if create else None

    def name_id(self, name: str) -> int | None:
        if self.name_ids is not None:
            return self.name_ids.get(name)
        i = bisect.bisect_left(self._sorted, name, key=self.names.__getitem__)
        if i < len(self._sorted) and self.names[self._sorted[i]] == name:
            return self._sorted[i]
        return None

    def nodes_named(self, name: str) -> list[int]:
        """Every node of a name: the shared one first, then the file-local ones."""
        nid = self.name_id(name)
        if nid is None:
            return []
        out = [self._global[nid]] if self._global[nid] >= 0 else []
        out.extend(node for node in range(len(self.node_name)) if self.node_name[node] == nid and self.node_file[node] >= 0)
        return out

    def name_of(self, node: int) -> str:
        return self.names[self.node_name[node]]

    def file_of(self, node: int) -> str | None:
        fid = self.node_file[node]
        return self.paths[fid] if fid >= 0 else None

    def _def_range(self, node: int) -> range:
        return range(self.def_off[node], self.def_off[node + 1])

    def is_function(self, node: int) -> bool:
        if node + 1 >= len(self.def_off):
            return False  # created after the definitions were grouped: no definition
        return any(not (self.def_flags[j] & _MACRO) for j in self._def_range(node))

    def definitions(self, node: int) -> list[dict[str, Any]]:
        if node + 1 >= len(self.def_off):
            return []
        return [{"path": self.paths[self.def_file[j]], "line": self.def_line_s[j], "line_e": self.def_line_e[j],
                 "static": bool(self.def_flags[j] & F_STATIC), "macro": bool(self.def_flags[j] & _MACRO)}
                for j in self._def_range(node)]

    # -- ARCH
    def allowed(self, arch_key: str, test: Callable[[str], bool]) -> bytearray:
        """Per path id: 1 when calls made there count for the ARCH (cached per ARCH)."""
        hit = self._allowed.get(arch_key)
        if hit is None:
            hit = bytearray(1 if test(p) else 0 for p in self.paths)
            self._allowed[arch_key] = hit
        return hit

    # -- edges: (other node, path id, line, via)
    def callees(self, node: int, allowed: bytearray | None = None, indirect: bool = False,
                cap: int = 300) -> list[tuple[int, int, int, str]]:
        out = []
        for j in range(self.out_off[node], self.out_off[node + 1]):
            p = self.out_path[j]
            if allowed is None or allowed[p]:
                out.append((self.out_dst[j], p, self.out_line[j], ""))
        if indirect:
            seen: set[tuple[int, int]] = set()
            for fld, p, line in self.member_calls.get(node, ()):
                if allowed is not None and not allowed[p]:
                    continue
                for i in self.leaf_entries.get(fld, ())[:cap]:
                    t_idx, field, _leaf, target, _vline = self.entries[i]
                    if (fld, target) in seen:
                        continue
                    seen.add((fld, target))
                    out.append((target, p, line, f"{self.names[self.tables[t_idx][0]]}.{self.names[field]}"))
        return out

    def callers(self, node: int, allowed: bytearray | None = None, indirect: bool = False,
                cap: int = 300) -> list[tuple[int, int, int, str]]:
        out = []
        for j in range(self.in_off[node], self.in_off[node + 1]):
            p = self.in_path[j]
            if allowed is None or allowed[p]:
                out.append((self.in_src[j], p, self.in_line[j], ""))
        if indirect:
            seen: set[int] = set()
            for i in self.node_entries.get(node, ()):
                t_idx, field, leaf, _target, _vline = self.entries[i]
                if leaf in seen:
                    continue
                seen.add(leaf)
                via = f"{self.names[self.tables[t_idx][0]]}.{self.names[field]}"
                for caller, p, line in self.field_callers.get(leaf, ())[:cap]:
                    if allowed is None or allowed[p]:
                        out.append((caller, p, line, via))
        return out

    # -- ops tables (N1)
    def struct_report(self, struct: str) -> dict[str, Any]:
        sid = self.name_id(struct)
        idxs = list(self.struct_tables.get(sid, [])) if sid is not None else []
        if not idxs:
            for s_id in self.struct_tables:
                s_name = self.names[s_id]
                if s_name.endswith("::" + struct) or struct.endswith("::" + s_name):
                    for idx in self.struct_tables[s_id]:
                        if idx not in idxs:
                            idxs.append(idx)
        wanted = set(idxs)
        fields: dict[str, list[dict[str, Any]]] = {}
        files = {self.tables[i][2] for i in idxs}
        positional = sum(self.tables[i][4] for i in idxs)
        for t_idx, field, _leaf, node, line in (self.entries[i] for i in self._entries_of_tables(wanted)):
            name, fid, tline = self.tables[t_idx][1], self.tables[t_idx][2], self.tables[t_idx][3]
            fields.setdefault(self.names[field], []).append({
                "function": self.name_of(node), "path": self.paths[fid], "line": line,
                "table": {"name": self.names[name], "path": self.paths[fid], "line": tline}})
        return {"struct": struct, "tables": len(idxs), "files": len(files), "positional_skipped": positional,
                "fields": {k: fields[k] for k in sorted(fields)}}

    def _entries_of_tables(self, wanted: set[int]) -> Iterable[int]:
        if not wanted:
            return []
        if self._table_entries is None:
            te: dict[int, list[int]] = {}
            for i, e in enumerate(self.entries):
                te.setdefault(e[0], []).append(i)
            self._table_entries = te
        return [i for t in sorted(wanted) for i in self._table_entries.get(t, ())]

    def uses_of(self, function: str) -> list[dict[str, Any]]:
        out = []
        for node in self.nodes_named(function):
            for i in self.node_entries.get(node, ()):
                t_idx, field, _leaf, _node, line = self.entries[i]
                struct, table, fid, tline, _pos = self.tables[t_idx]
                out.append({"struct": self.names[struct], "field": self.names[field], "table": self.names[table],
                            "table_line": tline, "line": line, "path": self.paths[fid]})
        out.sort(key=lambda u: (u["path"], u["line"]))
        return out

    def summary(self) -> dict[str, Any]:
        return {"sha": self.sha, "built_at": self.built_at, "files": len(self.paths), "nodes": len(self.node_name),
                "calls": self.calls, "member_calls": sum(len(v) for v in self.member_calls.values()),
                "tables": len(self.tables), "table_entries": len(self.entries)}

    # -- persistence: zlib of a JSON header line, the names and paths (NUL-separated), then raw int32 arrays
    _ARRAYS = ("node_name", "node_file", "_global", "def_off", "def_file", "def_line_s", "def_line_e", "def_flags",
               "out_off", "out_dst", "out_path", "out_line", "in_off", "in_src", "in_path", "in_line", "_sorted")

    @staticmethod
    def _flatten(groups: dict[int, list[tuple[int, int, int]]]) -> tuple[array, ...]:
        key, a, b, c = array("i"), array("i"), array("i"), array("i")
        for k, items in groups.items():
            for x, y, z in items:
                key.append(k)
                a.append(x)
                b.append(y)
                c.append(z)
        return key, a, b, c

    def to_bytes(self) -> bytes:
        cols: dict[str, array] = {name: getattr(self, name) for name in self._ARRAYS}
        for prefix, groups in (("mc", self.member_calls), ("fc", self.field_callers)):
            for suffix, arr in zip("kabc", self._flatten(groups)):
                cols[f"{prefix}_{suffix}"] = arr
        for prefix, rows in (("t", self.tables), ("e", self.entries)):
            for j in range(5):
                cols[f"{prefix}{j}"] = array("i", (r[j] for r in rows))
        names = "\0".join(self.names).encode("utf-8", "surrogatepass")
        paths = "\0".join(self.paths).encode("utf-8", "surrogatepass")
        header = {"format": FORMAT, "facts": FACTS_FORMAT, "sha": self.sha, "built_at": self.built_at, "calls": self.calls,
                  "n_names": len(self.names), "n_paths": len(self.paths), "names": len(names), "paths": len(paths),
                  "arrays": [[name, len(arr)] for name, arr in cols.items()], "byteorder": sys.byteorder}
        parts = [json.dumps(header, separators=(",", ":")).encode() + b"\n", names, paths]
        parts.extend(arr.tobytes() for arr in cols.values())
        return zlib.compress(b"".join(parts), 1)

    @classmethod
    def from_bytes(cls, data: bytes) -> RevisionIndex | None:
        """`to_bytes()` reversed; None for another format or damaged bytes."""
        try:
            raw = zlib.decompress(data)
            nl = raw.index(b"\n")
            header = json.loads(raw[:nl])
        except (zlib.error, ValueError):
            return None
        if header.get("format") != FORMAT or header.get("facts") != FACTS_FORMAT or header.get("byteorder") != sys.byteorder:
            return None
        pos = nl + 1
        names = raw[pos:pos + header["names"]].decode("utf-8", "surrogatepass")
        pos += header["names"]
        paths = raw[pos:pos + header["paths"]].decode("utf-8", "surrogatepass")
        pos += header["paths"]
        cols: dict[str, array] = {}
        for name, n in header["arrays"]:
            arr = array("i")
            arr.frombytes(raw[pos:pos + 4 * n])
            pos += 4 * n
            cols[name] = arr
        if pos != len(raw):
            return None
        self = cls.__new__(cls)
        self.sha = header["sha"]
        self.built_at = header["built_at"]
        self.calls = header["calls"]
        self.names = names.split("\0") if header["n_names"] else []
        self.paths = paths.split("\0") if header["n_paths"] else []
        for name in cls._ARRAYS:
            setattr(self, name, cols[name])
        self.member_calls = cls._group(cols, "mc")
        self.field_callers = cls._group(cols, "fc")
        self.tables = list(zip(*(cols[f"t{j}"] for j in range(5))))
        self.entries = list(zip(*(cols[f"e{j}"] for j in range(5))))
        self.struct_tables, self.leaf_entries, self.node_entries = {}, {}, {}
        for i, t in enumerate(self.tables):
            self.struct_tables.setdefault(t[0], []).append(i)
        for i, e in enumerate(self.entries):
            self.leaf_entries.setdefault(e[2], []).append(i)
            self.node_entries.setdefault(e[3], []).append(i)
        self.name_ids = None
        self._local = None
        self._allowed = {}
        self._table_entries = None
        return self

    @staticmethod
    def _group(cols: dict[str, array], prefix: str) -> dict[int, list[tuple[int, int, int]]]:
        out: dict[int, list[tuple[int, int, int]]] = {}
        for k, a, b, c in zip(cols[f"{prefix}_k"], cols[f"{prefix}_a"], cols[f"{prefix}_b"], cols[f"{prefix}_c"]):
            out.setdefault(k, []).append((a, b, c))
        return out


# ------------------------------------------------------------------------------------- jobs and cache

_REVISIONS: OrderedDict[str, RevisionIndex] = OrderedDict()
_LOCK = threading.Lock()


def _key(sha: str) -> str:
    return f"{get_active_repo_name()}|{FORMAT}|{FACTS_FORMAT}|{sha}"


_SAVED = disk_cache.BinaryDiskCache("source-index-rev", max_bytes=2 << 30)


def _saved_key(sha: str) -> str:
    # Commit SHAs name their tree in every repository; the formats change what is stored
    return f"{FORMAT}|{FACTS_FORMAT}|{sha}"


def load_saved(sha: str) -> RevisionIndex | None:
    """The revision as assembled earlier (disk cache), or None."""
    data = _SAVED.get_bytes(_saved_key(sha))
    if not data:
        return None
    idx = RevisionIndex.from_bytes(data)
    if idx is None or idx.sha != sha:
        _SAVED.delete(_saved_key(sha))
        return None
    return idx


def save(idx: RevisionIndex) -> bool:
    limit = _settings()["revisions"]
    if limit <= 0:
        return False
    _SAVED.max_entries = limit
    return _SAVED.put_bytes(_saved_key(idx.sha), idx.to_bytes())


def build_revision(sha: str, progress: Callable | None = None, workers: int | None = None) -> RevisionIndex:
    """The revision's index: saved earlier, else assembled from the facts (the parser's, then local extraction of
    the blobs it lacks) and saved."""
    started = time.time()
    saved = load_saved(sha)
    if saved is not None:
        logger.info("Source index of %s read from the disk cache in %.1f s", sha[:12], time.time() - started)
        return saved
    if progress:
        progress(0, 0, "listing the tree")
    files = revision_files(sha)
    blobs = list(dict.fromkeys(b for _p, b in files))
    in_db = db_has(blobs)
    missing = fact_store.missing([b for b in blobs if b not in in_db])
    if missing:
        settings = _settings()
        extract_blobs(missing, progress, workers or settings["workers"])
        try:
            fact_store.prune(settings["max_mb"] << 20)
        except CacheError as exc:
            logger.debug("source index prune failed: %s", exc)
    idx = RevisionIndex(sha, files, load_facts, progress)
    if progress:
        progress(1, 1, "saving")
    save(idx)
    logger.info("Source index of %s: %d files (%d facts from the database, %d extracted) in %.1f s",
                sha[:12], len(files), len(in_db), len(missing), time.time() - started)
    return idx


def _remember(key: str, idx: RevisionIndex) -> None:
    with _LOCK:
        _REVISIONS[key] = idx
        _REVISIONS.move_to_end(key)
        while len(_REVISIONS) > MAX_REVISIONS:
            _REVISIONS.popitem(last=False)


def revision_state(sha: str, wait: float = 0.0) -> dict[str, Any]:
    """{state: "done", index} once the revision is assembled; the job state (with progress) while it builds."""
    key = _key(sha)
    with _LOCK:
        idx = _REVISIONS.get(key)
        if idx is not None:
            _REVISIONS.move_to_end(key)
            return {"state": "done", "index": idx}

    def run(progress: Callable) -> dict[str, Any]:
        built = build_revision(sha, progress)
        _remember(key, built)
        return built.summary()

    state = jobs.submit(f"source-index:{key}", run)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"source-index:{key}", wait) or state
    if state.get("state") == "done":
        with _LOCK:
            idx = _REVISIONS.get(key)
        if idx is None:
            # Evicted since the job finished: build again
            jobs.forget(f"source-index:{key}")
            return revision_state(sha, wait)
        return {"state": "done", "index": idx}
    return state


def _db_blob_estimate() -> int | None:
    try:
        with get_reader() as reader:
            stats = reader.table_stats(["m_blob_facts"])
        # An estimate of the rows of both families (calls for .c/.h, Kernel API for .c/.h/.S): about two per blob
        return int(stats[0].rows) // 2 if stats else None
    except Exception:  # noqa: BLE001
        return None


def status() -> dict[str, Any]:
    try:
        return {"db_blobs": _db_blob_estimate(), "blobs": fact_store.count(), "bytes": fact_store.size(),
                "saved_revisions": len(_SAVED.entries()), "saved_bytes": _SAVED.size_bytes(),
                "revisions": [i.summary() for i in list(_REVISIONS.values())], **_settings()}
    except CacheError as exc:
        return {"error": str(exc)}

