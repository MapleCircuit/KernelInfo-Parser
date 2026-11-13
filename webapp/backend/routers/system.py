"""webapp/backend/routers/system.py - Server change tokens and ingestion status (webapp/API.md section 4.1)."""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

from fastapi import APIRouter

from db_engine.spec import Join, gt

from webapp.backend.config import get_active_repo_name, get_repo_dir
from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id, safe_decode
from webapp.backend.database.pool import get_reader
from webapp.backend.services.sync_service import sync_service

sync_router = APIRouter(prefix="/api", tags=["system"])

SERVER_STARTED = time.time()
# A version still marked "parsing" after this long was most likely interrupted; the parser rolls such a
# version back on its next start (core/API.md section 4.1)
STALE_PARSING_S = 30 * 60
INGEST_TTL_S = 10.0
RECENT_LIMIT = 20
INGEST_PARSING = 1
INGEST_DONE = 2

_INGEST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INGEST_LOCK = threading.Lock()


@sync_router.get("/sync")
def get_sync(fresh: bool = False) -> dict[str, Any]:
    """Change tokens of the server: database instance, frontend build, parsed versions, Git refs and the
    repository list.

    Clients compare them with the ones they saw last (webapp/frontend/js/sync.js); see
    webapp/backend/services/sync_service.py for what each one covers. `fresh=true` recomputes the
    tokens instead of serving the cached ones (not sent by the frontend).
    """
    return sync_service.snapshot(fresh=fresh)


def _git_tag_count() -> int | None:
    try:
        out = subprocess.run(["git", "-C", str(get_repo_dir()), "for-each-ref", "--format=x", "refs/tags"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.count("\n") if out.returncode == 0 else None


def _ref_names(rows: list[dict[str, Any]]) -> dict[int, str]:
    names: dict[int, str] = {}
    for r in rows:
        names.setdefault(int(r["commit_id"]), safe_decode(r["ref_name"]))
    return names


def ingest_status(now: float | None = None) -> dict[str, Any]:
    """What the parser has done for the active repository, read with SELECTs only (section 2.5)."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {
        "repository": get_active_repo_name(),
        "catalog": {"tags": None, "parsed": 0, "parsing": 0, "git_only": None},
        "parsing": [],
        "recent": [],
        "db": {"tables": [], "total_mb": 0.0},
        "available": True,
    }
    try:
        with get_reader() as reader:
            network_id = get_active_network_id(reader)
            repo_id = get_active_repo_id(reader)
            if network_id is not None:
                states = reader.select_rows(
                    ("m_ingest_state", "s"),
                    ["s.commit_id", "s.status", "s.updated_at", "c.commit_hash"],
                    {"s.network_id": network_id, "s.status": [INGEST_PARSING, INGEST_DONE]},
                    joins=[Join(("m_commit", "c"), {"c.commit_id": "s.commit_id"})],
                    order_by=["-s.updated_at"],
                )
                names = _ref_names(
                    reader.select_rows(
                        ("m_git_ref", "r"),
                        ["r.commit_id", "r.ref_name"],
                        {"r.repo_id": repo_id, "r.commit_id": gt(0)},
                        order_by=["r.ref_id"],
                    )
                )
                parsing = [r for r in states if int(r["status"]) == INGEST_PARSING]
                done = [r for r in states if int(r["status"]) == INGEST_DONE]
                out["catalog"]["parsing"] = len(parsing)
                out["catalog"]["parsed"] = len(done)
                for r in parsing:
                    updated = int(r["updated_at"] or 0)
                    out["parsing"].append({
                        "version": names.get(int(r["commit_id"])) or _short(r["commit_hash"]),
                        "commit": _short(r["commit_hash"]),
                        "updated_at": updated,
                        "stale": bool(updated) and now - updated > STALE_PARSING_S,
                    })
                for r in done[:RECENT_LIMIT]:
                    out["recent"].append({
                        "version": names.get(int(r["commit_id"])) or _short(r["commit_hash"]),
                        "commit": _short(r["commit_hash"]),
                        "finished_at": int(r["updated_at"] or 0),
                    })
            total = 0
            for stat in reader.table_stats():
                total += stat.data_bytes + stat.index_bytes
                out["db"]["tables"].append({
                    "name": safe_decode(stat.name),
                    "rows_estimate": int(stat.rows or 0),
                    "data_mb": round(stat.data_bytes / 1048576, 1),
                    "index_mb": round(stat.index_bytes / 1048576, 1),
                })
            out["db"]["total_mb"] = round(total / 1048576, 1)
    except Exception as exc:  # noqa: BLE001 - the status page also reports a missing database
        out["available"] = False
        out["error"] = str(exc)[:300]
    tags = _git_tag_count()
    out["catalog"]["tags"] = tags
    if tags is not None:
        out["catalog"]["git_only"] = max(0, tags - out["catalog"]["parsed"])
    return out


def _short(sha: Any) -> str:
    text = sha.hex() if isinstance(sha, (bytes, bytearray)) else safe_decode(sha)
    return text[:12]


def server_status() -> dict[str, Any]:
    """Uptime, warm-up, background jobs and cache occupancy of this process."""
    from webapp.backend.routers.versions import PREWARM_STATE
    from webapp.backend.services import disk_cache
    from webapp.backend.services.grep_service import grep_service
    from webapp.backend.services.jobs import jobs
    caches = [{"name": "grep results", "size": len(grep_service._cache), "maxsize": grep_service._cache.maxsize}]
    return {
        "uptime_s": int(time.time() - SERVER_STARTED),
        "prewarm": PREWARM_STATE.get("state", "idle"),
        "jobs": jobs.stats(),
        "caches": caches,
        "disk_cache": {"namespaces": disk_cache.usage(), "max_mb": disk_cache.total_cap_bytes() >> 20},
    }


@sync_router.get("/system/ingest")
def get_ingest_status() -> dict[str, Any]:
    """Ingestion status of the active repository (versions parsed, being parsed or interrupted, recent
    completions, database size) plus the server's own sync and cache state; cached for 10 s."""
    key = get_active_repo_name()
    now = time.time()
    with _INGEST_LOCK:
        hit = _INGEST_CACHE.get(key)
        if hit and now - hit[0] < INGEST_TTL_S:
            body = hit[1]
        else:
            body = None
    if body is None:
        body = ingest_status(now)
        with _INGEST_LOCK:
            _INGEST_CACHE[key] = (now, body)
    return {**body, "sync": sync_service.snapshot(), "server": server_status(), "generated_at": int(now)}
