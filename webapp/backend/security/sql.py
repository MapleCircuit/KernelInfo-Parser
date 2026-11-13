"""webapp/backend/security/sql.py - Identifier Validation."""
from __future__ import annotations
import re


def is_valid_identifier(name: str) -> bool:
    """Validate that a symbol or identifier consists of legal C/Kconfig chars."""
    if not name or not isinstance(name, str):
        return False
    return bool(re.match(r"^[A-Za-z0-9_.~+-]+$", name))
