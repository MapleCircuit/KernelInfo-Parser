"""Tests package."""
from __future__ import annotations

import unittest


def require_indexed_ref(ref: str = "v3.0") -> int:
    """commit_id of `ref` once its ingestion finished in the active database; skips the test otherwise
    (tests reading parsed data of a version cannot pass while that version is not, or not yet, parsed)."""
    from db_engine.spec import finished_ref, gt
    from tests.db_fixtures import require_live_db
    from webapp.backend.database.helpers import get_active_repo_id
    from webapp.backend.database.pool import get_reader

    require_live_db(f"needs {ref} indexed")
    try:
        with get_reader() as reader:
            row = reader.select_one(
                ("m_git_ref", "r"),
                ["r.commit_id"],
                [{"r.ref_name": ref, "r.repo_id": get_active_repo_id(reader), "r.commit_id": gt(0)}, finished_ref("r")],
            )
    except Exception as exc:
        raise unittest.SkipTest(f"Database check failed: {exc}")
    if not row:
        raise unittest.SkipTest(f"{ref} is not indexed in the active database.")
    return int(row["commit_id"])

