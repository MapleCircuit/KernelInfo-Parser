"""webapp/backend/services/diff_service.py - 3-Level Semantic Cross-Version Diff Engine.

Level 1: O(1) content blob filter comparing Git blob SHAs (Path C) or tree resolver blob_ids.
Level 2: AST & symbol structural diffing on changed files.
Level 3: Scoped Kconfig file and symbol deltas.

Every side of a comparison may be a release tag, a branch, or an arbitrary commit SHA.
"""
from __future__ import annotations
import json
import logging
import posixpath
import re
import subprocess
import threading
from collections import OrderedDict
from webapp.backend.config import get_repo_dir
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    get_version_info,
)
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.services.tree_resolver import tree_resolver, git_tree_resolver
from webapp.backend.services.history_service import KCONFIG_TYPE_NAMES, kconfig_definitions, kconfig_field_changes
from core.globalstuff import ASTT, is_safe_git_rev

logger = logging.getLogger(__name__)

MAX_SYMBOL_DIFF_FILES = 50
# Files whose definitions Level 2 compares (paths sort documentation and dotfiles first)
SYMBOL_DIFF_SUFFIXES = (".c", ".h", ".S", ".rs")
MAX_FILE_DIFF_BYTES = 512 * 1024
# Rename detection (git diff-tree -M): a deleted and an added path pair up when their contents are
# at least RENAME_SIMILARITY % alike. The quick pass of every comparison compares contents only while
# deleted x added candidates stay within RENAME_LIMIT^2 (beyond that Git pairs exact copies and files
# that kept their basename); search_renames() compares every candidate pair up to MAX_RENAME_SEARCH_PAIRS.
RENAME_SIMILARITY = 50
RENAME_LIMIT = 1000
RENAME_TIMEOUT = 30.0
MAX_RENAME_SEARCH_PAIRS = 60_000_000
RENAME_SEARCH_TIMEOUT = 110.0
_RENAME_LIMIT_RE = re.compile(rb"at least (\d+)")


KCONFIG_TYPE_CODES = {name: code for code, name in KCONFIG_TYPE_NAMES.items()}
# Kconfig blob SHA -> {symbol: [facts]}; content addressed, so valid for every revision and repository
_DEFS_CACHE: OrderedDict[str, dict[str, list[dict[str, Any]]]] = OrderedDict()
_DEFS_CACHE_SIZE = 4096
_DEFS_LOCK = threading.Lock()


def _read_kconfig_blobs(shas: set[str]) -> dict[str, str]:
    """Text of the Git blobs not already parsed (one `git cat-file --batch` process)."""
    with _DEFS_LOCK:
        missing = sorted(s for s in shas if s not in _DEFS_CACHE)
    if not missing:
        return {}
    try:
        out = subprocess.run(
            ["git", "-C", str(get_repo_dir()), "cat-file", "--batch"],
            input="".join(f"{sha}\n" for sha in missing).encode(),
            capture_output=True,
            timeout=60,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("git cat-file for Kconfig blobs failed: %s", e)
        return {}
    texts: dict[str, str] = {}
    pos = 0
    for sha in missing:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl].split()
        if len(header) < 3 or header[1] != b"blob":
            pos = nl + 1
            continue
        size = int(header[2])
        texts[sha] = out[nl + 1:nl + 1 + size].decode("utf-8", errors="replace")
        pos = nl + 1 + size + 1
    return texts


def _blob_definitions(sha: str, text: str | None) -> dict[str, list[dict[str, Any]]]:
    """kconfig_definitions() of one blob (cached by SHA), each with its raw prompt and first default."""
    with _DEFS_LOCK:
        cached = _DEFS_CACHE.get(sha)
        if cached is not None:
            _DEFS_CACHE.move_to_end(sha)
            return cached
    if text is None:  # evicted since the batch read
        text = _read_kconfig_blobs({sha}).get(sha, "")
    from parser.kconfig_ast.kconfig_lexer import KconfigLexer
    from parser.kconfig_ast.kconfig_parser import KconfigParser, KconfigConfig, KconfigIf, KconfigMenu, KconfigChoice

    defs = kconfig_definitions(text)
    # The compatibility fields (`prompt` without its condition, `def_val` = first default value)
    raw: dict[str, list[tuple[str, str]]] = {}
    try:
        items = KconfigParser(KconfigLexer(text).tokenize(), text).parse()
    except Exception:
        items = []

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if isinstance(node, KconfigConfig):
                raw.setdefault(node.name, []).append((node.prompt or "", node.defaults[0][0].to_string() if node.defaults else ""))
            elif isinstance(node, (KconfigIf, KconfigMenu, KconfigChoice)):
                walk(node.children)

    walk(items)
    for name, entries in defs.items():
        for facts, (prompt, first) in zip(entries, raw.get(name, [])):
            facts["prompt_text"] = prompt
            facts["first_default"] = first
        for facts in entries:
            facts.setdefault("prompt_text", "")
            facts.setdefault("first_default", "")
    with _DEFS_LOCK:
        _DEFS_CACHE[sha] = defs
        while len(_DEFS_CACHE) > _DEFS_CACHE_SIZE:
            _DEFS_CACHE.popitem(last=False)
    return defs


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

    def _fetch_tree(self, cursor: Any, vid: int, vname: str) -> tuple[dict[str, str], bool]:
        """Return ({path: content_key}, is_git). Content keys are Git blob SHAs (Path C) or DB blob_ids."""
        git_tree = git_tree_resolver.list_tree(vname)
        if git_tree is not None:
            return git_tree, True
        db_tree = self._fetch_commit_files(cursor, vid)
        return {p: str(b) for p, b in db_tree.items()}, False

    def _fetch_trees(
        self, cursor: Any, v1: str, v2: str,
    ) -> tuple[str, str, dict[str, str], dict[str, str], bool]:
        """Level 1 inputs: (vname1, vname2, {path: content_key} of each side, both from Git)."""
        vid1, vname1, vid2, vname2 = self._resolve_pair(cursor, v1, v2)
        files_v1, git1 = self._fetch_tree(cursor, vid1, vname1)
        files_v2, git2 = self._fetch_tree(cursor, vid2, vname2)
        use_git = git1 and git2
        if not use_git and (git1 or git2):
            # Mixed sources cannot be compared by key; fall back to DB on both sides
            files_v1 = {p: str(b) for p, b in self._fetch_commit_files(cursor, vid1).items()}
            files_v2 = {p: str(b) for p, b in self._fetch_commit_files(cursor, vid2).items()}
        return vname1, vname2, files_v1, files_v2, use_git

    def get_unchanged_files(self, v1: str, v2: str) -> dict[str, Any]:
        """Paths present at both revisions with the same content (the `unchanged_count` of the diff)."""
        with get_db_cursor() as cursor:
            vname1, vname2, files_v1, files_v2, use_git = self._fetch_trees(cursor, v1, v2)
        files = sorted(p for p, key in files_v2.items() if files_v1.get(p) == key)
        return {"v1": vname1, "v2": vname2, "source": "git" if use_git else "db", "count": len(files), "files": files}

    def get_versions_diff(self, v1: str, v2: str) -> dict[str, Any]:
        """Compare file hierarchies between two revisions using 3-level blob-skip diffing."""
        with get_db_cursor() as cursor:
            vname1, vname2, files_v1, files_v2, use_git = self._fetch_trees(cursor, v1, v2)

            added_paths = [p for p in files_v2 if p not in files_v1]
            deleted_paths = [p for p in files_v1 if p not in files_v2]
            modified_paths = [p for p in files_v2 if p in files_v1 and files_v1[p] != files_v2[p]]
            unchanged_count = len(files_v2) - len(added_paths) - len(modified_paths)

            renamed_pairs, rename_info = self._pair_renames(
                vname1, vname2, files_v1, files_v2, added_paths, deleted_paths, use_git,
            )
            renamed_old = {o for o, _, _ in renamed_pairs}
            renamed_new = {n for _, n, _ in renamed_pairs}
            added_paths = sorted(p for p in added_paths if p not in renamed_new)
            deleted_paths = sorted(p for p in deleted_paths if p not in renamed_old)
            modified_paths.sort()

            # Map content keys to indexed blob_ids (0 when the blob was never parsed)
            if use_git:
                needed = [files_v2[p] for p in added_paths + modified_paths] + [files_v1[p] for p in deleted_paths + modified_paths]
                needed += [files_v2[n] for _, n, _ in renamed_pairs]
                blob_meta = git_tree_resolver.map_blob_hashes(cursor, needed)

                def blob_id_of(key: str) -> int:
                    return blob_meta.get(key, (0, 0))[0]
            else:
                def blob_id_of(key: str) -> int:
                    return int(key) if str(key).isdigit() else 0

            added = [{"file": p, "fid": blob_id_of(files_v2[p])} for p in added_paths]
            deleted = [{"file": p, "fid": blob_id_of(files_v1[p])} for p in deleted_paths]
            renamed = [
                {"file": n, "old_file": o, "similarity": sim, "fid": blob_id_of(files_v2[n])}
                for o, n, sim in renamed_pairs
            ]
            modified = [
                {"file": p, "fid_old": blob_id_of(files_v1[p]), "fid_new": blob_id_of(files_v2[p])}
                for p in modified_paths
            ]

            # Level 2 AST & symbol semantic diff on changed files (one query for all of them)
            sym_files = [m for m in modified
                         if m["file"].endswith(SYMBOL_DIFF_SUFFIXES)
                         and m["fid_old"] > 0 and m["fid_new"] > 0 and m["fid_old"] != m["fid_new"]][:MAX_SYMBOL_DIFF_FILES]
            blob_ids = sorted({b for m in sym_files for b in (m["fid_old"], m["fid_new"])})
            syms_by_blob: dict[int, set[tuple[str, int]]] = {b: set() for b in blob_ids}
            if blob_ids:
                cursor.execute(
                    f"SELECT blob_id, name, type_id FROM m_symbol_def WHERE blob_id IN ({','.join(['%s'] * len(blob_ids))});",
                    blob_ids,
                )
                for r in cursor.fetchall():
                    syms_by_blob[r["blob_id"]].add((safe_decode(r["name"]), r["type_id"]))
            for m in sym_files:
                old_syms, new_syms = syms_by_blob[m["fid_old"]], syms_by_blob[m["fid_new"]]
                m["symbols_added"] = len(new_syms - old_syms)
                m["symbols_removed"] = len(old_syms - new_syms)

            # One row per change (diff_view.js filters and pages them client-side)
            changes = []
            for a in added:
                changes.append({"type": "added", "file_path": a["file"], "fid": a.get("fid")})
            for d in deleted:
                changes.append({"type": "deleted", "file_path": d["file"], "fid": d.get("fid")})
            for r in renamed:
                changes.append({
                    "type": "renamed",
                    "file_path": r["file"],
                    "old_file": r["old_file"],
                    "similarity": r["similarity"],
                    "fid": r.get("fid"),
                })
            for m in modified:
                changes.append({
                    "type": "modified",
                    "file_path": m["file"],
                    "fid_old": m.get("fid_old"),
                    "fid_new": m.get("fid_new"),
                    "symbols_added": m.get("symbols_added", 0),
                    "symbols_removed": m.get("symbols_removed", 0),
                })
            changes.sort(key=lambda c: c["file_path"])

            return {
                "v1": vname1,
                "v2": vname2,
                "source": "git" if use_git else "db",
                "summary": {
                    "added_count": len(added),
                    "removed_count": len(deleted),
                    "modified_count": len(modified),
                    "renamed_count": len(renamed),
                    "unchanged_count": unchanged_count,
                },
                "renames": rename_info,
                "changes": changes,
            }

    def _pair_renames(
        self,
        vname1: str,
        vname2: str,
        files_v1: dict[str, str],
        files_v2: dict[str, str],
        added_paths: list[str],
        deleted_paths: list[str],
        use_git: bool,
    ) -> tuple[list[tuple[str, str, int]], dict[str, Any]]:
        """Pair deleted with added paths: Git's similarity detection, then exact content for the rest."""
        info: dict[str, Any] = {"similarity": RENAME_SIMILARITY, "exhaustive": False, "limit_needed": 0, "source": "exact"}
        added_set, deleted_set = set(added_paths), set(deleted_paths)
        pairs: list[tuple[str, str, int]] = []
        if use_git and added_set and deleted_set:
            found = self._git_renames(vname1, vname2, RENAME_SIMILARITY, RENAME_LIMIT, "", RENAME_TIMEOUT)
            if found is not None:
                git_pairs, limit_needed = found
                info.update(source="git", exhaustive=limit_needed == 0, limit_needed=limit_needed)
                for old_p, new_p, sim in git_pairs:
                    if old_p in deleted_set and new_p in added_set:
                        pairs.append((old_p, new_p, sim))
                        deleted_set.discard(old_p)
                        added_set.discard(new_p)
        # Exact renames Git did not report (no Git, or the detection failed): same content key
        added_by_key: dict[str, list[str]] = {}
        for p in sorted(added_set):
            added_by_key.setdefault(files_v2[p], []).append(p)
        for old_p in sorted(deleted_set):
            candidates = added_by_key.get(files_v1[old_p])
            if candidates:
                pairs.append((old_p, candidates.pop(0), 100))
        return pairs, info

    @staticmethod
    def _git_renames(
        rev1: str, rev2: str, similarity: int, limit: int, scope: str, timeout: float,
    ) -> tuple[list[tuple[str, str, int]], int] | None:
        """(renames [(old, new, similarity %)], limit_needed) from git diff-tree -M; None on failure.

        limit_needed is 0 when every candidate pair was compared, else the rename limit Git asked for
        (it then only paired exact copies and files that kept their basename).
        """
        sha1 = git_tree_resolver.resolve_commit_sha(rev1)
        sha2 = git_tree_resolver.resolve_commit_sha(rev2)
        if sha1 is None or sha2 is None:
            return None
        cmd = ["git", "-C", str(get_repo_dir()), "diff-tree", "-r", "-z", "--name-status", "--diff-filter=R",
               f"-M{similarity}%", f"-l{limit}", sha1, sha2]
        if scope:
            cmd += ["--", f":(literal){scope}"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("git diff-tree -M %s..%s failed: %s", sha1, sha2, e)
            return None
        if out.returncode != 0:
            logger.warning("git diff-tree -M %s..%s failed: %s", sha1, sha2,
                           out.stderr.decode("utf-8", errors="replace").strip())
            return None
        fields = out.stdout.split(b"\0")
        pairs: list[tuple[str, str, int]] = []
        i = 0
        while i + 2 < len(fields):
            status = fields[i]
            if status.startswith(b"R"):
                score = int(status[1:] or b"100")
                pairs.append((safe_decode(fields[i + 1]), safe_decode(fields[i + 2]), score))
                i += 3
            else:
                i += 2 if status else 1
        limit_needed = 0
        m = _RENAME_LIMIT_RE.search(out.stderr)
        if m or b"rename detection was skipped" in out.stderr:
            limit_needed = int(m.group(1)) if m else limit + 1
        return pairs, limit_needed

    def search_renames(self, v1: str, v2: str, path: str = "", similarity: int = RENAME_SIMILARITY) -> dict[str, Any]:
        """Exhaustive rename search: compare every deleted with every added path under `path`."""
        norm_path = path.strip().strip("/")
        if norm_path and not is_safe_rel_path(norm_path):
            raise HTTPException(status_code=400, detail="Invalid directory.")
        similarity = max(1, min(100, int(similarity)))
        prefix = f"{norm_path}/" if norm_path else ""
        files_v1 = git_tree_resolver.list_tree(v1)
        files_v2 = git_tree_resolver.list_tree(v2)
        if files_v1 is None or files_v2 is None:
            missing = v1 if files_v1 is None else v2
            raise HTTPException(status_code=404, detail=f"Version or commit '{missing}' not found in Git.")

        def in_scope(p: str) -> bool:
            return not prefix or p.startswith(prefix) or p == norm_path

        added = [p for p in files_v2 if p not in files_v1 and in_scope(p)]
        deleted = [p for p in files_v1 if p not in files_v2 and in_scope(p)]
        # Exact copies are paired by Git before any content comparison; they cost nothing
        deleted_keys = {files_v1[p] for p in deleted}
        exact = sum(1 for p in added if files_v2[p] in deleted_keys)
        candidate_pairs = max(0, len(added) - exact) * max(0, len(deleted) - exact)
        result: dict[str, Any] = {
            "v1": v1,
            "v2": v2,
            "path": norm_path,
            "similarity": similarity,
            "candidates": {"added": len(added), "deleted": len(deleted), "pairs": candidate_pairs},
            "max_pairs": MAX_RENAME_SEARCH_PAIRS,
            "complete": False,
            "reason": "",
            "renames": [],
        }
        if not added or not deleted:
            result["complete"] = True
            return result
        if candidate_pairs > MAX_RENAME_SEARCH_PAIRS:
            result["reason"] = "too_many"
            return result
        # Git compares contents only while each side, and their product, stay within the limit
        limit = max(RENAME_LIMIT, len(added), len(deleted)) + 1
        found = self._git_renames(v1, v2, similarity, limit, norm_path, RENAME_SEARCH_TIMEOUT)
        if found is None:
            result["reason"] = "failed"
            return result
        pairs, limit_needed = found
        added_set, deleted_set = set(added), set(deleted)
        with get_db_cursor() as cursor:
            meta = git_tree_resolver.map_blob_hashes(cursor, [files_v2[n] for _, n, _ in pairs if n in files_v2])
        result["renames"] = [
            {"old_file": o, "file": n, "similarity": sim, "fid": meta.get(files_v2[n], (0, 0))[0]}
            for o, n, sim in pairs if o in deleted_set and n in added_set
        ]
        result["complete"] = limit_needed == 0
        if limit_needed:
            result["reason"] = "too_many"
        return result

    def get_kconfig_diff(self, v1: str, v2: str) -> dict[str, Any]:
        """Compare Kconfig files and symbol definitions between two revisions (Level 3).

        Both trees come from Git and only the Kconfig files whose blob differs are parsed: a file with
        the same blob on both sides defines the same entries. A symbol is `added`/`removed` when no
        Kconfig file of the other side defines it, and `changed` when the definitions in the changed
        files differ in a property (type, prompt, defaults, dependencies including those inherited
        from enclosing if/menu/choice blocks, selects, implies, ranges, choice, options, help); a
        definition that only moved lines is unchanged.
        """
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
                return self._kconfig_diff_from_tags(cursor, empty, vid1, vid2)

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
        changed_paths = {f["file"] for f in files}

        defs_v1 = self._kconfig_defs(k1, changed_paths)
        defs_v2 = self._kconfig_defs(k2, changed_paths)
        candidates = set(defs_v1) | set(defs_v2)
        # Files with the same blob on both sides: whatever they define exists in both revisions
        elsewhere = self._kconfig_names_in(vname2, {p for p in k2 if p not in changed_paths}, candidates)

        symbols: list[dict[str, Any]] = []
        for name in sorted(candidates):
            before = defs_v1.get(name, [])
            after = defs_v2.get(name, [])
            if [self._comparable(d) for d in before] == [self._comparable(d) for d in after]:
                continue
            if not before and name not in elsewhere:
                change = "added"
            elif not after and name not in elsewhere:
                change = "removed"
            else:
                change = "changed"
            entry: dict[str, Any] = {"name": name, "change": change, "before": before, "after": after}
            if len(before) == 1 and len(after) == 1:
                entry["changes"] = kconfig_field_changes(before[0]["facts"], after[0]["facts"])
            symbols.append(entry)

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
            # Both trees are read from Git, so every file is compared (kept for API compatibility)
            "unindexed_files": [],
        }

    @staticmethod
    def _comparable(d: dict[str, Any]) -> tuple:
        """A definition without its line numbers (a definition that only moved is unchanged)."""
        facts = {k: v for k, v in d["facts"].items() if k not in ("line_s", "line_e")}
        return d["file"], json.dumps(facts, sort_keys=True)

    @staticmethod
    def _kconfig_defs(tree: dict[str, str], paths: set[str]) -> dict[str, list[dict[str, Any]]]:
        """{symbol: [definition, ...]} of the given Kconfig files of one tree, parsed from Git blobs."""
        wanted = {p: tree[p] for p in paths if p in tree}
        texts = _read_kconfig_blobs(set(wanted.values()))
        defs: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(wanted):
            for name, entries in _blob_definitions(wanted[path], texts.get(wanted[path])).items():
                for facts in entries:
                    defs.setdefault(name, []).append({
                        "file": path,
                        "line": facts["line_s"],
                        "type": KCONFIG_TYPE_CODES.get(facts["type"], 0),
                        "prompt": facts["prompt_text"],
                        "def_val": facts["first_default"],
                        "facts": {k: v for k, v in facts.items() if k not in ("prompt_text", "first_default")},
                    })
        return defs

    @staticmethod
    def _kconfig_names_in(rev: str, paths: set[str], names: set[str]) -> set[str]:
        """The `names` some file of `paths` defines with `config`/`menuconfig` at `rev` (one git grep)."""
        if not paths or not names or not is_safe_git_rev(rev):
            return set()
        try:
            out = subprocess.run(
                ["git", "-C", str(get_repo_dir()), "grep", "-I", "-H", "-o", "-E",
                 r"^[[:space:]]*(menu)?config[[:space:]]+[A-Za-z0-9_]+", rev, "--", "*Kconfig*"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("git grep for Kconfig definitions at %s failed: %s", rev, e)
            return set()
        found: set[str] = set()
        prefix = f"{rev}:"
        for line in out.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith(prefix):
                line = line[len(prefix):]
            path, _, match = line.partition(":")
            if path not in paths:
                continue
            name = match.split()[-1] if match.split() else ""
            if name in names:
                found.add(name)
        return found

    @staticmethod
    def _kconfig_diff_from_tags(cursor: Any, result: dict[str, Any], vid1: int, vid2: int) -> dict[str, Any]:
        """Without Git trees: compare the Kconfig tags (name -> set of code hashes) of two indexed versions."""
        side1 = DiffService._fetch_kconfig_symbols(cursor, vid1)
        side2 = DiffService._fetch_kconfig_symbols(cursor, vid2)
        added = sorted(k for k in side2 if k not in side1)
        removed = sorted(k for k in side1 if k not in side2)
        changed = sorted(k for k in side1 if k in side2 and side1[k] != side2[k])
        result.update({
            "summary": {**result["summary"], "added": len(added), "removed": len(removed), "changed": len(changed)},
            "added": added,
            "removed": removed,
            "changed": changed,
            "symbols": (
                [{"name": k, "change": "added"} for k in added]
                + [{"name": k, "change": "removed"} for k in removed]
                + [{"name": k, "change": "changed"} for k in changed]
            ),
        })
        return result

    def get_file_diff(self, v1: str, v2: str, path: str, old_path: str = "", similarity: int = RENAME_SIMILARITY) -> dict[str, Any]:
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
                ["git", "-C", str(get_repo_dir()), "diff", "--no-color", f"-M{max(1, min(100, int(similarity)))}%",
                 sha1, sha2, "--", *(f":(literal){p}" for p in pathspecs)],
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
    def _fetch_commit_files(cursor: Any, commit_id: int) -> dict[str, int]:
        cursor.execute(
            """
            SELECT COALESCE(NULLIF(fn.fname, ''), CONCAT('file_', bc.fnid)) AS fname, bc.blob_id
            FROM m_bridge_checkpoint bc
            LEFT JOIN m_file_name fn ON bc.fnid = fn.fnid AND fn.fnid > 0
            WHERE bc.commit_id = %s;
            """,
            (commit_id,),
        )
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
            cursor.execute(f"SELECT fnid, fname FROM m_file_name WHERE fnid IN ({fmt});", chunk)
            for r in cursor.fetchall():
                fn = safe_decode(r["fname"]) or f"file_{r['fnid']}"
                res[fn] = fnid_map[r["fnid"]]
        return res

    @staticmethod
    def _fetch_kconfig_symbols(cursor: Any, commit_id: int) -> dict[str, frozenset[bytes]]:
        """{symbol: code hashes of its Kconfig definition tags} of one indexed version.

        Kconfig entries are tags (m_ast name with type Kconfig_Config/Kconfig_Menuconfig): the tag
        hash identifies the definition text, so two versions differ exactly when the sets differ.
        """
        types = (int(ASTT.Kconfig_Config), int(ASTT.Kconfig_Menuconfig))
        sql_tail = """
            JOIN m_bridge_tag bt ON bt.blob_id = {blob}
            JOIN m_tag t ON t.tag_id = bt.tag_id
            JOIN m_ast a ON a.ast_id = t.ast_id
            WHERE a.type_id IN (%s, %s)
        """
        cursor.execute(
            "SELECT a.name, t.hash FROM m_bridge_checkpoint bc" + sql_tail.format(blob="bc.blob_id") + " AND bc.commit_id = %s;",
            (*types, commit_id),
        )
        rows = cursor.fetchall()
        if not rows:
            tree_map = tree_resolver.resolve_tree(commit_id, cursor)
            blob_ids = sorted(set(tree_map.values())) if tree_map else []
            rows = []
            for i in range(0, len(blob_ids), 5000):
                chunk = blob_ids[i:i + 5000]
                fmt = ",".join(["%s"] * len(chunk))
                cursor.execute(
                    f"""SELECT a.name, t.hash FROM m_bridge_tag bt
                        JOIN m_tag t ON t.tag_id = bt.tag_id
                        JOIN m_ast a ON a.ast_id = t.ast_id
                        WHERE a.type_id IN (%s, %s) AND bt.blob_id IN ({fmt});""",
                    (*types, *chunk),
                )
                rows.extend(cursor.fetchall())
        out: dict[str, set[bytes]] = {}
        for r in rows:
            out.setdefault(safe_decode(r["name"]), set()).add(bytes(r["hash"]))
        return {k: frozenset(v) for k, v in out.items()}

