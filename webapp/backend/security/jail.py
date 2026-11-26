"""webapp/backend/security/jail.py - Strict Path Canonicalization & Repository Jail.

Prevents Local File Inclusion (LFI), null-byte injections, directory traversal (../),
and symbolic link escapes outside of the configured Linux repository directory.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from webapp.backend.config import get_repo_dir


def is_safe_rel_path(path_str: str) -> bool:
    """Check whether a relative path string contains no malicious traversal or control markers."""
    if not path_str or not isinstance(path_str, str) or not path_str.strip() or path_str != path_str.strip():
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in path_str):
        return False
    raw = path_str.strip().replace("\\", "/")
    if raw.startswith("/") or ".." in raw.split("/"):
        return False
    clean = os.path.normpath(raw)
    parts = clean.split("/")
    return not (clean in ("", ".", "..") or ".." in parts or clean.startswith("/"))


def resolve_and_verify_repo_path(requested_path: str, repo_base: Path | None = None) -> Path:
    """Canonicalize and verify that requested_path resides strictly within repo_base.

    Existence is deliberately not checked: callers resolve paths for a past revision, where the
    file may be absent from the current work tree (`git blame <rev> -- <path>` still works). A
    caller that needs the file on disk checks for itself.

    Raises:
        HTTPException(400): If path is empty, malformed, contains control bytes, or uses '..'.
        HTTPException(403): If the resolved path escapes outside repo_base.
    """
    if not requested_path or not isinstance(requested_path, str) or not requested_path.strip():
        raise HTTPException(status_code=400, detail="Path parameter cannot be empty.")

    if "\0" in requested_path:
        raise HTTPException(status_code=400, detail="Malformed path: null byte detected.")

    if any(ord(c) < 32 or ord(c) == 127 for c in requested_path):
        raise HTTPException(status_code=400, detail="Malformed path: control character detected.")

    base_dir = (repo_base or get_repo_dir()).resolve()

    # Reject raw traversal segments BEFORE normpath collapses leading slashes (e.g. "/../../etc/passwd")
    raw_norm = requested_path.strip().replace("\\", "/")
    if ".." in raw_norm.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal attempt detected.")

    clean_rel = os.path.normpath(raw_norm.lstrip("/"))
    parts = clean_rel.split("/")
    if not clean_rel or clean_rel == "." or clean_rel.startswith("/") or ".." in parts:
        raise HTTPException(status_code=400, detail="Path traversal attempt detected.")

    target_path = (base_dir / clean_rel).resolve()
    if target_path == base_dir:
        raise HTTPException(status_code=400, detail="Path parameter cannot resolve to repository root.")

    # Assert boundary containment (Python 3.8+ compatible)
    try:
        target_path.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside repository root.")

    return target_path
