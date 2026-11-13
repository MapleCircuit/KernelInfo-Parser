"""webapp/backend/database/helpers.py - Version Caching & Data Serialization Helpers."""
from __future__ import annotations
import logging
from webapp.backend.config import RepoScopedCache, get_active_repository, get_repo_dir
from core.globalstuff import is_safe_git_rev
from typing import Any

from db_engine.spec import finished_ref, gt, hash_prefix

logger = logging.getLogger(__name__)

STAT_LABEL_MAP: dict[str, str] = {
    "A": "Added",
    "M": "Modified",
    "R": "Renamed",
    "D": "Deleted",
    "0": "Active",
}

# Bounded: besides the repository's refs (preloaded), any commit SHA or prefix a caller names adds entries
_NAME_TO_VID_CACHE: dict[str, int] = RepoScopedCache(maxsize=50_000)
_VID_TO_NAME_CACHE: dict[int, str] = RepoScopedCache(maxsize=50_000)
# Whether the repository's refs were preloaded into the two caches above
_PRELOADED: dict[str, bool] = RepoScopedCache()


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

    A driver can return a binary hash whose bytes happen to be valid UTF-8 as str; such a str
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


def get_active_repo_id(reader: Any) -> int:
    """m_repository.repo_id of the request's repository (-1 when that repository was never parsed)."""
    if "repo_id" in _REPO_ID_CACHE:
        return _REPO_ID_CACHE["repo_id"]
    repo = get_active_repository()
    repo_id = -1
    try:
        row = reader.select_one("m_repository", ["repo_id"], {"name": repo.get("name"), "repo_id": gt(0)})
        if not row and repo.get("git_url"):
            row = reader.select_one("m_repository", ["repo_id"], {"git_url": repo["git_url"], "repo_id": gt(0)})
        if row:
            repo_id = row["repo_id"]
    except Exception as e:
        logger.debug("Could not resolve repo_id for '%s': %s", repo.get("name"), e)
        return repo_id
    _REPO_ID_CACHE["repo_id"] = repo_id
    return repo_id


def get_active_network_id(reader: Any) -> int | None:
    """m_git_network.network_id of the request's repository: the commits m_commit holds for it (and for
    the other clones of its history). None when the repository was never parsed."""
    cached = _REPO_ID_CACHE.get("network_id")
    if cached is not None:
        return cached if cached >= 0 else None
    repo_id = get_active_repo_id(reader)
    network_id = -1
    if repo_id > 0:
        try:
            row = reader.select_one("m_repository", ["network_id"], {"repo_id": repo_id})
            if row:
                network_id = int(row["network_id"])
        except Exception as e:
            logger.debug("Could not resolve network_id of repo %s: %s", repo_id, e)
            return None
    _REPO_ID_CACHE["network_id"] = network_id
    return network_id if network_id >= 0 else None


def preload_version_cache(reader: Any) -> None:
    """Preload the active repository's refs into in-memory caches from native m_git_ref."""
    _PRELOADED["loaded"] = True
    try:
        rows = reader.select_rows(
            ("m_git_ref", "r"),
            ["r.commit_id", "r.ref_name"],
            [{"r.commit_id": gt(0), "r.repo_id": get_active_repo_id(reader)}, finished_ref("r")],
            order_by=["r.commit_id"],
        )
        for r in rows:
            rvid = r["commit_id"]
            rvname = safe_decode(r["ref_name"])
            if rvid and rvname:
                _NAME_TO_VID_CACHE[rvname] = rvid
                _NAME_TO_VID_CACHE[str(rvid)] = rvid
                _VID_TO_NAME_CACHE[rvid] = rvname
    except Exception as e:
        logger.debug("Error preloading version cache: %s", e)


def _ref_row(reader: Any, where: dict[str, Any]) -> dict[str, Any] | None:
    """The finished m_git_ref row (`vid`, `vname`) matching `where`."""
    return reader.select_one(
        ("m_git_ref", "r"),
        {"vid": "r.commit_id", "vname": "r.ref_name"},
        [where, finished_ref("r")],
    )


def _finished_commits(reader: Any, where: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Up to `limit` finished m_commit rows (`commit_id`, `commit_hash`) matching `where`."""
    return reader.select_rows(
        ("m_commit", "c"), ["c.commit_id", "c.commit_hash"], [where, finished_ref("c")], limit=limit
    )


def get_version_info(reader: Any, version_name_or_id: str | int) -> tuple[int | None, str]:
    """Resolve both vid (commit_id) and vname with thread-safe in-memory caching and Git commit SHA lookup."""
    if not _PRELOADED.get("loaded"):
        preload_version_cache(reader)

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
        repo_id = get_active_repo_id(reader)
        if ver_str.isdigit():
            row = _ref_row(reader, {"r.commit_id": int(ver_str), "r.repo_id": repo_id})
            if not row:
                c_row = reader.select_one(
                    "m_commit", {"vid": "commit_id", "commit_hash": "commit_hash"}, {"commit_id": int(ver_str)}
                )
                if c_row:
                    cid = c_row["vid"]
                    hex_str = hash_hex(c_row["commit_hash"])
                    _NAME_TO_VID_CACHE[hex_str] = cid
                    _NAME_TO_VID_CACHE[str(cid)] = cid
                    if cid not in _VID_TO_NAME_CACHE:
                        _VID_TO_NAME_CACHE[cid] = hex_str
                    return cid, hex_str
        else:
            row = _ref_row(reader, {"r.ref_name": ver_str, "r.repo_id": repo_id})

        if row:
            rvid = row["vid"]
            rvname = safe_decode(row["vname"])
            _NAME_TO_VID_CACHE[rvname] = rvid
            _NAME_TO_VID_CACHE[str(rvid)] = rvid
            _VID_TO_NAME_CACHE[rvid] = rvname
            return rvid, rvname

        # If not matched as tag/ref, check if ver_str is a Git commit SHA (full 40-char or short prefix)
        is_hex = len(ver_str) >= 7 and all(c in "0123456789abcdefABCDEF" for c in ver_str)
        if is_hex:
            if len(ver_str) == 40:
                c_rows = _finished_commits(reader, {"c.commit_hash": bytes.fromhex(ver_str)}, 1)
            else:
                # Every digit counts (git abbreviates to 7), and a prefix two commits share names neither
                c_rows = _finished_commits(reader, {"c.commit_hash": hash_prefix(ver_str)}, 2)
                if len(c_rows) > 1:
                    return None, ver_str
            c_row = c_rows[0] if c_rows else None
            if c_row:
                cid = c_row["commit_id"]
                hex_str = hash_hex(c_row["commit_hash"])
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
                    c_rows = _finished_commits(reader, {"c.commit_hash": bytes.fromhex(git_sha)}, 1)
                    if c_rows:
                        cid = c_rows[0]["commit_id"]
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

