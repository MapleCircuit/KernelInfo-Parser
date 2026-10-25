"""Tests package."""
from __future__ import annotations

import unittest


def require_indexed_ref(ref: str = "v3.0") -> int:
    """commit_id of `ref` once its ingestion finished in the active database; skips the test otherwise
    (tests reading parsed data of a version cannot pass while that version is not, or not yet, parsed)."""
    from webapp.backend.database.helpers import finished_ref_sql, get_active_repo_id
    from webapp.backend.database.pool import get_db_cursor

    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                f"SELECT r.commit_id FROM m_git_ref r WHERE r.ref_name = %s AND r.repo_id = %s AND r.commit_id > 0 AND {finished_ref_sql()} LIMIT 1;",
                (ref, get_active_repo_id(cursor)),
            )
            row = cursor.fetchone()
    except Exception as exc:
        raise unittest.SkipTest(f"Database check failed: {exc}")
    if not row:
        raise unittest.SkipTest(f"{ref} is not indexed in the active database.")
    return int(row["commit_id"] if isinstance(row, dict) else row[0])
