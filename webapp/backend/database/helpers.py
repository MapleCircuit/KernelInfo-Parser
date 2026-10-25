"""webapp/backend/database/helpers.py - Version Caching & Data Serialization Helpers."""
from __future__ import annotations
import logging
from webapp.backend.config import RepoScopedCache, get_active_repository, get_repo_dir
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


_NAME_TO_VID_CACHE: dict[str, int] = RepoScopedCache()
_VID_TO_NAME_CACHE: dict[int, str] = RepoScopedCache()


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


def format_stat_label(stat: str | None, is_end: bool = False) -> str:
    """Return friendly label for start or end status code."""
    if not stat or stat == "0":
        return "Active" if is_end else "Added"
    return STAT_LABEL_MAP.get(stat, str(stat))


_REPO_ID_CACHE: dict[str, int] = RepoScopedCache()


def get_active_repo_id(cursor: Any) -> int:
    """m_repository.repo_id of the request's repository (-1 when that repository was never parsed)."""
    if "repo_id" in _REPO_ID_CACHE:
        return _REPO_ID_CACHE["repo_id"]
    repo = get_active_repository()
    repo_id = -1
    try:
        cursor.execute("SELECT repo_id FROM m_repository WHERE name = %s AND repo_id > 0 LIMIT 1;", (repo.get("name"),))
        row = cursor.fetchone()
        if not row and repo.get("git_url"):
            cursor.execute("SELECT repo_id FROM m_repository WHERE git_url = %s AND repo_id > 0 LIMIT 1;", (repo["git_url"],))
            row = cursor.fetchone()
        if row:
            repo_id = row["repo_id"] if isinstance(row, dict) else row[0]
    except Exception as e:
        logger.debug("Could not resolve repo_id for '%s': %s", repo.get("name"), e)
        return repo_id
    _REPO_ID_CACHE["repo_id"] = repo_id
    return repo_id


def preload_version_cache(cursor: Any) -> None:
    """Preload the active repository's refs into in-memory caches from native m_git_ref."""
    _VID_TO_NAME_CACHE["__loaded__"] = True
    try:
        cursor.execute(
            f"SELECT r.commit_id, r.ref_name FROM m_git_ref r WHERE r.commit_id > 0 AND r.repo_id = %s AND {finished_ref_sql()} "
            "ORDER BY r.commit_id ASC;",
            (get_active_repo_id(cursor),),
        )
        rows = cursor.fetchall()
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
    if "__loaded__" not in _VID_TO_NAME_CACHE:
        preload_version_cache(cursor)

    if isinstance(version_name_or_id, int):
        if version_name_or_id in _VID_TO_NAME_CACHE:
            return version_name_or_id, _VID_TO_NAME_CACHE[version_name_or_id]
    elif str(version_name_or_id).isdigit():
        vid_int = int(version_name_or_id)
        if vid_int in _VID_TO_NAME_CACHE:
            return vid_int, _VID_TO_NAME_CACHE[vid_int]

    ver_str = str(version_name_or_id).strip()
    if ver_str in _NAME_TO_VID_CACHE:
        vid = _NAME_TO_VID_CACHE[ver_str]
        if not ver_str.isdigit():
            return vid, ver_str
        return vid, _VID_TO_NAME_CACHE.get(vid, ver_str)

    # Database query: try m_git_ref (for tags/branches), then m_commit (for arbitrary Git commit SHAs)
    try:
        if ver_str.isdigit():
            cursor.execute(
                f"SELECT r.commit_id AS vid, r.ref_name AS vname FROM m_git_ref r WHERE r.commit_id = %s AND r.repo_id = %s AND {finished_ref_sql()} LIMIT 1;",
                (int(ver_str), get_active_repo_id(cursor)),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "SELECT commit_id AS vid, commit_hash FROM m_commit WHERE commit_id = %s LIMIT 1;",
                    (int(ver_str),),
                )
                c_row = cursor.fetchone()
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
            cursor.execute(
                f"SELECT r.commit_id AS vid, r.ref_name AS vname FROM m_git_ref r WHERE r.ref_name = %s AND r.repo_id = %s AND {finished_ref_sql()} LIMIT 1;",
                (ver_str, get_active_repo_id(cursor)),
            )
            row = cursor.fetchone()

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
                cursor.execute(
                    "SELECT commit_id, commit_hash FROM m_commit WHERE commit_hash = %s LIMIT 1;",
                    (c_bytes,),
                )
            else:
                prefix_hex = ver_str.lower()
                cursor.execute(
                    "SELECT commit_id, commit_hash FROM m_commit WHERE HEX(commit_hash) LIKE %s LIMIT 1;",
                    (f"{prefix_hex.upper()}%",),
                )
            c_row = cursor.fetchone()
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
                    cursor.execute("SELECT commit_id FROM m_commit WHERE commit_hash = %s LIMIT 1;", (c_bytes,))
                    c_row = cursor.fetchone()
                    if c_row:
                        cid = c_row["commit_id"] if isinstance(c_row, dict) else c_row[0]
                        _NAME_TO_VID_CACHE[ver_str] = cid
                        _NAME_TO_VID_CACHE[git_sha] = cid
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

