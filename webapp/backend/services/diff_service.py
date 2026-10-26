"""webapp/backend/services/diff_service.py - 3-Level Semantic Cross-Version Diff Engine.

Level 1: O(1) content blob filter comparing Git blob SHAs (Path C) or tree resolver blob_ids.
Level 2: AST & symbol structural diffing on changed files.
Level 3: Scoped Kconfig file and symbol deltas.

Every side of a comparison may be a release tag, a branch, or an arbitrary commit SHA.
"""
from __future__ import annotations
import logging
import posixpath
import subprocess
from webapp.backend.config import get_repo_dir
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.security.sql import escape_like
from webapp.backend.services.tree_resolver import tree_resolver, git_tree_resolver

logger = logging.getLogger(__name__)

KCONFIG_FTYPE = 2
MAX_SYMBOL_DIFF_FILES = 50
MAX_FILE_DIFF_BYTES = 512 * 1024


def is_kconfig_path(path: str) -> bool:
    """Kconfig sources are named Kconfig or Kconfig.<suffix> (e.g. Kconfig.debug)."""
    base = posixpath.basename(path)
    return base == "Kconfig" or base.startswith("Kconfig.")


class DiffService:
    """Computes cross-version semantic deltas for files, symbols, and Kconfig options."""

    def _resolve_pair(self, cursor: Any, v1: str, v2: str) -> tuple[int, str, int, str]:
        vid1, vname1 = get_version_info(cursor, v1)
        vid2, vname2 = get_version_info(cursor, v2)
        if vid1 is None or vid2 is None:
            missing = v1 if vid1 is None else v2
            raise HTTPException(status_code=404, detail=f"Version or commit '{missing}' not found.")
        return vid1, vname1, vid2, vname2

    def _fetch_tree(self, cursor: Any, vid: int, vname: str, prefix: str, norm_path: str) -> tuple[dict[str, str], bool]:
        """Return ({path: content_key}, is_git). Content keys are Git blob SHAs (Path C) or DB blob_ids."""
        git_tree = git_tree_resolver.list_tree(vname)
        if git_tree is not None:
            if prefix:
                git_tree = {p: s for p, s in git_tree.items() if p.startswith(prefix) or p == norm_path}
            return git_tree, True
        db_tree = self._fetch_commit_files(cursor, vid, prefix, norm_path)
        return {p: str(b) for p, b in db_tree.items()}, False

    def get_versions_diff(self, v1: str, v2: str, path: str = "") -> dict[str, Any]:
        """Compare file hierarchies between two revisions using 3-level blob-skip diffing."""
        norm_path = path.strip().strip("/")
        prefix = f"{norm_path}/" if norm_path else ""

        with get_db_cursor() as cursor:
            vid1, vname1, vid2, vname2 = self._resolve_pair(cursor, v1, v2)

            # Level 1: content-key comparison ({path: blob_sha} from Git, or {path: blob_id} from DB)
            files_v1, git1 = self._fetch_tree(cursor, vid1, vname1, prefix, norm_path)
            files_v2, git2 = self._fetch_tree(cursor, vid2, vname2, prefix, norm_path)
            use_git = git1 and git2
            if not use_git and (git1 or git2):
                # Mixed sources cannot be compared by key; fall back to DB on both sides
                files_v1 = {p: str(b) for p, b in self._fetch_commit_files(cursor, vid1, prefix, norm_path).items()}
                files_v2 = {p: str(b) for p, b in self._fetch_commit_files(cursor, vid2, prefix, norm_path).items()}

            added_paths = [p for p in files_v2 if p not in files_v1]
            deleted_paths = [p for p in files_v1 if p not in files_v2]
            modified_paths = [p for p in files_v2 if p in files_v1 and files_v1[p] != files_v2[p]]
            unchanged_count = len(files_v2) - len(added_paths) - len(modified_paths)

            # Exact renames: a deleted path whose content reappears under a single added path
            renamed_pairs: list[tuple[str, str]] = []
            added_by_key: dict[str, list[str]] = {}
            for p in added_paths:
                added_by_key.setdefault(files_v2[p], []).append(p)
            for old_p in sorted(deleted_paths):
                candidates = added_by_key.get(files_v1[old_p])
                if candidates:
                    renamed_pairs.append((old_p, candidates.pop(0)))
            renamed_old = {o for o, _ in renamed_pairs}
            renamed_new = {n for _, n in renamed_pairs}
            added_paths = sorted(p for p in added_paths if p not in renamed_new)
            deleted_paths = sorted(p for p in deleted_paths if p not in renamed_old)
            modified_paths.sort()

            # Map content keys to indexed blob_ids (0 when the blob was never parsed)
            if use_git:
                needed = [files_v2[p] for p in added_paths + modified_paths] + [files_v1[p] for p in deleted_paths + modified_paths]
                needed += [files_v2[n] for _, n in renamed_pairs]
                blob_meta = git_tree_resolver.map_blob_hashes(cursor, needed)

                def blob_id_of(key: str) -> int:
                    return blob_meta.get(key, (0, 0))[0]
            else:
                def blob_id_of(key: str) -> int:
                    return int(key) if str(key).isdigit() else 0

            added = [{"file": p, "fid": blob_id_of(files_v2[p])} for p in added_paths]
            deleted = [{"file": p, "fid": blob_id_of(files_v1[p])} for p in deleted_paths]
            renamed = [{"file": n, "old_file": o, "fid": blob_id_of(files_v2[n])} for o, n in renamed_pairs]
            modified = [
                {"file": p, "fid_old": blob_id_of(files_v1[p]), "fid_new": blob_id_of(files_v2[p])}
                for p in modified_paths
            ]

            # Level 2 AST & symbol semantic diff on changed files
            for m in modified[:MAX_SYMBOL_DIFF_FILES]:
                old_blob = m.get("fid_old", 0)
                new_blob = m.get("fid_new", 0)
                if old_blob > 0 and new_blob > 0 and old_blob != new_blob:
                    cursor.execute(
                        "SELECT name, type_id FROM m_symbol_def WHERE blob_id = %s;",
                        (old_blob,),
                    )
                    old_syms = {(safe_decode(r["name"]), r["type_id"]) for r in cursor.fetchall()}
                    cursor.execute(
                        "SELECT name, type_id FROM m_symbol_def WHERE blob_id = %s;",
                        (new_blob,),
                    )
                    new_syms = {(safe_decode(r["name"]), r["type_id"]) for r in cursor.fetchall()}
                    m["symbols_added"] = len(new_syms - old_syms)
                    m["symbols_removed"] = len(old_syms - new_syms)

            changes = []
            for a in added:
                changes.append({"type": "added", "file": a["file"], "file_path": a["file"], "name": a["file"], "fid": a.get("fid")})
            for d in deleted:
                changes.append({"type": "deleted", "file": d["file"], "file_path": d["file"], "name": d["file"], "fid": d.get("fid")})
            for r in renamed:
                changes.append({
                    "type": "renamed",
                    "file": r["file"],
                    "file_path": r["file"],
                    "old_file": r["old_file"],
                    "name": r["file"],
                    "fid": r.get("fid"),
                })
            for m in modified:
                changes.append({
                    "type": "modified",
                    "file": m["file"],
                    "file_path": m["file"],
                    "name": m["file"],
                    "fid_old": m.get("fid_old"),
                    "fid_new": m.get("fid_new"),
                    "symbols_added": m.get("symbols_added", 0),
                    "symbols_removed": m.get("symbols_removed", 0),
                })
            changes.sort(key=lambda c: c["file"])

            return {
                "v1": vname1,
                "v2": vname2,
                "path": norm_path,
                "source": "git" if use_git else "db",
                "summary": {
                    "added_count": len(added),
                    "removed_count": len(deleted),
                    "modified_count": len(modified),
                    "renamed_count": len(renamed),
                    "unchanged_count": unchanged_count,
                    "added": len(added),
                    "deleted": len(deleted),
                    "modified": len(modified),
                    "renamed": len(renamed),
                    "unchanged": unchanged_count,
                },
                "added": added,
                "deleted": deleted,
                "modified": modified,
                "renamed": renamed,
                "changes": changes,
            }

    def get_kconfig_diff(self, v1: str, v2: str) -> dict[str, Any]:
        """Compare Kconfig files and symbol definitions between two revisions (Level 3)."""
        with get_db_cursor() as cursor:
            vid1, vname1, vid2, vname2 = self._resolve_pair(cursor, v1, v2)

            empty = {
                "v1": vname1,
                "v2": vname2,
                "summary": {"added": 0, "removed": 0, "changed": 0, "files_added": 0, "files_removed": 0, "files_modified": 0},
                "added": [],
                "removed": [],
                "changed": [],
                "symbols": [],
                "files": [],
                "unindexed_files": [],
            }
            if vname1 == vname2 or (vid1 == vid2 and vid1 > 0):
                return empty

            tree1 = git_tree_resolver.list_tree(vname1)
            tree2 = git_tree_resolver.list_tree(vname2)
            if tree1 is None or tree2 is None:
                syms_v1 = self._fetch_kconfig_symbols(cursor, vid1)
                syms_v2 = self._fetch_kconfig_symbols(cursor, vid2)
                added = sorted(k for k in syms_v2 if k not in syms_v1)
                removed = sorted(k for k in syms_v1 if k not in syms_v2)
                changed = sorted(k for k in syms_v1 if k in syms_v2 and syms_v1[k] != syms_v2[k])
                empty.update({
                    "summary": {**empty["summary"], "added": len(added), "removed": len(removed), "changed": len(changed)},
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                    "symbols": (
                        [{"name": k, "change": "added"} for k in added]
                        + [{"name": k, "change": "removed"} for k in removed]
                        + [{"name": k, "change": "changed"} for k in changed]
                    ),
                })
                return empty

            k1 = {p: s for p, s in tree1.items() if is_kconfig_path(p)}
            k2 = {p: s for p, s in tree2.items() if is_kconfig_path(p)}

            files: list[dict[str, Any]] = []
            for p in sorted(set(k1) | set(k2)):
                if p not in k1:
                    files.append({"file": p, "change": "added"})
                elif p not in k2:
                    files.append({"file": p, "change": "removed"})
                elif k1[p] != k2[p]:
                    files.append({"file": p, "change": "modified"})

            blob_meta = git_tree_resolver.map_blob_hashes(cursor, list(k1.values()) + list(k2.values()))
            unindexed = sorted({p for p, s in k1.items() if s not in blob_meta} | {p for p, s in k2.items() if s not in blob_meta})

            # A file only contributes symbols when every side it exists on is indexed; otherwise an
            # unparsed blob would surface its whole symbol set as spuriously added or removed.
            comparable = {
                p for p in set(k1) | set(k2)
                if (p not in k1 or k1[p] in blob_meta) and (p not in k2 or k2[p] in blob_meta)
            }
            defs_v1 = self._kconfig_defs_by_blob(cursor, {p: s for p, s in k1.items() if p in comparable}, blob_meta)
            defs_v2 = self._kconfig_defs_by_blob(cursor, {p: s for p, s in k2.items() if p in comparable}, blob_meta)

            symbols: list[dict[str, Any]] = []
            for name in sorted(set(defs_v1) | set(defs_v2)):
                before = defs_v1.get(name, [])
                after = defs_v2.get(name, [])
                if not before:
                    change = "added"
                elif not after:
                    change = "removed"
                elif before != after:
                    change = "changed"
                else:
                    continue
                symbols.append({"name": name, "change": change, "before": before, "after": after})

            added = [s["name"] for s in symbols if s["change"] == "added"]
            removed = [s["name"] for s in symbols if s["change"] == "removed"]
            changed = [s["name"] for s in symbols if s["change"] == "changed"]
            return {
                "v1": vname1,
                "v2": vname2,
                "summary": {
                    "added": len(added),
                    "removed": len(removed),
                    "changed": len(changed),
                    "files_added": sum(1 for f in files if f["change"] == "added"),
                    "files_removed": sum(1 for f in files if f["change"] == "removed"),
                    "files_modified": sum(1 for f in files if f["change"] == "modified"),
                },
                "added": added,
                "removed": removed,
                "changed": changed,
                "symbols": symbols,
                "files": files,
                "unindexed_files": unindexed,
            }

    def get_file_diff(self, v1: str, v2: str, path: str, old_path: str = "") -> dict[str, Any]:
        """Unified Git diff of one file between two revisions (old_path for renames)."""
        norm_path = path.strip().strip("/")
        norm_old = (old_path or norm_path).strip().strip("/")
        if not norm_path or not is_safe_rel_path(norm_path) or not is_safe_rel_path(norm_old):
            raise HTTPException(status_code=400, detail="A valid relative file path must be specified.")

        sha1 = git_tree_resolver.resolve_commit_sha(v1)
        sha2 = git_tree_resolver.resolve_commit_sha(v2)
        if sha1 is None or sha2 is None:
            missing = v1 if sha1 is None else v2
            raise HTTPException(status_code=404, detail=f"Version or commit '{missing}' not found in Git.")

        pathspecs = [norm_old] if norm_old == norm_path else [norm_old, norm_path]
        try:
            out = subprocess.run(
                ["git", "-C", str(get_repo_dir()), "diff", "--no-color", "-M", sha1, sha2, "--", *pathspecs],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("git diff %s..%s failed: %s", sha1, sha2, e)
            raise HTTPException(status_code=500, detail="git diff failed") from e
        if out.returncode != 0:
            # stderr carries absolute repository paths: it is logged, not returned
            logger.warning("git diff %s..%s -- %s failed: %s", sha1, sha2, norm_path,
                           out.stderr.decode("utf-8", errors="replace").strip())
            raise HTTPException(status_code=500, detail="git diff failed")

        raw = out.stdout
        truncated = len(raw) > MAX_FILE_DIFF_BYTES
        text = raw[:MAX_FILE_DIFF_BYTES].decode("utf-8", errors="replace")
        additions = deletions = 0
        for line in text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        return {
            "v1": v1,
            "v2": v2,
            "path": norm_path,
            "old_path": norm_old,
            "diff": text,
            "additions": additions,
            "deletions": deletions,
            "is_binary": b"\nBinary files " in raw or raw.startswith(b"Binary files "),
            "truncated": truncated,
        }

    @staticmethod
    def _kconfig_defs_by_blob(
        cursor: Any, kfiles: dict[str, str], blob_meta: dict[str, tuple[int, int]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Return {symbol: [{file, type, prompt, def_val}, ...]} for the indexed Kconfig blobs of one tree."""
        blob_to_paths: dict[int, list[str]] = {}
        for p, sha in kfiles.items():
            if sha in blob_meta:
                blob_to_paths.setdefault(blob_meta[sha][0], []).append(p)
        if not blob_to_paths:
            return {}

        defs: dict[str, set[tuple[str, int, str, str]]] = {}
        blob_ids = list(blob_to_paths)
        chunk_size = 2000
        for i in range(0, len(blob_ids), chunk_size):
            chunk = blob_ids[i:i + chunk_size]
            fmt = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT DISTINCT bt.blob_id, s.name, s.type, s.prompt, s.def_val
                FROM m_kconfig_symbol s
                JOIN m_tag t ON s.ast_id = t.ast_id
                JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
                WHERE bt.blob_id IN ({fmt});
                """,
                chunk,
            )
            for r in cursor.fetchall():
                name = safe_decode(r["name"])
                if not name:
                    continue
                for p in blob_to_paths.get(r["blob_id"], []):
                    defs.setdefault(name, set()).add(
                        (p, r["type"], safe_decode(r["prompt"]) or "", safe_decode(r["def_val"]) or "")
                    )
        # Sorted, de-duplicated records make the before/after comparison order-independent
        return {
            name: [{"file": f, "type": t, "prompt": pr, "def_val": dv} for f, t, pr, dv in sorted(entries)]
            for name, entries in defs.items()
        }

    @staticmethod
    def _fetch_commit_files(cursor: Any, commit_id: int, prefix: str = "", norm_path: str = "") -> dict[str, int]:
        if prefix:
            sql = """
                SELECT COALESCE(NULLIF(fn.fname, ''), CONCAT('file_', bc.fnid)) AS fname, bc.blob_id
                FROM m_bridge_checkpoint bc
                LEFT JOIN m_file_name fn ON bc.fnid = fn.fnid AND fn.fnid > 0
                WHERE bc.commit_id = %s AND (fn.fname LIKE %s OR fn.fname = %s);
            """
            cursor.execute(sql, (commit_id, f"{escape_like(prefix)}%", norm_path))
        else:
            sql = """
                SELECT COALESCE(NULLIF(fn.fname, ''), CONCAT('file_', bc.fnid)) AS fname, bc.blob_id
                FROM m_bridge_checkpoint bc
                LEFT JOIN m_file_name fn ON bc.fnid = fn.fnid AND fn.fnid > 0
                WHERE bc.commit_id = %s;
            """
            cursor.execute(sql, (commit_id,))
        rows = cursor.fetchall()
        if rows:
            return {safe_decode(r["fname"]): r["blob_id"] for r in rows}

        fnid_map = tree_resolver.resolve_tree(commit_id, cursor)
        if not fnid_map:
            return {}

        fnids = list(fnid_map.keys())
        res: dict[str, int] = {}
        chunk_size = 5000
        for i in range(0, len(fnids), chunk_size):
            chunk = fnids[i:i + chunk_size]
            fmt = ",".join(["%s"] * len(chunk))
            if prefix:
                cursor.execute(
                    f"SELECT fnid, fname FROM m_file_name WHERE fnid IN ({fmt}) AND (fname LIKE %s OR fname = %s);",
                    [*chunk, f"{escape_like(prefix)}%", norm_path],
                )
            else:
                cursor.execute(
                    f"SELECT fnid, fname FROM m_file_name WHERE fnid IN ({fmt});",
                    chunk,
                )
            for r in cursor.fetchall():
                fn = safe_decode(r["fname"]) or f"file_{r['fnid']}"
                res[fn] = fnid_map[r["fnid"]]
        return res

    @staticmethod
    def _fetch_kconfig_symbols(cursor: Any, commit_id: int) -> dict[str, str]:
        cursor.execute(
            """
            SELECT s.name, s.def_val
            FROM m_kconfig_symbol s
            JOIN m_tag t ON s.ast_id = t.ast_id
            JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
            JOIN m_bridge_checkpoint bc ON bt.blob_id = bc.blob_id
            WHERE bc.commit_id = %s;
            """,
            (commit_id,),
        )
        rows = cursor.fetchall()
        if rows:
            return {safe_decode(r["name"]): safe_decode(r["def_val"]) for r in rows}

        tree_map = tree_resolver.resolve_tree(commit_id, cursor)
        if not tree_map:
            return {}

        blob_ids = list(set(tree_map.values()))
        syms: dict[str, str] = {}
        chunk_size = 5000
        for i in range(0, len(blob_ids), chunk_size):
            chunk = blob_ids[i:i + chunk_size]
            fmt = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT s.name, s.def_val
                FROM m_kconfig_symbol s
                JOIN m_tag t ON s.ast_id = t.ast_id
                JOIN m_bridge_tag bt ON t.tag_id = bt.tag_id
                WHERE bt.blob_id IN ({fmt});
                """,
                chunk,
            )
            for r in cursor.fetchall():
                syms[safe_decode(r["name"])] = safe_decode(r["def_val"])
        return syms
