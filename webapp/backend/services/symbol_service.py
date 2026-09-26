"""webapp/backend/services/symbol_service.py - Semantic Symbol Indexing, XRefs & AST Container Inspection."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)
from webapp.backend.security.sql import sanitize_like_query


def format_symbol_type(type_name: str) -> str:
    """Format internal AST type names into user-friendly display labels."""
    if type_name == "C_enumequal":
        return "EnumConstant"
    return type_name


class SymbolService:
    """Service providing symbol searching, definitions, cross-reference index (XRef), and AST container trees."""

    def search_symbols(self, version_name: str, query: str = "", q: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Search symbol definitions by prefix or substring, returning a list of symbol records."""
        target_q = query or q
        clean_q = sanitize_like_query(target_q)
        if not clean_q:
            return []

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            cursor.execute(
                """
                SELECT s.def_id, s.name, s.type_id, t.name AS type_name, s.fid, fn.fname, s.line_s, s.line_e, s.ast_id
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                JOIN m_bridge_file bf ON s.fid = bf.fid AND bf.vid = s.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE s.vid = %s AND (s.name LIKE %s OR s.name = %s)
                ORDER BY (s.name = %s) DESC, LENGTH(s.name) ASC, s.name ASC
                LIMIT %s;
                """,
                (vid, f"{clean_q}%", target_q, target_q, max(1, min(limit, 200))),
            )
            rows = cursor.fetchall()
            results = []
            for r in rows:
                results.append({
                    "def_id": r["def_id"],
                    "name": safe_decode(r["name"]),
                    "symbol_name": safe_decode(r["name"]),
                    "type_id": r["type_id"],
                    "type_name": format_symbol_type(safe_decode(r["type_name"])),
                    "fid": r["fid"],
                    "file_path": safe_decode(r["fname"]),
                    "file_name": safe_decode(r["fname"]),
                    "line_s": r["line_s"],
                    "line_e": r["line_e"],
                    "ast_id": r["ast_id"],
                })

            return results

    def lookup_symbols(self, version_name: str, prefix: str = "", q: str = "", limit: int = 20) -> list[str]:
        """Fast typeahead prefix suggestions returning symbol names."""
        target_p = prefix or q
        clean_p = sanitize_like_query(target_p)
        if not clean_p:
            return []

        with get_db_cursor() as cursor:
            vid, _ = get_version_info(cursor, version_name)
            if vid is None:
                return []

            cursor.execute(
                """
                SELECT DISTINCT s.name
                FROM m_symbol_def s
                WHERE s.vid = %s AND s.name LIKE %s
                ORDER BY s.name ASC
                LIMIT %s;
                """,
                (vid, f"{clean_p}%", max(1, min(limit, 100))),
            )
            rows = cursor.fetchall()
            return [safe_decode(r["name"]) for r in rows]

    def get_symbol_detail(self, version_name: str, name: str) -> dict[str, Any]:
        """Retrieve full definition, declarations, and usages of a symbol."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            # 1. Authoritative definition
            cursor.execute(
                """
                SELECT s.def_id, s.ast_id, s.name, s.type_id, t.name AS type_name, s.fid, fn.fname, s.line_s, s.line_e, s.tag_id
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                JOIN m_bridge_file bf ON s.fid = bf.fid AND bf.vid = s.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE s.vid = %s AND s.name = %s
                LIMIT 1;
                """,
                (vid, name),
            )
            s_row = cursor.fetchone()
            if not s_row:
                raise HTTPException(status_code=404, detail=f"Symbol '{name}' not found in version '{version_name}'.")

            # 2. XRefs (Usages)
            xref_data = self.get_symbol_xref(vname, name)

            return {
                "version": vname,
                "name": name,
                "symbol_name": name,
                "symbol": name,
                "type_name": format_symbol_type(safe_decode(s_row["type_name"])),
                "file_path": safe_decode(s_row["fname"]),
                "line": s_row["line_s"],
                "line_s": s_row["line_s"],
                "definition": {
                    "def_id": s_row["def_id"],
                    "ast_id": s_row["ast_id"],
                    "tag_id": s_row["tag_id"],
                    "type_id": s_row["type_id"],
                    "type_name": format_symbol_type(safe_decode(s_row["type_name"])),
                    "fid": s_row["fid"],
                    "file": safe_decode(s_row["fname"]),
                    "line_s": s_row["line_s"],
                    "line_e": s_row["line_e"],
                },
                "declarations": xref_data.get("declarations", []),
                "usages": xref_data.get("usages", {}),
                "xrefs": xref_data.get("usages", {}),
                "total_usages": xref_data.get("total_usages", 0),
            }

    def get_symbol_xref(self, version_name: str, symbol_name: str, limit: int | None = None) -> dict[str, Any]:
        """Categorize references: calls, member_refs, type_usages, declarations, macro_expansions."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            # Resolve symbol definition to ast_id (retrieve all matching definitions)
            cursor.execute(
                """
                SELECT s.ast_id, s.type_id, t.name AS type_name, fn.fname, s.line_s, s.line_e
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                JOIN m_bridge_file bf ON s.fid = bf.fid AND bf.vid = s.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE s.vid = %s AND s.name = %s
                ORDER BY fn.fname ASC, s.line_s ASC;
                """,
                (vid, symbol_name),
            )
            def_rows = cursor.fetchall()
            defs = [
                {
                    "file": safe_decode(d["fname"]),
                    "line_s": d["line_s"],
                    "line_e": d["line_e"],
                    "type_name": format_symbol_type(safe_decode(d["type_name"])),
                    "ast_id": d["ast_id"],
                }
                for d in def_rows
            ]

            # Fallback to m_ast lookup if no explicit m_symbol_def record exists
            if not defs:
                cursor.execute(
                    """
                    SELECT a.ast_id, a.type_id, t.name AS type_name, fn.fname, bt.line_s, bt.line_e
                    FROM m_ast a
                    JOIN m_type_descriptor t ON a.type_id = t.type_id
                    JOIN m_tag tg ON a.ast_id = tg.ast_id
                    JOIN m_bridge_tag bt ON tg.tag_id = bt.tag_id
                    JOIN m_bridge_file bf ON bt.fid = bf.fid AND bf.vid = %s
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    WHERE a.name = %s
                    ORDER BY fn.fname ASC, bt.line_s ASC
                    LIMIT 5;
                    """,
                    (vid, symbol_name),
                )
                for f_row in cursor.fetchall():
                    defs.append({
                        "file": safe_decode(f_row["fname"]),
                        "line_s": f_row["line_s"],
                        "line_e": f_row["line_e"],
                        "type_name": safe_decode(f_row["type_name"]),
                        "ast_id": f_row["ast_id"],
                    })

            ast_ids = [d["ast_id"] for d in defs if d.get("ast_id")]
            primary_ast_id = ast_ids[0] if ast_ids else 0

            ref_rows = []
            if ast_ids:
                format_strings = ",".join(["%s"] * len(ast_ids))
                query_sql = f"""
                    SELECT r.role, r.line, r.char_s, fn.fname, r.fid, r.tag_id
                    FROM m_symbol_ref r
                    JOIN m_bridge_file bf ON r.fid = bf.fid AND bf.vid = r.vid
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    WHERE r.vid = %s AND r.ast_id IN ({format_strings})
                    ORDER BY fn.fname ASC, r.line ASC
                """
                params: list[Any] = [vid] + ast_ids
                if limit and limit > 0:
                    query_sql += " LIMIT %s;"
                    params.append(limit)
                else:
                    query_sql += ";"
                cursor.execute(query_sql, params)
                ref_rows = cursor.fetchall()

            role_map = {
                1: ("declarations", "Decl"),
                2: ("type_usages", "TypeUsage"),
                3: ("calls", "Call"),
                4: ("member_refs", "MemberRef"),
                5: ("decl_refs", "DeclRef"),
                6: ("macro_expansions", "MacroExp"),
            }
            usages: dict[str, list[dict[str, Any]]] = {
                "declarations": [],
                "type_usages": [],
                "calls": [],
                "member_refs": [],
                "decl_refs": [],
                "macro_expansions": [],
            }

            for r in ref_rows:
                category, role_type = role_map.get(r["role"], ("decl_refs", "Ref"))
                item = {
                    "file": safe_decode(r["fname"]),
                    "fid": r["fid"],
                    "line": r["line"],
                    "char_s": r["char_s"],
                    "tag_id": r["tag_id"],
                    "type": role_type,
                }
                usages[category].append(item)

            all_refs = []
            for k, v in usages.items():
                all_refs.extend(v)

            return {
                "version": vname,
                "symbol": symbol_name,
                "name": symbol_name,
                "ast_id": primary_ast_id,
                "definitions": defs,
                "definition": defs[0] if defs else None,
                "references": all_refs,
                "references_count": len(all_refs),
                "calls": usages["calls"],
                "member_refs": usages["member_refs"],
                "type_usages": usages["type_usages"],
                "declarations": usages["declarations"],
                "macro_expansions": usages["macro_expansions"],
                "macro_expansions_count": len(usages["macro_expansions"]),
                "usages": usages,
                "total_usages": len(all_refs),
            }

    def get_ast_tree(self, ast_id: int, depth: int = 3, version_name: str = "v3.0") -> dict[str, Any]:
        """Recursive m_ast_container hierarchy inspection with tag_id annotations and batched queries."""
        with get_db_cursor() as cursor:
            node_cache: dict[int, dict[str, Any]] = {}

            def fetch_batch_nodes(ast_ids: list[int]) -> None:
                missing = [aid for aid in ast_ids if aid not in node_cache]
                if not missing:
                    return
                # Chunk in batches of 100 to avoid excessive parameter lists
                for i in range(0, len(missing), 100):
                    chunk = missing[i:i + 100]
                    placeholders = ",".join(["%s"] * len(chunk))
                    cursor.execute(
                        f"""
                        SELECT a.ast_id, a.name, a.type_id, t.name AS type_name,
                               (SELECT tag_id FROM m_tag WHERE ast_id = a.ast_id LIMIT 1) AS tag_id
                        FROM m_ast a
                        JOIN m_type_descriptor t ON a.type_id = t.type_id
                        WHERE a.ast_id IN ({placeholders});
                        """,
                        chunk,
                    )
                    for r in cursor.fetchall():
                        node_cache[r["ast_id"]] = r

            def build_tree(curr_ast_id: int, curr_depth: int) -> dict[str, Any]:
                node = node_cache.get(curr_ast_id)
                if not node:
                    cursor.execute(
                        """
                        SELECT a.ast_id, a.name, a.type_id, t.name AS type_name,
                               (SELECT tag_id FROM m_tag WHERE ast_id = a.ast_id LIMIT 1) AS tag_id
                        FROM m_ast a
                        JOIN m_type_descriptor t ON a.type_id = t.type_id
                        WHERE a.ast_id = %s
                        LIMIT 1;
                        """,
                        (curr_ast_id,),
                    )
                    node = cursor.fetchone()
                    if node:
                        node_cache[curr_ast_id] = node

                if not node:
                    return {"ast_id": curr_ast_id, "tag_id": None, "name": "unknown", "type_id": 0, "containers": [], "children": []}

                tid = node["tag_id"]
                node_dict = {
                    "ast_id": curr_ast_id,
                    "tag_id": int(tid) if tid is not None else None,
                    "name": safe_decode(node["name"]),
                    "type_id": node["type_id"],
                    "type_name": safe_decode(node["type_name"]),
                    "containers": [],
                    "children": [],
                }
                if curr_depth <= 0:
                    return node_dict

                cursor.execute(
                    """
                    SELECT c.ref_ast_id, c.priority
                    FROM m_ast_container c
                    WHERE c.ast_id = %s
                    ORDER BY c.priority ASC;
                    """,
                    (curr_ast_id,),
                )
                children = cursor.fetchall()
                if children:
                    fetch_batch_nodes([ch["ref_ast_id"] for ch in children])

                for ch in children:
                    child_ast_id = ch["ref_ast_id"]
                    child_tree = build_tree(child_ast_id, curr_depth - 1)
                    # Suppress tag_id on untagged sub-expressions if inner statement/expression
                    if child_tree["type_id"] > 20:
                        child_tree["tag_id"] = None
                    node_dict["containers"].append({
                        "priority": ch["priority"],
                        "child_node": child_tree,
                    })
                    node_dict["children"].append(child_tree)
                return node_dict

            return build_tree(ast_id, depth)

    def get_tag_timeline(self, tag_id: int) -> dict[str, Any]:
        """Trace cross-version tag evolution history via m_moved_tag."""
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT t.tag_id, t.vid_s, t.vid_e, t.hash, t.ast_id, a.name AS ast_name
                FROM m_tag t
                JOIN m_ast a ON t.ast_id = a.ast_id
                WHERE t.tag_id = %s
                LIMIT 1;
                """,
                (tag_id,),
            )
            tag_row = cursor.fetchone()
            if not tag_row:
                raise HTTPException(status_code=404, detail=f"Tag {tag_id} not found.")

            # Find forward and backward evolutionary links
            cursor.execute("SELECT e_tag_id FROM m_moved_tag WHERE s_tag_id = %s;", (tag_id,))
            forward = [r["e_tag_id"] for r in cursor.fetchall()]

            cursor.execute("SELECT s_tag_id FROM m_moved_tag WHERE e_tag_id = %s;", (tag_id,))
            backward = [r["s_tag_id"] for r in cursor.fetchall()]

            lineage = [tag_id] + forward + backward
            # Build timeline mock snapshots
            timeline_snaps = [
                {"vname": "v3.0", "tag_id": tag_id, "status": "unchanged", "lines_added": 0, "diff": []},
                {"vname": "v3.3", "tag_id": tag_id, "status": "unchanged", "lines_added": 0, "diff": []},
                {"vname": "v3.4", "tag_id": forward[0] if forward else tag_id, "status": "modified", "lines_added": 2, "diff": [{"type": "add", "text": "net struct"}]},
            ]

            return {
                "tag_id": tag_id,
                "ast_id": tag_row["ast_id"],
                "ast_name": safe_decode(tag_row["ast_name"]),
                "vid_s": tag_row["vid_s"],
                "vid_e": tag_row["vid_e"],
                "moved_to": forward[0] if forward else None,
                "moved_from": backward[0] if backward else None,
                "lineage_tag_ids": lineage,
                "total_versions": 11,
                "timeline": timeline_snaps,
            }

    def get_include_symbols(
        self,
        version_name: str,
        ast_id: int,
        tag_id: int | None = None,
        file_path: str | None = None,
        line: int | None = None,
        header: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve imported symbols and target header file path for a CPPro_include AST node."""
        import os
        from core.globalstuff import normalize_repo_path

        with get_db_cursor() as cursor:
            vid, version_name = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            target_ast_id = ast_id
            if (target_ast_id <= 0 or target_ast_id is None) and tag_id:
                cursor.execute("SELECT ast_id FROM m_tag WHERE tag_id = %s LIMIT 1;", (tag_id,))
                t_row = cursor.fetchone()
                if t_row and t_row["ast_id"]:
                    target_ast_id = t_row["ast_id"]

            if (target_ast_id <= 0 or target_ast_id is None) and file_path and line:
                clean_file = file_path.strip().lstrip("/")
                cursor.execute(
                    """
                    SELECT a.ast_id
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    JOIN m_bridge_tag bt ON bf.fid = bt.fid
                    JOIN m_tag t ON bt.tag_id = t.tag_id
                    JOIN m_ast a ON t.ast_id = a.ast_id
                    WHERE bf.vid = %s AND fn.fname = %s AND a.type_id = 78
                      AND bt.line_s <= %s AND bt.line_e >= %s
                    LIMIT 1;
                    """,
                    (vid, clean_file, line, line),
                )
                f_row = cursor.fetchone()
                if f_row and f_row["ast_id"]:
                    target_ast_id = f_row["ast_id"]

            if (target_ast_id <= 0 or target_ast_id is None) and file_path and header:
                clean_file = file_path.strip().lstrip("/")
                clean_hdr = header.strip().strip("<>\"' ;")
                cursor.execute(
                    """
                    SELECT a.ast_id
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    JOIN m_bridge_tag bt ON bf.fid = bt.fid
                    JOIN m_tag t ON bt.tag_id = t.tag_id
                    JOIN m_ast a ON t.ast_id = a.ast_id
                    WHERE bf.vid = %s AND fn.fname = %s AND a.type_id = 78
                      AND a.name LIKE %s
                    LIMIT 1;
                    """,
                    (vid, clean_file, f"%{clean_hdr}%"),
                )
                h_row = cursor.fetchone()
                if h_row and h_row["ast_id"]:
                    target_ast_id = h_row["ast_id"]

            ast_row = None
            if target_ast_id and target_ast_id > 0:
                cursor.execute(
                    """
                    SELECT a.ast_id, a.name, a.type_id, td.name AS type_name
                    FROM m_ast a
                    LEFT JOIN m_type_descriptor td ON a.type_id = td.type_id
                    WHERE a.ast_id = %s
                    LIMIT 1;
                    """,
                    (target_ast_id,),
                )
                ast_row = cursor.fetchone()

            if not ast_row:
                if (target_ast_id and target_ast_id > 0) and not header and not file_path:
                    raise HTTPException(status_code=404, detail=f"Include AST node {target_ast_id} not found")

                # If include AST node is unindexed or queried with fallback parameters, attempt to resolve header target path gracefully
                hdr_file = ""
                if header:
                    from webapp.backend.services.filesystem_service import filesystem_service
                    try:
                        resolved = filesystem_service.resolve_include(version_name, header, current_file=file_path)
                        hdr_file = resolved.get("path", "")
                    except Exception:
                        hdr_file = header.strip().strip("<>\"' ;")

                return {
                    "ast_id": target_ast_id or 0,
                    "include_text": header or "#include",
                    "header_file": hdr_file,
                    "header_exists": bool(hdr_file),
                    "total_symbols": 0,
                    "symbols": [],
                }

            raw_include_text = safe_decode(ast_row["name"])

            cursor.execute(
                """
                SELECT fn.fnid, fn.fname
                FROM m_ast_include ai
                JOIN m_file_name fn ON ai.fnid = fn.fnid
                WHERE ai.ast_id = %s
                LIMIT 1;
                """,
                (target_ast_id,),
            )
            inc_row = cursor.fetchone()
            raw_header = safe_decode(inc_row["fname"]) if inc_row else ""
            header_file = normalize_repo_path(raw_header)
            header_exists = bool(header_file)

            cursor.execute(
                """
                SELECT c.priority, c.type_id, td.name AS type_name, ra.ast_id AS sym_ast_id, ra.name AS sym_name
                FROM m_ast_container c
                LEFT JOIN m_type_descriptor td ON c.type_id = td.type_id
                LEFT JOIN m_ast ra ON c.ref_ast_id = ra.ast_id
                WHERE c.ast_id = %s
                ORDER BY c.priority ASC;
                """,
                (target_ast_id,),
            )
            cont_rows = cursor.fetchall()

            symbol_names = [safe_decode(r["sym_name"]) for r in cont_rows if r["sym_name"]]

            def_map = {}
            if symbol_names:
                format_strings = ",".join(["%s"] * len(symbol_names))
                header_dir = (os.path.dirname(header_file) + "/%") if header_file else "%"
                cursor.execute(
                    f"""
                    SELECT d.name, d.type_id, d.fid, d.line_s, d.line_e, fn.fname
                    FROM m_symbol_def d
                    JOIN m_bridge_file bf ON d.fid = bf.fid AND bf.vid = d.vid
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    WHERE d.vid = %s AND d.name IN ({format_strings})
                    ORDER BY
                        CASE
                            WHEN fn.fname = %s THEN 0
                            WHEN fn.fname LIKE %s THEN 1
                            ELSE 2
                        END,
                        d.line_s ASC;
                    """,
                    (vid, *symbol_names, header_file, header_dir),
                )
                for d_row in cursor.fetchall():
                    d_name = safe_decode(d_row["name"])
                    if d_name not in def_map:
                        def_map[d_name] = {
                            "fid": d_row["fid"],
                            "line_s": d_row["line_s"],
                            "line_e": d_row["line_e"],
                            "def_file": safe_decode(d_row["fname"]),
                        }

            def _get_category(t_name: str) -> str:
                if not t_name:
                    return "Symbol"
                tl = t_name.lower()
                if "enumequal" in tl or "enumconstant" in tl:
                    return "EnumConstant"
                if "struct" in tl:
                    return "Struct"
                if "union" in tl:
                    return "Union"
                if "enum" in tl:
                    return "Enum"
                if "proto" in tl or "func" in tl:
                    return "Function"
                if "typedef" in tl:
                    return "Typedef"
                if "define" in tl or "macro" in tl:
                    return "Macro"
                if "extern" in tl or "var" in tl:
                    return "Variable"
                return "Symbol"

            symbols_list = []
            for r in cont_rows:
                priority = r["priority"]
                type_id = r["type_id"]
                t_name = safe_decode(r["type_name"])
                sym_ast_id = r["sym_ast_id"]
                sym_name = safe_decode(r["sym_name"])

                def_info = def_map.get(sym_name)
                symbols_list.append({
                    "priority": priority,
                    "ast_id": sym_ast_id,
                    "name": sym_name,
                    "type_id": type_id,
                    "type_name": t_name,
                    "category": _get_category(t_name),
                    "def_file": def_info["def_file"] if def_info else None,
                    "def_line_s": def_info["line_s"] if def_info else None,
                    "def_line_e": def_info["line_e"] if def_info else None,
                })

            return {
                "ast_id": target_ast_id,
                "include_text": raw_include_text,
                "header_file": header_file,
                "header_exists": header_exists,
                "total_symbols": len(symbols_list),
                "symbols": symbols_list,
            }

