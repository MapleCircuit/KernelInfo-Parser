"""webapp/backend/services/filesystem_service.py - Directory & Source File Inspection."""
from __future__ import annotations
from collections.abc import Iterator
import heapq
import os
from webapp.backend.config import RepoScopedCache, get_repo_dir
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    format_stat_label,
    get_version_info,
)
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.security.sql import escape_like
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.tree_resolver import tree_resolver, git_tree_resolver
# Bounded: every key below carries a request parameter (a header, a path), so an unbounded cache
# would grow with each distinct value a caller sends. Treemaps are whole subtrees, so fewer are kept.
_INCLUDE_CACHE: dict[tuple[str, str, str | None], dict[str, Any]] = RepoScopedCache(maxsize=4096)
_FILE_TOKENS_CACHE: dict[int, list[list[int]]] = RepoScopedCache(maxsize=512)
_TREEMAP_CACHE: dict[tuple[str, int, str], dict[str, Any]] = RepoScopedCache(maxsize=64)
# Only image types are served raw (documentation figures); everything else goes through get_file()
RAW_MEDIA_TYPES = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}


class FilesystemService:
    """Service handling directory tree traversal, file content extraction, and AST coordinate mapping."""

    def get_tree(self, version_name: str, path: str = "", cursor: Any = None) -> dict[str, Any]:
        """Retrieve directory hierarchy and file entries for the given version (on `cursor` when given)."""
        if cursor is None:
            with get_db_cursor() as cur:
                return self._get_tree(cur, version_name, path)
        return self._get_tree(cursor, version_name, path)

    def _get_tree(self, cursor: Any, version_name: str, path: str) -> dict[str, Any]:
        norm_path = path.strip().strip("/")
        vid, vname = get_version_info(cursor, version_name)
        if vid is None:
            raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

        prefix = f"{norm_path}/" if norm_path else ""
        # Wildcards in the requested path are escaped: only the trailing % is a wildcard
        like_pattern = f"{escape_like(prefix)}%" if prefix else "%"

        # Path C: the Git tree is authoritative for every revision (tags, branches, arbitrary commits)
        git_dir = git_tree_resolver.list_dir(vname, norm_path)
        is_checkpoint = False
        if git_dir is None:
            # Git unavailable: fall back to milestone checkpoint in m_bridge_checkpoint
            cursor.execute("SELECT 1 FROM m_bridge_checkpoint WHERE commit_id = %s LIMIT 1;", (vid,))
            is_checkpoint = cursor.fetchone() is not None

        if git_dir is not None:
            rows = self._git_tree_rows(cursor, git_dir, prefix)
        elif is_checkpoint:
            sql = """
                SELECT fn.fname, bc.blob_id AS fid, fb.ftype, '0' AS s_stat, '0' AS e_stat
                FROM m_bridge_checkpoint bc
                JOIN m_file_blob fb ON bc.blob_id = fb.blob_id
                JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE bc.commit_id = %s AND (fn.fname LIKE %s OR %s = '')
                ORDER BY fn.fname ASC;
            """
            cursor.execute(sql, (vid, like_pattern, prefix))
            rows = cursor.fetchall()
        else:
            tree_map = tree_resolver.resolve_tree(vid, cursor)
            if not tree_map:
                rows = []
            else:
                fnids = list(tree_map.keys())
                rows = []
                chunk_size = 5000
                for i in range(0, len(fnids), chunk_size):
                    chunk = fnids[i:i + chunk_size]
                    fmt = ",".join(["%s"] * len(chunk))
                    if prefix:
                        cursor.execute(
                            f"SELECT fnid, fname FROM m_file_name WHERE fnid IN ({fmt}) AND fname LIKE %s ORDER BY fname ASC;",
                            [*chunk, like_pattern],
                        )
                    else:
                        cursor.execute(
                            f"SELECT fnid, fname FROM m_file_name WHERE fnid IN ({fmt}) ORDER BY fname ASC;",
                            chunk,
                        )
                    c_rows = cursor.fetchall()
                    blob_ids = list({tree_map[r["fnid"]] for r in c_rows})
                    b_types: dict[int, int] = {}
                    if blob_ids:
                        b_fmt = ",".join(["%s"] * len(blob_ids))
                        cursor.execute(f"SELECT blob_id, ftype FROM m_file_blob WHERE blob_id IN ({b_fmt});", blob_ids)
                        b_types = {br["blob_id"]: br["ftype"] for br in cursor.fetchall()}
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
    def _git_tree_rows(cursor: Any, git_dir: tuple[set[str], dict[str, str]], prefix: str) -> list[dict[str, Any]]:
        """Direct-child rows of prefix from a Git directory listing (list_dir), annotating files via m_file_blob blob_hash."""
        subdirs, direct_files = git_dir
        blob_meta = git_tree_resolver.map_blob_hashes(cursor, direct_files.values())
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

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            raw_bytes, is_dir, sha1 = git_reader.read_file(vname, norm_path)
            if is_dir:
                tree_data = self.get_tree(version_name, norm_path, cursor=cursor)
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

            blob_meta = git_tree_resolver.map_blob_hashes(cursor, [sha1]).get(sha1) if sha1 else None
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
                    compact_tokens = self._blob_tokens(cursor, fid)
                    _FILE_TOKENS_CACHE[fid] = compact_tokens

        last_changed = None
        try:
            from webapp.backend.services.git_service import GitService
            last_changed = GitService().get_file_last_changed(vname, norm_path)
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
    def _blob_tokens(cursor: Any, blob_id: int) -> list[list[int]]:
        """Absolute [line_s, char_s, line_e, char_e, ast_id, type_id] of every AST map of one blob."""
        cursor.execute(
            """
            SELECT bt.line_s, bt.char_s, bt.line_e, bt.char_e,
                   m.line_s AS m_line_s, m.char_s AS m_char_s, m.line_e AS m_line_e, m.char_e AS m_char_e,
                   m.ast_id, a.type_id
            FROM m_bridge_tag bt
            JOIN m_map_ast m ON m.map_id = bt.tag_id
            JOIN m_ast a ON m.ast_id = a.ast_id
            WHERE bt.blob_id = %s
            ORDER BY bt.line_s ASC, bt.char_s ASC;
            """,
            (blob_id,),
        )
        tokens = []
        for r in cursor.fetchall():
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
        arch_dir = "x86" if arch in ("x86", "x86_64", "i386") else arch
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        tree = git_tree_resolver.list_tree(vname)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"Revision '{vname}' is not available in Git.")

        base_dir = str(get_repo_dir())
        includes = (
            f"-Iarch/{arch_dir}/include -Iarch/{arch_dir}/include/generated -Iinclude "
            f"-Iarch/{arch_dir}/include/uapi -Iinclude/uapi -include include/linux/kconfig.h"
        )
        commands = []
        for f in sorted(tree):
            if not f.endswith(".c"):
                continue
            if f.startswith("arch/") and not f.startswith(f"arch/{arch_dir}/"):
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
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
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
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
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
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
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

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
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
                cursor.execute(
                    """
                    SELECT fn_target.fname
                    FROM m_bridge_checkpoint bc_src
                    JOIN m_file_name fn_src ON bc_src.fnid = fn_src.fnid
                    JOIN m_file_reference fr ON fr.source_blob_id = bc_src.blob_id AND fr.source_fnid = bc_src.fnid
                    JOIN m_file_name fn_target ON fr.target_fnid = fn_target.fnid
                    WHERE bc_src.commit_id = %s AND fn_src.fname = %s AND fr.ref_type = 1
                      AND (fn_target.fname = %s OR fn_target.fname LIKE %s)
                    LIMIT 1;
                    """,
                    (vid, current_file, clean_header, f"%/{clean_header}"),
                )
                fr_row = cursor.fetchone()
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
                cursor.execute(
                    """
                    SELECT fn.fname
                    FROM m_ast_include ai
                    JOIN m_file_name fn ON ai.fnid = fn.fnid
                    WHERE ai.ast_id = %s
                    LIMIT 1;
                    """,
                    (int(ast_id),),
                )
                ai_row = cursor.fetchone()
                if ai_row and ai_row["fname"]:
                    raw_path = safe_decode(ai_row["fname"]).strip("<>\"' ")
                    # Normalize away ../../ components
                    normalized = os.path.normpath(raw_path).lstrip("./")
                    # Check if normalized or raw path exists in DB for this version (must be file, not dir)
                    cursor.execute(
                        """
                        SELECT fn.fname
                        FROM m_file_name fn
                        JOIN m_bridge_checkpoint bc ON fn.fnid = bc.fnid
                        JOIN m_file_blob fb ON bc.blob_id = fb.blob_id
                        WHERE bc.commit_id = %s AND fb.ftype != 0 AND (fn.fname = %s OR fn.fname = %s OR fn.fname LIKE %s)
                        LIMIT 1;
                        """,
                        (vid, normalized, raw_path, f"%/{clean_header}"),
                    )
                    found = cursor.fetchone()
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

            # Query database for candidate match (excluding directories ftype != 0)
            placeholders = ", ".join(["%s"] * len(candidates))
            cursor.execute(
                f"""
                SELECT fn.fname
                FROM m_file_name fn
                JOIN m_bridge_checkpoint bc ON fn.fnid = bc.fnid
                JOIN m_file_blob fb ON bc.blob_id = fb.blob_id
                WHERE bc.commit_id = %s AND fb.ftype != 0 AND (fn.fname IN ({placeholders}) OR fn.fname LIKE %s)
                ORDER BY
                    CASE
                        WHEN fn.fname = %s THEN 0
                        WHEN fn.fname = %s THEN 1
                        WHEN fn.fname = %s THEN 2
                        WHEN fn.fname LIKE %s THEN 3
                        ELSE 4
                    END,
                    LENGTH(fn.fname) ASC
                LIMIT 1;
                """,
                (vid, *candidates, f"%/{clean_header}", candidates[0], f"include/{clean_header}", f"include/uapi/{clean_header}", f"%/{clean_header}"),
            )

            match_row = cursor.fetchone()
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

