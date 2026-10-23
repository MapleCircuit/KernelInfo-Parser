"""webapp/backend/services/filesystem_service.py - Directory & Source File Inspection."""
from __future__ import annotations
import os
from webapp.backend.config import RepoScopedCache, get_repo_dir
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    format_stat_label,
    get_version_info,
)
from webapp.backend.security.jail import resolve_and_verify_repo_path, is_safe_rel_path
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.tree_resolver import tree_resolver, git_tree_resolver
from core.globalstuff import format_ref_type_label, FileRefType
_INCLUDE_CACHE: dict[tuple[str, str, str | None], dict[str, Any]] = RepoScopedCache()
_FILE_TOKENS_CACHE: dict[tuple[str, str], list[list[int]]] = RepoScopedCache()
_TREEMAP_CACHE: dict[tuple[str, int, str], dict[str, Any]] = RepoScopedCache()
# Only image types are served raw (documentation figures); everything else goes through get_file()
RAW_MEDIA_TYPES = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}


class FilesystemService:
    """Service handling directory tree traversal, file content extraction, and AST coordinate mapping."""

    def get_tree(self, version_name: str, path: str = "") -> dict[str, Any]:
        """Retrieve directory hierarchy and file entries for the given version."""
        norm_path = path.strip().strip("/")
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            prefix = f"{norm_path}/" if norm_path else ""
            like_pattern = f"{prefix}%" if prefix else "%"

            # Path C: the Git tree is authoritative for every revision (tags, branches, arbitrary commits)
            git_tree = git_tree_resolver.list_tree(vname)
            is_checkpoint = False
            if git_tree is None:
                # Git unavailable: fall back to milestone checkpoint in m_bridge_checkpoint
                cursor.execute("SELECT 1 FROM m_bridge_checkpoint WHERE commit_id = %s LIMIT 1;", (vid,))
                is_checkpoint = cursor.fetchone() is not None

            if git_tree is not None:
                rows = self._git_tree_rows(cursor, git_tree, prefix)
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
                        "s_stat_label": format_stat_label(safe_decode(r["s_stat"]), is_end=False),
                        "e_stat_label": format_stat_label(safe_decode(r["e_stat"]), is_end=True),
                    })

            entries.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
            return {
                "version": vname,
                "vid": vid,
                "path": norm_path,
                "entries": entries,
                "tree": entries,
                "total_count": len(entries),
            }

    @staticmethod
    def _git_tree_rows(cursor: Any, git_tree: dict[str, str], prefix: str) -> list[dict[str, Any]]:
        """Build direct-child rows of prefix from a Git tree, annotating files via m_file_blob blob_hash."""
        prefix_len = len(prefix)
        subdirs: set[str] = set()
        direct_files: dict[str, str] = {}
        for fname, blob_sha in git_tree.items():
            if prefix and not fname.startswith(prefix):
                continue
            rel = fname[prefix_len:]
            if "/" in rel:
                subdirs.add(rel.split("/", 1)[0])
            elif rel:
                direct_files[fname] = blob_sha

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
        """Fetch raw file content, metadata, AST spatial coordinate maps, and incoming references."""
        norm_path = path.strip().strip("/")
        if not norm_path:
            raise HTTPException(status_code=400, detail="File path must be specified.")
        if not is_safe_rel_path(norm_path):
            raise HTTPException(status_code=400, detail="Path traversal attempt detected.")

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            # 1. Query file metadata from m_bridge_checkpoint and m_file_blob (if indexed)
            cursor.execute(
                """
                SELECT bc.blob_id AS fid, NULL AS vid_s, NULL AS vid_e, fb.ftype, '0' AS s_stat, '0' AS e_stat, fn.fnid, fn.fname
                FROM m_file_name fn
                JOIN m_bridge_checkpoint bc ON fn.fnid = bc.fnid
                JOIN m_file_blob fb ON bc.blob_id = fb.blob_id
                WHERE bc.commit_id = %s AND fn.fname = %s
                LIMIT 1;
                """,
                (vid, norm_path),
            )
            f_row = cursor.fetchone()

            # 2. Read version-specific source content from Git
            raw_bytes, is_dir, sha1 = git_reader.read_file(vname, norm_path)

            # Non-checkpoint commit: resolve the indexed blob through its content-addressed Git SHA-1
            if not f_row and sha1 and not is_dir:
                blob_meta = git_tree_resolver.map_blob_hashes(cursor, [sha1]).get(sha1)
                if blob_meta:
                    cursor.execute("SELECT fnid FROM m_file_name WHERE fname = %s LIMIT 1;", (norm_path,))
                    fn_match = cursor.fetchone()
                    if fn_match:
                        f_row = {
                            "fid": blob_meta[0],
                            "vid_s": vid,
                            "vid_e": None,
                            "ftype": blob_meta[1],
                            "s_stat": "0",
                            "e_stat": "0",
                            "fnid": fn_match["fnid"],
                            "fname": norm_path,
                        }

            # Check if directory either in git tree or database ftype == 0
            is_directory = is_dir or (f_row is not None and (f_row["ftype"] == 0 or str(f_row["ftype"]) == "0"))
            if is_directory:
                tree_data = self.get_tree(version_name, norm_path)
                return {
                    "type": "dir",
                    "is_directory": True,
                    "version": vname,
                    "vid": vid,
                    "path": norm_path,
                    "file_info": {
                        "fid": f_row["fid"] if f_row else None,
                        "fnid": f_row["fnid"] if f_row else None,
                        "fname": norm_path,
                        "ftype": "dir",
                    },
                    "tree": tree_data.get("tree", []),
                    "entries": tree_data.get("entries", []),
                    "content": f"[Directory: {norm_path}]",
                    "tokens": [],
                    "token_count": 0,
                    "subsystems": [],
                    "used_by": {"total": 0, "counts": {}, "references": []},
                }

            if raw_bytes is None:
                raise HTTPException(status_code=404, detail=f"File '{norm_path}' not found in version '{vname}'.")

            file_size = len(raw_bytes)

            # Safeguard: detect binary files or oversized blobs
            if b"\0" in raw_bytes[:1024]:
                return {
                    "is_binary": True,
                    "file_size": file_size,
                    "path": norm_path,
                    "version": vname,
                    "fid": f_row["fid"] if f_row else None,
                    "message": "Binary file cannot be displayed.",
                }

            if file_size > 10 * 1024 * 1024:
                return {
                    "is_binary": False,
                    "is_oversized": True,
                    "file_size": file_size,
                    "path": norm_path,
                    "version": vname,
                    "fid": f_row["fid"] if f_row else None,
                    "message": f"File size ({file_size / (1024*1024):.2f} MB) exceeds maximum viewer display limit of 10 MB.",
                }

            raw_content = raw_bytes.decode("utf-8", errors="replace")

            # Graceful unindexed fallback: if file exists in Git but has no DB record in checkpoint
            if not f_row:
                from webapp.backend.services.maintainer_service import MaintainerService
                maintainer_srv = MaintainerService()
                subsystems = maintainer_srv.resolve_subsystems_for_file(vname, norm_path)

                cursor.execute("SELECT fnid FROM m_file_name WHERE fname = %s LIMIT 1;", (norm_path,))
                fn_match = cursor.fetchone()
                fallback_fid = fn_match["fnid"] if fn_match else None

                last_changed = None
                try:
                    from webapp.backend.services.git_service import GitService
                    last_changed = GitService().get_file_last_changed(vname, norm_path)
                except Exception:
                    pass

                return {
                    "type": "file",
                    "is_binary": False,
                    "version": vname,
                    "vid": vid,
                    "path": norm_path,
                    "file_info": {
                        "fid": fallback_fid,
                        "fnid": fallback_fid,
                        "fname": norm_path,
                        "ftype": 1,
                        "vid_s": vid,
                        "vid_e": 0,
                        "vname_s": vname,
                        "vname_e": "Active",
                        "added_version": vname,
                        "s_stat": "A",
                        "e_stat": "0",
                        "s_stat_label": "Added",
                        "e_stat_label": "Active",
                        "file_size": file_size,
                        "last_changed": last_changed,
                        "history": [
                            {
                                "fid": fallback_fid,
                                "vid_s": vid,
                                "vid_e": 0,
                                "vname_s": vname,
                                "vname_e": "Active",
                                "s_stat": "A",
                                "e_stat": "0",
                                "s_stat_label": "Added",
                                "e_stat_label": "Active",
                            }
                        ],
                    },
                    "content": raw_content,
                    "tokens": [],
                    "token_count": 0,
                    "subsystems": subsystems,
                    "used_by": {"total": 0, "counts": {}, "references": []},
                }

            fid = f_row["fid"]
            fnid = f_row["fnid"]

            # 3. Compact AST token maps: [line_s, char_s, line_e, char_e, ast_id, type_id]
            token_cache_key = (vname, norm_path)
            if token_cache_key in _FILE_TOKENS_CACHE:
                compact_tokens = _FILE_TOKENS_CACHE[token_cache_key]
            else:
                cursor.execute(
                    """
                    SELECT bt.line_s, bt.char_s, bt.line_e, bt.char_e,
                           m.line_s AS m_line_s, m.char_s AS m_char_s, m.line_e AS m_line_e, m.char_e AS m_char_e,
                           m.ast_id, a.type_id
                    FROM m_bridge_tag bt
                    JOIN m_bridge_map bm ON bt.tag_id = bm.tag_id
                    JOIN m_map_ast m ON bm.map_id = m.map_id
                    JOIN m_ast a ON m.ast_id = a.ast_id
                    WHERE bt.blob_id = %s
                    ORDER BY bt.line_s ASC, bt.char_s ASC;
                    """,
                    (fid,),
                )
                map_rows = cursor.fetchall()
                compact_tokens = []
                for r in map_rows:
                    # Calculate absolute line and 0-indexed column coordinates from 1-indexed snippet-relative map
                    abs_ls = r["line_s"] + r["m_line_s"] - 1
                    abs_le = r["line_s"] + r["m_line_e"] - 1
                    raw_cs = (r["char_s"] + r["m_char_s"] - 1) if r["m_line_s"] == 1 else r["m_char_s"]
                    raw_ce = (r["char_s"] + r["m_char_e"] - 1) if r["m_line_e"] == 1 else r["m_char_e"]
                    abs_cs = max(0, raw_cs - 1)
                    abs_ce = max(abs_cs, raw_ce)
                    compact_tokens.append([abs_ls, abs_cs, abs_le, abs_ce, r["ast_id"], r["type_id"]])
                _FILE_TOKENS_CACHE[token_cache_key] = compact_tokens


            # 4. History across versions and commits
            cursor.execute(
                """
                SELECT
                    bc.blob_id AS fid,
                    bc.commit_id AS vid_s,
                    '0' AS s_stat,
                    c.commit_hash,
                    c.author_date,
                    c.subject,
                    r.ref_name
                FROM m_file_name fn
                JOIN m_bridge_checkpoint bc ON fn.fnid = bc.fnid
                JOIN m_commit c ON bc.commit_id = c.commit_id
                LEFT JOIN m_git_ref r ON bc.commit_id = r.commit_id
                WHERE fn.fname = %s
                ORDER BY c.author_date ASC, bc.commit_id ASC;
                """,
                (norm_path,),
            )
            hist_rows = cursor.fetchall()
            if not hist_rows:
                cursor.execute(
                    """
                    SELECT
                        cf.blob_id AS fid,
                        cf.commit_id AS vid_s,
                        cf.change_type AS s_stat,
                        c.commit_hash,
                        c.author_date,
                        c.subject,
                        r.ref_name
                    FROM m_file_name fn
                    JOIN m_commit_file cf ON fn.fnid = cf.fnid
                    JOIN m_commit c ON cf.commit_id = c.commit_id
                    LEFT JOIN m_git_ref r ON c.commit_id = r.commit_id
                    WHERE fn.fname = %s
                    ORDER BY c.author_date ASC, cf.commit_id ASC;
                    """,
                    (norm_path,),
                )
                hist_rows = cursor.fetchall()


            history = []
            for hr in hist_rows:
                h_vid_s = hr["vid_s"] or vid
                ref_name = safe_decode(hr.get("ref_name"))
                if ref_name:
                    h_vname_s = ref_name
                else:
                    _, h_vname_s = get_version_info(cursor, h_vid_s)
                if not h_vname_s or h_vname_s == "default" or h_vid_s == 0:
                    c_h = hr.get("commit_hash")
                    if c_h:
                        h_vname_s = bytes(c_h).hex()[:8] if isinstance(c_h, (bytes, bytearray, memoryview)) else str(c_h)[:8]
                    else:
                        h_vname_s = vname
                c_hash_hex = ""
                if hr.get("commit_hash"):
                    c_h = hr["commit_hash"]
                    c_hash_hex = bytes(c_h).hex() if isinstance(c_h, (bytes, bytearray, memoryview)) else str(c_h)
                s_stat_val = safe_decode(hr.get("s_stat")) or "0"
                history.append({
                    "fid": hr["fid"],
                    "vid_s": h_vid_s,
                    "vid_e": None,
                    "vname_s": h_vname_s,
                    "vname_e": "Active",
                    "commit_hash": c_hash_hex,
                    "subject": safe_decode(hr.get("subject")) or "",
                    "author_date": hr.get("author_date"),
                    "s_stat": s_stat_val,
                    "e_stat": "0",
                    "s_stat_label": format_stat_label(s_stat_val),
                    "e_stat_label": "Active",
                })


            vid_s = f_row["vid_s"] or vid
            vid_e = f_row["vid_e"]
            _, vname_s = get_version_info(cursor, vid_s)
            if vname_s == "default" or vid_s == 0:
                vname_s = vname
                vid_s = vid
            _, vname_e = get_version_info(cursor, vid_e) if vid_e else (None, "Active")

            # 5. Incoming references ("Used By")
            used_by = self.get_file_references(vname, fid)

            # 6. Subsystems governing this file
            from webapp.backend.services.maintainer_service import MaintainerService
            maintainer_srv = MaintainerService()
            subsystems = maintainer_srv.resolve_subsystems_for_file(vname, norm_path)

            # 7. Last changed commit metadata
            last_changed = None
            try:
                from webapp.backend.services.git_service import GitService
                last_changed = GitService().get_file_last_changed(vname, norm_path)
            except Exception:
                pass

            return {
                "type": "file",
                "is_binary": False,
                "version": vname,
                "vid": vid,
                "path": norm_path,
                "file_info": {
                    "fid": fid,
                    "fnid": fnid,
                    "fname": norm_path,
                    "ftype": safe_decode(f_row["ftype"]),
                    "vid_s": vid_s,
                    "vid_e": vid_e,
                    "vname_s": vname_s,
                    "vname_e": vname_e,
                    "added_version": vname_s,
                    "s_stat": safe_decode(f_row["s_stat"]),
                    "e_stat": safe_decode(f_row["e_stat"]),
                    "s_stat_label": format_stat_label(safe_decode(f_row["s_stat"])),
                    "e_stat_label": format_stat_label(safe_decode(f_row["e_stat"]), is_end=True),
                    "file_size": file_size,
                    "last_changed": last_changed,
                    "history": history,
                },
                "content": raw_content,
                "tokens": compact_tokens,
                "token_count": len(compact_tokens),
                "subsystems": subsystems,
                "used_by": used_by,
            }

    def browse_path(self, version_name: str, path: str = "") -> dict[str, Any]:
        """Unified endpoint: introspects path to determine if it is directory tree or file."""
        norm_path = path.strip().strip("/")
        if not norm_path:
            return self.get_tree(version_name, "")
        if not is_safe_rel_path(norm_path):
            raise HTTPException(status_code=400, detail="Path traversal attempt detected.")

        obj_type, _, _ = git_reader.read_object(version_name, norm_path)
        if obj_type == "tree":
            return self.get_tree(version_name, norm_path)
        return self.get_file(version_name, norm_path)

    def get_file_references(self, version_name: str, fid: int | None, ref_type: str | None = None) -> dict[str, Any]:
        """Query incoming cross-file references from m_file_reference."""
        empty_counts = {
            "include": 0,
            "kbuild": 0,
            "kconfig": 0,
            "makefile": 0,
            "documentation": 0,
        }
        if fid is None:
            return {"total": 0, "counts": empty_counts, "references": []}
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                return {"total": 0, "counts": empty_counts, "references": []}

            # Map fid or fnid to target_fnid
            cursor.execute(
                """
                SELECT bc.fnid FROM m_bridge_checkpoint bc WHERE (bc.blob_id = %s OR bc.fnid = %s) AND bc.commit_id = %s LIMIT 1;
                """,
                (fid, fid, vid),
            )
            row = cursor.fetchone()
            target_fnid = row["fnid"] if row else fid

            cursor.execute(
                """
                SELECT r.ref_type, r.line_no, r.details, fn.fname, r.source_blob_id AS source_fid
                FROM m_file_reference r
                LEFT JOIN m_bridge_checkpoint bc ON r.source_blob_id = bc.blob_id AND bc.commit_id = %s
                LEFT JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE r.target_fnid = %s
                ORDER BY r.ref_type ASC, fn.fname ASC, r.line_no ASC;
                """,
                (vid, target_fnid),
            )
            rows = cursor.fetchall()

            counts = {
                "include": 0,
                "kbuild": 0,
                "kconfig": 0,
                "makefile": 0,
                "documentation": 0,
            }
            references = []
            for r in rows:
                rtype_code = r["ref_type"]
                try:
                    cat_name = format_ref_type_label(FileRefType(rtype_code)).lower()
                except Exception:
                    cat_name = "include"

                counts[cat_name] = counts.get(cat_name, 0) + 1
                if ref_type and ref_type.lower() != cat_name:
                    continue

                references.append({
                    "ref_type": rtype_code,
                    "ref_type_name": cat_name,
                    "line_no": r["line_no"],
                    "details": safe_decode(r["details"]),
                    "source_fname": safe_decode(r["fname"]),
                    "source_fid": r["source_fid"],
                })

            return {
                "total": len(rows),
                "filtered_total": len(references),
                "counts": counts,
                "references": references,
            }

    def get_file_by_id(self, fid: int, version_name: str | None = None) -> dict[str, Any]:
        """Fetch file metadata by primary key fid."""
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT bc.blob_id AS fid, fn.fname, bc.commit_id AS vid
                FROM m_bridge_checkpoint bc
                JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE bc.blob_id = %s
                LIMIT 1;
                """,
                (fid,),
            )
            r = cursor.fetchone()
            if not r:
                # Fallback: check if fid corresponds to an fnid in m_file_name
                cursor.execute("SELECT fname FROM m_file_name WHERE fnid = %s LIMIT 1;", (fid,))
                fn_r = cursor.fetchone()
                if fn_r:
                    v_target = version_name or "v3.0"
                    return self.get_file(str(v_target), safe_decode(fn_r["fname"]))
                raise HTTPException(status_code=404, detail=f"File with FID {fid} not found.")

            v_target = version_name or r["vid"]
            return self.get_file(str(v_target), safe_decode(r["fname"]))

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
        if treemap_key in _TREEMAP_CACHE:
            return _TREEMAP_CACHE[treemap_key]

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
        """Fuzzy path search over the revision's Git tree (basename matches rank first)."""
        q = (query or "").strip().lower()
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
        tree = git_tree_resolver.list_tree(vname) or {}
        if not q:
            return {"version": vname, "query": query, "total": 0, "files": []}

        def subsequence_gaps(needle: str, hay: str) -> int | None:
            pos = -1
            gaps = 0
            for ch in needle:
                nxt = hay.find(ch, pos + 1)
                if nxt == -1:
                    return None
                gaps += nxt - pos - 1
                pos = nxt
            return gaps

        scored: list[tuple[float, str]] = []
        for p in tree:
            lp = p.lower()
            base = lp.rsplit("/", 1)[-1]
            if base == q:
                score = 0.0
            elif base.startswith(q):
                score = 1.0 + len(base) / 1000
            elif q in base:
                score = 2.0 + base.index(q) / 100
            elif q in lp:
                score = 3.0 + len(lp) / 1000
            else:
                gaps = subsequence_gaps(q, lp) if len(q) >= 2 else None
                if gaps is None:
                    continue
                score = 4.0 + gaps / 100
            scored.append((score, p))
        scored.sort()
        files = [{"path": p, "name": p.rsplit("/", 1)[-1], "score": round(sc, 3)} for sc, p in scored[: max(1, min(limit, 200))]]
        return {"version": vname, "query": query, "total": len(scored), "files": files}

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
                if len(data) > 10 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="File too large to serve.")
                return data, media_type
        raise HTTPException(status_code=404, detail=f"No image found for '{norm_path}' at '{vname}'.")

    def get_include_symbols(self, version_name: str, path: str) -> dict[str, Any]:
        """Retrieve symbols defined in headers included by the file."""
        return {"file": path, "symbols": []}

    def resolve_include(
        self,
        version_name: str,
        header: str,
        ast_id: int | None = None,
        current_file: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an #include header directive into an authoritative git repo file path."""
        import re

        clean_header = header.strip()
        # Strip #include prefix if present
        if clean_header.startswith("#"):
            clean_header = re.sub(r"^#\s*include\s*", "", clean_header).strip()
        clean_header = clean_header.strip("<>\"' ;")
        # Strip any trailing comments
        clean_header = clean_header.split("//")[0].split("/*")[0].strip()

        if not clean_header:
            raise HTTPException(status_code=400, detail="Empty header specified.")

        cache_key = (version_name, clean_header, current_file)
        if cache_key in _INCLUDE_CACHE:
            return _INCLUDE_CACHE[cache_key]

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            # 1. Check compiler-verified m_file_reference if current_file is provided
            if current_file:
                cursor.execute(
                    """
                    SELECT fn_target.fname
                    FROM m_bridge_checkpoint bc_src
                    JOIN m_file_name fn_src ON bc_src.fnid = fn_src.fnid
                    JOIN m_file_reference fr ON bc_src.blob_id = fr.source_blob_id
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

            # Check physical file if unindexed (must be file, never directory)
            for cand in candidates:
                cand_path = get_repo_dir() / cand
                if cand_path.is_file():
                    res = {
                        "header": clean_header,
                        "path": cand,
                        "resolved": True,
                        "method": "disk",
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

