"""Command-line arguments (`arg_handling`)."""
from core.globalstuff import (
    G,
    COLOR,
    setup_memory_limit,
)
from core.config import (
    find_config_path,
    init_config,
    get_config,
    sync_environ,
    get_parser_config,
    get_db_config,
    get_ssh_tunnel_config,
    get_mailing_list_config,
    get_workspace_config,
    sync_repositories,
    normalize_memory_mode,
)
from core.ssh_tunnel import start_ssh_tunnel
from core.Profiler import parse_duration, PipelineStageProfiler
import os
import sys

import argparse
from db_engine import (
    available_engines,
    get_db_engine,
)
from table_engine import TECachedDB, get_table_engine
from pipeline.db_setup import ensure_db_instance
from pipeline.multicore import _resolve_mem_max
from pipeline.state import gp, logger




def arg_handling() -> argparse.Namespace:
    """Handle arguments passed with python."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c", "--config",
        dest="config",
        default=None,
        help="Path to JSON configuration file",
    )
    parser.add_argument(
        "--configure", "--config-tui",
        dest="configure",
        action="store_true",
        default=False,
        help="Launch the interactive Terminal User Interface (TUI) to configure config.json",
    )
    parser.add_argument(
        "--repo", "--repository",
        dest="repo",
        default=None,
        help="Configured repository name or local repository path (default: the default repository in config 'repositories')",
    )
    parser.add_argument(
        "--detect-repos",
        dest="detect_repos",
        action="store_true",
        help="Detect Git repositories under workspace.repos_dir, register them in the config file, print them and exit",
    )
    parser.add_argument(
        "-b", "--branches", "--all-branches",
        dest="branches",
        action="store_true",
        default=False,
        help="Discover and parse the tip commit of all branches matching the repository's branch_filter, updating config.json",
    )
    parser.add_argument(
        "-v", "--ref", "--version",
        dest="ref",
        nargs="*",
        default=None,
        help="Target Git reference(s), tag(s), branch(es), or starting commit to parse (default: the repository's first commit, continuing through subsequent releases)",
    )
    parser.add_argument(
        "-s", "--single-version", "--single",
        dest="single_version",
        action="store_true",
        default=None,
        help="Stop after parsing the first/specified version instead of continuing through subsequent releases (default: False)",
    )
    parser.add_argument(
        "--rc", "--include-rc", "--include-pre-releases",
        dest="include_pre_releases",
        action="store_true",
        default=None,
        help="Include release candidate tags (-rc#) and pre-releases during continuous version discovery (default)",
    )
    parser.add_argument(
        "--no-rc", "--releases-only",
        dest="include_pre_releases",
        action="store_false",
        help="Skip release candidate tags (-rc#) and pre-releases; parse final releases only",
    )
    parser.add_argument(
        "--project",
        dest="project",
        default="linux",
        help="Project ecosystem slug/name (default: 'linux')",
    )
    parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=None,
        help="Optional maximum commit count to ingest in Git DAG history",
    )
    parser.add_argument(
        "-r", "--reset", "--reset-db",
        dest="reset",
        action="store_true",
        help="Reset and recreate all database tables from scratch",
    )
    parser.add_argument(
        "-D", "--Drop",
        help="Drop all tables", action="store_true",
    )
    parser.add_argument(
        "-C", "--Create-Tables",
        help="Generate all tables", action="store_true",
    )
    parser.add_argument(
        "-u", "--unit-test", "--test-unit",
        dest="unit_test",
        nargs="?",
        const="",
        default=None,
        help="Run all tests in the testing suite (optionally specify a single file to test C-AST)",
    )
    unit = parser.add_argument_group("unit-test options (with -u)")
    unit.add_argument("--modules", nargs="+", metavar="MODULE", default=None,
                      help="Run only these test modules (tests.test_x, tests/test_x.py or test_x)")
    unit.add_argument("--changed", action="store_true",
                      help="Run only test modules affected by changed files (.agents/scripts/affected_tests.py)")
    unit.add_argument("--base", metavar="REF", default=None,
                      help="With --changed: diff the working tree against REF (default HEAD, untracked included)")
    unit.add_argument("--summary", action="store_true",
                      help="Compact output: one line per failing/erroring test plus totals; full log path printed")
    unit.add_argument("--no-live-db", dest="no_live_db", action="store_true",
                      help="Skip tests that need a live MariaDB (tests.db_fixtures.requires_live_db)")
    unit.add_argument("--timeout", type=int, default=None, metavar="S",
                      help="Per-module timeout in seconds, runs each module in a subprocess (default 300)")
    unit.add_argument("--isolate", action="store_true",
                      help="Run each test module in its own subprocess with a timeout")
    parser.add_argument(
        "-T", "--Test",
        help="Test/Parse a specific file",
    )
    parser.add_argument(
        "-p", "--profile",
        action="store_true",
        help="Enable granular stage timing profiler across tests and update loop cycles",
    )
    parser.add_argument(
        "--duration",
        dest="duration",
        default=None,
        help="Maximum execution duration before graceful halt and profiling report (e.g., '30m', '1800s', '1h')",
    )
    parser.add_argument(
        "--clean-temp",
        dest="clean_temp",
        action="store_true",
        help="Purge orphaned /dev/shm/code-parser.* scratch directories left by prior crashed processes",
    )
    parser.add_argument(
        "--repair",
        dest="repair",
        action="store_true",
        help="Scan version sequence, backfill missing/interrupted versions in topological order, and re-align downstream differential links",
    )
    parser.add_argument(
        "--backfill",
        dest="backfill",
        default=None,
        help="Target a specific version for backfill, followed by re-aligning its immediate downstream successor",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Preview version discovery, gap detection, and planned repair actions without modifying the database",
    )
    parser.add_argument(
        "-l", "--low-mem", "--low-memory",
        dest="low_mem",
        action="store_true",
        help="Enable Low Memory Mode: utilizes all CPU cores with reduced batch sizes and intermediate chunk commits",
    )
    parser.add_argument(
        "-vl", "--very-low-mem", "--very-low-memory",
        dest="very_low_mem",
        action="store_true",
        help="Enable Very Low Memory Mode: throttles CPU cores (<=2) with ultra-compact batch sizes and continuous memory compaction",
    )
    parser.add_argument(
        "--mm", "--mem-max",
        dest="mem_max",
        default=None,
        type=float,
        help="Set memory max limit in GB (0 disables the memory guard). Defaults to parser.mem_max, else 60GB",
    )
    parser.add_argument(
        "--db", "--db-engine",
        dest="db_engine",
        default=None,
        choices=list(available_engines(include_aliases=True)),
        help="Select database backend engine (default: from config or mariadb)",
    )
    parser.add_argument(
        "--db-socket",
        dest="db_socket",
        default=None,
        help="Path to the database server's Unix domain socket for local non-tunneled connections (or 'off'/'disabled' to force TCP)",
    )
    parser.add_argument(
        "--te", "--table-engine",
        dest="table_engine",
        default=None,
        choices=["cached", "direct", "tecacheddb", "tedirectdb"],
        help="Select Table Engine architecture backend (default: from config or cached)",
    )
    parser.add_argument(
        "--hugepages",
        dest="hugepages",
        default=None,
        choices=["auto", "1g", "2m", "thp", "off"],
        help="Configure huge-page backing for TableEngine shared memory (default: auto)",
    )
    parser.add_argument(
        "--no-hugepages",
        dest="hugepages",
        action="store_const",
        const="off",
        help="Disable huge-page shared memory backing in TableEngine",
    )
    parser.add_argument(
        "--cache-max-table-mb",
        dest="cache_max_table_mb",
        type=int,
        default=None,
        help="Ceiling (MB) on the size of one TE-cached table; a bigger one is read straight from the "
             "database instead of being cached (0: no ceiling, default: from config)",
    )
    parser.add_argument(
        "--ssh-tunnel",
        dest="ssh_tunnel",
        action="store_true",
        default=None,
        help="Enable SSH port forwarding tunnel to the remote database server",
    )
    parser.add_argument(
        "--no-ssh-tunnel",
        dest="ssh_tunnel",
        action="store_false",
        help="Disable SSH port forwarding tunnel to the remote database server",
    )
    parser.add_argument(
        "--ssh-host",
        dest="ssh_host",
        default=None,
        help="Remote SSH server hostname or IP address",
    )
    parser.add_argument(
        "--ssh-port",
        dest="ssh_port",
        type=int,
        default=None,
        help="Remote SSH server port (default: 22)",
    )
    parser.add_argument(
        "--ssh-user",
        dest="ssh_user",
        default=None,
        help="Username for SSH authentication",
    )
    parser.add_argument(
        "--ssh-password",
        dest="ssh_password",
        default=None,
        help="Password for SSH authentication",
    )
    parser.add_argument(
        "--ssh-key-file",
        dest="ssh_key_file",
        default=None,
        help="Path to SSH private key file",
    )
    parser.add_argument(
        "--ssh-remote-host",
        dest="ssh_remote_host",
        default=None,
        help="Target database host from remote server perspective (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--ssh-remote-port",
        dest="ssh_remote_port",
        type=int,
        default=None,
        help="Target database port on remote server (default: 3306)",
    )
    parser.add_argument(
        "--ssh-local-port",
        dest="ssh_local_port",
        type=int,
        default=None,
        help="Local port to bind SSH forward to (default: 0 for dynamic ephemeral port)",
    )
    parser.add_argument(
        "--ssh-strict-host-key-checking",
        dest="ssh_strict_host_key_checking",
        choices=["accept-new", "no", "yes"],
        default=None,
        help="SSH host key verification mode (default: accept-new)",
    )
    parser.add_argument(
        "--cve",
        dest="cve",
        action="store_true",
        help="Only ingest the CVE list (config cve.repo_dir) into m_cve / m_cve_commit, then exit",
    )
    parser.add_argument(
        "--cve-full",
        dest="cve_full",
        action="store_true",
        help="Ingest the whole CVE list again instead of the records changed since the last ingestion",
    )
    parser.add_argument(
        "--no-cve",
        dest="no_cve",
        action="store_true",
        help="Do not update the CVE list at the start of this run",
    )
    parser.add_argument(
        "--release-stats",
        dest="release_stats",
        action="store_true",
        help="Only summarize the release tags of the repository into m_release_segment / m_release_person / "
             "m_person_first_release (core/API.md section 10), then exit",
    )
    parser.add_argument(
        "--no-release-stats",
        dest="no_release_stats",
        action="store_true",
        help="Do not update the release statistics in this run",
    )
    # One-off refills of derived tables for versions ingested before they existed (core/backfills.py)
    parser.add_argument(
        "--backfill-maintainer-files",
        dest="backfill_maintainer_files",
        action="store_true",
        help="Refill m_maintainer_file (files of each MAINTAINERS section version) for the finished versions "
             "of the repository, then exit (core/API.md section 4, row 19)",
    )
    parser.add_argument(
        "--backfill-kbuild",
        dest="backfill_kbuild",
        action="store_true",
        help="Fill m_kbuild_symbol (Kbuild rules with their CONFIG_ symbols) for every Makefile/Kbuild (path, blob) "
             "pair of the database without rows, then exit (core/API.md section 12)",
    )
    parser.add_argument(
        "--from",
        dest="backfill_from",
        default=None,
        metavar="REF",
        help="With --backfill-maintainer-files: resume at this finished version",
    )
    parser.add_argument(
        "--backfill-facts",
        dest="backfill_facts",
        action="store_true",
        help="Fill m_blob_facts (per-blob facts, families 1-6) for the blobs of every finished version that lack "
             "them, then exit (core/API.md section 12)",
    )
    parser.add_argument(
        "--families",
        dest="facts_families",
        type=lambda v: [int(x) for x in v.split(",") if x.strip()],
        default=None,
        metavar="1,2,3,4,5,6",
        help="With --backfill-facts: only these fact families",
    )
    parser.add_argument(
        "--backfill-commit-relations",
        dest="backfill_commit_relations",
        action="store_true",
        help="Parse the stored message of every commit into m_commit_relation (Fixes:, reverts, backports, "
             "Cc: stable) and m_commit_link (Link:, Message-Id:), then exit (core/API.md section 12)",
    )
    parser.add_argument(
        "--from-commit-id",
        dest="from_commit_id",
        type=int,
        default=None,
        metavar="N",
        help="With --backfill-commit-relations: start after this commit_id (default: resume where it stopped)",
    )
    parser.add_argument(
        "--repair-head-commit-files",
        dest="repair_head_commit_files",
        action="store_true",
        help="Delete the m_commit_file rows of version head commits naming files the commit did not touch "
             "(written by Step 4.5 before 2026-11-07), then exit (core/API.md section 12)",
    )
    parser.add_argument(
        "--backfill-doc-references",
        dest="backfill_doc_references",
        action="store_true",
        help="Scan documentation files across finished versions and stage cross-file references into m_file_reference (core/API.md section 12)",
    )
    parser.add_argument(
        "--backfill-cve-files",
        dest="backfill_cve_files",
        action="store_true",
        help="Populate m_cve_file (affected source files per CVE) from parsed m_cve.data, then exit (core/API.md section 12)",
    )
    parser.add_argument(
        "--backfill-mailing-list",
        dest="backfill_mailing_list",
        action="store_true",
        help="Execute public-inbox mailing list resolution across commit range into m_commit_email, then exit (core/API.md section 12)",
    )
    parser.add_argument(
        "--no-backfill",
        dest="no_backfill",
        action="store_true",
        help="Skip the automatic one-off backfills run when their table has just been created",
    )
    parser.add_argument(
        "--resolve-ml",
        dest="resolve_ml",
        action="store_true",
        help="Run standalone mailing list commit-to-email resolution",
    )
    parser.add_argument(
        "--ml-range",
        dest="ml_range",
        default=None,
        help="Git commit range for standalone mailing list resolution (e.g., 'v3.0..v3.1' or 'HEAD~100..HEAD')",
    )
    parser.add_argument(
        "--ml-storage-dir",
        dest="ml_storage_dir",
        default=None,
        help="Local or remote path to public-inbox storage directory (default: from config 'mailing_list.storage_dir')",
    )
    parser.add_argument(
        "--role",
        dest="role",
        choices=["standalone", "master", "worker"],
        default=None,
        help="Opt-in distributed parsing role: 'standalone' (default, single computer), 'master' (mainline only, coordinates tasks), or 'worker' (parses delegated stable/past branches)",
    )
    parser.add_argument(
        "--worker-id",
        dest="worker_id",
        default=None,
        help="Unique identifier for this parser node (default: <role>:<hostname>:<pid>)",
    )
    parser.add_argument(
        "--delegate-stable",
        dest="delegate_stable",
        action="store_true",
        default=None,
        help="In master role, automatically delegate stable release chains (vX.Y.1..vX.Y.Z) to secondary workers",
    )
    parser.add_argument(
        "--no-delegate-stable",
        dest="delegate_stable",
        action="store_false",
        help="In master role, do not delegate stable releases",
    )
    parser.add_argument(
        "--delegate-branch",
        dest="delegate_branches",
        action="append",
        default=None,
        help="In master role, delegate specific branch(es) or past tag ranges to secondary workers",
    )
    parser.add_argument(
        "--worker-once",
        dest="worker_once",
        action="store_true",
        default=False,
        help="In worker role, exit when queue is empty instead of waiting for more versions from master",
    )
    args = parser.parse_args()
    if args.unit_test is None and (args.modules or args.changed or args.base or args.summary or args.no_live_db
                                   or args.timeout is not None or args.isolate):
        parser.error("--modules/--changed/--base/--summary/--no-live-db/--timeout/--isolate require -u")
    if args.base and not args.changed:
        parser.error("--base requires --changed")
    if args.modules and args.changed:
        parser.error("--modules and --changed are mutually exclusive")

    # Launch configuration TUI if requested or if no configuration file exists in an interactive session
    cfg_file = find_config_path(args.config)
    launch_tui = bool(getattr(args, "configure", False)) or (
        cfg_file is None
        and sys.stdin.isatty()
        and args.unit_test is None
        and not args.detect_repos
        and not getattr(args, "worker_once", False)
    )
    if launch_tui:
        from core.tui.app import run_config_tui_launch
        launch = run_config_tui_launch(args.config)
        if launch is None:
            sys.exit(0)
        # TUI run actions (parse selected tags, table reset) arrive as CLI argument overrides
        for key, value in launch.items():
            setattr(args, key, value)
        init_config(args.config)
    elif args.config is not None:
        init_config(args.config)

    # Register repositories found under workspace.repos_dir into the config file
    if args.detect_repos or get_workspace_config().get("auto_detect_repos", True):
        repos = sync_repositories(write=True)
        if args.detect_repos:
            if not repos:
                print("No repositories configured or detected.")
            for r in repos:
                branches_count = len(r.get("branches") or [])
                print(
                    f"{r.get('name', '?'):<16} {r.get('path', ''):<28} branch={r.get('default_branch', '')} "
                    f"branches={branches_count} start={str(r.get('start_ref', ''))[:12]} enabled={r.get('enabled', True)}"
                )
            sys.exit(0)

    parser_cfg = get_parser_config()

    # --mm wins over parser.mem_max; both fall back to 60 GB
    if args.mem_max is None:
        args.mem_max = _resolve_mem_max(parser_cfg.get("mem_max"))

    # Enforce memory limit early so re-exec (systemd-run) occurs before opening SSH tunnels or DB connections
    if args.mem_max > 0:
        setup_memory_limit(float(args.mem_max))
    dist_cfg = parser_cfg.get("distributed", {}) if isinstance(parser_cfg, dict) else {}
    if getattr(args, "role", None) is None:
        args.role = dist_cfg.get("role", "standalone") if dist_cfg.get("enabled", False) else "standalone"
    if getattr(args, "worker_id", None) is None:
        args.worker_id = dist_cfg.get("worker_id")
    if getattr(args, "delegate_stable", None) is None:
        args.delegate_stable = dist_cfg.get("delegate_stable", True)
    if getattr(args, "delegate_branches", None) is None:
        args.delegate_branches = dist_cfg.get("delegate_branches", [])
    db_cfg = get_db_config()
    ssh_cfg = get_ssh_tunnel_config()
    ml_cfg = get_mailing_list_config()

    if args.ml_storage_dir is not None:
        ml_cfg["storage_dir"] = args.ml_storage_dir

    if args.db_socket is not None:
        db_cfg["unix_socket"] = args.db_socket
        sync_environ(get_config())

    # Apply CLI argument overrides for SSH tunnel (CLI > ENV > JSON > Defaults)
    if args.ssh_tunnel is not None:
        ssh_cfg["enabled"] = args.ssh_tunnel
        ml_tunnel_cfg = get_ssh_tunnel_config("mailing_list")
        if ml_tunnel_cfg:
            ml_tunnel_cfg["enabled"] = args.ssh_tunnel
    if args.ssh_host is not None:
        ssh_cfg["host"] = args.ssh_host
    if args.ssh_port is not None:
        ssh_cfg["port"] = args.ssh_port
    if args.ssh_user is not None:
        ssh_cfg["user"] = args.ssh_user
    if args.ssh_password is not None:
        ssh_cfg["password"] = args.ssh_password
    if args.ssh_key_file is not None:
        ssh_cfg["key_file"] = args.ssh_key_file
    if args.ssh_remote_host is not None:
        ssh_cfg["remote_host"] = args.ssh_remote_host
    if args.ssh_remote_port is not None:
        ssh_cfg["remote_port"] = args.ssh_remote_port
    if args.ssh_local_port is not None:
        ssh_cfg["local_port"] = args.ssh_local_port
    if args.ssh_strict_host_key_checking is not None:
        ssh_cfg["strict_host_key_checking"] = args.ssh_strict_host_key_checking

    if ssh_cfg.get("enabled"):
        start_ssh_tunnel(ssh_cfg)
        # Re-fetch db_cfg since start_ssh_tunnel reroutes host and port
        db_cfg = get_db_config()

    if args.very_low_mem:
        G.MEMORY_MODE = "very_low"
    elif args.low_mem:
        G.MEMORY_MODE = "low"
    elif parser_cfg.get("memory_mode"):
        G.MEMORY_MODE = normalize_memory_mode(parser_cfg["memory_mode"])

    if getattr(args, "single_version", None) is None:
        args.single_version = parser_cfg.get("single_version", False)

    # --limit bounds how many commits ingest_git_dag() records per version range (None = all)
    gp.commit_limit = args.limit if args.limit and args.limit > 0 else None

    if getattr(args, "include_pre_releases", None) is None:
        args.include_pre_releases = parser_cfg.get("include_pre_releases", parser_cfg.get("rc", True))

    gp.init_cs_dict()

    if getattr(args, "duration", None):
        duration_s = parse_duration(args.duration)
        if duration_s is not None:
            G.set_execution_deadline(duration_s)
            G.PROFILING_ENABLED = True
            logger.info(COLOR.cyan(f"[Duration Limit] Configured maximum run duration of {args.duration} ({duration_s:.1f}s)"))

    if args.profile or G.PROFILING_ENABLED:
        G.PROFILING_ENABLED = True
        if getattr(G, "STAGE_PROFILER", None) is None:
            G.STAGE_PROFILER = PipelineStageProfiler()

    effective_db_engine = args.db_engine or db_cfg.get("engine") or "mariadb"
    effective_table_engine = args.table_engine or parser_cfg.get("table_engine") or "cached"
    effective_hugepages = args.hugepages or parser_cfg.get("hugepages") or "auto"
    effective_cache_max_mb = (
        args.cache_max_table_mb if args.cache_max_table_mb is not None else parser_cfg.get("cache_max_table_mb", 0)
    )
    G.SYMBOL_LAYOUT = "blob" if str(parser_cfg.get("symbol_layout", "tag")).lower() == "blob" else "tag"

    G.DB = get_db_engine(effective_db_engine)
    EngineCls = get_table_engine(effective_table_engine)
    if issubclass(EngineCls, TECachedDB):
        G.TE = EngineCls(hugepages=effective_hugepages, cache_max_table_mb=effective_cache_max_mb)
    else:
        G.TE = EngineCls()

    if args.Drop:
        logger.info("Dropping all tables")
        gp.drop_all()
    if args.Create_Tables:
        gp.create_table_all()
        G.emergency_shutdown(0)
    if args.unit_test is not None:
        try:
            with G.DB() as db:
                missing = db.test_tables(gp.Table_Array)
                if missing:
                    logger.info(f"Missing tables detected ({missing}), creating tables...")
                    missing_tables = [tbl for tbl in gp.Table_Array if tbl.table_name in missing]
                    db.create_table(missing_tables)
                ensure_db_instance(db)
        except Exception as e:
            logger.debug(f"DB check before unit tests: {e}")

        target = args.unit_test if args.unit_test != "" else None
        from tests.test_c_ast import run_c_ast_tests
        if target:
            code = run_c_ast_tests(target, profile=args.profile, table_engine=args.table_engine)
            sys.exit(code)

        from unit_test import runner as test_runner
        if args.no_live_db:
            os.environ[test_runner.NO_LIVE_DB_ENV] = "1"
        selective = bool(args.modules or args.changed)
        if selective or args.summary or args.isolate or args.timeout is not None:
            if args.modules:
                modules = [test_runner.normalize_module(m) for m in args.modules]
            elif args.changed:
                modules, full, why = test_runner.select_changed(args.base)
                if full:
                    print(COLOR.yellow(f"WARNING: global change ({why}); running the full suite."))
                    modules = list(test_runner.DEFAULT_MODULES)
                elif not modules:
                    print("No test modules affected by the changes.")
                    sys.exit(0)
                else:
                    print(f"{len(modules)} affected test module(s): {' '.join(modules)}")
            else:
                modules = list(test_runner.DEFAULT_MODULES)
            sys.exit(test_runner.run_isolated(
                modules, timeout=args.timeout or test_runner.DEFAULT_TIMEOUT, summary=args.summary,
                no_live_db=args.no_live_db,
            ))

        # 1. Multi-Core C-AST regression suite
        c_ast_code = run_c_ast_tests(None, profile=args.profile, table_engine=args.table_engine)

        # 2. Comprehensive Unittest Suite across all test modules
        print(COLOR.cyan("=========================================================================================="))
        print(COLOR.cyan("                        RUNNING COMPREHENSIVE UNIT TEST SUITE                             "))
        print(COLOR.cyan("=========================================================================================="))
        res = test_runner.run_in_process(test_runner.DEFAULT_MODULES)

        all_ok = (c_ast_code == 0) and res.wasSuccessful()
        if all_ok:
            print(COLOR.green(f"\n[+] ALL {res.testsRun} UNIT TESTS AND C-AST REGRESSIONS COMPLETED SUCCESSFULLY!"))
        else:
            print(COLOR.red("\n[-] TEST FAILURES DETECTED IN TEST SUITE."))
        sys.exit(0 if all_ok else 1)
    if args.Test:
        from tests.test_c_ast import run_c_ast_tests
        code = run_c_ast_tests(args.Test, profile=args.profile, table_engine=args.table_engine)
        sys.exit(code)

    return args
