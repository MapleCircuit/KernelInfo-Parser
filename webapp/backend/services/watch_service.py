"""webapp/backend/services/watch_service.py - What changed for watched items between two revisions (G10, webapp/API.md section 4.12).

Composes Git and the existing services, read-only:
  file    - the blob at both revisions (added / removed / modified / same)
  dir     - `git diff-tree -r --name-status` under the prefix (changed paths, counts)
  symbol  - the definition Go to Definition opens at both revisions, compared after normalizing code
            (modified / cosmetic / added / removed / same)
  kconfig - the option's Kconfig entry text at both revisions, compared the same way
  section - the MAINTAINERS section block at both revisions
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

from fastapi import HTTPException

MAX_ITEMS = 200
MAX_DIR_PATHS = 200
_IDENT = re.compile(r"^(CONFIG_)?[A-Za-z_][A-Za-z0-9_]{0,127}$")


def compare_texts(a: str | None, b: str | None, normalize: Callable[[str], str]) -> str:
    if a is None and b is None:
        return "absent"
    if a is None:
        return "added"
    if b is None:
        return "removed"
    if a == b:
        return "same"
    return "cosmetic" if normalize(a) == normalize(b) else "modified"


def section_block(text: str, name: str) -> str | None:
    """The MAINTAINERS entry whose title line is `name` (up to the next blank line)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == name.strip():
            out = [line]
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    break
                out.append(nxt)
            return "\n".join(out)
    return None


def safe_path(p: str) -> bool:
    return bool(p) and len(p) <= 1024 and not p.startswith("/") and ".." not in p.split("/") and "\0" not in p and "\\" not in p


class WatchChecker:
    def __init__(self, git: Callable[..., str], read: Callable[[str, str], str | None], detail: Callable[[str, str, str], dict[str, Any] | None],
                 normalize: Callable[[str], str]) -> None:
        self.git, self.read, self.detail, self.normalize = git, read, detail, normalize

    def _lines(self, rev: str, path: str, a: int, b: int) -> str | None:
        text = self.read(rev, path)
        if text is None:
            return None
        return "\n".join(text.split("\n")[max(0, a - 1):b])

    def _definition(self, rev: str, name: str, arch: str) -> tuple[str | None, dict[str, Any] | None]:
        d = self.detail(rev, name, arch)
        if not d or not d.get("file_path"):
            return None, None
        line_s = int(d.get("line_s") or (d.get("definition") or {}).get("line_s") or 1)
        # C symbols carry their extent under `definition`, Kconfig ones at the top level
        line_e = int(d.get("line_e") or (d.get("definition") or {}).get("line_e") or line_s)
        return self._lines(rev, d["file_path"], line_s, line_e), d

    def check_item(self, a: str, b: str, item: dict[str, Any]) -> dict[str, Any]:
        t = item.get("type")
        key = str(item.get("key") or "")
        out: dict[str, Any] = {"key": key, "type": t}
        if t in ("file", "dir"):
            path = str(item.get("path") or "").strip("/")
            if not safe_path(path):
                return {**out, "error": "invalid path"}
            if t == "file":
                status = compare_texts(self.read(a, path), self.read(b, path), lambda s: s)
                status = "modified" if status == "cosmetic" else status
                return {**out, "status": status, "changed": status not in ("same", "absent"), "summary": status,
                        "links": {"compare": {"a": a, "b": b, "path": path}, "commits": {"path": path}}}
            rows = [ln.split("\t", 1) for ln in self.git("diff-tree", "-r", "--name-status", a, b, "--", path).splitlines() if "\t" in ln]
            paths = [{"status": s[0], "path": p} for s, p in rows][:MAX_DIR_PATHS]
            return {**out, "status": "modified" if rows else "same", "changed": bool(rows), "summary": f"{len(rows)} file{'s' if len(rows) != 1 else ''} changed",
                    "paths": paths, "truncated": len(rows) > MAX_DIR_PATHS, "links": {"compare": {"a": a, "b": b, "path": path}, "commits": {"path": path}}}
        if t in ("symbol", "kconfig"):
            name = str(item.get("name") or "")
            if not _IDENT.match(name):
                return {**out, "error": "invalid name"}
            arch = str(item.get("arch") or "x86_64")
            if t == "kconfig" and not name.startswith("CONFIG_"):
                name = f"CONFIG_{name}"
            ta, da = self._definition(a, name, arch)
            tb, db = self._definition(b, name, arch)
            status = compare_texts(ta, tb, self.normalize)
            where = (db or da or {})
            return {**out, "status": status, "changed": status in ("modified", "added", "removed"), "summary": status,
                    "file": where.get("file_path"), "line": where.get("line_s"),
                    "links": {"history": {"version": b, "path": where.get("file_path"), "line": where.get("line_s"), "name": name}}}
        if t == "section":
            name = str(item.get("name") or "")
            if not name or len(name) > 200:
                return {**out, "error": "invalid section"}
            sa = section_block(self.read(a, "MAINTAINERS") or "", name)
            sb = section_block(self.read(b, "MAINTAINERS") or "", name)
            status = compare_texts(sa, sb, lambda s: re.sub(r"\s+", " ", s))
            return {**out, "status": status, "changed": status in ("modified", "added", "removed"), "summary": status,
                    "links": {"commits": {"section": name}}}
        return {**out, "error": "unknown type"}

    def check(self, a: str, b: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.check_item(a, b, it) for it in items]


def check_changes(payload: dict[str, Any]) -> dict[str, Any]:
    from webapp.backend.config import get_repo_dir
    from webapp.backend.security.git import safe_rev
    from webapp.backend.services.git_reader import git_reader
    from webapp.backend.services.history_service import normalize_code
    from webapp.backend.services.symbol_service import SymbolService
    from webapp.backend.services.tree_resolver import git_tree_resolver
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"items must list at most {MAX_ITEMS} watches")
    shas = []
    for side in ("from", "to"):
        rev = str(payload.get(side) or "")
        safe_rev(rev)
        sha = git_tree_resolver.resolve_commit_sha(rev)
        if not sha:
            raise HTTPException(status_code=404, detail=f"Revision not found: {rev}")
        shas.append(sha)
    a, b = shas

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(get_repo_dir()), *args], capture_output=True, text=True, timeout=60).stdout

    def read(rev: str, path: str) -> str | None:
        data, is_dir, _ = git_reader.read_file(rev, path)
        return None if data is None or is_dir else data.decode("utf-8", "replace")

    svc = SymbolService()

    def detail(rev: str, name: str, arch: str) -> dict[str, Any] | None:
        try:
            return svc.get_symbol_detail(rev, name, arch, None)
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise

    results = WatchChecker(git, read, detail, normalize_code).check(a, b, items)
    return {"from": payload["from"], "to": payload["to"], "from_sha": a, "to_sha": b, "items": results,
            "changed": sum(1 for r in results if r.get("changed"))}
