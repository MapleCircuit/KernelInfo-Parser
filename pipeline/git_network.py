"""Repository/network resolution for the checked-out project."""
from core.globalstuff import (
    G,
)
from core.config import (
    get_repository,
    LINUX_PROJECT,
    MANPAGES_PROJECT,
    MANPAGES_ROOT_COMMIT,
    is_kernel_project,
    project_display_name,
    repository_project,
)
import os
import subprocess as sp

from typing import Any
from collections.abc import Iterable
import contextlib
from core.DBLayout import (
    m_project,
    m_git_network,
    m_repository,
)
from pipeline.blob_reuse import _SQL_IN_CHUNK
from pipeline.state import MF, gp, logger




_network_repo_cache: dict[tuple[str, str, str, str], tuple[int, int]] = {}


# repo_dir (absolute) -> project slug of its repositories[] entry (configured `project`, else by root commit)
_repo_project_cache: dict[str, str] = {}


def repo_project_slug(repo_dir: str | None = None, roots: Iterable[str] | None = None) -> str:
    """Project slug of the git tree at `repo_dir`: the `project` of its repositories[] entry, else `man-pages`
    for a tree rooted at the man-pages root commit, else `linux`."""
    repo_dir = repo_dir or getattr(MF, "repo_dir", None) or "linux"
    key = os.path.abspath(repo_dir)
    if key in _repo_project_cache:
        return _repo_project_cache[key]
    entry = None
    with contextlib.suppress(Exception):
        entry = get_repository(key)
    if entry and str(entry.get("project") or "").strip():
        slug = repository_project(entry)
    elif roots is not None:
        slug = MANPAGES_PROJECT if MANPAGES_ROOT_COMMIT in set(roots) else LINUX_PROJECT
    else:
        return LINUX_PROJECT
    _repo_project_cache[key] = slug
    return slug


def kernel_steps_apply() -> bool:
    """False while the version being ingested belongs to a project known not to be a kernel tree (`man-pages`):
    the Linux-only steps (Kbuild rules, ARCH layouts, MAINTAINERS matching) are skipped for it."""
    return is_kernel_project(getattr(gp, "project_slug", None) or LINUX_PROJECT)


def _next_id(db: Any, table: Any) -> int:
    """Allocate a primary key id for `table`, using `reserve_id_block` in distributed mode."""
    if getattr(G.TE, "distributed", False) or getattr(G, "distributed", False):
        return db.reserve_id_block(table, 1)[0]
    return db.get_next_id(table)


def resolve_git_network_and_repo(
    repo_dir: str | None = None,
    repo_name: str | None = None,
    project_name: str | None = None,
    project_slug: str | None = None,
) -> tuple[int, int]:
    """Discover or register m_project, m_git_network, and m_repository for the given git tree.

    The project comes from the tree's repositories[] entry (`repo_project_slug()`) unless `project_slug` is given:
    `linux` ("Linux Kernel"), `man-pages` ("Linux man-pages"), or any other slug (named after itself); its network is
    `<slug>-network`. The slug is kept in `gp.project_slug` for the per-version steps (`kernel_steps_apply()`)."""
    repo_dir = repo_dir or getattr(MF, "repo_dir", None) or "linux"
    abs_dir = os.path.abspath(repo_dir)
    if project_slug is None and abs_dir in _repo_project_cache:
        project_slug = _repo_project_cache[abs_dir]
    if project_slug is not None:
        cache_key = (abs_dir, repo_name or "", project_name or project_display_name(project_slug), project_slug)
        if cache_key in _network_repo_cache:
            net_id, r_id = _network_repo_cache[cache_key]
            gp.network_id = net_id
            gp.repo_id = r_id
            gp.project_slug = project_slug
            return net_id, r_id

    git_dir = os.path.join(repo_dir, ".git") if os.path.isdir(os.path.join(repo_dir, ".git")) else repo_dir

    roots = []
    try:
        res = sp.run(
            ["git", f"--git-dir={git_dir}", "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        roots = [r.strip() for r in res.stdout.splitlines() if r.strip()]
    except Exception as e:
        logger.warning(f"Failed to query root commit for {repo_dir}: {e}")

    if roots:
        root_hex = sorted(roots)[0]
        root_bytes = bytes.fromhex(root_hex) if len(root_hex) == 40 else b"\x00" * 20
    else:
        root_bytes = b"\x00" * 20

    if project_slug is None:
        project_slug = repo_project_slug(repo_dir, roots)
    if project_name is None:
        project_name = project_display_name(project_slug)
    cache_key = (abs_dir, repo_name or "", project_name, project_slug)
    if cache_key in _network_repo_cache:
        net_id, r_id = _network_repo_cache[cache_key]
        gp.network_id = net_id
        gp.repo_id = r_id
        gp.project_slug = project_slug
        return net_id, r_id

    if not repo_name:
        repo_name = os.path.basename(os.path.abspath(repo_dir)) or "linux"

    with G.DB() as db:
        # 1. Resolve m_project
        proj = db.select(m_project, (None, None, project_slug))
        if proj:
            project_id = proj[0]
        else:
            project_id = _next_id(db, m_project)
            db.insert(m_project, (project_id, project_name, project_slug))

        # 2. Resolve m_git_network by root_commit_hash
        net = db.select(m_git_network, (None, None, None, root_bytes))
        if net:
            network_id = net[0]
        else:
            network_id = _next_id(db, m_git_network)
            net_name = f"{project_slug}-network"
            db.insert(m_git_network, (network_id, project_id, net_name, root_bytes))

        # 3. Resolve m_repository by (network_id, repo_name)
        repo_match = db.select(m_repository, (None, network_id, repo_name, None, None))
        if repo_match:
            repo_id = repo_match[0]
        else:
            repo_id = _next_id(db, m_repository)
            git_url = ""
            try:
                url_res = sp.run(
                    ["git", f"--git-dir={git_dir}", "remote", "get-url", "origin"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                git_url = url_res.stdout.strip()
            except Exception:
                pass

            def_branch = "master"
            try:
                branch_res = sp.run(
                    ["git", f"--git-dir={git_dir}", "symbolic-ref", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if branch_res.stdout.strip():
                    def_branch = branch_res.stdout.strip()
            except Exception:
                pass

            db.insert(m_repository, (repo_id, network_id, repo_name, git_url, def_branch))

    gp.network_id = network_id
    gp.repo_id = repo_id
    gp.project_slug = project_slug
    _network_repo_cache[cache_key] = (network_id, repo_id)
    return network_id, repo_id


def _lookup_commit_ids(db: Any, network_id: int, hashes: Iterable[str], strict: bool = False) -> dict[str, int]:
    """Map 40-hex commit hashes already registered in m_commit for `network_id` to their commit ids.

    A failed read returns what was found so far, or raises with `strict` (for a caller that inserts every
    hash it does not get back)."""
    wanted = [h for h in dict.fromkeys(hashes) if isinstance(h, str) and len(h) == 40]
    found: dict[str, int] = {}
    if not wanted:
        return found
    try:
        for i in range(0, len(wanted), _SQL_IN_CHUNK):
            chunk = wanted[i : i + _SQL_IN_CHUNK]
            rows = db.select_rows(
                "m_commit", ["commit_hash", "commit_id"],
                {"network_id": network_id, "commit_hash": [bytes.fromhex(h) for h in chunk]},
                as_dict=False,
            )
            for c_hash, c_id in rows:
                found[bytes(c_hash).hex()] = int(c_id)
    except Exception as exc:
        if strict:
            raise
        logger.debug(f"_lookup_commit_ids query failed: {exc}")
    return found
