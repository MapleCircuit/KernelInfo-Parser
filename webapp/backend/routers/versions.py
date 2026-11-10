"""webapp/backend/routers/versions.py - Release Versions, Projects, Branches & Catalog."""
from __future__ import annotations

import copy
import difflib
import json
import logging
import subprocess
import threading
import time
from collections import OrderedDict
from functools import cached_property
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from core.config import DEFAULT_TAG_VIEWS, detect_default_branch, matches_branch_filter
from webapp.backend.config import (
    get_active_repo_name,
    get_active_repository,
    get_repo_dir,
    list_repositories,
    reset_active_repository,
    resolve_repo_path,
    set_active_repository,
)
from webapp.backend.database.helpers import finished_ref_sql, get_active_repo_id, hash_hex, safe_decode
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.security.git import safe_rev
from webapp.backend.services.catalog_cache import JsonBody, TokenCache, client_etags, digest, dumps, respond

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["versions"])

PRERELEASE_MARKERS = ("-rc", "-pre", "-alpha", "-beta")

# Compact version catalog (GET /api/versions?compact=1, webapp/API.md section 4.1). The layout version
# is part of its ETag, so a client never applies a delta to a base of another layout.
COMPACT_FORMAT = "c1"
# Commit of a ref without a known commit in the fixed-width hash column (Git's null object id)
NULL_SHA12 = "0" * 12
# Generations of the compact catalog a delta can start from, per repository and include_rc: about two
# hours while the parser finishes a version every ~2 minutes; an older base gets the full catalog
_HISTORY_MAX = 64

# Tags reachable from a branch, keyed by (repository, branch head): what the header's Branch selector
# scopes the Tag/RC lists with (webapp/API.md section 5.13). Git computes it by walking the branch's
# whole history (~18 s for master of linux with its 1.5 M commits, a fraction of that for a stable
# branch or with a Git commit-graph written), so the answer is kept per head and only recomputed when
# the branch moves.
_BRANCH_TAGS_CACHE: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
_BRANCH_TAGS_CACHE_MAX = 32
# Guards the cache and the in-flight table. A walk being computed is entered in _BRANCH_TAGS_INFLIGHT,
# so a request arriving meanwhile (e.g. while the startup warm-up walks master) waits for that walk
# instead of starting a second one.
_BRANCH_TAGS_LOCK = threading.Lock()
_BRANCH_TAGS_INFLIGHT: dict[tuple[str, str], threading.Lock] = {}
# `git for-each-ref --merged` processes still running, terminated on shutdown (cancel_branch_walks)
_RUNNING_WALKS: set[subprocess.Popen] = set()
_BRANCH_WALK_TIMEOUT_S = 180


def _stamp(commit_hash: str, parsed_at: Any) -> str:
    """Client cache generation of a version: its commit and when its ingestion finished ("git" if never).
    Cached data of a version is dropped when its stamp changes (a branch moved, a tag got parsed)."""
    return f"{(commit_hash or '')[:12]}.{parsed_at if parsed_at is not None else 'git'}"


def _hex(val: Any) -> str:
    return hash_hex(val) if val else ""


def default_branch_of(repo: dict[str, Any]) -> str:
    """The repository's default branch: configured, else what its clone has checked out (as core.config
    detects it for auto-detected repositories), else "master"."""
    configured = (repo.get("default_branch") or "").strip()
    if configured:
        return configured
    try:
        detected = detect_default_branch(resolve_repo_path(repo))
    except Exception:  # noqa: BLE001 - a missing clone still lists its configured branches
        detected = ""
    return detected if detected and detected != "HEAD" else "master"


def _git_lines(args: list[str], timeout: float = 10.0) -> list[str]:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(get_repo_dir()),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


@router.get("/projects")
def get_projects() -> dict[str, Any]:
    """List the configured repositories, the request's repository marked `active`."""
    active = get_active_repo_name()
    projects = [{**p, "active": p["repo"] == active} for p in projects_catalog()]
    return {"total": len(projects), "active": active, "projects": projects}


def projects_catalog() -> list[dict[str, Any]]:
    """The configured repositories (config "repositories", auto-detected under workspace.repos_dir), the same for
    every request (`active` False): what GET /api/projects lists and the `projects` sync token digests.

    `indexed` tells whether the parser has ingested the repository into the database; unindexed
    repositories are still browsable through their Git object store.
    """
    indexed: dict[str, dict[str, Any]] = {}
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT r.repo_id, r.name, r.git_url, p.project_id, p.name AS project_name, p.slug
                FROM m_repository r
                LEFT JOIN m_git_network n ON n.network_id = r.network_id
                LEFT JOIN m_project p ON p.project_id = n.project_id
                WHERE r.repo_id > 0;
                """
            )
            for row in cursor.fetchall():
                indexed[safe_decode(row["name"])] = row
    except Exception as e:  # noqa: BLE001 - catalog still lists configured repositories without the DB
        logger.warning("Could not read m_repository: %s", e)

    projects = []
    for idx, repo in enumerate(list_repositories()):
        name = repo.get("name") or f"repo{idx}"
        db_row = indexed.get(name) or next((r for r in indexed.values() if repo.get("git_url") and safe_decode(r["git_url"]) == repo.get("git_url")), None)
        projects.append({
            "project_id": db_row["project_id"] if db_row and db_row.get("project_id") else idx + 1,
            "repo_id": db_row["repo_id"] if db_row else None,
            "name": safe_decode(db_row["project_name"]) if db_row and db_row.get("project_name") else name,
            "repo": name,
            "slug": repo.get("project") or name,
            "path": repo.get("path", ""),
            "default_branch": default_branch_of(repo),
            "git_url": repo.get("git_url", ""),
            "indexed": db_row is not None,
            "available": resolve_repo_path(repo).exists(),
            "active": False,
            "tag_views": repo.get("tag_views") or copy.deepcopy(DEFAULT_TAG_VIEWS),
        })
    return projects


_BRANCHES: TokenCache[JsonBody] = TokenCache()


def _branches_body() -> JsonBody:
    """The branch catalog of the active repository, built once per repository state (section 2.13)."""
    return _BRANCHES.get(get_active_repo_name(), lambda _previous: JsonBody(_build_branches()))


def get_branches() -> dict[str, Any]:
    """Branch catalog of the active repository (a copy of the cached one)."""
    return json.loads(_branches_body().raw)


@router.get("/branches")
def branches_endpoint(request: Request) -> Response:
    """List git branches of the active repository (indexed refs first, then Git branches); ETag / 304."""
    return respond(request, _branches_body())


def _build_branches() -> dict[str, Any]:
    """List git branches of the active repository (indexed refs first, then Git branches)."""
    branches = []
    seen = set()

    repo = get_active_repository()
    branch_filter = repo.get("branch_filter")
    default_branch = default_branch_of(repo)

    local_branches: set[str] = set()
    ref_entries: list[tuple[str, str, str]] = []  # (refname, short_name, obj)

    for line in _git_lines(
        ["for-each-ref", "--format=%(refname) %(refname:short) %(symref) %(objectname)", "refs/heads/", "refs/remotes/"],
        timeout=5,
    ):
        parts = line.strip().split()
        if not parts:
            continue
        refname = parts[0]
        short_name = parts[1] if len(parts) > 1 else refname
        symref = parts[2] if len(parts) > 2 and parts[2] != parts[-1] else ""
        obj = parts[-1] if len(parts) >= 2 else ""

        if symref or refname.endswith("/HEAD"):
            continue

        if refname.startswith("refs/heads/"):
            local_branches.add(refname[len("refs/heads/"):])
            local_branches.add(short_name)

        ref_entries.append((refname, short_name, obj))

    git_heads = {}
    for refname, short_name, obj in ref_entries:
        if refname.startswith("refs/remotes/"):
            rel = refname[len("refs/remotes/"):]
            remote_name, _, remote_branch = rel.partition("/")
            if remote_name == "origin" and remote_branch in local_branches:
                continue

        if short_name and matches_branch_filter(short_name, branch_filter):
            git_heads[short_name] = obj

    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT r.ref_name, r.commit_id, c.commit_hash, s.updated_at AS parsed_at
            FROM m_git_ref r
            LEFT JOIN m_commit c ON r.commit_id = c.commit_id
            LEFT JOIN m_ingest_state s ON s.commit_id = r.commit_id
            WHERE r.ref_type = 2 AND r.repo_id = %s AND {finished_ref_sql()}
            ORDER BY r.ref_name ASC;
            """,
            (get_active_repo_id(cursor),),
        )
        for row in cursor.fetchall():
            bname = safe_decode(row["ref_name"])
            if bname and bname.startswith("origin/") and bname[7:] in local_branches:
                continue
            if bname and bname not in seen and matches_branch_filter(bname, branch_filter):
                seen.add(bname)
                commit_hash = _hex(row.get("commit_hash"))
                branches.append({
                    "name": bname,
                    "commit_id": row["commit_id"],
                    "commit_hash": commit_hash,
                    "is_default": bname == default_branch,
                    # The branch may have moved in Git since it was parsed
                    "stamp": _stamp(git_heads.get(bname) or commit_hash, row.get("parsed_at") or 0),
                })

    for bname, head in git_heads.items():
        if bname not in seen:
            seen.add(bname)
            branches.append({
                "name": bname,
                "commit_id": 0,
                "commit_hash": head,
                "is_default": bname == default_branch,
                "stamp": _stamp(head, None),
            })

    if not branches:
        branches.append({"name": default_branch, "commit_id": 0, "commit_hash": "", "is_default": True, "stamp": ""})

    def branch_sort_key(b: dict[str, Any]) -> tuple[int, str]:
        if b.get("is_default"):
            return (0, b["name"])
        if b["name"] in ("main", "master"):
            return (1, b["name"])
        return (2, b["name"])

    branches.sort(key=branch_sort_key)
    tag_views = repo.get("tag_views") or copy.deepcopy(DEFAULT_TAG_VIEWS)
    return {"total": len(branches), "branches": branches, "tag_views": tag_views}


def _merged_tags(head: str) -> list[str] | None:
    """Tags reachable from `head` (one walk of its history); None when Git failed, timed out or was cancelled."""
    try:
        proc = subprocess.Popen(
            ["git", "for-each-ref", f"--merged={head}", "--format=%(refname:short)", "refs/tags"],
            cwd=str(get_repo_dir()),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    with _BRANCH_TAGS_LOCK:
        _RUNNING_WALKS.add(proc)
    try:
        out, _ = proc.communicate(timeout=_BRANCH_WALK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    finally:
        with _BRANCH_TAGS_LOCK:
            _RUNNING_WALKS.discard(proc)
    if proc.returncode != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def branch_tags(head: str) -> list[str] | None:
    """Tags reachable from commit `head` of the active repository, cached per (repository, head).

    Concurrent callers for the same head share one walk. A failed walk is not cached (None)."""
    key = (str(get_repo_dir()), head)
    with _BRANCH_TAGS_LOCK:
        tags = _BRANCH_TAGS_CACHE.get(key)
        if tags is not None:
            _BRANCH_TAGS_CACHE.move_to_end(key)
            return tags
        walk = _BRANCH_TAGS_INFLIGHT.setdefault(key, threading.Lock())
    with walk:
        with _BRANCH_TAGS_LOCK:
            tags = _BRANCH_TAGS_CACHE.get(key)
        if tags is not None:
            return tags  # computed by the walk this caller waited for
        try:
            tags = _merged_tags(head)
        finally:
            with _BRANCH_TAGS_LOCK:
                if tags is not None:
                    _BRANCH_TAGS_CACHE[key] = tags
                    while len(_BRANCH_TAGS_CACHE) > _BRANCH_TAGS_CACHE_MAX:
                        _BRANCH_TAGS_CACHE.popitem(last=False)
                _BRANCH_TAGS_INFLIGHT.pop(key, None)
    return tags


def cancel_branch_walks() -> None:
    """Terminate running branch walks (server shutdown: a --reload restart must not leave them behind)."""
    with _BRANCH_TAGS_LOCK:
        procs = list(_RUNNING_WALKS)
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass


def _branch_head(branch: str) -> str | None:
    head_lines = _git_lines(["rev-parse", "--verify", f"{branch}^{{commit}}"], timeout=5)
    return head_lines[0] if head_lines else None


@router.get("/branch/tags")
def get_branch_tags(branch: str = Query(..., min_length=1)) -> dict[str, Any]:
    """Tag names reachable from `branch`: the releases that belong to that branch's history.

    This is what makes the header's **Branch** selector scope the Tag and RC dropdowns (§5.13 of
    `webapp/API.md`): `v2.6.16.62` was tagged on `stable/linux-2.6.16.y` and is not reachable from
    `master`, so it is listed for that branch only. `head` is the branch's current commit, which is
    also the cache generation of the answer (a branch that moved returns a new head).
    """
    safe_rev(branch, "branch")
    head = _branch_head(branch)
    if not head:
        raise HTTPException(status_code=404, detail=f"Unknown branch: {branch}")
    tags = branch_tags(head)
    if tags is None:
        raise HTTPException(status_code=503, detail=f"Could not list the tags of {branch}")
    return {"branch": branch, "head": head, "total": len(tags), "tags": tags}


# Progress of the startup warm-up, reported by GET /api/system/ingest (webapp/API.md section 4.1)
PREWARM_STATE: dict[str, Any] = {"state": "idle", "started_at": None, "finished_at": None}


def prewarm_catalog() -> None:
    """Startup warm-up (webapp/API.md section 2.12): for every available repository, read the version and
    branch catalogs and walk the default branch's tags, so the first page load after a restart does not
    pay for them. Runs in a background thread; errors are logged and never stop the server."""
    PREWARM_STATE.update(state="running", started_at=time.time(), finished_at=None)
    try:
        _prewarm_repositories()
    finally:
        PREWARM_STATE.update(state="done", finished_at=time.time())


def _prewarm_repositories() -> None:
    for repo in list_repositories():
        name = repo.get("name")
        if not name or not resolve_repo_path(repo).exists():
            continue
        token = set_active_repository(name)
        if token is None:
            continue
        started = time.monotonic()
        try:
            get_versions(True)
            default = next((b for b in get_branches()["branches"] if b.get("is_default")), None)
            head = _branch_head(default["name"]) if default else None
            tags = branch_tags(head) if head else None
            logger.info(
                "Pre-warmed %s in %.1f s (default branch %s: %s tags)",
                name, time.monotonic() - started,
                default["name"] if default else "-", len(tags) if tags is not None else "no",
            )
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            logger.warning("Pre-warm of %s failed: %s", name, exc)
        finally:
            reset_active_repository(token)


def start_prewarm() -> threading.Thread:
    """Run prewarm_catalog() in a daemon thread (does not delay startup)."""
    thread = threading.Thread(target=prewarm_catalog, name="kip-prewarm", daemon=True)
    thread.start()
    return thread


def _is_prerelease(name: str) -> bool:
    return any(m in name for m in PRERELEASE_MARKERS)


def _version_rows(include_rc: bool) -> list[tuple[int, str, str, int, int | None]]:
    """Versions of the active repository as (vid, vname, commit_hash, ref_type, parsed), in catalog order.

    Indexed refs from m_git_ref (`parsed`: when their ingestion finished, 0 when it predates
    m_ingest_state) plus every Git tag that was not ingested (vid 0, `parsed` None); those remain
    browsable because file trees, files and diffs are resolved from Git. Pre-release tags only Git has
    are included only when `include_rc`; indexed refs are always included.
    """
    rows: list[tuple[int, str, str, int, int | None]] = []
    seen_vnames: set[str] = set()

    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                r.commit_id AS vid,
                r.ref_name AS vname,
                r.ref_type,
                c.commit_hash,
                c.author_date,
                s.updated_at AS parsed_at
            FROM m_git_ref r
            LEFT JOIN m_commit c ON r.commit_id = c.commit_id
            LEFT JOIN m_ingest_state s ON s.commit_id = r.commit_id
            WHERE r.commit_id > 0 AND r.repo_id = %s AND {finished_ref_sql()}
            ORDER BY c.author_date ASC, r.commit_id ASC;
            """,
            (get_active_repo_id(cursor),),
        )
        for r in cursor.fetchall():
            vname = safe_decode(r["vname"]) or ""
            if vname and vname not in seen_vnames:
                seen_vnames.add(vname)
                rows.append((r["vid"], vname, _hex(r.get("commit_hash")), r.get("ref_type"), r.get("parsed_at") or 0))

    # Git tags of the active repository that were not ingested, in version order
    tag_lines = _git_lines([
        "-c", "versionsort.suffix=-rc", "-c", "versionsort.suffix=-pre",
        "for-each-ref", "--sort=v:refname", "--format=%(refname:short) %(*objecttype)%(objecttype) %(*objectname) %(objectname)", "refs/tags",
    ])
    for line in tag_lines:
        parts = line.split()
        tname = parts[0]
        # The first type is the peeled one for annotated tags; only commit-pointing tags are versions
        if tname in seen_vnames or len(parts) < 3 or not parts[1].startswith("commit"):
            continue
        if not include_rc and _is_prerelease(tname):
            continue
        seen_vnames.add(tname)
        # Annotated tags list the peeled commit (%(*objectname)) first; lightweight tags only have %(objectname)
        rows.append((0, tname, parts[2], 1, None))

    # Version order (git versionsort) for tags; other indexed refs (branches, commits) keep DB order at the end
    order = {line.split()[0]: i for i, line in enumerate(tag_lines)}
    rows.sort(key=lambda row: order.get(row[1], len(order)))
    return rows


# Compact rows of recent catalog generations per (repository, include_rc): the bases deltas start from
_HISTORY: dict[tuple[str, bool], OrderedDict[str, list[tuple]]] = {}
_HISTORY_LOCK = threading.Lock()


def _remember_generation(key: tuple[str, bool], gen: str, compact: list[tuple]) -> None:
    with _HISTORY_LOCK:
        generations = _HISTORY.setdefault(key, OrderedDict())
        generations[gen] = compact
        generations.move_to_end(gen)
        while len(generations) > _HISTORY_MAX:
            generations.popitem(last=False)


def _generation(key: tuple[str, bool], gen: str) -> list[tuple] | None:
    with _HISTORY_LOCK:
        generations = _HISTORY.get(key)
        return generations.get(gen) if generations else None


def _wire_row(row: tuple[str, str, int | None, int]) -> list[Any]:
    """A compact row as sent: [vname, sha12, parsed, ref_type] without trailing defaults (parsed None, ref_type 1)."""
    vname, sha, parsed, ref_type = row
    if ref_type != 1:
        return [vname, sha, parsed, ref_type]
    if parsed is not None:
        return [vname, sha, parsed]
    return [vname, sha]


def _delta_ops(base: list[tuple], cur: list[tuple]) -> list[list[Any]]:
    """Operations rebuilding `cur` from `base`, in order: [start, count] copies `count` rows of `base` from
    `start`; any other entry (a list starting with a name) is a row of `cur`.

    Names are matched first (tags are added or removed rarely), then the rows of matching names compared:
    a version that finished parsing is one row between two copies."""
    ops: list[list[Any]] = []

    def copy(start: int, count: int) -> None:
        last = ops[-1] if ops else None
        if last is not None and isinstance(last[0], int) and last[0] + last[1] == start:
            last[1] += count
        else:
            ops.append([start, count])

    matcher = difflib.SequenceMatcher(None, [row[0] for row in base], [row[0] for row in cur], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            ops.extend(_wire_row(row) for row in cur[j1:j2])
            continue
        run = i1  # first row of the current run of unchanged rows
        for k in range(i2 - i1):
            if base[i1 + k] != cur[j1 + k]:
                if run < i1 + k:
                    copy(run, i1 + k - run)
                ops.append(_wire_row(cur[j1 + k]))
                run = i1 + k + 1
        if run < i2:
            copy(run, i2 - run)
    return ops


def _wire_size(body: JsonBody) -> int:
    return len(body.gz) if body.gz is not None else len(body.raw)


class VersionCatalog:
    """The version list of one repository state and its encodings (webapp/API.md sections 2.13 and 4.1).

    `rows` are (vid, vname, commit_hash, ref_type, parsed) in catalog order; `compact` the positional rows
    (vname, sha12, parsed, ref_type) the frontend reads, deriving `indexed` (parsed is not None),
    `is_prerelease` (from the name) and the stamp from them. `gen` is a digest of the whole content.
    """

    def __init__(self, key: tuple[str, bool], rows: list[tuple], gen: str, previous: VersionCatalog | None = None) -> None:
        self.key = key
        self.rows = rows
        self.gen = gen
        compact = [(vname, sha[:12] if len(sha) >= 12 else "", parsed, ref_type or 1) for _vid, vname, sha, ref_type, parsed in rows]
        if previous is not None:
            # Rows unchanged since the previous generation share its tuples: the history holds one copy of them
            same = {row: row for row in previous.compact}
            compact = [same.get(row, row) for row in compact]
        self.compact = compact
        self._deltas: dict[str, JsonBody | None] = {}

    @classmethod
    def build(cls, key: tuple[str, bool], previous: VersionCatalog | None = None) -> VersionCatalog:
        rows = _version_rows(key[1])
        gen = digest(dumps(rows))
        if previous is not None and previous.gen == gen:
            return previous  # same content (e.g. only a remote branch moved): keep its encodings
        catalog = cls(key, rows, gen, previous)
        _remember_generation(key, gen, catalog.compact)
        return catalog

    def legacy(self) -> dict[str, Any]:
        """The documented layout: {total, versions: [{vid, vname, commit_hash, is_prerelease, ref_type, indexed, stamp}]}."""
        versions = [
            {
                "vid": vid,
                "vname": vname,
                "commit_hash": sha,
                "is_prerelease": _is_prerelease(vname),
                "ref_type": ref_type,
                "indexed": parsed is not None,
                "stamp": _stamp(sha, parsed),
            }
            for vid, vname, sha, ref_type, parsed in self.rows
        ]
        return {"total": len(versions), "versions": versions}

    @cached_property
    def legacy_body(self) -> JsonBody:
        return JsonBody(self.legacy())

    @cached_property
    def compact_body(self) -> JsonBody:
        """Columns: names, commits as one string of 12-hex chunks, parse times (null: Git only) and the
        ref types other than 1 as [index, type] pairs."""
        return JsonBody({
            "g": self.gen,
            "n": [row[0] for row in self.compact],
            "h": "".join(row[1] or NULL_SHA12 for row in self.compact),
            "p": [row[2] for row in self.compact],
            "t": [[i, row[3]] for i, row in enumerate(self.compact) if row[3] != 1],
        }, etag=f"{COMPACT_FORMAT}-{self.gen}")

    def delta_from(self, base_gen: str) -> JsonBody | None:
        """The rows changed since generation `base_gen`; None when this process does not know that
        generation or the delta would not be smaller than the full catalog."""
        if base_gen in self._deltas:
            return self._deltas[base_gen]
        base = _generation(self.key, base_gen)
        if base is None:
            return None
        body: JsonBody | None = JsonBody({"g": self.gen, "b": base_gen, "d": _delta_ops(base, self.compact)})
        if _wire_size(body) >= _wire_size(self.compact_body):
            body = None
        self._deltas[base_gen] = body  # bounded: only generations of the history get here
        return body


_CATALOGS: TokenCache[VersionCatalog] = TokenCache()


def version_catalog(include_rc: bool = True) -> VersionCatalog:
    """The version catalog of the active repository, built once per repository state (section 2.13)."""
    key = (get_active_repo_name(), bool(include_rc))
    return _CATALOGS.get(key, lambda previous: VersionCatalog.build(key, previous))


def get_versions(include_rc: bool = True) -> dict[str, Any]:
    """Versions of the active repository in the documented entry layout (a fresh copy)."""
    return version_catalog(include_rc).legacy()


@router.get("/versions")
def versions_endpoint(request: Request, include_rc: bool = Query(True), compact: bool = Query(False)) -> Response:
    """List versions of the active repository: indexed refs from m_git_ref plus the repository's Git tags.

    `compact` selects the positional form the frontend reads. Its ETag names the catalog generation: a
    client sending the generation it has (If-None-Match) gets 304, or only the rows changed since then.
    """
    catalog = version_catalog(include_rc)
    if not compact:
        return respond(request, catalog.legacy_body)
    prefix = f"{COMPACT_FORMAT}-"
    for tag in client_etags(request):
        if tag.startswith(prefix) and tag[len(prefix):] != catalog.gen:
            delta = catalog.delta_from(tag[len(prefix):])
            if delta is not None:
                return respond(request, delta, cache_control="no-store", etag=False)
    return respond(request, catalog.compact_body)
