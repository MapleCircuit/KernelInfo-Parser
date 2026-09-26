"""webapp/backend/routers/system.py - System & Database Instance State."""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any
from fastapi import APIRouter
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import safe_decode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/db", tags=["system"])

# Process-level fallback instance fingerprint if database table is missing or unseeded
_FALLBACK_INSTANCE_HASH = secrets.token_hex(32)
_FALLBACK_CREATED_AT = int(time.time())


@router.get("/instance")
def get_db_instance() -> dict[str, Any]:
    """Retrieve the unique database instance hash and initialization timestamp.

    Strictly read-only query. If the m_db_instance table is missing or unseeded,
    logs a warning and returns an in-memory process fallback without mutating the database.
    """
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT instance_hash, created_at FROM m_db_instance LIMIT 1;")
            row = cursor.fetchone()
            if row:
                return {
                    "instance_hash": safe_decode(row["instance_hash"]),
                    "created_at": int(row["created_at"]),
                }
            logger.warning("m_db_instance table is empty; returning process fallback instance hash.")
    except Exception as err:
        logger.warning("Failed to query m_db_instance from database (%s); returning fallback.", err)

    return {
        "instance_hash": _FALLBACK_INSTANCE_HASH,
        "created_at": _FALLBACK_CREATED_AT,
    }
