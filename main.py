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
  `gp.Table_Array` registers 14 core relational tables defining the schema:
    - Version & File tracking: `m_v_main`, `m_file_name`, `m_file`, `m_bridge_file`, `m_moved_file`
    - AST Schema: `m_type_descriptor`, `m_ast`, `m_ast_container`, `m_ast_include`, `m_ast_debug`
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
from globalstuff import (
    G,
    COLOR,
    type_check,
    REF_ROOT,
    REF_OLD,
    REF_NOT_RESOLVABLE,
    CONTINUE_EXCEPTION,
    T_DIR,
    ASTT,
)
import sys
import logging
import argparse
import multiprocessing
import traceback
import pickle
from queue import SimpleQueue
from db_engine import MariaDB, MockDB, get_db_engine
from table_engine.te_direct_db import TEDirectDB
from FileHandler import MasterFile
from TableHandling import Table, ChangeSet
from GreatProcessor import GreatProcessor
from parser.c_ast.c_ast import Ast_Manager


G.DB = MariaDB
G.TE = TEDirectDB()
MF = MasterFile()
G.MF = MF
gp = GreatProcessor()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


##################################
# DB STRUCTURE (Extracted to DBLayout.py)
from DBLayout import (
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
)

init_db_layout(gp)
##################################

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
    logger.info(COLOR.green(f"=======================Working on {version}======================="))
    
    # -------------------------------------------------------------------------
    # STEP 1: Register new version release in m_v_main
    # -------------------------------------------------------------------------
    create_new_vid(version)

    # -------------------------------------------------------------------------
    # STEP 2: Create temporary performance indexes on DB tables
    # -------------------------------------------------------------------------
    with G.DB() as db:
        db.create_index("ast_index", m_ast, (m_ast.name, m_ast.type_id))
        db.create_index("file_name_index", m_file_name, (m_file_name.fname,))

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
    cs_queue = SimpleQueue()

    for key in gp.ChangeSet_Dict:
        cs_queue.put(key)

    max_loop = len(gp.ChangeSet_Dict) * G.OVERRIDE_FC_MAX_LOOP_EXEC_MULT
    while not cs_queue.empty():
        max_loop -= 1
        if max_loop < 0:
            logger.error(f"max loop ({len(gp.ChangeSet_Dict)*G.OVERRIDE_FC_MAX_LOOP_EXEC_MULT}) was brought to 0, printing queue:")
            debug_unresolved = []
            while not cs_queue.empty():
                debug_unresolved.append(gp.ChangeSet_Dict[cs_queue.get()])

            G.BP_ON_REF_FAIL = True

            for item in debug_unresolved:
                item.execute()

            G.emergency_shutdown(666)
        current_cs = cs_queue.get()
        if not gp.ChangeSet_Dict[current_cs].execute():
            cs_queue.put(current_cs)


    # -------------------------------------------------------------------------
    # STEP 7: Remove temporary indexes, commit transactions, and reset state
    # -------------------------------------------------------------------------
    with G.DB() as db:
        db.remove_index("ast_index", m_ast)
        db.remove_index("file_name_index", m_file_name)

    for table in gp.Table_Array:
        G.TE.commit(table.table_id)

    gp.reset_cs()
    return


def trigger_multicore(batch_size: int = 200) -> None:
    """Distribute file parsing across `G.CPUS - 1` parallel worker processes in dynamic batches."""
    gp.start_manager()
    task_queue = gp.Manager.Queue()
    error_list = gp.Manager.list()

    change_list = gp.Change_List
    total_files = len(change_list)
    total_batches = (total_files + batch_size - 1) // batch_size if total_files > 0 else 0

    num_workers = max(1, int(G.CPUS - 1))
    logger.info(
        f"Distributing {total_files} changed files in {total_batches} batches "
        f"(batch size: {batch_size}) across {num_workers} parallel workers"
    )

    for i in range(0, total_files, batch_size):
        task_queue.put((i // batch_size, change_list[i : i + batch_size]))

    for _ in range(num_workers):
        task_queue.put(None)  # Sentinel to terminate each worker

    processes = []
    for worker_id in range(num_workers):
        p = multiprocessing.Process(
            target=file_processing_worker,
            args=(task_queue, error_list, gp.Shared_ChangeSet_Dict_List, gp.VID, gp, MF, worker_id),
        )
        processes.append(p)
        p.start()

    G.TE.start_new_db(G.DB)
    # needs to be try: protected
    processing_dirs()
    processing_unchanges()

    failed_workers = 0
    for p in processes:
        p.join()
        if p.exitcode != 0:
            failed_workers += 1
            logger.error(COLOR.red(f"Worker PID {p.pid} terminated abnormally with exit code {p.exitcode}"))

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

    gp.stop_manager()

    del processes

    return


def main() -> None:
    """Set the plan for what version to parse."""
    arg_handling()
    with G.DB() as db:
        db.drop_table(gp.Table_Array)
        db.create_table(gp.Table_Array)
        db.create_index("v_main_index", m_v_main, (m_v_main.vname,))



    #try:
    update("v3.0")
    #update("v3.1")
    #update("v3.2")
    #update("v3.3")
    #update("v3.4")
    #update("v3.5")
    #except Exception as e:  # noqa: BLE001
    #    logger.error("Error in Update()")
    #    logger.error(e)
    #    G.emergency_shutdown(2)


    logger.info("We are done! Closing")
    G.emergency_shutdown(0)
    return


def arg_handling() -> None:
    """Handle arguments passed with python."""
    parser = argparse.ArgumentParser()
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
        help="Run C-AST unit test suite in /dev/shm (optionally specify a single file to test)",
    )
    parser.add_argument(
        "-T", "--Test",
        help="Test/Parse a specific file",
    )
    parser.add_argument(
        "--db", "--db-engine",
        dest="db_engine",
        default="mariadb",
        choices=["mariadb", "mysql", "mock", "mockdb", "inmemory"],
        help="Select database backend engine (default: mariadb)",
    )
    args = parser.parse_args()

    if args.db_engine:
        G.DB = get_db_engine(args.db_engine)

    if args.Drop:
        logger.info("Dropping all tables")
        gp.drop_all()
    if args.Create_Tables:
        gp.create_table_all()
        G.emergency_shutdown(0)
    if args.unit_test is not None:
        target = args.unit_test if args.unit_test != "" else None
        from tests.test_c_ast import run_c_ast_tests
        code = run_c_ast_tests(target)
        sys.exit(code)
    if args.Test:
        from tests.test_c_ast import run_c_ast_tests
        code = run_c_ast_tests(args.Test)
        sys.exit(code)
    return


def create_new_vid(name: str) -> None:
    """Create new m_v_main row."""
    gp.Old_Version_Name = gp.Version_Name
    gp.Version_Name = name
    gp.Old_VID = gp.VID
    gp.VID += 1
    with G.DB() as db:
        db.insert(m_v_main, (gp.VID, name))

    return

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
                CS.store(m_bridge_file.get(
                    gp.Old_VID,
                    CS.ref(m_file_name.fnid),
                    None,
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

        elif CS.file_operation[1:] == "R":
            ## Currently no difference between R100 or R##
            with CS(REF_OLD):
                # Get old file_name
                CS.store(m_file_name.get_set(
                    None,
                    CS.old_path,
                ))
                CS.last_not_none()

                # Get old_bf
                CS.store(m_bridge_file.get(
                    gp.Old_VID,
                    CS.ref(m_file_name.fnid),
                    None,
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

            # Check if FNAME exist/Create FNAME
            CS.store(m_file_name.get_set(
                None,
                CS.current_path,
            ))

            if CS.file_operation == "R100":
                # Exact Moved
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

            else:
                # RENAME MODIFY
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
                CS.store(m_bridge_file.get(
                    gp.Old_VID,
                    CS.ref(m_file_name.fnid, REF_ROOT),
                    None,
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
    error_list: list,
    shared_dict_list: list,
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

                default_processing(CS)
                CS.parse()

                # Clean bloat and store in batch dict
                CS.clear_bloat()
                batch_cs_dict[CS.current_path] = CS
            except Exception as e:
                err_str = str(e)
                tb_str = traceback.format_exc()
                logger.error(COLOR.red(f"Worker {worker_id} error parsing '{changed_file}': {err_str}"))
                error_list.append((changed_file, err_str, tb_str))

        if batch_cs_dict:
            try:
                shared_dict_list.append(pickle.dumps(batch_cs_dict))
            except Exception as e:
                logger.error(COLOR.red(f"Worker {worker_id} failed to serialize batch {batch_id}: {e}"))
                error_list.append((f"Batch-{batch_id}", str(e), traceback.format_exc()))

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

            default_processing(CS)
            CS.parse()

            # Store Set
            CS.clear_bloat()
            gp.ChangeSet_Dict[CS.current_path] = CS
        except Exception as e:
            logger.error(COLOR.red(f"Error processing file '{changed_file}': {e}\n{traceback.format_exc()}"))

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

    #changed_set = {x.split("\t")[-1] for x in filter(lambda x: not x.startswith("D"), gp.Change_List)}
    #deleted_set = {x.split("\t")[-1] for x in filter(lambda x: x.startswith("D"), gp.Change_List)}

    unchanged_set = full_set - changed_set

    old_full_set = set(MF.git_file_list(gp.Old_Version_Name).splitlines())
    forgotten_delete = (old_full_set - full_set) - deleted_set

    if forgotten_delete:
        logger.warning("There seems to be forgotten deletes... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_delete)
        file_processing(0, 0, (f"D\t{x}" for x in forgotten_delete))
        #map(lambda x: f"D\t{x}", forgotten_delete))

    forgotten_new = (full_set - old_full_set) - changed_set
    if forgotten_new:
        logger.warning("There seems to be forgotten_new...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_new)

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
        CS.store(m_bridge_file.set(
            gp.VID,
            un_m_file_name[2][0],
            un_m_bridge_file[2][2],
        ))
    if CS.cs:
        gp.ChangeSet_Dict["-UNCHANGED-"] = CS
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
            CS.store(m_bridge_file.set(
                gp.VID,
                un_m_file_name[2][0],
                old_m_bridge_file[2][2],
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


if __name__ == "__main__":
    main()
