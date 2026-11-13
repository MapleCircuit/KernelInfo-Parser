"""webapp/backend/services/filesystem_service.py - Directory & Source File Inspection."""
from __future__ import annotations
from collections.abc import Iterator
import heapq
import logging
import os
from webapp.backend.config import RepoScopedCache, get_repo_dir
from typing import Any
from fastapi import HTTPException
from db_engine.spec import AnyOf, Join, like, ne
from webapp.backend.database.pool import get_reader
from webapp.backend.database.helpers import (
    safe_decode,
    format_stat_label,
    get_version_info,
)
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.tree_resolver import tree_resolver, git_tree_resolver
logger = logging.getLogger(__name__)

# Bounded: every key below carries a request parameter (a header, a path), so an unbounded cache
# would grow with each distinct value a caller sends. Treemaps are whole subtrees, so fewer are kept.
_INCLUDE_CACHE: dict[tuple[str, str, str | None], dict[str, Any]] = RepoScopedCache(maxsize=4096)
_FILE_TOKENS_CACHE: dict[int, list[list[int]]] = RepoScopedCache(maxsize=512)
_TREEMAP_CACHE: dict[tuple[str, int, str], dict[str, Any]] = RepoScopedCache(maxsize=64)
# Only image types are served raw (documentation figures); everything else goes through get_file()
RAW_MEDIA_TYPES = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}


class FilesystemService:
    """Service handling directory tree traversal, file content extraction, and AST coordinate mapping."""

    def get_tree(self, version_name: str, path: str = "", reader: Any = None) -> dict[str, Any]:
        """Retrieve directory hierarchy and file entries for the given version (on `reader` when given)."""
        if reader is None:
            with get_reader() as rd:
                return self._get_tree(rd, version_name, path)
        return self._get_tree(reader, version_name, path)

    def _get_tree(self, reader: Any, version_name: str, path: str) -> dict[str, Any]:
        norm_path = path.strip().strip("/")
        vid, vname = get_version_info(reader, version_name)
        if vid is None:
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

        prefix = f"{norm_path}/" if norm_path else ""
        # Wildcards in the requested path are escaped by like(): only the trailing % is a wildcard
        name_filter = like(prefix, "prefix") if prefix else None

        # Path C: the Git tree is authoritative for every revision (tags, branches, arbitrary commits)
        git_dir = git_tree_resolver.list_dir(vname, norm_path)
        is_checkpoint = False
        if git_dir is None:
            # Git unavailable: fall back to milestone checkpoint in m_bridge_checkpoint
            is_checkpoint = reader.checkpoint_tree_exists(vid)

        if git_dir is not None:
            rows = self._git_tree_rows(reader, git_dir, prefix)
        elif is_checkpoint:
            rows = [
                {"fname": r["fname"], "fid": r["blob_id"], "ftype": r["ftype"], "s_stat": "0", "e_stat": "0"}
                for r in reader.checkpoint_tree_files(vid, name=name_filter, with_ftype=True, order_by_name=True)
            ]
        else:
            tree_map = tree_resolver.resolve_tree(vid, reader)
            if not tree_map:
                rows = []
            else:
                fnids = list(tree_map.keys())
                rows = []
                chunk_size = 5000
                for i in range(0, len(fnids), chunk_size):
                    chunk = fnids[i:i + chunk_size]
                    name_where: dict[str, Any] = {"fnid": chunk}
                    if name_filter is not None:
                        name_where["fname"] = name_filter
                    c_rows = reader.select_rows("m_file_name", ["fnid", "fname"], name_where, order_by=["fname"], as_dict=True)
                    blob_ids = list({tree_map[r["fnid"]] for r in c_rows})
                    b_types: dict[int, int] = {}
                    if blob_ids:
                        b_types = {
                            br["blob_id"]: br["ftype"]
                            for br in reader.select_rows("m_file_blob", ["blob_id", "ftype"], {"blob_id": blob_ids}, as_dict=True)
                        }
                    for r in c_rows:
                        b_id = tree_map[r["fnid"]]
                        rows.append({
                            "fname": r["fname"],
                            "fid": b_id,
                            "ftype": b_types.get(b_id, 1),
                            "s_stat": "0",
                            "e_stat": "0",
                        })


        entries = []
        seen_dirs = set()
        prefix_len = len(prefix)

        for r in rows:
            fname = safe_decode(r["fname"])
            rel = fname[prefix_len:]
            if not rel:
                continue
            ftype = r["ftype"]
            if "/" in rel:
                # Directory entry
                dirname = rel.split("/", 1)[0]
                if dirname not in seen_dirs:
                    seen_dirs.add(dirname)
                    entries.append({
                        "name": dirname,
                        "type": "dir",
                        "path": f"{prefix}{dirname}" if prefix else dirname,
                    })
            elif ftype == 0 or str(ftype) == "0":
                # Explicit directory record (ftype=0)
                dirname = rel
                if dirname not in seen_dirs:
                    seen_dirs.add(dirname)
                    entries.append({
                        "name": dirname,
                        "type": "dir",
                        "path": f"{prefix}{dirname}" if prefix else dirname,
                    })
            else:
                # File entry (ftype != 0)
                if rel in seen_dirs:
                    continue
                entries.append({
                    "name": rel,
                    "type": "file",
                    "fid": r["fid"],
                    "ftype": safe_decode(r["ftype"]),
                    "path": fname,
                    "s_stat": safe_decode(r["s_stat"]),
                    "e_stat": safe_decode(r["e_stat"]),
                    "s_stat_label": format_stat_label(safe_decode(r["s_stat"])),
                })

        entries.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
        return {
            "version": vname,
            "vid": vid,
            "path": norm_path,
            "entries": entries,
            "tree": entries,
        }

    @staticmethod
    def _git_tree_rows(reader: Any, git_dir: tuple[set[str], dict[str, str]], prefix: str) -> list[dict[str, Any]]:
        """Direct-child rows of prefix from a Git directory listing (list_dir), annotating files via m_file_blob blob_hash."""
        subdirs, direct_files = git_dir
        blob_meta = git_tree_resolver.map_blob_hashes(reader, direct_files.values())
        rows: list[dict[str, Any]] = [
            {"fname": f"{prefix}{d}", "fid": None, "ftype": 0, "s_stat": "0", "e_stat": "0"}
            for d in subdirs
        ]
        for fname, blob_sha in direct_files.items():
            blob_id, ftype = blob_meta.get(blob_sha, (None, 0))
            rows.append({
                "fname": fname,
                "fid": blob_id,
                # Unindexed files are listed as Raw (7); ftype 0 is reserved for directories
                "ftype": ftype or 7,
                "s_stat": "0",
                "e_stat": "0",
            })
        return rows

    def get_file(self, version_name: str, path: str) -> dict[str, Any]:
        """Content of a file at a revision, with the AST token map of its indexed blob.

        The blob is found by content (the Git blob SHA is `m_file_blob.blob_hash`), so the tokens always
        belong to the text served, whatever the revision: a tag, a branch that moved since it was
        parsed, or a commit never ingested. A directory path answers with its listing.
        """
        norm_path = path.strip().strip("/")
        if not norm_path:
            raise HTTPException(status_code=400, detail="File path must be specified.")
        if not is_safe_rel_path(norm_path):
            raise HTTPException(status_code=400, detail="Path traversal attempt detected.")

        with get_reader() as reader:
            vid, vname = get_version_info(reader, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            raw_bytes, is_dir, sha1 = git_reader.read_file(vname, norm_path)
            if is_dir:
                tree_data = self.get_tree(version_name, norm_path, reader=reader)
                return {
                    "type": "dir",
                    "is_directory": True,
                    "version": vname,
                    "vid": vid,
                    "path": norm_path,
                    "ftype": 0,
                    "file_info": {"fid": None, "fname": norm_path, "ftype": "dir"},
                    "tree": tree_data.get("tree", []),
                    "entries": tree_data.get("entries", []),
                    "content": f"[Directory: {norm_path}]",
                    "tokens": [],
                }
            if raw_bytes is None:
                raise HTTPException(status_code=404, detail=f"File '{norm_path}' not found in version '{vname}'.")

            blob_meta = git_tree_resolver.map_blob_hashes(reader, [sha1]).get(sha1) if sha1 else None
            fid, ftype = (blob_meta[0], blob_meta[1]) if blob_meta else (None, None)
            file_size = len(raw_bytes)

            # Safeguard: binary files and oversized blobs are not displayed
            if b"\0" in raw_bytes[:1024]:
                return {
                    "is_binary": True,
                    "path": norm_path,
                    "version": vname,
                    "fid": fid,
                    "ftype": ftype,
                    "message": "Binary file cannot be displayed.",
                }
            if file_size > 50 * 1024 * 1024:
                return {
                    "is_binary": False,
                    "is_oversized": True,
                    "path": norm_path,
                    "version": vname,
                    "fid": fid,
                    "ftype": ftype,
                    "message": f"File size ({file_size / (1024*1024):.2f} MB) exceeds maximum viewer display limit of 50 MB.",
                }

            # Compact AST token maps: [line_s, char_s, line_e, char_e, ast_id, type_id], per blob (content
            # addressed, so shared by every revision holding the same file content)
            compact_tokens: list[list[int]] = []
            if fid is not None:
                # .get() into a local: the cache evicts, so contains-then-get could race a miss
                cached_tokens = _FILE_TOKENS_CACHE.get(fid)
                if cached_tokens is not None:
                    compact_tokens = cached_tokens
                else:
                    compact_tokens = self._blob_tokens(reader, fid)
                    _FILE_TOKENS_CACHE[fid] = compact_tokens

        last_changed = None
        try:
            from webapp.backend.services.git_service import GitService
            last_changed = GitService().get_file_last_changed(vname, norm_path, blob_id=fid)
        except Exception:  # noqa: BLE001 - the header line is optional
            pass

        return {
            "type": "file",
            "is_binary": False,
            "version": vname,
            "vid": vid,
            "path": norm_path,
            "ftype": ftype,
            "file_info": {
                "fid": fid,
                "fname": norm_path,
                "ftype": ftype,
                "last_changed": last_changed,
            },
            "content": raw_bytes.decode("utf-8", errors="replace"),
            "tokens": compact_tokens,
        }

    @staticmethod
    def _blob_tokens(reader: Any, blob_id: int) -> list[list[int]]:
        """Absolute [line_s, char_s, line_e, char_e, ast_id, type_id] of every AST map of one blob."""
        rows = reader.select_rows(
            ("m_bridge_tag", "bt"),
            {
                "line_s": "bt.line_s", "char_s": "bt.char_s", "line_e": "bt.line_e", "char_e": "bt.char_e",
                "m_line_s": "m.line_s", "m_char_s": "m.char_s", "m_line_e": "m.line_e", "m_char_e": "m.char_e",
                "ast_id": "m.ast_id", "type_id": "a.type_id",
            },
            {"bt.blob_id": blob_id},
            joins=[
                Join(("m_map_ast", "m"), {"m.map_id": "bt.tag_id"}),
                Join(("m_ast", "a"), {"a.ast_id": "m.ast_id"}),
            ],
            order_by=["bt.line_s", "bt.char_s"],
            as_dict=True,
        )
        tokens = []
        for r in rows:
            # Absolute line and 0-indexed column coordinates from the 1-indexed snippet-relative map
            abs_ls = r["line_s"] + r["m_line_s"] - 1
            abs_le = r["line_s"] + r["m_line_e"] - 1
            raw_cs = (r["char_s"] + r["m_char_s"] - 1) if r["m_line_s"] == 1 else r["m_char_s"]
            raw_ce = (r["char_s"] + r["m_char_e"] - 1) if r["m_line_e"] == 1 else r["m_char_e"]
            abs_cs = max(0, raw_cs - 1)
            abs_ce = max(abs_cs, raw_ce)
            tokens.append([abs_ls, abs_cs, abs_le, abs_ce, r["ast_id"], r["type_id"]])
        return tokens

    def export_compile_commands(self, version_name: str, arch: str = "x86") -> list[dict[str, str]]:
        """Export a clang compile_commands.json database for every C source of the revision.

        Sources come from the Git tree (Path C) so every revision is complete; files under other
        architectures' arch/<name>/ subtrees are excluded.
        """
        with get_reader() as reader:
            vid, vname = get_version_info(reader, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        tree = git_tree_resolver.list_tree(vname)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"Revision '{vname}' is not available in Git.")

        from webapp.backend.services.arch_layout_service import arch_layout_service
        lay = arch_layout_service.layout(vname, arch)
        srcarch = lay.srcarch if lay else ("x86" if arch in ("x86", "x86_64", "i386") else arch)
        if lay and lay.includes:
            inc_args = " ".join(f"-I{inc.rstrip('/')}" for inc in lay.includes)
            includes = f"{inc_args} -Iarch/{srcarch}/include/uapi -Iinclude/uapi -include include/linux/kconfig.h"
        else:
            includes = (
                f"-Iarch/{srcarch}/include -Iarch/{srcarch}/include/generated -Iinclude "
                f"-Iarch/{srcarch}/include/uapi -Iinclude/uapi -include include/linux/kconfig.h"
            )

        base_dir = str(get_repo_dir())
        commands = []
        for f in sorted(tree):
            if not f.endswith(".c"):
                continue
            if f.startswith("arch/") and not f.startswith(f"arch/{srcarch}/"):
                continue
            commands.append({
                "directory": base_dir,
                "command": f"clang {includes} -D__KERNEL__ -nostdinc -c {f} -o {f[:-2]}.o",
                "file": f,
            })
        return commands

    def get_codebase_treemap(self, version_name: str, max_depth: int = 3, path: str = "") -> dict[str, Any]:
        """Directory hierarchy of the revision weighted by bytes and file counts (for treemap rendering)."""
        norm_path = path.strip().strip("/")
        max_depth = max(1, min(int(max_depth or 3), 8))
        with get_reader() as reader:
            vid, vname = get_version_info(reader, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        treemap_key = (vname, max_depth, norm_path)
        cached_map = _TREEMAP_CACHE.get(treemap_key)
        if cached_map is not None:
            return cached_map

        sizes = git_tree_resolver.list_tree_sizes(vname)
        if sizes is None:
            raise HTTPException(status_code=404, detail=f"Revision '{vname}' is not available in Git.")

        prefix = f"{norm_path}/" if norm_path else ""
        root: dict[str, Any] = {"name": norm_path.split("/")[-1] if norm_path else "root", "path": norm_path, "size": 0, "file_count": 0, "children": {}}
        for f, size in sizes.items():
            if prefix and not f.startswith(prefix):
                continue
            parts = f[len(prefix):].split("/")
            node = root
            node["size"] += size
            node["file_count"] += 1
            for depth, part in enumerate(parts[:max_depth]):
                is_file = depth == len(parts) - 1
                child = node["children"].get(part)
                if child is None:
                    child_path = f"{node['path']}/{part}" if node["path"] else part
                    child = {"name": part, "path": child_path, "size": 0, "file_count": 0, "type": "file" if is_file else "dir", "children": {}}
                    node["children"][part] = child
                child["size"] += size
                child["file_count"] += 1
                node = child

        def serialize(node: dict[str, Any]) -> dict[str, Any]:
            children = sorted(node["children"].values(), key=lambda c: c["size"], reverse=True)
            return {
                "name": node["name"],
                "path": node["path"],
                "type": node.get("type", "dir"),
                "size": node["size"],
                "file_count": node["file_count"],
                "children": [serialize(c) for c in children],
            }

        result = {"version": vname, "max_depth": max_depth, **serialize(root)}
        _TREEMAP_CACHE[treemap_key] = result
        return result

    def search_files(self, version_name: str, query: str, limit: int = 50) -> dict[str, Any]:
        """Fuzzy path search over the revision's Git tree (basename matches rank first).

        Scans a per-commit index of lowercased paths (search_index) and keeps only the best `limit`
        matches (heapq), instead of lowercasing and sorting every match on each keystroke.
        """
        q = (query or "").strip().lower()
        with get_reader() as reader:
            vid, vname = get_version_info(reader, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        if not q:
            return {"version": vname, "query": query, "total": 0, "files": []}
        index = git_tree_resolver.search_index(vname) or []
        fuzzy = len(q) >= 2

        def subsequence_gaps(hay: str) -> int | None:
            pos = -1
            gaps = 0
            for ch in q:
                nxt = hay.find(ch, pos + 1)
                if nxt == -1:
                    return None
                gaps += nxt - pos - 1
                pos = nxt
            return gaps

        def scored() -> Iterator[tuple[float, str]]:
            for lp, base, p in index:
                if base == q:
                    yield (0.0, p)
                elif base.startswith(q):
                    yield (1.0 + len(base) / 1000, p)
                elif q in base:
                    yield (2.0 + base.index(q) / 100, p)
                elif q in lp:
                    yield (3.0 + len(lp) / 1000, p)
                elif fuzzy:
                    gaps = subsequence_gaps(lp)
                    if gaps is not None:
                        yield (4.0 + gaps / 100, p)

        total = 0

        def counted() -> Iterator[tuple[float, str]]:
            nonlocal total
            for item in scored():
                total += 1
                yield item

        best = heapq.nsmallest(max(1, min(limit, 200)), counted())
        files = [{"path": p, "name": p.rsplit("/", 1)[-1], "score": round(sc, 3)} for sc, p in best]
        return {"version": vname, "query": query, "total": total, "files": files}

    def read_raw(self, version_name: str, path: str) -> tuple[bytes, str]:
        """Raw image bytes for documentation figures; `name.*` picks the first existing svg/png/jpg/gif."""
        norm_path = path.strip().strip("/")
        if not norm_path or not is_safe_rel_path(norm_path.replace(".*", ".x")):
            raise HTTPException(status_code=400, detail="A valid relative file path must be specified.")
        with get_reader() as reader:
            vid, vname = get_version_info(reader, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        candidates = [norm_path[:-2] + ext for ext in (".svg", ".png", ".jpg", ".jpeg", ".gif")] if norm_path.endswith(".*") else [norm_path]
        for cand in candidates:
            ext = cand.rsplit(".", 1)[-1].lower() if "." in cand else ""
            media_type = RAW_MEDIA_TYPES.get(ext)
            if media_type is None:
                continue
            data, is_dir, _ = git_reader.read_file(vname, cand)
            if data is not None and not is_dir:
                if len(data) > 50 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="File too large to serve.")
                return data, media_type
        raise HTTPException(status_code=404, detail=f"No image found for '{norm_path}' at '{vname}'.")

    # ------------------------------------------------------------------ references to a file
    REF_INCLUDE, REF_KCONFIG_SOURCE, REF_DOC = 1, 2, 5

    def get_file_references(self, version_name: str, path: str, limit: int = 200) -> dict[str, Any]:
        """What refers to a file at a revision (webapp/API.md section 4.2): the files including it, the Kconfig
        files `source`-ing it and the documents naming it (m_file_reference, the parser's Step 6.6 rows), and
        the Kbuild rules building it (m_kbuild_symbol). Rows are stored per (source path, blob); only those
        whose source blob is the revision's blob of that path are kept."""
        import time
        started = time.perf_counter()
        norm = (path or "").strip().strip("/")
        if not norm or not is_safe_rel_path(norm):
            raise HTTPException(status_code=400, detail="A valid relative file path must be specified.")
        limit = max(1, min(int(limit or 200), 1000))
        lists: dict[str, list[dict[str, Any]]] = {"included_by": [], "sourced_by": [], "documented_in": [], "built_by": []}
        out: dict[str, Any] = {"version": version_name, "path": norm, "includes_source": "parser", "indexed": False}
        with get_reader() as reader:
            vid, vname = get_version_info(reader, version_name)
            rev = vname or version_name
            blob_paths = git_tree_resolver.blob_paths(reader, rev)
            if blob_paths is None:
                raise HTTPException(status_code=404, detail=f"Revision '{version_name}' is not available in Git.")
            in_tree = {(bid, p) for bid, paths in blob_paths.items() for p in paths}
            out["indexed"] = any(p == norm for _bid, p in in_tree)
            row = reader.select_value("m_file_name", "fnid", {"fname": norm})
            if row is not None:
                fnid = int(row)
                kind_of = {self.REF_INCLUDE: "included_by", self.REF_KCONFIG_SOURCE: "sourced_by", self.REF_DOC: "documented_in"}
                # Read from the covering index fref_tgt_src_idx alone (ref_id is its primary key part); the
                # paths and the documents' details are fetched for the rows of the revision only
                rows = [
                    r
                    for r in reader.select_rows(
                        "m_file_reference",
                        ["ref_id", "ref_type", "line_no", "source_blob_id", "source_fnid"],
                        {"target_fnid": fnid, "ref_type": [self.REF_INCLUDE, self.REF_KCONFIG_SOURCE, self.REF_DOC]},
                        as_dict=True,
                    )
                    if int(r["source_blob_id"]) in blob_paths
                ]
                names = self._fnames(reader, {int(r["source_fnid"]) for r in rows})
                kept = [(r, names.get(int(r["source_fnid"]), "")) for r in rows]
                kept = [(r, src) for r, src in kept if (int(r["source_blob_id"]), src) in in_tree]
                details: dict[int, str] = {}
                doc_ids = sorted(int(r["ref_id"]) for r, _src in kept if int(r["ref_type"]) == self.REF_DOC)
                for i in range(0, len(doc_ids), 1000):
                    part = doc_ids[i:i + 1000]
                    details.update(
                        {
                            int(d["ref_id"]): safe_decode(d["details"]) or ""
                            for d in reader.select_rows("m_file_reference", ["ref_id", "details"], {"ref_id": part}, as_dict=True)
                        }
                    )
                seen: set[tuple] = set()
                for r, src in kept:
                    kind = kind_of[int(r["ref_type"])]
                    item = {"path": src, "line": int(r["line_no"] or 0)}
                    if kind == "documented_in":
                        item["details"] = details.get(int(r["ref_id"]), "")
                    key = (kind, src, item["line"])
                    if key not in seen:
                        seen.add(key)
                        lists[kind].append(item)
                try:
                    kbuild_rows = reader.select_rows(
                        ("m_kbuild_symbol", "k"),
                        {
                            "source_blob_id": "k.source_blob_id", "line_no": "k.line_no", "symbol": "k.symbol",
                            "rule": "k.rule", "fname": "fn.fname",
                        },
                        {"k.target_fnid": fnid, "k.via": [1, 2]},
                        joins=[Join(("m_file_name", "fn"), {"fn.fnid": "k.source_fnid"})],
                        as_dict=True,
                    )
                    rules: dict[tuple[str, int], dict[str, Any]] = {}
                    for r in kbuild_rows:
                        src = safe_decode(r["fname"])
                        if (int(r["source_blob_id"]), src) not in in_tree:
                            continue
                        entry = rules.setdefault((src, int(r["line_no"] or 0)), {
                            "path": src, "line": int(r["line_no"] or 0), "rule": safe_decode(r["rule"]) or "", "symbols": []})
                        sym = safe_decode(r["symbol"]) or ""
                        if sym and sym not in entry["symbols"]:
                            entry["symbols"].append(sym)
                    lists["built_by"] = list(rules.values())
                except Exception as exc:  # noqa: BLE001 - an older database without m_kbuild_symbol
                    logger.debug("m_kbuild_symbol unavailable: %s", exc)
        totals = {}
        truncated = False
        for kind, items in lists.items():
            items.sort(key=lambda it: (it["path"], it["line"]))
            totals[kind] = len(items)
            if len(items) > limit:
                lists[kind] = items[:limit]
                truncated = True
        out.update(lists)
        out["totals"] = totals
        out["truncated"] = truncated
        out["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return out

    @staticmethod
    def _fnames(reader: Any, fnids: set[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        ids = sorted(fnids)
        for i in range(0, len(ids), 1000):
            part = ids[i:i + 1000]
            out.update(
                {
                    int(r["fnid"]): safe_decode(r["fname"])
                    for r in reader.select_rows("m_file_name", ["fnid", "fname"], {"fnid": part}, as_dict=True)
                }
            )
        return out

    def resolve_include(
        self,
        version_name: str,
        header: str,
        ast_id: int | None = None,
        current_file: str | None = None,
        arch: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an #include header directive into an authoritative git repo file path.

        The ARCH's build layout decides first (the compiler's search: the including file's directory for
        quoted names, the -I directories, build-time symlinks and generated wrappers; see
        arch_layout_service.resolve_include); the database and search-path heuristics are fallbacks.
        """
        import re

        quoted = '"' in header or "<" not in header
        clean_header = header.strip()
        # Strip #include prefix if present
        if clean_header.startswith("#"):
            clean_header = re.sub(r"^#\s*include\s*", "", clean_header).strip()
        clean_header = clean_header.strip("<>\"' ;")
        # Strip any trailing comments
        clean_header = clean_header.split("//")[0].split("/*")[0].strip()

        if not clean_header:
            raise HTTPException(status_code=400, detail="Empty header specified.")
        # The header becomes a candidate repository path, probed on disk further down. Without this
        # check a traversing header ("../../../../etc/passwd") would be reported as resolved, which
        # tells the caller whether an arbitrary host path exists.
        if not is_safe_rel_path(clean_header):
            raise HTTPException(status_code=400, detail="Path traversal attempt detected.")
        if current_file and not is_safe_rel_path(current_file):
            raise HTTPException(status_code=400, detail="Path traversal attempt detected.")

        cache_key = (version_name, clean_header, current_file, arch, quoted)
        cached = _INCLUDE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        with get_reader() as reader:
            vid, vname = get_version_info(reader, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            # 0. The compiler's search for the ARCH's build layout
            from webapp.backend.services.arch_layout_service import arch_layout_service
            from webapp.backend.services.tree_resolver import definition_arch

            arch_id = definition_arch(vname or version_name, arch)[0]
            path = arch_layout_service.resolve_include(vname or version_name, arch_id, clean_header, current_file, quoted)
            if path:
                res = {"header": clean_header, "path": path, "resolved": True, "method": "build_layout", "arch": arch_id}
                _INCLUDE_CACHE[cache_key] = res
                return res

            # 1. Check compiler-verified m_file_reference if current_file is provided
            if current_file:
                fr_row = reader.select_one(
                    (reader.checkpoint_tree_source(), "bc_src"),
                    {"fname": "fn_target.fname"},
                    [
                        {"bc_src.commit_id": vid, "fn_src.fname": current_file, "fr.ref_type": 1},
                        AnyOf({"fn_target.fname": clean_header}, {"fn_target.fname": like(f"/{clean_header}", "suffix")}),
                    ],
                    joins=[
                        Join(("m_file_name", "fn_src"), {"fn_src.fnid": "bc_src.fnid"}),
                        Join(("m_file_reference", "fr"), {"fr.source_blob_id": "bc_src.blob_id", "fr.source_fnid": "bc_src.fnid"}),
                        Join(("m_file_name", "fn_target"), {"fn_target.fnid": "fr.target_fnid"}),
                    ],
                    as_dict=True,
                )
                if fr_row and fr_row["fname"]:
                    res = {
                        "header": clean_header,
                        "path": safe_decode(fr_row["fname"]),
                        "resolved": True,
                        "method": "file_reference",
                    }
                    _INCLUDE_CACHE[cache_key] = res
                    return res

            # 2. If AST ID provided, check m_ast_include
            if ast_id and int(ast_id) > 0:
                ai_row = reader.select_one(
                    ("m_ast_include", "ai"),
                    {"fname": "fn.fname"},
                    {"ai.ast_id": int(ast_id)},
                    joins=[Join(("m_file_name", "fn"), {"fn.fnid": "ai.fnid"})],
                    as_dict=True,
                )
                if ai_row and ai_row["fname"]:
                    raw_path = safe_decode(ai_row["fname"]).strip("<>\"' ")
                    # Normalize away ../../ components
                    normalized = os.path.normpath(raw_path).lstrip("./")
                    # Check if normalized or raw path exists in DB for this version (must be file, not dir)
                    found = reader.select_one(
                        ("m_file_name", "fn"),
                        {"fname": "fn.fname"},
                        [
                            {"bc.commit_id": vid, "fb.ftype": ne(0)},
                            AnyOf({"fn.fname": [normalized, raw_path]}, {"fn.fname": like(f"/{clean_header}", "suffix")}),
                        ],
                        joins=[
                            Join((reader.checkpoint_tree_source(), "bc"), {"bc.fnid": "fn.fnid"}),
                            Join(("m_file_blob", "fb"), {"fb.blob_id": "bc.blob_id"}),
                        ],
                        as_dict=True,
                    )
                    if found:
                        res = {
                            "header": clean_header,
                            "path": safe_decode(found["fname"]),
                            "resolved": True,
                            "method": "ast_include",
                        }
                        _INCLUDE_CACHE[cache_key] = res
                        return res

            # 3. Build prioritized candidate search paths
            candidates = []
            if current_file:
                cur_dir = os.path.dirname(current_file)
                if cur_dir:
                    candidates.append(os.path.normpath(os.path.join(cur_dir, clean_header)).lstrip("./"))

            candidates.append(clean_header)
            candidates.append(f"include/{clean_header}")
            candidates.append(f"include/uapi/{clean_header}")

            if current_file and current_file.startswith("arch/"):
                parts = current_file.split("/")
                if len(parts) > 1:
                    arch = parts[1]
                    candidates.append(f"arch/{arch}/include/{clean_header}")
                    candidates.append(f"arch/{arch}/include/uapi/{clean_header}")

            candidates.append(f"arch/x86/include/{clean_header}")
            candidates.append(f"arch/x86/include/uapi/{clean_header}")

            # Query database for candidate match (directories excluded): the earlier candidate wins, then a
            # path ending with the header, then the shortest path
            match_path = reader.checkpoint_find_file(vid, candidates, suffix=f"/{clean_header}")
            match_row = {"fname": match_path} if match_path else None
            if match_row:
                res = {
                    "header": clean_header,
                    "path": safe_decode(match_row["fname"]),
                    "resolved": True,
                    "method": "search_path",
                }
                _INCLUDE_CACHE[cache_key] = res
                return res

            # Unindexed revision or file: a candidate that exists in the revision's Git tree
            tree = git_tree_resolver.list_tree(vname) or {}
            for cand in candidates:
                if cand in tree:
                    res = {
                        "header": clean_header,
                        "path": cand,
                        "resolved": True,
                        "method": "git_tree",
                    }
                    _INCLUDE_CACHE[cache_key] = res
                    return res

            res = {
                "header": clean_header,
                "path": None,
                "resolved": False,
                "detail": f"Header '{clean_header}' not found in kernel tree.",
            }
            _INCLUDE_CACHE[cache_key] = res
            return res


filesystem_service = FilesystemService()

