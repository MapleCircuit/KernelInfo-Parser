# Core Subsystem API & State Architecture Specification

Dense architectural contract and state interaction reference across `globalstuff.py`, `FileHandler.py`, `GreatProcessor.py`, `DBLayout.py`, and `TableHandling.py`. Designed as an authoritative reference for AI agents.

---

## 1. Global Runtime & Type Subsystem (`core/globalstuff.py`)

### 1.1. Singleton Context (`G = GlobalStuff()`)
- **Engine & Driver Handles**:
  - `G.DB`: Active database driver instance (`BaseDBEngine` / `MariaDB` / `MockDB`).
  - `G.TE`: Active TableEngine instance (`TECachedDB` or `TEDirectDB`).
  - `G.MF`: Active `MasterFile` instance.
- **Environment & Configuration**:
  - `G.RAMDISK`: Path to temporary working directory mount (default `"/dev/shm"`).
  - `G.CPUS`: Worker process count for parallel parsing (default `8`).
  - `G.linux_directory`: `Path("linux")` repository location.
  - `G.CURRENT_PARSING_FILE`: Thread/process-local relative file path for log formatting.
- **Flags & Debugging**:
  - `G.DEBUG_TYPECHECK`: Enables runtime assertions on type validation decorators.
  - `G.BP_ON_SHUTDOWN`: Triggers `sys.breakpointhook()` on emergency exit.
  - `G.BP_ON_REF_FAIL`: Triggers `sys.breakpointhook()` when a `RefType` cannot be resolved.
  - `G.PROFILING_ENABLED`: Enables AST & execution duration profiling.
- **Control Methods**:
  - `G.emergency_shutdown(code: int = 1) -> None`: Cleans all paths in `gp.PURGE_LIST` and exits.
  - `G.BP() -> None`: Drops into breakpoint hook (`sys.breakpointhook()`).
  - `G.type_check(*expected_types)`: Method decorator enforcing runtime type validation.

### 1.2. Core Data Types & Canonical Bounds
- **`PointerType`**: `tuple[int, int]` &mdash; `(table_id, col_idx)` referencing a table column.
- **`JoinType`**: `tuple[PointerType, PointerType, int] | tuple[PointerType]` &mdash; Relational link `((from_t, from_c), (to_t, to_c), repeat_count)` or single table root `((t_id, c_idx),)`.
- **`JoinsType`**: `tuple[JoinType, ...]` &mdash; Immutable relational join graph tuple.
- **`OperationType`**: `tuple[JoinsType | int, int, tuple]` &mdash; `(target, op_code, data_tuple)`.
- **`LinkType`**: `int | str` &mdash; Context route marker (`REF_ROOT`, `REF_C_AST`, `REF_OLD`, `REF_POS`, `REF_MULTI`, `REF_FILE`, `REF_NO_REF`, or custom string identifier).
- **`RouteType`**: `tuple[LinkType, ...] | list[LinkType, ...]` &mdash; Path sequence identifying a stored operation.
- **`RefType`**: `tuple[PointerType, int, RouteType]` &mdash; Unresolved reference tuple: `(query_pointer, OP_REF, route_tuple)`.
- **`SafeDataType`**: `int | str | None` &mdash; Primitive scalar values acceptable to TableEngine and SQL backends.
- **`UnSafeDataType`**: `SafeDataType | RefType` &mdash; Data scalar containing either a primitive or an unresolved reference.

### 1.3. Global Constants & Enums
- **Operation Codes (`op_code`)**:
  - `OP_DONE = 0`: Already resolved/executed table operation.
  - `OP_SET = 1`: Staged insert or deduplicated lookup.
  - `OP_UPDATE = 2`: Staged upsert/update.
  - `OP_REF = 3`: Reference marker in data tuples.
  - `OP_REF_VIEW = 4`: Dynamic schema-driven AST view expansion operation.
  - `OP_VIEW_DONE = 5`: Resolved joined view operation.
  - `OP_VIEW_SET = 6`: Staged joined view operation.
- **Link Markers**:
  - `REF_ROOT = 0`: Global/file root context.
  - `REF_OLD = 1`: Prior version context scope.
  - `REF_POS = 2`: Direct numerical index offset in `CS.cs`.
  - `REF_FILE = 3`: Cross-file redirection link (followed by relative path string).
  - `REF_MULTI = 4`: Multi-item array bucket link.
  - `REF_C_AST = 5`: C AST parser scope.
  - `REF_NO_REF = 6`: Null/no-reference marker (evaluates to `None`).
- **File Types**:
  - `T_DIR = 0`, `T_C = 1`, `T_KCONFIG = 2`, `T_RUST = 3`, `T_ASM = 4`.
- **Helper Classes**:
  - `PointerGetter(joins)`: Iterator extracting `(repeat_count, pointer)` sequentially from `JoinsType`.
    - `get_first_pointer() -> PointerType`: Root table pointer.
    - `get_first_table_id() -> int`: Root table index.
    - `add_join(joins_list, join_tuple) -> None`: Upgrades single pointer or increments repeat counter.
  - `ASTT(IntEnum)`: AST construct category identifiers across C (`C_struct`, `C_Compound`), Preprocessor (`CPPro_define`, `CPPro_include`), ASM (`ASM_Instruction`, `ASM_Macro`), and Kconfig (`Kconfig_Config`, `Kconfig_Menu`, `Kconfig_Choice`, `Kconfig_Depends_On`, `Kconfig_Select`, `Kconfig_Op_And`, etc.).

---

## 2. Filesystem & Git Subsystem (`core/FileHandler.py`)

### 2.1. `MasterFile` (`mf`)
Manages RAMDISK temporary working trees and Git repository extraction.
- **State**:
  - `version_dict`: `dict[str, str]` &mdash; `version_name -> ramdisk_path`.
  - `file_dict`: `dict[str, dict[str, str]]` &mdash; `version_name -> {relative_path: file_content}`.
- **Key Methods**:
  - `create_temp_dir() -> str`: Spawns `/dev/shm/code-parser.XXXXXX` via `mktemp`.
  - `add_version(version_name: str, purge_list: list) -> None`: Clones version tree into RAMDISK and registers in `purge_list`.
  - `git_clone(version: str) -> str`: Executes `git archive <version> | tar -x` and provisions `include/asm` and `include/uapi/asm` symlinks pointing to `asm-generic`.
  - `get_file(file_path: str, version: str) -> str`: Returns cached content from RAMDISK (latin-1) or queries `git show <version>:<file_path>`.
  - `generate_change_list(gp: GreatProcessor) -> list[str]`: Runs `git diff <Old_Version_Name> <Version_Name> --name-status` and sets `gp.Change_List`.
  - `git_file_list(version: str) -> str`: Executes `git ls-tree -r --name-only <version>`.
  - `get_dir_list(version_name: str) -> list[str]`: Executes `git ls-tree -r -d --name-only <version_name>`.
  - `resolve_path(file_path: str) -> str`: Strips working directory prefix to return repo-relative path.
  - `trim_version(keep: int = 2) -> int`: Deletes oldest RAMDISK directory if cached versions exceed `keep`.
  - `clear_all_version() -> None`: Cleans all version trees from disk and resets dictionaries.

---

## 3. Runtime State & Multiprocessing IPC (`core/GreatProcessor.py`)

### 3.1. `GreatProcessor` (`gp`)
Central runtime container, schema registry, and worker IPC coordinator.
- **State Registry**:
  - `PURGE_LIST: list[str]`: Working directory paths for cleanup on exit.
  - `Table_Array: list[Table]`: Registered `Table` instances (indexed by `table_id`).
  - `Version_Name: str`: Active Git release tag (default empty git tree hash `"4b825dc642cb6eb9a060e54bf8d69288fbee4904"`).
  - `Old_Version_Name: str | int`: Prior Git release tag.
  - `VID: int` / `Old_VID: int`: Monotonic database primary keys for current and prior versions in `m_v_main`.
  - `Change_List: list[str] | None`: Raw diff lines (`"M\tpath"`, `"R100\told\tnew"`).
  - `ChangeSet_Dict: dict[str, ChangeSet]`: Main process dictionary mapping relative file paths to parsed `ChangeSet` objects.
  - `Alt_ChangeSet_Dict: dict[str, ChangeSet]`: Secondary cache for on-demand parsed foreign `ChangeSets` during cross-file reference resolution.
  - `Shared_ChangeSet_Dict_List: list[bytes] | None`: IPC list holding worker `pickle.dumps()` payloads.
- **IPC Protocol**:
  - `start_manager() -> None`: Initializes `Shared_ChangeSet_Dict_List = []`.
  - `push_set_to_main() -> None`: Executed by worker; serializes `ChangeSet_Dict` via `pickle.dumps()` onto `Shared_ChangeSet_Dict_List`.
  - `stop_manager() -> None`: Executed by main process; deserializes (`pickle.loads()`) and merges worker dictionaries into `gp.ChangeSet_Dict`.
  - `safe_get_cs(path: str) -> ChangeSet`: Lookup sequence: `ChangeSet_Dict` &rarr; `Alt_ChangeSet_Dict` &rarr; creates `ChangeSet("M", path)`, triggers `CS.parse()`, stores in `Alt_ChangeSet_Dict`, and returns `CS`.
  - `reset_cs() -> None`: Clears `Change_List`, `ChangeSet_Dict`, and `Alt_ChangeSet_Dict`.

---

## 4. Relational Database Schema Registry (`core/DBLayout.py`)

18 Core Tables defined via `Table` instances and exported in `TABLES` tuple (`init_db_layout(gp)` sets `gp.Table_Array = list(TABLES)`):

| `table_id` | Table Name | Columns | Primary Key | `no_duplicate` | `te_cached` | `hashing_table` | Description |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **0** | `m_v_main` | `(vid, vname)` | `("vid",)` | `True` | `True` | `False` | Version tag registry |
| **1** | `m_file_name` | `(fnid, fname)` | `("fnid",)` | `True` | `True` | `False` | Unique file path registry |
| **2** | `m_file` | `(fid, vid_s, vid_e, ftype, s_stat, e_stat)` | `("fid",)` | `False` | `True` | `False` | File lifecycle & status instance |
| **3** | `m_bridge_file` | `(vid, fnid, fid)` | `("vid", "fnid")` | `False` | `True` | `False` | Version-to-file instance bridge |
| **4** | `m_moved_file` | `(s_fid, e_fid)` | `("s_fid", "e_fid")`| `False` | `False`| `False` | File rename/movement tracking |
| **5** | `m_type_descriptor`| `(type_id, name)` | `("type_id",)` | `False` | `True` | `False` | AST node type registry (seeded from `ASTT`) |
| **6** | `m_ast` | `(ast_id, name, type_id)` | `("ast_id",)` | `False` | `False`| `"m_ast_hash"` | AST symbol nodes |
| **7** | `m_ast_container` | `(ast_id, priority, type_id, ref_ast_id)`| `("ast_id", "priority")`| `False` | `False` | `False` | AST child hierarchy links |
| **8** | `m_ast_include` | `(ast_id, fnid)` | `("ast_id",)` | `False` | `False`| `False` | AST `#include` / `source` references |
| **9** | `m_ast_debug` | `(ast_id, ast_raw)` | `("ast_id",)` | `False` | `False`| `False` | JSON dumps of AST structures |
| **10** | `m_tag` | `(tag_id, vid_s, vid_e, code, ast_id, hl_s, hl_l)` | `("tag_id", "vid_s")`| `False` | `False` | `False` | Code snippet occurrence tag |
| **11** | `m_bridge_tag` | `(fid, tag_id, line_s, line_e, char_s, char_e)` | `("fid", "tag_id")` | `False` | `False` | `False` | Tag line & coordinate mapping |
| **12** | `m_map_ast` | `(map_id, line_s, char_s, line_e, char_e, ast_id)`| `("map_id", "line_s", ...)`| `False` | `False`| `False` | Spatial AST coordinate region |
| **13** | `m_bridge_map` | `(tag_id, map_id)` | `("tag_id", "map_id")` | `False` | `False` | `False` | Tag-to-AST spatial map bridge |
| **14** | `m_ast_hash` | `(hash, ast_id)` | `("hash",)` | `False` | `True` | `False` | SHA-256 AST structural hash deduplication |
| **15** | `m_kconfig_symbol` | `(kcid, name, type, prompt, def_val, help, ast_id)` | `("kcid",)` | `True` | `True` | `False` | Normalized Kconfig symbol definitions |
| **16** | `m_kconfig_relation`| `(kcid, target_name, rel_type, cond_ast_id, priority)`| `("kcid", "rel_type", ...)`| `False` | `False` | `False` | Direct depends_on / select / imply dependency graph |
| **17** | `m_kconfig_tree` | `(tree_id, parent_id, node_type, title, kcid, priority, dep_ast_id, ast_id)` | `("tree_id",)` | `False` | `False` | `False` | Hierarchical Menuconfig tree & UI ordering |

---

## 5. Data Staging, Routing & Reference Resolution (`core/TableHandling.py`)

### 5.1. Data Sanitization Helpers
- `to_safe_data(val: Any) -> SafeDataType`: Coerces Enums/IntEnums, booleans, and custom objects to native Python `int`, `str`, or `None`.
- `is_data_unsafe(data: tuple) -> bool`: Returns `True` if any element in `data` is a reference tuple.
- `normalize_data_tuple(data: tuple) -> tuple[UnSafeDataType, ...]`: Converts primitive elements via `to_safe_data()` while preserving reference tuples intact.

### 5.2. `Table` Class Interface
- **Dynamic Pointer Attributes**: On init, sets `self.<col_name> = (table_id, col_idx)` (e.g. `m_file_name.fnid = (1, 0)`). Injects table instance into `parser.c_ast.c_ast_type.<table_name>`.
- **Operation Builders**:
  - `set(*columns) -> OperationType`: Returns `(table_id, OP_SET, columns)` (redirects to `get_set()` if `table.no_duplicate == True`).
  - `update(*columns) -> OperationType`: Returns `(table_id, OP_UPDATE, columns)`. If partial row provided, queries `G.TE.get()` to populate missing values.
  - `get(*columns) -> OperationType | None`: Queries `G.TE.get()` immediately; returns `(table_id, OP_DONE, result)` or `None`.
  - `get_set(*columns) -> OperationType`: Queries `G.TE.get()`; returns `(table_id, OP_DONE, result)` if found, else `(table_id, OP_SET, columns)`.
  - `view(joins, *data) -> OperationType`: Checks `G.TE.view_get()`; returns `(joins, OP_VIEW_DONE, result)` if found, else `(joins, OP_VIEW_SET, data)`.
  - `view_get(joins, *data) -> OperationType | None`: Queries `G.TE.view_get()`; returns `(joins, OP_VIEW_DONE, result)` or `None`.
  - `view_get_multiple(joins, *data) -> list[tuple] | None`: Returns list of matching row tuples from `G.TE.view_get_multiple()`.
  - `ref_view(joins, *data) -> OperationType`: Returns `(joins, OP_REF_VIEW, data)` for dynamic AST schema resolution.

### 5.3. `ChangeSet` (`CS`) Class & Context Routing
Represents a parsed file diff and acts as the relational staging buffer.
- **State**:
  - `file_operation`: Raw git operation string (`"M"`, `"A"`, `"D"`, `"R100"`).
  - `current_path`: Relative target file path.
  - `old_path`: Relative source file path (for renames).
  - `cs: list[OperationType]`: Ordered queue of operations to execute.
  - `cs_result: list[tuple[SafeDataType, ...]]`: Resolved execution output rows matching `cs`.
  - `store_dict: dict`: Multi-dimensional routing index: `store_dict[parsed_route][table_id] = op_idx` and `store_dict[REF_MULTI] = [ [op_idx, ...], ... ]`.
  - `route: list[LinkType]`: Active context route stack (initialized to `[REF_ROOT]`).
  - `route_count: list[int]`: Number of items to pop on context exit.
  - `multi_stack: list[int]`: Active `REF_MULTI` bucket indices.
- **Context Routing Protocol**:
  - `with CS(link1, link2):` pushes links onto `self.route` via `__call__()`.
  - On `REF_MULTI`, allocates a new list in `store_dict[REF_MULTI]` and tracks index on `multi_stack`.
  - Context exit (`__exit__()`) pops exactly `route_count.pop()` elements from `self.route`.
- **Route Canonicalization (`route_parse(route) -> list`)**:
  - Normalizes route list by applying link reduction rules:
    - `REF_POS` & `REF_MULTI`: Clears preceding links; enables `data_bypass` to capture subsequent position argument.
    - `REF_ROOT`, `REF_C_AST`, `REF_NO_REF`: Clears all preceding links.
    - `REF_FILE`: Extracts target file path and prefixes canonical route with `[REF_FILE, target_file]`.
- **Storing & Referencing**:
  - `store(operation: OperationType, *route: LinkType) -> None`:
    - Appends `operation` to `self.cs`.
    - Canonicalizes current route stack + `route` arguments via `route_parse()`.
    - Indexes position in `store_dict[parsed_route][target_table_id] = len(self.cs) - 1` (or appends to `store_dict[REF_MULTI][idx]`).
  - `ref(query: PointerType, *route_args: LinkType) -> UnSafeDataType`:
    - If resolvable immediately via `resolve_ref()`, returns primitive `SafeDataType`.
    - Otherwise returns reference tuple: `(query, OP_REF, parsed_route)`.
- **Reference Resolution (`resolve_ref(query, parsed_route) -> SafeDataType | list`)**:
  1. `parsed_route[0] == REF_NO_REF`: Returns `None`.
  2. `parsed_route[0] == REF_FILE`: Calls `gp.safe_get_cs(parsed_route[1]).resolve_ref(query, parsed_route[2:])`.
  3. `parsed_route[0] == REF_POS`: Fetches directly from `self.cs[parsed_route[1]]` column `query[1]`.
  4. `parsed_route[0] == REF_MULTI`: Fetches list of column values across `store_dict[REF_MULTI][parsed_route[1]]`.
  5. Default: Looks up `pos = store_dict[parsed_route][query[0]]` and extracts column value `query[1]`.
- **Dynamic AST Views (`_unpack_ref_view(operation) -> OperationType | None`)**:
  - Evaluates AST rule schemas against records in `store_dict`, matches conditional rules (`schema_ifs`), dynamically constructs joined table graph (`schema_thens`), and converts `OP_REF_VIEW` into concrete `(joins_tuple, OP_VIEW_SET, data_tuple)`.
  - Queries candidate rows via `CS.get_available_data(route, target_table_id)`, which filters candidate operations strictly by `tableid` (handling both integer IDs and joined view tuples) to isolate `m_ast` records from co-located tag and bridge operations.
- **Pipeline Execution (`execute() -> bool`)**:
  - Iterates over `CS.cs` starting at `len(CS.cs_result)`.
  - Unpacks dynamic views (`OP_REF_VIEW` &rarr; `OP_VIEW_SET`).
  - Converts all reference tuples in data to `SafeDataType` via `_resolve_ref_from_tuple()`.
  - Dispatches operations downstream to `G.TE.set()`, `G.TE.update()`, or `G.TE.view_set()`.
  - Appends resulting rows to `CS.cs_result` and marks `cs_processed = True`.
- **IPC Sanitization (`clear_bloat() -> None`)**:
  - Drops unpicklable object handles (`self.gp = None`, `self.mf = None`, `self.debug = []`, `self.parsers = {}`) before worker IPC serialization.

---

## 6. End-to-End Inter-Module Execution Lifecycle

```
========================================================================================
[1. INITIALIZATION & REPO EXTRACTION]
----------------------------------------------------------------------------------------
MasterFile (mf)                    GreatProcessor (gp)              DBLayout (TABLES)
  |                                        |                                |
  |-- add_version(vname)                   |-- init_db_layout(gp) --------->|
  |   (Clones git tree to /dev/shm)        |   (Populates gp.Table_Array)   |
  |-- generate_change_list(gp) ----------->|                                |
      (Populates gp.Change_List)           |                                |
========================================================================================
[2. MULTIPROCESSING PARSING & IPC]
----------------------------------------------------------------------------------------
Main Process                               Worker Process (CPUs 1..N)
  |                                                |
  |-- gp.start_manager()                           |-- ChangeSet(diff_line)
  |   (Allocates Shared_ChangeSet_Dict_List)       |-- CS.parse() -> AST Parsers
  |                                                |-- Table.<op>() builders
  |                                                |-- CS.store() (Buffers into CS.cs)
  |                                                |-- CS.clear_bloat()
  |                                                |-- gp.push_set_to_main()
  |                                                |   (pickle.dumps -> Shared List)
  |                                                |
  |<-- gp.stop_manager() <-------------------------|
       (pickle.loads -> Merges into gp.ChangeSet_Dict)
========================================================================================
[3. REFERENCE RESOLUTION & TABLE ENGINE COMMIT]
----------------------------------------------------------------------------------------
gp.ChangeSet_Dict                  TableEngine (G.TE)               Database (G.DB)
  |                                        |                                |
  |-- For each CS in ChangeSet_Dict:       |                                |
  |   |-- CS.execute()                     |                                |
  |   |   |-- CS.resolve_ref()             |                                |
  |   |   |   (Resolves REF_FILE           |                                |
  |   |   |    via gp.safe_get_cs)         |                                |
  |   |   |-- CS._unpack_ref_view()        |                                |
  |   |   |-- G.TE.set() / update() ------>| (Stages in queued_set/update)  |
  |   |   |-- G.TE.view_set() ------------>| (Decomposes joins & hashes)    |
  |                                        |                                |
  |--------------------------------------->|-- G.TE.commit_all() ---------->|
                                               (Parallel SQL batch flush)   |
========================================================================================
```
