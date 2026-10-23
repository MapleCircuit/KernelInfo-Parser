"""webapp/backend/routers/docs.py - Documentation Cross-References & kernel-doc Endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from webapp.backend.services.docs_service import DocsService

router = APIRouter(prefix="/api", tags=["docs"])
docs_service = DocsService()


@router.get("/docs/{version_name}/labels")
def get_doc_labels(version_name: str) -> dict[str, Any]:
    """Every `.. _label:` target under Documentation/ with its document path and section title."""
    return docs_service.get_label_index(version_name)


@router.get("/docs/{version_name}/kernel-doc")
def get_kernel_doc(
    version_name: str,
    path: str = Query(...),
    export: bool = Query(False),
    internal: bool = Query(False),
    functions: str = Query(""),
    doc: str = Query(""),
    no_identifiers: str = Query(""),
) -> dict[str, Any]:
    """kernel-doc comments of a source file, filtered like the `.. kernel-doc::` directive options."""
    return docs_service.get_kernel_doc(version_name, path, export, internal, functions, doc, no_identifiers)
