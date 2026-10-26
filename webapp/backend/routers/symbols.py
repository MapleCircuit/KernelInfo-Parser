"""webapp/backend/routers/symbols.py - Symbol Search, XRefs & AST Container Inspection."""
from __future__ import annotations
from typing import Annotated, Any
from fastapi import APIRouter, Query
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.history_service import history_service
from webapp.backend.services.tree_resolver import DEFAULT_DEFINITION_ARCH

# ARCH whose definitions win when a symbol is defined per architecture (the KConfig engine's ARCH)
ArchParam = Annotated[str, Query(description="ARCH value ranking arch-specific definitions (default x86_64)")]

router = APIRouter(prefix="/api", tags=["symbols"])
symbol_service = SymbolService()


@router.get("/symbols/search")
@router.get("/symbols/{version_name}/search")
def search_symbols(
    version: str = Query("v3.0", alias="version_name"),
    q: str = Query(""),
    limit: int = Query(50),
    version_name: str | None = None,
) -> dict[str, Any]:
    """Search symbol definitions by prefix or substring."""
    v = version_name or version
    results = symbol_service.search_symbols(v, q, limit)
    return {"total": len(results), "symbols": results}


@router.get("/symbols/lookup")
@router.get("/symbols/{version_name}/lookup")
@router.get("/version/{version_name}/symbol_lookup")
@router.get("/version/{version_name}/lookup")
def lookup_symbols(
    version: str = Query("v3.0", alias="version_name"),
    q: str = Query(""),
    limit: int = Query(20),
    version_name: str | None = None,
) -> list[str]:
    """Typeahead symbol suggestion lookup."""
    v = version_name or version
    return symbol_service.lookup_symbols(v, q, limit)


@router.get("/version/{version_name}/symbol/{symbol_name}")
@router.get("/symbols/{version_name}/detail/{symbol_name}")
def get_symbol_detail(
    version_name: str,
    symbol_name: str,
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
    from_file: Annotated[str | None, Query(description="File the symbol is used in: definitions its #include chain reaches win")] = None,
) -> dict[str, Any]:
    """Get authoritative definition and usage overview for a symbol."""
    return symbol_service.get_symbol_detail(version_name, symbol_name, arch, from_file)


@router.get("/version/{version_name}/xref/{symbol_name}")
@router.get("/symbols/{version_name}/xref/{symbol_name}")
def get_symbol_xref(
    version_name: str,
    symbol_name: str,
    limit: int | None = Query(None, ge=1, le=5000),
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
) -> dict[str, Any]:
    """Get itemized categorized references (calls, member_refs, type_usages) for a symbol."""
    return symbol_service.get_symbol_xref(version_name, symbol_name, limit, arch)


@router.get("/ast/{ast_id}/tree")
@router.get("/symbols/{version_name}/ast/{ast_id}")
def get_ast_tree(
    ast_id: int,
    version: str = Query("v3.0", alias="version_name"),
    depth: int = Query(3, ge=0, le=12),
    version_name: str | None = None,
) -> dict[str, Any]:
    """Recursive AST container inspection."""
    v = version_name or version
    return symbol_service.get_ast_tree(ast_id, depth, v)


@router.get("/symbols/{version_name}/history")
def get_symbol_history(
    version_name: str,
    path: str = Query(...),
    line: int | None = Query(None),
    ast_id: int | None = Query(None),
    name: str | None = Query(None, description="Symbol under the cursor: anchors on its definition"),
    include_rc: bool = Query(False, description="Also list release candidates"),
    arch: ArchParam = DEFAULT_DEFINITION_ARCH,
) -> dict[str, Any]:
    """Release-by-release history of the definition at path:line (a Kconfig entry returns its Kconfig history)."""
    return history_service.get_symbol_history(version_name, path, ast_id=ast_id, line=line, name=name,
                                              include_rc=include_rc, arch=arch)


@router.get("/tag/{tag_id}/timeline")
def get_tag_timeline(tag_id: int, include_rc: bool = Query(False)) -> dict[str, Any]:
    """Release-by-release history of the definition a tag belongs to."""
    return history_service.get_tag_history(tag_id, include_rc=include_rc)


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
@router.get("/include/{ast_id}")
@router.get("/version/{version_name}/include/{ast_id}/symbols")
@router.get("/version/{version_name}/include/{ast_id}")
def get_include_symbols(
    ast_id: int,
    version: str = Query("v3.0", alias="version_name"),
    version_name: str | None = None,
    tag_id: int | None = Query(None),
    file_path: str | None = Query(None),
    line: int | None = Query(None),
    header: str | None = Query(None),
) -> dict[str, Any]:
    """Retrieve imported symbols and target header file path for a CPPro_include AST node."""
    v = version_name or version
    return symbol_service.get_include_symbols(
        v,
        ast_id,
        tag_id=tag_id,
        file_path=file_path,
        line=line,
        header=header,
    )

