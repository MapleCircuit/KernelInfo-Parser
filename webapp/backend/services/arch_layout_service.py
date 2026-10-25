"""webapp/backend/services/arch_layout_service.py - Build layout of an ARCH at a revision, and the
files a compilation reaches through its #include chain.

The parser stores, for the head commit of every ingested version, what `make ARCH=<arch>` uses
(`m_arch_root`, written by `main.processing_arch_layouts()` from `parser/arch_layout.py`). Revisions
without rows (branches, commits never ingested) are evaluated from their Git tree with the same code.
Layouts depend only on the commit's files, so they are cached per (commit, ARCH).

`reachable()` / `resolve_include()` follow `#include` directives with that layout
(`parser/include_graph.py`); each blob's directives are read once (cached by blob SHA) and each walk
is cached per (commit, ARCH, start file).
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from parser.arch_layout import ArchLayout, compute_layouts
from parser.include_graph import IncludeResolver, include_directives
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.tree_resolver import git_tree_resolver

logger = logging.getLogger(__name__)

_CACHE_SIZE = 64
_DIRECTIVES_CACHE_SIZE = 50_000
_WALK_CACHE_SIZE = 32


class ArchLayoutService:
    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, str], ArchLayout | None] = OrderedDict()
        self._directives: OrderedDict[str, list[tuple[str, bool]]] = OrderedDict()
        self._walks: OrderedDict[tuple[str, str, str], dict[str, str | None]] = OrderedDict()
        self._lock = threading.Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._directives.clear()
            self._walks.clear()

    def layout(self, rev: str, arch: str) -> ArchLayout | None:
        """Layout of ARCH=<arch> at `rev` (None when the revision does not build that ARCH)."""
        sha = git_tree_resolver.resolve_commit_sha(rev) if rev else None
        if not sha or not arch:
            return None
        key = (sha, arch)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        layout = self._from_db(sha, arch)
        if layout is None:
            layout = self._from_git(sha, arch)
        with self._lock:
            self._cache[key] = layout
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return layout

    # ------------------------------------------------------------------ includes
    def resolver(self, rev: str, arch: str) -> tuple[IncludeResolver, str, dict[str, str]] | None:
        """(resolver, commit SHA, {path: blob SHA}) for ARCH=<arch> at `rev`, None without a layout."""
        sha = git_tree_resolver.resolve_commit_sha(rev) if rev else None
        layout = self.layout(sha, arch) if sha else None
        files = git_tree_resolver.list_tree(sha) if layout else None
        if not layout or not files:
            return None
        return IncludeResolver(layout, files.__contains__), sha, files

    def resolve_include(self, rev: str, arch: str, name: str, including_file: str | None, quoted: bool) -> str | None:
        """Tree file `#include <name>` / `#include "name"` reads for ARCH=<arch> (None when unresolved)."""
        found = self.resolver(rev, arch)
        return found[0].resolve(name, including_file, quoted) if found else None

    def reachable(self, rev: str, arch: str, start: str) -> dict[str, str | None]:
        """Files the compilation of `start` reads for ARCH=<arch> ({path: including file}, see
        parser/include_graph.py); empty without a layout."""
        found = self.resolver(rev, arch)
        if not found:
            return {}
        resolver, sha, files = found
        key = (sha, arch, start)
        with self._lock:
            if key in self._walks:
                self._walks.move_to_end(key)
                return self._walks[key]
        walk = resolver.reachable(start, lambda path: self._file_directives(sha, path, files))
        with self._lock:
            self._walks[key] = walk
            while len(self._walks) > _WALK_CACHE_SIZE:
                self._walks.popitem(last=False)
        return walk

    def _file_directives(self, sha: str, path: str, files: dict[str, str]) -> list[tuple[str, bool]]:
        blob = files.get(path)
        if not blob:
            return []
        with self._lock:
            hit = self._directives.get(blob)
            if hit is not None:
                self._directives.move_to_end(blob)
                return hit
        try:
            raw, is_dir, _ = git_reader.read_file(sha, path)
        except Exception as exc:
            logger.debug("git read %s:%s failed: %s", sha, path, exc)
            raw, is_dir = None, False
        directives = include_directives(raw.decode("utf-8", errors="replace")) if raw and not is_dir else []
        with self._lock:
            self._directives[blob] = directives
            while len(self._directives) > _DIRECTIVES_CACHE_SIZE:
                self._directives.popitem(last=False)
        return directives

    # ------------------------------------------------------------------ storage
    @staticmethod
    def _from_db(sha: str, arch: str) -> ArchLayout | None:
        try:
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT r.role, r.priority, r.path, r.target
                    FROM m_commit c
                    JOIN m_arch_root r ON r.commit_id = c.commit_id
                    WHERE c.commit_hash = %s AND r.arch = %s;
                    """,
                    (bytes.fromhex(sha), arch),
                )
                rows = cursor.fetchall()
        except Exception as exc:  # table missing (older database) or no database
            logger.debug("m_arch_root unavailable: %s", exc)
            return None
        if not rows:
            return None
        return ArchLayout.from_rows(arch, ((r["role"], r["priority"], _text(r["path"]), _text(r["target"])) for r in rows))

    @staticmethod
    def _from_git(sha: str, arch: str) -> ArchLayout | None:
        files = git_tree_resolver.list_tree(sha)
        if not files:
            return None

        def read(path: str) -> str | None:
            try:
                raw, is_dir, _ = git_reader.read_file(sha, path)
            except Exception as exc:
                logger.debug("git read %s:%s failed: %s", sha, path, exc)
                return None
            return None if raw is None or is_dir else raw.decode("utf-8", errors="replace")

        return compute_layouts(files.keys(), read, [arch]).get(arch)


def _text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value or ""


arch_layout_service = ArchLayoutService()
