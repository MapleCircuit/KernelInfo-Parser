"""webapp/backend/services/callgraph_service.py - Bidirectional Caller / Callee Flow Graph."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from core.SymbolRows import rows_of_targets_sql
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)


class CallgraphService:
    """Provides bidirectional caller and callee query trees for C functions."""

    def get_function_callgraph(self, version_name: str, function_name: str) -> dict[str, Any]:
        """Retrieve incoming callers and outgoing callees for a function."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            cursor.execute(
                """
                SELECT s.def_id, s.ast_id, s.blob_id AS fid, fn.fname, s.line_s, s.line_e
                FROM m_symbol_def s
                LEFT JOIN m_bridge_checkpoint bc ON s.blob_id = bc.blob_id AND bc.commit_id = %s
                LEFT JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE s.name = %s
                ORDER BY (s.type_id IN (1, 2)) DESC
                LIMIT 1;
                """,
                (vid, function_name),
            )
            fn_def = cursor.fetchone()
            if not fn_def:
                return {
                    "function": function_name,
                    "function_name": function_name,
                    "version": vname,
                    "file": "",
                    "file_path": "",
                    "line_s": 0,
                    "line_e": 0,
                    "caller_count": 0,
                    "callee_count": 0,
                    "callers": [],
                    "callees": [],
                }

            target_ast_id = fn_def["ast_id"]
            target_fid = fn_def["fid"]
            file_path = safe_decode(fn_def["fname"])

            # 1. Callers (incoming): calls in this version's blobs, named by the AST of the tag they are in. The
            # references come key first (rows_of_targets_sql); filtering the m_symbol_ref view by ast_id is slow.
            refs_sql, repeat = rows_of_targets_sql("m_symbol_ref", 1)
            cursor.execute(
                f"""
                SELECT a.name AS caller_name, fn.fname AS file_name, r.line AS line_no
                FROM ({refs_sql}) r
                JOIN m_tag t ON r.tag_id = t.tag_id
                JOIN m_ast a ON a.ast_id = t.ast_id
                JOIN m_bridge_checkpoint bc ON bc.blob_id = r.blob_id AND bc.commit_id = %s
                LEFT JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE r.role = 3
                ORDER BY a.name ASC, r.line ASC;
                """,
                (target_ast_id,) * repeat + (vid,),
            )
            raw_callers = cursor.fetchall()
            callers_map: dict[tuple[str, str], dict[str, Any]] = {}
            for r in raw_callers:
                c_name = safe_decode(r["caller_name"])
                f_path = safe_decode(r["file_name"])
                key = (c_name, f_path)
                if key not in callers_map:
                    callers_map[key] = {
                        "name": c_name,
                        "caller_name": c_name,
                        "file_path": f_path,
                        "file_name": f_path,
                        "line_no": r["line_no"],
                        "line_s": r["line_no"],
                        "call_count": 0,
                        "lines": [],
                    }
                callers_map[key]["call_count"] += 1
                if r["line_no"] not in callers_map[key]["lines"]:
                    callers_map[key]["lines"].append(r["line_no"])

            callers = list(callers_map.values())

            # 2. Callees (outgoing): calls inside the function's lines, with the files defining each callee in
            # this version (joining the m_symbol_def view by ast_id would materialize it)
            cursor.execute(
                """
                SELECT r.ast_id, a.name AS callee_name, r.line AS line_no
                FROM m_symbol_ref r
                JOIN m_ast a ON a.ast_id = r.ast_id
                WHERE r.blob_id = %s AND r.line BETWEEN %s AND %s AND r.role = 3
                ORDER BY a.name ASC, r.line ASC;
                """,
                (target_fid, fn_def["line_s"], fn_def["line_e"]),
            )
            call_rows = cursor.fetchall()
            callee_ids = sorted({r["ast_id"] for r in call_rows})
            callee_files: dict[int, list[Any]] = {}
            if callee_ids:
                defs_sql, repeat = rows_of_targets_sql("m_symbol_def", len(callee_ids))
                cursor.execute(
                    f"""
                    SELECT DISTINCT d.ast_id, fn.fname
                    FROM ({defs_sql}) d
                    JOIN m_bridge_checkpoint bc ON bc.blob_id = d.blob_id AND bc.commit_id = %s
                    JOIN m_file_name fn ON bc.fnid = fn.fnid;
                    """,
                    tuple(callee_ids) * repeat + (vid,),
                )
                for d_row in cursor.fetchall():
                    callee_files.setdefault(d_row["ast_id"], []).append(d_row["fname"])
            raw_callees = [
                {"callee_name": r["callee_name"], "file_name": f_name, "line_no": r["line_no"]}
                for r in call_rows
                for f_name in (callee_files.get(r["ast_id"]) or [None])
            ]
            callees_map: dict[tuple[str, str], dict[str, Any]] = {}
            for r in raw_callees:
                c_name = safe_decode(r["callee_name"])
                f_path = safe_decode(r["file_name"])
                key = (c_name, f_path)
                if key not in callees_map:
                    callees_map[key] = {
                        "name": c_name,
                        "callee_name": c_name,
                        "file_path": f_path,
                        "file_name": f_path,
                        "line_no": r["line_no"],
                        "line": r["line_no"],
                        "call_count": 0,
                        "lines": [],
                    }
                callees_map[key]["call_count"] += 1
                if r["line_no"] not in callees_map[key]["lines"]:
                    callees_map[key]["lines"].append(r["line_no"])

            callees = list(callees_map.values())

            return {
                "function": function_name,
                "function_name": function_name,
                "version": vname,
                "file": file_path,
                "file_path": file_path,
                "line_s": fn_def["line_s"],
                "line_e": fn_def["line_e"],
                "caller_count": len(callers),
                "callee_count": len(callees),
                "callers": callers,
                "callees": callees,
            }
