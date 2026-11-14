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
from core.config import (
    get_parser_config,
    get_ssh_tunnel_config,
    get_mailing_list_config,
    get_repository,
    get_default_repo_path,
    resolve_repo_path,
    is_kernel_project,
    repository_project,
)
from core.ssh_tunnel import start_ssh_tunnel
import contextlib
import sys

import time
from core.DBLayout import (
    TABLES,
    m_v_main,
)
from pipeline.backfill_steps import run_automatic_backfills, run_requested_backfills
from pipeline.cli import arg_handling
from pipeline.db_setup import drop_familyless_blob_facts, drop_legacy_symbol_tables, drop_obsolete_kconfig_tables, drop_outdated_derived_tables, ensure_db_instance, ensure_db_views, ensure_declared_indexes, outdated_layout
from pipeline.external_steps import processing_cve_list, processing_release_stats
from pipeline.git_ingest import _forget_rolled_back_rows, _purge_partial_version, _resolve_legacy_state, refresh_person_identities
from pipeline.git_network import repo_project_slug, resolve_git_network_and_repo
from pipeline.mailing_list_steps import standalone_resolve_mailing_list
from pipeline.multicore import _apply_db_server_tuning, _preflight_libclang
from pipeline.repair import orchestrate_repair_pipeline
from pipeline.state import MF, gp, logger
from pipeline.systemic import SystemicParseError
from pipeline.update import update




def _shutdown_node() -> None:
    """Stop the heartbeat and remove this node from m_parse_node. Idempotent and never raises: it runs on
    every way out of `main()` (also from `G.emergency_shutdown()`, before the SSH tunnel stops)."""
    daemon = getattr(G, "heartbeat_daemon", None)
    node_id = getattr(G, "node_id", None)
    G.heartbeat_daemon = None
    G.node_id = None
    if daemon is not None:
        with contextlib.suppress(Exception):
            daemon.stop()
    if node_id:
        try:
            with G.DB() as db:
                distributed.unregister_node(db, node_id)
        except BaseException as e:  # noqa: BLE001 - e.g. Ctrl+C also stopped the SSH tunnel
            logger.warning(COLOR.yellow(f"[Distributed] Node '{node_id}' stays registered until its heartbeat expires: {e}"))


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
    is_distributed = role in (distributed.ROLE_MASTER, distributed.ROLE_WORKER)
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
            if role != distributed.ROLE_WORKER:
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
            if role != distributed.ROLE_WORKER:
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
        kernel_repo = is_kernel_project(repo_project_slug(repo_path, [MF.get_root_commit() or ""]))
    else:
        kernel_repo = is_kernel_project(repository_project(repo_entry))
    if not kernel_repo and getattr(args, "cve", False):
        logger.warning(COLOR.yellow(f"[CVE] '{repo_path}' is not a kernel repository; --cve has nothing to resolve in it."))
    if kernel_repo and role != distributed.ROLE_WORKER and (getattr(args, "cve", False) or not getattr(args, "no_cve", False)):
        _cve_net, cve_repo_id = resolve_git_network_and_repo(repo_path)
        summary = processing_cve_list(force=bool(getattr(args, "cve_full", False)), kernel_repo=repo_path, repo_id=cve_repo_id)
        if summary and summary.get("state") == "current":
            logger.info(f"[CVE] The CVE list is current ({summary.get('records')} kernel records, HEAD {str(summary.get('head'))[:12]}).")
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

    if not (getattr(args, "release_stats", False) or getattr(args, "dry_run", False) or getattr(args, "backfill", None)):
        # Before registering as a node or leasing a task
        _preflight_libclang()

    node_id = None
    heartbeat_daemon = None
    lease_ttl, heartbeat_interval = distributed.resolve_lease_settings(get_parser_config().get("distributed"))
    if is_distributed:
        node_id = distributed.get_default_node_id(role, getattr(args, "worker_id", None))
        G.node_id = node_id
        with G.DB() as db:
            distributed.register_node(db, node_id, role, network_id)
        heartbeat_daemon = distributed.NodeHeartbeatDaemon(
            G.DB, node_id, interval=heartbeat_interval, lease_ttl=lease_ttl, role=role, network_id=network_id
        )
        heartbeat_daemon.start()
        G.heartbeat_daemon = heartbeat_daemon
        if _shutdown_node not in G.shutdown_hooks:
            G.shutdown_hooks.append(_shutdown_node)
    else:
        G.node_id = None
        G.heartbeat_daemon = None

    if role == distributed.ROLE_WORKER:
        current = {"ref": None}
        quit_listener = GracefulQuitListener(
            on_request=lambda: logger.warning(COLOR.yellow(
                f"[Quit Requested] Finishing '{current['ref']}' before stopping. Press Ctrl+C to abort immediately."
            ))
        )
        quit_listener.start()
        logger.info(COLOR.cyan(f"[Worker] Starting secondary worker parser loop (node: '{node_id}')..."))
        preferred_chain = None
        gp._active_task_id = None
        worker_exit_code = 0
        try:
            while not G.is_deadline_exceeded() and not quit_listener.requested.is_set():
                with G.DB() as db:
                    distributed.reclaim_expired_leases(db, network_id, lease_ttl=lease_ttl)
                    task = distributed.claim_next_task(
                        db, network_id, node_id, preferred_chain=preferred_chain, lease_ttl=lease_ttl
                    )

                if task is not None:
                    t_id = task["task_id"]
                    ref_name = task["ref_name"]
                    b_name = task["base_ref_name"]
                    b_cid = task["base_commit_id"]
                    cid = task["commit_id"]
                    preferred_chain = task["chain_name"]
                    gp._active_task_id = t_id
                    heartbeat_daemon.set_active(cid, task_id=t_id)
                    current["ref"] = ref_name
                    gp.Old_VID = b_cid
                    gp.Old_Version_Name = b_name
                    logger.info(COLOR.green(
                        f"[Worker] Processing assigned task {t_id} ('{ref_name}', chain: '{preferred_chain}', base: '{b_name}', commit_id: {cid})."
                    ))
                    try:
                        update(ref_name, repo_path=repo_path)
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
                                if getattr(gp, "VID", 0):
                                    ingest_state.rollback_version(db, network_id, gp.VID, gp.Table_Array)
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
                                if getattr(gp, "VID", 0):
                                    ingest_state.rollback_version(db, network_id, gp.VID, gp.Table_Array)
                                distributed.mark_task_failed(db, t_id, str(e), worker_id=node_id)
                            else:
                                logger.warning(COLOR.yellow(f"[Worker] Lease for task {t_id} was lost/revoked; skipping rollback to avoid corrupting peer data."))
                    finally:
                        gp._active_task_id = None
                        heartbeat_daemon.clear_active()
                else:
                    preferred_chain = None
                    if getattr(args, "worker_once", False):
                        logger.info("[Worker] No eligible tasks found and --worker-once is set. Exiting.")
                        break
                    with G.DB() as db:
                        finished = distributed.is_parsing_finished(db, network_id)
                    if finished:
                        logger.info(COLOR.green("[Worker] All parsing completed across repository. Exiting cleanly."))
                        break
                    logger.info("[Worker] Queue is currently empty. Waiting for more versions from Master...")
                    time.sleep(5.0)
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
            distributed.publish_delegable_chains(
                db,
                network_id,
                repo_id,
                all_tags,
                version_states,
                delegate_stable=delegate_stable,
                delegate_branches=branch_refs,
                repo_dir=repo_path,
            )
        # The master keeps what is not published: mainline release tags, trunk branches and refs that are no
        # release tag (core/distributed.py partition_master_refs)
        target_refs, delegated_refs = distributed.partition_master_refs(target_refs, all_tags, delegate_stable, branch_refs)
        logger.info(COLOR.cyan(
            f"[Master] Parsing {len(target_refs)} mainline/trunk ref(s); {len(delegated_refs)} stable/next/branch "
            f"ref(s) of this run are published as tasks for secondary workers."
        ))

    # Release statistics of every release tag (core/API.md section 10); --release-stats does only this
    if getattr(args, "release_stats", False) or not (getattr(args, "no_release_stats", False) or getattr(args, "dry_run", False)):
        processing_release_stats(repo_path, repo_id)
    if getattr(args, "release_stats", False):
        G.emergency_shutdown(0)
        return

    if run_requested_backfills(args, network_id, repo_id):
        G.emergency_shutdown(0)
        return
    if not getattr(args, "dry_run", False):
        run_automatic_backfills(args)

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
            update(ref, repo_path=repo_path)
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

    logger.info("We are done! Closing")
    G.emergency_shutdown(0)
    return


if __name__ == "__main__":
    main()
