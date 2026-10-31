"""webapp/backend/services/tree_resolver.py - Tri-Path TreeResolver & Arch-Aware Definition Ranking.

Implements:
1. TreeResolverService:
   - Path A: Single-file lookup (<0.2ms) walking parent_idx=0 back to checkpoint.
   - Path B: Full-tree resolution (<15ms, LRU cached) applying m_commit_file deltas to checkpoint baseline.
2. GitTreeResolver:
   - Path C: Git-native full-tree listing (`git ls-tree -r`, LRU cached per commit SHA) for any revision,
     annotated with m_file_blob metadata through the content-addressed blob_hash.
3. Definition ranking (`definition_rank`, `best_path`):
   - Orders a symbol's definitions for one ARCH: the architecture trees its build layout uses
     (`ArchScope`, from parser/arch_layout.py: arch/x86/ for x86_64 at v3.0, include/asm-x86_64/ at
     v2.6.12) and the asm-generic headers it takes through generated wrappers first, then generic
     code, include/asm-generic/, other architectures; files of the other word size and weak
     definitions after the rest, prototypes last.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.globalstuff import SymbolLinkage, is_safe_git_rev
from parser.arch_layout import ArchLayout, path_arch
from parser.kbuild_conditions import own_arch_dirs
from webapp.backend.config import get_repo_dir
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import hash_hex, safe_decode

logger = logging.getLogger(__name__)


class TreeResolverService:
    """Dual-path Git tree resolver mapping commit_id and fnid to content-addressable blob_ids."""

    def __init__(self, max_cache_size: int = 128) -> None:
        self._tree_cache: OrderedDict[int, dict[int, int]] = OrderedDict()
        self._checkpoint_cache: dict[int, int] = {}
        self._parent_cache: dict[int, int] = {}
        self._max_cache_size = max_cache_size

    def clear_cache(self) -> None:
        """Clear all in-memory tree and checkpoint caches."""
        self._tree_cache.clear()
        self._checkpoint_cache.clear()
        self._parent_cache.clear()

    def _get_first_parent(self, commit_id: int, cursor: Any) -> int | None:
        """Resolve the primary (first-parent, parent_idx=0) commit_id."""
        if commit_id in self._parent_cache:
            return self._parent_cache[commit_id]

        cursor.execute(
            """
            SELECT parent_commit_id
            FROM m_commit_parent
            WHERE commit_id = %s AND parent_idx = 0
            LIMIT 1;
            """,
            (commit_id,),
        )
        row = cursor.fetchone()
        if row:
            parent_id = row["parent_commit_id"] if isinstance(row, dict) else row[0]
            self._parent_cache[commit_id] = parent_id
            return parent_id
        return None

    def _find_nearest_checkpoint_and_path(
        self, commit_id: int, cursor: Any, max_depth: int = 500
    ) -> tuple[int | None, list[int]]:
        """Walk parent_idx=0 backwards from commit_id to locate nearest m_bridge_checkpoint.
        
        Returns:
            (checkpoint_commit_id, forward_delta_commits)
            where forward_delta_commits is ordered chronologically forward [C_chk+1, ..., commit_id].
        """
        if commit_id in self._checkpoint_cache:
            chk_id = self._checkpoint_cache[commit_id]
            # Build forward path from chk_id to commit_id if cached
            curr = commit_id
            path_rev: list[int] = []
            while curr != chk_id and curr is not None and len(path_rev) <= max_depth:
                path_rev.append(curr)
                curr = self._get_first_parent(curr, cursor)
            if curr == chk_id:
                return chk_id, list(reversed(path_rev))

        curr = commit_id
        path_rev = []
        chk_id: int | None = None

        while curr is not None and curr > 0 and len(path_rev) <= max_depth:
            # Check if curr is a checkpoint commit
            cursor.execute(
                "SELECT 1 FROM m_bridge_checkpoint WHERE commit_id = %s LIMIT 1;",
                (curr,),
            )
            if cursor.fetchone():
                chk_id = curr
                break
            path_rev.append(curr)
            curr = self._get_first_parent(curr, cursor)

        if chk_id is not None:
            self._checkpoint_cache[commit_id] = chk_id
        return chk_id, list(reversed(path_rev))

    def resolve_file_blob(self, commit_id: int, fnid: int, cursor: Any = None) -> int | None:
        """Path A: Single-file lookup (<0.2ms) walking parent_idx=0 back to checkpoint."""
        if commit_id <= 0 or fnid <= 0:
            return None

        # Check full-tree LRU cache first
        if commit_id in self._tree_cache:
            return self._tree_cache[commit_id].get(fnid)

        if cursor is not None:
            return self._resolve_file_blob_with_cursor(commit_id, fnid, cursor)

        with get_db_cursor() as cur:
            return self._resolve_file_blob_with_cursor(commit_id, fnid, cur)

    def _resolve_file_blob_with_cursor(self, commit_id: int, fnid: int, cursor: Any) -> int | None:
        chk_id, forward_path = self._find_nearest_checkpoint_and_path(commit_id, cursor)

        # Check deltas along forward path (from newest commit_id backwards to oldest)
        if forward_path:
            format_strings = ",".join(["%s"] * len(forward_path))
            sql = f"""
                SELECT commit_id, blob_id, change_type
                FROM m_commit_file
                WHERE fnid = %s AND commit_id IN ({format_strings});
            """
            cursor.execute(sql, [fnid, *forward_path])
            rows = cursor.fetchall()
            if rows:
                commit_to_delta = {
                    (r["commit_id"] if isinstance(r, dict) else r[0]): (
                        r["blob_id"] if isinstance(r, dict) else r[1],
                        safe_decode(r["change_type"] if isinstance(r, dict) else r[2]),
                    )
                    for r in rows
                }
                for cid in reversed(forward_path):
                    if cid in commit_to_delta:
                        blob_id, change_type = commit_to_delta[cid]
                        if change_type == "D" or blob_id == 0:
                            return None
                        return blob_id

        # If no delta was applied along the path, check baseline checkpoint
        if chk_id is not None:
            cursor.execute(
                """
                SELECT blob_id
                FROM m_bridge_checkpoint
                WHERE commit_id = %s AND fnid = %s
                LIMIT 1;
                """,
                (chk_id, fnid),
            )
            row = cursor.fetchone()
            if row:
                blob_id = row["blob_id"] if isinstance(row, dict) else row[0]
                return blob_id if blob_id != 0 else None

        return None

    def resolve_tree(self, commit_id: int, cursor: Any = None) -> dict[int, int]:
        """Path B: Full-tree resolution (<15ms, LRU cached) applying m_commit_file deltas to checkpoint."""
        if commit_id <= 0:
            return {}

        if commit_id in self._tree_cache:
            self._tree_cache.move_to_end(commit_id)
            return dict(self._tree_cache[commit_id])

        if cursor is not None:
            return self._resolve_tree_with_cursor(commit_id, cursor)

        with get_db_cursor() as cur:
            return self._resolve_tree_with_cursor(commit_id, cur)

    def _resolve_tree_with_cursor(self, commit_id: int, cursor: Any) -> dict[int, int]:
        chk_id, forward_path = self._find_nearest_checkpoint_and_path(commit_id, cursor)

        # 1. Load baseline tree from checkpoint
        if chk_id is not None and chk_id in self._tree_cache:
            tree_map = dict(self._tree_cache[chk_id])
        elif chk_id is not None:
            cursor.execute(
                "SELECT fnid, blob_id FROM m_bridge_checkpoint WHERE commit_id = %s;",
                (chk_id,),
            )
            rows = cursor.fetchall()
            tree_map = {
                (r["fnid"] if isinstance(r, dict) else r[0]): (r["blob_id"] if isinstance(r, dict) else r[1])
                for r in rows
                if (r["blob_id"] if isinstance(r, dict) else r[1]) != 0
            }
            self._cache_tree(chk_id, dict(tree_map))
        else:
            tree_map = {}

        # 2. Apply forward deltas along first-parent path
        if forward_path:
            format_strings = ",".join(["%s"] * len(forward_path))
            sql = f"""
                SELECT commit_id, fnid, blob_id, change_type
                FROM m_commit_file
                WHERE commit_id IN ({format_strings});
            """
            cursor.execute(sql, forward_path)
            rows = cursor.fetchall()
            deltas_by_commit: dict[int, list[tuple[int, int, str]]] = {cid: [] for cid in forward_path}
            for r in rows:
                cid = r["commit_id"] if isinstance(r, dict) else r[0]
                fnid = r["fnid"] if isinstance(r, dict) else r[1]
                blob_id = r["blob_id"] if isinstance(r, dict) else r[2]
                change_type = safe_decode(r["change_type"] if isinstance(r, dict) else r[3])
                if cid in deltas_by_commit:
                    deltas_by_commit[cid].append((fnid, blob_id, change_type))

            for cid in forward_path:
                for fnid, blob_id, change_type in deltas_by_commit[cid]:
                    if change_type == "D" or blob_id == 0:
                        tree_map.pop(fnid, None)
                    else:
                        tree_map[fnid] = blob_id

        self._cache_tree(commit_id, dict(tree_map))
        return dict(tree_map)

    def _cache_tree(self, commit_id: int, tree_map: dict[int, int]) -> None:
        self._tree_cache[commit_id] = tree_map
        if len(self._tree_cache) > self._max_cache_size:
            self._tree_cache.popitem(last=False)


BLOB_PATHS_TTL = 120.0


class GitTreeResolver:
    """Path C: Git-native full-tree resolver for any revision (tag, branch, or arbitrary commit).

    The relational commit DAG only covers ingested commits, so Path A/B cannot reconstruct the
    tree of a commit whose first-parent chain leaves the indexed set. The Git object store is
    always complete, so this resolver lists the tree directly and joins m_file_blob through the
    content-addressed blob_hash (blob_hash == Git blob SHA-1).
    """

    def __init__(self, repo_dir: Path | str | None = None, max_cache_size: int = 16) -> None:
        self._pinned_repo_dir = Path(repo_dir) if repo_dir else None
        self._lock = threading.Lock()
        self._tree_cache: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._size_cache: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._blob_path_cache: OrderedDict[str, tuple[float, dict[int, list[str]]]] = OrderedDict()
        self._max_cache_size = max_cache_size

    @property
    def repo_dir(self) -> Path:
        """Pinned repository, else the repository selected for the current request."""
        return self._pinned_repo_dir or get_repo_dir()

    def clear_cache(self) -> None:
        with self._lock:
            self._tree_cache.clear()
            self._size_cache.clear()
            self._blob_path_cache.clear()

    def _run_git(self, args: list[str], timeout: float = 30.0) -> bytes | None:
        if not self.repo_dir.exists():
            return None
        try:
            out = subprocess.run(
                ["git", "-C", str(self.repo_dir), *args],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("git %s failed: %s", args[0], e)
            return None
        return out.stdout if out.returncode == 0 else None

    def resolve_commit_sha(self, rev: str) -> str | None:
        """Resolve any revision (tag, branch, short/full SHA) to its full 40-char commit SHA."""
        # Only a plain ref name or SHA may reach the command line: git reads "-..." as an option
        if not is_safe_git_rev(rev):
            return None
        out = self._run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], timeout=5.0)
        sha = out.decode("ascii", errors="replace").strip() if out else ""
        return sha if len(sha) == 40 else None

    def list_tree(self, rev: str) -> dict[str, str] | None:
        """Return {path: blob_sha_hex} for every blob in the tree of rev, or None if unresolvable."""
        sha = self.resolve_commit_sha(rev)
        if sha is None:
            return None

        with self._lock:
            if sha in self._tree_cache:
                self._tree_cache.move_to_end(sha)
                return self._tree_cache[sha]

        out = self._run_git(["ls-tree", "-r", "-z", "--full-tree", sha])
        if out is None:
            return None

        tree: dict[str, str] = {}
        for entry in out.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            parts = meta.split()
            # Submodules (type 'commit') carry no blob content
            if len(parts) != 3 or parts[1] != b"blob":
                continue
            tree[raw_path.decode("utf-8", errors="surrogateescape")] = parts[2].decode("ascii")

        with self._lock:
            self._tree_cache[sha] = tree
            if len(self._tree_cache) > self._max_cache_size:
                self._tree_cache.popitem(last=False)
        return tree

    def list_tree_sizes(self, rev: str) -> dict[str, int] | None:
        """Return {path: blob size in bytes} for every blob in the tree of rev (LRU cached per commit SHA)."""
        sha = self.resolve_commit_sha(rev)
        if sha is None:
            return None
        key = f"sizes:{sha}"
        with self._lock:
            if key in self._size_cache:
                self._size_cache.move_to_end(key)
                return self._size_cache[key]

        out = self._run_git(["ls-tree", "-r", "-l", "-z", "--full-tree", sha], timeout=120)
        if out is None:
            return None
        sizes: dict[str, int] = {}
        for entry in out.split(b"\0"):
            meta, _, raw_path = entry.partition(b"\t")
            parts = meta.split()
            if len(parts) != 4 or parts[1] != b"blob":
                continue
            try:
                sizes[raw_path.decode("utf-8", errors="surrogateescape")] = int(parts[3])
            except ValueError:
                continue

        with self._lock:
            self._size_cache[key] = sizes
            if len(self._size_cache) > self._max_cache_size:
                self._size_cache.popitem(last=False)
        return sizes

    def blob_paths(self, cursor: Any, rev: str) -> dict[int, list[str]] | None:
        """Return {blob_id: [paths]} for the indexed blobs in the tree of rev, or None if rev is unresolvable.

        Symbols are content-addressed (m_symbol_def.blob_id), so this scopes symbol queries to any
        revision, including commits without an m_bridge_checkpoint snapshot. Cached per commit SHA
        for BLOB_PATHS_TTL seconds so blobs indexed by a running parse show up without a restart.
        """
        sha = self.resolve_commit_sha(rev)
        if sha is None:
            return None
        now = time.monotonic()
        with self._lock:
            hit = self._blob_path_cache.get(sha)
            if hit is not None and now - hit[0] < BLOB_PATHS_TTL:
                self._blob_path_cache.move_to_end(sha)
                return hit[1]

        tree = self.list_tree(sha)
        if tree is None:
            return None
        meta = self.map_blob_hashes(cursor, tree.values())
        paths: dict[int, list[str]] = {}
        for path, blob_sha in tree.items():
            m = meta.get(blob_sha)
            if m is not None:
                paths.setdefault(m[0], []).append(path)
        for v in paths.values():
            v.sort()

        with self._lock:
            self._blob_path_cache[sha] = (now, paths)
            self._blob_path_cache.move_to_end(sha)
            if len(self._blob_path_cache) > 8:
                self._blob_path_cache.popitem(last=False)
        return paths

    @staticmethod
    def map_blob_hashes(cursor: Any, blob_shas: Iterable[str]) -> dict[str, tuple[int, int]]:
        """Map Git blob SHA hex strings to (blob_id, ftype) indexed in m_file_blob."""
        unique = sorted({s for s in blob_shas if s and len(s) == 40})
        result: dict[str, tuple[int, int]] = {}
        chunk_size = 2000
        for i in range(0, len(unique), chunk_size):
            chunk = [bytes.fromhex(s) for s in unique[i:i + chunk_size]]
            fmt = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"SELECT blob_hash, blob_id, ftype FROM m_file_blob WHERE blob_hash IN ({fmt});",
                chunk,
            )
            for r in cursor.fetchall():
                h = r["blob_hash"] if isinstance(r, dict) else r[0]
                b_id = r["blob_id"] if isinstance(r, dict) else r[1]
                ftype = r["ftype"] if isinstance(r, dict) else r[2]
                key = hash_hex(h)
                # The same content may be indexed under several ftypes; prefer a typed (non-directory) row
                if key not in result or (result[key][1] == 0 and ftype != 0):
                    result[key] = (b_id, ftype)
        return result


# Module-level singletons so LRU caches survive across requests
tree_resolver = TreeResolverService()
git_tree_resolver = GitTreeResolver()


# ---------------------------------------------------------------------------- definition ranking
# Ranking groups of a definition's path for one architecture
ARCH_OWN, ARCH_GENERIC, ARCH_ASM_GENERIC, ARCH_OTHER = 0, 1, 2, 3
_LINKAGE_RANK = {SymbolLinkage.WeakOrAliasDef: 1, SymbolLinkage.PrototypeDecl: 2}


def arch_bits(arch: str) -> int | None:
    """Word size an ARCH value implies (x86_64 -> 64, i386 -> 32), None for generic ones (x86, sparc)."""
    arch = (arch or "").lower()
    if "64" in arch:
        return 64
    if arch in ("i386", "x86_32") or "32" in arch:
        return 32
    return None


@dataclass(frozen=True)
class ArchScope:
    """What one ARCH uses at one revision, for ranking: its architecture trees (path_arch names) and the
    asm-generic headers its generated wrappers include."""
    srcarch: str
    trees: frozenset[str]
    headers: frozenset[str] = frozenset()

    @classmethod
    def of(cls, scope: ArchScope | str) -> ArchScope:
        """The scope itself, or the fallback scope of a bare SRCARCH (own_arch_dirs)."""
        if isinstance(scope, ArchScope):
            return scope
        return cls(scope, frozenset(own_arch_dirs(scope)))

    @classmethod
    def from_layout(cls, layout: ArchLayout) -> ArchScope:
        return cls(layout.srcarch, frozenset(layout.arch_trees()), frozenset(layout.generic_headers()))


def path_arch_group(path: str, scope: ArchScope | str) -> int:
    """ARCH_OWN for the ARCH's architecture trees and the asm-generic headers it uses, ARCH_OTHER for other
    architecture trees, ARCH_ASM_GENERIC for the other asm-generic headers, else ARCH_GENERIC."""
    scope = ArchScope.of(scope)
    if path in scope.headers:
        return ARCH_OWN
    arch = path_arch(path)
    if arch is not None:
        return ARCH_OWN if arch in scope.trees else ARCH_OTHER
    if path.startswith(("include/asm-generic/", "include/uapi/asm-generic/")):
        return ARCH_ASM_GENERIC
    return ARCH_GENERIC


def _bits_penalty(path: str, bits: int | None) -> int:
    """1 for files of the other word size (process_32.c, page_64.h), else 0."""
    if not bits:
        return 0
    other = 32 if bits == 64 else 64
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    return 1 if re.search(rf"(?:^|[_-]){other}(?:$|[_-])", stem) else 0


def path_rank(path: str | None, srcarch: ArchScope | str, bits: int | None) -> tuple[int, int, str]:
    """Sort key of a definition's path: own arch, generic, asm-generic, other arches; right word size first."""
    if not path:
        return (ARCH_OTHER + 1, 0, "")
    return (path_arch_group(path, srcarch), _bits_penalty(path, bits), path)


def best_path(paths: list[str] | None, srcarch: ArchScope | str, bits: int | None) -> str | None:
    """The best-ranked of the paths a blob sits at."""
    return min(paths, key=lambda p: path_rank(p, srcarch, bits)) if paths else None


def definition_rank(path: str | None, linkage: Any, line_s: int | None, srcarch: ArchScope | str, bits: int | None) -> tuple:
    """Sort key of a symbol definition: real definitions before weak ones and prototypes, then by path."""
    try:
        link_rank = _LINKAGE_RANK.get(SymbolLinkage(int(linkage)), 0)
    except (TypeError, ValueError):
        link_rank = 0
    return (1 if link_rank == 2 else 0, *path_rank(path, srcarch, bits)[:2], link_rank, path or "", line_s or 0)


DEFAULT_DEFINITION_ARCH = "x86_64"


def definition_arch(version_name: str, arch: str | None) -> tuple[str, ArchScope, int | None]:
    """(ARCH, ArchScope, word size) used to rank definitions; unknown ARCH values fall back to x86_64.

    The scope comes from the ARCH's build layout at the revision (webapp/backend/services/
    arch_layout_service.py); without one, the SRCARCH's own_arch_dirs are assumed.
    """
    from webapp.backend.services.arch_layout_service import arch_layout_service
    from webapp.backend.services.kconfig_service import KconfigService

    for candidate in (arch or DEFAULT_DEFINITION_ARCH, DEFAULT_DEFINITION_ARCH):
        try:
            info = KconfigService().resolve_arch(version_name, candidate)
        except Exception as exc:
            logger.debug("ARCH %s not resolvable at %s: %s", candidate, version_name, exc)
            continue
        layout = arch_layout_service.layout(info["rev"], info["arch"])
        scope = ArchScope.from_layout(layout) if layout else ArchScope.of(info["srcarch"])
        return info["arch"], scope, arch_bits(info["arch"])
    return DEFAULT_DEFINITION_ARCH, ArchScope.of("x86"), 64
