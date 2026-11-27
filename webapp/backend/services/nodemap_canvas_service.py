"""webapp/backend/services/nodemap_canvas_service.py - Programmatic NodeMap canvas authoring, persistence & expansion.

Stores saved canvases as atomic UTF-8 JSON files under `<nodemap_canvas_dir>/<safe_repo>/<canvas_id>.json`
with zero SQLite dependency and no coupling between `webapp` and `api` `cache_engine` instances.
Supports optimistic concurrency (`revision` / `expected_revision` -> HTTP 409), live node hydration,
struct `fromField`/`toField` resolution, batch declarative operations, server-side graph expansions
(`struct_types`, `callgraph`, `callpath`, `kconfig`, `commit`, `impact_map`), deterministic layout,
stateless read-only preview/export, and nested NodeMap references (`nodemap` nodes naming another saved
canvas by `data.canvasId`): acyclic reference validation on every write, bounded static previews of the
referenced canvases on reads (`nested_maps`), hierarchy reads, attach and group-into-nested operations.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from core.config import get_nodemap_canvas_dir
from webapp.backend.config import get_active_repo_name
from webapp.backend.security.git import safe_rev
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services.callgraph_service import CallgraphService
from webapp.backend.services.callpath_service import callpath_state
from webapp.backend.services.git_service import GitService
from webapp.backend.services.kconfig_service import DEFAULT_ARCH, KconfigService
from webapp.backend.services.maintainer_service import MaintainerService
from webapp.backend.services.nodemap_canvas_layout import (
    apply_canvas_layout,
    estimate_node_dimensions,
    export_canvas,
)
from webapp.backend.services.nodemap_canvas_schema import (
    CANVAS_SCHEMA,
    CANVAS_VERSION,
    GROUP_LAYOUT_PAD,
    ID_RE,
    LOCK_KINDS,
    NODE_TYPES,
    bounds_of,
    create_edge,
    create_node,
    default_title,
    display_title,
    empty_canvas,
    fit_group_to_members,
    fit_viewport,
    is_valid_id,
    new_id,
    normalize_canvas,
    normalize_color,
    normalize_edge,
    normalize_field_locks,
    normalize_layers,
    normalize_node,
    normalize_node_locks,
    normalize_settings,
    normalize_tours,
    remove_nodes,
    serialize_canvas,
    struct_item_key,
)
from webapp.backend.services.nodemap_nesting import (
    HIERARCHY_DEPTH_DEFAULT,
    HIERARCHY_DEPTH_MAX,
    HIERARCHY_MAX_ENTRIES,
    NESTED_NODE_TYPE,
    ancestors_of,
    build_static_preview,
    canvas_counts,
    find_cycle,
    nested_node_frame,
    nested_nodes,
    nested_refs,
    remap_tours,
    split_for_group,
)
from webapp.backend.services.nodemap_service import nodemap_service
from webapp.backend.services.symbol_service import SymbolService

_INFO_CACHE_MAX = 2048
_SAFE_SEGMENT_RE = re.compile(r"\A[\w-]{1,64}\Z", re.ASCII)

_QUALIFIERS = frozenset(
    {
        "const", "volatile", "restrict", "__restrict", "__restrict__", "static", "extern",
        "inline", "__inline", "__inline__", "register", "auto", "typedef", "_Atomic",
        "_Noreturn", "_Thread_local", "__extension__", "__user", "__kernel", "__iomem",
        "__percpu", "__rcu", "__force", "__bitwise", "__private", "__must_check",
        "__read_mostly", "__packed", "__randomize_layout", "__nocast", "__safe", "__data_racy",
    }
)
_SCALARS = frozenset(
    {
        "void", "char", "short", "int", "long", "float", "double", "bool", "_Bool", "_Complex",
        "__int128", "signed", "__signed__", "unsigned", "size_t", "ssize_t", "ptrdiff_t",
        "intptr_t", "uintptr_t", "intmax_t", "uintmax_t", "wchar_t", "off_t", "loff_t",
        "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "u_int8_t", "u_int16_t", "u_int32_t", "u_int64_t", "u_char", "u_short", "u_int", "u_long",
        "uint", "ulong", "ushort", "uchar", "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64",
        "u128", "s128", "__u8", "__u16", "__u32", "__u64", "__s8", "__s16", "__s32", "__s64",
        "__u128", "__s128", "__le16", "__le32", "__le64", "__be16", "__be32", "__be64",
        "__sum16", "__wsum", "pid_t", "uid_t", "gid_t", "dev_t", "ino_t", "mode_t", "umode_t",
        "nlink_t", "time_t", "clock_t", "key_t", "timer_t", "clockid_t", "fmode_t", "sector_t",
        "blkcnt_t", "ktime_t", "__kernel_pid_t", "__kernel_uid_t", "__kernel_gid_t",
        "__kernel_time_t", "__kernel_off_t", "__kernel_loff_t", "phys_addr_t", "dma_addr_t",
        "resource_size_t", "__kernel_size_t", "__kernel_ssize_t",
    }
)
_STANDARD_C_TYPES = _QUALIFIERS | _SCALARS
_TAG_KINDS = frozenset({"struct", "union", "enum"})

_STRUCT_SCAN_RE = re.compile(r"\b(?:struct|union)\s+([A-Za-z_]\w*)\b")
_HEADING_FN_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")
_SIG_LINE_RE = re.compile(r"^[a-zA-Z_][\w\s\*]*\b([A-Za-z_]\w*)\s*\([^;]*$")
_NON_FUNC_IDENTS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "return", "sizeof", "typeof",
        "__typeof__", "alignof", "__alignof__", "defined", "__attribute__", "__asm__", "asm",
        "struct", "union", "enum", "typedef", "static", "inline", "const", "volatile", "extern",
        "likely", "unlikely", "BUG_ON", "WARN_ON", "WARN_ON_ONCE", "BUILD_BUG_ON",
        "EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL", "MODULE_LICENSE", "MODULE_AUTHOR", "MODULE_DESCRIPTION",
    }
)

_callgraph_service = CallgraphService()
_kconfig_service = KconfigService()
_git_service = GitService()
_symbol_service = SymbolService()
_maintainer_service = MaintainerService()


def validate_safe_segment(value: str, label: str = "id") -> str:
    """Validate `repo` or `canvas_id` against `/^[\\w-]{1,64}$/` to prevent path traversal."""
    cleaned = str(value or "").strip()
    if not _SAFE_SEGMENT_RE.match(cleaned) or ".." in cleaned:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: must match /^[\\w-]{{1,64}}$/.",
        )
    return cleaned


def webapp_canvas_url(version: str, canvas_id: str, tour_id: str | None = None) -> str:
    ver = str(version or "v3.0").strip() or "v3.0"
    url = f"/#v={ver}&tab=nodemap&canvas={canvas_id}"
    if tour_id and is_valid_id(tour_id):
        url += f"&tour={tour_id}"
    return url


def _flatten_struct_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if it.get("kind") != "directive":
            out.append(it)
        children = it.get("children")
        if isinstance(children, list):
            out.extend(_flatten_struct_items(children))
    return out


def extract_type_refs(type_str: str) -> list[dict[str, Any]]:
    """Extract referenced non-standard C types from a struct member type string (`struct_view.js:extractTypeRefs`)."""
    cleaned = re.sub(r"/\*.*?\*/|//.*$", " ", str(type_str or "")).strip()
    if not cleaned:
        return []

    def seg_ref(seg: str, default_label: str = "") -> dict[str, Any] | None:
        toks = re.findall(r"[A-Za-z_]\w*|\*", seg)
        is_ptr = "*" in toks
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "*":
                i += 1
                continue
            if t in _TAG_KINDS:
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                if not nxt or nxt == "*" or nxt in _TAG_KINDS or nxt in _STANDARD_C_TYPES:
                    return None
                edge_lbl = default_label or ("points to" if is_ptr else ("type" if t == "enum" else "embeds"))
                return {"kind": t, "name": nxt, "isPointer": is_ptr, "edgeLabel": edge_lbl}
            if t in _QUALIFIERS:
                i += 1
                continue
            if re.match(r"^_{2,}[a-z]\w*$", t) and t not in _SCALARS and not t.endswith("_t"):
                i += 1
                continue
            if t in _SCALARS:
                return None
            if re.match(r"^[A-Z0-9_]+$", t):
                return None
            edge_lbl = default_label or ("points to" if is_ptr else "type")
            return {"kind": "typedef", "name": t, "isPointer": is_ptr, "edgeLabel": edge_lbl}
        return None

    ref = seg_ref(cleaned)
    return [ref] if ref else []


class NodeMapCanvasService:
    """Atomic JSON file persistence, hydration, batch operations, layout, and graph expansion for NodeMap."""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._canvas_locks: dict[tuple[str, str], threading.RLock] = {}
        self._graph_locks: dict[str, threading.RLock] = {}
        # (repo, canvas_id) -> (mtime_ns, size, info): reference index entries of the stored canvases
        self._info_cache: OrderedDict[tuple[str, str], tuple[int, int, dict[str, Any]]] = OrderedDict()

    def _graph_lock(self, repo: str) -> threading.RLock:
        """Per-repository lock serializing reference validation with the write it guards.

        Lock order: canvas locks (ascending id) before the graph lock."""
        with self._guard:
            lock = self._graph_locks.get(repo)
            if lock is None:
                lock = threading.RLock()
                self._graph_locks[repo] = lock
            return lock

    @contextlib.contextmanager
    def _canvas_locks_sorted(self, repo: str, ids: list[str]) -> Iterator[None]:
        with contextlib.ExitStack() as stack:
            for cid in sorted(set(ids)):
                stack.enter_context(self._lock_for(repo, cid))
            yield

    def _lock_for(self, repo: str, canvas_id: str) -> threading.RLock:
        key = (repo, canvas_id)
        with self._guard:
            lock = self._canvas_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._canvas_locks[key] = lock
            return lock

    def _base_dir(self) -> Path:
        return get_nodemap_canvas_dir()

    def _repo_dir(self, repo: str) -> Path:
        safe_repo = validate_safe_segment(repo, "repo")
        base = self._base_dir().resolve()
        target = (base / safe_repo).resolve()
        if target.parent != base:
            raise HTTPException(status_code=400, detail="Invalid repository canvas directory.")
        return target

    def _canvas_path(self, repo: str, canvas_id: str) -> Path:
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        repo_dir = self._repo_dir(repo)
        target = (repo_dir / f"{safe_id}.json").resolve()
        if target.parent != repo_dir:
            raise HTTPException(status_code=400, detail="Invalid canvas file path.")
        return target

    def _read_envelope(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        canvas = normalize_canvas(raw.get("canvas"), strict=True)
        if canvas is None:
            return None
        raw["canvas"] = canvas
        return raw

    def _write_envelope_atomic(self, path: Path, envelope: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(envelope, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _build_envelope(
        self,
        *,
        canvas_id: str,
        repo: str,
        name: str,
        description: str,
        version: str,
        revision: int,
        created_at: int,
        updated_at: int,
        created_by: str,
        canvas: dict[str, Any],
    ) -> dict[str, Any]:
        canonical = serialize_canvas(canvas)
        nodes = canonical.get("nodes") or []
        edges = canonical.get("edges") or []
        tours = canonical.get("tours") or []
        return {
            "id": canvas_id,
            "repo": repo,
            "name": str(name or "").strip()[:256] or "Untitled Canvas",
            "description": str(description or "").strip()[:2000],
            "version": str(version or "v3.0").strip()[:256] or "v3.0",
            "revision": int(revision),
            "created_at": int(created_at),
            "updated_at": int(updated_at),
            "created_by": str(created_by or "api").strip()[:128] or "api",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "tour_count": len(tours),
            "nested_count": len(nested_nodes(canonical)),
            "nested_refs": nested_refs(canonical),
            "webapp_url": webapp_canvas_url(version, canvas_id),
            "canvas": canonical,
        }

    def _summary_of(self, env: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": env["id"],
            "repo": env.get("repo", get_active_repo_name()),
            "name": env.get("name", "Untitled Canvas"),
            "description": env.get("description", ""),
            "version": env.get("version", "v3.0"),
            "revision": int(env.get("revision", 1)),
            "created_at": int(env.get("created_at", 0)),
            "updated_at": int(env.get("updated_at", 0)),
            "created_by": env.get("created_by", "api"),
            "node_count": int(env.get("node_count", len((env.get("canvas") or {}).get("nodes") or []))),
            "edge_count": int(env.get("edge_count", len((env.get("canvas") or {}).get("edges") or []))),
            "tour_count": int(env.get("tour_count", len((env.get("canvas") or {}).get("tours") or []))),
            "nested_count": len(nested_nodes(env.get("canvas"))),
            "nested_refs": nested_refs(env.get("canvas")),
            "webapp_url": env.get("webapp_url") or webapp_canvas_url(env.get("version", "v3.0"), env["id"]),
        }

    # ------------------------------------------------------------------ Nested NodeMap references

    def _canvas_info(self, repo: str, canvas_id: str) -> dict[str, Any] | None:
        """Reference-index entry of a stored canvas (None when absent or unreadable), cached per file stat.

        `{"id", "name", "description", "version", "revision", "updated_at", "refs", "nested": [{"node_id",
        "canvas_id", "title"}], "counts", "preview"}` -- never the document itself."""
        path = self._canvas_path(repo, canvas_id)
        try:
            st = path.stat()
        except OSError:
            with self._guard:
                self._info_cache.pop((repo, canvas_id), None)
            return None
        key = (repo, canvas_id)
        with self._guard:
            hit = self._info_cache.get(key)
            if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                self._info_cache.move_to_end(key)
                return hit[2]
        env = self._read_envelope(path)
        if env is None:
            return None
        canvas = env["canvas"]
        info = {
            "id": canvas_id,
            "name": str(env.get("name") or "Untitled Canvas"),
            "description": str(env.get("description") or ""),
            "version": str(env.get("version") or "v3.0"),
            "revision": int(env.get("revision", 1)),
            "updated_at": int(env.get("updated_at", 0)),
            "refs": nested_refs(canvas),
            "nested": [
                {"node_id": n["id"], "canvas_id": (n.get("data") or {}).get("canvasId") or "", "title": display_title(n)}
                for n in nested_nodes(canvas)
            ],
            "counts": canvas_counts(canvas),
            "preview": build_static_preview(canvas),
        }
        with self._guard:
            self._info_cache[key] = (st.st_mtime_ns, st.st_size, info)
            self._info_cache.move_to_end(key)
            while len(self._info_cache) > _INFO_CACHE_MAX:
                self._info_cache.popitem(last=False)
        return info

    def _drop_info(self, repo: str, canvas_id: str) -> None:
        with self._guard:
            self._info_cache.pop((repo, canvas_id), None)

    def _stored_ids(self, repo: str) -> list[str]:
        repo_dir = self._repo_dir(repo)
        if not repo_dir.is_dir():
            return []
        return sorted(
            fp.stem for fp in repo_dir.glob("*.json")
            if not fp.name.startswith(".") and _SAFE_SEGMENT_RE.match(fp.stem)
        )

    def _ref_graph(self, repo: str) -> dict[str, list[str]]:
        """Stored reference graph of a repository: canvas id -> referenced canvas ids."""
        graph: dict[str, list[str]] = {}
        for cid in self._stored_ids(repo):
            info = self._canvas_info(repo, cid)
            if info is not None:
                graph[cid] = info["refs"]
        return graph

    def _nested_summary(self, repo: str, canvas_id: str) -> dict[str, Any]:
        """Lightweight card of a referenced canvas: summary, counts and static preview, or `status: "missing"`."""
        info = self._canvas_info(repo, canvas_id) if _SAFE_SEGMENT_RE.match(canvas_id or "") else None
        if info is None:
            return {"canvas_id": canvas_id, "status": "missing"}
        return {
            "canvas_id": canvas_id,
            "status": "ok",
            "name": info["name"],
            "description": info["description"],
            "version": info["version"],
            "revision": info["revision"],
            "updated_at": info["updated_at"],
            "counts": dict(info["counts"]),
            "preview": info["preview"],
            "webapp_url": webapp_canvas_url(info["version"], canvas_id),
        }

    def nested_maps_of(self, canvas: dict[str, Any], *, repo: str | None = None) -> dict[str, dict[str, Any]]:
        """`{canvas_id: nested summary}` for every canvas referenced by `canvas` (each target once)."""
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        return {cid: self._nested_summary(active_repo, cid) for cid in nested_refs(canvas)}

    def _validate_nesting(
        self,
        repo: str,
        canvas_id: str,
        canvas: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> None:
        """Reject (422) a document whose new references name a missing canvas or close a reference cycle.

        References kept from `previous` may point at deleted canvases (they read back as missing)."""
        refs = nested_refs(canvas)
        if not refs:
            return
        graph = self._ref_graph(repo)
        added = set(refs) - set(nested_refs(previous))
        missing = sorted(r for r in added if r != canvas_id and r not in graph)
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Nested NodeMap target canvas not found: {', '.join(repr(m) for m in missing)}.",
            )
        cycle = find_cycle(graph, canvas_id, refs)
        if cycle:
            raise HTTPException(
                status_code=422,
                detail=f"Nested NodeMap reference cycle: {' -> '.join(cycle)}.",
            )

    def _commit(
        self,
        repo: str,
        canvas_id: str,
        path: Path,
        env: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> None:
        """Validate the references of `env` and write it, atomically with respect to other writes of `repo`."""
        with self._graph_lock(repo):
            self._validate_nesting(repo, canvas_id, env["canvas"], previous)
            self._write_envelope_atomic(path, env)
            self._drop_info(repo, canvas_id)

    def _with_nested(self, repo: str, record: dict[str, Any]) -> dict[str, Any]:
        return {**record, "nested_maps": self.nested_maps_of(record.get("canvas") or {}, repo=repo)}

    # ------------------------------------------------------------------ Hydration & Field Resolution

    def _hydrate_node(
        self,
        node: dict[str, Any],
        default_version: str,
        default_repo: str,
        struct_cache: dict[tuple[str, str, str], dict[str, Any] | None],
    ) -> None:
        ntype = node.get("type")
        data = node.get("data")
        if not isinstance(data, dict):
            return
        ver = str(node.get("version") or default_version or "v3.0").strip() or "v3.0"
        if not node.get("version"):
            node["version"] = ver
        if not node.get("repo"):
            node["repo"] = default_repo

        hydrated_meta: dict[str, Any] = {}
        old_default = default_title(ntype, data)

        if ntype == "struct" and data.get("name"):
            name = str(data["name"]).strip()
            hint = str(data.get("path") or data.get("hint") or "").strip()
            cache_key = (ver, name, hint)
            if cache_key not in struct_cache:
                try:
                    struct_cache[cache_key] = nodemap_service.get_struct(
                        ver,
                        name,
                        path=hint or None,
                        kind=data.get("kind") or None,
                        line=data.get("line"),
                    )
                except Exception:  # noqa: BLE001
                    struct_cache[cache_key] = None
            st_res = struct_cache[cache_key]
            if isinstance(st_res, dict):
                if st_res.get("file_path") and not data.get("path"):
                    data["path"] = st_res["file_path"]
                if st_res.get("kind") in ("struct", "union"):
                    data["kind"] = st_res["kind"]
                if isinstance(st_res.get("line_s"), int) and not data.get("line"):
                    data["line"] = st_res["line_s"]
                flat = _flatten_struct_items(st_res.get("items") or [])
                hydrated_meta["member_count"] = len(flat)
                struct_cache[(ver, name, "")] = st_res

        elif ntype == "source" and (data.get("path") or data.get("symbol")):
            try:
                src_res = nodemap_service.get_source(
                    ver,
                    path=data.get("path") or None,
                    start=data.get("start"),
                    end=data.get("end"),
                    symbol=data.get("symbol") or None,
                    kind=data.get("symbolKind") or None,
                    context=int(data.get("context") or 0),
                )
            except Exception:  # noqa: BLE001
                src_res = None
            if isinstance(src_res, dict):
                if src_res.get("path"):
                    data["path"] = src_res["path"]
                if data.get("symbol") and not data.get("start"):
                    data["mode"] = "symbol"
                if isinstance(src_res.get("start_line"), int) and data.get("mode") == "range" and not data.get("start"):
                    data["start"] = src_res["start_line"]
                if isinstance(src_res.get("end_line"), int) and data.get("mode") == "range" and not data.get("end"):
                    data["end"] = src_res["end_line"]
                lines = src_res.get("lines")
                if isinstance(lines, list):
                    hydrated_meta["line_count"] = len(lines)

        elif ntype == "symbol" and data.get("name"):
            try:
                sym_res = _symbol_service.get_symbol_detail(ver, str(data["name"]).strip())
            except Exception:  # noqa: BLE001
                sym_res = None
            if isinstance(sym_res, dict):
                defn = sym_res.get("definition") if isinstance(sym_res.get("definition"), dict) else sym_res
                tname = str(defn.get("type_name") or "").lower()
                if data.get("kind") in ("", "symbol") and "func" in tname:
                    data["kind"] = "func"

        elif ntype == "commit" and data.get("commitHash") and not data.get("subject"):
            try:
                c_res = _git_service.get_commit_detail(str(data["commitHash"]))
            except Exception:  # noqa: BLE001
                c_res = None
            if isinstance(c_res, dict) and c_res.get("subject"):
                data["subject"] = str(c_res["subject"]).strip()[:512]

        if not node.get("customTitle") and (not node.get("title") or node.get("title") == old_default):
            node["title"] = default_title(ntype, data)
        estimate_node_dimensions(node, hydrated_meta)

    def _resolve_struct_field_key(
        self,
        node: dict[str, Any] | None,
        field_spec: str | None,
        default_version: str,
        struct_cache: dict[tuple[str, str, str], dict[str, Any] | None],
    ) -> str | None:
        if not field_spec or not isinstance(field_spec, str):
            return None
        cleaned = field_spec.strip()
        if not cleaned:
            return None
        if re.match(r"^\d+:", cleaned) or cleaned.startswith("po:"):
            return cleaned
        if not isinstance(node, dict) or node.get("type") != "struct":
            return cleaned
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        name = str(data.get("name") or "").strip()
        if not name:
            return cleaned
        ver = str(node.get("version") or default_version or "v3.0").strip() or "v3.0"
        hint = str(data.get("path") or data.get("hint") or "").strip()
        st_res = struct_cache.get((ver, name, hint)) or struct_cache.get((ver, name, ""))
        if st_res is None and (ver, name, hint) not in struct_cache:
            try:
                st_res = nodemap_service.get_struct(
                    ver,
                    name,
                    path=hint or None,
                    kind=data.get("kind") or None,
                    line=data.get("line"),
                )
            except Exception:  # noqa: BLE001
                st_res = None
            struct_cache[(ver, name, hint)] = st_res
        if not isinstance(st_res, dict):
            return cleaned
        flat = _flatten_struct_items(st_res.get("items") or [])
        leaf = cleaned.split(".")[-1].strip()
        for it in flat:
            if it.get("name") == cleaned or it.get("name") == leaf:
                return struct_item_key(it)
        return cleaned

    # ------------------------------------------------------------------ Declarative Batch Ops

    def apply_ops(
        self,
        canvas: dict[str, Any],
        ops: list[dict[str, Any]],
        *,
        default_version: str = "v3.0",
        default_repo: str = "linux",
        default_hydrate: bool = True,
        struct_cache: dict[tuple[str, str, str], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Apply an ordered batch of declarative operations to `canvas` in place."""
        scache = struct_cache if struct_cache is not None else {}
        for raw_op in ops or []:
            if not isinstance(raw_op, dict):
                continue
            op_name = str(raw_op.get("op") or raw_op.get("action") or raw_op.get("type") or "").strip().lower()

            if op_name in ("upsert_nodes", "add_nodes", "add_node", "upsert_node", "update_nodes", "update_node") or ("nodes" in raw_op and not op_name):
                hydrate = bool(raw_op.get("hydrate", default_hydrate))
                if isinstance(raw_op.get("nodes"), list):
                    raw_nodes = raw_op["nodes"]
                elif isinstance(raw_op.get("node"), dict):
                    raw_nodes = [raw_op["node"]]
                else:
                    raw_nodes = []
                by_id = {n["id"]: (idx, n) for idx, n in enumerate(canvas["nodes"])}
                for item in raw_nodes:
                    if not isinstance(item, dict):
                        continue
                    nid = item.get("id")
                    if isinstance(nid, str) and nid in by_id:
                        idx, existing = by_id[nid]
                        merged = copy.deepcopy(existing)
                        for k, v in item.items():
                            if k == "data" and isinstance(v, dict) and isinstance(merged.get("data"), dict):
                                merged["data"] = {**merged["data"], **v}
                            elif v is not None:
                                merged[k] = v
                        norm = normalize_node(merged)
                        if norm:
                            if hydrate:
                                self._hydrate_node(norm, default_version, default_repo, scache)
                            canvas["nodes"][idx] = norm
                            by_id[nid] = (idx, norm)
                    elif op_name not in ("update_nodes", "update_node"):
                        norm = normalize_node(item)
                        if norm:
                            if norm["id"] in by_id:
                                idx, _ = by_id[norm["id"]]
                                if hydrate:
                                    self._hydrate_node(norm, default_version, default_repo, scache)
                                canvas["nodes"][idx] = norm
                                by_id[norm["id"]] = (idx, norm)
                            else:
                                if hydrate:
                                    self._hydrate_node(norm, default_version, default_repo, scache)
                                idx = len(canvas["nodes"])
                                canvas["nodes"].append(norm)
                                by_id[norm["id"]] = (idx, norm)

            elif op_name in ("remove_nodes", "remove_node", "delete_nodes"):
                ids = raw_op.get("node_ids") or raw_op.get("ids") or ([raw_op["id"]] if raw_op.get("id") else [])
                if isinstance(ids, list):
                    remove_nodes(
                        canvas,
                        [str(x) for x in ids if x],
                        cascade_group_members=bool(raw_op.get("cascade_group_members", False)),
                    )

            elif op_name in ("upsert_edges", "add_edges", "add_edge", "connect") or ("edges" in raw_op and not op_name):
                if isinstance(raw_op.get("edges"), list):
                    raw_edges = raw_op["edges"]
                elif isinstance(raw_op.get("edge"), dict):
                    raw_edges = [raw_op["edge"]]
                elif raw_op.get("from") and raw_op.get("to"):
                    raw_edges = [raw_op]
                else:
                    raw_edges = []
                nodes_by_id = {n["id"]: n for n in canvas["nodes"]}
                node_id_set = set(nodes_by_id.keys())
                edges_by_id = {e["id"]: (idx, e) for idx, e in enumerate(canvas["edges"])}
                for item in raw_edges:
                    if not isinstance(item, dict):
                        continue
                    candidate = dict(item)
                    if "lock_kind" in candidate and "lockKind" not in candidate:
                        candidate["lockKind"] = candidate.pop("lock_kind")
                    if "from_field" in candidate and "fromField" not in candidate:
                        candidate["fromField"] = candidate.pop("from_field")
                    if "to_field" in candidate and "toField" not in candidate:
                        candidate["toField"] = candidate.pop("to_field")
                    if "from_side" in candidate and "fromSide" not in candidate:
                        candidate["fromSide"] = candidate.pop("from_side")
                    if "to_side" in candidate and "toSide" not in candidate:
                        candidate["toSide"] = candidate.pop("to_side")
                    src_node = nodes_by_id.get(str(candidate.get("from") or ""))
                    dst_node = nodes_by_id.get(str(candidate.get("to") or ""))
                    for end, ref in (("fromField", src_node), ("toField", dst_node)):
                        if candidate.get(end) and ref is not None and ref.get("type") == NESTED_NODE_TYPE:
                            raise HTTPException(
                                status_code=422,
                                detail=f"Edges attach to nested NodeMap node '{ref['id']}' as a whole: '{end}' is not allowed.",
                            )
                    if candidate.get("fromField"):
                        candidate["fromField"] = self._resolve_struct_field_key(
                            src_node, str(candidate["fromField"]), default_version, scache
                        )
                    if candidate.get("toField"):
                        candidate["toField"] = self._resolve_struct_field_key(
                            dst_node, str(candidate["toField"]), default_version, scache
                        )
                    eid = candidate.get("id")
                    if isinstance(eid, str) and eid in edges_by_id:
                        idx, existing = edges_by_id[eid]
                        merged = {**existing, **candidate}
                        norm_e = normalize_edge(merged, node_id_set)
                        if norm_e:
                            canvas["edges"][idx] = norm_e
                            edges_by_id[eid] = (idx, norm_e)
                    else:
                        norm_e = normalize_edge(candidate, node_id_set)
                        if norm_e:
                            if norm_e["id"] in edges_by_id:
                                idx, _ = edges_by_id[norm_e["id"]]
                                canvas["edges"][idx] = norm_e
                                edges_by_id[norm_e["id"]] = (idx, norm_e)
                            else:
                                idx = len(canvas["edges"])
                                canvas["edges"].append(norm_e)
                                edges_by_id[norm_e["id"]] = (idx, norm_e)

            elif op_name in ("remove_edges", "remove_edge", "disconnect"):
                ids = set(str(x) for x in (raw_op.get("edge_ids") or raw_op.get("ids") or ([raw_op["id"]] if raw_op.get("id") else [])) if x)
                from_id = raw_op.get("from")
                to_id = raw_op.get("to")
                canvas["edges"] = [
                    e
                    for e in canvas["edges"]
                    if e["id"] not in ids
                    and not (from_id and to_id and e.get("from") == from_id and e.get("to") == to_id)
                ]

            elif op_name in ("upsert_groups", "add_group", "upsert_group", "assign_group"):
                raw_groups = raw_op.get("groups") if isinstance(raw_op.get("groups"), list) else [raw_op]
                by_id = {n["id"]: (idx, n) for idx, n in enumerate(canvas["nodes"])}
                for gspec in raw_groups:
                    if not isinstance(gspec, dict) or (gspec is raw_op and "groups" in raw_op):
                        continue
                    gid = gspec.get("group_id") or gspec.get("groupId") or (gspec.get("id") if is_valid_id(gspec.get("id")) else new_id("grp"))
                    if op_name != "assign_group" or gid not in by_id:
                        title = str(gspec.get("title") or gspec.get("customTitle") or "Group").strip()
                        gnode_raw = {
                            "id": gid,
                            "type": "group",
                            "x": gspec.get("x", 80),
                            "y": gspec.get("y", 80),
                            "w": gspec.get("w", 520),
                            "h": gspec.get("h", 360),
                            "title": title,
                            "customTitle": str(gspec.get("customTitle") or title),
                            "color": gspec.get("color", ""),
                            "bgColor": gspec.get("bgColor", ""),
                            "groupId": gspec.get("parentGroupId"),
                            "layerId": gspec.get("layerId", ""),
                            "version": gspec.get("version") or default_version,
                            "repo": gspec.get("repo") or default_repo,
                            "data": {"subCanvas": gspec["subCanvas"]} if isinstance(gspec.get("subCanvas"), dict) else {},
                        }
                        if gid in by_id:
                            idx, existing = by_id[gid]
                            merged = {**existing, **{k: v for k, v in gnode_raw.items() if v is not None}}
                            gnode = normalize_node(merged)
                            if gnode:
                                canvas["nodes"][idx] = gnode
                                by_id[gid] = (idx, gnode)
                        else:
                            gnode = normalize_node(gnode_raw)
                            if gnode:
                                idx = len(canvas["nodes"])
                                canvas["nodes"].append(gnode)
                                by_id[gid] = (idx, gnode)

                    member_ids = gspec.get("members") or gspec.get("member_ids") or gspec.get("node_ids") or []
                    if isinstance(member_ids, list):
                        for mid in member_ids:
                            if isinstance(mid, str) and mid in by_id and mid != gid:
                                by_id[mid][1]["groupId"] = gid
                    members = [n for n in canvas["nodes"] if n.get("groupId") == gid]
                    fit_flag = gspec.get("fit_to_members") if "fit_to_members" in gspec else gspec.get("auto_fit", True)
                    if members and gid in by_id and fit_flag:
                        grp_ref = by_id[gid][1]
                        mb = bounds_of(members)
                        if mb:
                            grp_ref["x"] = mb["x"] - GROUP_LAYOUT_PAD["x"]
                            grp_ref["y"] = mb["y"] - GROUP_LAYOUT_PAD["top"]
                            fit_group_to_members(grp_ref, members, GROUP_LAYOUT_PAD)

            elif op_name in ("annotate", "add_note"):
                by_id = {n["id"]: n for n in canvas["nodes"]}
                # Optional layer upserts
                if isinstance(raw_op.get("layers"), list):
                    existing_layers = {l["id"]: l for l in (canvas["settings"].get("layers") or [])}
                    for lspec in raw_op["layers"]:
                        if isinstance(lspec, dict):
                            lid = lspec.get("id") if is_valid_id(lspec.get("id")) else new_id("lyr")
                            existing_layers[lid] = {**existing_layers.get(lid, {}), **lspec, "id": lid}
                    canvas["settings"]["layers"] = normalize_layers(list(existing_layers.values()))

                explicit_target_id = raw_op.get("target_id") or raw_op.get("node_id")
                target_id = explicit_target_id or raw_op.get("id")
                target = by_id.get(str(target_id)) if target_id else None
                note_text = raw_op.get("note") if "note" in raw_op else raw_op.get("text")
                has_note_text = note_text is not None
                if target is not None and not (explicit_target_id and has_note_text):
                    if "customTitle" in raw_op or "title" in raw_op:
                        t_str = str(raw_op.get("customTitle") or raw_op.get("title") or "")[:512]
                        target["customTitle"] = t_str
                    if "color" in raw_op:
                        target["color"] = normalize_color(raw_op.get("color"))
                    if "bgColor" in raw_op:
                        target["bgColor"] = normalize_color(raw_op.get("bgColor"))
                    if "layerId" in raw_op and (raw_op["layerId"] == "" or is_valid_id(raw_op["layerId"])):
                        target["layerId"] = raw_op["layerId"]
                    if isinstance(raw_op.get("locks"), list):
                        if raw_op.get("replace_locks", False):
                            target["locks"] = normalize_node_locks(raw_op["locks"])
                        else:
                            target["locks"] = normalize_node_locks([*(target.get("locks") or []), *raw_op["locks"]])
                    if isinstance(raw_op.get("field_locks") or raw_op.get("fieldLocks"), dict) and target.get("type") == "struct":
                        fl_in = raw_op.get("field_locks") or raw_op.get("fieldLocks")
                        resolved_fl: dict[str, Any] = dict(target["data"].get("fieldLocks") or {})
                        for fk, fval in fl_in.items():
                            rkey = self._resolve_struct_field_key(target, str(fk), default_version, scache) or str(fk)
                            if fval is None:
                                resolved_fl.pop(rkey, None)
                            else:
                                resolved_fl[rkey] = fval
                        target["data"]["fieldLocks"] = normalize_field_locks(resolved_fl)
                    if has_note_text and target.get("type") == "note":
                        target["data"]["text"] = str(note_text or "")[:20000]

                # Or create a standalone/linked note node when `note` or `text` is given
                if has_note_text and (target is None or target.get("type") != "note") and raw_op.get("create_note", True):
                    candidate_note_id = raw_op.get("note_id") or (raw_op.get("id") if explicit_target_id or target is None else None)
                    note_id = candidate_note_id if is_valid_id(candidate_note_id) and candidate_note_id not in by_id else new_id("note")
                    note_node = create_node(
                        "note",
                        {
                            "id": note_id,
                            "x": raw_op.get("x", (target["x"] if target else 80)),
                            "y": raw_op.get("y", (max(20, target["y"] - 160) if target else 80)),
                            "title": raw_op.get("title") or "Note",
                            "customTitle": raw_op.get("customTitle") or raw_op.get("title") or "",
                            "color": raw_op.get("color") or "yellow",
                            "groupId": raw_op.get("groupId") or (target.get("groupId") if target else None),
                            "layerId": raw_op.get("layerId") or "",
                            "version": default_version,
                            "repo": default_repo,
                            "data": {"text": str(note_text or ""), "markdown": True},
                        },
                    )
                    if note_node:
                        estimate_node_dimensions(note_node)
                        canvas["nodes"].append(note_node)
                        if target is not None:
                            edge = create_edge(
                                {
                                    "from": note_node["id"],
                                    "to": target["id"],
                                    "label": "annotates",
                                    "style": "dotted",
                                    "color": "yellow",
                                },
                                {n["id"] for n in canvas["nodes"]},
                            )
                            if edge:
                                canvas["edges"].append(edge)

            elif op_name in ("upsert_tour", "upsert_tours", "add_tour"):
                raw_tours = raw_op.get("tours") if isinstance(raw_op.get("tours"), list) else [raw_op.get("tour") or raw_op]
                by_id = {n["id"]: n for n in canvas["nodes"]}
                existing_tours = {t["id"]: (idx, t) for idx, t in enumerate(canvas.get("tours") or [])}
                for tspec in raw_tours:
                    if not isinstance(tspec, dict):
                        continue
                    tid = tspec.get("id") if is_valid_id(tspec.get("id")) else new_id("tour")
                    raw_steps = tspec.get("steps") if isinstance(tspec.get("steps"), list) else []
                    built_steps: list[dict[str, Any]] = []
                    for s in raw_steps:
                        if not isinstance(s, dict):
                            continue
                        raw_nids = s.get("nodeIds") or s.get("node_ids") or ([s["nodeId"]] if s.get("nodeId") else [])
                        nids = [str(x) for x in raw_nids if is_valid_id(str(x))]
                        raw_eids = s.get("edgeIds") or s.get("edge_ids") or ([s["edgeId"]] if s.get("edgeId") else [])
                        eids = [str(x) for x in raw_eids if is_valid_id(str(x))]
                        vp = s.get("viewport")
                        if not isinstance(vp, dict) or s.get("auto_viewport"):
                            step_nodes = [by_id[nid] for nid in nids if nid in by_id]
                            vp = fit_viewport(bounds_of(step_nodes or canvas["nodes"]))
                        built_steps.append(
                            {
                                "id": s.get("id") if is_valid_id(s.get("id")) else new_id("step"),
                                "title": s.get("title") or "Step",
                                "caption": s.get("caption") or "",
                                "nodeIds": nids,
                                "edgeIds": eids,
                                "viewport": vp,
                            }
                        )
                    norm_tour_list = normalize_tours(
                        [{"id": tid, "title": tspec.get("title") or "Walkthrough", "steps": built_steps}]
                    )
                    if norm_tour_list:
                        norm_tour = norm_tour_list[0]
                        if tid in existing_tours:
                            idx, _ = existing_tours[tid]
                            canvas["tours"][idx] = norm_tour
                        else:
                            idx = len(canvas["tours"])
                            canvas["tours"].append(norm_tour)
                            existing_tours[tid] = (idx, norm_tour)

            elif op_name in ("remove_tour", "remove_tours", "delete_tour"):
                tids = set(str(x) for x in (raw_op.get("tour_ids") or raw_op.get("ids") or ([raw_op["id"]] if raw_op.get("id") else [])) if x)
                canvas["tours"] = [t for t in (canvas.get("tours") or []) if t.get("id") not in tids]

            elif op_name == "expand":
                self.apply_expansion(
                    canvas,
                    raw_op,
                    default_version=default_version,
                    default_repo=default_repo,
                    struct_cache=scache,
                )

        normalized = normalize_canvas(canvas, strict=False)
        if normalized is not None:
            canvas.clear()
            canvas.update(normalized)
        return canvas

    # ------------------------------------------------------------------ Graph Expansion Modes

    def apply_expansion(
        self,
        canvas: dict[str, Any],
        spec: dict[str, Any],
        *,
        default_version: str = "v3.0",
        default_repo: str = "linux",
        struct_cache: dict[tuple[str, str, str], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Execute a server-side expansion (`struct_types`, `callgraph`, `callpath`, `kconfig`, `commit`, `impact_map`)."""
        scache = struct_cache if struct_cache is not None else {}
        mode = str(spec.get("mode") or "").strip().lower()
        ver = safe_rev(str(spec.get("version") or default_version or "v3.0").strip() or "v3.0", "version")
        repo = str(spec.get("repo") or default_repo or get_active_repo_name()).strip() or "linux"
        limit = max(1, min(100, int(spec.get("limit") or 16)))
        by_id = {n["id"]: n for n in canvas["nodes"]}
        source_node = by_id.get(str(spec.get("node_id") or "")) if spec.get("node_id") else None

        if mode == "struct_types":
            struct_name = str(
                spec.get("struct")
                or spec.get("name")
                or ((source_node.get("data") or {}).get("name") if source_node else "")
                or ""
            ).strip()
            if not struct_name:
                raise HTTPException(status_code=400, detail="struct_types expansion requires `struct` name or struct `node_id`.")
            path_hint = str(
                spec.get("path")
                or ((source_node.get("data") or {}).get("path") if source_node else "")
                or ((source_node.get("data") or {}).get("hint") if source_node else "")
                or ""
            ).strip()
            if source_node is None:
                source_node = next(
                    (
                        n
                        for n in canvas["nodes"]
                        if n.get("type") == "struct" and (n.get("data") or {}).get("name") == struct_name
                    ),
                    None,
                )
            if source_node is None:
                source_node = create_node(
                    "struct",
                    {
                        "version": ver,
                        "repo": repo,
                        "color": "purple",
                        "data": {"name": struct_name, "kind": "struct", "path": path_hint},
                    },
                )
                if source_node:
                    self._hydrate_node(source_node, ver, repo, scache)
                    canvas["nodes"].append(source_node)
                    by_id[source_node["id"]] = source_node

            st_res = scache.get((ver, struct_name, path_hint)) or scache.get((ver, struct_name, ""))
            if st_res is None:
                st_res = nodemap_service.get_struct(ver, struct_name, path=path_hint or None)
                scache[(ver, struct_name, path_hint)] = st_res
            flat = _flatten_struct_items(st_res.get("items") or [])
            added = 0
            existing_structs = {
                (n.get("type"), (n.get("data") or {}).get("name")): n
                for n in canvas["nodes"]
                if n.get("type") in ("struct", "symbol")
            }
            for item in flat:
                if added >= limit:
                    break
                type_text = (
                    f"{item.get('tag', 'struct')} {str(item.get('type_name') or '').removeprefix('struct ').removeprefix('union ')}"
                    if item.get("kind") == "aggregate" and item.get("type_name")
                    else str(item.get("type") or "")
                )
                refs = extract_type_refs(type_text)
                for ref in refs:
                    if added >= limit:
                        break
                    rname = ref["name"]
                    rkind = ref["kind"]
                    if rname == struct_name:
                        continue
                    target_ntype = "struct" if rkind in ("struct", "union") else "symbol"
                    target_node = existing_structs.get((target_ntype, rname))
                    if target_node is None:
                        ndata = (
                            {"name": rname, "kind": rkind, "hint": st_res.get("file_path") or path_hint}
                            if target_ntype == "struct"
                            else {"name": rname, "kind": rkind}
                        )
                        target_node = create_node(
                            target_ntype,
                            {
                                "version": ver,
                                "repo": repo,
                                "color": "cyan" if ref["isPointer"] else "purple",
                                "groupId": source_node.get("groupId") if source_node else None,
                                "data": ndata,
                            },
                        )
                        if target_node:
                            self._hydrate_node(target_node, ver, repo, scache)
                            canvas["nodes"].append(target_node)
                            by_id[target_node["id"]] = target_node
                            existing_structs[(target_ntype, rname)] = target_node
                            added += 1
                    if source_node and target_node and source_node["id"] != target_node["id"]:
                        f_key = struct_item_key(item)
                        preset = "points to" if ref["isPointer"] else ("embeds" if rkind in ("struct", "union") else "")
                        edge = create_edge(
                            {
                                "from": source_node["id"],
                                "to": target_node["id"],
                                "fromField": f_key,
                                "preset": preset,
                                "label": ref["edgeLabel"],
                            },
                            set(by_id.keys()),
                        )
                        if edge and not any(
                            e["from"] == edge["from"] and e["to"] == edge["to"] and e.get("fromField") == f_key
                            for e in canvas["edges"]
                        ):
                            canvas["edges"].append(edge)

        elif mode == "callgraph":
            fn_name = str(
                spec.get("function")
                or spec.get("name")
                or ((source_node.get("data") or {}).get("name") if source_node else "")
                or ""
            ).strip().removesuffix("()")
            if not fn_name or not is_valid_identifier(fn_name):
                raise HTTPException(status_code=400, detail="callgraph expansion requires a valid `function` name or symbol `node_id`.")
            direction = str(spec.get("direction") or "both").strip().lower()
            if source_node is None:
                source_node = next(
                    (
                        n
                        for n in canvas["nodes"]
                        if n.get("type") == "symbol" and str((n.get("data") or {}).get("name") or "").removesuffix("()") == fn_name
                    ),
                    None,
                )
            if source_node is None:
                source_node = create_node(
                    "symbol",
                    {
                        "version": ver,
                        "repo": repo,
                        "color": "blue",
                        "data": {"name": fn_name, "kind": "func"},
                    },
                )
                if source_node:
                    canvas["nodes"].append(source_node)
                    by_id[source_node["id"]] = source_node

            cg_res = _callgraph_service.get_function_callgraph(ver, fn_name)
            sym_by_name = {
                str((n.get("data") or {}).get("name") or "").removesuffix("()"): n
                for n in canvas["nodes"]
                if n.get("type") == "symbol"
            }

            def ensure_fn_node(name: str, color: str = "blue") -> dict[str, Any] | None:
                clean_fn = name.strip().removesuffix("()")
                if not clean_fn:
                    return None
                hit = sym_by_name.get(clean_fn)
                if hit:
                    return hit
                node = create_node(
                    "symbol",
                    {
                        "version": ver,
                        "repo": repo,
                        "color": color,
                        "groupId": source_node.get("groupId") if source_node else None,
                        "data": {"name": clean_fn, "kind": "func"},
                    },
                )
                if node:
                    canvas["nodes"].append(node)
                    by_id[node["id"]] = node
                    sym_by_name[clean_fn] = node
                return node

            if direction in ("both", "callers", "in"):
                for caller in (cg_res.get("callers") or [])[:limit]:
                    cname = str(caller.get("name") or "").strip()
                    cnode = ensure_fn_node(cname, "cyan")
                    if cnode and source_node and cnode["id"] != source_node["id"]:
                        cs = (
                            {"path": caller["file_path"], "line": int(caller.get("line_no") or 1)}
                            if caller.get("file_path")
                            else None
                        )
                        edge = create_edge(
                            {"from": cnode["id"], "to": source_node["id"], "preset": "calls", "callSite": cs},
                            set(by_id.keys()),
                        )
                        if edge and not any(e["from"] == edge["from"] and e["to"] == edge["to"] for e in canvas["edges"]):
                            canvas["edges"].append(edge)

            if direction in ("both", "callees", "out"):
                for callee in (cg_res.get("callees") or [])[:limit]:
                    cname = str(callee.get("name") or "").strip()
                    cnode = ensure_fn_node(cname, "blue")
                    if cnode and source_node and cnode["id"] != source_node["id"]:
                        cs = (
                            {"path": callee["file_path"], "line": int(callee.get("line_no") or 1)}
                            if callee.get("file_path")
                            else None
                        )
                        edge = create_edge(
                            {"from": source_node["id"], "to": cnode["id"], "preset": "calls", "callSite": cs},
                            set(by_id.keys()),
                        )
                        if edge and not any(e["from"] == edge["from"] and e["to"] == edge["to"] for e in canvas["edges"]):
                            canvas["edges"].append(edge)

        elif mode == "callpath":
            src_fn = str(spec.get("from") or spec.get("from_func") or "").strip().removesuffix("()")
            dst_fn = str(spec.get("to") or spec.get("to_func") or "").strip().removesuffix("()")
            if not src_fn and source_node:
                src_fn = str((source_node.get("data") or {}).get("name") or "").strip().removesuffix("()")
            if not src_fn or not dst_fn or not is_valid_identifier(src_fn) or not is_valid_identifier(dst_fn):
                raise HTTPException(status_code=400, detail="callpath expansion requires valid `from` and `to` function names.")
            max_depth = max(1, min(10, int(spec.get("max_depth") or 6)))
            k_paths = max(1, min(20, int(spec.get("k") or 5)))
            st = callpath_state(ver, src_fn, dst_fn, max_depth=max_depth, k=k_paths)
            # Wait briefly if background job just started in tests/CLI
            deadline = time.monotonic() + 2.0
            while st.get("state") == "building" and time.monotonic() < deadline:
                time.sleep(0.05)
                st = callpath_state(ver, src_fn, dst_fn, max_depth=max_depth, k=k_paths)
            res = st.get("result") if isinstance(st.get("result"), dict) else {}
            paths = res.get("paths") if isinstance(res.get("paths"), list) else []
            if not paths:
                paths = [[src_fn, dst_fn]]

            sym_by_name = {
                str((n.get("data") or {}).get("name") or "").removesuffix("()"): n
                for n in canvas["nodes"]
                if n.get("type") == "symbol"
            }
            for chain in paths[:k_paths]:
                if not isinstance(chain, list):
                    continue
                prev_node: dict[str, Any] | None = None
                for step_item in chain:
                    fn = (
                        str(step_item.get("name") or step_item.get("function") or "")
                        if isinstance(step_item, dict)
                        else str(step_item or "")
                    ).strip().removesuffix("()")
                    if not fn:
                        continue
                    node = sym_by_name.get(fn)
                    if node is None:
                        node = create_node(
                            "symbol",
                            {
                                "version": ver,
                                "repo": repo,
                                "color": "green" if fn in (src_fn, dst_fn) else "blue",
                                "data": {"name": fn, "kind": "func"},
                            },
                        )
                        if node:
                            canvas["nodes"].append(node)
                            by_id[node["id"]] = node
                            sym_by_name[fn] = node
                    if prev_node and node and prev_node["id"] != node["id"]:
                        edge = create_edge(
                            {"from": prev_node["id"], "to": node["id"], "preset": "calls"},
                            set(by_id.keys()),
                        )
                        if edge and not any(e["from"] == edge["from"] and e["to"] == edge["to"] for e in canvas["edges"]):
                            canvas["edges"].append(edge)
                    prev_node = node

        elif mode == "kconfig":
            sym = str(
                spec.get("symbol")
                or spec.get("name")
                or ((source_node.get("data") or {}).get("symbol") if source_node else "")
                or ""
            ).strip().removeprefix("CONFIG_")
            if not sym or not is_valid_identifier(sym):
                raise HTTPException(status_code=400, detail="kconfig expansion requires a valid Kconfig `symbol` or `node_id`.")
            arch = str(spec.get("arch") or DEFAULT_ARCH).strip() or DEFAULT_ARCH
            if source_node is None:
                source_node = next(
                    (
                        n
                        for n in canvas["nodes"]
                        if n.get("type") == "kconfig" and (n.get("data") or {}).get("symbol") == sym
                    ),
                    None,
                )
            if source_node is None:
                source_node = create_node(
                    "kconfig",
                    {
                        "version": ver,
                        "repo": repo,
                        "color": "yellow",
                        "data": {"symbol": sym, "arch": arch},
                    },
                )
                if source_node:
                    canvas["nodes"].append(source_node)
                    by_id[source_node["id"]] = source_node

            try:
                detail = _kconfig_service.get_symbol_detail(ver, sym, arch)
            except Exception:  # noqa: BLE001
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            kc_by_sym = {
                str((n.get("data") or {}).get("symbol") or ""): n
                for n in canvas["nodes"]
                if n.get("type") == "kconfig"
            }
            related_syms: list[tuple[str, str]] = []
            for dep in detail.get("depends_on") or detail.get("dependencies") or []:
                for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(dep)):
                    clean_tok = tok.removeprefix("CONFIG_")
                    if clean_tok not in ("y", "m", "n") and clean_tok != sym:
                        related_syms.append((clean_tok, "depends on"))
            for sel in detail.get("selects") or []:
                s_name = str(sel.get("symbol") if isinstance(sel, dict) else sel).strip().removeprefix("CONFIG_")
                if s_name and s_name != sym:
                    related_syms.append((s_name, "depends on"))

            added = 0
            for rel_sym, preset in related_syms:
                if added >= limit:
                    break
                target_node = kc_by_sym.get(rel_sym)
                if target_node is None:
                    target_node = create_node(
                        "kconfig",
                        {
                            "version": ver,
                            "repo": repo,
                            "color": "yellow",
                            "groupId": source_node.get("groupId") if source_node else None,
                            "data": {"symbol": rel_sym, "arch": arch},
                        },
                    )
                    if target_node:
                        canvas["nodes"].append(target_node)
                        by_id[target_node["id"]] = target_node
                        kc_by_sym[rel_sym] = target_node
                        added += 1
                if source_node and target_node and source_node["id"] != target_node["id"]:
                    edge = create_edge(
                        {"from": source_node["id"], "to": target_node["id"], "preset": preset},
                        set(by_id.keys()),
                    )
                    if edge and not any(e["from"] == edge["from"] and e["to"] == edge["to"] for e in canvas["edges"]):
                        canvas["edges"].append(edge)

        elif mode == "commit":
            commit_hash = str(
                spec.get("commit_hash")
                or spec.get("commitHash")
                or ((source_node.get("data") or {}).get("commitHash") if source_node else "")
                or ""
            ).strip()
            if not commit_hash:
                raise HTTPException(status_code=400, detail="commit expansion requires `commit_hash` or commit `node_id`.")
            safe_rev(commit_hash, "commit_hash")
            detail = _git_service.get_commit_detail(commit_hash)
            full_sha = str(detail.get("commit_hash") or commit_hash).lower()
            subject = str(detail.get("subject") or "").strip()
            if source_node is None:
                source_node = create_node(
                    "commit",
                    {
                        "version": ver,
                        "repo": repo,
                        "color": "orange",
                        "data": {"commitHash": full_sha, "subject": subject},
                    },
                )
                if source_node:
                    canvas["nodes"].append(source_node)
                    by_id[source_node["id"]] = source_node
            for f in (detail.get("files") or [])[:limit]:
                fpath = str(f.get("path") or f.get("new_path") or f.get("old_path") or "").strip() if isinstance(f, dict) else str(f).strip()
                if not fpath:
                    continue
                dnode = create_node(
                    "diff",
                    {
                        "version": ver,
                        "repo": repo,
                        "color": "orange",
                        "groupId": source_node.get("groupId") if source_node else None,
                        "data": {"mode": "commit", "commitHash": full_sha, "path": fpath},
                    },
                )
                if dnode and source_node:
                    canvas["nodes"].append(dnode)
                    by_id[dnode["id"]] = dnode
                    edge = create_edge(
                        {"from": source_node["id"], "to": dnode["id"], "label": "touches", "color": "orange"},
                        set(by_id.keys()),
                    )
                    if edge:
                        canvas["edges"].append(edge)

        elif mode == "impact_map":
            raw_files = list(spec.get("files") if isinstance(spec.get("files"), list) else [])
            fallback_path = str(spec.get("path") or spec.get("target") or "").strip()
            if fallback_path and not raw_files and not (spec.get("commit_hash") or spec.get("commitHash")):
                raw_files = [fallback_path]
            built = self._build_server_impact_map(
                version=ver,
                repo=repo,
                title=str(spec.get("title") or "").strip(),
                commit_hash=str(spec.get("commit_hash") or spec.get("commitHash") or "").strip(),
                files=raw_files,
                diff_text=str(spec.get("diff_text") or spec.get("diffText") or ""),
                scache=scache,
            )
            existing_ids = {n["id"] for n in canvas["nodes"]}
            for n in built["nodes"]:
                if n["id"] not in existing_ids:
                    canvas["nodes"].append(n)
                    existing_ids.add(n["id"])
            existing_eids = {e["id"] for e in canvas["edges"]}
            for e in built["edges"]:
                if e["id"] not in existing_eids:
                    canvas["edges"].append(e)
                    existing_eids.add(e["id"])
            for t in built.get("tours") or []:
                canvas["tours"].append(t)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported expand mode {mode!r}; expected struct_types, callgraph, callpath, kconfig, commit, or impact_map.",
            )

        normalized = normalize_canvas(canvas, strict=False)
        if normalized is not None:
            canvas.clear()
            canvas.update(normalized)
        return canvas

    def _build_server_impact_map(
        self,
        *,
        version: str,
        repo: str,
        title: str,
        commit_hash: str,
        files: list[Any],
        diff_text: str,
        scache: dict[tuple[str, str, str], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Server-side port of `impact_map.js:generateImpactMap` + `buildImpactMap`."""
        effective_files = list(files or [])
        clean_commit = commit_hash.lower() if re.match(r"^[0-9a-f]{4,64}$", commit_hash, re.IGNORECASE) else ""
        if clean_commit and not effective_files:
            try:
                diff_res = _git_service.get_commit_diff(clean_commit)
                if isinstance(diff_res.get("files"), list):
                    effective_files = diff_res["files"]
            except Exception:  # noqa: BLE001
                effective_files = []

        norm_files: list[dict[str, Any]] = []
        structs: list[str] = []
        seen_structs: set[str] = set()
        functions: list[str] = []
        seen_funcs: set[str] = set()
        struct_files: dict[str, str] = {}
        func_files: dict[str, str] = {}

        def add_struct(sname: str, fpath: str) -> None:
            if sname and sname not in _STANDARD_C_TYPES and sname not in seen_structs and len(structs) < 10:
                seen_structs.add(sname)
                structs.append(sname)
                struct_files.setdefault(sname, fpath)

        def add_func(fname: str, fpath: str) -> None:
            if fname and fname not in _NON_FUNC_IDENTS and fname not in seen_funcs and len(functions) < 12:
                seen_funcs.add(fname)
                functions.append(fname)
                func_files.setdefault(fname, fpath)

        for raw_f in effective_files:
            if isinstance(raw_f, str):
                fpath = raw_f.strip()
                if fpath:
                    norm_files.append({"path": fpath, "oldPath": fpath, "status": "M", "additions": 0, "deletions": 0, "hunks": []})
                continue
            if not isinstance(raw_f, dict):
                continue
            fpath = str(raw_f.get("path") or raw_f.get("new_path") or raw_f.get("old_path") or "").strip()
            if not fpath:
                continue
            hunks = raw_f.get("hunks") if isinstance(raw_f.get("hunks"), list) else []
            norm_files.append(
                {
                    "path": fpath,
                    "oldPath": str(raw_f.get("oldPath") or raw_f.get("old_path") or fpath).strip(),
                    "status": str(raw_f.get("status") or raw_f.get("change_type") or "M").strip(),
                    "additions": int(raw_f.get("additions") or 0),
                    "deletions": int(raw_f.get("deletions") or 0),
                    "hunks": hunks,
                }
            )
            for h in hunks:
                if not isinstance(h, dict):
                    continue
                heading = str(h.get("heading") or h.get("header") or "").strip()
                for m in _STRUCT_SCAN_RE.finditer(heading):
                    add_struct(m.group(1), fpath)
                hm = _HEADING_FN_RE.search(heading)
                if hm:
                    add_func(hm.group(1), fpath)
                for ln in h.get("lines") or []:
                    body = str(ln[3] if isinstance(ln, list) and len(ln) > 3 else (ln[1:] if isinstance(ln, str) and ln else ""))
                    for m in _STRUCT_SCAN_RE.finditer(body):
                        add_struct(m.group(1), fpath)
                    sm = _SIG_LINE_RE.match(body.rstrip())
                    if sm:
                        add_func(sm.group(1), fpath)

        if diff_text:
            cur_path = norm_files[0]["path"] if norm_files else "patch.c"
            for raw_ln in diff_text.splitlines():
                if raw_ln.startswith("+++ b/"):
                    cur_path = raw_ln[6:].strip()
                    if not any(f["path"] == cur_path for f in norm_files):
                        norm_files.append({"path": cur_path, "oldPath": cur_path, "status": "M", "additions": 0, "deletions": 0, "hunks": []})
                elif raw_ln.startswith(("@@", "+", "-")) and not raw_ln.startswith(("+++", "---")):
                    for m in _STRUCT_SCAN_RE.finditer(raw_ln):
                        add_struct(m.group(1), cur_path)
                    sm = _SIG_LINE_RE.match(raw_ln[1:].rstrip())
                    if sm:
                        add_func(sm.group(1), cur_path)

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        col_y = 180
        pad_top = 48
        pad_bottom = 28
        gap_y = 24

        clean_title = title or (f"Commit Impact: {clean_commit[:12]}" if clean_commit else "Patch / Commit Impact Map")
        total_add = sum(int(f.get("additions") or 0) for f in norm_files)
        total_del = sum(int(f.get("deletions") or 0) for f in norm_files)
        note_lines = [
            f"### {clean_title}",
            f"- **Commit:** `{clean_commit[:12]}`" if clean_commit else "- **Scope:** Working patch / diff",
            f"- **Changed:** {len(norm_files)} file(s) (+{total_add} / -{total_del})",
            f"- **Touched:** {len(structs)} struct(s), {len(functions)} function(s)",
        ]
        note_node = create_node(
            "note",
            {
                "x": 80, "y": 20, "w": 620, "h": 130, "version": version, "repo": repo,
                "color": "yellow", "customTitle": clean_title,
                "data": {"text": "\n".join(note_lines), "markdown": True},
            },
        )
        if note_node:
            nodes.append(note_node)

        g_changes = create_node("group", {"x": 80, "y": col_y, "w": 620, "h": 360, "version": version, "repo": repo, "color": "orange", "customTitle": "Patch / Commit Changes"})
        g_touched = create_node("group", {"x": 780, "y": col_y, "w": 380, "h": 360, "version": version, "repo": repo, "color": "blue", "customTitle": "Touched Data & Symbols"})
        g_blast = create_node("group", {"x": 1220, "y": col_y, "w": 420, "h": 360, "version": version, "repo": repo, "color": "green", "customTitle": "Blast Radius & Ownership"})
        assert g_changes and g_touched and g_blast

        change_nodes: list[dict[str, Any]] = []
        change_by_path: dict[str, dict[str, Any]] = {}
        cur_y = col_y + pad_top
        for f in norm_files[:16]:
            c_node = create_node(
                "diff" if clean_commit else "source",
                {
                    "x": 100, "y": cur_y, "w": 580, "h": 240 if clean_commit else 220,
                    "version": version, "repo": repo, "groupId": g_changes["id"], "color": "orange",
                    "data": (
                        {"mode": "commit", "commitHash": clean_commit, "path": f["path"]}
                        if clean_commit
                        else {"mode": "file", "path": f["path"]}
                    ),
                },
            )
            if c_node:
                change_nodes.append(c_node)
                change_by_path[f["path"]] = c_node
                cur_y += c_node["h"] + gap_y
        g_changes["h"] = max(240, cur_y - col_y - gap_y + pad_bottom)

        col2_nodes: list[dict[str, Any]] = []
        col2_sym_by_name: dict[str, dict[str, Any]] = {}
        mod_edges: list[dict[str, Any]] = []
        cur_y = col_y + pad_top
        for s_name in structs:
            hint_path = struct_files.get(s_name, "")
            s_node = create_node(
                "struct",
                {
                    "x": 800, "y": cur_y, "w": 340, "h": 220,
                    "version": version, "repo": repo, "groupId": g_touched["id"], "color": "purple",
                    "data": {"name": s_name, "kind": "struct", "hint": hint_path},
                },
            )
            if s_node:
                self._hydrate_node(s_node, version, repo, scache)
                col2_nodes.append(s_node)
                cur_y += s_node["h"] + gap_y
                src_change = change_by_path.get(hint_path) or (change_nodes[0] if change_nodes else None)
                if src_change:
                    e = create_edge({"from": src_change["id"], "to": s_node["id"], "label": "modifies", "color": "orange"})
                    if e:
                        edges.append(e)
                        mod_edges.append(e)

        for fn_name in functions:
            hint_path = func_files.get(fn_name, "")
            fn_node = create_node(
                "symbol",
                {
                    "x": 800, "y": cur_y, "w": 280, "h": 110,
                    "version": version, "repo": repo, "groupId": g_touched["id"], "color": "blue",
                    "data": {"name": fn_name, "kind": "func"},
                },
            )
            if fn_node:
                col2_nodes.append(fn_node)
                col2_sym_by_name[fn_name] = fn_node
                cur_y += fn_node["h"] + gap_y
                src_change = change_by_path.get(hint_path) or (change_nodes[0] if change_nodes else None)
                if src_change:
                    e = create_edge({"from": src_change["id"], "to": fn_node["id"], "label": "modifies", "color": "orange"})
                    if e:
                        edges.append(e)
                        mod_edges.append(e)
        g_touched["h"] = max(240, cur_y - col_y - gap_y + pad_bottom)

        col3_nodes: list[dict[str, Any]] = []
        col3_edges: list[dict[str, Any]] = []
        cur_y = col_y + pad_top
        for fn_name in functions[:5]:
            callee_node = col2_sym_by_name.get(fn_name)
            if not callee_node:
                continue
            try:
                cg = _callgraph_service.get_function_callgraph(version, fn_name)
                callers = cg.get("callers") or []
            except Exception:  # noqa: BLE001
                callers = []
            for raw_c in callers[:4]:
                cname = str(raw_c.get("name") or "").strip() if isinstance(raw_c, dict) else str(raw_c).strip()
                if not cname or cname == fn_name:
                    continue
                cnode = create_node(
                    "symbol",
                    {
                        "x": 1240, "y": cur_y, "w": 280, "h": 110,
                        "version": version, "repo": repo, "groupId": g_blast["id"], "color": "blue",
                        "data": {"name": cname, "kind": "func"},
                    },
                )
                if cnode:
                    col3_nodes.append(cnode)
                    cur_y += cnode["h"] + gap_y
                    e = create_edge({"from": cnode["id"], "to": callee_node["id"], "preset": "calls"})
                    if e:
                        edges.append(e)
                        col3_edges.append(e)

        touched_paths = [f["path"] for f in norm_files if f.get("path")]
        if touched_paths:
            try:
                m_res = _maintainer_service.match_maintainers(version, paths=touched_paths)
                sections = m_res.get("sections") or m_res.get("subsystems") or []
            except Exception:  # noqa: BLE001
                sections = []
            for sec in sections[:6]:
                sname = str(sec.get("name") or sec.get("title") or "").strip() if isinstance(sec, dict) else str(sec).strip()
                if not sname:
                    continue
                mnode = create_node(
                    "maintainer",
                    {
                        "x": 1240, "y": cur_y, "w": 380, "h": 240,
                        "version": version, "repo": repo, "groupId": g_blast["id"], "color": "green",
                        "data": {"title": sname, "query": sname},
                    },
                )
                if mnode:
                    col3_nodes.append(mnode)
                    cur_y += mnode["h"] + gap_y
                    e = create_edge({"from": g_changes["id"], "to": mnode["id"], "label": "maintains", "color": "green", "style": "dashed"})
                    if e:
                        edges.append(e)
                        col3_edges.append(e)
        g_blast["h"] = max(240, cur_y - col_y - gap_y + pad_bottom)

        nodes.extend([g_changes, g_touched, g_blast, *change_nodes, *col2_nodes, *col3_nodes])
        tours = normalize_tours(
            [
                {
                    "id": new_id("tour"),
                    "title": "Impact Walkthrough",
                    "steps": [
                        {
                            "id": new_id("step"),
                            "title": "1. Changes",
                            "caption": f"{len(norm_files)} modified file(s) included in this patch or commit.",
                            "nodeIds": [g_changes["id"], *[n["id"] for n in change_nodes]],
                            "edgeIds": [],
                            "viewport": {"x": 20, "y": 0, "zoom": 0.85},
                        },
                        {
                            "id": new_id("step"),
                            "title": "2. Touched Data & Symbols",
                            "caption": f"{len(structs)} struct(s) and {len(functions)} function(s) impacted directly.",
                            "nodeIds": [g_touched["id"], *[n["id"] for n in col2_nodes]],
                            "edgeIds": [e["id"] for e in mod_edges],
                            "viewport": {"x": -480, "y": 0, "zoom": 0.85},
                        },
                        {
                            "id": new_id("step"),
                            "title": "3. Blast Radius & Ownership",
                            "caption": f"{len(col3_nodes)} blast-radius caller(s) and maintainer section(s).",
                            "nodeIds": [g_blast["id"], *[n["id"] for n in col3_nodes]],
                            "edgeIds": [e["id"] for e in col3_edges],
                            "viewport": {"x": -860, "y": 0, "zoom": 0.85},
                        },
                    ],
                }
            ]
        )
        return normalize_canvas({**empty_canvas(), "nodes": nodes, "edges": edges, "tours": tours}, strict=False) or empty_canvas()

    # ------------------------------------------------------------------ Stateless Preview & CRUD

    def build_canvas_in_memory(
        self,
        payload: dict[str, Any],
        *,
        default_version: str = "v3.0",
        default_repo: str = "linux",
    ) -> dict[str, Any]:
        """Build, hydrate, expand, and lay out a canonical canvas in memory with zero disk writes."""
        ver = str(payload.get("version") or default_version or "v3.0").strip() or "v3.0"
        repo = str(payload.get("repo") or default_repo or get_active_repo_name()).strip() or "linux"
        hydrate = bool(payload.get("hydrate", True))
        scache: dict[tuple[str, str, str], dict[str, Any] | None] = {}

        raw_canvas = payload.get("canvas") if isinstance(payload.get("canvas"), dict) else {}
        base = normalize_canvas(
            {
                "schema": CANVAS_SCHEMA,
                "version": CANVAS_VERSION,
                "nodes": raw_canvas.get("nodes") if isinstance(raw_canvas.get("nodes"), list) else [],
                "edges": raw_canvas.get("edges") if isinstance(raw_canvas.get("edges"), list) else [],
                "viewport": payload.get("viewport") or raw_canvas.get("viewport") or {"x": 0, "y": 0, "zoom": 1},
                "settings": payload.get("settings") or raw_canvas.get("settings") or {},
                "tours": payload.get("tours") or raw_canvas.get("tours") or [],
            },
            strict=False,
        ) or empty_canvas()

        if hydrate and base["nodes"]:
            for n in base["nodes"]:
                self._hydrate_node(n, ver, repo, scache)

        initial_ops: list[dict[str, Any]] = []
        if isinstance(payload.get("nodes"), list) and payload["nodes"]:
            initial_ops.append({"op": "upsert_nodes", "nodes": payload["nodes"], "hydrate": hydrate})
        if isinstance(payload.get("groups"), list) and payload["groups"]:
            initial_ops.append({"op": "upsert_groups", "groups": payload["groups"]})
        if isinstance(payload.get("edges"), list) and payload["edges"]:
            initial_ops.append({"op": "upsert_edges", "edges": payload["edges"]})
        if isinstance(payload.get("ops"), list):
            initial_ops.extend(payload["ops"])
        if isinstance(payload.get("expansions"), list):
            for exp in payload["expansions"]:
                if isinstance(exp, dict):
                    initial_ops.append({"op": "expand", **exp})
        elif isinstance(payload.get("expand"), dict):
            initial_ops.append({"op": "expand", **payload["expand"]})

        if initial_ops:
            self.apply_ops(
                base,
                initial_ops,
                default_version=ver,
                default_repo=repo,
                default_hydrate=hydrate,
                struct_cache=scache,
            )

        auto_layout = payload.get("auto_layout")
        raw_layout = payload.get("layout")
        if isinstance(raw_layout, dict):
            auto_layout = raw_layout.get("mode") or auto_layout or "dag"
        elif isinstance(raw_layout, str) and raw_layout.strip():
            auto_layout = raw_layout.strip()
        if auto_layout:
            lmode = "force" if str(auto_layout).strip().lower() == "force" else "dag"
            apply_canvas_layout(base, mode=lmode)
            if base["nodes"]:
                base["viewport"] = fit_viewport(bounds_of(base["nodes"]))

        return normalize_canvas(base, strict=False) or empty_canvas()

    def preview_canvas(
        self,
        payload: dict[str, Any],
        *,
        default_version: str = "v3.0",
        default_repo: str = "linux",
    ) -> dict[str, Any]:
        """Stateless read-only canvas builder and optional exporter (`json`, `mermaid`, `dot`)."""
        ver = str(payload.get("version") or default_version or "v3.0").strip() or "v3.0"
        repo = str(payload.get("repo") or default_repo or get_active_repo_name()).strip() or "linux"
        canvas = self.build_canvas_in_memory(payload, default_version=ver, default_repo=repo)
        canonical = serialize_canvas(canvas)
        out: dict[str, Any] = {
            "repo": repo,
            "version": ver,
            "name": str(payload.get("name") or "Preview Canvas").strip()[:256] or "Preview Canvas",
            "node_count": len(canonical["nodes"]),
            "edge_count": len(canonical["edges"]),
            "tour_count": len(canonical["tours"]),
            "canvas": canonical,
        }
        exp_fmt = str(payload.get("export_format") or "").strip().lower()
        if exp_fmt in ("json", "mermaid", "dot"):
            exported = export_canvas(canonical, exp_fmt, direction=str(payload.get("direction") or "LR"))
            out["export_format"] = exported["format"]
            out["export"] = exported["content"]
            out["export_content"] = exported["content"]
        return out

    def list_canvases(
        self,
        *,
        repo: str | None = None,
        q: str | None = "",
        limit: int = 50,
        offset: int = 0,
        insertable_into: str | None = None,
    ) -> dict[str, Any]:
        """Saved canvas summaries, newest first. With `insertable_into` (a canvas id) each item also carries
        `insertable`: false for that canvas and its ancestors, whose reference would close a cycle."""
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        repo_dir = self._repo_dir(active_repo)
        blocked: set[str] | None = None
        if insertable_into:
            host = validate_safe_segment(insertable_into, "insertable_into")
            blocked = {host, *ancestors_of(self._ref_graph(active_repo), host)}
        items: list[dict[str, Any]] = []
        needle = str(q or "").strip().lower()
        if repo_dir.is_dir():
            for fp in repo_dir.glob("*.json"):
                if fp.name.startswith("."):
                    continue
                env = self._read_envelope(fp)
                if not env:
                    continue
                summary = self._summary_of(env)
                if needle:
                    hay = f"{summary['id']} {summary['name']} {summary['description']}".lower()
                    if needle not in hay:
                        continue
                if blocked is not None:
                    summary["insertable"] = summary["id"] not in blocked
                items.append(summary)
        items.sort(key=lambda r: (-int(r.get("updated_at") or 0), str(r.get("name") or "")))
        total = len(items)
        sliced = items[offset : offset + limit]
        return {
            "repo": active_repo,
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": (offset + len(sliced)) < total,
            "items": sliced,
            "canvases": sliced,
        }

    def get_canvas(self, canvas_id: str, *, repo: str | None = None) -> dict[str, Any]:
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        lock = self._lock_for(active_repo, safe_id)
        with lock:
            path = self._canvas_path(active_repo, safe_id)
            env = self._read_envelope(path)
            if env is None:
                raise HTTPException(status_code=404, detail=f"NodeMap canvas '{safe_id}' not found.")
            record = {
                **self._summary_of(env),
                "canvas": serialize_canvas(env["canvas"]),
            }
        return self._with_nested(active_repo, record)

    def export_saved_canvas(
        self,
        canvas_id: str,
        *,
        fmt: str = "json",
        direction: str = "LR",
        repo: str | None = None,
    ) -> dict[str, Any]:
        rec = self.get_canvas(canvas_id, repo=repo)
        exported = export_canvas(rec["canvas"], fmt, direction=direction)
        return {
            "repo": rec["repo"],
            "id": rec["id"],
            "name": rec["name"],
            "version": rec["version"],
            "revision": rec["revision"],
            **exported,
        }

    def create_canvas(
        self,
        payload: dict[str, Any] | None = None,
        *,
        canvas_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        version: str | None = None,
        canvas: dict[str, Any] | None = None,
        repo: str | None = None,
        default_version: str = "v3.0",
        created_by: str = "api",
    ) -> dict[str, Any]:
        eff_payload = dict(payload or {})
        if canvas_id is not None:
            eff_payload["id"] = canvas_id
        if name is not None:
            eff_payload["name"] = name
        if description is not None:
            eff_payload["description"] = description
        if version is not None:
            eff_payload["version"] = version
        if canvas is not None:
            eff_payload["canvas"] = canvas
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        raw_id = eff_payload.get("id") if "id" in eff_payload and eff_payload.get("id") is not None else eff_payload.get("canvas_id")
        cid = validate_safe_segment(str(raw_id), "canvas_id") if raw_id is not None else new_id("nm")
        ver = safe_rev(str(eff_payload.get("version") or default_version or "v3.0").strip() or "v3.0", "version")
        cname = str(eff_payload.get("name") or "Untitled Canvas").strip()[:256] or "Untitled Canvas"
        cdesc = str(eff_payload.get("description") or "").strip()[:2000]

        lock = self._lock_for(active_repo, cid)
        with lock:
            path = self._canvas_path(active_repo, cid)
            if path.exists():
                raise HTTPException(status_code=409, detail=f"NodeMap canvas '{cid}' already exists.")
            built_canvas = self.build_canvas_in_memory(eff_payload, default_version=ver, default_repo=active_repo)
            now_ms = int(time.time() * 1000)
            env = self._build_envelope(
                canvas_id=cid,
                repo=active_repo,
                name=cname,
                description=cdesc,
                version=ver,
                revision=1,
                created_at=now_ms,
                updated_at=now_ms,
                created_by=created_by,
                canvas=built_canvas,
            )
            self._commit(active_repo, cid, path, env, None)
            return self._with_nested(active_repo, env)

    def replace_canvas(
        self,
        canvas_id: str,
        payload: dict[str, Any],
        *,
        repo: str | None = None,
        default_version: str = "v3.0",
        updated_by: str = "api",
    ) -> dict[str, Any]:
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        lock = self._lock_for(active_repo, safe_id)
        with lock:
            path = self._canvas_path(active_repo, safe_id)
            existing = self._read_envelope(path)
            expected_rev = payload.get("expected_revision")
            if existing is None:
                if expected_rev is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Canvas '{safe_id}' does not exist (expected_revision={expected_rev}).",
                    )
                cur_rev = 0
                created_at = int(time.time() * 1000)
                created_by = updated_by
            else:
                cur_rev = int(existing.get("revision", 1))
                if expected_rev is not None and int(expected_rev) != cur_rev:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Revision conflict on canvas '{safe_id}': expected {expected_rev}, current is {cur_rev}.",
                    )
                created_at = int(existing.get("created_at") or int(time.time() * 1000))
                created_by = str(existing.get("created_by") or updated_by)

            ver = safe_rev(
                str(payload.get("version") or (existing or {}).get("version") or default_version or "v3.0").strip() or "v3.0",
                "version",
            )
            name = str(payload.get("name") or (existing or {}).get("name") or "Untitled Canvas").strip()[:256] or "Untitled Canvas"
            description = (
                str(payload["description"]).strip()[:2000]
                if "description" in payload and payload["description"] is not None
                else str((existing or {}).get("description") or "")
            )
            canvas = self.build_canvas_in_memory(payload, default_version=ver, default_repo=active_repo)
            now_ms = int(time.time() * 1000)
            env = self._build_envelope(
                canvas_id=safe_id,
                repo=active_repo,
                name=name,
                description=description,
                version=ver,
                revision=cur_rev + 1,
                created_at=created_at,
                updated_at=now_ms,
                created_by=created_by,
                canvas=canvas,
            )
            self._commit(active_repo, safe_id, path, env, (existing or {}).get("canvas"))
            return self._with_nested(active_repo, env)

    def delete_canvas(self, canvas_id: str, *, repo: str | None = None) -> dict[str, Any]:
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        lock = self._lock_for(active_repo, safe_id)
        with lock:
            path = self._canvas_path(active_repo, safe_id)
            if not path.is_file():
                raise HTTPException(status_code=404, detail=f"NodeMap canvas '{safe_id}' not found.")
            with self._graph_lock(active_repo):
                path.unlink()
                self._drop_info(active_repo, safe_id)
                referenced_by = sorted(c for c, refs in self._ref_graph(active_repo).items() if safe_id in refs)
            return {"repo": active_repo, "id": safe_id, "deleted": True, "referenced_by": referenced_by}

    def mutate_canvas(
        self,
        canvas_id: str,
        payload: dict[str, Any],
        *,
        repo: str | None = None,
    ) -> dict[str, Any]:
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        lock = self._lock_for(active_repo, safe_id)
        with lock:
            path = self._canvas_path(active_repo, safe_id)
            existing = self._read_envelope(path)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"NodeMap canvas '{safe_id}' not found.")
            cur_rev = int(existing.get("revision", 1))
            expected_rev = payload.get("expected_revision")
            if expected_rev is not None and int(expected_rev) != cur_rev:
                raise HTTPException(
                    status_code=409,
                    detail=f"Revision conflict on canvas '{safe_id}': expected {expected_rev}, current is {cur_rev}.",
                )

            ver = str(payload.get("version") or existing.get("version") or "v3.0").strip() or "v3.0"
            canvas = copy.deepcopy(existing["canvas"])
            ops = list(payload.get("ops") or [])
            hydrate = bool(payload.get("hydrate", True))
            if isinstance(payload.get("upsert_nodes"), list):
                ops.append({"op": "upsert_nodes", "nodes": payload["upsert_nodes"], "hydrate": hydrate})
            if isinstance(payload.get("remove_nodes"), list):
                ops.append({"op": "remove_nodes", "node_ids": payload["remove_nodes"]})
            if isinstance(payload.get("upsert_groups"), list):
                ops.append({"op": "upsert_groups", "groups": payload["upsert_groups"]})
            if isinstance(payload.get("upsert_edges"), list):
                ops.append({"op": "upsert_edges", "edges": payload["upsert_edges"]})
            if isinstance(payload.get("remove_edges"), list):
                ops.append({"op": "remove_edges", "edge_ids": payload["remove_edges"]})
            if isinstance(payload.get("annotate"), dict):
                ops.append({"op": "annotate", **payload["annotate"]})
            elif isinstance(payload.get("annotate"), list):
                for ann in payload["annotate"]:
                    if isinstance(ann, dict):
                        ops.append({"op": "annotate", **ann})
            if isinstance(payload.get("upsert_tour"), dict):
                ops.append({"op": "upsert_tour", "tour": payload["upsert_tour"]})
            elif isinstance(payload.get("upsert_tours"), list):
                ops.append({"op": "upsert_tours", "tours": payload["upsert_tours"]})

            self.apply_ops(
                canvas,
                ops,
                default_version=ver,
                default_repo=active_repo,
                default_hydrate=hydrate,
            )

            auto_layout = payload.get("auto_layout")
            if auto_layout:
                lmode = "force" if str(auto_layout).strip().lower() == "force" else "dag"
                apply_canvas_layout(canvas, mode=lmode)

            name = str(payload.get("name") or existing.get("name") or "Untitled Canvas").strip()[:256]
            description = (
                str(payload["description"]).strip()[:2000]
                if "description" in payload and payload["description"] is not None
                else str(existing.get("description") or "")
            )
            now_ms = int(time.time() * 1000)
            env = self._build_envelope(
                canvas_id=safe_id,
                repo=active_repo,
                name=name,
                description=description,
                version=ver,
                revision=cur_rev + 1,
                created_at=int(existing.get("created_at") or now_ms),
                updated_at=now_ms,
                created_by=str(existing.get("created_by") or "api"),
                canvas=canvas,
            )
            self._commit(active_repo, safe_id, path, env, existing["canvas"])
            return self._with_nested(active_repo, env)

    def expand_canvas(
        self,
        canvas_id: str,
        payload: dict[str, Any],
        *,
        repo: str | None = None,
    ) -> dict[str, Any]:
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        lock = self._lock_for(active_repo, safe_id)
        with lock:
            path = self._canvas_path(active_repo, safe_id)
            existing = self._read_envelope(path)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"NodeMap canvas '{safe_id}' not found.")
            cur_rev = int(existing.get("revision", 1))
            expected_rev = payload.get("expected_revision")
            if expected_rev is not None and int(expected_rev) != cur_rev:
                raise HTTPException(
                    status_code=409,
                    detail=f"Revision conflict on canvas '{safe_id}': expected {expected_rev}, current is {cur_rev}.",
                )
            ver = str(payload.get("version") or existing.get("version") or "v3.0").strip() or "v3.0"
            before_nids = {n["id"] for n in (existing["canvas"].get("nodes") or [])}
            before_eids = {e["id"] for e in (existing["canvas"].get("edges") or [])}
            canvas = copy.deepcopy(existing["canvas"])
            self.apply_expansion(canvas, payload, default_version=ver, default_repo=active_repo)

            auto_layout = payload.get("auto_layout", "dag")
            if auto_layout:
                lmode = "force" if str(auto_layout).strip().lower() == "force" else "dag"
                apply_canvas_layout(canvas, mode=lmode)

            now_ms = int(time.time() * 1000)
            env = self._build_envelope(
                canvas_id=safe_id,
                repo=active_repo,
                name=str(existing.get("name") or "Untitled Canvas"),
                description=str(existing.get("description") or ""),
                version=ver,
                revision=cur_rev + 1,
                created_at=int(existing.get("created_at") or now_ms),
                updated_at=now_ms,
                created_by=str(existing.get("created_by") or "api"),
                canvas=canvas,
            )
            self._commit(active_repo, safe_id, path, env, existing["canvas"])
            return {
                **self._with_nested(active_repo, env),
                "added_node_ids": [n["id"] for n in env["canvas"]["nodes"] if n["id"] not in before_nids],
                "added_edge_ids": [e["id"] for e in env["canvas"]["edges"] if e["id"] not in before_eids],
            }

    def layout_canvas(
        self,
        canvas_id: str,
        payload: dict[str, Any],
        *,
        repo: str | None = None,
    ) -> dict[str, Any]:
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        lock = self._lock_for(active_repo, safe_id)
        with lock:
            path = self._canvas_path(active_repo, safe_id)
            existing = self._read_envelope(path)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"NodeMap canvas '{safe_id}' not found.")
            cur_rev = int(existing.get("revision", 1))
            expected_rev = payload.get("expected_revision")
            if expected_rev is not None and int(expected_rev) != cur_rev:
                raise HTTPException(
                    status_code=409,
                    detail=f"Revision conflict on canvas '{safe_id}': expected {expected_rev}, current is {cur_rev}.",
                )

            mode = str(payload.get("mode") or "dag").strip().lower()
            if mode not in ("dag", "force"):
                raise HTTPException(status_code=400, detail="Layout mode must be 'dag' or 'force'.")
            group_id = str(payload["group_id"]).strip() if payload.get("group_id") else None
            node_ids = [str(x) for x in payload["node_ids"] if x] if isinstance(payload.get("node_ids"), list) else None
            options = payload.get("options") if isinstance(payload.get("options"), dict) else None

            canvas = copy.deepcopy(existing["canvas"])
            apply_canvas_layout(canvas, mode=mode, group_id=group_id, node_ids=node_ids, options=options)

            now_ms = int(time.time() * 1000)
            env = self._build_envelope(
                canvas_id=safe_id,
                repo=active_repo,
                name=str(existing.get("name") or "Untitled Canvas"),
                description=str(existing.get("description") or ""),
                version=str(existing.get("version") or "v3.0"),
                revision=cur_rev + 1,
                created_at=int(existing.get("created_at") or now_ms),
                updated_at=now_ms,
                created_by=str(existing.get("created_by") or "api"),
                canvas=canvas,
            )
            self._commit(active_repo, safe_id, path, env, existing["canvas"])
            return self._with_nested(active_repo, env)

    def canvas_hierarchy(
        self,
        canvas_id: str,
        *,
        depth: int = HIERARCHY_DEPTH_DEFAULT,
        repo: str | None = None,
    ) -> dict[str, Any]:
        """Reference tree below a canvas, its direct parents and every ancestor, without node/edge payloads.

        The tree descends `depth` levels (`1..HIERARCHY_DEPTH_MAX`) and holds at most
        `HIERARCHY_MAX_ENTRIES` entries; a reused target appears under each parent referencing it.
        `invalid_targets` lists the canvases that cannot be inserted into this one (itself and its ancestors).
        """
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        if not 1 <= int(depth) <= HIERARCHY_DEPTH_MAX:
            raise HTTPException(status_code=400, detail=f"depth must be between 1 and {HIERARCHY_DEPTH_MAX}.")
        root_info = self._canvas_info(active_repo, safe_id)
        if root_info is None:
            raise HTTPException(status_code=404, detail=f"NodeMap canvas '{safe_id}' not found.")
        graph = self._ref_graph(active_repo)
        budget = {"left": HIERARCHY_MAX_ENTRIES, "truncated": False}

        def entry(cid: str, levels: int, path: tuple[str, ...]) -> dict[str, Any]:
            budget["left"] -= 1
            info = self._canvas_info(active_repo, cid) if _SAFE_SEGMENT_RE.match(cid or "") else None
            if info is None:
                return {"canvas_id": cid, "status": "missing", "children": []}
            out: dict[str, Any] = {
                "canvas_id": cid,
                "status": "cycle" if cid in path else "ok",
                "name": info["name"],
                "version": info["version"],
                "revision": info["revision"],
                "counts": dict(info["counts"]),
                "children": [],
            }
            if cid in path:
                return out
            kids = [k for k in info["nested"] if k["canvas_id"]]
            if levels <= 0:
                out["children_truncated"] = bool(kids)
                return out
            for k in kids:
                if budget["left"] <= 0:
                    budget["truncated"] = True
                    out["children_truncated"] = True
                    break
                child = entry(k["canvas_id"], levels - 1, (*path, cid))
                out["children"].append({"node_id": k["node_id"], "title": k["title"], **child})
            return out

        tree = entry(safe_id, int(depth), ())
        parents = []
        for cid in sorted(c for c, refs in graph.items() if safe_id in refs and c != safe_id):
            pinfo = self._canvas_info(active_repo, cid)
            if pinfo is not None:
                parents.append({
                    "canvas_id": cid,
                    "name": pinfo["name"],
                    "node_ids": [k["node_id"] for k in pinfo["nested"] if k["canvas_id"] == safe_id],
                })
        ancestors = sorted(ancestors_of(graph, safe_id))
        return {
            "repo": active_repo,
            "id": safe_id,
            "name": root_info["name"],
            "depth": int(depth),
            "tree": tree,
            "parents": parents,
            "ancestors": ancestors,
            "invalid_targets": sorted({safe_id, *ancestors}),
            "entries": HIERARCHY_MAX_ENTRIES - budget["left"],
            "truncated": budget["truncated"],
        }

    def _locked_parent(self, repo: str, canvas_id: str, payload: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        """(path, envelope) of an existing canvas whose revision matches `payload["expected_revision"]` (404 / 409)."""
        path = self._canvas_path(repo, canvas_id)
        existing = self._read_envelope(path)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"NodeMap canvas '{canvas_id}' not found.")
        cur_rev = int(existing.get("revision", 1))
        expected_rev = payload.get("expected_revision")
        if expected_rev is not None and int(expected_rev) != cur_rev:
            raise HTTPException(
                status_code=409,
                detail=f"Revision conflict on canvas '{canvas_id}': expected {expected_rev}, current is {cur_rev}.",
            )
        return path, existing

    def _new_child_envelope(
        self,
        repo: str,
        child_id: str,
        spec: dict[str, Any],
        parent: dict[str, Any],
        canvas: dict[str, Any],
        created_by: str,
    ) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        ver = safe_rev(str(spec.get("version") or parent.get("version") or "v3.0").strip() or "v3.0", "version")
        return self._build_envelope(
            canvas_id=child_id,
            repo=repo,
            name=str(spec.get("name") or "Nested NodeMap").strip()[:256] or "Nested NodeMap",
            description=str(spec.get("description") or "").strip()[:2000],
            version=ver,
            revision=1,
            created_at=now_ms,
            updated_at=now_ms,
            created_by=created_by,
            canvas=canvas,
        )

    def _write_parent_and_child(
        self,
        repo: str,
        parent_id: str,
        parent_path: Path,
        parent_env: dict[str, Any],
        previous: dict[str, Any],
        child: tuple[str, Path, dict[str, Any]] | None,
    ) -> None:
        """Write a new child canvas then its parent under the graph lock; the child is removed if the parent fails."""
        with self._graph_lock(repo):
            if child is not None:
                cid, cpath, cenv = child
                self._commit(repo, cid, cpath, cenv, None)
            try:
                self._commit(repo, parent_id, parent_path, parent_env, previous)
            except BaseException:
                if child is not None:
                    with contextlib.suppress(OSError):
                        child[1].unlink()
                    self._drop_info(repo, child[0])
                raise

    def _child_id(self, repo: str, payload: dict[str, Any]) -> tuple[str, Path]:
        raw = payload.get("new_canvas_id")
        cid = validate_safe_segment(str(raw), "new_canvas_id") if raw else new_id("nm")
        path = self._canvas_path(repo, cid)
        if path.exists():
            raise HTTPException(status_code=409, detail=f"NodeMap canvas '{cid}' already exists.")
        return cid, path

    def attach_nested(
        self,
        canvas_id: str,
        payload: dict[str, Any],
        *,
        repo: str | None = None,
        created_by: str = "api",
    ) -> dict[str, Any]:
        """Add a `nodemap` node to a canvas referencing `target_id`, or a new empty canvas when it is omitted.

        `payload`: `target_id`, `new_canvas_id`, `name`, `description`, `version` (new child), `node`
        (`id`, `x`, `y`, `w`, `h`, `customTitle`, `color`, `bgColor`, `groupId`, `layerId`),
        `expected_revision` (parent). Missing targets and cycles are 422; the write is atomic.
        """
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        target_raw = payload.get("target_id")
        target = validate_safe_segment(str(target_raw), "target_id") if target_raw else None
        child_id, child_path = (target, self._canvas_path(active_repo, target)) if target else self._child_id(active_repo, payload)
        with self._canvas_locks_sorted(active_repo, [safe_id, child_id]), self._graph_lock(active_repo):
            path, existing = self._locked_parent(active_repo, safe_id, payload)
            child: tuple[str, Path, dict[str, Any]] | None = None
            if target is None:
                if child_path.exists():
                    raise HTTPException(status_code=409, detail=f"NodeMap canvas '{child_id}' already exists.")
                child_env = self._new_child_envelope(active_repo, child_id, payload, existing, empty_canvas(), created_by)
                child = (child_id, child_path, child_env)
                title = child_env["name"]
            else:
                tinfo = self._canvas_info(active_repo, target)
                title = tinfo["name"] if tinfo else ""
            spec = payload.get("node") if isinstance(payload.get("node"), dict) else {}
            node_raw = {k: spec[k] for k in ("id", "x", "y", "w", "h", "customTitle", "color", "bgColor", "groupId", "layerId") if k in spec}
            node = normalize_node({
                **node_raw,
                "type": NESTED_NODE_TYPE,
                "id": node_raw.get("id") if is_valid_id(node_raw.get("id")) else new_id("n"),
                "title": title,
                "version": existing.get("version") or "v3.0",
                "repo": active_repo,
                "data": {"canvasId": child_id},
            })
            assert node is not None
            canvas = copy.deepcopy(existing["canvas"])
            if any(n["id"] == node["id"] for n in canvas["nodes"]):
                raise HTTPException(status_code=409, detail=f"Node '{node['id']}' already exists on canvas '{safe_id}'.")
            canvas["nodes"].append(node)
            canvas = normalize_canvas(canvas, strict=False) or canvas
            now_ms = int(time.time() * 1000)
            env = self._build_envelope(
                canvas_id=safe_id,
                repo=active_repo,
                name=str(existing.get("name") or "Untitled Canvas"),
                description=str(existing.get("description") or ""),
                version=str(existing.get("version") or "v3.0"),
                revision=int(existing.get("revision", 1)) + 1,
                created_at=int(existing.get("created_at") or now_ms),
                updated_at=now_ms,
                created_by=str(existing.get("created_by") or "api"),
                canvas=canvas,
            )
            self._write_parent_and_child(active_repo, safe_id, path, env, existing["canvas"], child)
            return {
                "repo": active_repo,
                "id": safe_id,
                "node_id": node["id"],
                "target_id": child_id,
                "created": child is not None,
                "parent": self._with_nested(active_repo, env),
            }

    def group_into_nested(
        self,
        canvas_id: str,
        payload: dict[str, Any],
        *,
        repo: str | None = None,
        created_by: str = "api",
    ) -> dict[str, Any]:
        """Move `payload["node_ids"]` (plus descendants of selected groups) into a new child canvas.

        The child receives the moved nodes and the edges between them; the parent gets a `nodemap` node
        at the selection's place, and each edge crossing the selection boundary is rewired to that node.
        Tour steps targeting moved nodes target the nested node. `new_canvas_id`, `name`, `description`,
        `version`, `node` (`id`, `customTitle`, `color`, `bgColor`, `layerId`) and `expected_revision` are
        optional. Both canvases are written atomically (422 for an empty or unknown selection).
        """
        active_repo = validate_safe_segment(repo or get_active_repo_name(), "repo")
        safe_id = validate_safe_segment(canvas_id, "canvas_id")
        raw_ids = payload.get("node_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(status_code=422, detail="node_ids must be a non-empty list of node ids.")
        child_id, child_path = self._child_id(active_repo, payload)
        with self._canvas_locks_sorted(active_repo, [safe_id, child_id]), self._graph_lock(active_repo):
            path, existing = self._locked_parent(active_repo, safe_id, payload)
            if child_path.exists():
                raise HTTPException(status_code=409, detail=f"NodeMap canvas '{child_id}' already exists.")
            canvas = existing["canvas"]
            known = {n["id"] for n in canvas["nodes"]}
            unknown = sorted({str(x) for x in raw_ids} - known)
            if unknown:
                raise HTTPException(status_code=422, detail=f"Unknown node ids on canvas '{safe_id}': {', '.join(unknown[:20])}.")
            spec = payload.get("node") if isinstance(payload.get("node"), dict) else {}
            nid = spec.get("id") if is_valid_id(spec.get("id")) and spec.get("id") not in known else new_id("n")
            split = split_for_group(canvas, [str(x) for x in raw_ids], nid)

            child_canvas = normalize_canvas({
                **empty_canvas(),
                "nodes": split["child_nodes"],
                "edges": split["child_edges"],
                "settings": copy.deepcopy(canvas.get("settings") or {}),
                "viewport": fit_viewport(bounds_of(split["child_nodes"])),
            }) or empty_canvas()
            child_env = self._new_child_envelope(active_repo, child_id, payload, existing, child_canvas, created_by)

            frame = nested_node_frame(split["bounds"])
            node = normalize_node({
                **{k: spec[k] for k in ("customTitle", "color", "bgColor", "layerId") if k in spec},
                **frame,
                "id": nid,
                "type": NESTED_NODE_TYPE,
                "title": child_env["name"],
                "groupId": split["group_id"],
                "version": existing.get("version") or "v3.0",
                "repo": active_repo,
                "data": {"canvasId": child_id},
            })
            assert node is not None
            moved = set(split["moved_ids"])
            parent_edges = split["parent_edges"]
            parent_canvas = normalize_canvas({
                **canvas,
                "nodes": [*split["parent_nodes"], node],
                "edges": parent_edges,
                "tours": remap_tours(canvas.get("tours") or [], moved, nid, {e["id"] for e in parent_edges}),
            }) or empty_canvas()
            now_ms = int(time.time() * 1000)
            env = self._build_envelope(
                canvas_id=safe_id,
                repo=active_repo,
                name=str(existing.get("name") or "Untitled Canvas"),
                description=str(existing.get("description") or ""),
                version=str(existing.get("version") or "v3.0"),
                revision=int(existing.get("revision", 1)) + 1,
                created_at=int(existing.get("created_at") or now_ms),
                updated_at=now_ms,
                created_by=str(existing.get("created_by") or "api"),
                canvas=parent_canvas,
            )
            self._write_parent_and_child(active_repo, safe_id, path, env, canvas, (child_id, child_path, child_env))
            return {
                "repo": active_repo,
                "id": safe_id,
                "node_id": nid,
                "target_id": child_id,
                "moved_node_ids": split["moved_ids"],
                "moved_edge_ids": [e["id"] for e in split["child_edges"]],
                "rewired_edge_ids": split["rewired_edge_ids"],
                "child": self._summary_of(child_env),
                "parent": self._with_nested(active_repo, env),
            }


nodemap_canvas_service = NodeMapCanvasService()
