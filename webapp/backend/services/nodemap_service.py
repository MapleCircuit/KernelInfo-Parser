"""webapp/backend/services/nodemap_service.py - NodeMap struct layouts with preprocessor guards and source slices.

The database holds where a symbol is defined (`m_symbol_def`: blob, line_s, line_e) but no member guards, so
both endpoints read the file text through Git; the span comes from the database when the revision was parsed
(`span_source: "db"`) and from the pure extractors of `parser/struct_source.py` otherwise (`"git"`).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from parser.struct_source import (
    extract_struct_layout,
    find_definition_span,
    outer_conditions,
)
from webapp.backend.config import RepoScopedCache
from webapp.backend.security.git import safe_rev
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.security.sql import is_valid_identifier
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.symbol_service import SymbolService

log = logging.getLogger(__name__)

MAX_SLICE_LINES = 6000
MAX_CONTEXT = 200
STRUCT_KINDS = ("struct", "union")
SOURCE_KINDS = ("struct", "union", "function", "macro", "enum")

# Parsed layouts are a function of the blob and the span: {(blob sha, line_s, line_e): (items, has_guards, outer conds)}
_LAYOUT_CACHE = RepoScopedCache(maxsize=64)


def _kind_of_type(type_name: str) -> str | None:
    """Definition kind of a raw `m_type_descriptor` name (`C_structdecl`, `CPPro_define`, ...)."""
    t = (type_name or "").lower()
    if "structdecl" in t:
        return "struct"
    if "uniondecl" in t:
        return "union"
    if "enumdecl" in t:
        return "enum"
    if "define" in t:
        return "macro"
    if "function" in t:
        return "function"
    return None


def _clean_path(path: str | None) -> str | None:
    if path is None or not path.strip():
        return None
    norm = path.strip().strip("/")
    if not norm or not is_safe_rel_path(norm):
        raise HTTPException(status_code=400, detail="Invalid path.")
    return norm


class NodeMapService:
    """Struct member trees with guards and file / symbol source slices for the NodeMap canvas."""

    @staticmethod
    def _db_definition(version: str, name: str, kinds: tuple[str, ...], path: str | None, line: int | None = None) -> dict[str, Any] | None:
        """The ranked in-tree definition of `name` of one of `kinds` (in `path` when given), else None.
        `line` picks, among several, the one containing it or starting nearest."""
        try:
            rows = SymbolService().ranked_definitions(version, name, kind="type" if set(kinds) <= {"struct", "union", "enum"} else None)
        except HTTPException:
            return None
        except Exception:
            log.warning("nodemap: definition lookup of %s failed", name, exc_info=True)
            return None
        found = []
        for r in rows:
            k = _kind_of_type(r.get("type_name") or "")
            f = r.get("file")
            if k not in kinds or not f or r.get("line_s") is None or (path and f != path):
                continue
            found.append({"kind": k, "file": f, "line_s": int(r["line_s"]), "line_e": int(r.get("line_e") or r["line_s"])})
        if not found:
            return None
        if line is not None:
            return min(found, key=lambda d: (not d["line_s"] <= line <= d["line_e"], abs(d["line_s"] - line)))
        return found[0]

    @staticmethod
    def _read_text(version: str, path: str) -> tuple[str, str | None]:
        """(text, blob sha) of `path` at `version`: 404 when absent, 400 for a directory, 415 for a binary."""
        raw, is_dir, sha = git_reader.read_file(version, path)
        if is_dir:
            raise HTTPException(status_code=400, detail=f"'{path}' is a directory.")
        if raw is None:
            raise HTTPException(status_code=404, detail=f"File '{path}' not found in version '{version}'.")
        if b"\0" in raw[:8192]:
            raise HTTPException(status_code=415, detail="Binary file cannot be displayed.")
        return raw.decode("utf-8", errors="replace"), sha

    def get_struct(self, version_name: str, name: str, path: str | None = None, kind: str | None = None,
                   line: int | None = None) -> dict[str, Any]:
        version = safe_rev(version_name.strip(), "version")
        clean = (name or "").strip()
        for prefix in ("struct ", "union "):
            if clean.startswith(prefix):
                clean = clean[len(prefix):].strip()
        if not is_valid_identifier(clean):
            raise HTTPException(status_code=400, detail="Invalid struct name.")
        if kind not in (None, "", *STRUCT_KINDS):
            raise HTTPException(status_code=400, detail="kind must be 'struct' or 'union'.")
        kind = kind or None
        norm = _clean_path(path)

        kinds = (kind,) if kind else STRUCT_KINDS
        span = self._db_definition(version, clean, kinds, norm, line)
        span_source = "db"
        if span is not None:
            file_path, found_kind, line_s, line_e = span["file"], span["kind"], span["line_s"], span["line_e"]
            text, sha = self._read_text(version, file_path)
        else:
            if norm is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No definition of '{clean}' is indexed at '{version}'; pass `path` to read it from the Git source.",
                )
            span_source = "git"
            file_path = norm
            text, sha = self._read_text(version, file_path)
            hit = None
            for k in kinds:
                hit = find_definition_span(text, clean, k)
                if hit:
                    break
            if hit is None:
                raise HTTPException(status_code=404, detail=f"No struct or union '{clean}' in '{file_path}' at '{version}'.")
            found_kind, line_s, line_e = hit

        key = (sha, line_s, line_e) if sha else None
        cached = _LAYOUT_CACHE.get(key) if key else None
        if cached is None:
            layout = extract_struct_layout(text, line_s, line_e)
            cached = (layout["items"], layout["has_guards"], outer_conditions(text, line_s))
            if key:
                _LAYOUT_CACHE[key] = cached
        items, has_guards, outer = cached
        return {
            "version": version, "name": clean, "kind": found_kind, "file_path": file_path,
            "line_s": line_s, "line_e": line_e, "span_source": span_source, "guard_source": "source",
            "has_guards": has_guards, "outer_conds": list(outer), "items": items,
        }

    def get_source(self, version_name: str, path: str | None = None, start: int | None = None, end: int | None = None,
                   symbol: str | None = None, kind: str | None = None, context: int = 0) -> dict[str, Any]:
        version = safe_rev(version_name.strip(), "version")
        norm = _clean_path(path)
        symbol = (symbol or "").strip() or None
        if symbol and not is_valid_identifier(symbol):
            raise HTTPException(status_code=400, detail="Invalid symbol name.")
        if kind not in (None, "", *SOURCE_KINDS):
            raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(SOURCE_KINDS)}.")
        kind = kind or None
        if not 0 <= context <= MAX_CONTEXT:
            raise HTTPException(status_code=400, detail=f"context must be between 0 and {MAX_CONTEXT}.")
        if norm is None and symbol is None:
            raise HTTPException(status_code=400, detail="A path or a symbol must be specified.")

        sym: dict[str, Any] | None = None
        if symbol:
            span = self._db_definition(version, symbol, (kind,) if kind else SOURCE_KINDS, norm)
            if span is not None:
                norm = span["file"]
                sym = {"name": symbol, "kind": span["kind"], "line_s": span["line_s"], "line_e": span["line_e"], "span_source": "db"}
            elif norm is None:
                raise HTTPException(status_code=404, detail=f"No definition of '{symbol}' is indexed at '{version}'; pass `path`.")
        assert norm is not None
        text, _ = self._read_text(version, norm)
        lines = text.replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)

        if symbol and sym is None:
            hit = find_definition_span(text, symbol, kind)
            if hit is None:
                raise HTTPException(status_code=404, detail=f"No definition of '{symbol}' in '{norm}' at '{version}'.")
            sym = {"name": symbol, "kind": hit[0], "line_s": hit[1], "line_e": hit[2], "span_source": "git"}

        if sym is not None:
            s = max(1, sym["line_s"] - context)
            e = min(total, sym["line_e"] + context)
        elif start is not None or end is not None:
            if total == 0:
                s, e = 1, 0
            else:
                s = min(max(start if start is not None else 1, 1), total)
                e = min(max(end if end is not None else total, 1), total)
                if s > e:
                    raise HTTPException(status_code=400, detail="start must not exceed end.")
        else:
            s, e = 1, total
        truncated = False
        if e - s + 1 > MAX_SLICE_LINES:
            e = s + MAX_SLICE_LINES - 1
            truncated = True
        return {
            "version": version, "path": norm, "total_lines": total, "start_line": s, "end_line": e,
            "lines": lines[s - 1:e] if e >= s else [], "truncated": truncated, "symbol": sym,
        }


nodemap_service = NodeMapService()
