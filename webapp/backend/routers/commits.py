"""webapp/backend/routers/commits.py - Commit History & Git Inspection."""
from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from webapp.backend.config import require_feature
from webapp.backend.services import commit_relations, release_service
from webapp.backend.services.commit_facts import commit_ids, git_facts, release_badges
from webapp.backend.services.git_service import GitService
from webapp.backend.services.jobs import retry_after_ms

router = APIRouter(prefix="/api", tags=["commits"])
git_service = GitService()


@router.get("/commits/list", dependencies=[Depends(require_feature("commits"))])
def get_commits_unversioned(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    q: str = Query(""),
) -> dict[str, Any]:
    """Indexed commits of the repository matching `q`, newest first (versionless)."""
    return git_service.get_commits("", page, limit, q)


@router.get("/commits/{version_name}/list", dependencies=[Depends(require_feature("commits"))])
def get_commits(
    version_name: str,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    q: str = Query(""),
) -> dict[str, Any]:
    """Indexed commits of the repository matching `q`, newest first."""
    return git_service.get_commits(version_name, page, limit, q)


@router.get("/commits/detail/{commit_id_or_hash}", dependencies=[Depends(require_feature("commits"))])
def get_commit_unversioned(commit_id_or_hash: str) -> dict[str, Any]:
    """Commit details, files and multi-contributor trailers (versionless)."""
    return git_service.get_commit_detail(commit_id_or_hash)


@router.get("/commits/{version_name}/detail/{commit_id_or_hash}", dependencies=[Depends(require_feature("commits"))])
def get_commit(version_name: str, commit_id_or_hash: str) -> dict[str, Any]:
    """Commit details, files and multi-contributor trailers."""
    return git_service.get_commit_detail(commit_id_or_hash)


@router.post("/commits/resolve_mail/{commit_id_or_hash}", dependencies=[Depends(require_feature("commits", "mail"))])
def resolve_commit_mail_unversioned(commit_id_or_hash: str) -> dict[str, Any]:
    """On-demand resolution of a commit against the mailing list archive (versionless)."""
    return git_service.resolve_commit_mail(commit_id_or_hash)


@router.post("/commits/{version_name}/resolve_mail/{commit_id_or_hash}", dependencies=[Depends(require_feature("commits", "mail"))])
def resolve_commit_mail(version_name: str, commit_id_or_hash: str) -> dict[str, Any]:
    """On-demand resolution of a commit against the mailing list archive."""
    return git_service.resolve_commit_mail(commit_id_or_hash)


@router.get("/commits/diff/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_commit_diff_unversioned(commit_hash: str, context: int = Query(3, ge=0, le=20)) -> dict[str, Any]:
    """Structured diff of one commit against its first parent (versionless)."""
    return git_service.get_commit_diff(commit_hash, context)


@router.get("/commits/{version_name}/diff/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_commit_diff(version_name: str, commit_hash: str, context: int = Query(3, ge=0, le=20)) -> dict[str, Any]:
    """Structured diff of one commit against its first parent, with old/new line numbers per row."""
    return git_service.get_commit_diff(commit_hash, context)


@router.get("/commits/{version_name}/blame/{path:path}", dependencies=[Depends(require_feature("commits"))])
def get_blame(
    version_name: str,
    path: str,
    move: Annotated[int, Query(ge=0, le=1, description="1: follow lines moved within the file (-M)")] = 0,
    copy: Annotated[int, Query(ge=0, le=1, description="1: follow lines copied from other files of the commit (-C, slower)")] = 0,
    ws: Annotated[int, Query(ge=0, le=1, description="1: ignore whitespace changes (-w)")] = 0,
) -> dict[str, Any]:
    """Line-by-line `git blame` annotations."""
    return git_service.get_blame(version_name, path, move=bool(move), copy=bool(copy), ignore_ws=bool(ws))


@router.get("/commits/{version_name}/history/{path:path}", dependencies=[Depends(require_feature("commits"))])
def get_file_history(version_name: str, path: str, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """Commits that changed a file, newest first, and its last change."""
    return git_service.get_file_history(version_name, path, limit)


_TIMELINE_DIRECTIONS = {"past", "future", "around"}
_TIMELINE_GRANULARITIES = {"year", "month", "week", "day"}


def _validate_timeline_params(direction: str, granularity: str) -> str:
    norm_dir = direction.lower().strip()
    if norm_dir not in _TIMELINE_DIRECTIONS:
        raise HTTPException(status_code=400, detail="direction must be 'past', 'future', or 'around'.")
    if granularity.lower().strip() not in _TIMELINE_GRANULARITIES:
        raise HTTPException(status_code=400, detail="granularity must be one of: year, month, week, day.")
    return norm_dir


@router.get("/commits/timeline", dependencies=[Depends(require_feature("commits"))])
def get_commit_timeline_unversioned(
    limit: int = Query(100, ge=1, le=250),
    direction: Annotated[str, Query()] = "past",
    cursor_id: int | None = Query(None),
    granularity: Annotated[str, Query()] = "month",
) -> dict[str, Any]:
    """Commit timeline from the latest commits in the repository (versionless)."""
    norm_dir = _validate_timeline_params(direction, granularity)
    return git_service.get_commit_timeline("", limit=limit, direction=norm_dir, cursor_id=cursor_id)


@router.get("/commits/{version_name}/timeline", dependencies=[Depends(require_feature("commits"))])
def get_commit_timeline(
    version_name: str,
    limit: int = Query(100, ge=1, le=250),
    direction: Annotated[str, Query()] = "past",
    cursor_id: int | None = Query(None),
    granularity: Annotated[str, Query()] = "month",
) -> dict[str, Any]:
    """Commit timeline around the version's commit, with contributor summaries."""
    norm_dir = _validate_timeline_params(direction, granularity)
    return git_service.get_commit_timeline(version_name, limit=limit, direction=norm_dir, cursor_id=cursor_id)


def _search_commits(version_name: str, q: str, limit: int) -> dict[str, Any]:
    if len(q.strip()) < 2:
        return {"version": version_name, "page": 1, "limit": limit, "total": 0, "commits": []}
    return git_service.get_commits(version_name, page=1, limit=limit, query=q)


@router.get("/commits/search", dependencies=[Depends(require_feature("commits"))])
def search_commits_unversioned(
    q: Annotated[str, Query(max_length=256)],
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Commit search by hash prefix, subject or author (versionless)."""
    return _search_commits("", q, limit)


@router.get("/commits/{version_name}/search", dependencies=[Depends(require_feature("commits"))])
def search_commits(
    version_name: str,
    q: Annotated[str, Query(max_length=256)],
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Commit search by hash prefix, subject or author (at least two characters)."""
    return _search_commits(version_name, q, limit)


@router.post("/version/{version_name}/patch/series", dependencies=[Depends(require_feature("patch"))])
def format_patch_series(version_name: str, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """A multi-file patch series in git format-patch form (P3): cover letter, [PATCH n/N], To/Cc,
    base-commit, plus whether it applies cleanly to its base (checked in memory)."""
    from webapp.backend.services.patch_series import format_series
    return format_series(version_name, payload)


@router.post("/version/{version_name}/patch/format", dependencies=[Depends(require_feature("patch"))])
def format_patch(version_name: str, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """RFC-2822 email patch from in-browser edits."""
    return git_service.format_patch(version_name, payload)


# ------------------------------------------------------------------ release containment (H3) and relations (H1)
_HASH = re.compile(r"^[0-9a-fA-F]{4,64}$")
MAX_HASHES = 200
NO_STORE = {"Cache-Control": "no-store"}


def _hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH.match(value):
        raise HTTPException(status_code=400, detail="Commit hash must be 4-64 hexadecimal characters.")
    return value.lower()


def _hashes(payload: dict[str, Any]) -> list[str]:
    raw = (payload or {}).get("hashes")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="Body must be {\"hashes\": [...]} with 1-200 commit hashes.")
    if len(raw) > MAX_HASHES:
        raise HTTPException(status_code=400, detail=f"At most {MAX_HASHES} hashes.")
    return list(dict.fromkeys(_hash(h) for h in raw))


def _building(state: dict[str, Any], what: str) -> JSONResponse:
    body = {"state": "building", "index": what, "progress": state.get("progress") or {},
            "retry_after_ms": retry_after_ms(state)}
    return JSONResponse(body, status_code=202, headers=NO_STORE)


def _released_one(commit_hash: str) -> Any:
    info = release_service.first_containing(_hash(commit_hash))
    if not info.get("indexed") and not info.get("released"):
        return _building(release_service.ensure_release_index(), "release")
    body = {"commit": info["sha"], **{k: v for k, v in info.items() if k != "sha"}}
    # Not released yet: valid until tags are fetched (the client asks again after the next sync)
    return body if info.get("released") else JSONResponse(body, headers=NO_STORE)


@router.get("/commits/released/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_released_unversioned(commit_hash: str) -> Any:
    """First tag and release containing a commit (versionless)."""
    return _released_one(commit_hash)


@router.get("/commits/{version_name}/released/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_released(version_name: str, commit_hash: str) -> Any:
    """First tag and release containing a commit (H3): {commit, first_tag, first_release, first_stable,
    released} or {released: false, branches}. 202 while the release index is first built."""
    return _released_one(commit_hash)


def _released_batch(payload: dict[str, Any]) -> Any:
    hashes = _hashes(payload)
    state = release_service.ensure_release_index()
    if state.get("state") != "done":
        return _building(state, "release")
    results = {}
    for h in hashes:
        try:
            info = release_service.first_containing(h)
        except HTTPException:
            continue
        results[h] = {k: info.get(k) for k in ("sha", "first_tag", "first_release", "first_stable", "released")}
    return JSONResponse({"results": results, "index": {"state": "done"}}, headers=NO_STORE)


@router.post("/commits/released", dependencies=[Depends(require_feature("commits"))])
def post_released_unversioned(payload: Annotated[dict[str, Any], Body()]) -> Any:
    """Batched first releases (versionless)."""
    return _released_batch(payload)


@router.post("/commits/{version_name}/released", dependencies=[Depends(require_feature("commits"))])
def post_released(version_name: str, payload: Annotated[dict[str, Any], Body()]) -> Any:
    """Batched first releases (timeline cards, popovers): {results: {hash: {...}}, index: {state}}."""
    return _released_batch(payload)


def _enrich(relations: dict[str, Any]) -> dict[str, Any]:
    fields = ("fixes", "fixed_by", "reverts", "reverted_by", "upstream", "backports")
    shas = [it["sha"] for f in fields for it in relations.get(f, []) if it.get("sha")]
    facts = git_facts(shas)
    ids = commit_ids(shas)
    badges = release_badges(shas)
    for f in fields:
        for it in relations.get(f, []):
            sha = it.get("sha")
            if not sha:
                it["released"] = None
                continue
            fact = facts.get(sha) or {}
            it.setdefault("subject", fact.get("subject", ""))
            if not it.get("subject"):
                it["subject"] = fact.get("subject", "")
            it["author"] = fact.get("author", "")
            it["email"] = fact.get("email", "")
            it["date"] = it.get("date") or fact.get("date", 0)
            it["released"] = badges.get(sha)
            if sha in ids:
                it["commit_id"] = ids[sha]
    return relations


def _relations_one(commit_hash: str) -> Any:
    rel = commit_relations.relations(_hash(commit_hash))
    if rel.get("state") != "done":
        return _building(rel, "relations")
    body = _enrich(rel)
    body["commit"] = body.pop("sha")
    body["index"] = commit_relations.status()
    own = release_badges([body["commit"]]).get(body["commit"])
    body["released"] = own
    # CVEs this commit fixes or introduced, and those its upstream commit fixes (H4; empty without cve.repo_dir)
    try:
        from webapp.backend.services.cve_service import for_commit
        upstream = [u.get("sha") for u in body.get("upstream") or [] if u.get("sha")]
        body["cves"] = for_commit(body["commit"], upstream)
    except Exception:  # noqa: BLE001 - optional
        body["cves"] = {"fixes": [], "introduced": [], "fixes_upstream": []}
    body.pop("state", None)
    return JSONResponse(body, headers=NO_STORE) if body.get("refreshing") else body


@router.get("/commits/relations/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_relations_unversioned(commit_hash: str) -> Any:
    """Commit relations without a version path segment."""
    return _relations_one(commit_hash)


@router.get("/commits/{version_name}/relations/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_relations(version_name: str, commit_hash: str) -> Any:
    """What a commit fixes and what fixed it, reverts, backports, Cc: stable and mail links (H1, H2):
    m_commit_relation for ingested commits, else the commit relation index (webapp/API.md section 2.16),
    enriched with subjects, authors and first releases. 202 while the index is first built; no-store
    while it refreshes."""
    return _relations_one(commit_hash)


def _relations_batch(payload: dict[str, Any]) -> Any:
    hashes = _hashes(payload)
    full_of = {h: (h if len(h) == 40 else release_service.resolve_sha(h)) for h in hashes}
    found, missing = commit_relations.relation_counts([f for f in full_of.values() if f])
    results = {h: found[f] for h, f in full_of.items() if f in found}
    if missing:
        state = commit_relations.ensure_index()
        if state.get("state") != "done":
            if not results:
                return _building(state, "relations")
        else:
            for h, full in full_of.items():
                if full in missing:
                    r = commit_relations.relation_index.relations(full)
                    results[h] = {k: len(r[k]) for k in ("fixes", "fixed_by", "reverted_by", "backports")}
                    results[h]["stable_cc"] = bool(r["stable_cc"])
    return JSONResponse({"results": results}, headers=NO_STORE)


@router.post("/commits/relations", dependencies=[Depends(require_feature("commits"))])
def post_relations_unversioned(payload: Annotated[dict[str, Any], Body()]) -> Any:
    """Relation counts of many commits (versionless)."""
    return _relations_batch(payload)


@router.post("/commits/{version_name}/relations", dependencies=[Depends(require_feature("commits"))])
def post_relations(version_name: str, payload: Annotated[dict[str, Any], Body()]) -> Any:
    """Relation counts of many commits (timeline chips): {results: {hash: {fixed_by, reverted_by,
    backports, fixes, stable_cc}}}. Ingested commits are counted in the database; the others in the
    relation index (202 while it is first built and no commit could be answered)."""
    return _relations_batch(payload)


@router.get("/commits/query", dependencies=[Depends(require_feature("commits"))])
def query_commits_unversioned(
    path: Annotated[list[str] | None, Query(description="Touched path or glob (repeatable, at most 8): fs/ext4, *.h, !drivers/")] = None,
    author: Annotated[str, Query(description="Author name or email (text)")] = "",
    committer: Annotated[str, Query(description="Committer name or email (text)")] = "",
    role: Annotated[list[str] | None, Query(description="Trailer filter role:value (reviewer, acked, tested, reported, suggested, signed, codev, cc, fixes, link)")] = None,
    since: Annotated[str, Query(description="YYYY-MM-DD")] = "",
    until: Annotated[str, Query(description="YYYY-MM-DD")] = "",
    range: Annotated[str, Query(description="<rev>..<rev>")] = "",
    release: Annotated[str, Query(description="A release tag: its cycle (previous release..tag)")] = "",
    merges: Annotated[str, Query(description="only | none | all")] = "",
    section: Annotated[str, Query(description="MAINTAINERS section id or name: its F:/X: patterns")] = "",
    text: Annotated[str, Query(description="Subject or message text")] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Structured commit query run with git log defaulting to the latest branch (versionless)."""
    from webapp.backend.services.commit_query import query_commits as run_query
    return run_query("", paths=path, author=author, committer=committer, roles=role, since=since,
                     until=until, range_=range, release=release, merges=merges, section=section, text=text,
                     page=page, limit=limit)


@router.get("/commits/{version_name}/query", dependencies=[Depends(require_feature("commits"))])
def query_commits(
    version_name: str,
    path: Annotated[list[str] | None, Query(description="Touched path or glob (repeatable, at most 8): fs/ext4, *.h, !drivers/")] = None,
    author: Annotated[str, Query(description="Author name or email (text)")] = "",
    committer: Annotated[str, Query(description="Committer name or email (text)")] = "",
    role: Annotated[list[str] | None, Query(description="Trailer filter role:value (reviewer, acked, tested, reported, suggested, signed, codev, cc, fixes, link)")] = None,
    since: Annotated[str, Query(description="YYYY-MM-DD")] = "",
    until: Annotated[str, Query(description="YYYY-MM-DD")] = "",
    range: Annotated[str, Query(description="<rev>..<rev>")] = "",
    release: Annotated[str, Query(description="A release tag: its cycle (previous release..tag)")] = "",
    merges: Annotated[str, Query(description="only | none | all")] = "",
    section: Annotated[str, Query(description="MAINTAINERS section id or name: its F:/X: patterns")] = "",
    text: Annotated[str, Query(description="Subject or message text")] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Structured commit query run with git log (H8): paths, author/committer, trailer roles, dates, a
    range or a release cycle, merge filter, MAINTAINERS section and free text, newest first."""
    from webapp.backend.services.commit_query import query_commits as run_query
    return run_query(version_name, paths=path, author=author, committer=committer, roles=role, since=since,
                     until=until, range_=range, release=release, merges=merges, section=section, text=text,
                     page=page, limit=limit)
