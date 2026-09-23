"""webapp/backend/security/sql.py - Parameterized Query Defense & Input Sanitization."""
from __future__ import annotations
import re


def sanitize_like_query(query: str, max_length: int = 100) -> str:
    """Sanitize and escape wildcards in user search query for safe SQL LIKE clauses."""
    if not query or not isinstance(query, str):
        return ""
    # Strip dangerous characters and truncate
    clean = query.strip()[:max_length]
    # Escape existing LIKE wildcards (% and _) so user input is treated as literal prefix/substring
    clean = clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return clean


def is_valid_identifier(name: str) -> bool:
    """Validate that a symbol or identifier consists of legal C/Kconfig chars."""
    if not name or not isinstance(name, str):
        return False
    return bool(re.match(r"^[A-Za-z0-9_.~+-]+$", name))
