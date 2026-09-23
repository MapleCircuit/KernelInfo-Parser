"""webapp/backend/services/diff_service.py - Semantic Cross-Version Diffs."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)


class DiffService:
    """Computes cross-version semantic deltas for files, symbols, and Kconfig options."""

    def get_versions_diff(self, v1: str, v2: str, path: str = "") -> dict[str, Any]:
        """Compare file hierarchies between version v1 and version v2."""
        norm_path = path.strip().strip("/")
        prefix = f"{norm_path}/" if norm_path else ""

        with get_db_cursor() as cursor:
            vid1, vname1 = get_version_info(cursor, v1)
            vid2, vname2 = get_version_info(cursor, v2)
            if vid1 is None or vid2 is None:
                raise HTTPException(status_code=404, detail="One or both versions not found.")

            # Files in v1
            if prefix:
                sql = """
                    SELECT fn.fname, f.fid, f.s_stat, f.e_stat
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    JOIN m_file f ON bf.fid = f.fid
                    WHERE bf.vid = %s AND (fn.fname LIKE %s OR fn.fname = %s);
                """
                cursor.execute(sql, (vid1, f"{prefix}%", norm_path))
                files_v1 = {safe_decode(r["fname"]): r for r in cursor.fetchall()}

                cursor.execute(sql, (vid2, f"{prefix}%", norm_path))
                files_v2 = {safe_decode(r["fname"]): r for r in cursor.fetchall()}
            else:
                sql = """
                    SELECT fn.fname, f.fid, f.s_stat, f.e_stat
                    FROM m_bridge_file bf
                    JOIN m_file_name fn ON bf.fnid = fn.fnid
                    JOIN m_file f ON bf.fid = f.fid
                    WHERE bf.vid = %s;
                """
                cursor.execute(sql, (vid1,))
                files_v1 = {safe_decode(r["fname"]): r for r in cursor.fetchall()}

                cursor.execute(sql, (vid2,))
                files_v2 = {safe_decode(r["fname"]): r for r in cursor.fetchall()}

            added = []
            deleted = []
            modified = []
            unchanged = []

            for fname, r2 in files_v2.items():
                if fname not in files_v1:
                    added.append({"file": fname, "fid": r2["fid"]})
                else:
                    r1 = files_v1[fname]
                    if r1["fid"] != r2["fid"]:
                        modified.append({"file": fname, "fid_old": r1["fid"], "fid_new": r2["fid"]})
                    else:
                        unchanged.append(fname)

            for fname, r1 in files_v1.items():
                if fname not in files_v2:
                    deleted.append({"file": fname, "fid": r1["fid"]})

            changes = []
            for a in added:
                changes.append({"type": "added", "file": a["file"], "file_path": a["file"], "name": a["file"], "fid": a.get("fid")})
            for d in deleted:
                changes.append({"type": "deleted", "file": d["file"], "file_path": d["file"], "name": d["file"], "fid": d.get("fid")})
            for m in modified:
                changes.append({"type": "modified", "file": m["file"], "file_path": m["file"], "name": m["file"], "fid_old": m.get("fid_old"), "fid_new": m.get("fid_new")})

            return {
                "v1": vname1,
                "v2": vname2,
                "path": norm_path,
                "summary": {
                    "added_count": len(added),
                    "removed_count": len(deleted),
                    "modified_count": len(modified),
                    "unchanged_count": len(unchanged),
                    "added": len(added),
                    "deleted": len(deleted),
                    "modified": len(modified),
                    "unchanged": len(unchanged),
                },
                "added": added,
                "deleted": deleted,
                "modified": modified,
                "changes": changes,
            }

    def get_kconfig_diff(self, v1: str, v2: str) -> dict[str, Any]:
        """Compare KConfig symbols between two releases."""
        with get_db_cursor() as cursor:
            vid1, vname1 = get_version_info(cursor, v1)
            vid2, vname2 = get_version_info(cursor, v2)
            if vid1 is None or vid2 is None:
                raise HTTPException(status_code=404, detail="One or both versions not found.")

            cursor.execute("SELECT name, def_val FROM m_kconfig_symbol WHERE vid_s <= %s AND (vid_e >= %s OR vid_e = 0);", (vid1, vid1))
            syms_v1 = {safe_decode(r["name"]): safe_decode(r["def_val"]) for r in cursor.fetchall()}

            cursor.execute("SELECT name, def_val FROM m_kconfig_symbol WHERE vid_s <= %s AND (vid_e >= %s OR vid_e = 0);", (vid2, vid2))
            syms_v2 = {safe_decode(r["name"]): safe_decode(r["def_val"]) for r in cursor.fetchall()}

            added = [k for k in syms_v2 if k not in syms_v1]
            removed = [k for k in syms_v1 if k not in syms_v2]
            changed = [k for k in syms_v1 if k in syms_v2 and syms_v1[k] != syms_v2[k]]

            return {
                "v1": vname1,
                "v2": vname2,
                "summary": {
                    "added": len(added),
                    "removed": len(removed),
                    "changed": len(changed),
                },
                "added": added,
                "removed": removed,
                "changed": changed,
            }
