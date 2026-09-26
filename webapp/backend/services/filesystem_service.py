"""webapp/backend/services/filesystem_service.py - Directory & Source File Inspection."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    format_stat_label,
    get_version_info,
)
from webapp.backend.security.jail import resolve_and_verify_repo_path, is_safe_rel_path, LINUX_REPO_DIR
from webapp.backend.services.git_reader import git_reader
from core.globalstuff import format_ref_type_label, FileRefType
_INCLUDE_CACHE: dict[tuple[str, str, str | None], dict[str, Any]] = {}
_FILE_TOKENS_CACHE: dict[tuple[str, str], list[list[int]]] = {}
_TREEMAP_CACHE: dict[tuple[str, int, str], dict[str, Any]] = {}


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
            sql = """
                SELECT fn.fname, f.fid, f.ftype, f.s_stat, f.e_stat, f.vid_s, f.vid_e
                FROM m_bridge_file bf
                JOIN m_file f ON bf.fid = f.fid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE bf.vid = %s AND (fn.fname LIKE %s OR %s = '')
                ORDER BY fn.fname ASC;
            """
            like_pattern = f"{prefix}%" if prefix else "%"
            cursor.execute(sql, (vid, like_pattern, prefix))
            rows = cursor.fetchall()

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
                    # Explicit directory record from m_file (ftype=0)
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

            # 1. Query file metadata from m_bridge_file and m_file (if indexed)
            cursor.execute(
                """
                SELECT f.fid, f.vid_s, f.vid_e, f.ftype, f.s_stat, f.e_stat, fn.fnid, fn.fname
                FROM m_file_name fn
                JOIN m_bridge_file bf ON fn.fnid = bf.fnid
                JOIN m_file f ON bf.fid = f.fid
                WHERE bf.vid = %s AND fn.fname = %s
                LIMIT 1;
                """,
                (vid, norm_path),
            )
            f_row = cursor.fetchone()

            # 2. Read version-specific source content from Git
            raw_bytes, is_dir, sha1 = git_reader.read_file(vname, norm_path)

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

            # Graceful unindexed fallback: if file exists in Git but has no DB record in m_bridge_file
            if not f_row:
                from webapp.backend.services.maintainer_service import MaintainerService
                maintainer_srv = MaintainerService()
                subsystems = maintainer_srv.resolve_subsystems_for_file(vname, norm_path)

                cursor.execute("SELECT fnid FROM m_file_name WHERE fname = %s LIMIT 1;", (norm_path,))
                fn_match = cursor.fetchone()
                fallback_fid = fn_match["fnid"] if fn_match else None

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
                    WHERE bt.fid = %s
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


            # 4. History across versions
            cursor.execute(
                """
                SELECT f.fid, f.vid_s, f.vid_e, f.s_stat, f.e_stat
                FROM m_file_name fn
                JOIN m_bridge_file bf ON fn.fnid = bf.fnid
                JOIN m_file f ON bf.fid = f.fid
                WHERE fn.fname = %s
                ORDER BY f.vid_s ASC;
                """,
                (norm_path,),
            )
            hist_rows = cursor.fetchall()
            history = []
            for hr in hist_rows:
                h_vid_s = hr["vid_s"]
                h_vid_e = hr["vid_e"]
                _, h_vname_s = get_version_info(cursor, h_vid_s)
                _, h_vname_e = get_version_info(cursor, h_vid_e) if h_vid_e else (None, "Active")
                history.append({
                    "fid": hr["fid"],
                    "vid_s": h_vid_s,
                    "vid_e": h_vid_e,
                    "vname_s": h_vname_s,
                    "vname_e": h_vname_e,
                    "s_stat": safe_decode(hr["s_stat"]),
                    "e_stat": safe_decode(hr["e_stat"]),
                    "s_stat_label": format_stat_label(safe_decode(hr["s_stat"])),
                    "e_stat_label": format_stat_label(safe_decode(hr["e_stat"]), is_end=True),
                })

            vid_s = f_row["vid_s"]
            vid_e = f_row["vid_e"]
            _, vname_s = get_version_info(cursor, vid_s)
            _, vname_e = get_version_info(cursor, vid_e) if vid_e else (None, "Active")

            # 5. Incoming references ("Used By")
            used_by = self.get_file_references(vname, fid)

            # 6. Subsystems governing this file
            from webapp.backend.services.maintainer_service import MaintainerService
            maintainer_srv = MaintainerService()
            subsystems = maintainer_srv.resolve_subsystems_for_file(vname, norm_path)

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
        if fid is None:
            return {"total": 0, "counts": {}, "references": []}
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                return {"total": 0, "counts": {}, "references": []}

            # Map fid to fnid
            cursor.execute(
                """
                SELECT bf.fnid FROM m_bridge_file bf WHERE bf.fid = %s AND bf.vid = %s LIMIT 1;
                """,
                (fid, vid),
            )
            row = cursor.fetchone()
            if not row:
                return {"total": 0, "counts": {}, "references": []}
            target_fnid = row["fnid"]

            cursor.execute(
                """
                SELECT r.ref_type, r.line_no, r.details, fn.fname, r.source_fid
                FROM m_file_reference r
                JOIN m_bridge_file bf ON r.source_fid = bf.fid AND bf.vid = r.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE r.vid = %s AND r.target_fnid = %s
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
                SELECT f.fid, f.vid_s, f.vid_e, f.ftype, f.s_stat, f.e_stat, fn.fname, bf.vid
                FROM m_file f
                JOIN m_bridge_file bf ON f.fid = bf.fid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE f.fid = %s
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
        """Export compile_commands.json structure for clang tooling."""
        arch_dir = "x86" if arch in ("x86", "x86_64", "i386") else arch
        with get_db_cursor() as cursor:
            vid, _ = get_version_info(cursor, version_name)
            cursor.execute(
                """
                SELECT fn.fname
                FROM m_bridge_file bf
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE bf.vid = %s AND fn.fname LIKE '%.c'
                ORDER BY fn.fname ASC
                LIMIT 500;
                """,
                (vid or 1,),
            )
            files = [safe_decode(r["fname"]) for r in cursor.fetchall()]

        if not files:
            files = ["init/main.c", "kernel/sched.c", f"arch/{arch_dir}/kernel/setup.c"]

        base_dir = str(LINUX_REPO_DIR)
        commands = []
        for f in files:
            commands.append({
                "directory": base_dir,
                "command": f"clang -Iinclude -Iarch/{arch_dir}/include -D__KERNEL__ -c {f} -o {f[:-2]}.o",
                "file": f,
            })
        return commands

    def get_codebase_treemap(self, version_name: str, max_depth: int = 3, path: str = "") -> dict[str, Any]:
        """Generate squarified directory treemap hierarchy with file counts."""
        norm_path = path.strip().strip("/")
        treemap_key = (version_name, max_depth, norm_path)
        if treemap_key in _TREEMAP_CACHE:
            return _TREEMAP_CACHE[treemap_key]

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            prefix = f"{norm_path}/" if norm_path else ""
            if prefix:
                cursor.execute(
                    """
                    SELECT fn.fname
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    WHERE bf.vid = %s AND (fn.fname LIKE %s OR fn.fname = %s)
                    ORDER BY fn.fname ASC;
                    """,
                    (vid or 1, f"{prefix}%", norm_path),
                )
            else:
                cursor.execute(
                    """
                    SELECT fn.fname
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    WHERE bf.vid = %s
                    ORDER BY fn.fname ASC;
                    """,
                    (vid or 1,),
                )
            files = [safe_decode(r["fname"]) for r in cursor.fetchall()]

        # Build tree dictionary
        root_name = norm_path if norm_path else "root"
        prefix_len = len(prefix)
        root: dict[str, Any] = {"name": root_name, "children": {}, "file_count": len(files)}
        for f in files:
            rel = f[prefix_len:] if prefix_len else f
            if not rel:
                continue
            parts = rel.split("/")
            curr = root
            for part in parts[:max_depth]:
                if part not in curr.setdefault("children", {}):
                    curr["children"][part] = {"name": part, "children": {}, "file_count": 0}
                curr = curr["children"][part]
                curr["file_count"] = curr.get("file_count", 0) + 1

        def serialize(node: dict[str, Any]) -> dict[str, Any]:
            res: dict[str, Any] = {"name": node["name"], "file_count": node.get("file_count", 0)}
            if "children" in node and node["children"]:
                res["children"] = [serialize(child) for child in node["children"].values()]
            return res

        result = serialize(root)
        _TREEMAP_CACHE[treemap_key] = result
        return result

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

            # 1. If AST ID provided, check m_ast_include
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
                        JOIN m_bridge_file bf ON fn.fnid = bf.fnid
                        JOIN m_file f ON bf.fid = f.fid
                        WHERE bf.vid = %s AND f.ftype != 0 AND (fn.fname = %s OR fn.fname = %s OR fn.fname LIKE %s)
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

            # 2. Build prioritized candidate search paths
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
                JOIN m_bridge_file bf ON fn.fnid = bf.fnid
                JOIN m_file f ON bf.fid = f.fid
                WHERE bf.vid = %s AND f.ftype != 0 AND (fn.fname IN ({placeholders}) OR fn.fname LIKE %s)
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
                cand_path = LINUX_REPO_DIR / cand
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

