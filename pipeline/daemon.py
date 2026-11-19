"""pipeline/daemon.py - Continuous background coordinator & Git watcher service."""
from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.config import (
    default_project_features,
    get_repository,
    is_kernel_project,
    repository_project,
    resolve_repo_path,
)
from core.distributed import ROLE_DAEMON
from core.globalstuff import COLOR, G
from core.quit_listener import GracefulQuitListener
from pipeline.git_network import repo_project_slug, resolve_git_network_and_repo
from pipeline.git_sync import sync_and_enqueue_repo_tasks, sync_git_repository
from pipeline.scheduler import DistributedTaskScheduler
from pipeline.state import logger


def _repo_entry_details(entry: Any) -> tuple[str, str, str | None, str | None]:
    """Return `(repo_name, repo_path, start_ref, project_slug)` from a repository config mapping or path."""
    if isinstance(entry, (str, os.PathLike)):
        path = str(Path(entry))
        return Path(path).name or "linux", path, None, None
    if isinstance(entry, dict):
        path = str(resolve_repo_path(entry)) if entry.get("path") else ""
        name = str(entry.get("name") or (Path(path).name if path else "linux"))
        start_ref = entry.get("start_ref") or entry.get("root_commit") or None
        project_slug = str(entry.get("project") or "").strip() or None
        return name, path, str(start_ref) if start_ref else None, project_slug
    return "linux", "", None, None


def run_daemon_cycle(
    db_factory: Any,
    repos: Sequence[Any],
    *,
    node_id: str,
    lease_ttl: int = 180,
    auto_git_update: bool = True,
    git_update_interval: int = 0,
    last_git_sync: dict[str, float] | None = None,
    delegate_stable: bool = True,
    delegate_branches: Sequence[str] | None = None,
    include_pre_releases: bool = True,
    enqueue_preparse: bool = True,
) -> dict[str, Any]:
    """Execute one Daemon coordination cycle: sync Git mirrors, sweep zombie tasks, and enqueue work.

    Never executes heavy source file parsing (`update()`) or libclang initialization.
    """
    summary: dict[str, Any] = {
        "repos_checked": 0,
        "repos_synced": 0,
        "zombies_recovered": 0,
        "tasks_enqueued": 0,
        "errors": 0,
    }

    for entry in repos:
        if isinstance(entry, dict) and entry.get("enabled") is False:
            continue
        repo_name, repo_path, start_ref, project_slug = _repo_entry_details(entry)
        if not repo_path or not os.path.isdir(repo_path):
            logger.warning(COLOR.yellow(f"[Daemon] Skipping missing repository directory '{repo_path}' ({repo_name})."))
            summary["errors"] += 1
            continue

        summary["repos_checked"] += 1
        try:
            now_mono = time.monotonic()
            should_sync = bool(auto_git_update) and (
                last_git_sync is None
                or git_update_interval <= 0
                or (now_mono - last_git_sync.get(repo_path, float("-inf"))) >= git_update_interval
            )
            if should_sync:
                sync_res = sync_git_repository(repo_path, raise_on_error=False)
                if last_git_sync is not None:
                    last_git_sync[repo_path] = now_mono
                if sync_res.error:
                    logger.warning(COLOR.yellow(f"[Daemon] Git sync warning for '{repo_name}': {sync_res.error}"))
                    summary["errors"] += 1
                elif not sync_res.skipped:
                    summary["repos_synced"] += 1

            slug = project_slug or repo_project_slug(repo_path)
            is_kernel = is_kernel_project(slug)
            if isinstance(entry, dict) and entry.get("project"):
                is_kernel = is_kernel_project(repository_project(entry))
            network_id, repo_id = resolve_git_network_and_repo(
                repo_path,
                repo_name=repo_name,
                project_slug=slug,
            )

            with db_factory() as db:
                if db is None:
                    summary["errors"] += 1
                    continue
                scheduler = DistributedTaskScheduler(
                    db_factory,
                    network_id,
                    node_id,
                    ROLE_DAEMON,
                    heartbeat_timeout=lease_ttl,
                )
                recovered = scheduler.sweep_zombies(db)
                summary["zombies_recovered"] += int(recovered or 0)

                enq_res = sync_and_enqueue_repo_tasks(
                    db,
                    network_id,
                    repo_id,
                    repo_name,
                    repo_path,
                    start_ref=start_ref,
                    include_pre_releases=include_pre_releases,
                    delegate_stable=delegate_stable,
                    delegate_branches=delegate_branches,
                    enqueue_main=True,
                    enqueue_preparse=enqueue_preparse and bool(default_project_features(slug).get("insights", is_kernel)),
                    is_kernel=is_kernel,
                )
                summary["tasks_enqueued"] += int(enq_res.get("enqueued", 0) or 0)
        except Exception as exc:
            logger.error(f"[Daemon] Cycle error for repository '{repo_name}' ({repo_path}): {exc}", exc_info=True)
            summary["errors"] += 1

    return summary


def run_daemon_loop(
    args: Any,
    repos: Sequence[Any],
    *,
    node_id: str,
    lease_ttl: int = 180,
    heartbeat_interval: int = 30,
    poll_interval: int = 60,
    once: bool = False,
) -> int:
    """Run the continuous Daemon polling loop until stopped, deadline exceeded, or `once=True`."""
    stop_event = threading.Event()
    quit_listener = GracefulQuitListener(on_request=stop_event.set)
    quit_listener.start()

    prev_sigint = None
    prev_sigterm = None
    installed_signals = False
    if threading.current_thread() is threading.main_thread():
        def _handle_signal(_signum: int, _frame: Any) -> None:
            stop_event.set()

        try:
            prev_sigint = signal.getsignal(signal.SIGINT)
            prev_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            installed_signals = True
        except (OSError, ValueError):
            installed_signals = False

    no_git_update = getattr(args, "no_git_update", False) is True
    git_auto_flag = getattr(args, "git_auto_update", True)
    auto_git_update = (not no_git_update) and (git_auto_flag is not False)
    raw_git_interval = getattr(args, "git_update_interval", 300)
    git_update_interval = raw_git_interval if isinstance(raw_git_interval, int) and raw_git_interval > 0 else 300
    delegate_stable = getattr(args, "delegate_stable", True) is not False
    raw_branches = getattr(args, "delegate_branches", None)
    delegate_branches = raw_branches if isinstance(raw_branches, (list, tuple, set)) else None
    include_pre_releases = getattr(args, "include_pre_releases", True) is not False
    wait_seconds = max(1, int(poll_interval)) if isinstance(poll_interval, int) and poll_interval > 0 else 60
    last_git_sync: dict[str, float] = {}

    repo_list = list(repos)
    if not repo_list:
        default_entry = get_repository(None)
        if default_entry:
            repo_list = [default_entry]

    logger.info(
        COLOR.cyan(
            f"[Daemon] Coordinator loop started (node: '{node_id}', poll={wait_seconds}s, "
            f"heartbeat={heartbeat_interval}s, git_update={'off' if not auto_git_update else f'{git_update_interval}s'})."
        )
    )

    try:
        while not G.is_deadline_exceeded() and not stop_event.is_set() and not quit_listener.requested.is_set():
            cycle_res = run_daemon_cycle(
                G.DB,
                repo_list,
                node_id=node_id,
                lease_ttl=lease_ttl,
                auto_git_update=auto_git_update,
                git_update_interval=git_update_interval,
                last_git_sync=last_git_sync,
                delegate_stable=delegate_stable,
                delegate_branches=delegate_branches,
                include_pre_releases=include_pre_releases,
            )
            logger.info(
                "[Daemon] Cycle summary: checked=%d synced=%d zombies=%d enqueued=%d errors=%d",
                cycle_res["repos_checked"],
                cycle_res["repos_synced"],
                cycle_res["zombies_recovered"],
                cycle_res["tasks_enqueued"],
                cycle_res["errors"],
            )
            if once or G.is_deadline_exceeded() or quit_listener.requested.is_set():
                break
            if stop_event.wait(wait_seconds):
                break
    finally:
        quit_listener.stop()
        if installed_signals:
            try:
                if prev_sigint is not None:
                    signal.signal(signal.SIGINT, prev_sigint)
                if prev_sigterm is not None:
                    signal.signal(signal.SIGTERM, prev_sigterm)
            except (OSError, ValueError):
                pass
    logger.info("[Daemon] Coordinator loop stopped cleanly.")
    return 0
