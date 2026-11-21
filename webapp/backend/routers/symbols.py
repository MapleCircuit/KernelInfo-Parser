"""webapp/backend/routers/symbols.py - Symbol Search, XRefs, History & AST Container Inspection."""
from __future__ import annotations
import logging
from typing import Annotated, Any
from fastapi import APIRouter, HTTPException, Query
from webapp.backend.security.git import safe_rev
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.history_service import history_service
from webapp.backend.services.tree_resolver import DEFAULT_DEFINITION_ARCH

# ARCH whose definitions win when a symbol is defined per architecture (the KConfig engine's ARCH)
ArchParam = Annotated[str, Query(description="ARCH value ranking arch-specific definitions (default x86_64)")]

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["symbols"])
symbol_service = SymbolService()


@router.get("/symbols/{version_name}/search")
def search_symbols(version_name: str, q: str = Query(""), limit: int = Query(50)) -> dict[str, Any]:
    """Search symbol definitions by prefix."""
    results = symbol_service.search_symbols(version_name, q, limit=limit)
    return {"total": len(results), "symbols": results}


# Position of the clicked / hovered token: local scope resolution (webapp/API.md section 2.21) runs first
LineParam = Annotated[int | None, Query(ge=1, description="1-based line of the token in from_file: resolves "
                                                          "locals, parameters and struct fields first")]
ColParam = Annotated[int | None, Query(ge=0, description="1-based byte column of the token's first character")]


def _local_scope(version_name: str, symbol_name: str, arch: str, from_file: str | None, line: int | None,
                 col: int | None) -> tuple[dict[str, Any] | None, bool]:
    """(ScopeInfo of the token, position inside a function body) when the caller gives a position, else (None,
    False). Without `line` nothing resolves locally; an unsafe `from_file` is rejected."""
    if line is None or not from_file:
        return None, False
    clean = from_file.strip().strip("/")
    if not clean or not is_safe_rel_path(clean):
        raise HTTPException(status_code=400, detail="Invalid from_file path")
    safe_rev(version_name)
    if not is_valid_identifier(symbol_name):
        return None, False
    from webapp.backend.services.scope_service import scope_service
    try:
        return scope_service.locate(version_name, clean, line, col, symbol_name, arch)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable file falls back to the global lookups
        logger.debug("local scope of %s at %s:%s unavailable: %s", symbol_name, from_file, line, exc)
        return None, False


@router.get("/symbols/{version_name}/scope")
def get_symbol_scope(
    version_name: str,
    path: Annotated[str, Query(description="Repository-relative .c / .h file the token is in")],
    line: Annotated[int, Query(ge=1, description="1-based line of the token")],
    col: Annotated[int, Query(ge=0, description="1-based byte column of the token (0: the name's single use on the line)")] = 0,
    name: Annotated[str | None, Query(description="The token's text")] = None,
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
) -> dict[str, Any]:
    """ScopeInfo of a local variable, parameter, file variable or struct field at path:line:col
    (`{"found": false}` when the token is none the file's scope model knows)."""
    from webapp.backend.services.scope_service import scope_service
    safe_rev(version_name)
    clean = (path or "").strip().strip("/")
    if not clean or not is_safe_rel_path(clean):
        raise HTTPException(status_code=400, detail="Invalid path")
    if name is not None and not is_valid_identifier(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    return scope_service.resolve(version_name, clean, line, col, name, arch)


@router.get("/symbols/{version_name}/detail/{symbol_name}")
def get_symbol_detail(
    version_name: str,
    symbol_name: str,
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
    from_file: Annotated[str | None, Query(description="File the symbol is used in: definitions its #include chain reaches win")] = None,
    line: LineParam = None,
    col: ColParam = None,
    kind: Annotated[str | None, Query(pattern="^type$", description="type: struct/union/enum/typedef definitions rank first")] = None,
) -> dict[str, Any]:
    """The definition of a symbol in the revision's tree (ranked for `arch`); with a position, the declaration
    of the local variable, parameter or struct field there."""
    if kind != "type":
        info, _in_fn = _local_scope(version_name, symbol_name, arch, from_file, line, col)
        if info is not None:
            from webapp.backend.services.scope_service import detail_payload
            local = detail_payload(version_name, symbol_name, arch, from_file, info)
            if local is not None:
                return local
    return symbol_service.get_symbol_detail(version_name, symbol_name, arch, from_file, kind=kind)


@router.get("/symbols/{version_name}/hover/{symbol_name}")
def get_symbol_hover(
    version_name: str,
    symbol_name: str,
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
    from_file: Annotated[str | None, Query(description="File the symbol is used in")] = None,
    line: LineParam = None,
    col: ColParam = None,
) -> dict[str, Any]:
    """Hover card (G04): kind, signature, location and kernel-doc of the definition Go to Definition opens; with a
    position, the card of the local variable, parameter or struct field there."""
    info, _in_fn = _local_scope(version_name, symbol_name, arch, from_file, line, col)
    if info is not None:
        from webapp.backend.services.scope_service import hover_payload
        return hover_payload(version_name, symbol_name, arch, info)
    from webapp.backend.services.hover_service import hover_service
    return hover_service.hover(version_name, symbol_name, arch, from_file, symbol_service.get_symbol_detail)


@router.get("/symbols/{version_name}/xref/{symbol_name}")
def get_symbol_xref(
    version_name: str,
    symbol_name: str,
    limit: int | None = Query(None, ge=1, le=5000),
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
    from_file: Annotated[str | None, Query(description="File the symbol is used in (with line: local scope)")] = None,
    line: LineParam = None,
    col: ColParam = None,
) -> dict[str, Any]:
    """Itemized categorized references (calls, member_refs, type_usages) of a symbol; with a position, the
    declaration and in-scope uses of the local variable, parameter or struct field there."""
    info, in_fn = _local_scope(version_name, symbol_name, arch, from_file, line, col)
    if info is not None:
        from webapp.backend.services.scope_service import scope_service, xref_payload
        model = scope_service.model(version_name, from_file.strip().strip("/"))
        return xref_payload(version_name, symbol_name, arch, limit, model, info)
    return symbol_service.get_symbol_xref(version_name, symbol_name, limit, arch, name_fallback=not in_fn)


@router.get("/symbols/{version_name}/ast/{ast_id}")
def get_ast_tree(version_name: str, ast_id: int, depth: int = Query(3, ge=0, le=12)) -> dict[str, Any]:
    """Recursive AST container inspection."""
    return symbol_service.get_ast_tree(ast_id, depth)


@router.get("/symbols/{version_name}/history")
def get_symbol_history(
    version_name: str,
    path: str = Query(...),
    line: int | None = Query(None),
    ast_id: int | None = Query(None),
    name: str | None = Query(None, description="Symbol under the cursor: anchors on its definition"),
    include_rc: bool = Query(False, description="Also list release candidates"),
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
    branch: str | None = Query(None, description="Branch the timeline follows (default: chosen for version_name)"),
) -> dict[str, Any]:
    """Release-by-release history of the definition at path:line (a Kconfig entry returns its Kconfig history)."""
    return history_service.get_symbol_history(version_name, path, ast_id=ast_id, line=line, name=name,
                                              include_rc=include_rc, arch=arch, branch=branch or None)


@router.get("/history/commits")
def get_history_commits(
    path: str = Query(...),
    to: str = Query(..., description="Version (or commit) where the change is observed"),
    from_rev: str | None = Query(None, alias="from", description="Previous version of the timeline"),
    line_s: int | None = Query(None),
    line_e: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Commits of from..to that touched lines line_s..line_e (at `to`) of path (`git log -L`)."""
    return history_service.get_change_commits(path, to, from_rev, line_s, line_e, limit)


@router.get("/symbols/{version_name}/include/{ast_id}")
def get_include_symbols(
    version_name: str,
    ast_id: int,
    tag_id: int | None = Query(None),
    file_path: str | None = Query(None),
    line: int | None = Query(None),
    header: str | None = Query(None),
    arch: str | None = Query(None, description="ARCH whose #include closure locates the definitions (default x86_64)"),
) -> dict[str, Any]:
    """Imported symbols and target header file path of a CPPro_include AST node."""
    return symbol_service.get_include_symbols(
        version_name,
        ast_id,
        tag_id=tag_id,
        file_path=file_path,
        line=line,
        header=header,
        arch=arch,
    )
