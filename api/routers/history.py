"""api/routers/history.py - Commits, blame, file history, symbol history, and line-range history endpoints."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.config import (
    get_active_repo_name,
    paginate_items,
    require_feature,
    resolve_version,
)
from api.routers.common import (
    DEFAULT_DEFINITION_ARCH,
    _MAX_SEARCH_OFFSET,
    _git_service,
    _validate_identifier,
    _validate_rel_path,
    _validate_search_offset,
    _with_repo,
)
from webapp.backend.security.git import safe_rev, validate_pathspecs
from webapp.backend.services.history_service import history_service

router = APIRouter()


def _v1():
    from api.routers import v1

    return v1


@router.get("/commits", dependencies=[Depends(require_feature("commits"))])
def query_commits(
    q: Annotated[str, Query(max_length=256)] = "",
    version: Annotated[str | None, Query(max_length=256)] = None,
    path: Annotated[list[str] | None, Query()] = None,
    author: Annotated[str, Query(max_length=128)] = "",
    committer: Annotated[str, Query(max_length=128)] = "",
    role: Annotated[list[str] | None, Query()] = None,
    since: Annotated[str, Query(max_length=32)] = "",
    until: Annotated[str, Query(max_length=32)] = "",
    range: Annotated[str, Query(max_length=256)] = "",
    release: Annotated[str, Query(max_length=128)] = "",
    merges: Annotated[str, Query(max_length=16)] = "",
    section: Annotated[str, Query(max_length=128)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Commit search and structured git log query."""
    ver = resolve_version(version) if version else ""
    structured = any((path, author, committer, role, since, until, range, release, merges, section))
    if structured:
        validate_pathspecs(path)
        page_num = (offset // limit) + 1
        rel_off = offset - (page_num - 1) * limit
        run_fn = _v1().run_commit_query
        try:
            res = run_fn(
                ver,
                paths=path,
                author=author,
                committer=committer,
                roles=role,
                since=since,
                until=until,
                range_=range,
                release=release,
                merges=merges,
                section=section,
                text=q,
                page=page_num,
                limit=limit,
                offset=offset,
            )
            used_exact_offset = True
        except TypeError:
            res = run_fn(
                ver,
                paths=path,
                author=author,
                committer=committer,
                roles=role,
                since=since,
                until=until,
                range_=range,
                release=release,
                merges=merges,
                section=section,
                text=q,
                page=page_num,
                limit=limit,
            )
            used_exact_offset = False
        commits = list(res.get("commits") or [])
        has_more = bool(res.get("has_more"))
        if not used_exact_offset and rel_off > 0:
            if len(commits) > offset:
                page = commits[offset : offset + limit]
                truncated = has_more or (offset + len(page) < len(commits))
            else:
                if has_more and len(commits) >= rel_off:
                    res2 = run_fn(
                        ver,
                        paths=path,
                        author=author,
                        committer=committer,
                        roles=role,
                        since=since,
                        until=until,
                        range_=range,
                        release=release,
                        merges=merges,
                        section=section,
                        text=q,
                        page=page_num + 1,
                        limit=limit,
                    )
                    c2 = list(res2.get("commits") or [])
                    combined = commits + c2
                    page = combined[rel_off : rel_off + limit]
                    truncated = bool(res2.get("has_more")) or (rel_off + len(page) < len(combined))
                else:
                    page = commits[rel_off : rel_off + limit]
                    truncated = False
        else:
            if rel_off > 0 and len(commits) > limit:
                page = commits[offset : offset + limit]
                truncated = has_more or (offset + len(page) < len(commits))
            else:
                page = commits[:limit]
                truncated = has_more or len(commits) > limit
        total = int(res["total"]) if res.get("total") is not None else (offset + len(page) if page else 0)
        return {
            "repo": get_active_repo_name(),
            "version": res.get("version", ver),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "commits": page,
        }

    if offset + limit + 1 <= 500:
        res = _git_service.get_commits(ver, page=1, limit=offset + limit + 1, query=q)
        commits = res.get("commits") or []
        page, total, truncated = paginate_items(commits, limit, offset)
        return {
            "repo": get_active_repo_name(),
            "version": res.get("version", ver),
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "commits": page,
        }

    step = 250
    page_num = (offset // step) + 1
    base = (page_num - 1) * step
    rel_off = offset - base
    res = _git_service.get_commits(ver, page=page_num, limit=step, query=q)
    c1 = list(res.get("commits") or [])
    if rel_off + limit + 1 > step and len(c1) >= step:
        res2 = _git_service.get_commits(ver, page=page_num + 1, limit=step, query=q)
        c2 = list(res2.get("commits") or [])
        combined = c1 + c2
    else:
        combined = c1
    page = combined[rel_off : rel_off + limit]
    truncated = len(combined) > (rel_off + len(page))
    total = int(res["total"]) if res.get("total") not in (None, len(c1)) else (offset + len(page) if page else 0)
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "commits": page,
    }


@router.get("/commits/{commit_hash}", dependencies=[Depends(require_feature("commits"))])
def get_commit(commit_hash: str) -> dict[str, Any]:
    """Commit metadata, trailers, and touched files."""
    safe_rev(commit_hash, "commit")
    return {"repo": get_active_repo_name(), **_git_service.get_commit_detail(commit_hash)}


@router.get("/commits/{commit_hash}/diff", dependencies=[Depends(require_feature("commits"))])
def get_commit_diff(
    commit_hash: str,
    context: Annotated[int, Query(ge=0, le=20)] = 3,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Bounded structured diff of a commit against its first parent."""
    safe_rev(commit_hash, "commit")
    res = _git_service.get_commit_diff(commit_hash, context=context)
    files = res.get("files") or []
    page, total, truncated = paginate_items(
        files, limit, offset, already_truncated=bool(res.get("truncated"))
    )
    return {
        "repo": get_active_repo_name(),
        "commit_hash": res.get("commit_hash"),
        "parent_hash": res.get("parent_hash"),
        "is_merge": res.get("is_merge"),
        "context": res.get("context", context),
        "additions": res.get("additions", 0),
        "deletions": res.get("deletions", 0),
        "total_files": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "files": page,
    }


@router.get("/commits/{commit_hash}/relations", dependencies=[Depends(require_feature("commits"))])
def get_commit_relations(commit_hash: str) -> Any:
    """Fixes, fixed-by, reverts, reverted-by, upstream/backports, stable Cc, mail links, and CVEs of a commit."""
    safe_rev(commit_hash, "commit")
    return _with_repo(_v1()._relations_one(commit_hash))


@router.get("/commits/{commit_hash}/released", dependencies=[Depends(require_feature("commits"))])
def get_commit_released(commit_hash: str) -> Any:
    """First tag, mainline release, and stable release containing a commit."""
    safe_rev(commit_hash, "commit")
    return _with_repo(_v1()._released_one(commit_hash))


@router.get("/blame", dependencies=[Depends(require_feature("commits"))])
def get_file_blame(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    move: bool = False,
    ignore_ws: bool = False,
) -> dict[str, Any]:
    """Bounded git blame slice for a file at a revision."""
    if end_line is not None and end_line < start_line:
        raise HTTPException(status_code=400, detail="end_line must be >= start_line.")
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    res = _git_service.get_blame(ver, clean_path, move=move, copy=False, ignore_ws=ignore_ws)
    all_lines = res.get("lines") or []
    sliced_range = [
        ln
        for ln in all_lines
        if ln.get("line", 0) >= start_line and (end_line is None or ln.get("line", 0) <= end_line)
    ]
    page, total, truncated = paginate_items(sliced_range, limit, offset)
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": res.get("path", clean_path),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "lines": page,
    }


@router.get("/history/file", dependencies=[Depends(require_feature("commits"))])
def get_file_history(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    version: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Chronological commit history of a file up to `version`."""
    _validate_search_offset(offset, _MAX_SEARCH_OFFSET)
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    fetch_cap = min(_MAX_SEARCH_OFFSET + 1, offset + limit + 1)
    res = _git_service.get_file_history(ver, clean_path, limit=fetch_cap)
    commits = res.get("commits") or []
    page, total, truncated = paginate_items(
        commits, limit, offset, already_truncated=(len(commits) >= fetch_cap)
    )
    return {
        "repo": get_active_repo_name(),
        "version": res.get("version", ver),
        "path": res.get("path", clean_path),
        "last_changed": res.get("last_changed"),
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "commits": page,
    }


@router.get("/history/symbol", dependencies=[Depends(require_feature("commits"))])
def get_symbol_history(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    name: Annotated[str | None, Query(max_length=256)] = None,
    line: Annotated[int | None, Query(ge=1)] = None,
    ast_id: Annotated[int | None, Query(ge=1)] = None,
    include_rc: bool = False,
    branch: Annotated[str | None, Query(max_length=128)] = None,
    version: Annotated[str | None, Query(max_length=256)] = None,
    arch: Annotated[str, Query(max_length=64)] = DEFAULT_DEFINITION_ARCH,
) -> dict[str, Any]:
    """Release-by-release history of the definition at `path:line` (or anchored on symbol `name`)."""
    ver = resolve_version(version)
    clean_path = _validate_rel_path(path, allow_empty=False)
    ident = _validate_identifier(name, "symbol") if name else None
    clean_branch = safe_rev(branch, "branch") if branch else None
    res = history_service.get_symbol_history(
        ver,
        clean_path,
        ast_id=ast_id,
        line=line,
        name=ident,
        include_rc=include_rc,
        arch=arch,
        branch=clean_branch,
    )
    return {"repo": get_active_repo_name(), **res}


@router.get("/history/lines", dependencies=[Depends(require_feature("commits"))])
def get_line_range_commits(
    path: Annotated[str, Query(min_length=1, max_length=512)],
    to: Annotated[str | None, Query(max_length=256, description="Target Git revision (tag, branch, or commit SHA).")] = None,
    from_rev: Annotated[str | None, Query(alias="from", max_length=256, description="Base Git revision (tag, branch, or commit SHA).")] = None,
    line_s: Annotated[int | None, Query(ge=1)] = None,
    line_e: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Commits in `from..to` that touched lines `line_s..line_e` of `path` (`git log -L`)."""
    if line_s is not None and line_e is not None and line_e < line_s:
        raise HTTPException(status_code=400, detail="line_e must be >= line_s.")
    clean_path = _validate_rel_path(path, allow_empty=False)
    to_ver = resolve_version(to)
    from_ver = safe_rev(from_rev, "from") if from_rev else None
    fetch_cap = min(500, offset + limit)
    res = history_service.get_change_commits(clean_path, to_ver, from_ver, line_s, line_e, fetch_cap)
    commits = res.get("commits") or []
    page, total, truncated = paginate_items(
        commits, limit, offset, already_truncated=bool(res.get("truncated") or len(commits) >= fetch_cap)
    )
    return {
        "repo": get_active_repo_name(),
        **res,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "commits": page,
    }
