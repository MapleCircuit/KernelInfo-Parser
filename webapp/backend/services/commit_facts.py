"""webapp/backend/services/commit_facts.py - Commit enrichment shared by the history features (H1-H4).

`git_facts(shas)` reads subject, author and date of commits from Git in one call; `commit_ids(shas)`
maps the ones the parser ingested to their `m_commit.commit_id` (SELECT only, section 2.5);
`release_badges(shas)` adds the first tags containing them (release membership, section 2.17).
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Iterable

from webapp.backend.config import get_repo_dir
from webapp.backend.database.helpers import get_active_network_id
from webapp.backend.database.pool import get_db_cursor

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_BATCH = 200


def git_facts(shas: Iterable[str]) -> dict[str, dict[str, Any]]:
    """{sha: {subject, author, email, date}} for full SHAs (at most 200 per call; unknown ones omitted)."""
    keys = [s for s in dict.fromkeys(s.lower() for s in shas if s) if _FULL_SHA.match(s)][:MAX_BATCH]
    if not keys:
        return {}
    try:
        out = subprocess.run(
            ["git", "-C", str(get_repo_dir()), "log", "--no-walk=unsorted", "--format=%H%x1f%an%x1f%ae%x1f%at%x1f%s", *keys],
            capture_output=True, text=True, timeout=30, errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    facts = {}
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5 and _FULL_SHA.match(parts[0]):
            facts[parts[0]] = {"subject": parts[4], "author": parts[1], "email": parts[2].lower(),
                               "date": int(parts[3]) if parts[3].isdigit() else 0}
    return facts


def commit_ids(shas: Iterable[str]) -> dict[str, int]:
    """{sha: commit_id} of the commits the parser ingested for the active repository."""
    keys = [s for s in dict.fromkeys(s.lower() for s in shas if s) if _FULL_SHA.match(s)][:MAX_BATCH]
    if not keys:
        return {}
    try:
        with get_db_cursor() as cursor:
            network_id = get_active_network_id(cursor)
            if network_id is None:
                return {}
            cursor.execute(
                f"SELECT commit_id, commit_hash FROM m_commit WHERE network_id = %s AND commit_hash IN ({','.join(['%s'] * len(keys))});",
                (network_id, *(bytes.fromhex(k) for k in keys)),
            )
            rows = cursor.fetchall()
    except Exception:  # noqa: BLE001 - enrichment is optional
        return {}
    out = {}
    for r in rows:
        h = r["commit_hash"]
        out[(h.hex() if isinstance(h, (bytes, bytearray)) else str(h)).lower()] = int(r["commit_id"])
    return out


def release_badges(shas: Iterable[str]) -> dict[str, dict[str, Any]]:
    """{sha: {first_tag, first_release, first_stable, released}} from release membership (one batched
    lookup: m_commit_release, or the SQLite release index; no slow fallback). SHAs not answered yet
    (index still building) are omitted."""
    from webapp.backend.services import release_service as rs
    keys = [s for s in dict.fromkeys(x.lower() for x in shas if x) if _FULL_SHA.match(s)]
    if not keys:
        return {}
    try:
        found = rs.release_lookup(keys)
        names = set(rs.tag_commits())
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for s in keys:
        hit = found.get(s)
        if hit is None:
            continue
        first, stable = hit.get("mainline"), hit.get("stable")
        out[s] = {"first_tag": first, "first_release": rs.final_of(first, names) if first else None,
                  "first_stable": stable, "released": bool(first or stable)}
    return out
