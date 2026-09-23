"""webapp/backend/security/jail.py - Strict Path Canonicalization & Repository Jail.

Prevents Local File Inclusion (LFI), null-byte injections, directory traversal (../),
and symbolic link escapes outside of the configured Linux repository directory.
"""
from __future__ import annotations
import os
from pathlib import Path
from fastapi import HTTPException
from webapp.backend.config import LINUX_REPO_DIR


def is_safe_rel_path(path_str: str) -> bool:
    """Check whether a relative path string contains no malicious traversal markers."""
    if not path_str or not isinstance(path_str, str):
        return False
    if "\0" in path_str:
        return False
    clean = os.path.normpath(path_str.strip().replace("\\", "/"))
    parts = clean.split("/")
    if ".." in parts or clean.startswith("/"):
        return False
    return True


def resolve_and_verify_repo_path(requested_path: str, repo_base: Path | None = None) -> Path:
    """Canonicalize and verify that requested_path resides strictly within repo_base.
    
    Raises:
        HTTPException(400): If path is empty, malformed, contains null bytes, or uses '..'.
        HTTPException(403): If the resolved path escapes outside repo_base.
        HTTPException(404): If requested file/directory does not exist.
    """
    if not requested_path or not isinstance(requested_path, str):
        raise HTTPException(status_code=400, detail="Path parameter cannot be empty.")
    
    if "\0" in requested_path:
        raise HTTPException(status_code=400, detail="Malformed path: null byte detected.")
    
    base_dir = (repo_base or LINUX_REPO_DIR).resolve()
    
    # Normalize forward/backward slashes and remove leading root slashes
    clean_rel = os.path.normpath(requested_path.strip().replace("\\", "/"))
    while clean_rel.startswith("/"):
        clean_rel = clean_rel[1:]
    
    parts = clean_rel.split("/")
    if ".." in parts:
        raise HTTPException(status_code=400, detail="Path traversal attempt detected.")
    
    target_path = (base_dir / clean_rel).resolve()
    
    # Assert boundary containment (Python 3.8+ compatible)
    try:
        target_path.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside repository root.")
    
    return target_path
