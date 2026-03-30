"""Main.py - Main processing loop."""
from globalstuff import (
    G,
    COLOR,
    type_check,
    REF_ROOT,
    REF_OLD,
    REF_NOT_RESOLVABLE,
    CONTINUE_EXCEPTION,
)
import argparse
import multiprocessing
from queue import SimpleQueue
from DBHandling import MariaDB
from table_engine.te_direct_db import TEDirectDB
from FileHandler import MasterFile
from TableHandling import Table, ChangeSet
from GreatProcessor import GreatProcessor

G.DB = MariaDB
G.TE = TEDirectDB()
MF = MasterFile()
gp = GreatProcessor()

##################################
# DB STRUCTURE
# Order is important, keep vmain as 0 or change GP
gp.Table_Array.append(
    m_v_main := Table(
        table_id=len(gp.Table_Array),
        table_name="m_v_main",
        columns=(
            ("vid", "INT", "NOT NULL", "AUTO_INCREMENT"),
            ("vname", "VARCHAR(32)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ),
        primary=("vid",),
        foreign=None,
        initial_insert=((0, "latest"),),
        no_duplicate=True,
        select_procedure=True,
    ),
)

gp.Table_Array.append(
    m_file_name := Table(
        table_id=len(gp.Table_Array),
        table_name="m_file_name",
        columns=(
            ("fnid", "INT", "NOT NULL", "AUTO_INCREMENT"),
            ("fname", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
        ),
        primary=("fnid",),
        foreign=None,
        initial_insert=((0, ""),),
        no_duplicate=True,
        select_procedure=True,
    ),
)

gp.Table_Array.append(
    m_file := Table(
        table_id=len(gp.Table_Array),
        table_name="m_file",
        columns=(
            ("fid", "INT", "NOT NULL", "AUTO_INCREMENT"),
            ("vid_s", "INT", "NOT NULL"),
            ("vid_e", "INT", "NOT NULL"),
            ("ftype", "TINYINT", "UNSIGNED", "NOT NULL"),
            ("s_stat", "CHAR(1)", "NOT NULL"),
            ("e_stat", "CHAR(1)", "NOT NULL"),
        ),
        primary=("fid",),
        foreign=(("vid_s", "m_v_main", "vid"), ("vid_e", "m_v_main", "vid")),
        initial_insert=((0, 0, 0, 0, 0, 0)),
        no_duplicate=False,
        select_procedure=True,
    ),
)

#select_procedure=lambda x: (f"SELECT m_file.* FROM m_file INNER JOIN m_bridge_file ON m_bridge_file.fid = m_file.fid WHERE m_bridge_file.vid = {x.Old_VID};"),


gp.Table_Array.append(
    m_bridge_file := Table(
        table_id=len(gp.Table_Array),
        table_name="m_bridge_file",
        columns=(
            ("vid", "INT", "NOT NULL"),
            ("fnid", "INT", "NOT NULL"),
            ("fid", "INT", "NOT NULL"),
        ),
        primary=("vid", "fnid"),
        foreign=(
            ("vid", "m_v_main", "vid"),
            ("fnid", "m_file_name", "fnid"),
            ("fid", "m_file", "fid"),
        ),
        initial_insert=None,
        no_duplicate=False,
        select_procedure=True,
    ),
)

#lambda x: f"SELECT * FROM m_bridge_file WHERE m_bridge_file.vid = {x.Old_VID};",

gp.Table_Array.append(
    m_moved_file := Table(
        table_id=len(gp.Table_Array),
        table_name="m_moved_file",
        columns=(("s_fid", "INT", "NOT NULL"), ("e_fid", "INT", "NOT NULL")),
        primary=("s_fid", "e_fid"),
        foreign=(("s_fid", "m_file", "fid"), ("e_fid", "m_file", "fid")),
        initial_insert=None,
        no_duplicate=False,
        select_procedure=False,
    ),
)

gp.Table_Array.append(
    m_ast := Table(
        table_id=len(gp.Table_Array),
        table_name="m_ast",
        columns=(
            ("ast_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
            ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
            ("type_id", "TINYINT", "UNSIGNED", "NOT NULL"),
        ),
        primary=("ast_id",),
        foreign=None,
        initial_insert=(0, "", 0),
        no_duplicate=False,
        select_procedure=True,
    ),
)

gp.Table_Array.append(
    m_ast_container := Table(
        table_id=len(gp.Table_Array),
        table_name="m_ast_container",
        columns=(
            ("ast_id", "INT", "NOT NULL"),
            ("priority", "TINYINT", "UNSIGNED", "NOT NULL"),
            ("name", "VARCHAR(255)", "NOT NULL", "COLLATE utf8mb4_bin"),
            ("subtype", "TINYINT", "UNSIGNED", "NOT NULL"),
            ("ref_ast_id", "INT", "NOT NULL"),
        ),
        primary=("ast_id", "priority"),
        foreign=(("ast_id", "m_ast", "ast_id"), ("ref_ast_id", "m_ast", "ast_id")),
        initial_insert=None,
        no_duplicate=False,
        select_procedure=True,
    ),
)

gp.Table_Array.append(
    m_ast_include := Table(
        table_id=len(gp.Table_Array),
        table_name="m_ast_include",
        columns=(("ast_id", "INT", "NOT NULL"), ("fnid", "INT", "NOT NULL")),
        primary=("ast_id",),
        foreign=(("ast_id", "m_ast", "ast_id"), ("fnid", "m_file_name", "fnid")),
        initial_insert=None,
        no_duplicate=False,
        select_procedure=True,
    ),
)

gp.Table_Array.append(
    m_ast_debug := Table(
        table_id=len(gp.Table_Array),
        table_name="m_ast_debug",
        columns=(("ast_id", "INT", "NOT NULL"), ("ast_raw", "MEDIUMTEXT", "NOT NULL")),
        primary=("ast_id",),
        foreign=(("ast_id", "m_ast", "ast_id"),),
        initial_insert=None,
        no_duplicate=False,
        select_procedure=False,
    ),
)

gp.Table_Array.append(
    m_tag := Table(
        table_id=len(gp.Table_Array),
        table_name="m_tag",
        columns=(
            ("tag_id", "INT", "NOT NULL", "AUTO_INCREMENT"),
            ("vid_s", "INT", "NOT NULL"),
            ("vid_e", "INT", "NOT NULL"),
            ("code", "LONGTEXT", "NOT NULL"),
            ("ast_id", "INT", "NOT NULL"),
            ("hl_s", "INT", "NOT NULL"),
            ("hl_l", "INT", "NOT NULL"),
        ),
        primary=("tag_id", "vid_s"),
        foreign=(
            ("vid_s", "m_v_main", "vid"),
            ("vid_e", "m_v_main", "vid"),
            ("ast_id", "m_ast", "ast_id"),
        ),
        initial_insert=(0, 0, 0, "", 0, 0, 0),
        no_duplicate=False,
        select_procedure=True,  #only get the last version of the tags
    ),
)

gp.Table_Array.append(
    m_bridge_tag := Table(
        table_id=len(gp.Table_Array),
        table_name="m_bridge_tag",
        columns=(
            ("fid", "INT", "NOT NULL"),
            ("tag_id", "INT", "NOT NULL"),
            ("line_s", "INT", "NOT NULL"),
            ("line_e", "INT", "NOT NULL"),
        ),
        primary=("fid", "tag_id"),
        foreign=(("fid", "m_file", "fid"), ("tag_id", "m_tag", "tag_id")),
        initial_insert=None,
        no_duplicate=False,
        select_procedure=True,  #only get the last version of the tags
    ),
)

# DB STRUCTURE END
##################################

def update(version: str) -> None:
    """Execute main processing loop."""
    print(COLOR.green(f"=======================Working on {version}======================="))
    create_new_vid(version)

    # Index Handling
    with G.DB() as db:
        db.create_index("ast_index", m_ast, (m_ast.name, m_ast.type_id))
        db.create_index("file_name_index", m_file_name, (m_file_name.fname,))

    # Pre-Processing
    MF.add_version(version, gp.PURGE_LIST)

    MF.generate_change_list(gp)
    G.TE.start(gp.Table_Array, G.DB)
    ## preload/dirs
    # processing_dirs()
    # preload_fnid()

    # Main Processing
    trigger_multicore()

    G.TE.start_new_db(G.DB)
    # probably this loop missing some shit, IE: arch/x86/include/asm/xen/page.h
    cs_queue = SimpleQueue()

    for key in gp.ChangeSet_Dict:
        cs_queue.put(key)

    max_loop = len(gp.ChangeSet_Dict) * G.OVERRIDE_FC_MAX_LOOP_EXEC_MULT
    while not cs_queue.empty():
        max_loop -= 1
        if max_loop < 0:
            print(f"max loop ({len(gp.ChangeSet_Dict)*G.OVERRIDE_FC_MAX_LOOP_EXEC_MULT}) was brought to 0, printing queue:")
            while not cs_queue.empty():
                print(gp.ChangeSet_Dict[cs_queue.get()])
            G.emergency_shutdown(666)
        current_cs = cs_queue.get()
        try:
            if not gp.ChangeSet_Dict[current_cs].execute():
                cs_queue.put(current_cs)
        except REF_NOT_RESOLVABLE:
            cs_queue.put(current_cs)

    with G.DB() as db:
        db.remove_index("ast_index", m_ast)
        db.remove_index("file_name_index", m_file_name)

    for table in gp.Table_Array:
        G.TE.commit(table.table_id)

    gp.reset_cs()
    return

def trigger_multicore() -> None:
    """Execute G.CPUS - 1 process for parsing."""
    gp.start_manager()
    processes = []

    split_change_list_size = len(gp.Change_List) // int(G.CPUS - 1)

    for x in range(G.CPUS - 1):
        if x == (G.CPUS - 2):
            process_arg = (split_change_list_size * x, None)
            processes.append(
                multiprocessing.Process(
                    target=file_processing,
                    args=process_arg,
                ),
            )
        else:
            process_arg = (
                split_change_list_size * x,
                split_change_list_size * (x + 1),
            )
            processes.append(
                multiprocessing.Process(
                    target=file_processing,
                    args=process_arg,
                ),
            )
        processes[-1].start()

    G.TE.start_new_db(G.DB)
    # needs to be try: protected
    processing_dirs()
    processing_unchanges()

    for fp_instance in processes:
        fp_instance.join()

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
    update("v3.1")
    update("v3.2")
    update("v3.3")
    update("v3.4")
    update("v3.5")
    #except Exception as e:  # noqa: BLE001
    #    print("Error in Update()")
    #    print(e)
    #    G.emergency_shutdown(2)

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
        "-T", "--Test",
        help="Test/Parse a specific file",
    )
    parser.add_argument(
        "-V", "--version",
        help="Add a new version to the DB",
    )
    args = parser.parse_args()

    if args.Drop:
        print("Dropping all tables")
        gp.drop_all()
    if args.Create_Tables:
        gp.create_table_all()
        G.emergency_shutdown(0)
    if args.Test:
        # THIS SHIT AINT WORKIN
        gp.drop_all()
        gp.create_table_all()
        gp.clear_fetch_all()
        gp.create_new_vid("v3.0")
        MF.add_version("v3.0", gp.PURGE_LIST)
        # include/linux/netfilter_bridge/ebtables.h
        # include/linux/lockd/bind.h
        # include/linux/sched.h
        # Ast_Manager(MF.version_dict[gp.Version_Name], args.Test)
        G.emergency_shutdown(0)
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
        print(f"CONTINUE_EXCEPTION:'{CS.file_operation}'={CS.current_path}")

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

def file_processing(start: int, end: int | None, override_list: list[str] | None=None) -> None:
    """Process gp.Change_List and sent CS into gp.ChangeSet_Dict."""
    G.TE.start_new_db(G.DB)
    if override_list:
        changed_files = override_list
    elif end is None:
        changed_files = gp.Change_List[start:]
    else:
        changed_files = gp.Change_List[start:end]

    for changed_file in changed_files:
        CS = ChangeSet(changed_file)
        CS.current_vid = gp.VID
        CS.gp = gp
        CS.mf = MF

        default_processing(CS)

        CS.parse()

        # Store Set
        CS.clear_bloat()
        gp.ChangeSet_Dict[CS.current_path] = CS

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
        print("There seems to be forgotten deletes... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            print(forgotten_delete)
        file_processing(0, 0, (f"D\t{x}" for x in forgotten_delete))
        #map(lambda x: f"D\t{x}", forgotten_delete))

    forgotten_new = (full_set - old_full_set) - changed_set
    if forgotten_new:
        print("There seems to be forgotten_new...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            print(forgotten_new)

    CS = ChangeSet()
    for unchanged in unchanged_set:
        un_m_file_name = m_file_name.get(None, unchanged)
        if un_m_file_name is None:
            print("processing_unchanges: un_m_file_name is None")
            print(unchanged)
            print(gp.Old_VID)
            print(m_file_name.get(m_file_name.fname(unchanged)))
            continue
        un_m_bridge_file = m_bridge_file.get(gp.Old_VID, un_m_file_name[2][0], None)

        if un_m_bridge_file is None:
            print("processing_unchanges: un_m_bridge_file is None")
            print(unchanged)
            print(gp.Old_VID)
            print(m_file_name.get(m_file_name.fname(unchanged)))
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
                print("Unchanged dirs: m_file_name is None")
                print(single_dir)
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, un_m_file_name[2][0],None)
            if old_m_bridge_file is None:
                new_dir_list.add(single_dir)
                print("Unchanged dirs: old_m_bridge_file is None")
                print(single_dir)
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
            CS.store(m_file.set(None, gp.VID, 0, 1, "A", 0))
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
                print("Deleted dirs: m_file_name is None")
                print(single_dir)
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, del_m_file_name[2][0], None)
            if old_m_bridge_file is None:
                new_dir_list.add(single_dir)
                print("Deleted dirs: old_m_bridge_file is None")
                print(single_dir)
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
            CS.store(m_file.set(None, gp.VID, 0, 1, "A", 0))
            # 2 Create BRIDGE FILE
            CS.store(m_bridge_file.set(
                gp.VID,
                CS.ref(m_file_name.fnid),
                CS.ref(m_file.fid),
            ))
            gp.ChangeSet_Dict[single_dir] = CS
    return


####unused
def preload_fnid() -> None:  # noqa: D103
    # Based on change
    for changed_file in (x.split("\t")[-1] for x in gp.Change_List):
        m_file_name.actual_get_set(m_file_name.fname(changed_file))
    return


if __name__ == "__main__":
    main()
