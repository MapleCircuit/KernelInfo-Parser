"""webapp/backend/services/ops_service.py - Ops implementations: who fills a struct's function-pointer field (N1, webapp/API.md section 4.7).

Answered from the source index of the revision (section 2.19): every `struct <S> <name>[...] = { ... }` initializer of the
tree with its designated `.field = function` / `= &function` entries (arrays of structs, and one nested level as
`outer.inner`), the value resolved like a call made in that file (its own static function first). Values that are not
functions (`THIS_MODULE`, other tables) are left out; positional initializers are counted, not resolved; tables generated
by macros (`DEFINE_SIMPLE_ATTRIBUTE`, `*_FOPS`) are not seen.

    index_state("v3.0", "file_operations")  -> job state; result {struct, tables, files, fields: {field: [impl]}, positional_skipped}
    uses_state("v3.0", "ext4_file_write")   -> job state; result {function, uses: [{struct, field, table, table_line, line, path}]}
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

_IDENT_RE = re.compile(r"^[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*$")


def _check_ident(name: str, what: str) -> str:
    name = (name or "").strip()
    if not _IDENT_RE.match(name) or len(name) > 128:
        raise HTTPException(status_code=400, detail=f"{what} must be a valid C or Rust identifier")
    return name


def _revision(version: str, wait: float) -> dict[str, Any]:
    from webapp.backend.services.kapi_service import resolve
    from webapp.backend.services.source_index import revision_state
    return revision_state(resolve(version), wait=wait)


def index_state(version: str, struct: str, wait: float = 0.0) -> dict[str, Any]:
    """The initializers of `struct`: {state: "done", result} once the revision's source index is in memory."""
    struct = _check_ident(struct, "struct")
    state = _revision(version, wait)
    if state.get("state") != "done":
        return state
    return {"state": "done", "result": state["index"].struct_report(struct)}


def uses_state(version: str, function: str, wait: float = 0.0) -> dict[str, Any]:
    """The ops tables using `function`: {state: "done", result} once the revision's source index is in memory."""
    function = _check_ident(function, "function")
    state = _revision(version, wait)
    if state.get("state") != "done":
        return state
    return {"state": "done", "result": {"version": version, "function": function, "uses": state["index"].uses_of(function),
                                        "truncated": False}}
