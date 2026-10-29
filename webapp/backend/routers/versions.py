"""webapp/backend/routers/versions.py - Release Versions, Projects, Branches & Catalog."""
from __future__ import annotations

import logging
import subprocess
from typing import Any

from fastapi import APIRouter, Query

from core.config import matches_branch_filter
from webapp.backend.config import (
    get_active_repo_name,
    get_active_repository,
    get_repo_dir,
    list_repositories,
    resolve_repo_path,
)
from webapp.backend.database.helpers import finished_ref_sql, get_active_repo_id, hash_hex, safe_decode
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.services.git_service import GitService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["versions"])
git_service = GitService()

PRERELEASE_MARKERS = ("-rc", "-pre", "-alpha", "-beta")


def _stamp(commit_hash: str, parsed_at: Any) -> str:
    """Client cache generation of a version: its commit and when its ingestion finished ("git" if never).
    Cached data of a version is dropped when its stamp changes (a branch moved, a tag got parsed)."""
    return f"{(commit_hash or '')[:12]}.{parsed_at if parsed_at is not None else 'git'}"


def _hex(val: Any) -> str:
    return hash_hex(val) if val else ""


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
    """List the configured repositories (config "repositories", auto-detected under workspace.repos_dir).

    `indexed` tells whether the parser has ingested the repository into the database; unindexed
    repositories are still browsable through their Git object store.
    """
    active = get_active_repo_name()
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
            "default_branch": repo.get("default_branch") or "master",
            "git_url": repo.get("git_url", ""),
            "indexed": db_row is not None,
            "available": resolve_repo_path(repo).exists(),
            "active": name == active,
        })
    return {"total": len(projects), "active": active, "projects": projects}


@router.get("/branches")
def get_branches() -> dict[str, Any]:
    """List git branches of the active repository (indexed refs first, then Git branches)."""
    branches = []
    seen = set()

    repo = get_active_repository()
    branch_filter = repo.get("branch_filter")
    default_branch = repo.get("default_branch") or "main"

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
                is_default = bname == default_branch or (not default_branch and bname in ("main", "master"))
                branches.append({
                    "name": bname,
                    "commit_id": row["commit_id"],
                    "commit_hash": commit_hash,
                    "is_default": is_default,
                    # The branch may have moved in Git since it was parsed
                    "stamp": _stamp(git_heads.get(bname) or commit_hash, row.get("parsed_at") or 0),
                })

    for bname, head in git_heads.items():
        if bname not in seen:
            seen.add(bname)
            is_default = bname == default_branch or (not default_branch and bname in ("main", "master"))
            branches.append({
                "name": bname,
                "commit_id": 0,
                "commit_hash": head,
                "is_default": is_default,
                "stamp": _stamp(head, None),
            })

    if not branches:
        fallback_name = default_branch or "main"
        branches.append({"name": fallback_name, "commit_id": 0, "commit_hash": "", "is_default": True})

    def branch_sort_key(b: dict[str, Any]) -> tuple[int, str]:
        if b.get("is_default"):
            return (0, b["name"])
        if b["name"] in ("main", "master"):
            return (1, b["name"])
        return (2, b["name"])

    branches.sort(key=branch_sort_key)
    return {"total": len(branches), "branches": branches}


@router.get("/versions")
def get_versions(include_rc: bool = Query(True)) -> dict[str, Any]:
    """List versions of the active repository: indexed refs from m_git_ref plus the repository's Git tags.

    Tags that were never parsed are returned with `"indexed": false` (vid 0); they remain browsable
    because file trees, files and diffs are resolved from Git. Pre-release tags are included only
    when `include_rc` is true; indexed refs are always included.
    """
    versions = []
    seen_vnames = set()

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
                commit_hash = _hex(r.get("commit_hash"))
                versions.append({
                    "vid": r["vid"],
                    "vname": vname,
                    "commit_hash": commit_hash,
                    "is_prerelease": any(m in vname for m in PRERELEASE_MARKERS),
                    "ref_type": r.get("ref_type"),
                    "indexed": True,
                    "stamp": _stamp(commit_hash, r.get("parsed_at") or 0),
                })

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
        is_pre = any(m in tname for m in PRERELEASE_MARKERS)
        if is_pre and not include_rc:
            continue
        seen_vnames.add(tname)
        versions.append({
            "vid": 0,
            "vname": tname,
            # Annotated tags list the peeled commit (%(*objectname)) first; lightweight tags only have %(objectname)
            "commit_hash": parts[2],
            "is_prerelease": is_pre,
            "ref_type": 1,
            "indexed": False,
            "stamp": _stamp(parts[2], None),
        })

    # Version order (git versionsort) for tags; other indexed refs (branches, commits) keep DB order at the end
    order = {line.split()[0]: i for i, line in enumerate(tag_lines)}
    versions.sort(key=lambda v: order.get(v["vname"], len(order)))
    return {"total": len(versions), "versions": versions}


@router.get("/version/{version_name}/timeline")
def get_version_timeline(
    version_name: str,
    limit: int = Query(100),
    direction: str = Query("past"),
    cursor_id: int | None = Query(None),
) -> dict[str, Any]:
    """Retrieve structured commit timeline starting from version anchor commit."""
    return git_service.get_commit_timeline(version_name, limit=limit, direction=direction, cursor_id=cursor_id)
