"""webapp/backend/services/symbol_service.py - Semantic Symbol Indexing, XRefs & AST Container Inspection."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from core.SymbolRows import rows_of_targets_sql
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)
from webapp.backend.security.sql import sanitize_like_query
from webapp.backend.services.tree_resolver import git_tree_resolver

# Name-ordered candidates scanned before scoping to the revision's tree (symbols of every indexed
# version share m_symbol_def, so a prefix can match many blobs that are not in the requested tree)
SEARCH_CANDIDATES = 30000


def format_symbol_type(type_name: str) -> str:
    """Format internal AST type names into user-friendly display labels."""
    if type_name == "C_enumequal":
        return "EnumConstant"
    return type_name


class SymbolService:
    """Service providing symbol searching, definitions, cross-reference index (XRef), and AST container trees."""

    @staticmethod
    def _scope(cursor: Any, version_name: str) -> tuple[int | None, str, dict[int, list[str]]]:
        """Resolve (vid, vname, {blob_id: [paths]}) for any revision: tag, branch or arbitrary commit.

        The Git tree is authoritative (works for commits without a checkpoint snapshot, even commits
        that were never ingested); m_bridge_checkpoint is the fallback when Git is unavailable.
        """
        vid, vname = get_version_info(cursor, version_name)
        rev = vname if vid is not None else version_name
        blob_paths = git_tree_resolver.blob_paths(cursor, rev)
        if blob_paths is None:
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")
            cursor.execute(
                """
                SELECT bc.blob_id, fn.fname
                FROM m_bridge_checkpoint bc
                JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE bc.commit_id = %s;
                """,
                (vid,),
            )
            blob_paths = {}
            for r in cursor.fetchall():
                blob_paths.setdefault(r["blob_id"], []).append(safe_decode(r["fname"]))
        return vid, rev, blob_paths

    def search_symbols(self, version_name: str, query: str = "", q: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Search symbol definitions by prefix or substring, returning a list of symbol records."""
        target_q = query or q
        clean_q = sanitize_like_query(target_q)
        if not clean_q:
            return []

        with get_db_cursor() as cursor:
            _, _, blob_paths = self._scope(cursor, version_name)
            cap = max(1, min(limit, 200))
            # One query: m_symbol_def is a view whose shared rows have computed def_ids, so a second lookup by
            # def_id could not use an index
            cursor.execute(
                """
                SELECT s.def_id, s.name, s.type_id, t.name AS type_name, s.blob_id AS fid, s.line_s, s.line_e, s.ast_id
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                WHERE (s.name LIKE %s OR s.name = %s)
                ORDER BY (s.name = %s) DESC, LENGTH(s.name) ASC, s.name ASC
                LIMIT %s;
                """,
                (f"{clean_q}%", target_q, target_q, SEARCH_CANDIDATES),
            )
            picked = [r for r in cursor.fetchall() if r["fid"] in blob_paths][:cap]
            results = []
            for r in picked:
                path = blob_paths[r["fid"]][0]
                results.append({
                    "def_id": r["def_id"],
                    "name": safe_decode(r["name"]),
                    "symbol_name": safe_decode(r["name"]),
                    "type_id": r["type_id"],
                    "type_name": format_symbol_type(safe_decode(r["type_name"])),
                    "fid": r["fid"],
                    "file_path": path,
                    "file_name": path,
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
            try:
                _, _, blob_paths = self._scope(cursor, version_name)
            except HTTPException:
                return []

            cursor.execute(
                """
                SELECT s.name, s.blob_id
                FROM m_symbol_def s
                WHERE s.name LIKE %s
                ORDER BY s.name ASC
                LIMIT %s;
                """,
                (f"{clean_p}%", SEARCH_CANDIDATES),
            )
            cap = max(1, min(limit, 100))
            names: list[str] = []
            for r in cursor.fetchall():
                if r["blob_id"] not in blob_paths:
                    continue
                name = safe_decode(r["name"])
                if not names or names[-1] != name:
                    names.append(name)
                    if len(names) >= cap:
                        break
            return names

    def get_symbol_detail(self, version_name: str, name: str) -> dict[str, Any]:
        """Retrieve full definition, declarations, and usages of a symbol."""
        with get_db_cursor() as cursor:
            _, vname, blob_paths = self._scope(cursor, version_name)

            # Check if this is a Kconfig configuration symbol (e.g. CONFIG_NET or CONFIG_SMP)
            if name.startswith("CONFIG_"):
                clean_name = name[7:]
                try:
                    from webapp.backend.services.kconfig_service import KconfigService
                    k_svc = KconfigService()
                    k_detail = k_svc.get_symbol_detail(vname, clean_name)
                    if k_detail and k_detail.get("file_path"):
                        return {
                            "version": vname,
                            "name": name,
                            "symbol_name": name,
                            "symbol": name,
                            "type_name": "kconfig",
                            "file_path": k_detail["file_path"],
                            "line": k_detail["line_s"],
                            "line_s": k_detail["line_s"],
                            "line_e": k_detail["line_e"],
                            "definition": {
                                "def_id": 0,
                                "ast_id": 0,
                                "tag_id": 0,
                                "type_id": 88,
                                "type_name": "kconfig",
                                "fid": None,
                                "file": k_detail["file_path"],
                                "file_path": k_detail["file_path"],
                                "line_s": k_detail["line_s"],
                                "line_e": k_detail["line_e"],
                            },
                            "declarations": k_detail.get("declarations", []),
                            "usages": {},
                            "xrefs": {},
                            "total_usages": 0,
                            "is_kconfig": True,
                            "kconfig": k_detail,
                        }
                except Exception:
                    pass

            # 1. Authoritative definition: prefer one whose blob is in this revision's tree
            cursor.execute(
                """
                SELECT s.def_id, s.ast_id, s.name, s.type_id, t.name AS type_name, s.blob_id AS fid, s.line_s, s.line_e, s.tag_id
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                WHERE s.name = %s
                ORDER BY s.def_id ASC;
                """,
                (name,),
            )
            def_rows = cursor.fetchall()
            s_row = next((r for r in def_rows if r["fid"] in blob_paths), def_rows[0] if def_rows else None)
            if s_row is not None:
                s_row["fname"] = (blob_paths.get(s_row["fid"]) or [None])[0]
            if not s_row:
                # Check if this is a Kconfig configuration symbol (e.g. CONFIG_NET or NET)
                clean_name = name[7:] if name.startswith("CONFIG_") else name
                try:
                    from webapp.backend.services.kconfig_service import KconfigService
                    k_svc = KconfigService()
                    k_detail = k_svc.get_symbol_detail(vname, clean_name)
                    if k_detail and k_detail.get("file_path"):
                        return {
                            "version": vname,
                            "name": name,
                            "symbol_name": name,
                            "symbol": name,
                            "type_name": "kconfig",
                            "file_path": k_detail["file_path"],
                            "line": k_detail["line_s"],
                            "line_s": k_detail["line_s"],
                            "line_e": k_detail["line_e"],
                            "definition": {
                                "def_id": 0,
                                "ast_id": 0,
                                "tag_id": 0,
                                "type_id": 88,
                                "type_name": "kconfig",
                                "fid": None,
                                "file": k_detail["file_path"],
                                "file_path": k_detail["file_path"],
                                "line_s": k_detail["line_s"],
                                "line_e": k_detail["line_e"],
                            },
                            "declarations": k_detail.get("declarations", []),
                            "usages": {},
                            "xrefs": {},
                            "total_usages": 0,
                            "is_kconfig": True,
                            "kconfig": k_detail,
                        }
                except Exception:
                    pass
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
            _, vname, blob_paths = self._scope(cursor, version_name)

            # Resolve symbol definition to ast_id (all matching definitions; those in this revision's tree when any)
            cursor.execute(
                """
                SELECT s.ast_id, s.type_id, t.name AS type_name, s.line_s, s.line_e, s.blob_id, s.linkage
                FROM m_symbol_def s
                JOIN m_type_descriptor t ON s.type_id = t.type_id
                WHERE s.name = %s;
                """,
                (symbol_name,),
            )
            def_rows = cursor.fetchall()
            in_tree = [d for d in def_rows if d["blob_id"] in blob_paths]
            defs = sorted(
                (
                    {
                        "file": (blob_paths.get(d["blob_id"]) or [None])[0],
                        "line_s": d["line_s"],
                        "line_e": d["line_e"],
                        "type_name": format_symbol_type(safe_decode(d["type_name"])),
                        "ast_id": d["ast_id"],
                    }
                    for d in (in_tree or def_rows)
                ),
                key=lambda d: (d["file"] is None, d["file"] or "", d["line_s"] or 0),
            )

            # Fallback to m_ast lookup if no explicit m_symbol_def record exists
            if not defs:
                cursor.execute(
                    """
                    SELECT a.ast_id, a.type_id, t.name AS type_name, bt.blob_id, bt.line_s, bt.line_e
                    FROM m_ast a
                    JOIN m_type_descriptor t ON a.type_id = t.type_id
                    JOIN m_tag tg ON a.ast_id = tg.ast_id
                    JOIN m_bridge_tag bt ON tg.tag_id = bt.tag_id
                    WHERE a.name = %s
                    LIMIT 200;
                    """,
                    (symbol_name,),
                )
                tag_rows = cursor.fetchall()
                tag_rows = [r for r in tag_rows if r["blob_id"] in blob_paths] or tag_rows
                tag_rows.sort(key=lambda r: ((blob_paths.get(r["blob_id"]) or ["~"])[0], r["line_s"] or 0))
                for f_row in tag_rows[:5]:
                    defs.append({
                        "file": (blob_paths.get(f_row["blob_id"]) or [None])[0],
                        "line_s": f_row["line_s"],
                        "line_e": f_row["line_e"],
                        "type_name": safe_decode(f_row["type_name"]),
                        "ast_id": f_row["ast_id"],
                    })

            ast_ids = [d["ast_id"] for d in defs if d.get("ast_id")]
            primary_ast_id = ast_ids[0] if ast_ids else 0

            ref_rows = []
            if ast_ids:
                # Key-first form of `m_symbol_ref WHERE ast_id IN (...)` (the view itself takes seconds per target)
                refs_sql, repeat = rows_of_targets_sql("m_symbol_ref", len(ast_ids))
                query_sql = f"""
                    SELECT r.role, r.line, r.char_s, fn.fname, r.blob_id AS fid, r.tag_id
                    FROM ({refs_sql}) r
                    LEFT JOIN m_file_name fn ON r.target_fnid = fn.fnid
                    ORDER BY fn.fname ASC, r.line ASC
                """
                params: list[Any] = list(ast_ids) * repeat
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

    def get_tag_timeline(self, tag_id: int, include_rc: bool = False) -> dict[str, Any]:
        """Cross-version history of the symbol a tag belongs to (see HistoryService)."""
        from webapp.backend.services.history_service import history_service
        return history_service.get_tag_history(tag_id, include_rc=include_rc)

    def get_symbol_history(
        self,
        version_name: str | None,
        path: str,
        ast_id: int | None = None,
        line: int | None = None,
        tag_id: int | None = None,
        name: str | None = None,
        include_rc: bool = False,
    ) -> dict[str, Any]:
        """Release-by-release history of the definition at path:line (see HistoryService)."""
        from webapp.backend.services.history_service import history_service
        return history_service.get_symbol_history(
            version_name, path, ast_id=ast_id, line=line, tag_id=tag_id, name=name, include_rc=include_rc,
        )

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
                    FROM m_bridge_checkpoint bc
                    JOIN m_file_name fn ON bc.fnid = fn.fnid
                    JOIN m_bridge_tag bt ON bc.blob_id = bt.blob_id
                    JOIN m_tag t ON bt.tag_id = t.tag_id
                    JOIN m_ast a ON t.ast_id = a.ast_id
                    WHERE bc.commit_id = %s AND fn.fname = %s AND a.type_id = 78
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
                    FROM m_bridge_checkpoint bc
                    JOIN m_file_name fn ON bc.fnid = fn.fnid
                    JOIN m_bridge_tag bt ON bc.blob_id = bt.blob_id
                    JOIN m_tag t ON bt.tag_id = t.tag_id
                    JOIN m_ast a ON t.ast_id = a.ast_id
                    WHERE bc.commit_id = %s AND fn.fname = %s AND a.type_id = 78
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
                    SELECT d.name, d.type_id, d.blob_id AS fid, d.line_s, d.line_e, fn.fname
                    FROM m_symbol_def d
                    LEFT JOIN m_bridge_checkpoint bc ON d.blob_id = bc.blob_id AND bc.commit_id = %s
                    LEFT JOIN m_file_name fn ON bc.fnid = fn.fnid
                    WHERE d.name IN ({format_strings})
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

