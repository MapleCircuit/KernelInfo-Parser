"""main.py - Core Orchestration Loop, Table Definitions & Processing Workflows.

===============================================================================
SYSTEM ORCHESTRATION & PIPELINE ARCHITECTURAL GUIDE
===============================================================================
This module serves as the primary entry point and orchestrator for the parser.
It defines the database table schema array (`gp.Table_Array`), manages git version
lifecycles, coordinates multiprocessing parsing workers, and resolves queued
`ChangeSet` operations into database transactions.

1. DATABASE SCHEMA INITIALIZATION:
-------------------------------------------------------------------------------
  `gp.Table_Array` registers 15 core relational tables defining the schema:
    - Version & File tracking: `m_v_main`, `m_file_name`, `m_file`, `m_bridge_file`, `m_moved_file`
    - AST Schema: `m_type_descriptor`, `m_ast`, `m_ast_container`, `m_ast_include`, `m_ast_debug`, `m_ast_hash`
    - Version Tags & Spatial Coordinates: `m_tag`, `m_bridge_tag`, `m_map_ast`, `m_bridge_map`

2. VERSION PROCESSING PIPELINE (`update(version)`):
-------------------------------------------------------------------------------
  Step 1: Version Registration (`create_new_vid`)
          Registers new release tag in `m_v_main`.
  Step 2: Database Index Optimization (`create_index`)
          Creates temporary B-tree indexes (`ast_index`, `file_name_index`) for fast lookups.
  Step 3: Multiprocessing AST Parsing (`trigger_multicore`)
          Distributes `gp.Change_List` across `G.CPUS - 1` parallel worker processes.
          Each worker invokes `file_processing()`, runs `default_processing()` and `CS.parse()`,
          and returns picklable `ChangeSet` objects.
  Step 4: Unchanged File & Directory Propagation (`processing_unchanges`, `processing_dirs`)
          Propagates file and directory references from `gp.Old_VID` to `gp.VID` for
          unmodified files and directories.
  Step 5: ChangeSet Resolution Loop (`cs_queue.get()`)
          Iterates over queued `ChangeSet` instances in serial, calling `CS.execute()`.
          Unresolved references raise `REF_NOT_RESOLVABLE` and are requeued until satisfied.
  Step 6: Transaction Commit & Reset (`G.TE.commit`)
          Removes temporary indexes, commits database transactions, and purges RAMDISK.

3. DIFF OPERATION ROUTING (`default_processing(CS)`):
-------------------------------------------------------------------------------
  - `"A"` (Added): Inserts new `m_file_name`, `m_file` (vid_s=VID, s_stat='A'), and `m_bridge_file`.
  - `"M"` (Modified): Updates prior `m_file` (vid_e=Old_VID, e_stat='M') and creates new `m_file`.
  - `"R"` (Renamed): Updates prior `m_file` (e_stat='R'), creates new `m_file`, and inserts `m_moved_file`.
  - `"D"` (Deleted): Updates prior `m_file` (vid_e=Old_VID, e_stat='D').
===============================================================================
"""
from core.globalstuff import (
    G,
    COLOR,
    type_check,
    REF_ROOT,
    REF_OLD,
    REF_NOT_RESOLVABLE,
    CONTINUE_EXCEPTION,
    T_DIR,
    ASTT,
    configure_logging,
)
import os
import sys
import time
import logging
import argparse
import multiprocessing
from collections import deque
import pickle
import traceback
from parser.c_ast.c_ast import c_ast_parse
from parser.c_ast.c_ast_type import Line
from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from core.TableHandling import Table, ChangeSet
from db_engine import MariaDB, MockDB, get_db_engine
from table_engine import TEDirectDB, TECachedDB, get_table_engine
from core.DBLayout import (
    init_db_layout,
    m_v_main,
    m_file_name,
    m_file,
    m_bridge_file,
    m_moved_file,
    m_type_descriptor,
    m_ast,
    m_ast_container,
    m_ast_include,
    m_ast_debug,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_ast_hash,
    m_kconfig_symbol,
    m_kconfig_relation,
    m_kconfig_tree,
    m_kconfig_kbuild,
    m_maintainer_person,
    m_maintainer_section,
    m_maintainer_member,
    m_maintainer_pattern,
    m_maintainer_file,
    m_credits_entry,
    m_commit,
    m_bridge_commit_person,
    m_bridge_commit_file,
    m_bridge_commit_tag,
)


G.DB = MariaDB
G.TE = TECachedDB()
MF = MasterFile()
G.MF = MF
gp = GreatProcessor()
configure_logging(level=logging.INFO, fmt="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

init_db_layout(gp)


def update(version: str) -> None:
    """Execute the full version parsing and database ingestion pipeline for a target release version.
    
    Workflow Steps:
    1. Register new version string in `m_v_main` via `create_new_vid()`.
    2. Build temporary performance B-tree indexes (`ast_index`, `file_name_index`).
    3. Clone repository branch into RAMDISK via `MF.add_version()`.
    4. Generate git diff change list (`MF.generate_change_list()`) & start Table Engine cache.
    5. Spawn parallel worker processes (`trigger_multicore()`) to parse file diffs into ChangeSets.
    6. Enqueue all generated `ChangeSet` objects into `cs_queue` and resolve operations sequentially.
    7. Remove temporary indexes, commit table transactions (`G.TE.commit()`), and reset state.
    """
    # -------------------------------------------------------------------------
    # STEP 1: Register new version release in m_v_main (or skip if already exists)
    # -------------------------------------------------------------------------
    if create_new_vid(version):
        logger.info(COLOR.yellow(f"=======================Version '{version}' already exists in DB. Skipping======================="))
        return

    logger.info(COLOR.green(f"=======================Working on {version}======================="))

    # -------------------------------------------------------------------------
    # STEP 2: Ensure performance B-tree indexes are active for worker queries (in parallel)
    # -------------------------------------------------------------------------
    performance_indexes = (
        ("ast_index", m_ast, (m_ast.name, m_ast.type_id)),
        ("file_name_index", m_file_name, (m_file_name.fname,)),
        ("bridge_tag_fid_idx", m_bridge_tag, (m_bridge_tag.fid, m_bridge_tag.tag_id)),
    )
    with G.DB() as db:
        db.create_indexes(performance_indexes)

    # -------------------------------------------------------------------------
    # STEP 3: Clone repository branch version to RAMDISK workspace
    # -------------------------------------------------------------------------
    MF.add_version(version, gp.PURGE_LIST)

    # -------------------------------------------------------------------------
    # STEP 4: Generate git diff change list & initialize Table Engine cache
    # -------------------------------------------------------------------------
    MF.generate_change_list(gp)
    G.TE.start(gp.Table_Array, G.DB)

    # -------------------------------------------------------------------------
    # STEP 5: Spawn multicore workers to parse changed files in parallel
    # -------------------------------------------------------------------------
    trigger_multicore()

    # -------------------------------------------------------------------------
    # STEP 6: Enqueue ChangeSets and resolve operations sequentially
    # -------------------------------------------------------------------------
    G.TE.start_new_db(G.DB)
    cs_queue = deque(gp.ChangeSet_Dict.keys())

    max_loop = len(gp.ChangeSet_Dict) * G.OVERRIDE_FC_MAX_LOOP_EXEC_MULT
    while cs_queue:
        max_loop -= 1
        if max_loop < 0:
            logger.error(f"max loop ({len(gp.ChangeSet_Dict)*G.OVERRIDE_FC_MAX_LOOP_EXEC_MULT}) was brought to 0, printing queue:")
            debug_unresolved = [gp.ChangeSet_Dict[k] for k in cs_queue]

            G.BP_ON_REF_FAIL = True

            for item in debug_unresolved:
                item.execute()

            G.emergency_shutdown(666)
        current_cs = cs_queue.popleft()
        if not gp.ChangeSet_Dict[current_cs].execute():
            cs_queue.append(current_cs)

    # -------------------------------------------------------------------------
    # STEP 6.5: Parse Git Commits, Multi-Contributors & Bridge Tags to Commits
    # -------------------------------------------------------------------------
    processing_git_commits(version)

    # -------------------------------------------------------------------------
    # STEP 6.6: Parse Kbuild/Makefiles & Populate m_kconfig_kbuild
    # -------------------------------------------------------------------------
    processing_kbuild(version)

    # -------------------------------------------------------------------------
    # STEP 6.7: Batch Match Maintainer Sections & Populate m_maintainer_file
    # -------------------------------------------------------------------------
    processing_maintainer_files(version)

    # -------------------------------------------------------------------------
    # STEP 7: Drop secondary indexes, commit transactions, and rebuild indexes
    # -------------------------------------------------------------------------
    if getattr(G.TE, "db", None) is not None and hasattr(G.TE.db, "cnx") and G.TE.db.cnx is not None:
        try:
            G.TE.db.cnx.commit()
        except Exception:
            pass

    with G.DB() as db:
        db.remove_indexes(((item[0],item[1]) for item in performance_indexes))

    G.TE.commit_all()

    with G.DB() as db:
        db.create_indexes(performance_indexes)

    if G.PROFILING_ENABLED and gp.ChangeSet_Dict:
        from core.Profiler import format_profiling_report
        profilers = [cs.profiler for cs in gp.ChangeSet_Dict.values() if getattr(cs, "profiler", None)]
        if profilers:
            print(format_profiling_report(profilers, title=f"UPDATE CYCLE PROFILE: {version}"))

    gp.reset_cs()
    file_fid_cache.clear()
    MF.trim_version(keep=1)
    G.TE.close()
    import gc
    gc.collect()
    return


def trigger_multicore(batch_size: int = 200) -> None:
    """Distribute file parsing across `G.CPUS - 1` parallel worker processes in dynamic batches."""
    change_list = gp.Change_List or []
    total_files = len(change_list)
    total_batches = (total_files + batch_size - 1) // batch_size if total_files > 0 else 0

    num_workers = max(1, int(G.CPUS - 1))
    logger.info(
        f"Distributing {total_files} changed files in {total_batches} batches "
        f"(batch size: {batch_size}) across {num_workers} parallel workers"
    )

    # If no change within this version
    if total_files == 0:
        G.TE.start_new_db(G.DB)
        processing_dirs()
        processing_unchanges()
        return

    task_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    error_queue = multiprocessing.Queue()

    for i in range(0, total_files, batch_size):
        task_queue.put((i // batch_size, change_list[i : i + batch_size]))

    for _ in range(num_workers):
        task_queue.put(None)  # Sentinel to terminate each worker

    processes = []
    for worker_id in range(num_workers):
        p = multiprocessing.Process(
            target=file_processing_worker,
            args=(task_queue, error_queue, result_queue, gp.VID, gp, MF, worker_id),
        )
        processes.append(p)
        p.start()

    G.TE.start_new_db(G.DB)
    # needs to be try: protected
    processing_dirs()
    processing_unchanges()

    finished_workers = 0
    while finished_workers < num_workers:
        try:
            item = result_queue.get()
            if item is None:
                finished_workers += 1
            else:
                gp.ChangeSet_Dict.update(pickle.loads(item))
        except Exception as e:
            logger.error(f"Error reading worker batch result: {e}")
            break

    failed_workers = 0
    for p in processes:
        p.join()
        if p.exitcode != 0:
            failed_workers += 1
            logger.error(COLOR.red(f"Worker PID {p.pid} terminated abnormally with exit code {p.exitcode}"))

    error_list = []
    while not error_queue.empty():
        try:
            error_list.append(error_queue.get_nowait())
        except Exception:
            break

    if error_list:
        logger.error(COLOR.red(f"Multicore processing encountered {len(error_list)} file error(s):"))
        for failed_file, err, tb in error_list:
            logger.error(COLOR.red(f"  [ERROR] File: {failed_file} => {err}"))

    if failed_workers > 0 or error_list:
        logger.error(
            COLOR.red(
                f"Multicore execution completed with {failed_workers} crashed worker(s) "
                f"and {len(error_list)} file error(s)!"
            )
        )

    del processes

    return


def main() -> None:
    """Set the plan for what version to parse."""
    args = arg_handling()
    with G.DB() as db:
        if getattr(args, "reset", False) or getattr(args, "Drop", False):
            logger.info("Resetting and recreating all database tables...")
            db.drop_table(gp.Table_Array)
            db.create_table(gp.Table_Array)
            try:
                db.create_index("v_main_index", m_v_main, (m_v_main.vname,))
            except Exception:
                pass
        else:
            missing = db.test_tables(gp.Table_Array)
            if missing:
                logger.info(f"Missing tables detected ({missing}), creating tables...")
                db.create_table(gp.Table_Array)
                try:
                    db.create_index("v_main_index", m_v_main, (m_v_main.vname,))
                except Exception:
                    pass


    update("v3.0")
    update("v3.1")
    if True:
        update("v3.2")
        update("v3.3")
        update("v3.4")
        update("v3.5")
        update("v3.6")
        update("v3.7")
        update("v3.8")
        update("v3.9")
        update("v3.10")
        update("v3.11")
        update("v3.12")
        update("v3.13")
        update("v3.14")
        update("v3.15")
        update("v3.16")
        update("v3.17")
        update("v3.18")
        update("v3.19")
        update("v4.0")
        update("v4.1")
        update("v4.2")
        update("v4.3")
        update("v4.4")
        update("v4.5")
        update("v4.6")
        update("v4.7")
        update("v4.8")
        update("v4.9")
        update("v4.10")
        update("v4.11")
        update("v4.12")
        update("v4.13")
        update("v4.14")
        update("v4.15")
        update("v4.16")
        update("v4.17")
        update("v4.18")
        update("v4.19")
        update("v4.20")
        update("v5.0")
        update("v5.1")
        update("v5.2")
        update("v5.3")
        update("v5.4")
        update("v5.5")
        update("v5.6")
        update("v5.7")
        update("v5.8")
        update("v5.9")
        update("v5.10")
        update("v5.11")
        update("v5.12")
        update("v5.13")
        update("v5.14")
        update("v5.15")
        update("v5.16")
        update("v5.17")
        update("v5.18")
        update("v5.19")
        update("v6.0")
        update("v6.1")
        update("v6.2")
        update("v6.3")
        update("v6.4")
        update("v6.5")
        update("v6.6")
        update("v6.7")
        update("v6.8")
        update("v6.9")
        update("v6.10")
        update("v6.11")
        update("v6.12")
        update("v6.13")
        update("v6.14")
        update("v6.15")
        update("v6.16")
        update("v6.17")
        update("v6.18")
        update("v6.19")
        update("v7.0")
        update("v7.1")
        update("v7.2")


    logger.info("We are done! Closing")
    G.emergency_shutdown(0)
    return


def arg_handling() -> argparse.Namespace:
    """Handle arguments passed with python."""
    parser = argparse.ArgumentParser()
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
        "--db", "--db-engine",
        dest="db_engine",
        default="mariadb",
        choices=["mariadb", "mysql", "mock", "mockdb", "inmemory"],
        help="Select database backend engine (default: mariadb)",
    )
    parser.add_argument(
        "--te", "--table-engine",
        dest="table_engine",
        default="cached",
        choices=["cached", "direct", "tecacheddb", "tedirectdb"],
        help="Select Table Engine architecture backend (default: cached)",
    )
    args = parser.parse_args()

    if args.profile:
        G.PROFILING_ENABLED = True

    if args.db_engine:
        G.DB = get_db_engine(args.db_engine)

    if args.table_engine:
        G.TE = get_table_engine(args.table_engine)()

    if args.Drop:
        logger.info("Dropping all tables")
        gp.drop_all()
    if args.Create_Tables:
        gp.create_table_all()
        G.emergency_shutdown(0)
    if args.unit_test is not None:
        target = args.unit_test if args.unit_test != "" else None
        from tests.test_c_ast import run_c_ast_tests
        if target:
            code = run_c_ast_tests(target, profile=args.profile, table_engine=args.table_engine)
            sys.exit(code)

        # 1. Multi-Core C-AST regression suite
        c_ast_code = run_c_ast_tests(None, profile=args.profile, table_engine=args.table_engine)

        # 2. Comprehensive Unittest Suite across all test modules
        print(COLOR.cyan("=========================================================================================="))
        print(COLOR.cyan("                        RUNNING COMPREHENSIVE UNIT TEST SUITE                             "))
        print(COLOR.cyan("=========================================================================================="))
        import unittest
        loader = unittest.TestLoader()
        test_modules = [
            "tests.test_maintainer_ast",
            "tests.test_credits_lifecycle",
            "tests.test_bridge_map_dedup",
            "tests.test_git_commit_parser",
            "tests.test_te_db_integrity",
            "tests.test_kconfig_ast",
            "tests.test_webapp_defconfig",
            "tests.test_webapp_maintainer",
        ]
        suite = unittest.TestSuite()
        for mod_name in test_modules:
            try:
                suite.addTests(loader.loadTestsFromName(mod_name))
            except Exception as e:
                logger.error(f"Failed loading test module {mod_name}: {e}")

        runner = unittest.TextTestRunner(verbosity=2)
        res = runner.run(suite)

        all_ok = (c_ast_code == 0) and res.wasSuccessful()
        if all_ok:
            print(COLOR.green(f"\n[+] ALL {res.testsRun} UNIT TESTS AND C-AST REGRESSIONS COMPLETED SUCCESSFULLY!"))
        else:
            print(COLOR.red(f"\n[-] TEST FAILURES DETECTED IN TEST SUITE."))
        sys.exit(0 if all_ok else 1)
    if args.Test:
        from tests.test_c_ast import run_c_ast_tests
        code = run_c_ast_tests(args.Test, profile=args.profile, table_engine=args.table_engine)
        sys.exit(code)
    return args


def create_new_vid(name: str) -> bool:
    """Register or synchronize active version in m_v_main. Returns True if version already exists."""
    with G.DB() as db:
        existing = db.select(m_v_main, (None, name))
        if existing:
            gp.Old_VID = gp.VID
            gp.VID = existing[0]
            gp.Old_Version_Name = gp.Version_Name
            gp.Version_Name = name
            return True

        next_vid = db.get_next_id(m_v_main)
        gp.Old_VID = gp.VID
        gp.VID = next_vid
        gp.Old_Version_Name = gp.Version_Name
        gp.Version_Name = name

        db.insert(m_v_main, (gp.VID, name))
        return False

def default_processing(CS: ChangeSet) -> None:
    """Create m_file_name, m_file, m_bridge_file."""
    try:
        if CS.file_operation == "D":
            # DELETE
            with CS(REF_OLD):
                # Get old file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))
                CS.last_not_none()

                # Get old_bf
                CS.store(m_bridge_file.view(
                    ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                    gp.Old_VID,
                    CS.ref(m_file_name.fnid),
                    None,
                    None,
                    CS.current_path,
                ))
                CS.last_not_none()

                # Update FILE
                CS.store(m_file.update(
                    CS.ref(m_bridge_file.fid),
                    None,
                    gp.Old_VID,
                    None,
                    None,
                    "D",
                ))

        elif CS.file_operation and CS.file_operation[0] == "R":
            if CS.file_operation == "R100":
                # RENAME EXACT (content identical, reuse old fid)
                with CS(REF_OLD):
                    # Get old file_name
                    CS.store(m_file_name.get_set(
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                    # Get old_bf
                    CS.store(m_bridge_file.view(
                        ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                        gp.Old_VID,
                        CS.ref(m_file_name.fnid),
                        None,
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                # Get new file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))

                # Create BRIDGE FILE pointing to old fid
                CS.store(m_bridge_file.set(
                    gp.VID,
                    CS.ref(m_file_name.fnid),
                    CS.ref(m_bridge_file.fid, REF_OLD),
                ))

            else:
                # RENAME MODIFY (content modified, old fid ends and new fid starts)
                with CS(REF_OLD):
                    # Get old file_name
                    CS.store(m_file_name.get_set(
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                    # Get old_bf
                    CS.store(m_bridge_file.view(
                        ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                        gp.Old_VID,
                        CS.ref(m_file_name.fnid),
                        None,
                        None,
                        CS.old_path,
                    ))
                    CS.last_not_none()

                    # Update old FILE
                    CS.store(m_file.update(
                        CS.ref(m_bridge_file.fid),
                        None,
                        gp.Old_VID,
                        None,
                        None,
                        "R",
                    ))

                # Get new file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.current_path,
                ))

                # Get FILE
                CS.store(m_file.set(
                    None,
                    gp.VID,
                    0,
                    type_check(CS.current_path),
                    "R",
                    0,
                ))

                # Create BRIDGE FILE
                CS.store(m_bridge_file.set(
                    gp.VID,
                    CS.ref(m_file_name.fnid),
                    CS.ref(m_file.fid),
                ))

                # Create MOVED FILE
                CS.store(m_moved_file.set(
                    CS.ref(m_bridge_file.fid, REF_OLD),
                    CS.ref(m_file.fid),
                ))

        elif CS.file_operation == "M":
            # MODIFY
            # Get file_name
            CS.store(m_file_name.get_set(
                None,
                CS.current_path,
            ))
            CS.last_not_none()

            with CS(REF_OLD):
                # Get old_bf
                CS.store(m_bridge_file.view(
                    ((m_bridge_file.fnid, m_file_name.fnid, 1),),
                    gp.Old_VID,
                    CS.ref(m_file_name.fnid, REF_ROOT),
                    None,
                    None,
                    CS.current_path,
                ))
                CS.last_not_none()

                # 0 Update old FILE
                CS.store(m_file.update(
                    CS.ref(m_bridge_file.fid),
                    None,
                    gp.Old_VID,
                    None,
                    None,
                    "M",
                ))

            # 1 Create FILE
            CS.store(m_file.set(
                None,
                gp.VID,
                0,
                type_check(CS.current_path),
                "M",
                0,
            ))

            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))

    except CONTINUE_EXCEPTION:
        logger.error(f"CONTINUE_EXCEPTION:'{CS.file_operation}'={CS.current_path}")

    # If not yet processed
    if not CS.cs:
        # Add or other
        # 0 Check if FNAME exist/Create FNAME
        CS.store(m_file_name.get_set(
            None,
            CS.current_path,
        ))

        # 1 Create FILE
        CS.store(m_file.set(
            None,
            gp.VID,
            0,
            type_check(CS.current_path),
            "A",
            0,
        ))

        # 2 Create BRIDGE FILE
        CS.store(m_bridge_file.set(
            gp.VID,
            CS.ref(m_file_name.fnid),
            CS.ref(m_file.fid),
        ))
    return

def file_processing_worker(
    task_queue: multiprocessing.Queue,
    error_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    vid: int,
    gp_ref: GreatProcessor,
    mf_ref: MasterFile,
    worker_id: int = 0,
) -> None:
    """Worker process that continuously pulls and parses batches of files from `task_queue`."""
    G.TE.start_new_db(G.DB)

    while True:
        try:
            task = task_queue.get()
        except Exception as e:
            logger.error(f"Worker {worker_id} failed to get batch from queue: {e}")
            break

        if task is None:
            # Sentinel received, gracefully exit
            break

        batch_id, changed_files = task
        batch_cs_dict = {}

        for changed_file in changed_files:
            try:
                CS = ChangeSet(changed_file)
                CS.current_vid = vid
                CS.gp = gp_ref
                CS.mf = mf_ref
                G.CURRENT_PARSING_FILE = CS.current_path

                default_processing(CS)
                CS.parse()

                # Clean bloat and store in batch dict
                CS.clear_bloat()
                batch_cs_dict[CS.current_path] = CS
            except Exception as e:
                err_str = str(e)
                tb_str = traceback.format_exc()
                logger.error(COLOR.red(f"Worker {worker_id} error parsing '{changed_file}': {err_str}"))
                error_queue.put((changed_file, err_str, tb_str))
            finally:
                G.CURRENT_PARSING_FILE = None

        if batch_cs_dict:
            try:
                result_queue.put(pickle.dumps(batch_cs_dict, protocol=pickle.HIGHEST_PROTOCOL))
            except Exception as e:
                logger.error(COLOR.red(f"Worker {worker_id} failed to serialize batch {batch_id}: {e}"))
                error_queue.put((f"Batch-{batch_id}", str(e), traceback.format_exc()))

    result_queue.put(None)  # Worker done sentinel
    return


def file_processing(start: int, end: int | None, override_list: list[str] | None = None) -> None:
    """Process gp.Change_List (or override_list) and send CS into gp.ChangeSet_Dict."""
    G.TE.start_new_db(G.DB)
    if override_list:
        changed_files = override_list
    elif end is None:
        changed_files = gp.Change_List[start:]
    else:
        changed_files = gp.Change_List[start:end]

    for changed_file in changed_files:
        try:
            CS = ChangeSet(changed_file)
            CS.current_vid = gp.VID
            CS.gp = gp
            CS.mf = MF
            G.CURRENT_PARSING_FILE = CS.current_path

            default_processing(CS)
            CS.parse()

            # Store Set
            CS.clear_bloat()
            gp.ChangeSet_Dict[CS.current_path] = CS
        except Exception as e:
            logger.error(COLOR.red(f"Error processing file '{changed_file}': {e}\n{traceback.format_exc()}"))
        finally:
            G.CURRENT_PARSING_FILE = None

    if override_list is None:
        gp.push_set_to_main()

    return


def processing_unchanges() -> None:
    """Process everything outside gp.Change_List."""
    if gp.Old_VID == 0:
        return
    full_set = set(MF.git_file_list(gp.Version_Name).splitlines())

    changed_set = set()
    deleted_set = set()

    for item in gp.Change_List:
        if item.startswith("D"):
            deleted_set.add(item.split("\t")[-1])
        else:
            changed_set.add(item.split("\t")[-1])

    unchanged_set = full_set - changed_set

    old_full_set = set(MF.git_file_list(gp.Old_Version_Name).splitlines())
    forgotten_delete = (old_full_set - full_set) - deleted_set

    if forgotten_delete:
        logger.warning("There seems to be forgotten deletes... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_delete)
        file_processing(0, 0, (f"D\t{x}" for x in forgotten_delete))

    forgotten_new = (full_set - old_full_set) - changed_set
    if forgotten_new:
        logger.warning("There seems to be forgotten_new...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_new)

    batch_size = 1000
    batch_idx = 0
    CS = ChangeSet()
    for unchanged in unchanged_set:
        un_m_file_name = m_file_name.get(None, unchanged)
        if un_m_file_name is None:
            logger.error("processing_unchanges: un_m_file_name is None")
            logger.error(unchanged)
            logger.error(gp.Old_VID)
            logger.error(m_file_name.get(m_file_name.fname(unchanged)))
            continue
        un_m_bridge_file = m_bridge_file.get(gp.Old_VID, un_m_file_name[2][0], None)

        if un_m_bridge_file is None:
            logger.error("processing_unchanges: un_m_bridge_file is None")
            logger.error(unchanged)
            logger.error(gp.Old_VID)
            logger.error(m_file_name.get(m_file_name.fname(unchanged)))
            continue
        CS.cs.append((
            m_bridge_file.table_id,
            1,  # OP_SET
            (
                gp.VID,
                un_m_file_name[2][0],
                un_m_bridge_file[2][2],
            ),
        ))
        if len(CS.cs) >= batch_size:
            gp.ChangeSet_Dict[f"-UNCHANGED-{batch_idx}-"] = CS
            batch_idx += 1
            CS = ChangeSet()

    if CS.cs:
        gp.ChangeSet_Dict[f"-UNCHANGED-{batch_idx}-"] = CS
    return


def processing_dirs() -> None:  # noqa: C901
    """Process dirs."""
    dir_list = MF.get_dir_list(gp.Version_Name)

    if gp.Old_VID != 0:

        old_dir_list = set(MF.get_dir_list(gp.Old_Version_Name))
        dir_list = set(dir_list)
        new_dir_list = dir_list - old_dir_list

        CS = ChangeSet()
        # Unchanged dirs
        for single_dir in dir_list - (dir_list - old_dir_list):
            # Get m_file_name
            un_m_file_name = m_file_name.get(None, single_dir)
            if un_m_file_name is None:
                new_dir_list.add(single_dir)
                logger.error("Unchanged dirs: m_file_name is None")
                logger.error(single_dir)
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, un_m_file_name[2][0],None)
            if old_m_bridge_file is None:
                new_dir_list.add(single_dir)
                logger.error("Unchanged dirs: old_m_bridge_file is None")
                logger.error(single_dir)
                continue
            CS.cs.append((
                m_bridge_file.table_id,
                1,  # OP_SET
                (
                    gp.VID,
                    un_m_file_name[2][0],
                    old_m_bridge_file[2][2],
                ),
            ))

        if CS.cs:
            gp.ChangeSet_Dict["-UNCHANGED_DIRS-"] = CS

        # New dirs
        for single_dir in new_dir_list:
            CS = ChangeSet("A", single_dir)
            # 0 Check if FNAME exist/Create FNAME
            CS.store(m_file_name.get_set(None, single_dir))
            # 1 Create FILE
            CS.store(m_file.set(None, gp.VID, 0, T_DIR, "A", 0))
            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))
            gp.ChangeSet_Dict[single_dir] = CS

        CS = ChangeSet()
        # Deleted dirs
        for single_dir in old_dir_list - dir_list:
            # Get m_file_name
            if (del_m_file_name := m_file_name.get(None, single_dir)) is None:
                logger.error("Deleted dirs: m_file_name is None")
                logger.error(single_dir)
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, del_m_file_name[2][0], None)
            if old_m_bridge_file is None:
                new_dir_list.add(single_dir)
                logger.error("Deleted dirs: old_m_bridge_file is None")
                logger.error(single_dir)
                continue

            # 0 Update old FILE
            CS.store(m_file.update(
                old_m_bridge_file[2][2],
                None, gp.Old_VID,
                None,
                None,
                "R",
            ))

        if CS.cs:
            gp.ChangeSet_Dict["-DELETED_DIRS-"] = CS

    else:
        # If VID = 1, we need all dirs to be added
        for single_dir in dir_list:
            CS = ChangeSet("A", single_dir)
            # 0 Check if FNAME exist/Create FNAME
            CS.store(m_file_name.get_set(None, single_dir))
            # 1 Create FILE
            CS.store(m_file.set(None, gp.VID, 0, T_DIR, "A", 0))
            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))
            gp.ChangeSet_Dict[single_dir] = CS
    return


file_fid_cache: dict[str, int | None] = {}


def get_fid_for_path(path: str) -> int | None:
    """Resolve m_file.fid for a given path in active gp.VID."""
    if path in file_fid_cache:
        return file_fid_cache[path]
    fn_row = m_file_name.get(None, path)
    if fn_row and len(fn_row) >= 3 and fn_row[2]:
        fnid = fn_row[2][0]
        bf_row = m_bridge_file.get(gp.VID, fnid, None)
        if bf_row and len(bf_row) >= 3 and bf_row[2]:
            fid = bf_row[2][2]
            file_fid_cache[path] = fid
            return fid
    file_fid_cache[path] = None
    return None


def processing_git_commits(version: str) -> None:
    """Parse git commits for active version, link contributors, and bridge tags to commits."""
    from parser.git_ast import GitCommitParser
    git_parser = GitCommitParser()
    commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(
        gp.Old_Version_Name, gp.Version_Name
    )
    if not commits:
        return

    logger.info(f"Processing {len(commits)} git commits for version '{version}'...")

    commit_hash_to_id = {}
    for commit in commits:
        # Resolve author person_id
        author_res = G.TE.set(
            m_maintainer_person.table_id,
            (None, commit.author_name or commit.author_email, commit.author_email),
        )
        author_pid = author_res[0] if author_res else 1

        # Resolve committer person_id
        committer_res = G.TE.set(
            m_maintainer_person.table_id,
            (None, commit.committer_name or commit.committer_email, commit.committer_email),
        )
        committer_pid = committer_res[0] if committer_res else author_pid

        # Insert commit
        commit_res = G.TE.set(
            m_commit.table_id,
            (
                None,
                gp.VID,
                commit.commit_hash,
                author_pid,
                commit.author_date,
                committer_pid,
                commit.committer_date,
                commit.subject[:500],
                commit.message,
            ),
        )
        commit_id = commit_res[0]
        commit.commit_id = commit_id
        commit_hash_to_id[commit.commit_hash] = commit_id

        # Insert contributors (Author, Committer, Co-developed-by, Signed-off-by, Reviewed-by, etc.)
        for contrib in commit.contributors:
            c_res = G.TE.set(
                m_maintainer_person.table_id,
                (None, contrib.name or contrib.email, contrib.email),
            )
            c_pid = c_res[0] if c_res else 1
            G.TE.set(
                m_bridge_commit_person.table_id,
                (
                    commit_id,
                    c_pid,
                    int(contrib.role),
                    int(contrib.priority),
                ),
            )

        # Insert modified file bridges
        for change_type, file_path in commit.files:
            fid = get_fid_for_path(file_path)
            if fid is not None:
                G.TE.set(
                    m_bridge_commit_file.table_id,
                    (
                        commit_id,
                        gp.VID,
                        fid,
                        change_type[:1],
                    ),
                )

    # Link tags to commits for all changed files and evacuate executed ChangeSet memory
    for file_path, cs_obj in list(gp.ChangeSet_Dict.items()):
        if not cs_obj or not getattr(cs_obj, "current_path", None):
            continue
        c_path = cs_obj.current_path
        fid = get_fid_for_path(c_path)
        if fid is None:
            continue

        # Collect tags for this file from ChangeSet operations
        file_tags = []
        for op in getattr(cs_obj, "cs", []):
            if op and len(op) >= 3 and op[0] == m_bridge_tag.table_id:
                cols = op[2]
                if len(cols) >= 4:
                    tag_id = cols[1] if not isinstance(cols[1], tuple) else None
                    line_s = cols[2] if isinstance(cols[2], int) else 1
                    line_e = cols[3] if isinstance(cols[3], int) else line_s
                    if tag_id is not None:
                        file_tags.append((tag_id, fid, line_s, line_e))

        if file_tags:
            tag_bridges = git_parser.map_tags_to_commits(
                file_tags,
                c_path,
                commit_hash_to_id=commit_hash_to_id,
                file_hunks_map=file_hunks_map,
            )
            for cid, f_id, tid in tag_bridges:
                G.TE.set(
                    m_bridge_commit_tag.table_id,
                    (
                        cid,
                        gp.VID,
                        f_id,
                        tid,
                    ),
                )

        # Evacuate internal AST memory from executed ChangeSet
        if hasattr(cs_obj, "cs") and isinstance(cs_obj.cs, list):
            cs_obj.cs.clear()
        if hasattr(cs_obj, "store_dict") and isinstance(cs_obj.store_dict, dict):
            cs_obj.store_dict.clear()
        if hasattr(cs_obj, "cs_result") and isinstance(cs_obj.cs_result, list):
            cs_obj.cs_result.clear()


def processing_kbuild(version: str) -> None:
    """Parse Makefile and Kbuild files for active version and map Kconfig symbols to compiled sources."""
    if gp.VID == 0:
        return

    from parser.kbuild_parser import KbuildParser
    kbuild_parser = KbuildParser()

    try:
        file_list_raw = MF.git_file_list(gp.Version_Name)
    except Exception as e:
        logger.debug(f"Failed to get git_file_list for kbuild parsing in {version}: {e}")
        return

    all_files = file_list_raw.splitlines() if file_list_raw else []
    makefile_paths = [
        f for f in all_files
        if f.endswith("Makefile") or f.endswith("Kbuild") or f.endswith("/Makefile") or f.endswith("/Kbuild")
    ]
    if not makefile_paths:
        return

    logger.info(f"Processing {len(makefile_paths)} Makefile/Kbuild files for version '{version}'...")

    kcid_cache: dict[str, int] = {}

    def get_kcid_for_sym(sym: str) -> int:
        if not sym or sym in ("y", "m"):
            return 0
        clean_sym = sym[7:] if sym.startswith("CONFIG_") else sym
        if clean_sym in kcid_cache:
            return kcid_cache[clean_sym]
        sym_row = m_kconfig_symbol.get(None, None, None, clean_sym, None, None, None, None, None)
        if sym_row and len(sym_row) >= 3 and sym_row[2]:
            kcid = sym_row[2][0]
            kcid_cache[clean_sym] = kcid
            return kcid
        kcid_cache[clean_sym] = 0
        return 0

    count = 0
    for mk_path in makefile_paths:
        try:
            content = MF.get_file(mk_path, gp.Version_Name)
            if not content:
                continue
            dir_path = os.path.dirname(mk_path)
            bindings = kbuild_parser.parse_makefile_content(content, dir_path=dir_path)
            for b in bindings:
                fid = get_fid_for_path(b.source_file_rel)
                if fid is None and b.source_file_rel.endswith(".c"):
                    alt_asm = b.source_file_rel[:-2] + ".S"
                    fid = get_fid_for_path(alt_asm)
                if fid is not None:
                    kcid = get_kcid_for_sym(b.symbol_name)
                    G.TE.set(
                        m_kconfig_kbuild.table_id,
                        (
                            kcid,
                            gp.VID,
                            fid,
                            int(b.compile_mode),
                            b.target_obj[:64],
                        ),
                    )
                    count += 1
        except Exception as e:
            logger.debug(f"Error parsing kbuild file '{mk_path}': {e}")

    logger.info(f"Staged {count} Kbuild symbol-to-source mappings into m_kconfig_kbuild for version '{version}'.")


def processing_maintainer_files(version: str) -> None:
    """Batch match active files for version against MAINTAINERS patterns and populate m_maintainer_file."""
    if gp.VID == 0:
        return

    try:
        raw_maintainers = MF.get_file("MAINTAINERS", gp.Version_Name)
    except Exception as e:
        logger.debug(f"MAINTAINERS file not found for version '{version}': {e}")
        return

    if not raw_maintainers:
        return

    from parser.maintainer_ast.maintainer_parser import MaintainerParser
    from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher

    sections = MaintainerParser(raw_maintainers).parse()
    if not sections:
        return

    matcher = MaintainerMatcher(sections)

    sec_id_cache: dict[str, int | None] = {}

    def get_sec_id_for_name(name: str) -> int | None:
        if name in sec_id_cache:
            return sec_id_cache[name]
        sec_row = m_maintainer_section.get(None, None, None, name, None, None, None, None, None)
        if sec_row and len(sec_row) >= 3 and sec_row[2]:
            sec_id = sec_row[2][0]
            sec_id_cache[name] = sec_id
            return sec_id
        sec_id_cache[name] = None
        return None

    try:
        file_list_raw = MF.git_file_list(gp.Version_Name)
    except Exception as e:
        logger.debug(f"Failed to get git_file_list for maintainer matching in {version}: {e}")
        return

    all_files = file_list_raw.splitlines() if file_list_raw else []
    if not all_files:
        return

    logger.info(f"Matching {len(all_files)} files against {len(sections)} maintainer sections for version '{version}'...")

    matched_count = 0
    for file_path in all_files:
        matched_sections = matcher.match_file(file_path)
        if not matched_sections:
            continue
        fid = get_fid_for_path(file_path)
        if fid is None:
            continue
        for sec in matched_sections:
            sec_id = get_sec_id_for_name(sec.name)
            if sec_id is not None:
                G.TE.set(
                    m_maintainer_file.table_id,
                    (
                        gp.VID,
                        fid,
                        sec_id,
                    ),
                )
                matched_count += 1

    logger.info(f"Staged {matched_count} file-to-section mappings into m_maintainer_file for version '{version}'.")


if __name__ == "__main__":
    main()

