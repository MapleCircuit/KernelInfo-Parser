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
from core.globalstuff import ASTT, is_safe_git_rev, is_safe_git_spec
from parser.kconfig_ast.kconfig_definitions import (  # noqa: F401 - re-exported
    KCONFIG_TYPE_NAMES,
    _cond,
    _config_facts,
    _expr,
    kconfig_definitions,
    kconfig_field_changes,
    without_raw_fields,
)
from webapp.backend.services import kconfig_facts
from webapp.backend.config import get_repo_dir
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    finished_ref_sql,
    get_active_network_id,
    get_active_repo_id,
    get_version_info,
    hash_hex,
    safe_decode,
)
from webapp.backend.services.git_reader import git_reader
from webapp.backend.services.git_service import _compute_structured_diff
from webapp.backend.services.tree_resolver import DEFAULT_DEFINITION_ARCH, definition_arch, definition_rank, git_tree_resolver

logger = logging.getLogger(__name__)

# Pre-release tags (v3.1-rc2, v2.6.16.30-pre1, ...), the markers of PRERELEASE_MARKERS in routers/versions.py:
# hidden from a timeline unless include_rc
RC_RE = re.compile(r"-(?:rc|pre|alpha|beta)\d*$")
REF_TAG, REF_BRANCH = 1, 2
KCONFIG_TYPES = (int(ASTT.Kconfig_Config), int(ASTT.Kconfig_Menuconfig))
LINEAGE_LIMIT = 500
COMMIT_FORMAT = "--format=%H%x00%an%x00%ae%x00%at%x00%s"
_C_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
# Kconfig definitions are cached per blob SHA in webapp/backend/services/kconfig_facts.py (shared with
# Compare's Kconfig diff); kept as a name for sync_service's cache list
_FACTS_CACHE = kconfig_facts._DEFS_CACHE


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
    # Answers are matched to specs by position, and cat-file reads one request per line, so a spec
    # holding a newline would shift every later answer onto the wrong path: such specs are not sent.
    sent = [i for i, spec in enumerate(specs) if is_safe_git_spec(spec)]
    result: list[str | None] = [None] * len(specs)
    if not sent:
        return result
    out = _git(["cat-file", "--batch-check=%(objectname) %(objecttype)"],
               stdin=("\n".join(specs[i] for i in sent) + "\n").encode())
    lines = out.decode("utf-8", errors="replace").splitlines() if out else []
    for pos, idx in enumerate(sent):
        head, _, kind = lines[pos].rpartition(" ") if pos < len(lines) else ("", "", "")
        result[idx] = head if kind == "blob" and len(head) == 40 else None
    return result


def normalize_code(code: str, strip_c_comments: bool = True) -> str:
    """Code with comments (C family) and all whitespace differences removed: equal means a cosmetic change."""
    if strip_c_comments:
        code = _C_COMMENT_RE.sub(" ", code)
    return " ".join(code.split())


def indexed_versions(cursor: Any, include_rc: bool = False, scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Indexed refs of the active repository, oldest first; release candidates only with include_rc.

    `scope` (history_scope()) keeps one branch: the tags reachable from it and the branch's own ref,
    so a timeline never interleaves releases of other branches (v2.6.16.62 is on stable/linux-2.6.16.y,
    not on master). Without it every indexed ref is listed.
    """
    cursor.execute(
        f"""
        SELECT r.ref_name, r.commit_id, r.ref_type
        FROM m_git_ref r
        LEFT JOIN m_commit c ON c.commit_id = r.commit_id
        WHERE r.commit_id > 0 AND r.repo_id = %s AND {finished_ref_sql()}
        ORDER BY c.committer_date ASC, r.commit_id ASC;
        """,
        (get_active_repo_id(cursor),),
    )
    tags = scope["tags"] if scope else None
    versions = []
    seen: set[str] = set()
    rc_count = 0
    for r in cursor.fetchall():
        name = safe_decode(r["ref_name"])
        if name in seen:
            continue
        if tags is not None:
            on_branch = name in tags if r["ref_type"] == REF_TAG else (r["ref_type"] == REF_BRANCH and name == scope["branch"])
            if not on_branch:
                continue
        seen.add(name)
        is_release = not RC_RE.search(name)
        rc_count += not is_release
        if is_release or include_rc:
            versions.append({"vname": name, "vid": r["commit_id"], "is_release": is_release})
    if scope is not None:
        scope["rc_count"] = rc_count
    return versions


def _version_numbers(name: str) -> list[str]:
    """Leading dotted numbers of a version name: v2.6.32.71 -> ['2', '6', '32', '71']."""
    m = re.search(r"\d+(?:\.\d+)*", name or "")
    return m.group(0).split(".") if m else []


def history_scope(version_name: str | None, branch: str | None = None) -> dict[str, Any]:
    """The branch a timeline follows, with the tags reachable from it: {branch, tags, auto, candidates}.

    `branch` given: that branch (400 when not a plain ref, 404 unknown, 503 when Git could not walk it).
    Otherwise it is chosen for `version_name`: the version itself when it names a branch, the default
    branch when the version is one of its tags (or is not a tag at all, e.g. a commit), else the
    branches whose name carries the version's release number (stable/linux-2.6.32.y for v2.6.32.71),
    the first one reaching it; the default branch when none does. Branch tags are walked once per
    branch head and cached (routers/versions.py branch_tags()).
    """
    # The branch-tag walk and its cache live with GET /api/branch/tags (imported lazily: routers import services)
    from webapp.backend.routers.versions import _branch_head, branch_tags, get_branches

    def walk(name: str) -> set[str] | None:
        head = _branch_head(name)
        if not head:
            return None
        tags = branch_tags(head)
        return set(tags) if tags is not None else None

    if branch:
        if not is_safe_git_rev(branch):
            raise HTTPException(status_code=400, detail=f"Invalid branch: {branch}")
        if not _branch_head(branch):
            raise HTTPException(status_code=404, detail=f"Unknown branch: {branch}")
        tags = walk(branch)
        if tags is None:
            raise HTTPException(status_code=503, detail=f"Could not list the tags of {branch}")
        return {"branch": branch, "tags": tags, "auto": False}

    branches = get_branches()["branches"]
    names = [b["name"] for b in branches]
    default = next((b["name"] for b in branches if b.get("is_default")), names[0] if names else "")
    vname = (version_name or "").strip()
    if vname in names:
        tags = walk(vname)
        if tags is not None:
            return {"branch": vname, "tags": tags, "auto": True}
    default_tags = walk(default) if default else None
    if default_tags is None:
        raise HTTPException(status_code=503, detail=f"Could not list the tags of {default or 'the default branch'}")
    is_tag = is_safe_git_rev(vname) and bool(_git(["rev-parse", "--verify", "--quiet", f"refs/tags/{vname}"]))
    if not is_tag or vname in default_tags:
        return {"branch": default, "tags": default_tags, "auto": True}
    nums = _version_numbers(vname)
    for k in range(len(nums) - 1, 1, -1):
        token = re.compile(rf"(?<![\d.]){re.escape('.'.join(nums[:k]))}(?!\d)")
        candidates = [n for n in names if n != default and token.search(n)]
        if not candidates or len(candidates) > 3:
            continue  # nothing, or too broad a number (2.6 names every 2.6.x branch) to walk them all
        for name in candidates:
            tags = walk(name)
            if tags is not None and vname in tags:
                return {"branch": name, "tags": tags, "auto": True}
    return {"branch": default, "tags": default_tags, "auto": True}


# (tag_id) -> snippet code text; tags are immutable and content-addressed
_TAG_CODE_CACHE: OrderedDict[int, str] = OrderedDict()
_TAG_CODE_LOCK = threading.Lock()
_TAG_CODE_CACHE_LIMIT = 16384


def _slice_extent(lines: list[str], s_l: int, e_l: int, s_c: int, e_c: int) -> str:
    """Extract exact raw source string from line array using 1-based coordinates matching Extent.cc()."""
    if s_l <= 0 or e_l <= 0 or s_l > len(lines):
        return ""
    if s_l == e_l:
        line_str = lines[s_l - 1]
        line_len = len(line_str)
        s_idx = max(0, s_c - 1)
        e_idx = min(line_len, e_c - 1) if e_c > 0 else line_len
        return line_str[s_idx:e_idx]

    slices = []
    raw_len = len(lines)
    for l_num in range(s_l, min(e_l + 1, raw_len + 1)):
        line_str = lines[l_num - 1]
        line_len = len(line_str)
        if l_num == s_l:
            s_idx = max(0, s_c - 1)
            slices.append(line_str[s_idx:])
        elif l_num == e_l:
            e_idx = min(line_len, e_c - 1) if e_c > 0 else line_len
            slices.append(line_str[:e_idx])
        else:
            slices.append(line_str)

    return "\n".join(slices)


def tag_codes(cursor: Any, tag_ids: set[int]) -> dict[int, str]:
    """Code snippet of each tag reconstructed on-demand from Git blobs via m_bridge_tag coordinates."""
    codes: dict[int, str] = {}
    missing_ids: list[int] = []

    with _TAG_CODE_LOCK:
        for tid in tag_ids:
            if not tid:
                continue
            if tid in _TAG_CODE_CACHE:
                _TAG_CODE_CACHE.move_to_end(tid)
                codes[tid] = _TAG_CODE_CACHE[tid]
            else:
                missing_ids.append(tid)

    if not missing_ids:
        return codes

    missing_ids.sort()
    # tag_id -> (blob_hex, line_s, line_e, char_s, char_e)
    tag_spans: dict[int, tuple[str, int, int, int, int]] = {}
    for i in range(0, len(missing_ids), 1000):
        chunk = missing_ids[i:i + 1000]
        fmt = ",".join(["%s"] * len(chunk))
        cursor.execute(
            f"""
            SELECT bt.tag_id, fb.blob_hash, bt.line_s, bt.line_e, bt.char_s, bt.char_e
            FROM m_bridge_tag bt
            JOIN m_file_blob fb ON fb.blob_id = bt.blob_id
            WHERE bt.tag_id IN ({fmt});
            """,
            chunk,
        )
        for r in cursor.fetchall():
            tid = r["tag_id"]
            if tid not in tag_spans:
                tag_spans[tid] = (
                    hash_hex(r["blob_hash"]),
                    r["line_s"],
                    r["line_e"],
                    r["char_s"],
                    r["char_e"],
                )

    # Group tags by blob_hex to read each Git blob at most once
    blobs_to_tags: dict[str, list[tuple[int, int, int, int, int]]] = {}
    for tid, span in tag_spans.items():
        blobs_to_tags.setdefault(span[0], []).append((tid, span[1], span[2], span[3], span[4]))

    resolved: dict[int, str] = {}
    for blob_hex, spans in blobs_to_tags.items():
        if not blob_hex:
            continue
        obj_type, raw_bytes, _ = git_reader.read_object(blob_hex, "")
        if obj_type != "blob" or raw_bytes is None:
            continue
        # Split lines preserving exact structure
        lines = raw_bytes.decode("utf-8", errors="replace").splitlines()
        for tid, s_l, e_l, s_c, e_c in spans:
            resolved[tid] = _slice_extent(lines, s_l, e_l, s_c, e_c)

    # Populate missing entries with empty string if unresolvable
    for tid in missing_ids:
        c = resolved.get(tid, "")
        codes[tid] = c

    with _TAG_CODE_LOCK:
        for tid, c in resolved.items():
            _TAG_CODE_CACHE[tid] = c
            if len(_TAG_CODE_CACHE) > _TAG_CODE_CACHE_LIMIT:
                _TAG_CODE_CACHE.popitem(last=False)

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


# --- Kconfig definition facts (parser/kconfig_ast/kconfig_definitions.py; re-exported for importers) ------

def _kconfig_file_facts(text: str, sym: str) -> tuple[dict[str, Any], ...]:
    """Every definition of `sym` in one Kconfig file (see kconfig_definitions)."""
    return tuple(kconfig_definitions(text).get(sym, ()))


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


INGEST_DONE = 2  # core/ingest_state.INGEST_DONE
MAX_RENAMES = 5
INGEST_CHAIN_LIMIT = 200


def _ingest_chain(cursor: Any, network_id: int, from_sha: str, to_sha: str) -> list[int] | None:
    """commit_ids of the finished versions whose ingestion ranges lead from from_rev's commit to to_rev's
    (m_ingest_state.base_commit_id), to_rev's first; None when the bases do not reach from_rev (a version
    in between was never ingested, or an older database without base_commit_id)."""
    cursor.execute(
        "SELECT commit_id, commit_hash FROM m_commit WHERE network_id = %s AND commit_hash IN (%s, %s);",
        (network_id, bytes.fromhex(from_sha), bytes.fromhex(to_sha)),
    )
    ids = {hash_hex(r["commit_hash"]): int(r["commit_id"]) for r in cursor.fetchall()}
    if from_sha not in ids or to_sha not in ids:
        return None
    cursor.execute("SELECT commit_id, base_commit_id FROM m_ingest_state WHERE network_id = %s AND status = %s;",
                   (network_id, INGEST_DONE))
    base = {int(r["commit_id"]): int(r["base_commit_id"] or 0) for r in cursor.fetchall()}
    chain, cid = [], ids[to_sha]
    while cid != ids[from_sha]:
        if cid not in base or len(chain) >= INGEST_CHAIN_LIMIT:
            return None
        chain.append(cid)
        cid = base[cid]
    return chain or None


def _db_change_commits(cursor: Any, path: str, from_rev: str, to_rev: str, line_s: int, line_e: int,
                       limit: int) -> list[dict[str, Any]] | None:
    """The commits of from_rev..to_rev behind a change, from what the parser stored; None when the
    database cannot answer (then `git log` does).

    Both revisions must be release tags joined by release segments (m_commit_release, release_service.
    walk_segments()) and by finished ingestions (_ingest_chain()). With a line range, to_rev must have
    been ingested directly on from_rev (one range: m_bridge_commit_tag maps each commit's hunks to the
    tags of to_rev's blob) and the commits are those bridged to the blob's tags overlapping the lines;
    without one, the commits whose m_commit_file rows name the path (as old or new name). Merges have no
    file rows (parser/git_ast/GIT_API.md section 2.0), so they are never listed."""
    from webapp.backend.services import release_service as rs
    tags = rs.tag_commits()
    if from_rev not in tags or to_rev not in tags:
        return None
    segments = rs.walk_segments(from_rev, to_rev)
    if not segments:
        return None
    network_id = get_active_network_id(cursor)
    repo_id = get_active_repo_id(cursor)
    if network_id is None:
        return None
    chain = _ingest_chain(cursor, network_id, tags[from_rev], tags[to_rev])
    if chain is None:
        return None
    seg_sql = ",".join(["%s"] * len(segments))
    if line_s > 0 and line_e >= line_s:
        if len(chain) != 1:
            return None
        sha = batch_blob_shas([f"{to_rev}:{path}"])[0]
        meta = git_tree_resolver.map_blob_hashes(cursor, [sha]) if sha else {}
        if not sha or sha not in meta:
            return None
        blob_id = meta[sha][0]
        cursor.execute("SELECT tag_id FROM m_bridge_tag WHERE blob_id = %s AND line_s <= %s AND line_e >= %s;",
                       (blob_id, line_e, line_s))
        tag_ids = sorted({int(r["tag_id"]) for r in cursor.fetchall()})
        if not tag_ids:
            return []
        commit_ids: set[int] = set()
        for i in range(0, len(tag_ids), 500):
            part = tag_ids[i:i + 500]
            cursor.execute(
                f"SELECT DISTINCT commit_id FROM m_bridge_commit_tag WHERE blob_id = %s AND tag_id IN ({','.join(['%s'] * len(part))});",
                (blob_id, *part),
            )
            commit_ids.update(int(r["commit_id"]) for r in cursor.fetchall())
        if not commit_ids:
            return []
        ids = sorted(commit_ids)
        rows: list[dict[str, Any]] = []
        for i in range(0, len(ids), 1000):
            part = ids[i:i + 1000]
            cursor.execute(
                f"""
                SELECT c.commit_id, c.commit_hash, c.author_date, c.committer_date, c.subject, p.name, p.email
                FROM m_commit c
                JOIN m_commit_release cr ON cr.repo_id = %s AND cr.commit_hash = c.commit_hash AND cr.segment_id IN ({seg_sql})
                LEFT JOIN m_maintainer_person p ON p.person_id = c.author_id
                WHERE c.commit_id IN ({','.join(['%s'] * len(part))});
                """,
                (repo_id, *segments, *part),
            )
            rows.extend(cursor.fetchall())
    else:
        cursor.execute("SELECT fnid FROM m_file_name WHERE fname = %s LIMIT 1;", (path,))
        row = cursor.fetchone()
        if not row:
            return []
        fnid = int(row["fnid"])
        cursor.execute(
            f"""
            SELECT DISTINCT c.commit_id, c.commit_hash, c.author_date, c.committer_date, c.subject, p.name, p.email
            FROM m_commit_release cr
            JOIN m_commit c ON c.network_id = %s AND c.commit_hash = cr.commit_hash
            JOIN m_commit_file f ON f.commit_id = c.commit_id AND (f.fnid = %s OR f.old_fnid = %s)
            LEFT JOIN m_maintainer_person p ON p.person_id = c.author_id
            WHERE cr.repo_id = %s AND cr.segment_id IN ({seg_sql});
            """,
            (network_id, fnid, fnid, repo_id, *segments),
        )
        rows = cursor.fetchall()
    seen: set[str] = set()
    commits = []
    # Newest first like git log; same-second commits by commit_id (the parser registers parents first)
    for r in sorted(rows, key=lambda r: (int(r["committer_date"] or 0), int(r["commit_id"])), reverse=True):
        h = hash_hex(r["commit_hash"])
        if h in seen:
            continue
        seen.add(h)
        commits.append({
            "commit_hash": h,
            "commit_hash_short": h[:12],
            "author_name": safe_decode(r["name"]) or "",
            "author_email": safe_decode(r["email"]) or "",
            "author_date": int(r["author_date"] or 0),
            "subject": safe_decode(r["subject"]) or "",
        })
        if len(commits) >= limit:
            break
    return commits


class HistoryService:
    """Release-by-release history of a symbol definition (C, ASM, Rust...) or of a Kconfig symbol."""

    # --- Symbol definitions ---------------------------------------------------------------------------------

    def get_symbol_history(
        self,
        version_name: str,
        path: str,
        ast_id: int | None = None,
        line: int | None = None,
        name: str | None = None,
        include_rc: bool = False,
        arch: str = DEFAULT_DEFINITION_ARCH,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Trace the definition anchored at path:line across the indexed releases of one branch.

        The anchor is the narrowest definition covering `line` (preferring one named `name`, then
        `ast_id`); when `name` is not defined there (e.g. the cursor is on a call), the definition is
        located through the symbol index first, ranked for `arch`. A Kconfig anchor returns the Kconfig
        history instead. `branch` picks the branch the timeline follows (history_scope(): chosen for
        `version_name` when omitted).
        """
        norm_path = (path or "").strip().strip("/")
        scope = history_scope(version_name, branch)
        with get_db_cursor() as cursor:
            anchor = self._resolve_anchor(cursor, version_name, norm_path, line, ast_id, name, arch)
            if anchor["type_id"] in KCONFIG_TYPES:
                return self.get_kconfig_history(anchor["name"], version_name, include_rc, scope=scope)
            norm_path = anchor["path"]
            versions = indexed_versions(cursor, include_rc, scope)
            shas = batch_blob_shas([f"{v['vname']}:{norm_path}" for v in versions])
            blob_meta = git_tree_resolver.map_blob_hashes(cursor, [s for s in shas if s])
            paths, renamed_from = self._follow_renames(cursor, versions, norm_path, shas, blob_meta)
            blob_ids = sorted({blob_meta[s][0] for s in shas if s and s in blob_meta})
            lineage = tag_lineage(cursor, anchor["tag_id"])
            cands = self._candidates(cursor, blob_ids, anchor, lineage)
            codes = tag_codes(cursor, {c["tag_id"] for rows in cands.values() for c in rows} | {anchor["tag_id"]})
            cursor.execute("SELECT name FROM m_type_descriptor WHERE type_id = %s;", (anchor["type_id"],))
            type_row = cursor.fetchone()

        strip_comments = not norm_path.split("/")[-1].startswith("Kconfig")
        timeline: list[dict[str, Any]] = []
        prev: dict[str, Any] | None = None  # match of the previous version (None: absent)
        last_code: str | None = None
        ref_line = anchor["line_s"]
        prev_vname: str | None = None
        for i, (v, sha) in enumerate(zip(versions, shas)):
            v_path = paths[i]
            entry: dict[str, Any] = {
                **v, "prev_vname": prev_vname, "path": v_path, "tag_id": None, "line_s": None, "line_e": None,
                "lines_added": 0, "lines_removed": 0, "diff": [], "defs": [],
            }
            if i in renamed_from:
                entry["renamed_from"] = renamed_from[i]
            prev_vname = v["vname"]
            blob = blob_meta.get(sha) if sha else None
            if sha is not None and blob is None:
                entry["status"] = "unindexed"
                timeline.append(entry)
                continue
            match = self._pick(cands.get(blob[0], []) if blob else [], lineage, ref_line)
            if match is None and prev is not None and blob and anchor["name"] and self._mentions(v["vname"], v_path, anchor["name"]):
                # The file still names the symbol but its blob has no such tag: an indexing gap, not a removal
                entry["status"] = "unparsed"
                timeline.append(entry)
                continue
            if match is None:
                entry["status"] = "removed" if prev is not None else "absent"
                if prev is not None:
                    entry["defs"] = [{"path": v_path, "status": "removed", "tag_id": None,
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
            elif match["tag_id"] == prev["tag_id"] or code == last_code:
                # A new tag with byte-identical code (its hash covers more than the definition's text) is
                # no change: "cosmetic" with an empty diff misled (B14)
                entry["status"] = "unchanged"
            else:
                entry["status"] = _cosmetic_or_modified(last_code, code, strip_comments)
                entry.update(diff_stats(last_code, code))
            entry["defs"] = [{
                "path": v_path, "status": entry["status"], "tag_id": match["tag_id"],
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
            "branch": scope["branch"],
            "rc_count": scope["rc_count"],
            "total_versions": present,
            "change_count": changes,
            "snippets": {str(t): codes.get(t, "") for t in seen_tags},
            "timeline": timeline,
        }

    @staticmethod
    def _follow_renames(cursor: Any, versions: list[dict[str, Any]], path: str, shas: list[str | None],
                        blob_meta: dict[str, Any]) -> tuple[list[str], dict[int, str]]:
        """Paths of the timeline's versions, following renames: when the path has no blob at a version
        but had an indexed one at the previous, m_blob_transition (old blob -> new blob, written by the
        parser for each renamed file) and that version's checkpoint (m_checkpoint_tree) name the new path.
        With exactly one candidate the remaining versions are resolved on it (shas and blob_meta are
        updated in place); several or none leave the definition removed. -> (paths, {index: old path})."""
        paths = [path] * len(versions)
        renamed_from: dict[int, str] = {}
        start = 1
        for _ in range(MAX_RENAMES):
            k = next((i for i in range(start, len(versions))
                      if shas[i] is None and shas[i - 1] is not None and shas[i - 1] in blob_meta), None)
            if k is None:
                break
            start = k + 1
            old_blob = blob_meta[shas[k - 1]][0]
            try:
                cursor.execute("SELECT new_blob_id FROM m_blob_transition WHERE old_blob_id = %s;", (old_blob,))
                new_blobs = sorted({int(r["new_blob_id"]) for r in cursor.fetchall()})
                if not new_blobs:
                    continue
                cursor.execute(
                    f"""
                    SELECT DISTINCT f.fname FROM m_checkpoint_tree t JOIN m_file_name f ON f.fnid = t.fnid
                    WHERE t.commit_id = %s AND t.blob_id IN ({",".join(["%s"] * len(new_blobs))});
                    """,
                    (versions[k]["vid"], *new_blobs),
                )
                found = sorted({safe_decode(r["fname"]) for r in cursor.fetchall()} - {paths[k - 1]})
            except Exception as exc:  # noqa: BLE001 - the timeline stays on one path
                logger.debug("rename lookup failed: %s", exc)
                break
            if len(found) != 1:
                continue
            new_path = found[0]
            new_shas = batch_blob_shas([f"{v['vname']}:{new_path}" for v in versions[k:]])
            if new_shas[0] is None:
                continue
            shas[k:] = new_shas
            blob_meta.update(git_tree_resolver.map_blob_hashes(cursor, [s for s in new_shas if s and s not in blob_meta]))
            renamed_from[k] = paths[k - 1]
            paths[k:] = [new_path] * (len(versions) - k)
        return paths, renamed_from

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
        version_name: str,
        path: str,
        line: int | None,
        ast_id: int | None,
        name: str | None,
        arch: str = DEFAULT_DEFINITION_ARCH,
    ) -> dict[str, Any]:
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
            located = self._locate_definition(cursor, rev, name, {path}, arch)
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

    def _locate_definition(self, cursor: Any, rev: str, name: str, prefer_paths: set[str],
                           arch: str = DEFAULT_DEFINITION_ARCH) -> dict[str, Any] | None:
        blob_paths = git_tree_resolver.blob_paths(cursor, rev) or {}
        _, srcarch, bits = definition_arch(rev, arch)
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
        # Prefer the requested file, then the ARCH's definitions (definition_rank), then definitions with a
        # body over one-line declarations, then .c files
        def path_of(r: dict[str, Any]) -> str:
            paths = blob_paths[r["blob_id"]]
            return next((p for p in paths if p in prefer_paths), None) or min(
                paths, key=lambda p: definition_rank(p, r["linkage"], r["line_s"], srcarch, bits))

        def rank(r: dict[str, Any]) -> tuple:
            p = path_of(r)
            return (0 if p in prefer_paths else 1, *definition_rank(p, r["linkage"], r["line_s"], srcarch, bits)[:4],
                    -(r["line_e"] - r["line_s"]), 0 if p.endswith(".c") else 1, p)
        best = min(defs, key=rank)
        return self._anchor_row(cursor, best["tag_id"], path_of(best), best["line_s"])

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

    def get_kconfig_history(
        self,
        name: str,
        version_name: str | None = None,
        include_rc: bool = False,
        branch: str | None = None,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Every definition of CONFIG_<name> across the indexed releases of one branch, with property-level
        changes (`branch` / `scope` as in get_symbol_history)."""
        sym = (name or "").strip()
        if sym.startswith("CONFIG_"):
            sym = sym[7:]
        if not sym:
            raise HTTPException(status_code=400, detail="Kconfig symbol name is required.")
        if scope is None:
            scope = history_scope(version_name, branch)
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
            sha_to_blob = {hash_hex(r["blob_hash"]): r["blob_id"] for r in cursor.fetchall()}
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
            versions = indexed_versions(cursor, include_rc, scope)
            specs = [f"{v['vname']}:{p}" for v in versions for p in paths]
            shas = batch_blob_shas(specs)
            indexed = git_tree_resolver.map_blob_hashes(cursor, [s for s in shas if s and s not in sha_to_blob])
            codes = tag_codes(cursor, {r["tag_id"] for r in tag_rows})
        # The parser's stored definitions of every blob at once (m_blob_facts family 4); the rest parse from Git
        kconfig_facts.load_stored(s for s in shas if s)

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
            "branch": scope["branch"],
            "rc_count": scope["rc_count"],
            "total_versions": present,
            "change_count": changes,
            "snippets": {str(t): c for t, c in codes.items()},
            "timeline": timeline,
        }

    @staticmethod
    def _file_facts(vname: str, path: str, sha: str, sym: str) -> tuple[dict[str, Any], ...]:
        """Definitions of `sym` in the Kconfig blob `sha` (`vname:path`): one parse per blob for all its
        symbols, shared with Compare (kconfig_facts.definitions)."""
        def load() -> str:
            raw, _, _ = git_reader.read_file(vname, path)
            return raw.decode("utf-8", errors="replace") if raw is not None else ""

        return tuple(without_raw_fields(f) for f in kconfig_facts.definitions(sha, load).get(sym, ()))

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

        From the database when it holds the answer (_db_change_commits(): `source: "db"`); otherwise
        `git log -L` follows the range back through every commit of from_rev..to_rev, and without a line
        range (a removed definition) the commits touching the file are listed (`source: "git"`).
        """
        norm_path = (path or "").strip().strip("/")
        for rev in (to_rev, from_rev):
            if rev and not is_safe_git_rev(rev):
                raise HTTPException(status_code=400, detail=f"Invalid revision '{rev}'.")
        if not norm_path or norm_path.startswith("-") or ".." in norm_path.split("/"):
            raise HTTPException(status_code=400, detail="Invalid path.")
        limit = max(1, min(limit, 500))
        commits = None
        source = "git"
        if from_rev:
            try:
                with get_db_cursor() as cursor:
                    commits = _db_change_commits(cursor, norm_path, from_rev, to_rev, int(line_s or 0), int(line_e or 0), limit)
            except Exception as exc:  # noqa: BLE001 - git answers
                logger.debug("change commits of %s from the database failed: %s", norm_path, exc)
                commits = None
            if commits is not None:
                source = "db"
        if commits is None:
            try:
                commits = _cached_change_commits(
                    str(get_repo_dir()), norm_path, from_rev or "", to_rev, int(line_s or 0), int(line_e or 0), limit,
                )
            except subprocess.TimeoutExpired:
                raise HTTPException(status_code=504, detail="Git history operation timed out.")
        return {
            "source": source,
            "path": norm_path,
            "from": from_rev,
            "to": to_rev,
            "line_s": line_s,
            "line_e": line_e,
            "total": len(commits),
            "commits": list(commits),
        }


history_service = HistoryService()
