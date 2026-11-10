"""webapp/backend/services/merged_service.py - Mail thread -> merged commits (P2, webapp/API.md section 4.8).

Did a patch (series) land, as which commits, in which release?
1. `m_commit_email` (the parser's commit <-> mail links, core/API.md section 8.2) by message-id, or by
   thread for the whole series, relation Patch / CoverLetter / SiblingPatch, joined to `m_commit` of the
   active repository's network (SELECT only, section 2.5). Used when the table has its message-id and
   thread indexes (created by the parser at start); without them a lookup would scan the table.
2. Fallback: commits whose `Link:` / `Message-Id:` trailer names the message (`source: "trailers"`):
   `m_commit_link` (core/API.md section 4, row 64), else the commit relation index (section 2.16).
Each commit gets its first release (section 2.17).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import HTTPException

from webapp.backend.config import RepoScopedCache
from webapp.backend.database.helpers import get_active_network_id, safe_decode
from webapp.backend.database.pool import get_db_cursor

logger = logging.getLogger(__name__)

RELATIONS = {1: "patch", 2: "cover", 3: "sibling", 4: "pull"}
MAX_THREAD_MIDS = 300
_INDEXED: RepoScopedCache = RepoScopedCache()
_LOCK = threading.Lock()


def clean_mid(mid: str) -> str:
    """Unbracketed message-id, URL-decoded (the stored form keeps its case)."""
    from urllib.parse import unquote
    return unquote(str(mid or "")).strip().strip("<>").strip()[:255]


def indexes_present(cursor: Any) -> bool:
    hit = _INDEXED.get("mail_idx")
    if hit is not None:
        return hit
    try:
        cursor.execute(
            "SELECT COUNT(DISTINCT INDEX_NAME) AS n FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'm_commit_email' "
            "AND INDEX_NAME IN ('commit_email_mid_idx', 'commit_email_tid_idx');"
        )
        row = cursor.fetchone() or {}
        ok = int(row.get("n") or 0) == 2
    except Exception:  # noqa: BLE001
        ok = False
    _INDEXED["mail_idx"] = ok
    return ok


def _hex(h: Any) -> str:
    return h.hex() if isinstance(h, (bytes, bytearray)) else safe_decode(h)


def summarize(commits: list[dict[str, Any]]) -> dict[str, Any]:
    """Series aggregation: {total, merged, first_release} from the commits found."""
    patches = [c for c in commits if c.get("relation") in ("patch", "sibling")]
    total = max([c.get("patch_total") or 0 for c in commits] + [0])
    merged = len({c.get("patch_idx") for c in patches if c.get("patch_idx")}) or len({c["hash"] for c in patches})
    releases = [c["released"] for c in commits if c.get("released") and c["released"].get("released")]
    from webapp.backend.services.release_service import release_key
    first = min((r.get("first_release") or r.get("first_tag") for r in releases if r.get("first_release") or r.get("first_tag")),
                key=release_key, default=None)
    if not total and merged:
        total = merged
    return {"total": total, "merged": merged, "first_release": first}


def merged_commits(mid: str = "", tid: int = 0) -> dict[str, Any]:
    mid = clean_mid(mid)
    tid = int(tid or 0)
    if not mid and not tid:
        raise HTTPException(status_code=400, detail="mid or tid is required.")
    commits: list[dict[str, Any]] = []
    source = "db"
    with get_db_cursor() as cursor:
        network_id = get_active_network_id(cursor)
        use_db = network_id is not None and indexes_present(cursor)
        if use_db:
            where, params = [], []
            if tid:
                where.append("e.thread_id = %s")
                params.append(tid)
            if mid:
                where.append("e.message_id = %s")
                params.append(mid)
            cursor.execute(
                f"""
                SELECT e.message_id, e.thread_id, e.relation_type, e.match_tier, e.patch_version, e.patch_idx,
                       e.patch_total, c.commit_hash, c.subject
                FROM m_commit_email e
                JOIN m_commit c ON c.commit_id = e.commit_id
                WHERE c.network_id = %s AND e.relation_type IN (1, 2, 3) AND ({" OR ".join(where)})
                LIMIT 1000;
                """,
                (network_id, *params),
            )
            for r in cursor.fetchall():
                commits.append({
                    "hash": _hex(r["commit_hash"]), "subject": safe_decode(r["subject"]),
                    "relation": RELATIONS.get(int(r["relation_type"]), "patch"), "match_tier": int(r["match_tier"]),
                    "patch_version": int(r["patch_version"]), "patch_idx": int(r["patch_idx"]),
                    "patch_total": int(r["patch_total"]), "message_id": safe_decode(r["message_id"]),
                    "thread_id": int(r["thread_id"]),
                })
    if not commits:
        source = "trailers"
        commits = _from_trailers(mid, tid)
    # One entry per commit (a commit linked as patch and sibling counts once)
    seen: dict[str, dict[str, Any]] = {}
    for c in commits:
        seen.setdefault(c["hash"], c)
    commits = sorted(seen.values(), key=lambda c: (c.get("patch_idx") or 0, c["hash"]))
    from webapp.backend.services.commit_facts import release_badges
    badges = release_badges([c["hash"] for c in commits][:200])
    for c in commits:
        c["released"] = badges.get(c["hash"])
    return {"mid": mid, "tid": tid, "source": source, "commits": commits, "series": summarize(commits)}


def _from_trailers(mid: str, tid: int) -> list[dict[str, Any]]:
    from webapp.backend.services import commit_relations
    mids = [mid] if mid else []
    if tid:
        try:
            from webapp.backend.services.mailing_list_service import mailing_list_service
            thread = mailing_list_service.get_thread(tid, mid_hint=mid)
            for m in (thread.get("messages") or [])[:MAX_THREAD_MIDS]:
                if m.get("message_id"):
                    mids.append(clean_mid(m["message_id"]))
        except Exception as exc:  # noqa: BLE001 - the thread service may be unavailable
            logger.debug("thread %s unavailable for the trailer fallback: %s", tid, exc)
    out = []
    from webapp.backend.services.commit_facts import git_facts
    hashes = {}
    # m_commit_link first (ingested commits); the relation index only when the database does not answer
    stored = commit_relations.db_commits_for_mids(mids)
    if stored is None:
        state = commit_relations.ensure_index()
        if state.get("state") != "done":
            return []
    for m in dict.fromkeys(mids):
        found = stored.get(commit_relations.normalize_mid(m), []) if stored is not None else \
            commit_relations.relation_index.commits_for_mid(m)
        for h in found:
            hashes.setdefault(h, m)
    facts = git_facts(list(hashes)[:200])
    for h, m in hashes.items():
        out.append({"hash": h, "subject": (facts.get(h) or {}).get("subject", ""), "relation": "patch", "match_tier": 0,
                    "patch_version": 0, "patch_idx": 0, "patch_total": 0, "message_id": m, "thread_id": tid})
    return out
