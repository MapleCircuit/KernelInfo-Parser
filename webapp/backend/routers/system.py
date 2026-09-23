"""webapp/backend/routers/system.py - System & Database Instance State."""
from __future__ import annotations
import time
import secrets
from typing import Any
from fastapi import APIRouter
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import safe_decode

router = APIRouter(prefix="/api/db", tags=["system"])


@router.get("/instance")
def get_db_instance() -> dict[str, Any]:
    """Retrieve the unique database instance hash and initialization timestamp.
    
    If the table does not exist yet (e.g. legacy DB), it creates it and persists
    an initial unique instance hash.
    """
    with get_db_cursor(commit=True) as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS m_db_instance (
                instance_hash VARCHAR(64) NOT NULL PRIMARY KEY,
                created_at BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
        """)
        cursor.execute("SELECT instance_hash, created_at FROM m_db_instance LIMIT 1;")
        row = cursor.fetchone()
        if not row:
            token = secrets.token_hex(32)
            now = int(time.time())
            cursor.execute("INSERT INTO m_db_instance (instance_hash, created_at) VALUES (%s, %s);", (token, now))
            return {"instance_hash": token, "created_at": now}
        
        return {
            "instance_hash": safe_decode(row["instance_hash"]),
            "created_at": int(row["created_at"])
        }
