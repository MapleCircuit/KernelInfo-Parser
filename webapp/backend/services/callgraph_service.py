"""webapp/backend/services/callgraph_service.py - Bidirectional Caller / Callee Flow Graph."""
from __future__ import annotations
from typing import Any
from core.globalstuff import SymbolLinkage
from core.SymbolRows import rows_of_targets_sql
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import safe_decode
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.tree_resolver import best_path, definition_arch, definition_rank

# m_type_descriptor C_functionproto / C_functionprotodecl: function definitions and prototypes
FUNCTION_TYPES = (20, 21)
# Reference targets per callgraph: calls bind to the declaration visible at the call site (a header
# prototype), so every in-tree declaration of the function is one
MAX_TARGETS = 8
# Call sites kept per direction; a function like printk has tens of thousands
MAX_CALL_SITES = 2000


def _group_calls(rows: list[tuple[str, str | None, int]]) -> tuple[list[dict[str, Any]], bool]:
    """(name, file, line) call sites grouped per (function, file), at most MAX_CALL_SITES sites."""
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    truncated = len(rows) > MAX_CALL_SITES
    for name, path, line in sorted(rows, key=lambda r: (r[0] or "", r[1] or "", r[2] or 0))[:MAX_CALL_SITES]:
        entry = groups.get((name, path))
        if entry is None:
            entry = groups[(name, path)] = {"name": name, "file_path": path, "line_no": line, "call_count": 0, "lines": {}}
        entry["call_count"] += 1
        entry["lines"][line] = None
    for entry in groups.values():
        entry["lines"] = list(entry["lines"])
    return list(groups.values()), truncated


class CallgraphService:
    """Provides bidirectional caller and callee query trees for C functions."""

    def get_function_callgraph(self, version_name: str, function_name: str) -> dict[str, Any]:
        """Incoming callers and outgoing callees of a function, within the revision's tree."""
        with get_db_cursor() as cursor:
            _, vname, blob_paths = SymbolService._scope(cursor, version_name)
            _, scope, bits = definition_arch(vname, None)
            empty = {
                "function": function_name,
                "version": vname,
                "file_path": "",
                "line_s": 0,
                "line_e": 0,
                "callers": [],
                "callees": [],
                "truncated": False,
            }

            cursor.execute(
                """
                SELECT s.ast_id, s.blob_id, s.line_s, s.line_e, s.type_id, s.linkage, a.name AS ast_name
                FROM m_symbol_def s
                LEFT JOIN m_ast a ON a.ast_id = s.ast_id
                WHERE s.name = %s;
                """,
                (function_name,),
            )
            defs = [
                r for r in cursor.fetchall()
                if r["blob_id"] in blob_paths and r["ast_id"] and r["type_id"] in FUNCTION_TYPES
                and int(r["linkage"] or 0) != SymbolLinkage.MacroOrKernelDef
            ]
            if not defs:
                return empty
            for r in defs:
                r["fname"] = best_path(blob_paths[r["blob_id"]], scope, bits)
            # The definition with a body: never a prototype, a real definition before a header inline stub
            fn_def = min(defs, key=lambda r: (
                int(r["linkage"] or 0) == SymbolLinkage.PrototypeDecl,
                int(r["linkage"] or 0) == SymbolLinkage.InlineHeaderDef,
                definition_rank(r["fname"], r.get("linkage"), r["line_s"], scope, bits),
            ))
            # Reference targets: the declarations that are the function itself (a marker macro's
            # shared AST would bring the references of every function it marks)
            targets = [fn_def["ast_id"]] + [
                r["ast_id"] for r in sorted(defs, key=lambda r: definition_rank(r["fname"], r.get("linkage"), r["line_s"], scope, bits))
                if safe_decode(r["ast_name"]) == function_name
            ]
            targets = list(dict.fromkeys(targets))[:MAX_TARGETS]

            # 1. Callers: calls in this revision's blobs, named by the AST of the tag they are in. The
            # references come key first (rows_of_targets_sql); filtering the m_symbol_ref view by ast_id is slow.
            refs_sql, repeat = rows_of_targets_sql("m_symbol_ref", len(targets))
            cursor.execute(
                f"""
                SELECT a.name AS caller_name, r.blob_id, r.line AS line_no
                FROM ({refs_sql}) r
                JOIN m_tag t ON r.tag_id = t.tag_id
                JOIN m_ast a ON a.ast_id = t.ast_id
                WHERE r.role = 3;
                """,
                tuple(targets) * repeat,
            )
            caller_sites = [
                (safe_decode(r["caller_name"]), best_path(blob_paths[r["blob_id"]], scope, bits), r["line_no"])
                for r in cursor.fetchall() if r["blob_id"] in blob_paths
            ]
            callers, callers_truncated = _group_calls(caller_sites)

            # 2. Callees: calls inside the function's lines, with the files defining each callee in this
            # revision (joining the m_symbol_def view by ast_id would materialize it)
            cursor.execute(
                """
                SELECT r.ast_id, a.name AS callee_name, r.line AS line_no
                FROM m_symbol_ref r
                JOIN m_ast a ON a.ast_id = r.ast_id
                WHERE r.blob_id = %s AND r.line BETWEEN %s AND %s AND r.role = 3;
                """,
                (fn_def["blob_id"], fn_def["line_s"], fn_def["line_e"]),
            )
            call_rows = cursor.fetchall()
            callee_ids = sorted({r["ast_id"] for r in call_rows})
            callee_files: dict[int, list[str]] = {}
            if callee_ids:
                defs_sql, repeat = rows_of_targets_sql("m_symbol_def", len(callee_ids))
                cursor.execute(f"SELECT DISTINCT d.ast_id, d.blob_id FROM ({defs_sql}) d;", tuple(callee_ids) * repeat)
                for d_row in cursor.fetchall():
                    if d_row["blob_id"] in blob_paths:
                        path = best_path(blob_paths[d_row["blob_id"]], scope, bits)
                        files = callee_files.setdefault(d_row["ast_id"], [])
                        if path not in files:
                            files.append(path)
            callee_sites = [
                (safe_decode(r["callee_name"]), path, r["line_no"])
                for r in call_rows
                for path in (callee_files.get(r["ast_id"]) or [None])
            ]
            callees, callees_truncated = _group_calls(callee_sites)

            return {
                **empty,
                "file_path": fn_def["fname"],
                "line_s": fn_def["line_s"],
                "line_e": fn_def["line_e"],
                "callers": callers,
                "callees": callees,
                "truncated": callers_truncated or callees_truncated,
            }
