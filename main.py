"""main.py - CLI entry point of the kernel parser.

`main()` applies the parsed arguments (`pipeline/cli.py`), opens the database, runs the startup migrations
(`pipeline/db_setup.py`), then dispatches the requested mode: version parsing (`pipeline/update.py`), repair
(`pipeline/repair.py`), backfills (`pipeline/backfill_steps.py`), distributed master/worker or the unit tests.
The parse pipeline itself lives in the `pipeline/` package (see `pipeline/__init__.py` for the module map).
"""
from core.globalstuff import (
    G,
    COLOR,
    setup_memory_limit,
)
from core.quit_listener import GracefulQuitListener
from core import ingest_state, distributed
import contextlib
import os
from pathlib import Path
import sys
import time
from core.DBLayout import (
    TABLES,
    m_v_main,
)
from core.config import (
    default_project_features,
    get_default_repo_path,
    get_mailing_list_config,
    get_parser_config,
    get_repositories_config,
    get_repository,
    get_ssh_tunnel_config,
    is_kernel_project,
    repo_feature_enabled,
    repository_project,
    resolve_repo_path,
)
from core.ssh_tunnel import start_ssh_tunnel
from db_engine import ge, gt
from pipeline.backfill_steps import run_automatic_backfills, run_requested_backfills
from pipeline.cli import arg_handling
from pipeline.daemon import run_daemon_loop
from pipeline.db_setup import drop_familyless_blob_facts, drop_legacy_symbol_tables, drop_obsolete_kconfig_tables, drop_outdated_derived_tables, ensure_db_instance, ensure_db_views, ensure_declared_indexes, outdated_layout
from pipeline.external_steps import processing_cve_list, processing_cve_releases, processing_release_stats, processing_unified_preparse
from pipeline.git_ingest import EMPTY_TREE, _forget_rolled_back_rows, _purge_partial_version, _resolve_legacy_state, refresh_person_identities
from pipeline.git_network import repo_project_slug, resolve_git_network_and_repo
from pipeline.git_sync import (
    discover_local_repo_capabilities,
    git_repo_lock,
    sync_and_enqueue_repo_tasks,
    sync_git_repository,
    verify_local_ref_exists,
)
from pipeline.mailing_list_steps import standalone_resolve_mailing_list
from pipeline.multicore import _apply_db_server_tuning, _preflight_libclang
from pipeline.repair import orchestrate_repair_pipeline
from pipeline.scheduler import DistributedTaskScheduler
from pipeline.state import MF, gp, logger
from pipeline.systemic import SystemicParseError
from pipeline.update import update




def _arg_flag_true(args: object, name: str) -> bool:
    return getattr(args, name, False) is True


def _arg_flag_false(args: object, name: str) -> bool:
    return getattr(args, name, True) is False


def _arg_path_str(args: object, name: str) -> str | None:
    val = getattr(args, name, None)
    if isinstance(val, (str, os.PathLike)):
        s = str(val).strip()
        return s or None
    return None


def _arg_pos_int(args: object, name: str, default: int) -> int:
    val = getattr(args, name, None)
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return val
    return default


def _is_git_repo_dir(path: str | Path) -> bool:
    p = Path(path)
    return p.is_dir() and ((p / ".git").exists() or ((p / "HEAD").is_file() and (p / "objects").is_dir()))


def _rollback_and_clean_ingest_state(db: object, network_id: int, commit_id: int) -> None:
    if not commit_id or db is None:
        return
    rb = ingest_state.rollback_version(db, network_id, commit_id, gp.Table_Array)
    if rb is False:
        ingest_state.purge_partial_version(db, commit_id)
    with contextlib.suppress(Exception):
        db.delete_rows(
            "m_ingest_state",
            {"network_id": network_id, "commit_id": commit_id, "status": ingest_state.INGEST_PARSING},
        )


def _collect_local_repo_entries(repo_entry: dict | None, repo_path: str, explicit_repo: object) -> tuple[list[dict], dict[str, tuple[str, int, int]]]:
    entries: list[dict] = []
    if isinstance(explicit_repo, str) and explicit_repo.strip():
        if repo_entry:
            entries = [repo_entry]
        else:
            entries = [{"name": Path(repo_path).name or "linux", "path": repo_path, "enabled": True}]
    else:
        configured = [r for r in get_repositories_config() if isinstance(r, dict) and r.get("enabled", True)]
        entries = configured or ([repo_entry] if repo_entry else [{"name": Path(repo_path).name or "linux", "path": repo_path, "enabled": True}])

    repo_meta_by_name: dict[str, tuple[str, int, int]] = {}
    for entry in entries:
        r_path = str(resolve_repo_path(entry)) if entry.get("path") else repo_path
        r_name = str(entry.get("name") or Path(r_path).name or "linux")
        if _is_git_repo_dir(r_path):
            net_id, r_id = resolve_git_network_and_repo(r_path, repo_name=r_name)
        else:
            net_id = getattr(gp, "network_id", 1) or 1
            r_id = getattr(gp, "repo_id", 1) or 1
        repo_meta_by_name[r_name] = (r_path, net_id, r_id)
    return entries, repo_meta_by_name


def _register_node_capabilities(
    db: object,
    node_id: str,
    network_id: int,
    role: str,
    repo_entries: list[dict],
    default_repo_path: str,
    include_pre_releases: bool,
    repo_meta_by_name: dict[str, tuple[str, int, int]] | None = None,
) -> int:
    if db is None or not node_id:
        return 0
    caps_by_net: dict[int, list[dict]] = {}
    repos_seen = 0
    for entry in repo_entries:
        if isinstance(entry, dict) and entry.get("enabled") is False:
            continue
        r_path = str(resolve_repo_path(entry)) if entry.get("path") else default_repo_path
        r_name = str(entry.get("name") or Path(r_path).name or "linux")
        if _is_git_repo_dir(r_path):
            repos_seen += 1
            net_id = int(repo_meta_by_name[r_name][1]) if (repo_meta_by_name and r_name in repo_meta_by_name) else int(network_id)
            discovered = discover_local_repo_capabilities(
                r_path,
                net_id,
                r_name,
                include_pre_releases=include_pre_releases,
                supports_preparse=True,
            )
            if discovered:
                caps_by_net.setdefault(net_id, []).extend(discovered)
    total_tracks = 0
    for net_id, net_caps in caps_by_net.items():
        db.register_worker_capabilities(node_id, net_id, net_caps, role=role)
        total_tracks += len(net_caps)
    if total_tracks:
        logger.info(COLOR.cyan(
            f"[Distributed] Registered {total_tracks} local capability track(s) across {repos_seen} repo(s) "
            f"for {role} node '{node_id}'."
        ))
    return total_tracks


def _resolve_base_commit_id_for_task(
    db: object,
    network_id: int,
    repo_id: int,
    repo_name: str,
    base_ref_name: str,
    base_commit_id: object,
) -> int:
    if not base_ref_name:
        return 0
    cid = int(base_commit_id or 0)
    if cid > 0 or db is None:
        return cid
    try:
        ref_row = db.select_one(
            "m_git_ref",
            ["commit_id"],
            {"repo_id": int(repo_id), "ref_name": base_ref_name, "commit_id": gt(0)},
            as_dict=False,
        )
        if ref_row and ref_row[0]:
            return int(ref_row[0])
    except Exception:
        pass
    try:
        task_row = db.select_one(
            "distributed_tasks",
            ["commit_id"],
            {
                "network_id": int(network_id),
                "repo_name": repo_name,
                "ref_name": base_ref_name,
                "status": distributed.DIST_STATUS_COMPLETED,
                "commit_id": gt(0),
            },
            as_dict=False,
        )
        if task_row and task_row[0]:
            return int(task_row[0])
    except Exception:
        pass
    return 0


def _shutdown_node() -> None:
    """Stop the heartbeat, remove this node from m_parse_node, and release any singleton PID lock.

    Idempotent and never raises: runs on every way out of `main()` (including `G.emergency_shutdown()`).
    """
    daemon = getattr(G, "heartbeat_daemon", None)
    node_id = getattr(G, "node_id", None)
    singleton_lock = getattr(G, "singleton_lock", None)
    G.heartbeat_daemon = None
    G.node_id = None
    G.singleton_lock = None
    if daemon is not None:
        with contextlib.suppress(Exception):
            daemon.stop()
    if node_id:
        try:
            with G.DB() as db:
                distributed.unregister_node(db, node_id)
        except BaseException as e:  # noqa: BLE001 - e.g. Ctrl+C also stopped the SSH tunnel
            logger.warning(COLOR.yellow(f"[Distributed] Node '{node_id}' stays registered until its heartbeat expires: {e}"))
    if singleton_lock is not None:
        with contextlib.suppress(Exception):
            singleton_lock.release()


def main() -> None:
    """Set the plan for what version to parse."""
    try:
        _run()
    finally:
        _shutdown_node()


def _run() -> None:
    """Apply the arguments and run the requested mode."""
    args = arg_handling()
    if args.mem_max > 0:
        setup_memory_limit(float(args.mem_max))
    role = getattr(args, "role", "standalone") or "standalone"
    is_distributed = role in (distributed.ROLE_MASTER, distributed.ROLE_WORKER, distributed.ROLE_DAEMON)
    G.distributed = is_distributed
    G.distributed_role = role
    if hasattr(G.TE, "distributed"):
        G.TE.distributed = is_distributed

    with G.DB() as db:
        _apply_db_server_tuning(db, float(args.mem_max))
        if getattr(args, "reset", False) or getattr(args, "Drop", False):
            logger.info("Resetting and recreating all database tables...")
            db.drop_table(gp.Table_Array)
            drop_legacy_symbol_tables(db)
            drop_obsolete_kconfig_tables(db)
            db.create_table(gp.Table_Array)
            try:
                db.create_index("v_main_index", m_v_main, (m_v_main.vname,))
            except Exception:
                pass
            ensure_db_instance(db)
            ensure_db_views(db)
            if hasattr(G.TE, "start_new_db") and getattr(G.TE, "tables", None):
                G.TE.start_new_db(G.DB, is_worker=False)
            if getattr(args, "reset_only", False):
                logger.info(COLOR.green("Table reset complete; exiting without parsing (reset_only)."))
                G.emergency_shutdown(0)
        else:
            if role not in (distributed.ROLE_WORKER, distributed.ROLE_DAEMON):
                drop_obsolete_kconfig_tables(db)
                outdated = outdated_layout(db)
                if outdated:
                    raise RuntimeError(
                        "The database was created with an older table layout (" + "; ".join(outdated) + "): "
                        "rebuild it with --reset (core/API.md section 4)."
                    )
                drop_familyless_blob_facts(db)
                drop_outdated_derived_tables(db)
            missing = db.test_tables(gp.Table_Array)
            if missing:
                logger.info(f"Missing tables detected ({missing}), creating tables...")
                missing_tables = [tbl for tbl in gp.Table_Array if tbl.table_name in missing]
                db.create_table(missing_tables)
                try:
                    db.create_index("v_main_index", m_v_main, (m_v_main.vname,))
                except Exception:
                    pass
            if role not in (distributed.ROLE_WORKER, distributed.ROLE_DAEMON):
                ensure_db_instance(db)
                ensure_db_views(db)
                ensure_declared_indexes(db)
                refresh_person_identities(db, "start", only_if_incomplete=True)
        if is_distributed:
            distributed.ensure_distributed_tables(db)

    # Repository: --repo may be a configured repository name or a path; default is the configured default
    repo_arg = getattr(args, "repo", None)
    repo_entry = get_repository(repo_arg)
    if repo_entry:
        repo_path = str(resolve_repo_path(repo_entry))
    elif repo_arg:
        repo_path = repo_arg
    else:
        repo_path = str(get_default_repo_path())
    MF.set_repo_dir(repo_path)
    logger.info(f"[Repository] Using '{(repo_entry or {}).get('name', repo_path)}' at {repo_path}")

    # The CVE list (cve.repo_dir) is ingested when its HEAD moved, and its abbreviated commit hashes resolved
    # in this repository; --cve does only this. A repository of a project that is not a kernel tree (man-pages)
    # has no kernel commits to resolve: the CVE list is left to the kernel repository's runs
    if repo_entry is None:
        slug = repo_project_slug(repo_path, [MF.get_root_commit() or ""])
        kernel_repo = is_kernel_project(slug)
        cve_enabled = default_project_features(slug)["cve"]
    else:
        kernel_repo = is_kernel_project(repository_project(repo_entry))
        cve_enabled = repo_feature_enabled(repo_entry, "cve")
    if not kernel_repo and getattr(args, "cve", False):
        logger.warning(COLOR.yellow(f"[CVE] '{repo_path}' is not a kernel repository; --cve has nothing to resolve in it."))
    if kernel_repo and role not in (distributed.ROLE_WORKER, distributed.ROLE_DAEMON) and (
        getattr(args, "cve", False) or (cve_enabled and not getattr(args, "no_cve", False))
    ):
        _cve_net, cve_repo_id = resolve_git_network_and_repo(repo_path)
        cve_pull = bool(getattr(args, "cve_pull", False))
        summary = processing_cve_list(
            force=bool(getattr(args, "cve_full", False)),
            kernel_repo=repo_path,
            repo_id=cve_repo_id,
            pull=cve_pull,
        )
        if summary and summary.get("state") == "current":
            logger.info(f"[CVE] The CVE list is current ({summary.get('records')} kernel records, HEAD {str(summary.get('head'))[:12]}).")
        cve_interval = int(getattr(args, "cve_interval", 0) or 0)
        if getattr(args, "cve", False) and cve_interval > 0:
            import threading
            stop_ev = threading.Event()
            quit_listener = GracefulQuitListener(on_request=stop_ev.set)
            quit_listener.start()
            logger.info(COLOR.cyan(f"[CVE] Refresh loop active: checking every {cve_interval}s (press 'q' or Ctrl+C to stop)."))
            try:
                while not G.is_deadline_exceeded() and not stop_ev.wait(cve_interval):
                    if quit_listener.requested.is_set():
                        break
                    summary = processing_cve_list(
                        force=False,
                        kernel_repo=repo_path,
                        repo_id=cve_repo_id,
                        pull=cve_pull,
                    )
                    if summary and summary.get("state") == "current":
                        logger.debug(f"[CVE] Current at {str(summary.get('head'))[:12]}.")
            except KeyboardInterrupt:
                pass
            finally:
                quit_listener.stop()
    if getattr(args, "cve", False):
        G.emergency_shutdown(0)
        return

    if getattr(args, "clean_temp", False):
        ingest_state.cleanup_ramdisk_artifacts(getattr(G, "RAMDISK", None))

    # One mailing list tunnel for the whole run: the per-version forked commit workers and the
    # per-version Step 6.5 client inherit this forward (core/API.md 7.2) instead of each opening
    # - and, in a forked worker, orphaning - an SSH connection of their own.
    ml_tunnel_cfg = get_ssh_tunnel_config("mailing_list")
    if get_mailing_list_config().get("enabled", True) and ml_tunnel_cfg.get("enabled"):
        try:
            ml_tunnel = start_ssh_tunnel(ml_tunnel_cfg, feature="mailing_list")
            logger.info(f"[Mailing List] Shared SSH tunnel established on 127.0.0.1:{ml_tunnel.local_port} for the whole run.")
        except Exception as exc:
            logger.warning(COLOR.yellow(
                f"[Mailing List] Could not establish the shared SSH tunnel ({exc}); each worker will tunnel on its own."
            ))

    if getattr(args, "resolve_ml", False):
        commit_range = getattr(args, "ml_range", None) or (args.ref[0] if getattr(args, "ref", None) else "HEAD~100..HEAD")
        standalone_resolve_mailing_list(repo_path, commit_range, getattr(args, "ml_storage_dir", None))
        G.emergency_shutdown(0)
        return

    raw_auto_git = getattr(args, "git_auto_update", None)
    raw_no_git = getattr(args, "no_git_update", None)
    if _arg_flag_true(args, "no_git_update"):
        auto_git_sync = False
    elif isinstance(raw_auto_git, bool):
        auto_git_sync = raw_auto_git
    elif isinstance(raw_no_git, bool):
        auto_git_sync = not raw_no_git
    else:
        auto_git_sync = False

    force_singleton = _arg_flag_true(args, "force_singleton")
    lock_dir = _arg_path_str(args, "lock_dir")
    poll_interval = _arg_pos_int(args, "poll_interval", distributed.DEFAULT_POLL_INTERVAL)
    git_update_interval = _arg_pos_int(args, "git_update_interval", distributed.DEFAULT_GIT_UPDATE_INTERVAL)
    lease_ttl, heartbeat_interval = distributed.resolve_lease_settings(get_parser_config().get("distributed"))
    node_id = None
    heartbeat_daemon = None

    if role == distributed.ROLE_DAEMON:
        network_id, _repo_id = resolve_git_network_and_repo(repo_path)
        node_id = distributed.get_default_node_id(role, _arg_path_str(args, "worker_id"))
        G.node_id = node_id
        if _shutdown_node not in G.shutdown_hooks:
            G.shutdown_hooks.append(_shutdown_node)
        try:
            singleton_lock = distributed.acquire_singleton_lock(
                role, network_id, node_id, lock_dir=lock_dir, force=force_singleton
            )
            G.singleton_lock = singleton_lock
            with G.DB() as db:
                distributed.register_node(db, node_id, role, network_id, force=force_singleton)
        except distributed.SingletonRoleConflictError as exc:
            logger.critical(COLOR.red(f"[Daemon] Singleton lock conflict: {exc}"))
            _shutdown_node()
            sys.exit(1)
        heartbeat_daemon = distributed.NodeHeartbeatDaemon(
            G.DB,
            node_id,
            interval=heartbeat_interval,
            lease_ttl=lease_ttl,
            role=role,
            network_id=network_id,
            singleton_lock=singleton_lock,
        )
        heartbeat_daemon.start()
        G.heartbeat_daemon = heartbeat_daemon
        daemon_repos, _ = _collect_local_repo_entries(repo_entry, repo_path, getattr(args, "repo", None))
        daemon_once = _arg_flag_true(args, "daemon_once")
        try:
            rc = run_daemon_loop(
                args,
                daemon_repos,
                node_id=node_id,
                lease_ttl=lease_ttl,
                heartbeat_interval=heartbeat_interval,
                poll_interval=poll_interval,
                once=daemon_once,
            )
        finally:
            _shutdown_node()
        G.emergency_shutdown(rc)
        return

    if role in (distributed.ROLE_MASTER, distributed.ROLE_WORKER) and auto_git_sync and _is_git_repo_dir(repo_path):
        sync_res = sync_git_repository(repo_path, raise_on_error=False)
        if sync_res.error:
            logger.warning(COLOR.yellow(f"[GitSync] Startup update warning for '{repo_path}': {sync_res.error}"))

    is_branch_pipeline = bool(getattr(args, "branches", False))
    if is_branch_pipeline:
        from core.config import update_repository_branches
        discovered_branches = update_repository_branches((repo_entry or {}).get("name"), write=True)
        if not discovered_branches:
            logger.warning(COLOR.yellow(f"No branches matching branch_filter discovered for repository '{repo_path}'."))
            G.emergency_shutdown(0)
            return
        logger.info(COLOR.cyan(
            f"[Branch Pipeline] Discovered {len(discovered_branches)} branch(es) matching branch_filter; "
            f"updated configuration: {', '.join(discovered_branches[:8])}{'...' if len(discovered_branches) > 8 else ''}"
        ))
        target_refs = discovered_branches
        single_version = True
    else:
        raw_refs = getattr(args, "ref", None)
        if not raw_refs:
            # Default: parse from the first commit of the repository, then every subsequent release
            start_ref = (repo_entry or {}).get("start_ref") or (repo_entry or {}).get("root_commit") or MF.get_root_commit()
            if not start_ref:
                logger.critical(COLOR.red(f"Could not resolve the first commit of repository '{repo_path}'."))
                sys.exit(1)
            logger.info(f"[Release Pipeline] No --ref given; starting from the first commit {start_ref[:12]}")
            raw_refs = [start_ref]
        elif isinstance(raw_refs, str):
            raw_refs = [raw_refs]

        # By default, keep parsing subsequent releases after the first version unless single_version is requested
        single_version = bool(getattr(args, "single_version", False))
        include_pre_releases = getattr(args, "include_pre_releases", None)
        if len(raw_refs) == 1 and not single_version:
            start_ref = raw_refs[0]
            discovered = MF.get_version_tags(start_ref=start_ref, include_pre_releases=include_pre_releases)
            if len(discovered) > 1:
                logger.info(
                    f"[Release Pipeline] Continuous multi-version parsing active (default). "
                    f"Discovered {len(discovered)} sequential releases starting at '{start_ref}': "
                    f"{', '.join(discovered[:6])}{'...' if len(discovered) > 6 else ''}"
                )
                target_refs = discovered
            else:
                target_refs = raw_refs
        else:
            target_refs = raw_refs

    network_id, repo_id = resolve_git_network_and_repo(repo_path)
    with G.DB() as db:
        version_states = ingest_state.fetch_repo_version_states(db, repo_id, network_id)

    if not (
        getattr(args, "release_stats", False)
        or getattr(args, "preparse", False)
        or getattr(args, "dry_run", False)
        or getattr(args, "backfill", None)
    ):
        # Before registering as a node or leasing a task
        _preflight_libclang()

    node_id = None
    heartbeat_daemon = None
    lease_ttl, heartbeat_interval = distributed.resolve_lease_settings(get_parser_config().get("distributed"))
    if is_distributed:
        node_id = distributed.get_default_node_id(role, getattr(args, "worker_id", None))
        G.node_id = node_id
        if _shutdown_node not in G.shutdown_hooks:
            G.shutdown_hooks.append(_shutdown_node)
        singleton_lock = None
        try:
            if role in distributed.SINGLETON_ROLES:
                singleton_lock = distributed.acquire_singleton_lock(
                    role, network_id, node_id, lock_dir=lock_dir, force=force_singleton
                )
                G.singleton_lock = singleton_lock
            with G.DB() as db:
                if force_singleton:
                    distributed.register_node(db, node_id, role, network_id, force=True)
                else:
                    distributed.register_node(db, node_id, role, network_id)
        except distributed.SingletonRoleConflictError as exc:
            logger.critical(COLOR.red(f"[Distributed] Singleton lock conflict: {exc}"))
            _shutdown_node()
            sys.exit(1)
        if singleton_lock is not None:
            heartbeat_daemon = distributed.NodeHeartbeatDaemon(
                G.DB, node_id, interval=heartbeat_interval, lease_ttl=lease_ttl, role=role, network_id=network_id,
                singleton_lock=singleton_lock,
            )
        else:
            heartbeat_daemon = distributed.NodeHeartbeatDaemon(
                G.DB, node_id, interval=heartbeat_interval, lease_ttl=lease_ttl, role=role, network_id=network_id
            )
        heartbeat_daemon.start()
        G.heartbeat_daemon = heartbeat_daemon
    else:
        G.node_id = None
        G.heartbeat_daemon = None

    repo_entries, repo_meta_by_name = _collect_local_repo_entries(repo_entry, repo_path, getattr(args, "repo", None))
    include_pre_val = getattr(args, "include_pre_releases", True)
    cap_include_rc = include_pre_val is not False

    fallback_repo_name = str((repo_entry or {}).get("name") or Path(repo_path).name or "linux")
    if role == distributed.ROLE_WORKER:
        with G.DB() as db:
            _register_node_capabilities(
                db, node_id, network_id, role, repo_entries, repo_path, cap_include_rc,
                repo_meta_by_name=repo_meta_by_name,
            )

        current = {"ref": None}
        quit_listener = GracefulQuitListener(
            on_request=lambda: logger.warning(COLOR.yellow(
                f"[Quit Requested] Finishing '{current['ref']}' before stopping. Press Ctrl+C to abort immediately."
            ))
        )
        quit_listener.start()
        logger.info(COLOR.cyan(f"[Worker] Starting secondary worker parser loop (node: '{node_id}')..."))
        preferred_chain = None
        preferred_track = None
        gp._active_task_id = None
        gp._active_dist_task_id = None
        worker_exit_code = 0
        worker_nets: list[int] = [int(network_id)]
        for _rp, net_id, _rid in repo_meta_by_name.values():
            if int(net_id) not in worker_nets:
                worker_nets.append(int(net_id))
        worker_schedulers = [
            DistributedTaskScheduler(
                G.DB,
                net_id,
                node_id,
                role,
                heartbeat_timeout=lease_ttl,
                max_priority=distributed.PRIORITY_STABLE,
            )
            for net_id in worker_nets
        ]
        last_git_sync = time.monotonic()
        released_missing_worker: set[int] = set()
        try:
            while not G.is_deadline_exceeded() and not quit_listener.requested.is_set():
                if auto_git_sync and (time.monotonic() - last_git_sync) >= git_update_interval and _is_git_repo_dir(repo_path):
                    last_git_sync = time.monotonic()
                    sync_res = sync_git_repository(repo_path, raise_on_error=False)
                    if sync_res.updated:
                        with G.DB() as db:
                            _register_node_capabilities(
                                db, node_id, network_id, role, repo_entries, repo_path, cap_include_rc,
                                repo_meta_by_name=repo_meta_by_name,
                            )

                active_scheduler = worker_schedulers[0]
                with G.DB() as db:
                    task = None
                    for sched in worker_schedulers:
                        task = sched.claim_next(db, preferred_track=preferred_track)
                        if task is not None:
                            active_scheduler = sched
                            break
                    if task is None and db.count_rows("distributed_tasks", {"network_id": network_id}) == 0:
                        distributed.reclaim_expired_leases(db, network_id, lease_ttl=lease_ttl)
                        task = distributed.claim_next_task(
                            db, network_id, node_id, preferred_chain=preferred_chain, lease_ttl=lease_ttl
                        )

                if task is not None and task.get("_queue") == "distributed_tasks":
                    t_id = int(task["task_id"])
                    t_type = str(task.get("task_type") or "stable")
                    ref_name = str(task["ref_name"])
                    b_name = str(task.get("base_ref_name") or "")
                    b_cid = int(task.get("base_commit_id") or 0)
                    cid = int(task.get("commit_id") or 0)
                    preferred_track = str(task.get("track_name") or "") or None
                    t_repo_name = str(task.get("repo_name") or "")
                    task_repo_path, task_net_id, task_repo_id = repo_meta_by_name.get(
                        t_repo_name, (repo_path, network_id, repo_id)
                    )

                    if _is_git_repo_dir(task_repo_path) and not verify_local_ref_exists(task_repo_path, ref_name):
                        logger.warning(COLOR.yellow(
                            f"[Worker] Ref '{ref_name}' not found locally in '{task_repo_path}'; releasing task {t_id}."
                        ))
                        active_scheduler.release(t_id, f"Ref '{ref_name}' missing in local mirror")
                        updated_local = False
                        if auto_git_sync:
                            sync_res = sync_git_repository(task_repo_path, raise_on_error=False)
                            updated_local = bool(sync_res.updated)
                            with G.DB() as db:
                                _register_node_capabilities(
                                    db, node_id, network_id, role, repo_entries, repo_path, cap_include_rc,
                                    repo_meta_by_name=repo_meta_by_name,
                                )
                        preferred_track = None
                        if t_id in released_missing_worker and not updated_local:
                            if getattr(args, "worker_once", False):
                                break
                            time.sleep(min(5.0, float(poll_interval)))
                        released_missing_worker.add(t_id)
                        continue

                    if t_type != "preparse" and b_name and b_cid <= 0:
                        with G.DB() as db:
                            b_cid = _resolve_base_commit_id_for_task(
                                db, task_net_id, task_repo_id, t_repo_name or fallback_repo_name, b_name, b_cid
                            )

                    gp.VID = 0
                    gp._active_task_id = None
                    gp._active_dist_task_id = t_id
                    current["ref"] = ref_name
                    heartbeat_daemon.set_active(cid, dist_task_id=t_id)
                    logger.info(COLOR.green(
                        f"[Worker] Processing distributed task {t_id} ({t_type}: '{ref_name}', track: '{preferred_track}', "
                        f"repo: '{t_repo_name or 'default'}', base: '{b_name}', base_cid: {b_cid}, commit_id: {cid})."
                    ))
                    try:
                        if t_type == "preparse":
                            with git_repo_lock(task_repo_path, exclusive=False):
                                processing_unified_preparse(task_repo_path, task_repo_id, tags=[ref_name])
                            ok = active_scheduler.complete(t_id, cid, ref_name=ref_name)
                            if not ok:
                                logger.warning(COLOR.yellow(f"[Worker] Distributed pre-parse task {t_id} claim was lost before completion."))
                        else:
                            gp.Old_VID = b_cid if (b_name and b_cid > 0) else 0
                            gp.Old_Version_Name = b_name if (b_name and b_cid > 0) else EMPTY_TREE
                            completed = update(ref_name, repo_path=task_repo_path)
                            if completed is False:
                                logger.warning(COLOR.yellow(
                                    f"[Worker] Halted '{ref_name}' on execution deadline; rolling back and releasing task {t_id}."
                                ))
                                with G.DB() as db:
                                    _rollback_and_clean_ingest_state(db, task_net_id, getattr(gp, "VID", 0))
                                    active_scheduler.release(t_id, "Execution deadline reached", db=db)
                                break
                            ok = active_scheduler.complete(t_id, getattr(gp, "VID", cid), ref_name=ref_name)
                            if not ok:
                                logger.warning(COLOR.yellow(f"[Worker] Distributed task {t_id} claim was lost/revoked before completion."))
                    except SystemicParseError as e:
                        with G.DB() as db:
                            _rollback_and_clean_ingest_state(db, task_net_id, getattr(gp, "VID", 0))
                            active_scheduler.release(t_id, f"Released by {node_id}: {e}", db=db)
                        logger.critical(COLOR.red(
                            f"[Worker] Stopping: '{ref_name}' failed with one error across most files, so every task "
                            "would fail the same way on this node. Fix the cause above and restart the worker."
                        ))
                        worker_exit_code = 1
                        break
                    except Exception as e:
                        logger.error(f"[Worker] Failed distributed task {t_id} ('{ref_name}'): {e}", exc_info=True)
                        with G.DB() as db:
                            _rollback_and_clean_ingest_state(db, task_net_id, getattr(gp, "VID", 0))
                            active_scheduler.fail(t_id, str(e), db=db)
                    finally:
                        gp._active_dist_task_id = None
                        heartbeat_daemon.clear_active()
                elif task is not None:
                    t_id = task["task_id"]
                    ref_name = task["ref_name"]
                    b_name = task["base_ref_name"]
                    b_cid = task["base_commit_id"]
                    cid = task["commit_id"]
                    preferred_chain = task["chain_name"]
                    gp.VID = 0
                    gp._active_dist_task_id = None
                    gp._active_task_id = t_id
                    heartbeat_daemon.set_active(cid, task_id=t_id)
                    current["ref"] = ref_name
                    gp.Old_VID = b_cid
                    gp.Old_Version_Name = b_name
                    logger.info(COLOR.green(
                        f"[Worker] Processing assigned task {t_id} ('{ref_name}', chain: '{preferred_chain}', base: '{b_name}', commit_id: {cid})."
                    ))
                    try:
                        completed = update(ref_name, repo_path=repo_path)
                        if completed is False:
                            logger.warning(COLOR.yellow(
                                f"[Worker] Halted '{ref_name}' on execution deadline; rolling back and releasing task {t_id}."
                            ))
                            with G.DB() as db:
                                if distributed.check_lease_held(db, t_id, node_id):
                                    _rollback_and_clean_ingest_state(db, network_id, getattr(gp, "VID", 0))
                                    distributed.release_task(db, t_id, "Execution deadline reached", node_id)
                            break
                        with G.DB() as db:
                            success = distributed.mark_task_done(
                                db, t_id, getattr(gp, "VID", cid), network_id, ref_name=ref_name, worker_id=node_id
                            )
                            if not success:
                                logger.warning(COLOR.yellow(f"[Worker] Task {t_id} lease was lost/revoked before completion. Skipping rollback to avoid corrupting peer data."))
                    except SystemicParseError as e:
                        # This node cannot parse: undo the version, return the task unpenalized, and stop
                        with G.DB() as db:
                            if distributed.check_lease_held(db, t_id, node_id):
                                _rollback_and_clean_ingest_state(db, network_id, getattr(gp, "VID", 0))
                                distributed.release_task(db, t_id, f"Released by {node_id}: {e}", node_id)
                        logger.critical(COLOR.red(
                            f"[Worker] Stopping: '{ref_name}' failed with one error across most files, so every task "
                            "would fail the same way on this node. Fix the cause above and restart the worker."
                        ))
                        worker_exit_code = 1
                        break
                    except Exception as e:
                        logger.error(f"[Worker] Failed parsing '{ref_name}': {e}", exc_info=True)
                        with G.DB() as db:
                            lease_held = distributed.check_lease_held(db, t_id, node_id)
                            if lease_held:
                                _rollback_and_clean_ingest_state(db, network_id, getattr(gp, "VID", 0))
                                distributed.mark_task_failed(db, t_id, str(e), worker_id=node_id)
                            else:
                                logger.warning(COLOR.yellow(f"[Worker] Lease for task {t_id} was lost/revoked; skipping rollback to avoid corrupting peer data."))
                    finally:
                        gp._active_task_id = None
                        heartbeat_daemon.clear_active()
                else:
                    preferred_chain = None
                    preferred_track = None
                    if getattr(args, "worker_once", False):
                        logger.info("[Worker] No eligible tasks found and --worker-once is set. Exiting.")
                        break
                    with G.DB() as db:
                        dist_count = sum(db.count_rows("distributed_tasks", {"network_id": nid}) for nid in worker_nets)
                        legacy_count = db.count_rows("m_parse_task", {"network_id": network_id})
                        if dist_count > 0 and legacy_count == 0:
                            finished = all(sched.is_queue_drained(db) for sched in worker_schedulers)
                        elif dist_count > 0 and legacy_count > 0:
                            finished = (
                                all(sched.is_queue_drained(db) for sched in worker_schedulers)
                                and distributed.is_parsing_finished(db, network_id)
                            )
                        else:
                            finished = distributed.is_parsing_finished(db, network_id)
                    if finished:
                        logger.info(COLOR.green("[Worker] All parsing completed across repository. Exiting cleanly."))
                        break
                    logger.info("[Worker] Queue is currently empty. Waiting for more versions from Master/Daemon...")
                    time.sleep(min(5.0, float(poll_interval)))
        finally:
            quit_listener.stop()
            heartbeat_daemon.stop()
            try:
                with G.DB() as db:
                    if db is not None:
                        refresh_person_identities(db, "worker shutdown", only_if_incomplete=True)
            except Exception as e:  # noqa: BLE001
                # E.g. Ctrl+C also stopped the SSH tunnel: an unfinished lease expires and a peer reclaims it
                logger.warning(COLOR.yellow(f"[Worker] Shutdown cleanup skipped, database unreachable: {e}"))
            _shutdown_node()
        logger.info("[Worker] Secondary worker shutdown complete.")
        G.emergency_shutdown(worker_exit_code)
        return

    master_repo_name = fallback_repo_name
    if role == distributed.ROLE_MASTER:
        # Release tags the chains are built from: the documented default list starts at v3.0, a run that
        # starts at an older release takes the list from there
        all_tags = MF.get_version_tags(include_pre_releases=True)
        if target_refs and target_refs[0] in all_tags:
            all_tags = all_tags[all_tags.index(target_refs[0]):]
        elif target_refs and not is_branch_pipeline:
            from_start = MF.get_version_tags(start_ref=target_refs[0], include_pre_releases=True)
            if len(from_start) > 1:
                all_tags = from_start
        delegate_stable = bool(getattr(args, "delegate_stable", True))
        branch_refs = distributed.delegated_branches(getattr(args, "delegate_branches", None), repo_path)
        with G.DB() as db:
            _register_node_capabilities(
                db, node_id, network_id, role, repo_entries, repo_path, cap_include_rc,
                repo_meta_by_name=repo_meta_by_name,
            )
            if _is_git_repo_dir(repo_path):
                enq = sync_and_enqueue_repo_tasks(
                    db,
                    network_id,
                    repo_id,
                    master_repo_name,
                    repo_path,
                    start_ref=target_refs[0] if (target_refs and not is_branch_pipeline) else None,
                    include_pre_releases=cap_include_rc,
                    delegate_stable=delegate_stable,
                    delegate_branches=branch_refs,
                    enqueue_main=True,
                    enqueue_preparse=bool(kernel_repo),
                    is_kernel=kernel_repo,
                )
                logger.info(COLOR.cyan(
                    f"[Distributed] Queue sync for '{master_repo_name}': {enq['total']} task(s) "
                    f"({enq['enqueued']} new/updated, {enq['unchanged']} unchanged; "
                    f"main={enq['main']}, stable={enq['stable']}, next={enq['next']}, "
                    f"branch={enq['branch']}, preparse={enq['preparse']})."
                ))
        # The master keeps what is not delegated: mainline release tags, trunk branches and refs that are no
        # release tag (core/distributed.py partition_master_refs)
        target_refs, delegated_refs = distributed.partition_master_refs(target_refs, all_tags, delegate_stable, branch_refs)
        logger.info(COLOR.cyan(
            f"[Master] Parsing {len(target_refs)} mainline/trunk ref(s); {len(delegated_refs)} stable/next/branch "
            f"ref(s) of this run are published as tasks for workers."
        ))

    # Release statistics of every release tag (core/API.md section 10); --release-stats does only this
    if getattr(args, "release_stats", False):
        processing_release_stats(repo_path, repo_id)
        G.emergency_shutdown(0)
        return

    if getattr(args, "preparse", False):
        include_insights = True if getattr(args, "preparse_insights", None) is None else bool(args.preparse_insights)
        include_stable = True if getattr(args, "preparse_stable", None) is None else bool(args.preparse_stable)
        include_api = True if getattr(args, "preparse_api", None) is None else bool(args.preparse_api)
        raw_tags = getattr(args, "preparse_tags", None)
        preparse_tags = (
            [t.strip() for item in raw_tags for t in str(item).split(",") if t.strip()]
            if raw_tags is not None
            else None
        )
        raw_arches = getattr(args, "kapi_arches", None)
        kapi_arches = (
            tuple(dict.fromkeys(a.strip().lower() for a in raw_arches if a and a.strip()))
            if raw_arches
            else ("x86_64",)
        )
        summary = processing_unified_preparse(
            repo_path,
            repo_id,
            include_insights=include_insights,
            include_stable=include_stable,
            include_api=include_api,
            tags=preparse_tags,
            from_tag=getattr(args, "backfill_from", None),
            arches=kapi_arches,
            workers=getattr(args, "preparse_workers", None),
        )
        logger.info(f"[Pre-parse] Summary: {summary}")
        if not getattr(args, "no_release_stats", False) and preparse_tags is None and include_insights:
            processing_release_stats(repo_path, repo_id)
            if kernel_repo and cve_enabled and not getattr(args, "no_cve", False):
                processing_cve_releases(repo_path, repo_id)
        G.emergency_shutdown(0)
        return

    if not (getattr(args, "no_release_stats", False) or getattr(args, "dry_run", False)):
        processing_release_stats(repo_path, repo_id)
        if kernel_repo and cve_enabled and not getattr(args, "no_cve", False):
            processing_cve_releases(repo_path, repo_id)

    if run_requested_backfills(args, network_id, repo_id):
        G.emergency_shutdown(0)
        return
    if not getattr(args, "dry_run", False):
        run_automatic_backfills(args, repo_id=repo_id, repo_path=repo_path)

    repair_mode = bool(getattr(args, "repair", False))
    backfill_target = getattr(args, "backfill", None)
    dry_run = bool(getattr(args, "dry_run", False))

    if repair_mode or backfill_target:
        canonical_tags = MF.get_version_tags(start_ref=target_refs[0] if target_refs else None, include_pre_releases=True)
        if role == distributed.ROLE_MASTER:
            # Delegated chains are the workers' to parse: a stable tag is no gap of the master's chain
            canonical_tags, _delegated = distributed.partition_master_refs(
                canonical_tags, canonical_tags, bool(getattr(args, "delegate_stable", True)))
        for ref in [r for r, (_cid, st) in version_states.items() if st == "legacy"]:
            _resolve_legacy_state(ref, version_states, network_id)
        orchestrate_repair_pipeline(
            canonical_tags,
            version_states,
            network_id,
            repo_id,
            repo_path,
            backfill_target=backfill_target,
            dry_run=dry_run,
        )
        if backfill_target or dry_run:
            G.emergency_shutdown(0)
            return
        with G.DB() as db:
            version_states = ingest_state.fetch_repo_version_states(db, repo_id, network_id)

    if is_branch_pipeline:
        unparsed_branches = [ref for ref in target_refs if version_states.get(ref, (0, "new"))[1] != "done"]
        if not unparsed_branches:
            logger.info(COLOR.green(f"[Branch Pipeline] All {len(target_refs)} branch(es) already completed in DB. Nothing to parse."))
            G.emergency_shutdown(0)
            return
        target_refs = unparsed_branches
    else:
        # Fast-Forward Contiguous Prefix Scan
        resume_idx = 0
        for idx, ref in enumerate(target_refs):
            st = version_states.get(ref, (0, "new"))[1]
            if st == "legacy":
                st = _resolve_legacy_state(ref, version_states, network_id)
                if st == "done" and role == distributed.ROLE_MASTER:
                    resolved_cid = version_states[ref][0]
                    now_done = int(time.time())
                    with G.DB() as db:
                        distributed.unlock_dependent_tasks(db, network_id, resolved_cid, base_ref_name=ref)
                        if resolved_cid > 0:
                            db.update_rows(
                                "distributed_tasks",
                                {"base_commit_id": int(resolved_cid), "updated_ts": now_done},
                                {"network_id": network_id, "status": "pending", "base_ref_name": ref, "base_commit_id": 0},
                            )
                            db.update_rows(
                                "distributed_tasks",
                                {"status": distributed.DIST_STATUS_COMPLETED, "commit_id": int(resolved_cid), "updated_ts": now_done},
                                {"network_id": network_id, "repo_name": master_repo_name, "ref_name": ref, "task_type": ["main", "stable"]},
                            )
            if st == "done":
                resume_idx = idx + 1
            else:
                break

        if resume_idx == len(target_refs):
            logger.info(COLOR.green(f"[Release Pipeline] All {len(target_refs)} version(s) already completed in DB. Nothing to parse."))
            G.emergency_shutdown(0)
            return

        if resume_idx > 0:
            last_ref = target_refs[resume_idx - 1]
            last_cid = version_states[last_ref][0]
            gp.Old_VID = last_cid
            gp.Old_Version_Name = last_ref
            logger.info(COLOR.cyan(
                f"[Release Pipeline] Found {resume_idx} completed version(s) ({target_refs[0]} ... {last_ref}). "
                f"Resuming from version {resume_idx + 1}/{len(target_refs)} ('{target_refs[resume_idx]}') "
                f"using prior base '{last_ref}' (commit_id {last_cid})."
            ))

            resume_ref = target_refs[resume_idx]
            r_cid, r_st = version_states.get(resume_ref, (0, "new"))
            if r_st == "interrupted" and r_cid > 0:
                with G.DB() as db:
                    if db is not None:
                        ingest_state.clean_interrupted_version(db, network_id, r_cid, TABLES)
                _forget_rolled_back_rows()
                logger.warning(COLOR.yellow(
                    f"[Release Pipeline] Detected interrupted version '{resume_ref}' (commit_id {r_cid}). Rolled back partial database rows."
                ))
            elif r_st == "legacy_partial" and r_cid > 0:
                with G.DB() as db:
                    _purge_partial_version(db, r_cid)
                _forget_rolled_back_rows()
                logger.warning(COLOR.yellow(
                    f"[Release Pipeline] Detected incomplete legacy version '{resume_ref}' (commit_id {r_cid}). Purged partial records."
                ))

            target_refs = target_refs[resume_idx:]

    if dry_run:
        logger.info(COLOR.cyan(f"[Release Pipeline - Dry Run] Would resume parsing at '{target_refs[0]}'. Halting without making changes."))
        G.emergency_shutdown(0)
        return

    # Interactive terminals: pressing 'q' finishes the version being parsed, then quits
    current = {"ref": None}
    quit_listener = GracefulQuitListener(
        on_request=lambda: logger.warning(COLOR.yellow(
            f"[Quit Requested] Finishing '{current['ref']}' before stopping; remaining versions are skipped. "
            "Press Ctrl+C to abort immediately."
        ))
    )
    if quit_listener.start():
        logger.info(COLOR.cyan("[Release Pipeline] Press 'q' at any time to finish the current version and quit."))

    try:
        use_master_queue = (
            role == distributed.ROLE_MASTER
            and not is_branch_pipeline
            and not bool(getattr(args, "ref", None))
            and _is_git_repo_dir(repo_path)
        )
        if use_master_queue:
            master_scheduler = DistributedTaskScheduler(
                G.DB,
                network_id,
                node_id,
                role,
                heartbeat_timeout=lease_ttl,
                repo_name=master_repo_name,
                min_priority=distributed.PRIORITY_MAIN,
            )
            claimed_count = 0
            refreshed_caps = False
            released_missing: set[int] = set()
            while not G.is_deadline_exceeded() and not quit_listener.requested.is_set():
                pending_main = 0
                with G.DB() as db:
                    task = master_scheduler.claim_next(db)
                    if task is None:
                        pending_main = db.count_rows(
                            "distributed_tasks",
                            {
                                "network_id": network_id,
                                "repo_name": master_repo_name,
                                "status": "pending",
                                "priority": ge(distributed.PRIORITY_MAIN),
                            },
                        )
                if task is None and pending_main > 0 and not refreshed_caps:
                    refreshed_caps = True
                    if auto_git_sync:
                        sync_git_repository(repo_path, raise_on_error=False)
                    with G.DB() as db:
                        _register_node_capabilities(
                            db, node_id, network_id, role, repo_entries, repo_path, cap_include_rc,
                            repo_meta_by_name=repo_meta_by_name,
                        )
                        task = master_scheduler.claim_next(db)
                if task is None:
                    if pending_main > 0:
                        logger.warning(COLOR.yellow(
                            f"[Master] Stopping after {claimed_count} task(s): {pending_main} pending "
                            f"priority>={distributed.PRIORITY_MAIN} task(s) remain in distributed_tasks "
                            f"for '{master_repo_name}' with unmatched capabilities or unmet base dependencies."
                        ))
                    break

                claimed_count += 1
                refreshed_caps = False
                t_id = int(task["task_id"])
                ref = str(task["ref_name"])
                cid = int(task.get("commit_id") or 0)
                if not verify_local_ref_exists(repo_path, ref):
                    logger.warning(COLOR.yellow(
                        f"[Master] Ref '{ref}' not found locally in '{repo_path}'; releasing task {t_id}."
                    ))
                    with G.DB() as db:
                        master_scheduler.release(t_id, f"Ref '{ref}' missing in local mirror", db=db)
                    if auto_git_sync:
                        sync_git_repository(repo_path, raise_on_error=False)
                    with G.DB() as db:
                        _register_node_capabilities(
                            db, node_id, network_id, role, repo_entries, repo_path, cap_include_rc,
                            repo_meta_by_name=repo_meta_by_name,
                        )
                    if t_id in released_missing:
                        logger.error(COLOR.red(
                            f"[Master] Stopping: task {t_id} ('{ref}') still missing locally after capability refresh."
                        ))
                        break
                    released_missing.add(t_id)
                    continue

                b_name = str(task.get("base_ref_name") or "")
                with G.DB() as db:
                    b_cid = _resolve_base_commit_id_for_task(
                        db, network_id, repo_id, master_repo_name, b_name, task.get("base_commit_id")
                    )
                gp.VID = 0
                gp.Old_VID = b_cid if (b_name and b_cid > 0) else 0
                gp.Old_Version_Name = b_name if (b_name and b_cid > 0) else EMPTY_TREE
                current["ref"] = ref
                gp._active_dist_task_id = t_id
                if (ref.startswith("1da177") or ref in ("v2.6.12-rc2", "2.6.12-rc2")) and (
                    gp.Old_VID == 0 or not gp.Old_Version_Name or gp.Old_Version_Name == EMPTY_TREE
                ):
                    for hist_ref in ("v2.6.12-rc1", "history/master"):
                        if hist_ref in version_states and version_states[hist_ref][1] == "done":
                            gp.Old_VID = version_states[hist_ref][0]
                            gp.Old_Version_Name = hist_ref
                            logger.info(
                                f"[Mainline Bridge] Linking initial git commit '{ref}' incrementally to completed history tip '{hist_ref}' (commit_id {gp.Old_VID})."
                            )
                            break
                if heartbeat_daemon is not None:
                    heartbeat_daemon.set_active(cid, dist_task_id=t_id)
                logger.info(COLOR.cyan(
                    f"[Master] Claimed priority-{task.get('priority', 10)} task {t_id}: '{ref}' "
                    f"(base: '{gp.Old_Version_Name}', base_cid: {gp.Old_VID})."
                ))
                try:
                    completed = update(ref, repo_path=repo_path)
                    if completed is False:
                        with G.DB() as db:
                            _rollback_and_clean_ingest_state(db, network_id, getattr(gp, "VID", 0))
                            master_scheduler.release(t_id, "Execution deadline reached", db=db)
                        break
                    master_scheduler.complete(t_id, getattr(gp, "VID", cid), ref_name=ref)
                except SystemicParseError as exc:
                    with G.DB() as db:
                        _rollback_and_clean_ingest_state(db, network_id, getattr(gp, "VID", 0))
                        master_scheduler.release(t_id, f"Released by {node_id}: {exc}", db=db)
                    raise
                except Exception as exc:
                    with G.DB() as db:
                        _rollback_and_clean_ingest_state(db, network_id, getattr(gp, "VID", 0))
                        master_scheduler.fail(t_id, str(exc), db=db)
                    raise
                finally:
                    gp._active_dist_task_id = None
                    if heartbeat_daemon is not None:
                        heartbeat_daemon.clear_active()
        else:
            total_targets = len(target_refs)
            for ref_idx, ref in enumerate(target_refs):
                if G.is_deadline_exceeded():
                    logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached before starting next reference."))
                    break
                if quit_listener.requested.is_set():
                    logger.warning(COLOR.yellow(
                        f"[Quit Requested] Stopping before '{ref}' ({total_targets - ref_idx} version(s) not parsed)."
                    ))
                    break
                if is_branch_pipeline:
                    gp.Old_VID = 0
                    gp.Old_Version_Name = ""
                # Bridge initial git commit (1da177 / v2.6.12-rc2) from history tip (v2.6.12-rc1)
                if (ref.startswith("1da177") or ref in ("v2.6.12-rc2", "2.6.12-rc2")) and (gp.Old_VID == 0 or not gp.Old_Version_Name):
                    for hist_ref in ("v2.6.12-rc1", "history/master"):
                        if hist_ref in version_states and version_states[hist_ref][1] == "done":
                            gp.Old_VID = version_states[hist_ref][0]
                            gp.Old_Version_Name = hist_ref
                            logger.info(
                                f"[Mainline Bridge] Linking initial git commit '{ref}' incrementally to completed history tip '{hist_ref}' (commit_id {gp.Old_VID})."
                            )
                            break
                current["ref"] = ref
                if total_targets > 1:
                    label = "branch" if is_branch_pipeline else "version"
                    logger.info(COLOR.cyan(f"[Release Pipeline] Progress: {label} {ref_idx + 1}/{total_targets} ('{ref}')"))
                try:
                    completed = update(ref, repo_path=repo_path)
                    if completed is False:
                        break
                finally:
                    if heartbeat_daemon is not None:
                        heartbeat_daemon.clear_active()
    except MemoryError:
        logger.critical(COLOR.red("FATAL: Memory limit of 60.0 GB (RLIMIT_AS) exceeded! Terminating execution cleanly."))
        sys.exit(1)
    except SystemicParseError:
        logger.critical(COLOR.red(
            f"[Release Pipeline] Stopped at '{current['ref']}'; its partial rows are rolled back on the next start. "
            "Fix the cause above, then run again to resume."
        ))
        sys.exit(1)
    finally:
        quit_listener.stop()
        _shutdown_node()
        if getattr(G, "STAGE_PROFILER", None) is not None:
            print(G.STAGE_PROFILER.format_stage_report())

    # Again after the versions: a run that recovered an interrupted version skipped it at the start,
    # and tags fetched while it ran are picked up (nothing is read when every tag is summarized)
    if not getattr(args, "no_release_stats", False):
        processing_release_stats(repo_path, repo_id)
        if kernel_repo and cve_enabled and not getattr(args, "no_cve", False):
            processing_cve_releases(repo_path, repo_id)

    logger.info("We are done! Closing")
    G.emergency_shutdown(0)
    return


if __name__ == "__main__":
    main()
