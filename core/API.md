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
  - `G.LOW_MEMORY_MODE`: Enabled via `--low-mem`; throttles worker pool to `CPUS // 2` (batch size 50) and executes chunked commits every 500 ChangeSets.
  - `G.VERY_LOW_MEMORY_MODE`: Enabled via `--very-low-mem`; limits workers to `min(2, CPUS // 4)` (batch size 25) with chunked commits every 300 ChangeSets for hosts with < 64GB RAM.
  - `G.MEMORY_MODE`: String indicator (`"normal"`, `"low"`, `"very-low"`).
- **Flags & Debugging**:
  - `G.DEBUG_TYPECHECK`: Enables runtime assertions on type validation decorators.
  - `G.BP_ON_SHUTDOWN`: Triggers `sys.breakpointhook()` on emergency exit.
  - `G.BP_ON_REF_FAIL`: Triggers `sys.breakpointhook()` when a `RefType` cannot be resolved.
  - `G.PROFILING_ENABLED`: Enables AST & execution duration profiling.
- **Control Methods & Utilities**:
  - `G.emergency_shutdown(code: int = 1) -> None`: Cleans all paths in `gp.PURGE_LIST` and exits.
  - `G.BP() -> None`: Drops into breakpoint hook (`sys.breakpointhook()`).
  - `G.type_check(*expected_types)`: Method decorator enforcing runtime type validation.
  - `reclaim_system_memory() -> None`: Runs explicit garbage collection (`gc.collect()`) and invokes glibc `malloc_trim(0)` to return free memory pages to OS.
  - `compute_code_hash(code: str) -> bytes`: Computes a deterministic 32-byte binary SHA-256 digest (`BINARY(32)`) from normalized code snippet strings for `m_tag_code`.
  - `clean_unnamed_spelling(spelling: str) -> str`: Normalizes Libclang anonymous/unnamed cursor spellings (e.g. `(unnamed at /dev/shm/.../include/linux/foo.h:12:1)`) down to clean relative git repository paths (`(unnamed at include/linux/foo.h:12:1)`), stripping host and RAMDISK prefix directories.
  - `setup_memory_limit(limit_gb: float = 60.0) -> None`: Enforces a hard address space limit (`RLIMIT_AS`) on Linux hosts to eliminate catastrophic kernel lockups caused by runaway Clang C-heap memory allocation during massive multicore AST translation unit parsing.


### 1.2. Core Data Types & Canonical Bounds
- **`PointerType`**: `tuple[int, int]` &mdash; `(table_id, col_idx)` referencing a table column.
- **`JoinType`**: `tuple[PointerType, PointerType, int] | tuple[PointerType]` &mdash; Relational link `((from_t, from_c), (to_t, to_c), repeat_count)` or single table root `((t_id, c_idx),)`.
- **`JoinsType`**: `tuple[JoinType, ...]` &mdash; Immutable relational join graph tuple.
- **`OperationType`**: `tuple[JoinsType | int, int, tuple]` &mdash; `(target, op_code, data_tuple)`.
- **`LinkType`**: `int | str` &mdash; Context route marker (`REF_ROOT`, `REF_C_AST`, `REF_OLD`, `REF_POS`, `REF_MULTI`, `REF_FILE`, `REF_NO_REF`, or custom string identifier).
- **`RouteType`**: `tuple[LinkType, ...] | list[LinkType, ...]` &mdash; Path sequence identifying a stored operation.
- **`RefType`**: `tuple[PointerType, int, RouteType]` &mdash; Unresolved reference tuple: `(query_pointer, OP_REF, route_tuple)`.
- **`SafeDataType`**: `int | str | bytes | None` &mdash; Primitive scalar values acceptable to TableEngine and SQL backends (supports `bytes` for raw 32-byte binary SHA-256 digests).
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
  - `T_DIR = 0`: Directory container.
  - `T_C = 1`: C and Preprocessor source file (`.c`, `.h`).
  - `T_KCONFIG = 2`: Kconfig menu definitions (`Kconfig*`).
  - `T_RUST = 3`: Rust source file (`.rs`).
  - `T_ASM = 4`: Architecture assembly source (`.S`, `.s`).
  - `T_RAW = 5`: Fallback raw/unparsed text file (Documentation, README, licenses, build scripts).
  - `T_MAINTAINERS = 6`: Kernel subsystem maintainer directory (`MAINTAINERS`).
  - `T_CREDITS = 7`: Kernel contributor biographies (`CREDITS`).
- **Helper Classes**:
  - `PointerGetter(joins)`: Iterator extracting `(repeat_count, pointer)` sequentially from `JoinsType`.
    - `get_first_pointer() -> PointerType`: Root table pointer.
    - `get_first_table_id() -> int`: Root table index.
    - `add_join(joins_list, join_tuple) -> None`: Upgrades single pointer or increments repeat counter.
  - `SymbolRole(IntEnum)`: Categorical classification for symbol occurrences staged in `m_symbol_ref`:
    - `Declaration = 1`: Explicit symbol forward declaration or prototype.
    - `TypeUsage = 2`: Use of symbol as a type specifier or typecast.
    - `Call = 3`: Function invocation expression (`Ast_CallExpr`).
    - `MemberRef = 4`: Struct, union, or enum member access (`Ast_MemberRefExpr`).
    - `DeclRef = 5`: Direct variable, constant, or identifier reference (`Ast_DeclRefExpr`).
    - `MacroExpansion = 6`: Preprocessor macro instantiation or expansion (`Ast_MacroRefExpr`).

  - `FileRefType(IntEnum)`: Categorical classification for cross-file references staged in `m_file_reference`:
    - `Include = 1`: C and Preprocessor `#include <...>` or Assembly include statements.
    - `Kconfig = 2`: Kconfig `source` and `rsource` references.
    - `Kbuild = 3`: Kbuild compilation object mappings (`obj-$(CONFIG_...) += ...` and `*-objs`).
    - `Makefile = 4`: Makefile includes and subdirectory recursions.
    - `Documentation = 5`: Documentation and plain text file cross-references.

  - `STANDARD_C_KEYWORDS: dict[str, ASTT]`: Fast keyword-to-AST category mapping covering C control flow (`if`, `switch`, `case`, `default`, `while`, `do`, `for`, `return`, `break`, `continue`, `goto`, `asm`), qualifiers (`const`, `volatile`, `restrict`, `_Atomic`), storage classes (`static`, `extern`, `typedef`, `inline`), and primitive types (`void`, `char`, `short`, `int`, `long`, `signed`, `unsigned`, `float`, `double`, `struct`, `union`, `enum`).
  - `ASTT(IntEnum)`: AST construct category identifiers across C (`C_struct`, `C_Compound`, `C_SizeofExpr`, `C_TypeRef`), Preprocessor (`CPPro_define`, `CPPro_include`), ASM (`ASM_Instruction`, `ASM_Macro`), and Kconfig (`Kconfig_Config`, `Kconfig_Menu`, `Kconfig_Choice`, `Kconfig_Depends_On`, `Kconfig_Select`, `Kconfig_Op_And`, etc.).


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
  - `read_file(file_path: str, version: str) -> bytes`: Reads raw bytes from RAMDISK. Defensively inspects paths: if `p.is_symlink() and p.is_dir()` or on read errors, extracts link target via `os.readlink(p).encode("utf-8")` to eliminate `[Errno 21] Is a directory` exceptions on directory symlinks (e.g. `arch/*/boot/dts/include/dt-bindings`).
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
  - `Symlink_List: list[str]`: Buffers symlinks partitioned from `gp.Change_List` for zero-duplication deferred aliasing directly into `m_bridge_file` via `processing_symlinks()`.
  - `ChangeSet_Dict: dict[str, ChangeSet] | CompressedChangeSetDict`: Main process dictionary mapping relative file paths to parsed `ChangeSet` objects.
  - `Alt_ChangeSet_Dict: dict[str, ChangeSet] | CompressedChangeSetDict`: Secondary cache for on-demand parsed foreign `ChangeSets` during cross-file reference resolution.
  - `Shared_ChangeSet_Dict_List: list[bytes] | None`: IPC list holding worker `pickle.dumps()` payloads.
  - `unchanged_symbol_cache: dict[str, dict[tuple[str, int], int]]`: In-memory cache of symbol mappings `(name, type_id) -> ast_id` queried from the database for unchanged files.
  - `_changed_paths_set: set[str]`: Set of all relative file paths modified in the active release version.
  - `file_deps: dict[str, set[str]]`: Lightweight dependency graph mapping each changed file path to its set of unresolved foreign file dependencies (`foreign_deps`).
  - `file_symbols: dict[str, dict[tuple[str, int], int]]`: Per-file exported symbols map `rel_file -> {(sym_name, type_id): ast_id}` populated during Phase 1 staging.
  - `file_names: dict[str, dict[str, int]]`: Per-file exported symbol names map `rel_file -> {sym_name: ast_id}` for name-only fallback resolution.
- **Memory Compression (`CompressedChangeSetDict`)**:
  - Implements an on-demand compressed store backed by `zlib.compress(pickle.dumps(cs), level=1)` and an in-memory LRU uncompressed cache (`OrderedDict`, default capacity 500 items, throttled to 25 items under `--very-low-mem`).
  - Tracks dirty uncompressed keys via `_dirty_keys` and gracefully falls back to uncompressed LRU caching if zlib compression fails.
  - Provides `clear()` to thoroughly release compressed blobs, LRU instances, and dirty key sets upon completion of an update cycle.
- **IPC Protocol & Query Helpers**:
  - `start_manager() -> None`: Initializes `Shared_ChangeSet_Dict_List = []`.
  - `push_set_to_main() -> None`: Executed by worker; serializes `ChangeSet_Dict` via `pickle.dumps()` onto `Shared_ChangeSet_Dict_List`.
  - `stop_manager() -> None`: Executed by main process; deserializes (`pickle.loads()`) and merges worker dictionaries into `gp.ChangeSet_Dict`.
  - `safe_get_cs(path: str) -> ChangeSet | None`: Lookup sequence: `ChangeSet_Dict` &rarr; `Alt_ChangeSet_Dict` &rarr; checks existence in RAMDISK tree (skips non-kernel system paths `/usr/`, `/etc/`, `/lib/`, `/opt/`) &rarr; creates `ChangeSet("M", path)`, triggers `CS.parse()`, stores in `Alt_ChangeSet_Dict`, and returns `CS`.
  - `get_file_symbols_from_db(rel_file: str) -> dict[tuple[str, int], int] | None`: Queries TableEngine joined tables (`m_file_name` &rarr; `m_bridge_file` &rarr; `m_bridge_tag` &rarr; `m_tag` &rarr; `m_ast`) to extract active symbol mappings `(ast_name, ast_type) -> ast_id` for unchanged files without re-parsing source text.
  - `reset_cs() -> None`: Clears `Change_List`, `Symlink_List`, `ChangeSet_Dict`, `Alt_ChangeSet_Dict`, `unchanged_symbol_cache`, `file_deps`, `file_symbols`, and `file_names`.

---

## 4. Relational Database Schema Registry (`core/DBLayout.py`)

33 Core Tables defined via `Table` instances and exported in `TABLES` tuple (`init_db_layout(gp)` sets `gp.Table_Array = list(TABLES)`):

| `table_id` | Table Name | Columns | Primary Key | `no_duplicate` | `te_cached` | `hashing_table` | Description |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **0** | `m_v_main` | `(vid, vname)` | `("vid",)` | `True` | `True` | `False` | Version tag registry |
| **1** | `m_file_name` | `(fnid, fname)` | `("fnid",)` | `True` | `True` | `False` | Unique file path registry |
| **2** | `m_file` | `(fid, vid_s, vid_e, ftype, s_stat, e_stat)` | `("fid",)` | `False` | `True` | `False` | File lifecycle & status instance |
| **3** | `m_bridge_file` | `(vid, fnid, fid)` | `("vid", "fnid")` | `False` | `True` | `False` | Version-to-file instance bridge (N:1 aliases symlinks) |
| **4** | `m_moved_file` | `(s_fid, e_fid)` | `("s_fid", "e_fid")`| `False` | `False`| `False` | File rename/movement tracking |
| **5** | `m_type_descriptor`| `(type_id, name)` | `("type_id",)` | `False` | `True` | `False` | AST node type registry (seeded from `ASTT`) |
| **6** | `m_ast` | `(ast_id, name, type_id)` | `("ast_id",)` | `False` | `False`| `"m_ast_hash"` | AST symbol nodes |
| **7** | `m_ast_container` | `(ast_id, priority, type_id, ref_ast_id)`| `("ast_id", "priority")`| `False` | `False` | `False` | AST child hierarchy links |
| **8** | `m_ast_include` | `(ast_id, fnid)` | `("ast_id",)` | `False` | `False`| `False` | AST `#include` / `source` references |
| **9** | `m_ast_debug` | `(ast_id, ast_raw)` | `("ast_id",)` | `False` | `False`| `False` | JSON dumps of AST structures |
| **10** | `m_tag_code` | `(hash, code)` | `("hash",)` | `False` | `("hash",)` | `False` | Code snippet registry (32-byte binary SHA-256 hash, single-column caching) |
| **11** | `m_tag` | `(tag_id, vid_s, vid_e, hash, ast_id, hl_s, hl_l)` | `("tag_id", "vid_s")`| `False` | `False` | `False` | Code snippet occurrence tag |
| **12** | `m_bridge_tag` | `(fid, tag_id, line_s, line_e, char_s, char_e)` | `("fid", "tag_id")` | `False` | `False` | `False` | Tag line & coordinate mapping |
| **13** | `m_map_ast` | `(map_id, line_s, char_s, line_e, char_e, ast_id)`| `("map_id", "line_s", ...)`| `False` | `False`| `False` | Spatial AST coordinate region |
| **14** | `m_bridge_map` | `(tag_id, map_id)` | `("tag_id", "map_id")` | `False` | `False` | `False` | Tag-to-AST spatial map bridge |
| **15** | `m_ast_hash` | `(hash, ast_id)` | `("hash",)` | `False` | `True` | `False` | Binary 32-byte SHA-256 AST structural hash deduplication |

> [!IMPORTANT]
> **ChangeSet Tag Reference Order Invariant (Rule 12)**: When staging tags in ChangeSets (`with CS(REF_POS):`), `m_tag.set` MUST be the first operation inside the block so that `tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))` points directly to `m_tag`. Auxiliary deduplication tables (such as `m_tag_code.get_set`) must always be staged after `m_tag.set` within the block.
| **16** | `m_kconfig_symbol` | `(kcid, vid_s, vid_e, name, type, prompt, def_val, help, ast_id)` | `("kcid", "vid_s")` | `True` | `True` | `False` | Normalized Kconfig symbol definitions |
| **17** | `m_kconfig_relation`| `(rel_id, kcid, target_name, rel_type, cond_ast_id, priority)`| `("rel_id",)`| `True` | `True` | `False` | Direct depends_on / select / imply dependency graph |
| **18** | `m_kconfig_tree` | `(tree_id, vid, parent_id, node_type, title, kcid, priority, dep_ast_id, ast_id)` | `("tree_id", "vid")` | `False` | `True` | `False` | Hierarchical Menuconfig tree & UI ordering |
| **19** | `m_kconfig_kbuild`| `(kcid, vid, fid, compile_mode, target_obj)` | `("kcid", "vid", "fid", "compile_mode")` | `False` | `True` | `False` | Kconfig to compiled file/object map |
| **20** | `m_maintainer_person`| `(person_id, name, email)` | `("person_id",)` | `True` | `True` | `False` | Unique maintainer & contributor identity registry |
| **21** | `m_maintainer_section`| `(sec_id, vid_s, vid_e, name, status, scm_tree, web_page, mailing_list, ast_id)` | `("sec_id", "vid_s")` | `True` | `True` | `False` | Subsystem maintainer section definitions |
| **22** | `m_maintainer_member`| `(sec_id, person_id, role_type, priority)` | `("sec_id", "person_id", "role_type")` | `False` | `True` | `False` | Maintainer subsystem personnel roles |
| **23** | `m_maintainer_pattern`| `(sec_id, pat_type, pattern, priority)` | `("sec_id", "pat_type", "pattern", "priority")` | `False` | `True` | `False` | File & directory path matching patterns |
| **24** | `m_maintainer_file`| `(vid, fid, sec_id)` | `("vid", "fid", "sec_id")` | `False` | `False` | `False` | Resolved file-to-subsystem ownership bridge |
| **25** | `m_credits_entry` | `(credit_id, vid_s, vid_e, person_id, web_page, pgp_key, description, snail_mail, ast_id)` | `("credit_id", "vid_s")` | `True` | `True` | `False` | CREDITS file entries and biographies |
| **26** | `m_commit` | `(commit_id, vid, commit_hash, author_id, author_date, committer_id, committer_date, subject, message)` | `("commit_id",)` | `False` | `False` | `False` | Git commit metadata registry |
| **27** | `m_bridge_commit_person`| `(commit_id, person_id, role_type, priority)` | `("commit_id", "person_id", "role_type")` | `False` | `False` | `False` | Commit author, committer, and trailers |
| **28** | `m_bridge_commit_file`| `(commit_id, vid, fid, change_type)` | `("commit_id", "fid")` | `False` | `False` | `False` | Files touched per commit |
| **29** | `m_bridge_commit_tag`| `(commit_id, vid, fid, tag_id)` | `("commit_id", "tag_id")` | `False` | `False` | `False` | Code tags modified per commit |
| **30** | `m_moved_tag` | `(s_tag_id, e_tag_id)` | `("s_tag_id", "e_tag_id")` | `False` | `False` | `False` | Tag history & cross-version evolution tracking |
| **31** | `m_symbol_def` | `(def_id, vid, fid, tag_id, ast_id, name, type_id, line_s, line_e)` | `("def_id",)` | `False` | `True` | `False` | Authoritative Symbol Definition Registry (version-scoped) |
| **32** | `m_symbol_ref` | `(ref_id, vid, fid, tag_id, ast_id, role, line, char_s)` | `("ref_id",)` | `False` | `False` | `False` | Symbol Declarations & Usages Reference Index (version-scoped) |
| **33** | `m_file_reference` | `(ref_id, vid, source_fid, target_fnid, ref_type, line_no, details)` | `("ref_id",)` | `False` | `False` | `False` | Cross-File Usage & Dependency Index (`Include`=1, `Kconfig`=2, `Kbuild`=3, `Makefile`=4, `Documentation`=5) (version-scoped) |

---

## 5. Data Staging, Routing & Reference Resolution (`core/TableHandling.py`)

### 5.1. Data Sanitization Helpers
- `to_safe_data(val: Any) -> SafeDataType`: Coerces Enums/IntEnums, booleans, and custom objects to native Python `int`, `str`, or `None`.
- `is_data_unsafe(data: tuple) -> bool`: Returns `True` if any element in `data` is a reference tuple.
- `normalize_data_tuple(data: tuple) -> tuple[UnSafeDataType, ...]`: Converts primitive elements via `to_safe_data()` while preserving reference tuples intact.

### 5.2. `Table` Class Interface
- **Dynamic Pointer Attributes**: On init, sets `self.<col_name> = (table_id, col_idx)` (e.g. `m_file_name.fnid = (1, 0)`). Injects table instance into `parser.c_ast.c_ast` and `parser.c_ast`.
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
- **Reference Resolution (`resolve_ref(query, parsed_route, force_stubs=False) -> SafeDataType | list`)**:
  1. `parsed_route[0] == REF_NO_REF`: Returns `None`.
  2. `parsed_route[0] == REF_FILE`: Evaluates foreign cross-file symbol lookups with multi-tier fast paths:
     - **Step 0 (Per-File Symbols)**: Checks `gp.file_symbols[rel_file]` and `gp.file_names[rel_file]` for immediate $O(1)$ resolved symbol `ast_id`.
     - **Step 0b (Phase 2 Snapshot)**: Checks `_phase2_symbols` and `_phase2_names` global symbol snapshots.
     - **Step 1 (Active Batch / LRU Cache)**: Inspects `batch_cs_dict` or `ChangeSet_Dict._lru_cache[rel_file]`. If evacuated, reads from `foreign_cs.resolved_symbols`.
     - **Step 1b (Rule 24 Blocking Guard)**: If `rel_file` is an in-flight incomplete ChangeSet (`is_in_flight_changed` in `gp._changed_paths_set` or active in `ChangeSet_Dict` without `cs_processed`) and `force_stubs` is `False`, sets `self.blocked_on = rel_file` and returns `None` immediately, preventing redundant linear cache scans.
     - **Step 2 (Database Fallback)**: Calls `gp.get_file_symbols_from_db(rel_file)` to retrieve symbol mappings for unchanged kernel files directly from the database.
     - **Step 3 (Circular Dependency Breaking)**: If `force_stubs=True` is enabled, stages a canonical `notbind` stub symbol via `m_ast.view` and logs a circular dependency warning.
  3. `parsed_route[0] == REF_POS`: Fetches directly from `self.cs[parsed_route[1]]` column `query[1]`.
  4. `parsed_route[0] == REF_MULTI`: Fetches list of column values across `store_dict[REF_MULTI][parsed_route[1]]`.
  5. Default: Looks up `pos = store_dict[parsed_route][query[0]]` and extracts column value `query[1]`.
- **Worker Optimization & View Preprocessing**:
  - `preprocess_ref_views() -> None`: Pre-unpacks intra-file `OP_REF_VIEW` operations into concrete `OP_VIEW_SET` on worker cores before IPC serialization, distributing schema pattern matching across CPU cores.
  - `prune_unchanged_dependencies() -> None`: Queries `gp.get_file_symbols_from_db()` in workers during Phase 1 to pre-resolve references targeting unchanged files and prunes satisfied entries from `foreign_deps`.
  - `pre_resolve_operations(resolved_symbols_map, resolved_names_map) -> list`: Worker-side method invoked during Phase 2 to resolve foreign `REF_FILE` references and return a pre-resolved operation list to the main process for sequential lock-free staging.
- **Dynamic AST Views (`_unpack_ref_view(operation) -> OperationType | None`)**:
  - Evaluates AST rule schemas against records in `store_dict`, matches conditional rules (`schema_ifs`), dynamically constructs joined table graph (`schema_thens`), and converts `OP_REF_VIEW` into concrete `(joins_tuple, OP_VIEW_SET, data_tuple)`.
  - Queries candidate rows via `CS.get_available_data(route, target_table_id)`, which filters candidate operations strictly by `tableid` (handling both integer IDs and joined view tuples) to isolate `m_ast` records from co-located tag and bridge operations.
- **Pipeline Execution (`execute(force_stubs=False) -> bool`)**:
  - Iterates over `CS.cs` starting at `len(CS.cs_result)`.
  - Unpacks dynamic views (`OP_REF_VIEW` &rarr; `OP_VIEW_SET`).
  - Converts all reference tuples in data to `SafeDataType` via `_resolve_ref_from_tuple()`.
  - Dispatches operations downstream to `G.TE.set()`, `G.TE.update()`, or `G.TE.view_set()`.
  - Appends resulting rows to `CS.cs_result` and marks `cs_processed = True`.
- **IPC Sanitization (`clear_bloat() -> None`)**:
  - Drops unpicklable object handles (`self.gp = None`, `self.mf = None`, `self.file = None`, `self.debug = []`, `self.parsers = {}`, `self.prior_tags = None`, `self.prior_tags_map = None`, `self.active_tag_list = None`, `self.pending_symbol_refs = []`, `self.last_tag_ref = None`, `self._bridge_maps = set()`, `self.batch_cs_dict = None`, `self.blocked_on = None`) before worker IPC serialization.

---

## 6. End-to-End Inter-Module Execution Lifecycle

```
==================================================================================================
[1. INITIALIZATION & REPOSITORY EXTRACTION]
--------------------------------------------------------------------------------------------------
MasterFile (MF)                   GreatProcessor (gp)               TableEngine (G.TE)
  │                                       │                                 │
  ├─ add_version(vname)                   ├─ init_db_layout(gp)             │
  │  (Clones git tree to /dev/shm)        │  (Populates gp.Table_Array)     │
  └─ generate_change_list(gp) ───────────►│                                 │
     (Populates gp.Change_List)           └─ G.TE.start(gp.Table_Array) ────►
                                             (Preloads te_cached tables &
                                              packs 1GB/2MB/THP shared buffer)
==================================================================================================
[2. MULTICORE PARTITIONING & TWO-STAGE PARALLEL WORKER PARSING]
--------------------------------------------------------------------------------------------------
Main Process (trigger_multicore)          Worker Process 1..N
  │                                       │ (Inherits shared huge-page mmap across fork;
  │                                       │  calls G.TE.start_new_db(is_worker=True))
  ├─ Partition gp.Change_List:
  │  ├── order_changed_files()
  │  │   └── Kahn's topological sort on header '#include' dependencies
  │  ├── Stage 1: header_files (.h, .hpp) ──►│ ChangeSet(header_diff_line)
  │  │                                       │ ├── CS.parse() -> C/ASM/Kconfig/Rust Parsers
  │  │                                       │ ├── Table.<op>() builders (m_ast.view, m_tag.set)
  │  │                                       │ ├── CS.preprocess_ref_views()
  │  │                                       │ ├── CS.prune_unchanged_dependencies()
  │  │                                       │ ├── CS.store() (Buffers into CS.cs)
  │  │                                       │ └── CS.clear_bloat()
  │  │                                       └─► Worker IPC (Compressed pickle -> result_queue)
  │  ├── processing_dirs() (Staged directory ChangeSets)
  │  ├── processing_unchanges() (Propagates unchanged files & bridges)
  │  ├── scheduler.resolve_headers() (Resolves & evacuates header ChangeSets before sources)
  │  │
  │  ├── Stage 2: source_files (.c, .S) ───►│ ChangeSet(source_diff_line)
  │  │                                       │ (Parallel worker parsing & view preprocessing)
  │  │                                       └─► Streams into DependencyScheduler.ingest_batch()
  │  └── symlink_files
  │      (Appended to gp.Symlink_List for zero-duplication deferred alias)
  │
  └─ scheduler.finalize() (Drains deferred queue & triggers Phase 2 parallel waves)
[3. TWO-PHASE EXECUTION: STREAMING LEAF RESOLUTION & WAVE-BASED MULTICORE STAGING]
--------------------------------------------------------------------------------------------------
PHASE 1: Streaming Ingestion & Opportunistic Leaf Execution (main.py:trigger_multicore)
  │
  ├─ Worker Pool (G.CPUS) parses files, populating CS.foreign_deps in CS.ref()
  ├─ Streams compressed batches into DependencyScheduler.ingest_batch()
  ├─ Main Process drains immediately ready leaf ChangeSets (headers, self-contained files)
  │  ├─ Success: extract_tags_and_evacuate_cs(CS) & registers resolved_symbols
  │  └─ Blocked: skipped without spinning, deferred for Phase 2
  ├─ As soon as parsing workers finish, trigger_multicore() exits immediately
  └─ Phase 1 worker pool terminates cleanly -> Clang C-heap memory purged via reclaim_system_memory()
--------------------------------------------------------------------------------------------------
PHASE 2: Wave-Based Multicore Resolution & High-Speed Staging (main.py:execute_phase2_parallel_waves)
Main Process                                  Phase 2 Worker Pool (resolution_worker)
  │                                               │
  ├─ Extracts remaining unexecuted ChangeSets     │
  ├─ Builds wave DAG:                             │
  │  deps[path] = {target in cs.foreign_deps if target in remaining}
  │                                               │
  ├─ Loop while remaining:                        │
  │  ├─ Wave Selection:                           │
  │  │  wave = [path for path in remaining if not deps[path]]
  │  │                                            │
  │  ├─ [DEADLOCK GUARD] If wave is empty:        │
  │  │  ├─ Main core picks candidate with max dependents
  │  │  ├─ cand_cs.execute(force_stubs=True)      │
  │  │  └─ Evacuates buffers, updates resolved_symbols, unlocks next wave
  │  │                                            │
  │  ├─ [SMALL-WAVE BYPASS] If len(wave) < 4:    │
  │  │  └─ Executes directly on main core (bypasses IPC overhead)
  │  │                                            │
  │  └─ [PARALLEL WAVE DISPATCH] len(wave) >= 4:  │
  │     ├─ Sends chunks + resolved_symbols ──────►├─ Resolves foreign refs & unpacks views
  │     │                                         ├─ Normalizes data tuples
  │     │◄─ Returns (path, pre_resolved_cs) ──────┴─ Sends back lightweight operation list
  │     │   (compact IPC payload)
  │     │
  │     ├─ Sequential High-Speed Staging:
  │     │  ├─ cs.cs = pre_resolved_cs
  │     │  ├─ cs.execute() against TableEngine (pure in-memory inserts, zero waiting)
  │     │  ├─ extract_tags_and_evacuate_cs(cs)
  │     │  └─ Updates resolved_symbols & prunes deps graph
  │     │
  │     └─ Chunk Commit:
  │        └─ If LOW_MEMORY_MODE and executed_count >= threshold:
  │           G.TE.commit_all() & reclaim_system_memory()
  ▼
Phase 2 Workers terminate cleanly
--------------------------------------------------------------------------------------------------
Main Process (STEP 6.1+: Post-Processing Subsystems) TableEngine (G.TE)          Database (G.DB)
  │                                               │                                 │
  ├─ STEP 6.1: processing_symlinks()              │                                 │
  │  ├─ norm_target = normpath(target)            │                                 │
  │  ├─ target_fid = get_fid_for_path(norm_target)│                                 │
  │  ├─ m_bridge_file.set(vid, sym_fnid, fid) ──►│ (Direct zero-duplication alias) │
  │  └─ Broken symlink fallback -> file_processing│                                 │
  │                                               │                                 │
  ├─ Post-Processing Subsystems:                  │                                 │
  │  ├─ processing_git_commits(version) ─────────►│                                 │
  │  ├─ processing_maintainer_files(version) ────►│                                 │
  │  └─ processing_kbuild(version) ──────────────►│                                 │
  │                                               │                                 │
  ├─ Final Teardown Commit:                       │                                 │
  │  └─ G.TE.commit_all(update_in_mem_indexes=False) ──────────────────────────────►│
==================================================================================================
```
