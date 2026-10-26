"""webapp/backend/security/git.py - Git argument hardening for caller-supplied revisions.

Every endpoint takes a revision as a version name, a branch or a commit SHA, and the services pass
it to git as a positional argument (`git blame <rev> -- <path>`). Git parses an argument starting
with "-" as an option, so an unvalidated revision turns into `--contents=<file>` (git blame prints
that file) or `--output=<file>` (git log writes it). Revisions are therefore validated here before
they reach a command line, and `<rev>:<path>` specs before they reach a `cat-file --batch` stream.
"""
from __future__ import annotations

from fastapi import HTTPException

from core.globalstuff import is_safe_git_rev, is_safe_git_spec


def safe_rev(rev: str, what: str = "revision") -> str:
    """Return rev unchanged, or raise HTTP 400 when it is not a plain tag / branch / commit SHA."""
    if not is_safe_git_rev(rev):
        raise HTTPException(status_code=400, detail=f"Invalid {what}: must be a tag, branch or commit SHA.")
    return rev


def is_safe_rev(rev: object) -> bool:
    """Non-raising form of safe_rev(), for services that fall back instead of failing a request."""
    return is_safe_git_rev(rev)


def safe_blob_spec(spec: str) -> str:
    """Return a `<rev>:<path>` spec unchanged, or raise HTTP 400 when it would desync cat-file."""
    if not is_safe_git_spec(spec):
        raise HTTPException(status_code=400, detail="Invalid object spec.")
    return spec


__all__ = ["safe_rev", "is_safe_rev", "safe_blob_spec"]
