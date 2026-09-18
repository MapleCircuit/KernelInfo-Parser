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
    T_RAW,
    T_MAINTAINERS,
    T_CREDITS,
    ASTT,
    configure_logging,
    setup_memory_limit,
    FileRefType,
)
from core.config import init_config, get_parser_config, get_db_config
import os
import sys

# Raise recursion limit for parsing deeply nested ASTs in kernel source files
sys.setrecursionlimit(50000)
import time
import logging
import argparse
import multiprocessing
import re
from collections import deque, defaultdict
import pickle
import zlib
import traceback
import gc
import ctypes
from parser.c_ast import c_ast_parse, Line
from core.FileHandler import MasterFile
from core.GreatProcessor import GreatProcessor
from core.TableHandling import Table, ChangeSet
from db_engine import MariaDB, MockDB, get_db_engine
from table_engine import TECachedDB, TEDirectDB, get_table_engine
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
    m_tag_code,
    m_moved_tag,
    m_file_reference,
)


G.DB = MariaDB
G.TE = TECachedDB()
MF = MasterFile()
G.MF = MF
gp = GreatProcessor()
configure_logging(level=logging.INFO, fmt="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

init_db_layout(gp)

T_NO_FOREIGN = frozenset({T_RAW, T_DIR, T_MAINTAINERS, T_CREDITS})


def reclaim_system_memory() -> None:
    """Force CPython garbage collection and release heap pages back to OS via glibc."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


file_fid_cache: dict[str, int | None] = {}
file_fnid_cache: dict[str, int | None] = {}


def get_fnid_for_path(path: str) -> int | None:
    """Resolve m_file_name.fnid for a given path."""
    if path in file_fnid_cache:
        return file_fnid_cache[path]
    te = getattr(G, "TE", None)
    if not te or m_file_name.table_id not in getattr(te, "tables", {}):
        return None
    fn_row = m_file_name.get(None, path)
    if fn_row and len(fn_row) >= 3 and fn_row[2]:
        fnid = fn_row[2][0]
        file_fnid_cache[path] = fnid
        return fnid
    file_fnid_cache[path] = None
    return None


def get_fid_for_path(path: str) -> int | None:
    """Resolve m_file.fid for a given path in active gp.VID (falling back to gp.Old_VID for unchanged files)."""
    if path in file_fid_cache:
        return file_fid_cache[path]
    te = getattr(G, "TE", None)
    if not te or m_file_name.table_id not in getattr(te, "tables", {}) or m_bridge_file.table_id not in getattr(te, "tables", {}):
        return None
    fn_row = m_file_name.get(None, path)
    if fn_row and len(fn_row) >= 3 and fn_row[2]:
        fnid = fn_row[2][0]
        bf_row = m_bridge_file.get(gp.VID, fnid, None)
        if (bf_row is None or len(bf_row) < 3 or not bf_row[2]) and getattr(gp, "Old_VID", 0) > 0:
            bf_row = m_bridge_file.get(gp.Old_VID, fnid, None)
        if bf_row and len(bf_row) >= 3 and bf_row[2]:
            fid = bf_row[2][2]
            file_fid_cache[path] = fid
            return fid
    file_fid_cache[path] = None
    return None


def extract_tags_and_evacuate_cs(cs_obj: ChangeSet) -> None:
    """Extract bridge tags, preserve resolved symbols, and purge internal AST buffers from an executed ChangeSet."""
    c_path = getattr(cs_obj, "current_path", None)
    if not c_path:
        return
    fid = get_fid_for_path(c_path)

    # 1. Preserve resolved symbol map before internal buffers are cleared
    if hasattr(cs_obj, "symbol_dict") and cs_obj.symbol_dict:
        resolved_symbols = {}
        cs_res = getattr(cs_obj, "cs_result", None)
        cs_ops = getattr(cs_obj, "cs", None)
        for (sym_name, sym_type), op_pos in cs_obj.symbol_dict.items():
            ast_id = None
            if cs_res and op_pos < len(cs_res) and cs_res[op_pos] is not None:
                res_row = cs_res[op_pos]
                if len(res_row) > 0 and isinstance(res_row[0], int):
                    ast_id = res_row[0]
            elif cs_ops and op_pos < len(cs_ops) and cs_ops[op_pos] is not None:
                op = cs_ops[op_pos]
                if len(op) >= 3 and len(op[2]) > 0 and isinstance(op[2][0], int):
                    ast_id = op[2][0]
            if ast_id is not None:
                resolved_symbols[(sym_name, sym_type)] = ast_id
        cs_obj.resolved_symbols = resolved_symbols

        # Register resolved symbols into GreatProcessor per-file symbol tables for O(1) lookups
        gp_ref = getattr(cs_obj, "gp", None) or getattr(G, "GP", None)
        if gp_ref is not None:
            if hasattr(gp_ref, "file_symbols") and isinstance(gp_ref.file_symbols, dict):
                gp_ref.file_symbols[c_path] = resolved_symbols
            if hasattr(gp_ref, "file_names") and isinstance(gp_ref.file_names, dict):
                gp_ref.file_names[c_path] = {s_name: a_id for (s_name, _), a_id in resolved_symbols.items()}
            if hasattr(gp_ref, "_ast_staged_symbols") and isinstance(gp_ref._ast_staged_symbols, dict):
                gp_ref._ast_staged_symbols.update(resolved_symbols)
            if hasattr(gp_ref, "_ast_staged_names") and isinstance(gp_ref._ast_staged_names, dict):
                for (s_name, _s_type), a_id in resolved_symbols.items():
                    if s_name not in gp_ref._ast_staged_names:
                        gp_ref._ast_staged_names[s_name] = a_id

    # 2. Extract bridge tags with resolved tag_id from cs_result
    if fid is not None and not hasattr(cs_obj, "pre_extracted_tags"):
        file_tags = []
        cs_ops = getattr(cs_obj, "cs", [])
        cs_res = getattr(cs_obj, "cs_result", [])
        for i, op in enumerate(cs_ops):
            if op and len(op) >= 3 and op[0] == m_bridge_tag.table_id:
                cols = op[2]
                if len(cols) >= 4:
                    tag_id = None
                    if i < len(cs_res) and cs_res[i] is not None and len(cs_res[i]) >= 2:
                        res_val = cs_res[i][1]
                        if isinstance(res_val, int):
                            tag_id = res_val
                    if tag_id is None and not isinstance(cols[1], tuple):
                        tag_id = cols[1]

                    line_s = cols[2] if isinstance(cols[2], int) else 1
                    line_e = cols[3] if isinstance(cols[3], int) else line_s
                    if tag_id is not None:
                        file_tags.append((tag_id, fid, line_s, line_e))
        cs_obj.pre_extracted_tags = file_tags

    # 3. Evacuate internal AST buffers and bloat
    if hasattr(cs_obj, "cs") and isinstance(cs_obj.cs, list):
        cs_obj.cs.clear()
    if hasattr(cs_obj, "store_dict") and isinstance(cs_obj.store_dict, dict):
        cs_obj.store_dict.clear()
    if hasattr(cs_obj, "cs_result") and isinstance(cs_obj.cs_result, list):
        cs_obj.cs_result.clear()
    if hasattr(cs_obj, "symbol_dict") and isinstance(cs_obj.symbol_dict, dict):
        cs_obj.symbol_dict.clear()
    if hasattr(cs_obj, "foreign_deps") and isinstance(cs_obj.foreign_deps, set):
        cs_obj.foreign_deps.clear()
    if hasattr(cs_obj, "unresolved_indices") and cs_obj.unresolved_indices:
        cs_obj.unresolved_indices.clear()
    if hasattr(cs_obj, "clear_bloat"):
        cs_obj.clear_bloat()
    else:
        cs_obj.gp = None
        cs_obj.mf = None
        cs_obj.parsers = {}
        cs_obj.batch_cs_dict = None
        cs_obj.file = None


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
    gp.init_cs_dict()
    if create_new_vid(version):
        logger.info(COLOR.yellow(f"=======================Version '{version}' already exists in DB. Skipping======================="))
        return

    logger.info(COLOR.green(f"=======================Working on {version}======================="))

    # Ensure TableEngine in-memory indexing is enabled for the new update cycle
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = True

    # -------------------------------------------------------------------------
    # STEP 2: Ensure performance B-tree indexes are active for worker queries (in parallel)
    # -------------------------------------------------------------------------
    performance_indexes = (
        ("ast_index", m_ast, (m_ast.name, m_ast.type_id)),
        ("file_name_index", m_file_name, (m_file_name.fname,)),
        ("bridge_tag_fid_idx", m_bridge_tag, (m_bridge_tag.fid, m_bridge_tag.tag_id)),
        ("file_ref_target_idx", m_file_reference, (m_file_reference.vid, m_file_reference.target_fnid, m_file_reference.ref_type)),
        ("file_ref_source_idx", m_file_reference, (m_file_reference.vid, m_file_reference.source_fid)),
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
    # STEP 5 & 6: Spawn multicore workers to parse files & stream ChangeSet execution
    # -------------------------------------------------------------------------
    chunk_commit_interval = 300 if G.VERY_LOW_MEMORY_MODE else (500 if G.LOW_MEMORY_MODE else 1000)
    scheduler = DependencyScheduler(gp, chunk_commit_interval=chunk_commit_interval, streaming=True)
    trigger_multicore(scheduler=scheduler)
    scheduler.finalize()

    reclaim_system_memory()

    # -------------------------------------------------------------------------
    # STEP 6.1: Resolve Symbolic Links & Alias directly in m_bridge_file
    # -------------------------------------------------------------------------
    processing_symlinks()

    # Disable TableEngine in-memory secondary indexing for write-only batch staging phases
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = False

    # -------------------------------------------------------------------------
    # STEP 6.5: Parse Git Commits, Multi-Contributors & Bridge Tags to Commits
    # -------------------------------------------------------------------------
    processing_git_commits(version)

    # -------------------------------------------------------------------------
    # STEP 6.6: Parse Kbuild/Makefiles, Documentation & Cross-File References
    # -------------------------------------------------------------------------
    processing_file_references(version)

    # -------------------------------------------------------------------------
    # STEP 6.7: Batch Match Maintainer Sections & Populate m_maintainer_file
    # -------------------------------------------------------------------------
    processing_maintainer_files(version)

    if G.PROFILING_ENABLED and gp.ChangeSet_Dict:
        from core.Profiler import format_profiling_report
        profilers = [cs.profiler for cs in gp.ChangeSet_Dict.values() if getattr(cs, "profiler", None)]
        if profilers:
            print(format_profiling_report(profilers, title=f"UPDATE CYCLE PROFILE: {version}"))

    gp.reset_cs()
    reclaim_system_memory()

    # -------------------------------------------------------------------------
    # STEP 7: Drop secondary indexes, commit transactions, and rebuild indexes
    # -------------------------------------------------------------------------
    if getattr(G.TE, "db", None) is not None and hasattr(G.TE.db, "cnx") and G.TE.db.cnx is not None:
        try:
            G.TE.db.cnx.commit()
        except Exception:
            pass

    if gp.VID == 1:
        with G.DB() as db:
            db.remove_indexes(tuple((item[0],item[1]) for item in performance_indexes))

    G.TE.commit_all(update_in_mem_indexes=False)

    if gp.VID == 1:
        with G.DB() as db:
            db.create_indexes(performance_indexes)

    file_fid_cache.clear()
    file_fnid_cache.clear()
    MF.clear_version_cache(version)
    MF.trim_version(keep=1)
    G.TE.close()
    if hasattr(G.TE, "update_in_mem_indexes"):
        G.TE.update_in_mem_indexes = True
    reclaim_system_memory()
    return


_INCLUDE_REGEX = re.compile(r'^\s*#\s*include\s*["<]([^">]+)[">]', re.M)


def order_changed_files(regular_files: list[str], working_dir: str | None) -> list[str]:
    """Topologically sort changed files so header files (.h) are parsed before dependent .c/.S files."""
    if not regular_files or not working_dir:
        return regular_files

    header_items = []
    other_items = []
    header_path_to_item = {}

    for item in regular_files:
        fpath = item.split("\t")[-1]
        if fpath.endswith((".h", ".hpp", ".hxx")):
            header_items.append(item)
            header_path_to_item[fpath] = item
        else:
            other_items.append(item)

    if len(header_items) <= 1:
        return header_items + other_items

    # Build dependency graph between changed headers
    # edge A -> B means B includes A (A must be parsed before B)
    graph = defaultdict(set)
    in_degree = defaultdict(int)
    for fpath in header_path_to_item:
        in_degree[fpath] = 0

    for fpath, item in header_path_to_item.items():
        if item.startswith("D"):
            continue
        full_path = os.path.join(working_dir, fpath)
        try:
            with open(full_path, "r", encoding="latin-1", errors="ignore") as f:
                content = f.read(131072)  # Read up to 128KB
        except Exception:
            continue

        for inc_match in _INCLUDE_REGEX.finditer(content):
            inc_target = inc_match.group(1)
            target_fpath = None
            if inc_target in header_path_to_item:
                target_fpath = inc_target
            elif f"include/{inc_target}" in header_path_to_item:
                target_fpath = f"include/{inc_target}"
            else:
                rel_candidate = os.path.normpath(os.path.join(os.path.dirname(fpath), inc_target))
                if rel_candidate in header_path_to_item:
                    target_fpath = rel_candidate

            if target_fpath and target_fpath != fpath:
                if fpath not in graph[target_fpath]:
                    graph[target_fpath].add(fpath)
                    in_degree[fpath] += 1

    # Kahn's algorithm for topological sort
    zero_in = deque([f for f, deg in in_degree.items() if deg == 0])
    sorted_header_paths = []

    while zero_in:
        node = zero_in.popleft()
        sorted_header_paths.append(node)
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                zero_in.append(neighbor)

    # If cyclic dependencies exist, append remaining headers in their original order
    if len(sorted_header_paths) < len(header_path_to_item):
        seen = set(sorted_header_paths)
        for item in header_items:
            fpath = item.split("\t")[-1]
            if fpath not in seen:
                sorted_header_paths.append(fpath)
                seen.add(fpath)

    sorted_headers = [header_path_to_item[fpath] for fpath in sorted_header_paths]
    return sorted_headers + other_items


class DependencyScheduler:
    """Dependency-driven ChangeSet resolution and execution scheduler with two-stage streaming ingestion."""

    def __init__(self, gp_ref: GreatProcessor, chunk_commit_interval: int = 0, streaming: bool = True) -> None:
        self.gp = gp_ref
        self.streaming = streaming
        self.deferred_queue: deque[str] = deque()
        self.completed: set[str] = set()
        self.executed_count = 0
        self.chunk_commit_interval = chunk_commit_interval if chunk_commit_interval > 0 else 1000

    def ingest_batch(self, batch_dict: dict[str, ChangeSet], is_header_stage: bool = False) -> None:
        """Ingest newly arrived ChangeSets from a worker batch into gp.ChangeSet_Dict."""
        # 1. Extract lightweight foreign dependencies into gp.file_deps before compression
        for path, cs_obj in batch_dict.items():
            f_deps = getattr(cs_obj, "foreign_deps", None)
            if f_deps:
                self.gp.file_deps[path] = set(f_deps)
            else:
                self.gp.file_deps[path] = set()

        purged = execute_and_purge(batch_dict)
        self.gp.ChangeSet_Dict.update(purged)
        for path, cs_obj in purged.items():
            if getattr(cs_obj, "cs_processed", False):
                self.completed.add(path)

        if not is_header_stage:
            # During source stage (or standalone test ingestion), execute ChangeSets immediately in streaming mode!
            for path, cs_obj in purged.items():
                if path in self.completed:
                    continue
                cs_obj.gp = self.gp
                if cs_obj.execute():
                    self.completed.add(path)
                    self.executed_count += 1
                    extract_tags_and_evacuate_cs(cs_obj)
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    if self.executed_count % self.chunk_commit_interval == 0:
                        G.TE.commit_all()
                        reclaim_system_memory()
                else:
                    # Unresolvable reference: defer to final wave pass
                    if hasattr(cs_obj, "clear_bloat"):
                        cs_obj.clear_bloat()
                    else:
                        cs_obj.gp = None
                    self.gp.ChangeSet_Dict[path] = cs_obj
                    self.deferred_queue.append(path)

    def resolve_headers(self) -> None:
        """Resolve all remaining header ChangeSets in wave order, breaking circular deadlocks with stubs."""
        execute_phase2_parallel_waves(self.gp, chunk_commit_interval=self.chunk_commit_interval, completed_set=self.completed)
        self.completed.update(self.gp.ChangeSet_Dict.keys())
        self.executed_count = len(self.completed)
        G.TE.commit_all()
        reclaim_system_memory()

    def finalize(self) -> None:
        """Finalize resolution of all remaining ChangeSets via Phase 2 wave-based execution."""
        # 1. Drain deferred queue (rare C-to-C references) with force_stubs=True if needed
        while self.deferred_queue:
            current_cs = self.deferred_queue.popleft()
            if current_cs in self.completed:
                continue
            cs_obj = self.gp.ChangeSet_Dict.get(current_cs)
            if cs_obj is None:
                continue
            cs_obj.gp = self.gp
            cs_obj.execute(force_stubs=True)
            self.completed.add(current_cs)
            self.executed_count += 1
            extract_tags_and_evacuate_cs(cs_obj)
            self.gp.ChangeSet_Dict[current_cs] = cs_obj

        # 2. If any unexecuted ChangeSets remain, run wave resolution
        execute_phase2_parallel_waves(self.gp, chunk_commit_interval=self.chunk_commit_interval, completed_set=self.completed)
        self.completed.update(self.gp.ChangeSet_Dict.keys())
        self.executed_count = len(self.completed)
        G.TE.commit_all()
        reclaim_system_memory()



def execute_phase2_parallel_waves(
    gp: GreatProcessor,
    chunk_commit_interval: int = 0,
    completed_set: set[str] | None = None,
) -> None:
    """Execute unexecuted ChangeSets using in-process O(1) wave-based resolution.

    Eliminates 64GB memory spikes by tracking only path strings in remaining and deps (9MB DAG),
    decompressing ChangeSets on-demand from LRU cache per wave, and immediately evacuating AST
    buffers upon execution while committing to MariaDB every 1,000 ChangeSets.
    """
    if chunk_commit_interval <= 0:
        chunk_commit_interval = 1000

    if completed_set is None:
        completed_set = set()

    # If file_deps is not yet populated (e.g. synthetic test), populate from ChangeSet_Dict
    if not gp.file_deps and gp.ChangeSet_Dict:
        for path, cs_obj in gp.ChangeSet_Dict.items():
            f_deps = getattr(cs_obj, "foreign_deps", None)
            if f_deps is None:
                f_deps = set()
                cs_ops = getattr(cs_obj, "cs", [])
                for op in cs_ops:
                    if op and len(op) >= 3 and is_data_unsafe(op[2]):
                        for col in op[2]:
                            if type(col) is tuple and len(col) == 3 and col[1] == OP_REF and col[2] and col[2][0] == REF_FILE:
                                f_deps.add(col[2][1])
                cs_obj.foreign_deps = f_deps
            gp.file_deps[path] = set(f_deps)
            if getattr(cs_obj, "cs_processed", False):
                completed_set.add(path)

    # 1. Identify all remaining unexecuted paths (<2.5 MB)
    remaining: set[str] = set()
    for path in gp.file_deps.keys():
        if path in completed_set:
            continue
        cs = gp.ChangeSet_Dict.get(path)
        if cs is None:
            continue
        if not getattr(cs, "cs_processed", False) or (getattr(cs, "unresolved_indices", None) is not None and len(cs.unresolved_indices) > 0):
            remaining.add(path)
        else:
            completed_set.add(path)

    if not remaining:
        return

    logger.info(COLOR.cyan(f"[Phase 2] {len(remaining)} unexecuted ChangeSets entering wave resolution."))

    # 2. Build lightweight dependency DAG and inverted dependent mapping (~10 MB)
    dependents: dict[str, set[str]] = defaultdict(set)
    deps: dict[str, set[str]] = {}
    for path in remaining:
        f_deps = {target for target in gp.file_deps.get(path, ()) if target in remaining and target != path}
        deps[path] = f_deps
        for target in f_deps:
            dependents[target].add(path)

    wave_num = 0
    executed_count = 0
    last_commit_count = 0
    t_phase2_start = time.perf_counter()
    resolution_succeeded = False

    try:
        while remaining:
            wave_num += 1
            t_wave_start = time.perf_counter()
            # Wave selection: files with no unexecuted dependencies in remaining
            wave = [p for p in remaining if not deps.get(p)]

            if not wave:
                # Deadlock detected: cycle breaking on candidate with most dependents
                dep_counts = {p: len(dependents.get(p, ())) for p in remaining}
                best_cand = max(remaining, key=lambda k: (dep_counts[k], -len(getattr(gp.ChangeSet_Dict.get(k), "cs", ()) or ())))
                cand_cs = gp.ChangeSet_Dict[best_cand]
                cand_cs.gp = gp
                unresolved_cnt = len(cand_cs.unresolved_indices) if getattr(cand_cs, "unresolved_indices", None) is not None else len(getattr(cand_cs, "cs", ()) or ())
                logger.warning(COLOR.yellow(
                    f"[Phase 2] Breaking circular dependency deadlock: force-resolving stub for '{best_cand}' "
                    f"({dep_counts[best_cand]} dependents, {unresolved_cnt} ops remaining)"
                ))

                cand_cs.execute(force_stubs=True)
                extract_tags_and_evacuate_cs(cand_cs)
                gp.ChangeSet_Dict[best_cand] = cand_cs
                remaining.remove(best_cand)
                completed_set.add(best_cand)
                if best_cand in deps:
                    del deps[best_cand]
                for waiter in dependents.pop(best_cand, ()):
                    if waiter in deps:
                        deps[waiter].discard(best_cand)
                executed_count += 1

                if executed_count - last_commit_count >= chunk_commit_interval:
                    logger.info(COLOR.yellow(
                        f"[Phase 2] Chunk commit: committing batch at {executed_count} executed ChangeSets to database..."
                    ))
                    G.TE.commit_all()
                    reclaim_system_memory()
                    last_commit_count = executed_count
                continue

            logger.info(COLOR.cyan(
                f"[Phase 2] Wave {wave_num}: resolving {len(wave)} files in parallel ({len(remaining)} remaining)..."
            ))

            for path in wave:
                cs = gp.ChangeSet_Dict[path]
                cs.gp = gp
                cs.execute()
                extract_tags_and_evacuate_cs(cs)
                gp.ChangeSet_Dict[path] = cs
                remaining.remove(path)
                completed_set.add(path)
                if path in deps:
                    del deps[path]
                for waiter in dependents.pop(path, ()):
                    if waiter in deps:
                        deps[waiter].discard(path)
                executed_count += 1

                if executed_count - last_commit_count >= chunk_commit_interval:
                    logger.info(COLOR.yellow(
                        f"[Phase 2] Chunk commit: committing batch at {executed_count} executed ChangeSets to database..."
                    ))
                    G.TE.commit_all()
                    reclaim_system_memory()
                    last_commit_count = executed_count

            wave_elapsed = time.perf_counter() - t_wave_start
            velocity = len(wave) / max(0.001, wave_elapsed)
            logger.info(COLOR.green(
                f"[Phase 2] Wave {wave_num} resolved {len(wave)} files in {wave_elapsed:.2f}s "
                f"({velocity:.1f} files/s). {len(remaining)} files remaining."
            ))

        resolution_succeeded = True
    finally:
        if resolution_succeeded and executed_count > last_commit_count:
            logger.info(COLOR.yellow(
                f"[Phase 2] Chunk commit: committing final batch at {executed_count} executed ChangeSets to database..."
            ))
            G.TE.commit_all()
            reclaim_system_memory()

    total_phase2_time = time.perf_counter() - t_phase2_start
    avg_velocity = executed_count / max(0.001, total_phase2_time)
    logger.info(COLOR.green(
        f"[Phase 2] Completed resolution of {executed_count} ChangeSets across {wave_num} waves "
        f"in {total_phase2_time:.1f}s ({avg_velocity:.1f} files/s)."
    ))


def trigger_multicore(batch_size: int | None = None, scheduler: DependencyScheduler | None = None) -> None:
    """Distribute file parsing across parallel worker processes in two persistent stages: headers first, then sources."""
    change_list = gp.Change_List or []
    working_dir = MF.version_dict.get(gp.Version_Name)

    regular_files = []
    symlink_files = []

    for item in change_list:
        if not item:
            continue
        parts = item.split("\t")
        op = parts[0]
        fpath = parts[-1]
        if not op.startswith("D") and working_dir and os.path.islink(os.path.join(working_dir, fpath)):
            symlink_files.append(item)
        else:
            regular_files.append(item)

    if not hasattr(gp, "Symlink_List") or gp.Symlink_List is None:
        gp.Symlink_List = []
    gp.Symlink_List.extend(symlink_files)

    # Order regular files: header DAG first
    regular_files = order_changed_files(regular_files, working_dir)
    gp._changed_paths_set = set(item.split("\t")[-1] for item in (regular_files + symlink_files) if item)

    header_files = []
    source_files = []
    for item in regular_files:
        fpath = item.split("\t")[-1]
        if fpath.endswith((".h", ".hpp", ".hxx")):
            header_files.append(item)
        else:
            source_files.append(item)

    total_files = len(regular_files)
    total_headers = len(header_files)
    total_sources = len(source_files)

    if batch_size is None:
        if G.VERY_LOW_MEMORY_MODE:
            batch_size = 25
        elif G.LOW_MEMORY_MODE:
            batch_size = 50
        else:
            batch_size = 200

    if G.VERY_LOW_MEMORY_MODE:
        num_workers = max(1, min(2, int(G.CPUS // 4)))
    else:
        num_workers = max(1, int(G.CPUS - 1))

    total_header_batches = (total_headers + batch_size - 1) // batch_size if total_headers > 0 else 0
    total_source_batches = (total_sources + batch_size - 1) // batch_size if total_sources > 0 else 0
    total_batches = total_header_batches + total_source_batches

    logger.info(
        f"Distributing {total_files} changed files ({total_headers} headers, {total_sources} sources) in {total_batches} batches "
        f"(batch size: {batch_size}, memory mode: {G.MEMORY_MODE}) across {num_workers} parallel workers"
    )

    # If no change within this version
    if total_files == 0:
        processing_dirs()
        processing_unchanges()
        if scheduler is not None and gp.ChangeSet_Dict:
            scheduler.ingest_batch(dict(gp.ChangeSet_Dict), is_header_stage=False)
        return

    # Close active DB connection in parent before forking child worker processes
    # to prevent inherited socket file descriptor sharing / corruption across fork
    if getattr(G.TE, "db", None) is not None:
        try:
            G.TE.db.close()
        except Exception:
            pass
        G.TE.db = None

    task_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    error_queue = multiprocessing.Queue()

    # Enqueue all header batches first, followed immediately by all source batches
    for i in range(0, total_headers, batch_size):
        task_queue.put((i // batch_size, header_files[i : i + batch_size]))

    for i in range(0, total_sources, batch_size):
        task_queue.put((total_header_batches + i // batch_size, source_files[i : i + batch_size]))

    # Enqueue termination sentinels for workers
    for _ in range(num_workers):
        task_queue.put(None)

    processes = []
    for worker_id in range(num_workers):
        p = multiprocessing.Process(
            target=file_processing_worker,
            args=(task_queue, error_queue, result_queue, gp.VID, gp, MF, worker_id),
        )
        processes.append(p)
        p.start()

    # Re-initialize dedicated DB connection in parent process after workers have forked
    G.TE.start_new_db(G.DB)

    # needs to be try: protected
    processing_dirs()
    processing_unchanges()

    # Ingest initial directory and unchanged ChangeSets staged before worker results
    if scheduler is not None and gp.ChangeSet_Dict:
        scheduler.ingest_batch(dict(gp.ChangeSet_Dict), is_header_stage=True)

    # Stage 1: Receive all header batches
    header_batches_received = 0
    early_source_batches: list[tuple[int, bytes]] = []
    source_batches_received = 0
    finished_workers = 0
    t_stage1_start = time.perf_counter()

    while header_batches_received < total_header_batches:
        try:
            item = result_queue.get()
            if item is None:
                finished_workers += 1
                continue
            batch_id, compressed = item
            if batch_id < total_header_batches:
                header_batches_received += 1
                if compressed is not None:
                    try:
                        raw_bytes = zlib.decompress(compressed)
                        partial_dict = pickle.loads(raw_bytes)
                        del raw_bytes
                    except Exception:
                        partial_dict = pickle.loads(compressed)

                    if scheduler is not None:
                        scheduler.ingest_batch(partial_dict, is_header_stage=True)
                    else:
                        gp.ChangeSet_Dict.update(execute_and_purge(partial_dict))
                    del partial_dict

                if header_batches_received % 10 == 0 or header_batches_received == total_header_batches:
                    elapsed = time.perf_counter() - t_stage1_start
                    velocity = header_batches_received / max(0.001, elapsed)
                    logger.info(
                        f"[Stage 1 - Headers] Ingested header batch {header_batches_received}/{total_header_batches} "
                        f"({velocity:.1f} batches/s, elapsed {elapsed:.1f}s)"
                    )
            else:
                # Source batch finished ahead of slower header batch: buffer compressed payload
                if compressed is not None:
                    early_source_batches.append((batch_id, compressed))
        except Exception as e:
            logger.error(f"Error reading header batch result: {e}")
            break
        reclaim_system_memory()

    # Resolve Stage 1: All headers in wave order
    if scheduler is not None and total_headers > 0:
        logger.info(COLOR.cyan(f"[Stage 1 - Headers] Resolving {total_headers} header files in wave order..."))
        scheduler.resolve_headers()
        logger.info(COLOR.green(f"[Stage 1 - Headers] Header resolution complete. {len(gp.file_symbols)} header symbol tables indexed."))

    # Stage 2: Stream source batches
    t_stage2_start = time.perf_counter()

    # First drain any early source batches that were buffered
    if early_source_batches:
        logger.info(f"[Stage 2 - Sources] Streaming {len(early_source_batches)} buffered source batches...")
        for batch_id, compressed in early_source_batches:
            source_batches_received += 1
            try:
                raw_bytes = zlib.decompress(compressed)
                partial_dict = pickle.loads(raw_bytes)
                del raw_bytes
            except Exception:
                partial_dict = pickle.loads(compressed)

            if scheduler is not None:
                scheduler.ingest_batch(partial_dict, is_header_stage=False)
            else:
                gp.ChangeSet_Dict.update(execute_and_purge(partial_dict))
            del partial_dict
        early_source_batches.clear()
        reclaim_system_memory()

    while finished_workers < num_workers:
        try:
            item = result_queue.get()
            if item is None:
                finished_workers += 1
                continue
            batch_id, compressed = item
            source_batches_received += 1
            if compressed is not None:
                try:
                    raw_bytes = zlib.decompress(compressed)
                    partial_dict = pickle.loads(raw_bytes)
                    del raw_bytes
                except Exception:
                    partial_dict = pickle.loads(compressed)

                if scheduler is not None:
                    scheduler.ingest_batch(partial_dict, is_header_stage=False)
                else:
                    gp.ChangeSet_Dict.update(execute_and_purge(partial_dict))
                del partial_dict

            if source_batches_received % 10 == 0 or source_batches_received == total_source_batches:
                elapsed = time.perf_counter() - t_stage2_start
                velocity = source_batches_received / max(0.001, elapsed)
                logger.info(
                    f"[Stage 2 - Sources] Streamed source batch {source_batches_received}/{total_source_batches} "
                    f"({velocity:.1f} batches/s, {finished_workers}/{num_workers} workers done, elapsed {elapsed:.1f}s)"
                )
        except Exception as e:
            logger.error(f"Error reading source batch result: {e}")
            break
        reclaim_system_memory()

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
    reclaim_system_memory()

    return


def main() -> None:
    """Set the plan for what version to parse."""
    setup_memory_limit(60.0)
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
                missing_tables = [tbl for tbl in gp.Table_Array if tbl.table_name in missing]
                db.create_table(missing_tables)
                try:
                    db.create_index("v_main_index", m_v_main, (m_v_main.vname,))
                except Exception:
                    pass

    try:
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
    except MemoryError:
        logger.critical(COLOR.red("FATAL: Memory limit of 60.0 GB (RLIMIT_AS) exceeded! Terminating execution cleanly."))
        sys.exit(1)

    logger.info("We are done! Closing")
    G.emergency_shutdown(0)
    return


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
        "-f", "--fidelity",
        dest="fidelity",
        action="store_true",
        default=None,
        help="Display full tag text & source code fidelity audit report across test files (default: True)",
    )
    parser.add_argument(
        "--no-fidelity",
        dest="fidelity",
        action="store_false",
        help="Disable tag text & source code fidelity audit report",
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
        "--db", "--db-engine",
        dest="db_engine",
        default=None,
        choices=["mariadb", "mysql", "mock", "mockdb", "inmemory"],
        help="Select database backend engine (default: from config or mariadb)",
    )
    parser.add_argument(
        "--te", "--table-engine",
        dest="table_engine",
        default=None,
        choices=["cached", "direct", "tecacheddb", "tedirectdb"],
        help="Select Table Engine architecture backend (default: from config or cached)",
    )
    args = parser.parse_args()

    init_config(args.config)
    parser_cfg = get_parser_config()
    db_cfg = get_db_config()

    if args.very_low_mem:
        G.MEMORY_MODE = "very_low"
    elif args.low_mem:
        G.MEMORY_MODE = "low"
    elif parser_cfg.get("memory_mode"):
        G.MEMORY_MODE = parser_cfg["memory_mode"]

    if args.fidelity is None:
        args.fidelity = parser_cfg.get("fidelity", True)

    gp.init_cs_dict()

    if args.profile:
        G.PROFILING_ENABLED = True

    effective_db_engine = args.db_engine or db_cfg.get("engine") or "mariadb"
    effective_table_engine = args.table_engine or parser_cfg.get("table_engine") or "cached"

    G.DB = get_db_engine(effective_db_engine)
    G.TE = get_table_engine(effective_table_engine)()

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
        except Exception as e:
            logger.debug(f"DB check before unit tests: {e}")

        target = args.unit_test if args.unit_test != "" else None
        from tests.test_c_ast import run_c_ast_tests
        if target:
            code = run_c_ast_tests(target, profile=args.profile, fidelity=args.fidelity, table_engine=args.table_engine)
            sys.exit(code)

        # 1. Multi-Core C-AST regression suite
        c_ast_code = run_c_ast_tests(None, profile=args.profile, fidelity=args.fidelity, table_engine=args.table_engine)

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
            "tests.test_raw_ast",
            "tests.test_rust_ast",
            "tests.test_config",
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
        code = run_c_ast_tests(args.Test, profile=args.profile, fidelity=args.fidelity, table_engine=args.table_engine)
        sys.exit(code)

    return args


def create_new_vid(name: str) -> bool:
    """Register or synchronize active version in m_v_main. Returns True if version already exists and is complete."""
    with G.DB() as db:
        existing = db.select(m_v_main, (None, name))
        if existing:
            # Verify if this version actually contains committed bridge_file records
            bf_sample = db.select(m_bridge_file, (existing[0], None, None))
            if bf_sample:
                gp.Old_VID = gp.VID
                gp.VID = existing[0]
                gp.Old_Version_Name = gp.Version_Name
                gp.Version_Name = name
                return True
            else:
                logger.warning(
                    f"Version '{name}' exists in m_v_main (VID {existing[0]}) but has no committed files. Reparsing..."
                )
                gp.Old_VID = gp.VID
                gp.VID = existing[0]
                gp.Old_Version_Name = gp.Version_Name
                gp.Version_Name = name
                return False

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

                old_working_dir = getattr(MF, "version_dict", {}).get(getattr(gp, "Old_Version_Name", None))
                is_symlink_del = bool(old_working_dir and os.path.islink(os.path.join(old_working_dir, CS.current_path)))

                if not is_symlink_del:
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
                    CS.get_file_type() if hasattr(CS, "get_file_type") else type_check(CS.current_path),
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
                CS.get_file_type() if hasattr(CS, "get_file_type") else type_check(CS.current_path),
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
            CS.get_file_type() if hasattr(CS, "get_file_type") else type_check(CS.current_path),
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
    sys.setrecursionlimit(50000)
    # Ensure dedicated DB connection per worker process to avoid socket sharing across fork
    try:
        G.TE.start_new_db(G.DB)
    except Exception as e:
        logger.error(f"Worker {worker_id} failed to initialize DB connection: {e}")

    try:
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

            for f_idx, changed_file in enumerate(changed_files):
                try:
                    CS = ChangeSet(changed_file)
                    CS.current_vid = vid
                    CS.gp = gp_ref
                    CS.mf = mf_ref
                    CS.batch_cs_dict = batch_cs_dict
                    G.CURRENT_PARSING_FILE = CS.current_path

                    default_processing(CS)
                    CS.parse()

                    # Pre-unpack intra-file AST view schemas in parallel workers to distribute CPU load
                    if not G.VERY_LOW_MEMORY_MODE:
                        CS.preprocess_ref_views()

                    # In normal memory mode, pre-resolve unchanged DB dependencies to prune foreign_deps
                    if not G.LOW_MEMORY_MODE and not G.VERY_LOW_MEMORY_MODE:
                        CS.prune_unchanged_dependencies()

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

                # Periodic memory reclamation every 25 files to return heap pages to OS
                if (f_idx + 1) % 25 == 0:
                    reclaim_system_memory()

            if batch_cs_dict:
                try:
                    raw_bytes = pickle.dumps(batch_cs_dict, protocol=pickle.HIGHEST_PROTOCOL)
                    compressed = zlib.compress(raw_bytes, level=1)
                    result_queue.put((batch_id, compressed))
                    del raw_bytes
                    del compressed
                except Exception as e:
                    logger.error(COLOR.red(f"Worker {worker_id} failed to serialize batch {batch_id}: {e}"))
                    error_queue.put((f"Batch-{batch_id}", str(e), traceback.format_exc()))
                    result_queue.put((batch_id, None))
            else:
                result_queue.put((batch_id, None))

            del batch_cs_dict
            reclaim_system_memory()
    finally:
        if getattr(G.TE, "db", None) is not None:
            try:
                G.TE.db.close()
            except Exception:
                pass
            G.TE.db = None
        if G.LOW_MEMORY_MODE:
            reclaim_system_memory()

    result_queue.put(None)  # Worker done sentinel
    return


def file_processing(start: int, end: int | None, override_list: list[str] | None = None) -> None:
    """Process gp.Change_List (or override_list) and send CS into gp.ChangeSet_Dict."""
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
    """Process everything outside gp.Change_List with self-healing for legacy unparsed files."""
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
        logger.warning(f"Found {len(forgotten_new)} forgotten new files... Processing...")
        if G.OVERRIDE_FORGOTTEN_PRINT:
            logger.debug(forgotten_new)
        file_processing(0, 0, (f"A\t{x}" for x in forgotten_new))

    te_set = G.TE.set
    t_id = m_bridge_file.table_id
    vid = gp.VID
    old_vid = gp.Old_VID

    working_dir = MF.version_dict.get(gp.Version_Name)
    missing_unchanged = []
    for unchanged in unchanged_set:
        un_m_file_name = m_file_name.get(None, unchanged)
        if un_m_file_name is None:
            missing_unchanged.append(unchanged)
            continue
        un_m_bridge_file = m_bridge_file.get(old_vid, un_m_file_name[2][0], None)

        if un_m_bridge_file is None:
            missing_unchanged.append(unchanged)
            continue

        # If unchanged is a symlink, defer resolution to processing_symlinks so target fid stays in sync
        if working_dir and os.path.islink(os.path.join(working_dir, unchanged)):
            if not hasattr(gp, "Symlink_List") or gp.Symlink_List is None:
                gp.Symlink_List = []
            gp.Symlink_List.append(f"U\t{unchanged}")
            continue

        te_set(
            t_id,
            (
                vid,
                un_m_file_name[2][0],
                un_m_bridge_file[2][2],
            ),
        )
        file_fid_cache[unchanged] = un_m_bridge_file[2][2]

    if missing_unchanged:
        missing_syms = []
        missing_regs = []
        for x in missing_unchanged:
            if working_dir and os.path.islink(os.path.join(working_dir, x)):
                missing_syms.append(f"A\t{x}")
            else:
                missing_regs.append(f"A\t{x}")

        if missing_regs:
            logger.warning(
                f"Found {len(missing_regs)} files in unchanged_set missing from prior DB version (Old_VID {old_vid}). "
                f"Self-healing by processing as new additions in VID {vid}..."
            )
            file_processing(0, 0, missing_regs)

        if missing_syms:
            if not hasattr(gp, "Symlink_List") or gp.Symlink_List is None:
                gp.Symlink_List = []
            gp.Symlink_List.extend(missing_syms)

    return


def processing_dirs() -> None:  # noqa: C901
    """Process dirs."""
    dir_list = set(MF.get_dir_list(gp.Version_Name))

    if gp.Old_VID != 0:

        old_dir_list = set(MF.get_dir_list(gp.Old_Version_Name))
        new_dir_list = dir_list - old_dir_list
        unchanged_dirs = dir_list & old_dir_list
        deleted_dirs = old_dir_list - dir_list

        # Unchanged dirs
        for single_dir in unchanged_dirs:
            # Get m_file_name
            un_m_file_name = m_file_name.get(None, single_dir)
            if un_m_file_name is None:
                new_dir_list.add(single_dir)
                logger.warning(f"Unchanged dir missing m_file_name: '{single_dir}', self-healing as new addition...")
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, un_m_file_name[2][0], None)
            if old_m_bridge_file is None:
                new_dir_list.add(single_dir)
                logger.warning(
                    f"Unchanged dir '{single_dir}' missing prior m_bridge_file (Old_VID {gp.Old_VID}). "
                    f"Self-healing as new addition in VID {gp.VID}..."
                )
                continue
            G.TE.set(
                m_bridge_file.table_id,
                (
                    gp.VID,
                    un_m_file_name[2][0],
                    old_m_bridge_file[2][2],
                ),
            )

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
        for single_dir in deleted_dirs:
            # Get m_file_name
            if (del_m_file_name := m_file_name.get(None, single_dir)) is None:
                continue
            # Get old_m_bridge_file
            old_m_bridge_file = m_bridge_file.get(gp.Old_VID, del_m_file_name[2][0], None)
            if old_m_bridge_file is None:
                continue

            # 0 Update old FILE
            CS.store(m_file.update(
                old_m_bridge_file[2][2],
                None, gp.Old_VID,
                None,
                None,
                "D",
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


def processing_symlinks(override_symlinks: list[str] | None = None) -> None:
    """Resolve symbolic links by aliasing to target fid in m_bridge_file without duplicating AST tags."""
    symlink_list = override_symlinks if override_symlinks is not None else getattr(gp, "Symlink_List", None)
    if not symlink_list or gp.VID == 0:
        return

    working_dir = MF.version_dict.get(gp.Version_Name)
    if not working_dir:
        return

    te_set = G.TE.set
    t_id = m_bridge_file.table_id
    vid = gp.VID

    # Deduplicate paths while preserving order
    seen_paths = set()
    unique_symlinks = []
    for item in symlink_list:
        parts = item.split("\t")
        sym_path = parts[-1]
        if sym_path not in seen_paths:
            seen_paths.add(sym_path)
            unique_symlinks.append((parts[0], sym_path))

    fallback_files = []

    for op, symlink_path in unique_symlinks:
        full_p = os.path.join(working_dir, symlink_path)
        try:
            raw_target = os.readlink(full_p)
        except OSError:
            fallback_files.append(f"{op}\t{symlink_path}")
            continue

        # Normalize target relative to repository root
        norm_target = os.path.normpath(os.path.join(os.path.dirname(symlink_path), raw_target))
        if norm_target.startswith("./"):
            norm_target = norm_target[2:]

        # Lookup target fid in current version
        target_fid = get_fid_for_path(norm_target)

        if target_fid is not None:
            sym_fn_res = G.TE.set(m_file_name.table_id, (None, symlink_path))
            sym_fnid = sym_fn_res[0]
            te_set(t_id, (vid, sym_fnid, target_fid))
            file_fid_cache[symlink_path] = target_fid
            logger.info(f"Symlink aliased: '{symlink_path}' -> '{norm_target}' (fid {target_fid})")
        else:
            logger.info(
                f"Symlink target '{norm_target}' not found in active tree; falling back to raw processing for '{symlink_path}'"
            )
            fallback_op = "A" if op == "U" else op
            fallback_files.append(f"{fallback_op}\t{symlink_path}")

    if fallback_files:
        file_processing(0, 0, fallback_files)
        for fb in fallback_files:
            fb_path = fb.split("\t")[-1]
            if fb_path in gp.ChangeSet_Dict:
                cs_obj = gp.ChangeSet_Dict[fb_path]
                cs_obj.execute()
                extract_tags_and_evacuate_cs(cs_obj)

    if override_symlinks is None and hasattr(gp, "Symlink_List") and gp.Symlink_List:
        gp.Symlink_List.clear()


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

        # Collect tags for this file from ChangeSet operations or pre-extracted tags
        file_tags = getattr(cs_obj, "pre_extracted_tags", None)
        if file_tags is None:
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
    """Parse Makefile and Kbuild files for active version, map Kconfig symbols, and stage build references."""
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
    ref_count = 0
    for mk_path in makefile_paths:
        try:
            content = MF.get_file(mk_path, gp.Version_Name)
            if not content:
                continue
            mk_fid = get_fid_for_path(mk_path)
            dir_path = os.path.dirname(mk_path)
            bindings = kbuild_parser.parse_makefile_content(content, dir_path=dir_path)
            for b in bindings:
                target_fnid = get_fnid_for_path(b.source_file_rel)
                fid = get_fid_for_path(b.source_file_rel)
                if fid is None and b.source_file_rel.endswith(".c"):
                    alt_asm = b.source_file_rel[:-2] + ".S"
                    fid = get_fid_for_path(alt_asm)
                    if target_fnid is None:
                        target_fnid = get_fnid_for_path(alt_asm)

                # 1. Populate m_kconfig_kbuild for compiled source files
                if getattr(b, "ref_type", 3) == 3 and fid is not None:
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

                # 2. Populate m_file_reference for Kbuild compilations and Makefile includes/recursions
                if mk_fid is not None and target_fnid is not None:
                    ref_t = getattr(b, "ref_type", 3)
                    G.TE.set(
                        m_file_reference.table_id,
                        (
                            None,
                            gp.VID,
                            mk_fid,
                            target_fnid,
                            int(ref_t),
                            int(getattr(b, "source_line", 1)),
                            getattr(b, "details", "")[:255],
                        ),
                    )
                    ref_count += 1
        except Exception as e:
            logger.debug(f"Error parsing kbuild file '{mk_path}': {e}")

    logger.info(f"Staged {count} Kbuild symbol-to-source mappings and {ref_count} build references for version '{version}'.")


def processing_doc_references(version: str) -> None:
    """Scan documentation and text files for active version and map cross-file references into m_file_reference."""
    if gp.VID == 0:
        return

    from parser.doc_parser import DocReferenceScanner
    scanner = DocReferenceScanner()

    try:
        file_list_raw = MF.git_file_list(gp.Version_Name)
    except Exception as e:
        logger.debug(f"Failed to get git_file_list for doc reference parsing in {version}: {e}")
        return

    all_files = file_list_raw.splitlines() if file_list_raw else []
    doc_paths = [f for f in all_files if scanner.is_doc_candidate(f)]
    if not doc_paths:
        return

    logger.info(f"Processing {len(doc_paths)} documentation/text files for version '{version}'...")

    fnid_by_path: dict[str, int] = {}
    for p in all_files:
        fnid = get_fnid_for_path(p)
        if fnid is not None:
            fnid_by_path[p] = fnid

    count = 0
    for doc_p in doc_paths:
        try:
            doc_fid = get_fid_for_path(doc_p)
            if doc_fid is None:
                continue
            content = MF.get_file(doc_p, gp.Version_Name)
            if not content:
                continue
            refs = scanner.scan_file_content(content, doc_fid, fnid_by_path, source_path=doc_p)
            for r in refs:
                G.TE.set(
                    m_file_reference.table_id,
                    (
                        None,
                        gp.VID,
                        r.source_fid,
                        r.target_fnid,
                        int(FileRefType.Documentation),
                        int(r.line_no),
                        r.details[:255],
                    ),
                )
                count += 1
        except Exception as e:
            logger.debug(f"Error parsing documentation references in '{doc_p}': {e}")

    logger.info(f"Staged {count} documentation cross-file references into m_file_reference for version '{version}'.")


def processing_include_references(version: str) -> None:
    """Batch populate C/ASM and Kconfig include references into m_file_reference using set-based query."""
    if gp.VID == 0:
        return
    try:
        with G.DB() as db:
            cur = db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None
            if cur is None:
                return

            # Idempotent cleanup for this version's includes & Kconfig sources
            cur.execute(
                "DELETE FROM m_file_reference WHERE vid = %s AND ref_type IN (%s, %s);",
                (gp.VID, int(FileRefType.Include), int(FileRefType.Kconfig)),
            )

            sync_query = f"""
                INSERT IGNORE INTO m_file_reference (vid, source_fid, target_fnid, ref_type, line_no, details)
                SELECT STRAIGHT_JOIN DISTINCT bf.vid, bf.fid, inc.fnid,
                       CASE WHEN a.type_id IN ({ASTT.Kconfig_Source.value}, {ASTT.Kconfig_Rsource.value}) THEN {int(FileRefType.Kconfig)} ELSE {int(FileRefType.Include)} END AS ref_type,
                       bt.line_s,
                       LEFT(a.name, 255)
                FROM m_bridge_file bf
                JOIN m_bridge_tag bt ON bf.fid = bt.fid
                JOIN m_tag t ON bt.tag_id = t.tag_id
                JOIN m_ast_include inc ON t.ast_id = inc.ast_id
                JOIN m_ast a ON inc.ast_id = a.ast_id
                WHERE bf.vid = %s;
            """
            cur.execute(sync_query, (gp.VID,))
            inserted = cur.rowcount
            db.cnx.commit()
            cur.close()
            logger.info(f"Synchronized {inserted} include/Kconfig references into m_file_reference for version '{version}'.")
    except Exception as e:
        logger.warning(f"Error in processing_include_references for {version}: {e}", exc_info=True)


def processing_file_references(version: str) -> None:
    """Extract and consolidate all cross-file references (Kbuild, Makefiles, Documentation, Includes, Kconfig)."""
    if gp.VID == 0:
        return

    # 1. Version-scoped clean reset for idempotency across pipeline re-runs
    try:
        with G.DB() as db:
            cur = db.cnx.cursor() if hasattr(db, "cnx") and db.cnx else None
            if cur:
                cur.execute("DELETE FROM m_file_reference WHERE vid = %s;", (gp.VID,))
                db.cnx.commit()
                cur.close()
    except Exception as e:
        logger.debug(f"Note on clearing m_file_reference for {version}: {e}")

    # 2. Resynchronize TableEngine sequence next_id before staging fresh rows
    if hasattr(G.TE, "db") and G.TE.db:
        try:
            G.TE.next_id[m_file_reference.table_id] = G.TE.db.get_next_id(m_file_reference)
        except Exception:
            pass

    # 3. Parse Kbuild and Documentation files and stage into TableEngine
    processing_kbuild(version)
    processing_doc_references(version)

    # 4. Commit TableEngine queued rows immediately so they claim sequence IDs 1..N and empty the queue
    if m_file_reference.table_id in G.TE.queued_set and G.TE.queued_set[m_file_reference.table_id]:
        G.TE.commit(m_file_reference.table_id, update_in_mem_indexes=False)

    # 5. Populate C/ASM and Kconfig include references using set-based SQL (claims N+1..M via server AUTO_INCREMENT)
    processing_include_references(version)

    # 6. Resynchronize next_id so subsequent operations and commit_all have the accurate sequence boundary
    if hasattr(G.TE, "db") and G.TE.db:
        try:
            G.TE.next_id[m_file_reference.table_id] = G.TE.db.get_next_id(m_file_reference)
        except Exception:
            pass


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

    for sec in sections:
        if sec.name and sec.name not in sec_id_cache:
            get_sec_id_for_name(sec.name)

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

def execute_and_purge(cs_dict: dict) -> dict:
    """Execute non-foreign ChangeSets immediately and purge internal AST buffers to minimize RAM usage."""
    for CS in cs_dict.values():
        if not CS:
            continue
        c_path = getattr(CS, "current_path", None)
        if c_path and type_check(c_path) in T_NO_FOREIGN:
            if CS.execute():
                extract_tags_and_evacuate_cs(CS)
    return cs_dict

if __name__ == "__main__":
    main()

