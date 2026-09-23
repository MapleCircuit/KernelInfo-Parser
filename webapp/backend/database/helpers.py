"""webapp/backend/database/helpers.py - Version Caching & Data Serialization Helpers."""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

STAT_LABEL_MAP: dict[str, str] = {
    "A": "Added",
    "M": "Modified",
    "R": "Renamed",
    "D": "Deleted",
    "0": "Active",
}

_NAME_TO_VID_CACHE: dict[str, int] = {}
_VID_TO_NAME_CACHE: dict[int, str] = {}


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


def format_stat_label(stat: str | None, is_end: bool = False) -> str:
    """Return friendly label for start or end status code."""
    if not stat or stat == "0":
        return "Active" if is_end else "Added"
    return STAT_LABEL_MAP.get(stat, str(stat))


def preload_version_cache(cursor: Any) -> None:
    """Preload all kernel versions into in-memory caches."""
    try:
        cursor.execute("SELECT vid, vname FROM m_v_main ORDER BY vid ASC;")
        rows = cursor.fetchall()
        for r in rows:
            if isinstance(r, dict):
                rvid = r["vid"]
                rvname = safe_decode(r["vname"])
            else:
                rvid = r[0]
                rvname = safe_decode(r[1])
            _NAME_TO_VID_CACHE[rvname] = rvid
            _NAME_TO_VID_CACHE[str(rvid)] = rvid
            _VID_TO_NAME_CACHE[rvid] = rvname
    except Exception as e:
        logger.debug("Error preloading version cache: %s", e)


def get_version_info(cursor: Any, version_name_or_id: str | int) -> tuple[int | None, str]:
    """Resolve both vid and vname with thread-safe in-memory caching."""
    if not _VID_TO_NAME_CACHE:
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
        return vid, _VID_TO_NAME_CACHE.get(vid, ver_str)

    # Database query fallback
    try:
        if ver_str.isdigit():
            cursor.execute("SELECT vid, vname FROM m_v_main WHERE vid = %s LIMIT 1;", (int(ver_str),))
        else:
            cursor.execute("SELECT vid, vname FROM m_v_main WHERE vname = %s LIMIT 1;", (ver_str,))
        row = cursor.fetchone()
        if row:
            if isinstance(row, dict):
                rvid = row["vid"]
                rvname = safe_decode(row["vname"])
            else:
                rvid = row[0]
                rvname = safe_decode(row[1])
            _NAME_TO_VID_CACHE[rvname] = rvid
            _VID_TO_NAME_CACHE[rvid] = rvname
            return rvid, rvname
    except Exception as e:
        logger.error("Failed to query version info: %s", e)

    return None, ver_str
