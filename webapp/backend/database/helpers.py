"""webapp/backend/database/helpers.py - Version Caching & Data Serialization Helpers."""
from __future__ import annotations
import logging
import time
from webapp.backend.config import RepoScopedCache, get_active_repository, get_repo_dir
from core.globalstuff import is_safe_git_rev
from typing import Any

logger = logging.getLogger(__name__)

STAT_LABEL_MAP: dict[str, str] = {
    "A": "Added",
    "M": "Modified",
    "R": "Renamed",
    "D": "Deleted",
    "0": "Active",
}

# m_ingest_state.status of a version whose ingestion has not finished (core/ingest_state.py)
INGEST_PARSING = 1


def finished_ref_sql(alias: str = "r") -> str:
    """SQL condition keeping refs whose ingestion finished (or predates m_ingest_state): a version still
    being parsed, or left interrupted, is hidden until its run completes."""
    return f"NOT EXISTS (SELECT 1 FROM m_ingest_state s WHERE s.commit_id = {alias}.commit_id AND s.status = {INGEST_PARSING})"


_TREE_SOURCE: dict[str, Any] = {}


def _exec_query(db_or_cur: Any, sql: str, params: tuple = (), dictionary: bool = True) -> list:
    if hasattr(db_or_cur, "query") and callable(db_or_cur.query):
        return db_or_cur.query(sql, params, dictionary=dictionary)
    if hasattr(db_or_cur, "execute"):
        db_or_cur.execute(sql, params)
        if hasattr(db_or_cur, "fetchall"):
            return list(db_or_cur.fetchall() or ())
    return []


def _exec_query_one(db_or_cur: Any, sql: str, params: tuple = (), dictionary: bool = True) -> Any:
    if hasattr(db_or_cur, "query_one") and callable(db_or_cur.query_one):
        return db_or_cur.query_one(sql, params, dictionary=dictionary)
    if hasattr(db_or_cur, "execute"):
        db_or_cur.execute(sql, params)
        if hasattr(db_or_cur, "fetchone"):
            return db_or_cur.fetchone()
    return None


def tree_source(cursor: Any) -> str:
    """The relation holding every version's full checkpoint tree: the `m_checkpoint_tree` view (core/API.md section
    4.2: a checkpoint may be stored as a delta of another), or `m_bridge_checkpoint` on a database created before the
    view existed (every checkpoint full there). A definite answer from the database driver is kept for the process."""
    hit = _TREE_SOURCE.get("name")
    # The view is permanent once there; its absence is checked again every 5 minutes (a database reset to the
    # current layout while the webapp runs)
    if hit is not None and (hit == "m_checkpoint_tree" or time.monotonic() - _TREE_SOURCE.get("at", 0.0) < 300.0):
        return hit
    try:
        rows = _exec_query(
            cursor,
            "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'm_checkpoint_tree'",
            dictionary=True,
        )
    except Exception as exc:  # noqa: BLE001 - keep the view's name; the query itself reports the problem
        logger.debug("checkpoint tree view check failed: %s", exc)
        return "m_checkpoint_tree"
    n = None
    if len(rows) == 1:
        row = rows[0]
        n = row.get("n") if isinstance(row, dict) else (row[0] if isinstance(row, (tuple, list)) else None)
    if not isinstance(n, int):
        return "m_checkpoint_tree"  # not a database answer (a test double): nothing to remember
    hit = "m_checkpoint_tree" if n else "m_bridge_checkpoint"
    if type(cursor).__module__.startswith("mysql"):
        _TREE_SOURCE.update(name=hit, at=time.monotonic())
    return hit


# Bounded: besides the repository's refs (preloaded), any commit SHA or prefix a caller names adds entries
_NAME_TO_VID_CACHE: dict[str, int] = RepoScopedCache(maxsize=50_000)
_VID_TO_NAME_CACHE: dict[int, str] = RepoScopedCache(maxsize=50_000)
# Whether the repository's refs were preloaded into the two caches above
_PRELOADED: dict[str, bool] = RepoScopedCache()


def commit_prefix_range(prefix_hex: str) -> tuple[bytes, bytes | None]:
    """[low, high) BINARY(20) bounds of the commit hashes starting with a hex prefix of any length
    (an odd length bounds its last digit too); high is None when no upper bound is needed."""
    prefix_hex = prefix_hex.lower()
    lo_hex = prefix_hex + ("0" if len(prefix_hex) % 2 else "")
    hi_hex = prefix_hex + ("f" if len(prefix_hex) % 2 else "")
    nxt = int(hi_hex, 16) + 1
    hi = nxt.to_bytes(len(hi_hex) // 2, "big") if nxt < 16 ** len(hi_hex) else None
    return bytes.fromhex(lo_hex), hi


def safe_decode(val: Any) -> Any:
    """Safely decode bytearray/bytes/memoryview to string, preserving None and primitives."""
    if val is None:
        return None
    if isinstance(val, (bytearray, bytes, memoryview)):
        try:
            return bytes(val).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(val).decode("latin-1", errors="replace")
    return val


def hash_hex(val: Any) -> str:
    """Hex string of a BINARY(20) hash column, "" for NULL.

    mysql-connector can return a binary hash whose bytes happen to be valid UTF-8 as str; such a str
    re-encodes to 20 bytes, while an already hex-formatted hash is left as is.
    """
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val).hex()
    if isinstance(val, str):
        raw = val.encode("utf-8")
        return raw.hex() if len(raw) == 20 else val
    return str(val)


def format_stat_label(stat: str | None) -> str:
    """Return the friendly label of a start status code."""
    if not stat or stat == "0":
        return "Added"
    return STAT_LABEL_MAP.get(stat, str(stat))


_REPO_ID_CACHE: dict[str, int] = RepoScopedCache()


def get_active_repo_id(cursor: Any) -> int:
    """m_repository.repo_id of the request's repository (-1 when that repository was never parsed)."""
    if "repo_id" in _REPO_ID_CACHE:
        return _REPO_ID_CACHE["repo_id"]
    repo = get_active_repository()
    repo_id = -1
    try:
        row = _exec_query_one(cursor, "SELECT repo_id FROM m_repository WHERE name = %s AND repo_id > 0 LIMIT 1;", (repo.get("name"),))
        if not row and repo.get("git_url"):
            row = _exec_query_one(cursor, "SELECT repo_id FROM m_repository WHERE git_url = %s AND repo_id > 0 LIMIT 1;", (repo["git_url"],))
        if row:
            repo_id = row["repo_id"] if isinstance(row, dict) else row[0]
    except Exception as e:
        logger.debug("Could not resolve repo_id for '%s': %s", repo.get("name"), e)
        return repo_id
    _REPO_ID_CACHE["repo_id"] = repo_id
    return repo_id


def get_active_network_id(cursor: Any) -> int | None:
    """m_git_network.network_id of the request's repository: the commits m_commit holds for it (and for
    the other clones of its history). None when the repository was never parsed."""
    cached = _REPO_ID_CACHE.get("network_id")
    if cached is not None:
        return cached if cached >= 0 else None
    repo_id = get_active_repo_id(cursor)
    network_id = -1
    if repo_id > 0:
        try:
            row = _exec_query_one(cursor, "SELECT network_id FROM m_repository WHERE repo_id = %s LIMIT 1;", (repo_id,))
            if row:
                network_id = int(row["network_id"] if isinstance(row, dict) else row[0])
        except Exception as e:
            logger.debug("Could not resolve network_id of repo %s: %s", repo_id, e)
            return None
    _REPO_ID_CACHE["network_id"] = network_id
    return network_id if network_id >= 0 else None


def preload_version_cache(cursor: Any) -> None:
    """Preload the active repository's refs into in-memory caches from native m_git_ref."""
    _PRELOADED["loaded"] = True
    try:
        rows = _exec_query(
            cursor,
            f"SELECT r.commit_id, r.ref_name FROM m_git_ref r WHERE r.commit_id > 0 AND r.repo_id = %s AND {finished_ref_sql()} "
            "ORDER BY r.commit_id ASC;",
            (get_active_repo_id(cursor),),
        )
        for r in rows:
            if isinstance(r, dict):
                rvid = r.get("commit_id")
                rvname = safe_decode(r.get("ref_name"))
            else:
                rvid = r[0]
                rvname = safe_decode(r[1])
            if rvid and rvname:
                _NAME_TO_VID_CACHE[rvname] = rvid
                _NAME_TO_VID_CACHE[str(rvid)] = rvid
                _VID_TO_NAME_CACHE[rvid] = rvname
    except Exception as e:
        logger.debug("Error preloading version cache: %s", e)


def get_version_info(cursor: Any, version_name_or_id: str | int) -> tuple[int | None, str]:
    """Resolve both vid (commit_id) and vname with thread-safe in-memory caching and Git commit SHA lookup."""
    if not _PRELOADED.get("loaded"):
        preload_version_cache(cursor)

    # .get() into locals: both caches evict, so contains-then-get could race a miss
    if isinstance(version_name_or_id, int) or str(version_name_or_id).isdigit():
        vid_int = int(version_name_or_id)
        cached_name = _VID_TO_NAME_CACHE.get(vid_int)
        if cached_name is not None:
            return vid_int, cached_name

    ver_str = str(version_name_or_id).strip()
    cached_vid = _NAME_TO_VID_CACHE.get(ver_str)
    if cached_vid is not None:
        if not ver_str.isdigit():
            return cached_vid, ver_str
        return cached_vid, _VID_TO_NAME_CACHE.get(cached_vid, ver_str)

    # Database query: try m_git_ref (for tags/branches), then m_commit (for arbitrary Git commit SHAs)
    try:
        if ver_str.isdigit():
            row = _exec_query_one(
                cursor,
                f"SELECT r.commit_id AS vid, r.ref_name AS vname FROM m_git_ref r WHERE r.commit_id = %s AND r.repo_id = %s AND {finished_ref_sql()} LIMIT 1;",
                (int(ver_str), get_active_repo_id(cursor)),
            )
            if not row:
                c_row = _exec_query_one(
                    cursor,
                    "SELECT commit_id AS vid, commit_hash FROM m_commit WHERE commit_id = %s LIMIT 1;",
                    (int(ver_str),),
                )
                if c_row:
                    cid = c_row["vid"] if isinstance(c_row, dict) else c_row[0]
                    c_h = c_row["commit_hash"] if isinstance(c_row, dict) else c_row[1]
                    hex_str = hash_hex(c_h)
                    _NAME_TO_VID_CACHE[hex_str] = cid
                    _NAME_TO_VID_CACHE[str(cid)] = cid
                    if cid not in _VID_TO_NAME_CACHE:
                        _VID_TO_NAME_CACHE[cid] = hex_str
                    return cid, hex_str
        else:
            row = _exec_query_one(
                cursor,
                f"SELECT r.commit_id AS vid, r.ref_name AS vname FROM m_git_ref r WHERE r.ref_name = %s AND r.repo_id = %s AND {finished_ref_sql()} LIMIT 1;",
                (ver_str, get_active_repo_id(cursor)),
            )

        if row:
            if isinstance(row, dict):
                rvid = row.get("vid") or row.get("commit_id")
                rvname = safe_decode(row.get("vname") or row.get("ref_name"))
            else:
                rvid = row[0]
                rvname = safe_decode(row[1])
            _NAME_TO_VID_CACHE[rvname] = rvid
            _NAME_TO_VID_CACHE[str(rvid)] = rvid
            _VID_TO_NAME_CACHE[rvid] = rvname
            return rvid, rvname

        # If not matched as tag/ref, check if ver_str is a Git commit SHA (full 40-char or short prefix)
        is_hex = len(ver_str) >= 7 and all(c in "0123456789abcdefABCDEF" for c in ver_str)
        if is_hex:
            if len(ver_str) == 40:
                c_bytes = bytes.fromhex(ver_str)
                c_rows = _exec_query(
                    cursor,
                    f"SELECT c.commit_id, c.commit_hash FROM m_commit c WHERE c.commit_hash = %s AND {finished_ref_sql('c')} LIMIT 1;",
                    (c_bytes,),
                )
            else:
                # Every digit counts (git abbreviates to 7), and a prefix two commits share names neither
                lo, hi = commit_prefix_range(ver_str)
                c_rows = _exec_query(
                    cursor,
                    f"SELECT c.commit_id, c.commit_hash FROM m_commit c WHERE c.commit_hash >= %s"
                    f"{' AND c.commit_hash < %s' if hi else ''} AND {finished_ref_sql('c')} LIMIT 2;",
                    (lo, hi) if hi else (lo,),
                )
                if len(c_rows) > 1:
                    return None, ver_str
            c_row = c_rows[0] if c_rows else None
            if c_row:
                cid = c_row["commit_id"] if isinstance(c_row, dict) else c_row[0]
                c_h = c_row["commit_hash"] if isinstance(c_row, dict) else c_row[1]
                hex_str = hash_hex(c_h)
                _NAME_TO_VID_CACHE[hex_str] = cid
                _NAME_TO_VID_CACHE[ver_str] = cid
                _NAME_TO_VID_CACHE[str(cid)] = cid
                if cid not in _VID_TO_NAME_CACHE:
                    _VID_TO_NAME_CACHE[cid] = hex_str
                return cid, hex_str

        # If not matched in database tables, attempt git rev-parse resolution (for branches, -rc tags, arbitrary revisions)
        if not is_safe_git_rev(ver_str):
            # The name reaches git's command line below, where "-..." would be read as an option
            return None, ver_str
        try:
            import subprocess
            out = subprocess.run(
                ["git", "rev-parse", "--verify", f"{ver_str}^{{commit}}"],
                cwd=str(get_repo_dir()),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0:
                git_sha = out.stdout.strip()
                if len(git_sha) == 40:
                    c_bytes = bytes.fromhex(git_sha)
                    c_row = _exec_query_one(
                        cursor,
                        f"SELECT c.commit_id FROM m_commit c WHERE c.commit_hash = %s AND {finished_ref_sql('c')} LIMIT 1;",
                        (c_bytes,),
                    )
                    if c_row:
                        cid = c_row["commit_id"] if isinstance(c_row, dict) else c_row[0]
                        _NAME_TO_VID_CACHE[ver_str] = cid
                        _NAME_TO_VID_CACHE[git_sha] = cid
                        if cid not in _VID_TO_NAME_CACHE:
                            _VID_TO_NAME_CACHE[cid] = ver_str
                        return cid, ver_str
                    # Valid git revision not indexed into m_commit; assign pseudo vid 0
                    _NAME_TO_VID_CACHE[ver_str] = 0
                    _NAME_TO_VID_CACHE[git_sha] = 0
                    return 0, ver_str
        except Exception as git_err:
            logger.debug("git rev-parse fallback failed for '%s': %s", ver_str, git_err)
    except Exception as e:
        logger.error("Failed to query version info: %s", e)

    return None, ver_str

