"""webapp/backend/services/callgraph_service.py - Bidirectional Caller / Callee Flow Graph."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
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
                SELECT s.def_id, s.ast_id, s.fid, fn.fname, s.line_s, s.line_e
                FROM m_symbol_def s
                JOIN m_bridge_file bf ON s.fid = bf.fid AND bf.vid = s.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE s.vid = %s AND s.name = %s
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

            # 1. Callers (incoming)
            cursor.execute(
                """
                SELECT caller_sym.name AS caller_name, fn.fname AS file_name, r.line AS line_no
                FROM m_symbol_ref r
                JOIN m_tag t ON r.tag_id = t.tag_id
                JOIN m_symbol_def caller_sym ON t.ast_id = caller_sym.ast_id AND caller_sym.vid = r.vid
                JOIN m_bridge_file bf ON caller_sym.fid = bf.fid AND bf.vid = caller_sym.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE r.vid = %s AND r.ast_id = %s AND r.role = 3
                ORDER BY caller_sym.name ASC, r.line ASC;
                """,
                (vid, target_ast_id),
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

            # 2. Callees (outgoing)
            cursor.execute(
                """
                SELECT callee_sym.name AS callee_name, fn.fname AS file_name, r.line AS line_no
                FROM m_symbol_ref r
                JOIN m_symbol_def callee_sym ON r.ast_id = callee_sym.ast_id AND callee_sym.vid = r.vid
                JOIN m_bridge_file bf ON callee_sym.fid = bf.fid AND bf.vid = callee_sym.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE r.vid = %s AND r.fid = %s AND r.line BETWEEN %s AND %s AND r.role = 3
                ORDER BY callee_sym.name ASC, r.line ASC;
                """,
                (vid, target_fid, fn_def["line_s"], fn_def["line_e"]),
            )
            raw_callees = cursor.fetchall()
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
