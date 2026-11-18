"""webapp/backend/services/kapi_service.py - Kernel API catalogs (A1-A5, webapp/API.md section 4.9).

Syscalls, exported symbols, boot and module parameters, tracepoints and modules of any revision, assembled from the
Kernel API records of its blobs (`parser/kapi_facts.py`): the parser stores them per Git blob SHA for the versions it
ingests (`m_blob_facts` family 2, Step 6.9), and the blobs the database lacks (git-only revisions) are extracted here
once, into a local store (`kapi_store`), so a revision is never searched as a whole. The few files read whole (syscall
tables, kernel-parameters.txt) go through the persistent Git readers; module ownership comes from the Kbuild object
index (section 4.4). Each catalog is built once per (commit, ARCH) as a background job (section 2.15) and cached on
disk (section 2.14): a commit never changes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from fastapi import HTTPException

from cache_engine import CacheError
from parser import kapi_catalog as kc
from parser import kapi_facts as kf
from parser.kapi_catalog import (  # noqa: F401 - re-exported for tests and callers
    TBL_EXCLUDED_ABIS,
    _pp_eval,
    _syscall_defs,
    bits_conditional,
    is_trace_header,
    parse_kernel_parameters_doc,
    parse_syscall_table_s,
    parse_syscall_tbl,
    parse_unistd_header,
)
from parser.kapi_facts import (  # noqa: F401 - re-exported: the catalogs' readers (tests, device_index_service)
    invocation,
    parse_export,
    parse_tp_fields,
    parse_tracepoint,
    split_args,
    string_value,
    strip_comments,
)
from webapp.backend.config import get_active_repo_name
from webapp.backend.services import source_index as si
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.grep_service import grep_service
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

CATALOG_FORMAT = kc.CATALOG_FORMAT
_DISK = DiskCache("kapi", max_bytes=512 << 20)
SUFFIXES = (".c", ".h", ".S", ".rs")
MAX_REVISIONS = 2

# ------------------------------------------------------------------------------- source reading


class FileCache:
    """Lines of files of one commit, read once per catalog build."""

    def __init__(self, sha: str) -> None:
        self.sha = sha
        self._lines: dict[str, list[str]] = {}

    def lines(self, path: str) -> list[str]:
        if path not in self._lines:
            data, is_dir, _ = git_reader.read_file(self.sha, path)
            self._lines[path] = [] if data is None or is_dir else data.decode("utf-8", "replace").split("\n")
        return self._lines[path]

    def text(self, path: str) -> str:
        return "\n".join(self.lines(path))


# ------------------------------------------------------------------------------- Kernel API records of a revision

kapi_store = si.FactStore("kapi", kf.KAPI_FORMAT, kf.encode, kf.decode)
_REVISIONS: OrderedDict[str, list[tuple[str, dict[str, Any]]]] = OrderedDict()
_REV_LOCK = threading.Lock()
_BUILD_LOCKS: dict[str, threading.Lock] = {}


def _load_records(sha: str, progress: Callable | None) -> list[tuple[str, dict[str, Any]]]:
    started = time.time()
    if progress:
        progress(0, 0, "listing the tree")
    files = si.revision_files(sha, SUFFIXES)
    blobs = list(dict.fromkeys(b for _p, b in files))
    records: dict[str, dict[str, Any]] = si.db_facts(blobs, kf.FAMILY, kf.KAPI_FORMAT, kf.decode)
    from_db = len(records)
    rest = [b for b in blobs if b not in records]
    if rest:
        records.update(kapi_store.get_many(rest))
    missing = [b for b in rest if b not in records]
    if missing:
        settings = si._settings()
        si.extract_blobs(missing, progress, settings["workers"], store=kapi_store, extractor=kf.extract_many, empty={})
        records.update(kapi_store.get_many(missing))
        try:
            kapi_store.prune(settings["max_mb"] << 20)
        except CacheError as exc:
            logger.debug("Kernel API store prune failed: %s", exc)
    out = [(path, records[b]) for path, b in files if records.get(b)]
    logger.info("Kernel API records of %s: %d of %d files (%d blobs from the database, %d extracted) in %.1f s",
                sha[:12], len(out), len(files), from_db, len(missing), time.time() - started)
    return out


def revision_records(sha: str, progress: Callable | None = None) -> list[tuple[str, dict[str, Any]]]:
    """(path, Kernel API record) of the `.c` / `.h` / `.S` files of a commit that have one, in tree order (the order
    `git grep` reported them in): the database's records, then the local store's, extracting the blobs neither has.
    The two most recently used revisions stay in memory."""
    with _REV_LOCK:
        hit = _REVISIONS.get(sha)
        if hit is not None:
            _REVISIONS.move_to_end(sha)
            return hit
        lock = _BUILD_LOCKS.setdefault(sha, threading.Lock())
    with lock:  # one build per commit, however many catalogs ask at once
        with _REV_LOCK:
            hit = _REVISIONS.get(sha)
        if hit is None:
            hit = _load_records(sha, progress)
            with _REV_LOCK:
                _REVISIONS[sha] = hit
                while len(_REVISIONS) > MAX_REVISIONS:
                    _REVISIONS.popitem(last=False)
    with _REV_LOCK:
        _BUILD_LOCKS.pop(sha, None)
    return hit


def resolve(version: str) -> str:
    from webapp.backend.services.release_service import resolve_sha
    from webapp.backend.security.git import safe_rev
    safe_rev(version)
    sha = resolve_sha(version)
    if not sha:
        raise HTTPException(status_code=404, detail=f"Unknown revision: {version}")
    return sha


def _norm_arch(arch: str) -> str:
    return (arch or "x86_64").strip().lower()


def _db_catalog(kind: str, version: str, arch: str = "") -> dict[str, Any] | None:
    """Return a pre-parsed KAPI catalog payload from `m_kapi_catalog` when formats match, else None."""
    from webapp.backend.security.git import safe_rev

    safe_rev(version)
    db_arch = "" if kind in kc.ARCH_INDEPENDENT_KINDS else _norm_arch(arch)
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        from webapp.backend.database.pool import get_reader

        with get_reader() as reader:
            repo_id = get_active_repo_id(reader)
            commit_id = reader.kapi_resolve_commit_id(version, repo_id=repo_id)
            if commit_id is None:
                return None
            row = reader.kapi_catalog(commit_id, kind, arch=db_arch)
            if not row:
                return None
            if int(row.get("catalog_format") or 0) != CATALOG_FORMAT or int(row.get("kapi_format") or 0) != kf.KAPI_FORMAT:
                return None
            data = row.get("data")
            if not data:
                return None
            return kc.decode_catalog(data)
    except Exception as exc:  # noqa: BLE001 - fall back to git/DiskCache when DB is unavailable
        logger.debug("DB KAPI catalog lookup failed for %s (%s, %s): %s", version, kind, db_arch, exc)
        return None


def _catalog(kind: str, version: str, build: Callable[[str, Callable], Any], arch: str = "", wait: float = 0.0) -> dict[str, Any]:
    hit = _db_catalog(kind, version, arch)
    if hit is not None:
        return {"state": "done", "result": hit}
    sha = resolve(version)
    ck = f"{get_active_repo_name()}|{kind}|{CATALOG_FORMAT}.{kf.KAPI_FORMAT}|{sha}|{arch}"
    state = jobs.submit(f"kapi:{ck}", lambda p: build(sha, p), cache=_DISK, cache_key=ck)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"kapi:{ck}", wait) or state
    return state


def summary(version: str, arch: str = "x86_64") -> dict[str, Any]:
    """Per-release KAPI summary & counts: DB-first via `m_kapi_release_summary` / `m_kapi_catalog`,
    falling back to cached catalog metadata/counts when unparsed."""
    from webapp.backend.security.git import safe_rev

    safe_rev(version)
    norm_arch = _norm_arch(arch)
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        from webapp.backend.database.pool import get_reader

        with get_reader() as reader:
            repo_id = get_active_repo_id(reader)
            commit_id = reader.kapi_resolve_commit_id(version, repo_id=repo_id)
            if commit_id is not None:
                s_row = reader.kapi_release_summary(commit_id, arch=norm_arch)
                meta_rows = reader.kapi_catalog_meta(commit_id, arch=None)
                kinds_meta: dict[str, dict[str, Any]] = {}
                meta_counts: dict[str, int] = {}
                for r in meta_rows:
                    if r.get("arch") not in ("", norm_arch):
                        continue
                    if int(r.get("catalog_format") or 0) != CATALOG_FORMAT or int(r.get("kapi_format") or 0) != kf.KAPI_FORMAT:
                        continue
                    kind = r["kind"]
                    meta_counts[kind] = int(r.get("total_count") or 0)
                    if r.get("summary_json"):
                        kinds_meta[kind] = json.loads(r["summary_json"])
                if (
                    s_row
                    and int(s_row.get("catalog_format") or 0) == CATALOG_FORMAT
                    and int(s_row.get("kapi_format") or 0) == kf.KAPI_FORMAT
                ):
                    counts = {
                        "syscalls": int(s_row["syscalls_count"]),
                        "exports": int(s_row["exports_count"]),
                        "params": int(s_row["params_count"]),
                        "tracepoints": int(s_row["tracepoints_count"]),
                        "modules": int(s_row["modules_count"]),
                        "devices": int(s_row["devices_count"]),
                    }
                    subsystems = json.loads(s_row["subsystems_json"]) if s_row.get("subsystems_json") else {}
                    return {
                        "version": version,
                        "arch": norm_arch,
                        "source": "db",
                        "preparsed": True,
                        "counts": counts,
                        "kinds": kinds_meta,
                        "subsystems": subsystems,
                    }
                if meta_counts:
                    counts_out: dict[str, int | None] = {k: meta_counts.get(k) for k in kc.KAPI_KINDS}
                    return {
                        "version": version,
                        "arch": norm_arch,
                        "source": "db_meta",
                        "preparsed": True,
                        "counts": counts_out,
                        "kinds": kinds_meta,
                        "subsystems": {},
                    }
    except Exception as exc:  # noqa: BLE001 - fall back to DiskCache/unparsed summary
        logger.debug("DB KAPI summary lookup failed for %s (%s): %s", version, norm_arch, exc)

    sha = resolve(version)
    repo_name = get_active_repo_name()
    from webapp.backend.services import device_index_service as dis

    cached_payloads: dict[str, dict[str, Any]] = {}
    kinds_fb: dict[str, dict[str, Any]] = {}
    counts_fb: dict[str, int | None] = {}
    for kind in kc.KAPI_KINDS:
        if kind == "devices":
            payload = dis._DISK.get(f"{repo_name}|devices|{dis.INDEX_FORMAT}|{sha}")
        else:
            k_arch = "" if kind in kc.ARCH_INDEPENDENT_KINDS else norm_arch
            payload = _DISK.get(f"{repo_name}|{kind}|{CATALOG_FORMAT}.{kf.KAPI_FORMAT}|{sha}|{k_arch}")
        if isinstance(payload, dict):
            cached_payloads[kind] = payload
            s = kc.summarize_catalog(kind, payload)
            kinds_fb[kind] = s
            counts_fb[kind] = int(s.get("total", 0))
        else:
            counts_fb[kind] = None
    subsystems_fb = (
        kc.build_release_summary(cached_payloads)["subsystems"]
        if len(cached_payloads) == len(kc.KAPI_KINDS)
        else {}
    )
    return {
        "version": version,
        "arch": norm_arch,
        "source": "cache" if cached_payloads else "unparsed",
        "preparsed": False,
        "counts": counts_fb,
        "kinds": kinds_fb,
        "subsystems": subsystems_fb,
    }


def _kbuild_files(version: str, arch: str) -> dict[str, Any] | None:
    """The Kbuild object index of the ARCH (None when it cannot be built)."""
    try:
        from webapp.backend.services.kbuild_index_service import index_now
        return index_now(version, arch, timeout=180.0)
    except Exception as exc:  # noqa: BLE001 - catalogs work without module ownership
        logger.debug("Kbuild index unavailable for %s %s: %s", version, arch, exc)
        return None


def _owner(idx: dict[str, Any] | None, path: str) -> dict[str, Any] | None:
    if not idx:
        return None
    hits = idx["files"].get(path) or []
    if not hits:
        return None
    o = idx["objects"][hits[0]]
    return {"modname": o["modname"], "target": o["target"], "expr": idx["exprs"][o["expr"]]}


# ------------------------------------------------------------------------------- A1 syscalls


def _numbered_elsewhere(files: Any, paths: set[str], arch: str, srcarch: str) -> dict[str, set[str]]:
    if not hasattr(files, "text"):
        orig = files
        files = kc.TextCache(lambda p: "\n".join(orig.lines(p)))
    return kc._numbered_elsewhere(files, paths, arch, srcarch)


def build_syscalls(sha: str, arch: str, progress: Callable | None = None) -> dict[str, Any]:
    from webapp.backend.services.kconfig_service import KconfigService
    from webapp.backend.services.tree_resolver import git_tree_resolver

    info = KconfigService().resolve_arch(sha, arch)
    paths = set((git_tree_resolver.list_tree(sha) or {}).keys())
    files = FileCache(sha)
    records = revision_records(sha, progress)
    return kc.build_syscalls(files, paths, records, info["arch"], info["srcarch"])


def syscalls(version: str, arch: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("syscalls", version, lambda sha, p: build_syscalls(sha, arch, p), arch=arch, wait=wait)


# ------------------------------------------------------------------------------- A2 exports

def build_exports(sha: str, progress: Callable | None = None) -> dict[str, Any]:
    return kc.build_exports(revision_records(sha, progress))


def exports(version: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("exports", version, build_exports, wait=wait)


def export_detail(version: str, symbol: str, arch: str) -> dict[str, Any]:
    if not re.fullmatch(r"\w{1,128}", symbol or ""):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    st = exports(version, wait=60.0)
    if st.get("state") != "done":
        raise HTTPException(status_code=503, detail="The export catalog is still being built, retry")
    sites = [e for e in st["result"]["exports"] if e["symbol"] == symbol]
    if not sites:
        raise HTTPException(status_code=404, detail=f"{symbol} is not exported at {version}")
    idx = _kbuild_files(version, arch)
    for s in sites:
        s["owner"] = _owner(idx, s["path"])
    return {"version": version, "symbol": symbol, "sites": sites}


# ------------------------------------------------------------------------------- A3 parameters


def build_params(sha: str, arch: str, version: str, progress: Callable | None = None) -> dict[str, Any]:
    from webapp.backend.services.kconfig_service import KconfigService
    from webapp.backend.services.tree_resolver import git_tree_resolver

    srcarch = KconfigService().resolve_arch(sha, arch)["srcarch"]
    paths = set((git_tree_resolver.list_tree(sha) or {}).keys())
    files = FileCache(sha)
    records = revision_records(sha, progress)
    idx = _kbuild_files(version, arch)
    return kc.build_params(files, paths, records, srcarch, kbuild_idx=idx)


def params(version: str, arch: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("params", version, lambda sha, p: build_params(sha, arch, version, p), arch=arch, wait=wait)


def param_doc(version: str, line: int) -> dict[str, Any]:
    """The documentation entry starting at `line` of kernel-parameters.txt (text only)."""
    st = params(version, "x86_64", wait=0.0)
    path = (st.get("result") or {}).get("doc_path") or "Documentation/kernel-parameters.txt"
    files = FileCache(resolve(version))
    lines = files.lines(path)
    out = []
    for raw in lines[max(0, line - 1): line + 60]:
        if out and raw.startswith("\t") and not raw.startswith("\t\t") and raw.strip():
            break
        out.append(raw)
    return {"path": path, "line": line, "text": "\n".join(out).rstrip()}


# ------------------------------------------------------------------------------- A4 tracepoints


def build_tracepoints(sha: str, progress: Callable | None = None) -> dict[str, Any]:
    return kc.build_tracepoints(revision_records(sha, progress))


def tracepoints(version: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("tracepoints", version, build_tracepoints, wait=wait)


def tracepoint_callsites(version: str, name: str) -> dict[str, Any]:
    if not re.fullmatch(r"\w{1,128}", name or ""):
        raise HTTPException(status_code=400, detail="Invalid tracepoint name")
    # For parsed versions, query m_symbol_ref first
    try:
        from webapp.backend.database.pool import get_reader
        with get_reader() as reader:
            from webapp.backend.services.symbol_service import SymbolService
            from webapp.backend.services.tree_resolver import best_path, definition_arch
            _, vname, blob_paths = SymbolService._scope(reader, version)
            _, scope, bits = definition_arch(vname, None)
            tp_names = [f"trace_{name}", f"trace_{name}_rcuidle", name]
            def_rows = reader.select_rows("m_symbol_def", ["ast_id", "blob_id", "line_s"], {"name": tp_names})
            defs = [r for r in def_rows if r["blob_id"] in blob_paths and r["ast_id"]]
            if defs:
                targets = list(dict.fromkeys(r["ast_id"] for r in defs))
                ref_rows = [
                    {"blob_id": r["fid"], "line_no": r["line"]}
                    for r in reader.symbol_refs_of_targets(targets, role=3, limit=500)
                ]
                if ref_rows:
                    sites = []
                    for r in ref_rows:
                        if r["blob_id"] in blob_paths:
                            fpath = best_path(blob_paths[r["blob_id"]], scope, bits)
                            if not fpath.startswith("include/trace/events/"):
                                sites.append({"path": fpath, "line": r["line_no"], "text": f"trace_{name}(...)"})
                    if sites:
                        return {"name": name, "call_sites": sites, "truncated": len(ref_rows) >= 500}
    except Exception:
        pass
    sha = resolve(version)
    res = grep_service.grep(sha, rf"\btrace_{name}(_rcuidle)?[[:space:]]*\(", mode="regex", paths=["*.c", "*.h", "*.rs"],
                            max_matches=500, max_per_file=50)
    sites = [{"path": m["path"], "line": m["line"], "text": m["text"].strip()[:200]} for m in res["matches"]
             if not m["path"].startswith("include/trace/events/") and not m["text"].lstrip().startswith(("*", "//", "/*"))]
    return {"name": name, "call_sites": sites, "truncated": res["truncated"]}


# ------------------------------------------------------------------------------- A5 modules

def build_modules(sha: str, arch: str, version: str, progress: Callable | None = None) -> dict[str, Any]:
    idx = _kbuild_files(version, arch)
    if idx is None:
        raise RuntimeError("Kbuild object index unavailable")
    return kc.build_modules(revision_records(sha, progress), idx, arch=idx["arch"])


def modules(version: str, arch: str, wait: float = 0.0) -> dict[str, Any]:
    return _catalog("modules", version, lambda sha, p: build_modules(sha, arch, version, p), arch=arch, wait=wait)


def module_detail(version: str, arch: str, modname: str) -> dict[str, Any]:
    if not re.fullmatch(r"[\w-]{1,64}", modname or ""):
        raise HTTPException(status_code=400, detail="Invalid module name")
    st = modules(version, arch, wait=120.0)
    if st.get("state") != "done":
        raise HTTPException(status_code=503, detail="The module catalog is still being built, retry")
    mod = next((m for m in st["result"]["modules"] if m["modname"] == modname.replace("-", "_")), None)
    if mod is None:
        raise HTTPException(status_code=404, detail=f"No module {modname} for ARCH={arch} at {version}")
    members = set(mod["members"])
    out = dict(mod)
    p = params(version, arch, wait=0.0)
    out["params"] = [r for r in (p.get("result") or {}).get("params", []) if r["path"] in members] if p.get("state") == "done" else None
    e = exports(version, wait=0.0)
    out["exports"] = [x for x in (e.get("result") or {}).get("exports", []) if x["path"] in members] if e.get("state") == "done" else None
    try:
        from webapp.backend.services.maintainer_service import MaintainerService
        res = MaintainerService().match_maintainers(version, paths=list(members))
        out["maintainers"] = res.get("sections") or res.get("matches") or []
    except Exception:  # noqa: BLE001
        out["maintainers"] = []
    return out
