"""webapp/backend/security/sql.py - Identifier Validation."""
from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z0-9_.~+][A-Za-z0-9_.~+-]{0,127}")


def is_valid_identifier(name: str) -> bool:
    """Validate that a symbol or identifier consists of legal C/Kconfig chars."""
    if not name or not isinstance(name, str):
        return False
    if ".." in name or name in {".", ".."}:
        return False
    return bool(_IDENT_RE.fullmatch(name))
