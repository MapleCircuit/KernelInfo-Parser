"""webapp/backend/services/history_service.py - Cross-version history of symbol definitions and Kconfig symbols.

A symbol is followed by its identity (m_ast name + type_id), never by its ast_id: a function's ast_id
hashes its whole definition (signature + body), so every code change produces a new one. Each indexed
release resolves the file's blob from Git (`git cat-file --batch-check`, one process per request), the
blob's tags with that identity are looked up, and consecutive versions are compared by tag (tag_ids are
recycled for identical code). Kconfig symbols are followed across every Kconfig file defining them and
each definition is re-parsed so changes are reported per property (type, prompt, depends on, select...).
"""
from __future__ import annotations
import logging
import re
import subprocess
import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Any
from fastapi import HTTPException
from core.globalstuff import ASTT
from parser.kconfig_ast.kconfig_lexer import KconfigLexer
from parser.kconfig_ast.kconfig_parser import KconfigChoice, KconfigConfig, KconfigIf, KconfigMenu, KconfigParser
from webapp.backend.config import get_repo_dir
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import get_active_repo_id, get_version_info, safe_decode
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.git_service import _compute_structured_diff
from webapp.backend.services.tree_resolver import git_tree_resolver

logger = logging.getLogger(__name__)

RC_RE = re.compile(r"-rc\d+$")
KCONFIG_TYPES = (int(ASTT.Kconfig_Config), int(ASTT.Kconfig_Menuconfig))
KCONFIG_TYPE_NAMES = {1: "bool", 2: "tristate", 3: "string", 4: "hex", 5: "int"}
# Statuses that end a run of unchanged versions (the stops of the UI slider)
CHANGE_STATUSES = ("introduced", "added", "modified", "cosmetic", "removed")
LINEAGE_LIMIT = 500
COMMIT_FORMAT = "--format=%H%x00%an%x00%ae%x00%at%x00%s"
_C_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_SAFE_REV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")
# (blob sha, symbol) -> Kconfig definition facts; content addressed, so shared by every repository
_FACTS_CACHE: OrderedDict[tuple[str, str], tuple[dict[str, Any], ...]] = OrderedDict()
_FACTS_LOCK = threading.Lock()


def _git(args: list[str], stdin: bytes | None = None, timeout: float = 30.0) -> bytes | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(get_repo_dir()), *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("git %s failed: %s", args[0], e)
        return None
    return out.stdout if out.returncode == 0 else None


def batch_blob_shas(specs: list[str]) -> list[str | None]:
    """Resolve `<rev>:<path>` specs to blob SHAs (None when missing) with a single git process."""
    if not specs:
        return []
    out = _git(["cat-file", "--batch-check=%(objectname) %(objecttype)"], stdin=("\n".join(specs) + "\n").encode())
    lines = out.decode("utf-8", errors="replace").splitlines() if out else []
    result: list[str | None] = []
    for i in range(len(specs)):
        head, _, kind = lines[i].rpartition(" ") if i < len(lines) else ("", "", "")
        result.append(head if kind == "blob" and len(head) == 40 else None)
    return result


def normalize_code(code: str, strip_c_comments: bool = True) -> str:
    """Code with comments (C family) and all whitespace differences removed: equal means a cosmetic change."""
    if strip_c_comments:
        code = _C_COMMENT_RE.sub(" ", code)
    return " ".join(code.split())


def indexed_versions(cursor: Any, include_rc: bool = False) -> list[dict[str, Any]]:
    """Indexed refs of the active repository, oldest first; release candidates only with include_rc."""
    cursor.execute(
        """
        SELECT r.ref_name, r.commit_id, r.ref_type
        FROM m_git_ref r
        LEFT JOIN m_commit c ON c.commit_id = r.commit_id
        WHERE r.commit_id > 0 AND r.repo_id = %s
        ORDER BY c.committer_date ASC, r.commit_id ASC;
        """,
        (get_active_repo_id(cursor),),
    )
    versions = []
    for r in cursor.fetchall():
        name = safe_decode(r["ref_name"])
        is_release = not RC_RE.search(name)
        if is_release or include_rc:
            versions.append({"vname": name, "vid": r["commit_id"], "is_release": is_release})
    return versions


def tag_codes(cursor: Any, tag_ids: set[int]) -> dict[int, str]:
    """Code snippet of each tag (m_tag_code, content addressed)."""
    codes: dict[int, str] = {}
    ids = sorted(t for t in tag_ids if t)
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        cursor.execute(
            f"""
            SELECT t.tag_id, c.code
            FROM m_tag t
            JOIN m_tag_code c ON c.hash = t.hash
            WHERE t.tag_id IN ({",".join(["%s"] * len(chunk))});
            """,
            chunk,
        )
        for r in cursor.fetchall():
            codes[r["tag_id"]] = safe_decode(r["code"]) or ""
    return codes


def tag_lineage(cursor: Any, tag_id: int) -> set[int]:
    """Transitive closure of m_moved_tag around tag_id, in both directions (bounded)."""
    seen = {tag_id}
    frontier = [tag_id]
    while frontier and len(seen) < LINEAGE_LIMIT:
        fmt = ",".join(["%s"] * len(frontier))
        cursor.execute(
            f"""
            SELECT e_tag_id AS t FROM m_moved_tag WHERE s_tag_id IN ({fmt})
            UNION
            SELECT s_tag_id AS t FROM m_moved_tag WHERE e_tag_id IN ({fmt});
            """,
            frontier + frontier,
        )
        frontier = [r["t"] for r in cursor.fetchall() if r["t"] not in seen]
        seen.update(frontier)
    return seen


def diff_stats(old: str, new: str) -> dict[str, Any]:
    diff = _compute_structured_diff(old, new)
    return {
        "diff": diff,
        "lines_added": sum(1 for d in diff if d["type"] == "add"),
        "lines_removed": sum(1 for d in diff if d["type"] == "del"),
    }


def _cosmetic_or_modified(old: str, new: str, strip_c_comments: bool = True) -> str:
    return "cosmetic" if normalize_code(old, strip_c_comments) == normalize_code(new, strip_c_comments) else "modified"


def _summary(timeline: list[dict[str, Any]]) -> tuple[int, int]:
    present = sum(1 for t in timeline if t["status"] in ("introduced", "added", "modified", "cosmetic", "unchanged"))
    changes = sum(1 for t in timeline if t["status"] in ("modified", "cosmetic"))
    return present, changes


# --- Kconfig definition facts ------------------------------------------------------------------------------

def _expr(e: Any) -> str:
    return e.to_string() if e is not None else ""


def _cond(text: str, cond: Any) -> str:
    return f"{text} if {_expr(cond)}" if cond is not None else text


def _config_facts(cfg: KconfigConfig, inherited: list[str], choice: str | None) -> dict[str, Any]:
    return {
        "line_s": cfg.line_s,
        "line_e": cfg.line_e,
        "kind": "menuconfig" if cfg.is_menuconfig else "config",
        "type": KCONFIG_TYPE_NAMES.get(cfg.sym_type, ""),
        "prompt": _cond(f'"{cfg.prompt}"', cfg.prompt_cond) if cfg.prompt else "",
        "defaults": [_cond(_expr(v), c) for v, c in cfg.defaults],
        "depends_on": [_expr(d) for d in cfg.depends_on],
        "inherited_depends": inherited,
        "selects": [_cond(t, c) for t, c in cfg.selects],
        "implies": [_cond(t, c) for t, c in cfg.implies],
        "ranges": [_cond(f"{_expr(lo)} {_expr(hi)}", c) for lo, hi, c in cfg.ranges],
        "choice": choice or "",
        "options": list(cfg.options),
        "help": cfg.help_text or "",
    }


def _kconfig_file_facts(text: str, sym: str) -> tuple[dict[str, Any], ...]:
    """Every definition of `sym` in one Kconfig file with its properties and the dependencies it
    inherits from enclosing if/menu/choice blocks."""
    try:
        items = KconfigParser(KconfigLexer(text).tokenize(), text).parse()
    except Exception as e:  # malformed historical Kconfig: report the snippet without properties
        logger.debug("Kconfig parse failed for %s: %s", sym, e)
        return ()
    found: list[dict[str, Any]] = []

    def walk(nodes: list[Any], inherited: list[str], choice: str | None) -> None:
        for node in nodes:
            if isinstance(node, KconfigConfig):
                if node.name == sym:
                    found.append(_config_facts(node, inherited, choice))
            elif isinstance(node, KconfigIf):
                walk(node.children, inherited + [_expr(node.cond)], choice)
            elif isinstance(node, KconfigMenu):
                walk(node.children, inherited + [_expr(d) for d in node.depends_on], choice)
            elif isinstance(node, KconfigChoice):
                walk(node.children, inherited + [_expr(d) for d in node.depends_on], node.prompt or node.name or "choice")

    walk(items, [], None)
    return tuple(found)


def kconfig_field_changes(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Property-level differences between two definitions of one Kconfig symbol."""
    if not old or not new:
        return []
    changes: list[dict[str, Any]] = []
    for key in ("kind", "type", "prompt", "choice", "help"):
        if old.get(key) != new.get(key):
            changes.append({"field": key, "before": old.get(key), "after": new.get(key)})
    for key in ("defaults", "depends_on", "inherited_depends", "selects", "implies", "ranges", "options"):
        before, after = old.get(key) or [], new.get(key) or []
        if before != after:
            changes.append({
                "field": key,
                "added": [x for x in after if x not in before],
                "removed": [x for x in before if x not in after],
                "before": before,
                "after": after,
            })
    return changes


# --- Per-change commits ------------------------------------------------------------------------------------

@lru_cache(maxsize=1024)
def _cached_change_commits(repo_dir: str, path: str, from_rev: str, to_rev: str, line_s: int, line_e: int, limit: int) -> tuple[dict[str, Any], ...]:
    rng = f"{from_rev}..{to_rev}" if from_rev else to_rev
    if line_s > 0 and line_e >= line_s:
        args = ["log", f"-L{line_s},{line_e}:{path}", "--no-patch", COMMIT_FORMAT, f"-n{limit}", rng]
    else:
        args = ["log", COMMIT_FORMAT, f"-n{limit}", rng, "--", path]
    out = _git(args, timeout=30.0)
    commits = []
    for line in (out or b"").decode("utf-8", errors="replace").splitlines():
        parts = line.split("\x00")
        if len(parts) < 5 or len(parts[0]) != 40:
            continue
        commits.append({
            "commit_hash": parts[0],
            "commit_hash_short": parts[0][:12],
            "author_name": parts[1],
            "author_email": parts[2],
            "author_date": int(parts[3]) if parts[3].isdigit() else 0,
            "subject": parts[4],
        })
    return tuple(commits)


class HistoryService:
    """Release-by-release history of a symbol definition (C, ASM, Rust...) or of a Kconfig symbol."""

    # --- Symbol definitions ---------------------------------------------------------------------------------

    def get_symbol_history(
        self,
        version_name: str | None,
        path: str,
        ast_id: int | None = None,
        line: int | None = None,
        tag_id: int | None = None,
        name: str | None = None,
        include_rc: bool = False,
    ) -> dict[str, Any]:
        """Trace the definition anchored at path:line (or tag_id) across the indexed releases.

        The anchor is the narrowest definition covering `line` (preferring one named `name`, then
        `ast_id`); when `name` is not defined there (e.g. the cursor is on a call), the definition is
        located through the symbol index first. A Kconfig anchor returns the Kconfig history instead.
        """
        norm_path = (path or "").strip().strip("/")
        with get_db_cursor() as cursor:
            anchor = self._resolve_anchor(cursor, version_name, norm_path, line, ast_id, tag_id, name)
            if anchor["type_id"] in KCONFIG_TYPES:
                return self.get_kconfig_history(anchor["name"], version_name, include_rc)
            norm_path = anchor["path"]
            versions = indexed_versions(cursor, include_rc)
            shas = batch_blob_shas([f"{v['vname']}:{norm_path}" for v in versions])
            blob_meta = git_tree_resolver.map_blob_hashes(cursor, [s for s in shas if s])
            blob_ids = sorted({blob_meta[s][0] for s in shas if s and s in blob_meta})
            lineage = tag_lineage(cursor, anchor["tag_id"])
            cands = self._candidates(cursor, blob_ids, anchor, lineage)
            codes = tag_codes(cursor, {c["tag_id"] for rows in cands.values() for c in rows} | {anchor["tag_id"]})
            cursor.execute("SELECT name FROM m_type_descriptor WHERE type_id = %s;", (anchor["type_id"],))
            type_row = cursor.fetchone()
            cursor.execute("SELECT e_tag_id FROM m_moved_tag WHERE s_tag_id = %s;", (anchor["tag_id"],))
            forward = [r["e_tag_id"] for r in cursor.fetchall()]
            cursor.execute("SELECT s_tag_id FROM m_moved_tag WHERE e_tag_id = %s;", (anchor["tag_id"],))
            backward = [r["s_tag_id"] for r in cursor.fetchall()]

        strip_comments = not norm_path.split("/")[-1].startswith("Kconfig")
        timeline: list[dict[str, Any]] = []
        prev: dict[str, Any] | None = None  # match of the previous version (None: absent)
        last_code: str | None = None
        ref_line = anchor["line_s"]
        prev_vname: str | None = None
        for v, sha in zip(versions, shas):
            entry: dict[str, Any] = {
                **v, "prev_vname": prev_vname, "path": norm_path, "tag_id": None, "line_s": None, "line_e": None,
                "lines_added": 0, "lines_removed": 0, "diff": [], "defs": [],
            }
            prev_vname = v["vname"]
            blob = blob_meta.get(sha) if sha else None
            if sha is not None and blob is None:
                entry["status"] = "unindexed"
                timeline.append(entry)
                continue
            match = self._pick(cands.get(blob[0], []) if blob else [], lineage, ref_line)
            if match is None and prev is not None and blob and anchor["name"] and self._mentions(v["vname"], norm_path, anchor["name"]):
                # The file still names the symbol but its blob has no such tag: an indexing gap, not a removal
                entry["status"] = "unparsed"
                timeline.append(entry)
                continue
            if match is None:
                entry["status"] = "removed" if prev is not None else "absent"
                if prev is not None:
                    entry["defs"] = [{"path": norm_path, "status": "removed", "tag_id": None,
                                      "line_s": prev["line_s"], "line_e": prev["line_e"]}]
                prev = None
                timeline.append(entry)
                continue
            code = codes.get(match["tag_id"], "")
            entry.update({"tag_id": match["tag_id"], "line_s": match["line_s"], "line_e": match["line_e"], "blob_id": blob[0]})
            if last_code is None:
                entry["status"] = "introduced"
            elif prev is None:
                entry["status"] = "added"
                if code != last_code:
                    entry.update(diff_stats(last_code, code))
            elif match["tag_id"] == prev["tag_id"]:
                entry["status"] = "unchanged"
            else:
                entry["status"] = _cosmetic_or_modified(last_code, code, strip_comments)
                entry.update(diff_stats(last_code, code))
            entry["defs"] = [{
                "path": norm_path, "status": entry["status"], "tag_id": match["tag_id"],
                "line_s": match["line_s"], "line_e": match["line_e"], "diff": entry["diff"],
                "lines_added": entry["lines_added"], "lines_removed": entry["lines_removed"],
            }]
            prev, last_code, ref_line = match, code, match["line_s"]
            timeline.append(entry)

        present, changes = _summary(timeline)
        seen_tags = {t["tag_id"] for t in timeline if t["tag_id"]}
        return {
            "kind": "symbol",
            "tag_id": anchor["tag_id"],
            "ast_id": anchor["ast_id"],
            "ast_name": anchor["name"],
            "type_id": anchor["type_id"],
            "type_name": safe_decode(type_row["name"]) if type_row else "",
            "path": norm_path,
            "include_rc": include_rc,
            "moved_to": forward[0] if forward else None,
            "moved_from": backward[0] if backward else None,
            "lineage_tag_ids": [anchor["tag_id"]] + [t for t in sorted(lineage) if t != anchor["tag_id"]][:LINEAGE_LIMIT],
            "total_versions": present,
            "change_count": changes,
            "snippets": {str(t): codes.get(t, "") for t in seen_tags},
            "timeline": timeline,
        }

    def get_tag_history(self, tag_id: int, include_rc: bool = False) -> dict[str, Any]:
        """History of the definition a tag belongs to, located through any blob that holds it."""
        with get_db_cursor() as cursor:
            row = None
            for sql in (
                """
                SELECT fn.fname FROM m_bridge_tag bt
                JOIN m_bridge_checkpoint bc ON bc.blob_id = bt.blob_id
                JOIN m_file_name fn ON fn.fnid = bc.fnid
                WHERE bt.tag_id = %s LIMIT 1;
                """,
                """
                SELECT fn.fname FROM m_bridge_tag bt
                JOIN m_commit_file cf ON cf.blob_id = bt.blob_id
                JOIN m_file_name fn ON fn.fnid = cf.fnid
                WHERE bt.tag_id = %s LIMIT 1;
                """,
            ):
                cursor.execute(sql, (tag_id,))
                row = cursor.fetchone()
                if row:
                    break
        if not row:
            raise HTTPException(status_code=404, detail=f"Tag {tag_id} not found.")
        return self.get_symbol_history(None, safe_decode(row["fname"]), tag_id=tag_id, include_rc=include_rc)

    @staticmethod
    def _anchor_row(cursor: Any, tag_id: int, path: str, line_s: int | None = None) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT t.tag_id, t.ast_id, a.name, a.type_id
            FROM m_tag t JOIN m_ast a ON a.ast_id = t.ast_id
            WHERE t.tag_id = %s LIMIT 1;
            """,
            (tag_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Tag {tag_id} not found.")
        if line_s is None:
            cursor.execute("SELECT line_s FROM m_bridge_tag WHERE tag_id = %s LIMIT 1;", (tag_id,))
            r = cursor.fetchone()
            line_s = r["line_s"] if r else 0
        return {
            "tag_id": row["tag_id"], "ast_id": row["ast_id"], "name": safe_decode(row["name"]) or "",
            "type_id": row["type_id"], "path": path, "line_s": line_s or 0,
        }

    def _resolve_anchor(
        self,
        cursor: Any,
        version_name: str | None,
        path: str,
        line: int | None,
        ast_id: int | None,
        tag_id: int | None,
        name: str | None,
    ) -> dict[str, Any]:
        if tag_id is not None:
            return self._anchor_row(cursor, tag_id, path)
        if not version_name:
            raise HTTPException(status_code=400, detail="version_name is required without tag_id.")
        vid, vname = get_version_info(cursor, version_name)
        rev = vname if vid is not None else version_name
        sha = batch_blob_shas([f"{rev}:{path}"])[0]
        blob = git_tree_resolver.map_blob_hashes(cursor, [sha]).get(sha) if sha else None
        rows: list[dict[str, Any]] = []
        if blob:
            conds = ["bt.blob_id = %s"]
            params: list[Any] = [blob[0]]
            if line:
                conds.append("bt.line_s <= %s AND bt.line_e >= %s")
                params += [line, line]
            cursor.execute(
                f"""
                SELECT bt.tag_id, bt.line_s, bt.line_e, t.ast_id, a.name, a.type_id
                FROM m_bridge_tag bt
                JOIN m_tag t ON t.tag_id = bt.tag_id
                JOIN m_ast a ON a.ast_id = t.ast_id
                WHERE {' AND '.join(conds)}
                ORDER BY (bt.line_e - bt.line_s) ASC;
                """,
                params,
            )
            rows = cursor.fetchall()
            # Tags of this blob holding a definition: one query filtered by blob (a correlated EXISTS on the
            # m_symbol_def view per tag took seconds)
            cursor.execute("SELECT DISTINCT tag_id FROM m_symbol_def WHERE blob_id = %s;", (blob[0],))
            def_tags = {r["tag_id"] for r in cursor.fetchall()}
            for r in rows:
                r["name"] = safe_decode(r["name"]) or ""
                r["is_def"] = r["tag_id"] in def_tags
        named = [r for r in rows if r["is_def"] or r["type_id"] in KCONFIG_TYPES]
        pick = None
        if name:
            bare = name[7:] if name.startswith("CONFIG_") and any(r["type_id"] in KCONFIG_TYPES for r in rows) else name
            pick = next((r for r in rows if r["name"] == bare), None)
        if pick is None and ast_id:
            pick = next((r for r in rows if r["ast_id"] == ast_id), None)
        if pick is None and name:
            # Cursor on a use of the symbol: follow it to its definition
            located = self._locate_definition(cursor, rev, name, {path})
            if located:
                return located
        if pick is None:
            pick = named[0] if named else (rows[0] if rows else None)
        if pick is None:
            if not blob:
                raise HTTPException(status_code=404, detail=f"'{path}' is not indexed at '{rev}'.")
            raise HTTPException(status_code=404, detail=f"No indexed definition at {path}:{line or '?'} in '{rev}'.")
        return {
            "tag_id": pick["tag_id"], "ast_id": pick["ast_id"], "name": pick["name"], "type_id": pick["type_id"],
            "path": path, "line_s": pick["line_s"],
        }

    def _locate_definition(self, cursor: Any, rev: str, name: str, prefer_paths: set[str]) -> dict[str, Any] | None:
        blob_paths = git_tree_resolver.blob_paths(cursor, rev) or {}
        # The definition's own tag (EXPORT_SYMBOL(name) rows carry another tag)
        cursor.execute(
            """
            SELECT d.blob_id, d.tag_id, d.line_s, d.line_e, d.linkage
            FROM m_symbol_def d
            JOIN m_tag t ON t.tag_id = d.tag_id
            JOIN m_ast a ON a.ast_id = t.ast_id
            WHERE d.name = %s AND a.name = %s
            LIMIT 5000;
            """,
            (name, name),
        )
        defs = [r for r in cursor.fetchall() if r["blob_id"] in blob_paths]
        if not defs:
            return None
        # Prefer the requested file, then definitions with a body over one-line declarations, then .c files
        def rank(r: dict[str, Any]) -> tuple[int, int, int, str]:
            p = blob_paths[r["blob_id"]][0]
            return (0 if p in prefer_paths else 1, -(r["line_e"] - r["line_s"]), 0 if p.endswith(".c") else 1, p)
        best = min(defs, key=rank)
        return self._anchor_row(cursor, best["tag_id"], blob_paths[best["blob_id"]][0], best["line_s"])

    @staticmethod
    def _candidates(cursor: Any, blob_ids: list[int], anchor: dict[str, Any], lineage: set[int]) -> dict[int, list[dict[str, Any]]]:
        """Tags of each blob carrying the anchor's identity (name + type), or belonging to its lineage."""
        out: dict[int, list[dict[str, Any]]] = {}
        if not blob_ids:
            return out
        fmt = ",".join(["%s"] * len(blob_ids))
        if anchor["name"] and anchor["name"] != "{}":
            cursor.execute(
                f"""
                SELECT bt.blob_id, bt.tag_id, bt.line_s, bt.line_e, 1 AS same_name
                FROM m_bridge_tag bt
                JOIN m_tag t ON t.tag_id = bt.tag_id
                JOIN m_ast a ON a.ast_id = t.ast_id
                WHERE bt.blob_id IN ({fmt}) AND a.name = %s AND a.type_id = %s;
                """,
                [*blob_ids, anchor["name"], anchor["type_id"]],
            )
        else:
            cursor.execute(
                f"""
                SELECT bt.blob_id, bt.tag_id, bt.line_s, bt.line_e, 1 AS same_name
                FROM m_bridge_tag bt JOIN m_tag t ON t.tag_id = bt.tag_id
                WHERE bt.blob_id IN ({fmt}) AND t.ast_id = %s;
                """,
                [*blob_ids, anchor["ast_id"]],
            )
        rows = cursor.fetchall()
        seen = {(r["blob_id"], r["tag_id"]) for r in rows}
        lin = sorted(lineage)
        for i in range(0, len(lin), 1000):
            chunk = lin[i:i + 1000]
            cursor.execute(
                f"""
                SELECT blob_id, tag_id, line_s, line_e, 0 AS same_name
                FROM m_bridge_tag
                WHERE blob_id IN ({fmt}) AND tag_id IN ({",".join(["%s"] * len(chunk))});
                """,
                [*blob_ids, *chunk],
            )
            rows.extend(r for r in cursor.fetchall() if (r["blob_id"], r["tag_id"]) not in seen)
        for r in rows:
            out.setdefault(r["blob_id"], []).append(r)
        return out

    @staticmethod
    def _mentions(vname: str, path: str, name: str) -> bool:
        """Whether a non-indented line names the symbol (definitions start in column 0, uses are indented)."""
        raw, _, _ = git_reader.read_file(vname, path)
        pattern = rb"^(?![ \t])[^\n]*\b" + re.escape(name.encode()) + rb"\b"
        return raw is not None and re.search(pattern, raw, re.M) is not None

    @staticmethod
    def _pick(cands: list[dict[str, Any]], lineage: set[int], ref_line: int) -> dict[str, Any] | None:
        """Same-name tags first (lineage members preferred), then renamed lineage members; closest line wins."""
        same = [c for c in cands if c["same_name"]]
        pool = [c for c in same if c["tag_id"] in lineage] or same or [c for c in cands if c["tag_id"] in lineage]
        if not pool:
            return None
        return min(pool, key=lambda c: (abs((c["line_s"] or 0) - (ref_line or 0)), c["line_s"] or 0))

    # --- Kconfig symbols ------------------------------------------------------------------------------------

    def get_kconfig_history(self, name: str, version_name: str | None = None, include_rc: bool = False) -> dict[str, Any]:
        """Every definition of CONFIG_<name> across the indexed releases, with property-level changes."""
        sym = (name or "").strip()
        if sym.startswith("CONFIG_"):
            sym = sym[7:]
        if not sym:
            raise HTTPException(status_code=400, detail="Kconfig symbol name is required.")
        with get_db_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT bt.blob_id, bt.tag_id, bt.line_s, bt.line_e
                FROM m_ast a
                JOIN m_tag t ON t.ast_id = a.ast_id
                JOIN m_bridge_tag bt ON bt.tag_id = t.tag_id
                WHERE a.name = %s AND a.type_id IN ({",".join(["%s"] * len(KCONFIG_TYPES))})
                ORDER BY bt.line_s;
                """,
                (sym, *KCONFIG_TYPES),
            )
            tag_rows = cursor.fetchall()
            if not tag_rows:
                raise HTTPException(status_code=404, detail=f"CONFIG_{sym} is not defined in any indexed Kconfig file.")
            by_blob: dict[int, list[dict[str, Any]]] = {}
            for r in tag_rows:
                by_blob.setdefault(r["blob_id"], []).append(r)
            blob_ids = sorted(by_blob)
            fmt = ",".join(["%s"] * len(blob_ids))
            cursor.execute(f"SELECT blob_id, blob_hash FROM m_file_blob WHERE blob_id IN ({fmt});", blob_ids)
            sha_to_blob = {bytes(r["blob_hash"]).hex(): r["blob_id"] for r in cursor.fetchall()}
            cursor.execute(
                f"""
                SELECT DISTINCT fn.fname FROM m_bridge_checkpoint bc JOIN m_file_name fn ON fn.fnid = bc.fnid
                WHERE bc.blob_id IN ({fmt})
                UNION
                SELECT DISTINCT fn.fname FROM m_commit_file cf JOIN m_file_name fn ON fn.fnid = cf.fnid
                WHERE cf.blob_id IN ({fmt});
                """,
                blob_ids + blob_ids,
            )
            paths = sorted({safe_decode(r["fname"]) for r in cursor.fetchall()})
            versions = indexed_versions(cursor, include_rc)
            specs = [f"{v['vname']}:{p}" for v in versions for p in paths]
            shas = batch_blob_shas(specs)
            indexed = git_tree_resolver.map_blob_hashes(cursor, [s for s in shas if s and s not in sha_to_blob])
            codes = tag_codes(cursor, {r["tag_id"] for r in tag_rows})

        timeline: list[dict[str, Any]] = []
        prev_defs: dict[str, list[dict[str, Any]]] = {}  # path -> definitions in the previous version
        last_defs: dict[str, list[dict[str, Any]]] = {}  # path -> last known definitions (across absences)
        ever = False
        prev_vname: str | None = None
        all_paths: set[str] = set()
        for vi, v in enumerate(versions):
            cur_defs: dict[str, list[dict[str, Any]]] = {}
            unknown = 0
            for pi, p in enumerate(paths):
                sha = shas[vi * len(paths) + pi]
                if not sha:
                    continue
                blob_id = sha_to_blob.get(sha)
                if blob_id is None:
                    unknown += sha not in indexed
                    continue
                facts = self._file_facts(v["vname"], p, sha, sym)
                defs = []
                for di, r in enumerate(by_blob[blob_id]):
                    f = next((x for x in facts if x["line_s"] == r["line_s"]), facts[di] if di < len(facts) else None)
                    defs.append({"path": p, "tag_id": r["tag_id"], "line_s": r["line_s"], "line_e": r["line_e"], "facts": f})
                cur_defs[p] = defs
            entry: dict[str, Any] = {**v, "prev_vname": prev_vname, "lines_added": 0, "lines_removed": 0, "defs": []}
            prev_vname = v["vname"]
            if not cur_defs and unknown:
                entry["status"] = "unindexed"
                timeline.append(entry)
                continue
            statuses = set()
            for p in sorted(set(cur_defs) | set(prev_defs)):
                old, new = prev_defs.get(p, []), cur_defs.get(p, [])
                for i in range(max(len(old), len(new))):
                    o = old[i] if i < len(old) else None
                    n = new[i] if i < len(new) else None
                    if n is None:
                        d = {"path": p, "status": "removed", "tag_id": None, "line_s": o["line_s"], "line_e": o["line_e"], "facts": None, "changes": []}
                    else:
                        base = o or (last_defs.get(p, [])[i] if i < len(last_defs.get(p, [])) else None)
                        d = {**n, "changes": []}
                        if o is None:
                            d["status"] = "added"
                        elif o["tag_id"] == n["tag_id"]:
                            d["status"] = "unchanged"
                        else:
                            d["changes"] = kconfig_field_changes(o["facts"], n["facts"])
                            d["status"] = "modified" if d["changes"] else _cosmetic_or_modified(codes.get(o["tag_id"], ""), codes.get(n["tag_id"], ""), False)
                        if base is not None and base["tag_id"] != n["tag_id"]:
                            d.update(diff_stats(codes.get(base["tag_id"], ""), codes.get(n["tag_id"], "")))
                            if o is None:
                                d["changes"] = kconfig_field_changes(base["facts"], n["facts"])
                            entry["lines_added"] += d["lines_added"]
                            entry["lines_removed"] += d["lines_removed"]
                    statuses.add(d["status"])
                    entry["defs"].append(d)
            if not cur_defs:
                entry["status"] = "removed" if prev_defs else "absent"
            elif not ever:
                entry["status"] = "introduced"
                for d in entry["defs"]:
                    d["status"] = "introduced"
            elif not prev_defs:
                entry["status"] = "added"
            elif statuses & {"modified", "added", "removed"}:
                entry["status"] = "modified"
            elif "cosmetic" in statuses:
                entry["status"] = "cosmetic"
            else:
                entry["status"] = "unchanged"
            if cur_defs:
                ever = True
                last_defs.update(cur_defs)
                all_paths.update(cur_defs)
            prev_defs = cur_defs
            timeline.append(entry)

        present, changes = _summary(timeline)
        anchor_v = None
        if version_name:
            anchor_v = next((t for t in timeline if t["vname"] == version_name), None)
        current = anchor_v or next((t for t in reversed(timeline) if t["defs"] and t["status"] != "removed"), None)
        return {
            "kind": "kconfig",
            "name": sym,
            "ast_name": f"CONFIG_{sym}",
            "path": (current["defs"][0]["path"] if current and current["defs"] else (paths[0] if paths else "")),
            "paths": sorted(all_paths),
            "include_rc": include_rc,
            "total_versions": present,
            "change_count": changes,
            "snippets": {str(t): c for t, c in codes.items()},
            "timeline": timeline,
        }

    @staticmethod
    def _file_facts(vname: str, path: str, sha: str, sym: str) -> tuple[dict[str, Any], ...]:
        key = (sha, sym)
        with _FACTS_LOCK:
            if key in _FACTS_CACHE:
                _FACTS_CACHE.move_to_end(key)
                return _FACTS_CACHE[key]
        raw, _, _ = git_reader.read_file(vname, path)
        facts = _kconfig_file_facts(raw.decode("utf-8", errors="replace") if raw is not None else "", sym)
        with _FACTS_LOCK:
            _FACTS_CACHE[key] = facts
            if len(_FACTS_CACHE) > 2048:
                _FACTS_CACHE.popitem(last=False)
        return facts

    # --- Commits behind one change ----------------------------------------------------------------------------

    def get_change_commits(
        self,
        path: str,
        to_rev: str,
        from_rev: str | None = None,
        line_s: int | None = None,
        line_e: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Commits between two releases that touched lines line_s..line_e (at to_rev) of path.

        `git log -L` follows the range back through every commit of from_rev..to_rev; without a line
        range (a removed definition) the commits touching the file are listed.
        """
        norm_path = (path or "").strip().strip("/")
        for rev in (to_rev, from_rev):
            if rev and not _SAFE_REV_RE.match(rev):
                raise HTTPException(status_code=400, detail=f"Invalid revision '{rev}'.")
        if not norm_path or norm_path.startswith("-") or ".." in norm_path.split("/"):
            raise HTTPException(status_code=400, detail="Invalid path.")
        limit = max(1, min(limit, 500))
        try:
            commits = _cached_change_commits(
                str(get_repo_dir()), norm_path, from_rev or "", to_rev, int(line_s or 0), int(line_e or 0), limit,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Git history operation timed out.")
        return {
            "path": norm_path,
            "from": from_rev,
            "to": to_rev,
            "line_s": line_s,
            "line_e": line_e,
            "total": len(commits),
            "commits": list(commits),
        }


history_service = HistoryService()
