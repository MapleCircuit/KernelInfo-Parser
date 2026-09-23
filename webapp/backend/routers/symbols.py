"""webapp/backend/routers/symbols.py - Symbol Search, XRefs & AST Container Inspection."""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query
from webapp.backend.services.symbol_service import SymbolService

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
def get_symbol_detail(version_name: str, symbol_name: str) -> dict[str, Any]:
    """Get authoritative definition and usage overview for a symbol."""
    return symbol_service.get_symbol_detail(version_name, symbol_name)


@router.get("/version/{version_name}/xref/{symbol_name}")
@router.get("/symbols/{version_name}/xref/{symbol_name}")
def get_symbol_xref(
    version_name: str,
    symbol_name: str,
    limit: int | None = Query(None),
) -> dict[str, Any]:
    """Get itemized categorized references (calls, member_refs, type_usages) for a symbol."""
    return symbol_service.get_symbol_xref(version_name, symbol_name, limit)


@router.get("/ast/{ast_id}/tree")
@router.get("/symbols/{version_name}/ast/{ast_id}")
def get_ast_tree(
    ast_id: int,
    version: str = Query("v3.0", alias="version_name"),
    depth: int = Query(3),
    version_name: str | None = None,
) -> dict[str, Any]:
    """Recursive AST container inspection."""
    v = version_name or version
    return symbol_service.get_ast_tree(ast_id, depth, v)


@router.get("/tag/{tag_id}/timeline")
def get_tag_timeline(tag_id: int) -> dict[str, Any]:
    """Trace cross-version tag evolution history."""
    return symbol_service.get_tag_timeline(tag_id)


@router.get("/symbols/{version_name}/include/{ast_id}")
@router.get("/include/{ast_id}")
def get_include_symbols(
    ast_id: int,
    version: str = Query("v3.0", alias="version_name"),
    version_name: str | None = None,
) -> dict[str, Any]:
    """Retrieve imported symbols and target header file path for a CPPro_include AST node."""
    v = version_name or version
    return symbol_service.get_include_symbols(v, ast_id)

