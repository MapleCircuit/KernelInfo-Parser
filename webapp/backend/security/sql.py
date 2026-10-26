"""webapp/backend/security/sql.py - Parameterized Query Defense & Input Sanitization."""
from __future__ import annotations
import re


def escape_like(value: str) -> str:
    """Escape the LIKE wildcards in value so it matches literally; length is left untouched.

    Use this for values that must keep their full length (a path prefix); `sanitize_like_query`
    adds the search-query truncation on top. An unescaped `%` turns an anchored prefix match into
    a full scan, and `_` silently widens what matches.
    """
    if not value or not isinstance(value, str):
        return ""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sanitize_like_query(query: str, max_length: int = 100) -> str:
    """Sanitize and escape wildcards in user search query for safe SQL LIKE clauses."""
    if not query or not isinstance(query, str):
        return ""
    # Truncate first, then escape, so an escape sequence is never cut in half
    return escape_like(query.strip()[:max_length])


def is_valid_identifier(name: str) -> bool:
    """Validate that a symbol or identifier consists of legal C/Kconfig chars."""
    if not name or not isinstance(name, str):
        return False
    return bool(re.match(r"^[A-Za-z0-9_.~+-]+$", name))
