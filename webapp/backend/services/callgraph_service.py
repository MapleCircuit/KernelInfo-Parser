"""webapp/backend/services/callgraph_service.py - Bidirectional Caller / Callee Flow Graph."""
from __future__ import annotations
from typing import Any
from core.globalstuff import ASTT, SymbolLinkage
from db_engine.spec import Join, between
from webapp.backend.database.pool import get_reader
from webapp.backend.database.helpers import safe_decode
from webapp.backend.services.symbol_service import SymbolService, binding_ref_asts
from webapp.backend.services.tree_resolver import best_path, definition_arch, definition_rank

# m_type_descriptor C_functionproto / C_functionprotodecl / Rust_Fn / ASM_Label: function definitions and prototypes
FUNCTION_TYPES = (int(ASTT.C_functionproto), int(ASTT.C_functionprotodecl), int(ASTT.Rust_Fn), int(ASTT.ASM_Label))
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
        with get_reader() as reader:
            _, vname, blob_paths = SymbolService._scope(reader, version_name)
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

            def_rows = reader.select_rows(
                ("m_symbol_def", "s"),
                {"ast_id": "s.ast_id", "blob_id": "s.blob_id", "line_s": "s.line_s", "line_e": "s.line_e",
                 "type_id": "s.type_id", "linkage": "s.linkage", "ast_name": "a.name"},
                {"s.name": function_name},
                joins=[Join(("m_ast", "a"), {"a.ast_id": "s.ast_id"}, "left")],
            )
            defs = [
                r for r in def_rows
                if r["blob_id"] in blob_paths and r["ast_id"] and r["type_id"] in FUNCTION_TYPES
                and int(r["linkage"] or 0) != SymbolLinkage.MacroOrKernelDef
            ]
            if not defs:
                return empty
            for r in defs:
                r["fname"] = best_path(blob_paths[r["blob_id"]], scope, bits)
            # The definition with a body: never a prototype, a real definition before a header inline stub, C
            # before a Rust function of the same name (a method such as `open`), a Rust function C calls
            # (`#[no_mangle]`) before its C prototype
            fn_def = min(defs, key=lambda r: (
                int(r["linkage"] or 0) == SymbolLinkage.PrototypeDecl,
                int(r["linkage"] or 0) == SymbolLinkage.InlineHeaderDef,
                r["type_id"] == int(ASTT.Rust_Fn),
                definition_rank(r["fname"], r.get("linkage"), r["line_s"], scope, bits),
            ))
            # Reference targets: the declarations that are the function itself (a marker macro's
            # shared AST would bring the references of every function it marks)
            targets = [fn_def["ast_id"]] + [
                r["ast_id"] for r in sorted(defs, key=lambda r: definition_rank(r["fname"], r.get("linkage"), r["line_s"], scope, bits))
                if safe_decode(r["ast_name"]) == function_name
            ]
            targets = list(dict.fromkeys(targets))[:MAX_TARGETS]
            # Rust calls through bindgen (`bindings::foo(..)`) reference the function's name
            targets = list(dict.fromkeys(targets + binding_ref_asts(reader, function_name)))

            # 1. Callers: calls in this revision's blobs, named by the AST of the tag they are in. The
            # references come key first (symbol_refs_of_targets); filtering the m_symbol_ref view by ast_id is slow.
            caller_refs = reader.symbol_refs_of_targets(targets, role=3, with_caller=True)
            caller_sites = []
            for r in caller_refs:
                if r["fid"] not in blob_paths:
                    continue
                path = best_path(blob_paths[r["fid"]], scope, bits)
                caller_name = (safe_decode(r.get("caller")) or "").strip()
                line_no = r["line"]

                # For Rust method callers inside impl Trait for Type, resolve the actual method name
                if caller_name.startswith("impl ") or " for " in caller_name or path.endswith(".rs"):
                    if caller_name.startswith("impl ") or " for " in caller_name or not caller_name:
                        from webapp.backend.services.git_reader import git_reader
                        from parser.rust_ast.rust_pure_extractor import extract_rust_outline
                        from webapp.backend.services.outline_service import flatten
                        raw, is_dir, _ = git_reader.read_file(vname, path)
                        if raw and not is_dir:
                            items = flatten(extract_rust_outline(raw))
                            methods = [
                                it["name"] for it in items
                                if it.get("kind") == "function" and it.get("line_s", 0) <= line_no <= it.get("line_e", line_no)
                            ]
                            if methods:
                                caller_name = methods[0]

                # Handle loose callers where tag_id = 0 in assembly files
                if (not caller_name or caller_name == "<unknown>") and path.endswith((".S", ".s")):
                    from webapp.backend.services.git_reader import git_reader
                    from parser.asm_ast.asm_pure_extractor import extract_asm_outline
                    from webapp.backend.services.outline_service import flatten
                    raw, is_dir, _ = git_reader.read_file(vname, path)
                    if raw and not is_dir:
                        items = flatten(extract_asm_outline(raw))
                        funcs = [
                            it["name"] for it in items
                            if it.get("kind") == "function" and it.get("line_s", 0) <= line_no <= it.get("line_e", line_no)
                        ]
                        if funcs:
                            caller_name = funcs[0]

                # Filter out inner block tags named "{}"
                if caller_name == "{}":
                    continue

                if not caller_name:
                    caller_name = "<unknown>"

                caller_sites.append((caller_name, path, line_no))

            callers, callers_truncated = _group_calls(caller_sites)

            # 2. Callees: calls inside the function's lines, with the files defining each callee in this
            # revision (joining the m_symbol_def view by ast_id would materialize it)
            call_rows = reader.select_rows(
                ("m_symbol_ref", "r"),
                {"ast_id": "r.ast_id", "callee_name": "a.name", "callee_type": "a.type_id", "line_no": "r.line"},
                {"r.blob_id": fn_def["blob_id"], "r.line": between(fn_def["line_s"], fn_def["line_e"]), "r.role": 3},
                joins=[Join(("m_ast", "a"), {"a.ast_id": "r.ast_id"})],
            )
            callee_ids = sorted({r["ast_id"] for r in call_rows})
            callee_files: dict[int, list[str]] = {}
            if callee_ids:
                for def_ast, def_blob in sorted(reader.symbol_def_blobs_of_targets(callee_ids)):
                    if def_blob in blob_paths:
                        path = best_path(blob_paths[def_blob], scope, bits)
                        files = callee_files.setdefault(def_ast, [])
                        if path not in files:
                            files.append(path)
            # A Rust function's calls of C functions (`bindings::foo(..)`) name the callee only: its files are
            # those of the definitions of that name
            binding_callees = {
                r["ast_id"]: safe_decode(r["callee_name"]) for r in call_rows
                if r["ast_id"] not in callee_files and int(r.get("callee_type") or 0) == int(ASTT.Rust_BindingRef)
            }
            if binding_callees:
                names = sorted(set(binding_callees.values()))
                named_defs = reader.select_rows(
                    "m_symbol_def", ["name", "blob_id"], {"name": names, "type_id": list(FUNCTION_TYPES)},
                    distinct=True,
                )
                by_name: dict[str, list[str]] = {}
                for d_row in named_defs:
                    if d_row["blob_id"] in blob_paths:
                        path = best_path(blob_paths[d_row["blob_id"]], scope, bits)
                        files = by_name.setdefault(safe_decode(d_row["name"]), [])
                        if path not in files:
                            files.append(path)
                for ast_id, name in binding_callees.items():
                    if by_name.get(name):
                        callee_files[ast_id] = by_name[name]
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
