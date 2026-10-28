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
  - `G.linux_directory`: Active repository location. Defaults to `repos/linux` until `main()` selects the configured repository (`MF.set_repo_dir()`).
  - `G.CURRENT_PARSING_FILE`: Thread/process-local relative file path for log formatting.
  - `G.LOW_MEMORY_MODE`: Enabled via `--low-mem`; throttles worker pool to `CPUS // 2` (batch size 50) and executes chunked commits every 500 ChangeSets.
  - `G.VERY_LOW_MEMORY_MODE`: Enabled via `--very-low-mem`; limits workers to `min(2, CPUS // 4)` (batch size 25) with chunked commits every 300 ChangeSets for hosts with < 64GB RAM.
  - `G.MEMORY_MODE`: String indicator (`"normal"`, `"low"`, `"very-low"`).
- **Flags & Debugging**:
  - `G.DEBUG_TYPECHECK`: Enables runtime assertions on type validation decorators.
  - `G.BP_ON_SHUTDOWN`: Triggers `sys.breakpointhook()` on emergency exit.
  - `G.BP_ON_REF_FAIL`: Triggers `sys.breakpointhook()` when a `RefType` cannot be resolved.
  - `G.PROFILING_ENABLED`: Enables AST & execution duration profiling.
  - `G.STAGE_PROFILER`: System-level `PipelineStageProfiler` instance tracking end-to-end stage wall-clock timings, items/throughput, parallel worker CPU breakdowns, and top identified bottlenecks. Worker profiles with no measured time (e.g. directory ChangeSets) are not counted as parsed files; the CPU breakdown lists only measured components, with `include_symbols_s` (`extract_include_symbols()`) separate from the libclang parse; percent cells are padded before they are coloured, so columns stay aligned.
  - `G.EXECUTION_DEADLINE`: Optional UNIX timestamp epoch after which pipeline stages halt early and print diagnostic profiling summaries.
- **Control Methods & Utilities**:
  - `G.set_execution_deadline(duration_s: float | None) -> None`: Sets an execution deadline duration in seconds from invocation.
  - `G.is_deadline_exceeded() -> bool`: Returns True if the execution deadline has been reached.
  - `G.get_time_remaining() -> float | None`: Returns remaining seconds until execution deadline.
- **Interactive quit (`core/quit_listener.py`)**: `GracefulQuitListener` puts an interactive POSIX terminal in cbreak mode (no echo or line buffering; Ctrl+C still raises SIGINT) and reads keys on a daemon thread. Pressing `q`/`Q` sets `requested` and logs `[Quit Requested]`; the release loop in `main()` checks it before each version, so the version being parsed finishes and commits, the remaining versions are skipped, and the profiler report and normal shutdown run. Terminal settings are restored on stop and via `atexit`. The listener stays off when stdin is not a TTY (pipes, CI, `nohup`) or a debugger is attached (`sys.gettrace()`), since debuggers need stdin.
  - `G.measure_stage(name: str, count: int = 0, units: str = "items")`: Context manager measuring wall-clock duration and throughput for discrete pipeline stages when profiling is active. It yields this call's metric: its `count` (initially `count`; the block may assign a better one) is added once to the stage's total, so a stage measured once per version reports the sum over all versions.
  - `G.emergency_shutdown(code: int = 1) -> None`: Cleans all paths in `gp.PURGE_LIST` and exits.
  - `G.BP() -> None`: Drops into breakpoint hook (`sys.breakpointhook()`).
  - `G.type_check(*expected_types)`: Method decorator enforcing runtime type validation.
  - `reclaim_system_memory() -> None`: Runs explicit garbage collection (`gc.collect()`) and invokes glibc `malloc_trim(0)` to return free memory pages to OS.
  - `trim_heap() -> None`: Only invokes glibc `malloc_trim(0)` (no garbage collection pass); used after TableEngine commits, whose freed rows need no cycle collection.
  - `MEMORY_RECLAIM_STATS`: Per-process calls and seconds of both helpers (`collect_calls`, `collect_s`, `trim_calls`, `trim_s`); the stage time split reports their difference as GC/trim time.
  - `freeze_system_memory() -> None`: Reclaims unreferenced memory and moves living parent objects into permanent GC generation (`gc.freeze()`), making them immune to worker GC cycle traversal and preventing Copy-on-Write page duplication.
  - `unfreeze_system_memory() -> None`: Restores permanent GC generation back to generational tracking (`gc.unfreeze()`).
  - `compute_code_hash(code: str) -> bytes`: Computes a deterministic 32-byte binary SHA-256 digest (`BINARY(32)`) from normalized code snippet strings for `m_tag_code`.
  - `is_safe_git_rev(rev) -> bool`: True when `rev` is a plain tag, branch or commit SHA safe to pass to git as an argument: at most 255 characters, starting with `[A-Za-z0-9]` and otherwise limited to `[A-Za-z0-9._/+-]`. Git reads an argument starting with `-` as an option, so a revision placed before `--` must never start with one (`--contents=<file>` makes `git blame` print that file, `--output=<file>` makes `git log` write one). `MasterFile._checked_rev()` raises `ValueError` on a rejected revision before it reaches `git archive` / `diff` / `ls-tree` / `show`; the webapp wraps the same predicate as `HTTP 400` (`webapp/backend/security/git.py`, `webapp/API.md` §2.1.1).
  - `is_safe_git_spec(spec) -> bool`: True when a `<rev>:<path>` spec is one single `git cat-file --batch` request. That protocol reads one request per line, so a newline or NUL inside a spec injects further requests and desynchronizes every later answer on the long-lived stream — each blob then arrives for the wrong path. `MasterFile._read_blob()` returns `None` for such a spec instead of writing it.
  - `clean_unnamed_spelling(spelling: str) -> str`: Normalizes Libclang anonymous/unnamed cursor spellings (e.g. `(unnamed at /dev/shm/.../include/linux/foo.h:12:1)`) down to clean relative git repository paths (`(unnamed at include/linux/foo.h:12:1)`), stripping host and RAMDISK prefix directories.
  - `setup_memory_limit(limit_gb: float = 60.0) -> None`: Enforces an OS-level 60GB boundary via Linux cgroups v2 (`memory.max=60G`, `memory.high=54G`, `memory.oom.group=1`) via `systemd-run --user --scope` re-exec, with graceful fallback to per-process address space limit (`RLIMIT_AS`), preventing Linux OOM kernel freezes during massive multicore AST translation unit parsing.


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

  - `SymbolLinkage(IntEnum)`: Linkage semantics for symbol definitions staged in `m_symbol_def`:
    - `ExternalDef = 0`: Global non-static definition.
    - `InternalStaticDef = 1`: File-local static definition in a `.c` file.
    - `PrototypeDecl = 2`: Forward declaration or header prototype without body.
    - `InlineHeaderDef = 3`: Static inline or extern inline definition in a header (`.h`).
    - `WeakOrAliasDef = 4`: `__weak` or `__alias(...)` definition.
    - `MacroOrKernelDef = 5`: Preprocessor `#define`, `SYSCALL_DEFINE*`, `TRACE_EVENT`, `DEFINE_SPINLOCK`, `EXPORT_SYMBOL`.

  - `STANDARD_C_KEYWORDS: dict[str, ASTT]`: Fast keyword-to-AST category mapping covering C control flow (`if`, `switch`, `case`, `default`, `while`, `do`, `for`, `return`, `break`, `continue`, `goto`, `asm`), qualifiers (`const`, `volatile`, `restrict`, `_Atomic`), storage classes (`static`, `extern`, `typedef`, `inline`), and primitive types (`void`, `char`, `short`, `int`, `long`, `signed`, `unsigned`, `float`, `double`, `_Bool`, `bool`, `_Complex`, `_Imaginary`, `__float128`, `_Float128`, `__fp16`, `_Float16`, `char16_t`, `char32_t`, `wchar_t`, `struct`, `union`, `enum`).
  - `ASTT(IntEnum)`: AST construct category identifiers across C (`C_struct`, `C_Compound`, `C_SizeofExpr`, `C_TypeRef`, `C_PerCpuVarDef`, `C_ModuleParamDef`, `C_CastExpr`, `C_ConditionalOperator`, `C_ArraySubscriptExpr`, `C_DesignatedInitExpr`, `C_CompoundAssignOperator`, `C_CompoundLiteralExpr`, `C_IndirectGotoStmt`, `C_AddrLabelExpr`, `C_NullStmt`, `C_ParenExpr`, `C_InitcallDef`, `C_ExportSymbolDef`, `C_Bitfield`, `C_char16`, `C_char32`, `C_wchar`, `C_longdouble`, `C_float128`, `C_float16`, `C_Vector`, `C_vla`), Preprocessor (`CPPro_define`, `CPPro_include`), ASM (`ASM_Instruction`, `ASM_Macro`), and Kconfig (`Kconfig_Config`, `Kconfig_Menu`, `Kconfig_Choice`, `Kconfig_Depends_On`, `Kconfig_Select`, `Kconfig_Op_And`, etc.).


---

## 2. Filesystem & Git Subsystem (`core/FileHandler.py`)

### 2.1. `MasterFile` (`mf`)
Manages RAMDISK temporary working trees and Git repository extraction.
- **State**:
  - `version_dict`: `dict[str, str]` &mdash; `version_name -> ramdisk_path`.
  - `file_dict`: `dict[str, dict[str, str]]` &mdash; `version_name -> {relative_path: file_content}`.
  - `workspace_dir`: `str | None` &mdash; Path to persistent RAMDISK workspace directory.
  - `_cat_file_proc`: `sp.Popen | None` &mdash; Persistent `git cat-file --batch` subprocess handle.
- **Key Methods**:
  - `_checked_rev(rev: str) -> str`: Returns `rev`, or raises `ValueError` when `is_safe_git_rev()` rejects it (§1.1). Every method below that interpolates a revision into a git argument passes it through this guard first (`git_clone`, `get_file`, `generate_change_list`, `git_file_list`, `get_dir_list`); `_commit_start_sequence()` and `get_root_commit()` check the predicate directly and return their empty result instead of raising.
  - `create_temp_dir() -> str`: Spawns `/dev/shm/code-parser.XXXXXX` via `mktemp`.
  - `add_version(version_name: str, purge_list: list) -> None`: Clones version tree into RAMDISK and registers in `purge_list`.
  - `sync_workspace(target_rev: str, change_list: list[str]) -> str`: Incrementally updates the persistent `/dev/shm` workspace by streaming only added/modified blobs (`A`, `M`, `R`) from persistent `git cat-file --batch` and `os.unlink()`ing deleted paths (`D`), preserving full compiler `#include` headers without full archive extraction.
  - `git_clone(version: str) -> str`: Executes `git archive <version> | tar -x` and provisions `include/asm` and `include/uapi/asm` symlinks pointing to `asm-generic`.
  - `get_file(file_path: str, version: str) -> str`: Returns cached content from RAMDISK (latin-1) or queries `git show <version>:<file_path>`.
  - `read_file(file_path: str, version: str) -> bytes`: Reads raw bytes from RAMDISK. Defensively inspects paths: if `p.is_symlink() and p.is_dir()` or on read errors, extracts link target via `os.readlink(p).encode("utf-8")` to eliminate `[Errno 21] Is a directory` exceptions on directory symlinks (e.g. `arch/*/boot/dts/include/dt-bindings`).
  - `generate_change_list(gp: GreatProcessor) -> list[str]`: Runs `git diff <Old_Version_Name> <Version_Name> --name-status` and sets `gp.Change_List`.
  - `git_file_list(version: str) -> str`: Executes `git ls-tree -r --name-only <version>`; successful listings are cached per (repository, version) (at most 4, dropped by `clear_version_cache()` / `clear_all_version()` / `set_repo_dir()`), since one version is listed by several pipeline steps.
  - `get_dir_list(version_name: str) -> list[str]`: Executes `git ls-tree -r -d --name-only <version_name>`.
  - `get_version_tags(start_ref: str | None = None, include_pre_releases: bool | None = None) -> list[str]`: Discovers and sequences repository release tags using `git -c versionsort.suffix=-rc -c versionsort.suffix=-pre tag -l --sort=v:refname`, ensuring `-rc#` pre-releases are correctly ordered prior to final releases. Release candidates and other pre-releases (`-rc`, `-pre`, `-alpha`, `-beta`) are included by default (`include_pre_releases=None` → `True`, config `parser.include_pre_releases`, default `true`), so a run walks e.g. `v3.0 → v3.1-rc1 … v3.1-rc10 → v3.1`; `--no-rc` / `--releases-only` (or `"include_pre_releases": false`) keeps final releases only. `-tree` tags always are skipped (they point at trees, not commits). When `start_ref` is not a listed tag but resolves to a commit (e.g. the repository's first commit), `_commit_start_sequence()` returns `[start, *tags containing start]` in version order; a tag pointing exactly at `start` (e.g. `v2.6.12-rc2` on Linux's root commit) replaces the raw SHA as the first snapshot name.
  - `get_root_commit(rev="HEAD") -> str | None`: First commit of `rev`'s history (`git rev-list --max-parents=0 --first-parent`).
  - `resolve_path(file_path: str) -> str`: Strips working directory prefix to return repo-relative path.
  - `trim_version(keep: int = 2) -> int`: Deletes oldest RAMDISK directory if cached versions exceed `keep`.
  - `clear_all_version() -> None`: Cleans all version trees from disk, terminates `_cat_file_proc`, and resets dictionaries.

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
  - `worker_change_list: list[str] | None`: Filtered subset of changed files passed to multicore workers after content-addressable blob deduplication (`Step 4.5`), preserving authoritative git diff status in `gp.Change_List`. `None` means Step 4.5 did not filter (workers parse `gp.Change_List`); an empty list means every change was deduplicated and nothing is parsed (`main._select_worker_changes()`).
  - `Symlink_List: list[str]`: Buffers symlinks partitioned from `gp.Change_List` for zero-duplication deferred aliasing directly into `m_bridge_file` via `processing_symlinks()`.
  - `ChangeSet_Dict: dict[str, ChangeSet] | CompressedChangeSetDict`: Main process dictionary mapping relative file paths to parsed `ChangeSet` objects.
  - `Alt_ChangeSet_Dict: dict[str, ChangeSet] | CompressedChangeSetDict`: Secondary cache for on-demand parsed foreign `ChangeSets` during cross-file reference resolution.
  - `Shared_ChangeSet_Dict_List: list[bytes] | None`: IPC list holding worker `pickle.dumps()` payloads.
  - `unchanged_symbol_cache: dict[str, dict[tuple[str, int], int]]`: In-memory cache of symbol mappings `(name, type_id) -> ast_id` queried from the database for unchanged files.
  - `blob_symbol_cache: dict[int, dict[tuple[str, int], int]]`: The same maps by blob id, kept across versions (a blob's rows never change), filled only for files unchanged in the current update. `get_file_symbols_from_db()` checks it before querying. Forked workers read the parent's copy and collect what they load (`_blob_symbols_fetched`) and look up (`_blob_symbols_used`); their done frame carries both and `_run_worker_stage` merges them (`merge_blob_symbols()`). `prune_blob_symbol_cache(max_idle_versions=8)`, called by `reset_cs()`, advances the version counter and drops maps not looked up for 8 versions (`blob_symbol_used`). Each worker used to re-query every unchanged header it needed in every version (8–30s of worker time per small version).
  - `_changed_paths_set: set[str]`: Set of all relative file paths modified in the active release version.
  - `file_deps: dict[str, set[str]]`: Lightweight dependency graph mapping each changed file path to its set of unresolved foreign file dependencies (`foreign_deps`).
  - `cs_op_counts: dict[str, int]`: Operation count of each ingested ChangeSet, recorded by `DependencyScheduler.ingest_batch()`; the wave pass breaks cycles with it instead of decompressing stored ChangeSets.
  - `commit_ids_by_hash: dict[str, int]`: Commit hash -> `commit_id` for the version's range, registered in bulk by `ingest_git_dag()` (Step 1) and reused by `processing_git_commits()` (Step 6.5).
    `ingest_git_dag()` reports a version as already existing when its head commit's ingestion is done (`m_ingest_state`, §4.1), whether under this ref or another one (tags sharing a commit, e.g. `v2.6.32-rc1`/`v2.6.32-rc2`: the new ref is then only registered in `m_git_ref`, since re-ingesting the commit would insert duplicate `m_bridge_checkpoint` rows). An interrupted ingestion is rolled back and parsed again (§4.1).
  - `deduped_paths: set[str]`: Changed paths whose blob Step 4.5 reused instead of parsing; `processing_git_commits()` loads their tags from `m_bridge_tag` to link them to this range's commits.
  - `file_symbols: dict[str, dict[tuple[str, int], int]]`: Per-file exported symbols map `rel_file -> {(sym_name, type_id): ast_id}` populated during Phase 1 staging.
  - `completed_paths: set[str]`: Paths whose ChangeSet executed in the current update, recorded by `extract_tags_and_evacuate_cs()` (even when the file publishes no symbol). `resolve_ref()` treats these as completed after Stage 2 clears `file_symbols` (symbols moved to `_shared_symbols`); reset by `init_cs_dict()`.
  - `file_names: dict[str, dict[str, int]]`: Per-file exported symbol names map `rel_file -> {sym_name: ast_id}` for name-only fallback resolution.
  - `file_pre_extracted_tags: dict[str, list[tuple[int, int, int, int]]]`: Lightweight bridge tags map `rel_file -> [(tag_id, blob_id, line_s, line_e)]` populated upon ChangeSet evacuation to allow zero-decompression tag-to-commit mapping during post-processing.
  - `file_blob_ids: dict[str, int]`: Mapping `rel_file -> blob_id` populated during ChangeSet evacuation to enable $O(1)$ checkpoint staging without decompressing pickled ChangeSets.
  - `_ast_negative_cache: set[tuple[str, int]]`: Negative query cache tracking verified non-existent `(symbol_name, type_id)` AST symbols across TableEngine and database queries, cleared upon chunk commits.
  - `_shared_symbols`: `SharedSymbolChain | None` &mdash; File-scoped shared memory symbol segments published during Stage 2 (`main._publish_file_symbols()`), inherited by forked Stage 2 workers and read by `TableHandling.resolve_ref()` (step 0b) to resolve cross-file symbols in $O(1)$ time without heap duplication or IPC serialization. Released by `close_shared_symbols()` from `DependencyScheduler.finalize()`, `init_cs_dict()` and `reset_cs()`, so a version never resolves against a previous version's table.
  - `_commit_proc` / `_commit_result_dir`: Background git commit parser process (`main._git_commit_worker`, forked by `update()` right after Step 4) and the RAMDISK directory (registered in `PURGE_LIST`) where it pickles `(commits, file_hunks_map)` before exiting. `processing_git_commits()` waits for it via `main._collect_git_commit_result()`; `stop_commit_worker()` terminates it and deletes the directory.
  - `parsed_blob_ids: set[int]`: Registry of parsed content `blob_id`s in current wave/run to eliminate redundant worker dispatches on identical copies (C100).
- **Worker Lifecycle, Multi-Epoch Streaming & Copy-on-Write Isolation**:
  - **Worker Heap Isolation**: Workers shallow-copy `gp_ref` on startup and re-point large symbol/ChangeSet collections (`ChangeSet_Dict`, `_ast_staged_symbols`, `_ast_staged_names`, `unchanged_symbol_cache`, `file_symbols`, `file_names`, `_ast_negative_cache`, `_blob_symbols_fetched`, `_blob_symbols_used`) to empty process-local containers (`blob_symbol_cache` stays shared and is only read), and set `gp_ref._is_worker = True` so `resolve_ref()` never stages stubs into the worker's never-committed TableEngine. Worker-local mutations never dirty parent memory pages or trigger Copy-on-Write duplication.
  - Workers invoke `reclaim_system_memory()` after processing each batch to release fragmented C-heap pages back to the OS via glibc `malloc_trim(0)`.
  - **Worker Stage Runner (`main._run_worker_stage`)**: Stage 1, every Stage 2 epoch and retries share one runner. Workers are forked from the `fork` multiprocessing context with the parent DB socket closed and reopened around the fork (`on_started` callbacks run after the reopen). Each worker owns a duplex pipe (no shared task queue or lock): the parent's reader thread sends it batches in order, two ahead of the one it parses, and a stop frame once none is left; the worker answers each with one result frame (pickled once, `(batch_cs_dict, per_file_errors)`, zlib-compressed in low memory modes) and ends with a done frame. The reader thread hands at most 2 serialized batches to the ingest thread, and `send_bytes` blocks while the parent is behind, so memory stays bounded on both sides. A pipe closed without its done frame marks the worker as crashed: the oldest batch it had not returned is the one it died on (re-parsed one file per batch), and its other queued batches go back to the surviving workers. Retry rounds continue while they make progress; files that kill a worker again are reported. If no worker can initialize (an init-failure frame from every worker, e.g. the database is unreachable), the stage raises. Ingest failures are recorded per batch without stopping the stage. The deadline is checked while waiting, so an idle stage still stops on time. Stage 1 header batches are bounded by bytes and kept in include order; Stage 1 and each epoch log their parsing time and the gap between the first and the last worker exit.
  - **Background commits during stages**: Between forking its workers and reaping them, `_run_worker_stage` sets `G.TE.async_commits` (`_begin_background_commits()`), so chunk commits triggered while ingesting are written by the TableEngine's writer thread while the parent keeps ingesting. `_end_background_commits()` switches them off and joins the last one before anything forks again (also called first thing in every stage); a failed background commit put its rows back into staging, and they are committed again there through `_commit_all_with_retry()`. While the parent waits for worker results (a 1s poll without a batch), `_maybe_idle_commit()` also starts a background commit once 100k rows are staged (`_IDLE_COMMIT_MIN_ROWS`, `G.TE.staged_rows`) and none is running: chunk commits come every 1000 executed files, so a small version used to write all its rows in one commit after parsing with every worker idle. Commits outside the stages (header resolution, epoch boundaries, Phase 2, Steps 6.x and 7) stay synchronous.
  - **Stage time split**: Each stage logs `<label> time split: ...` (`_stage_time_split()`, `_format_time_split()`): how long the parent waited for results, decoded and ingested them (with its database reads), how long it was blocked by commits out of how long they took to write and how many rows, its GC/trim time, and the workers' share of time parsing (dependency pruning and database reads called out), blocked sending results to the parent, without a task (including the time a worker had already exited while another still parsed, `worker_tail_idle_s`), and in GC. Workers measure their part and send it with their done frame (`_WORKER_SPLIT_KEYS`; a bare done frame is still accepted). In normal memory modes the parent no longer runs a full collection every 10 ingested batches (the automatic collector and the trim after each commit cover it). Batch sizes shrink for small stages (`_balanced_batch_size()`: about 4 batches per worker, at least 4 files each, at most the memory profile's size) so a version with a few hundred changed files does not leave most workers waiting on one large batch. When Stage 2's sources (or Stage 1's headers, kept in include order) fit in one epoch, `partition_source_epochs(num_workers=...)` also caps a batch's bytes at an equal share (`_BATCHES_PER_WORKER` batches per worker), so batches cost about the same.
  - **Symbol counters**: Workers send `core/SymbolRows.STATS` with their done frame (`symbol_stats`), and each version logs `[Symbols] '<version>': N items stored in tags (M of them in reused tags, not rewritten); K per blob (changed reused tag, conflicting key, outside tag, without tag); R keys refined by position` (`_log_symbol_stats()`). Many items per blob under "changed reused tag" means reused tags are not being shared.
  - **Memory Modes (`main._memory_profile()`)**: normal: `CPUS - 1` workers, 200-file header batches, Stage 2 batches of 200 (50 with heavy files) up to 1.5MB, ~4,500-file epochs; low: `CPUS // 2` workers, 50 / 50 (25) / 750KB / 4,500; very-low: `min(2, CPUS // 4)` workers, 25 / 25 (10) / 350KB / 2,000.
  - **Stage 2 Multi-Epoch Streaming**: Source files are partitioned into natural subsystem-aligned epochs (bounded at modular directory transitions) using project-agnostic dynamic complexity batching: batches are bounded by file count (smaller when heavy AST files &ge;64KB are present) and cumulative source bytes, and each epoch's batches are queued largest-first so the epoch does not end waiting on one late heavy batch.
  - Workers run for the duration of an epoch, then exit and are reaped at the epoch boundary. Each epoch logs its parsing time, the gap between the first and the last worker exit, the boundary time, parent RSS and `gc.get_freeze_count()`, after its time split line.
  - **Inter-Epoch Publishing & Deferred Drain**: At each epoch boundary, `DependencyScheduler.drain_deferred_queue()` unblocks deferred ChangeSets, TableEngine commits staged rows (`G.TE.commit_all()`), and the symbols published since the previous boundary are packed into one new file-scoped segment appended to `gp._shared_symbols`, so later epochs pre-resolve cross-file symbols in $O(1)$ time. Parent heap symbol dictionaries (`file_symbols`, `_ast_staged_symbols`) are cleared after publishing to keep parent resident memory low.
- **Memory Compression (`CompressedChangeSetDict`)**:
  - Implements a hybrid zero-overhead store backed by `_evacuated_store` (storing lightweight evacuated ChangeSets directly in memory with zero `pickle` or `zlib` CPU cost) and an LRU cache (`OrderedDict`, default capacity 500 items, throttled to 25 items under `--very-low-mem`).
  - Active ChangeSets remain in `_lru_cache` uncompressed upon insertion, and are only serialized and compressed (`zlib.compress(pickle.dumps(cs), level=1)`) into `_compressed_store` when evicted from `_lru_cache` upon exceeding capacity.
  - Tracks dirty uncompressed keys via `_dirty_keys` with clean LRU eviction: only entries marked dirty (or uncompressed) are re-pickled and re-compressed when evicted from `_lru_cache`.
  - Exposes `mark_dirty(key)` to flag in-place mutated objects, `flush_dirty()` to synchronize dirty items prior to serialization, and gracefully falls back to uncompressed LRU caching if zlib compression fails.
  - Provides `clear()` to thoroughly release compressed blobs, evacuated instances, LRU items, and dirty key sets upon completion of an update cycle.
- **IPC Protocol & Query Helpers**:
  - `start_manager() -> None`: Initializes `Shared_ChangeSet_Dict_List = []`.
  - `push_set_to_main() -> None`: Executed by worker; serializes `ChangeSet_Dict` via `pickle.dumps()` onto `Shared_ChangeSet_Dict_List`.
  - `stop_manager() -> None`: Executed by main process; deserializes (`pickle.loads()`) and merges worker dictionaries into `gp.ChangeSet_Dict`.
  - `safe_get_cs(path: str) -> ChangeSet | None`: Lookup sequence: `ChangeSet_Dict` &rarr; `Alt_ChangeSet_Dict` &rarr; checks existence in RAMDISK tree (skips non-kernel system paths `/usr/`, `/etc/`, `/lib/`, `/opt/`) &rarr; creates `ChangeSet("M", path)`, triggers `CS.parse()`, stores in `Alt_ChangeSet_Dict`, and returns `CS`.
  - `get_file_symbols_from_db(rel_file: str) -> dict[tuple[str, int], int] | None`: Queries TableEngine joined tables (`m_file_name` &rarr; `m_bridge_file` &rarr; `m_bridge_tag` &rarr; `m_tag` &rarr; `m_ast`) to extract active symbol mappings `(ast_name, ast_type) -> ast_id` for unchanged files without re-parsing source text. Falls back to `Old_VID` only for files outside `_changed_paths_set`: a changed file without a `VID` row yet returns `None` uncached, since its previous version's symbols would be stale.
  - `reset_cs() -> None`: Clears `Change_List`, `Symlink_List`, `ChangeSet_Dict`, `Alt_ChangeSet_Dict`, `unchanged_symbol_cache`, `file_deps`, `file_symbols`, `file_names`, `file_pre_extracted_tags`, `file_blob_ids`, and `_ast_negative_cache`, prunes `blob_symbol_cache`; stops the git commit worker (`stop_commit_worker()`) and unmaps the shared symbol segments (`close_shared_symbols()`).

---

## 4. Relational Database Schema Registry (`core/DBLayout.py`)

48 Zero-VID Git-Native Core Tables defined via `Table` instances and exported in `TABLES` tuple (`init_db_layout(gp)` sets `gp.Table_Array = list(TABLES)`). The database schema eliminates synthetic linear release versions (`vid`), modeling the kernel codebase directly as a Git DAG rooted at `m_repository` $\to$ `m_commit` $\to$ `m_bridge_checkpoint` $\to$ `m_file_blob`:

| `table_id` | Table Name | Columns | Primary Key | `no_duplicate` | `te_cached` | `hashing_table` | Description |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **0** | `m_project` | `(project_id, name, slug)` | `("project_id",)` | `True` | `True` | `False` | Top-level project container |
| **1** | `m_file_name` | `(fnid, fname)` | `("fnid",)` | `True` | `True` | `False` | Unique repository path string registry |
| **2** | `m_file_blob` | `(blob_id, blob_hash, ftype)` | `("blob_id",)` | `True` | `True` | `False` | Content-addressed file blob store (aliased as `m_file`) |
| **3** | `m_bridge_checkpoint` | `(commit_id, fnid, blob_id)` | `("commit_id", "fnid")` | `False` | `True` | `False` | Checkpoint tree bridge (aliased as `m_bridge_file`) |
| **4** | `m_blob_transition` | `(old_blob_id, new_blob_id)` | `("old_blob_id", "new_blob_id")` | `False` | `True` | `False` | Inter-blob transition delta (aliased as `m_moved_file`) |
| **5** | `m_type_descriptor`| `(type_id, name)` | `("type_id",)` | `False` | `True` | `False` | AST node type registry (seeded from `ASTT`) |
| **6** | `m_ast` | `(ast_id, name, type_id)` | `("ast_id",)` | `False` | `False`| `"m_ast_hash"` | AST symbol nodes |
| **7** | `m_ast_container` | `(ast_id, priority, type_id, ref_ast_id)`| `("ast_id", "priority")`| `False` | `False` | `False` | AST child hierarchy links |
| **8** | `m_ast_include` | `(ast_id, fnid)` | `("ast_id",)` | `False` | `False`| `False` | AST `#include` / `source` references |
| **9** | `m_ast_debug` | `(ast_id, ast_raw)` | `("ast_id",)` | `False` | `False`| `False` | JSON dumps of AST structures |
| **10** | `m_tag_code` | `(hash, code)` | `("hash",)` | `False` | `("hash",)` | `False` | Code snippet registry (32-byte binary SHA-256 hash) |
| **11** | `m_tag` | `(tag_id, hash, ast_id, hl_s, hl_l)` | `("tag_id",)` | `False` | `False` | `False` | Content-addressed code snippet tag |
| **12** | `m_bridge_tag` | `(blob_id, tag_id, line_s, line_e, char_s, char_e)` | `("blob_id", "tag_id")` | `False` | `False` | `False` | Blob coordinate and line mapping |
| **13** | `m_map_ast` | `(map_id, line_s, char_s, line_e, char_e, ast_id)`| `("map_id", "line_s", ...)`| `False` | `False`| `False` | Spatial AST coordinate region |
| **14** | `m_bridge_map` | `(tag_id, map_id)` | `("tag_id", "map_id")` | `False` | `False` | `False` | Tag-to-AST spatial map bridge |
| **15** | `m_ast_hash` | `(hash, ast_id)` | `("hash",)` | `False` | `True` | `False` | Binary 32-byte SHA-256 AST structural hash deduplication |
| **16** | `m_kconfig_symbol` | `(kcid, name, type, prompt, def_val, help, ast_id)` | `("kcid",)` | `True` | `True` | `False` | Content-deduplicated Kconfig symbols |
| **17** | `m_kconfig_relation`| `(rel_id, kcid, target_name, rel_type, cond_ast_id, priority)`| `("rel_id",)`| `True` | `True` | `False` | Direct depends_on / select / imply / choice_member rules |
| **18** | `m_kconfig_tree` | `(tree_id, blob_id, parent_id, node_type, title, kcid, priority, dep_ast_id, ast_id)` | `("tree_id",)` | `False` | `True` | `False` | Hierarchical Menuconfig tree per blob |
| **19** | `m_kconfig_kbuild`| `(kcid, makefile_blob_id, target_fnid, compile_mode, target_obj)` | `("kcid", "makefile_blob_id", "target_fnid", "compile_mode")` | `False` | `True` | `False` | Kconfig to compiled object mapping |
| **20** | `m_maintainer_person`| `(person_id, name, email)` | `("person_id",)` | `True` | `True` | `False` | Canonical person identity registry |
| **21** | `m_maintainer_section`| `(sec_id, blob_id, name, status, scm_tree, web_page, mailing_list, ast_id)` | `("sec_id",)` | `True` | `True` | `False` | Subsystem maintainer sections per blob |
| **22** | `m_maintainer_member`| `(sec_id, person_id, role_type, priority)` | `("sec_id", "person_id", "role_type")` | `False` | `True` | `False` | Maintainer subsystem personnel roles |
| **23** | `m_maintainer_pattern`| `(sec_id, pat_type, pattern, priority)` | `("sec_id", "pat_type", "pattern", "priority")` | `False` | `True` | `False` | Section file matching patterns |
| **24** | `m_maintainer_file`| `(maintainers_blob_id, fnid, sec_id)` | `("maintainers_blob_id", "fnid", "sec_id")` | `False` | `False` | `False` | Path to maintainer section resolution |
| **25** | `m_credits_entry` | `(credit_id, blob_id, person_id, web_page, pgp_key, description, snail_mail, ast_id)` | `("credit_id",)` | `True` | `True` | `False` | CREDITS contributor biographies |
| **26** | `m_commit` | `(commit_id, network_id, commit_hash, author_id, author_date, committer_id, committer_date, subject, message)` | `("commit_id",)` | `False` | `False` | `False` | Git commit metadata registry |
| **27** | `m_bridge_commit_person`| `(commit_id, person_id, role_type, priority)` | `("commit_id", "person_id", "role_type")` | `False` | `False` | `False` | Commit author, committer, and trailers (write-only, idempotent: not preloaded) |
| **28** | `m_commit_file` | `(commit_id, fnid, blob_id, old_fnid, old_blob_id, change_type)` | `("commit_id", "fnid")` | `False` | `False` | `False` | Files touched per commit (aliased as `m_bridge_commit_file`) |
| **29** | `m_bridge_commit_tag`| `(commit_id, blob_id, tag_id)` | `("commit_id", "tag_id")` | `False` | `False` | `False` | Tags affected per commit |
| **30** | `m_moved_tag` | `(s_tag_id, e_tag_id)` | `("s_tag_id", "e_tag_id")` | `False` | `False` | `False` | Tag cross-blob evolution tracking |
| **31** | `m_blob_symbol_def` | `(def_id, blob_id, tag_id, ast_id, name, type_id, linkage, line_s, line_e)` | `("def_id",)` | `False` | `False` | `False` | Symbol definitions stored per blob: outside their tag, tagless, or a recycled tag whose definitions changed (see `m_symbol_def` view) |
| **32** | `m_blob_symbol_ref` | `(ref_id, blob_id, tag_id, ast_id, target_fnid, role, line, char_s)` | `("ref_id",)` | `False` | `False` | `False` | Symbol references stored per blob (same cases) |
| **33** | `m_file_reference` | `(ref_id, source_blob_id, target_fnid, ref_type, line_no, details)` | `("ref_id",)` | `False` | `False` | `False` | Cross-file usage and includes |
| **34** | `m_db_instance` | `(instance_hash, created_at)` | `("instance_hash",)` | `True` | `False` | `False` | Database instance fingerprint |
| **35** | `m_ast_attribute` | `(ast_id, priority, name, val)` | `("ast_id", "priority")` | `False` | `False` | `False` | AST compiler/kernel attributes |
| **36** | `m_git_network` | `(network_id, project_id, name, root_commit_hash)` | `("network_id",)` | `False` | `True` | `False` | Multi-remote repository network |
| **37** | `m_repository` | `(repo_id, network_id, name, git_url, default_branch)` | `("repo_id",)` | `False` | `True` | `False` | Tracked Git repository instance |
| **38** | `m_git_ref` | `(ref_id, repo_id, ref_type, ref_name, commit_id, tagger_id, tagger_date, tag_message)` | `("ref_id",)` | `False` | `True` | `False` | Named Git reference (aliased as `m_v_main`); `ref_type`: 1 = tag, 2 = branch (local or remote), 3 = bare commit |
| **39** | `m_commit_parent` | `(commit_id, parent_commit_id, parent_idx)` | `("commit_id", "parent_idx")` | `False` | `False` | `False` | Full Git commit DAG topology (write-only, idempotent: not preloaded) |
| **40** | `m_ingest_state` | `(network_id, commit_id, status, updated_at)` | `("network_id", "commit_id")` | `False` | `False` | `False` | Ingestion status of a version's head commit: 1 = parsing, 2 = done (§4.1); the webapp only lists done versions |
| **41** | `m_tag_def` | `(tag_id, def_idx, sym_key, name, type_id, linkage, rel_line_s, rel_line_e)` | `("tag_id", "def_idx")` | `False` | `False` | `False` | A tag's definitions, lines relative to the tag, written once when the tag is new |
| **42** | `m_tag_ref` | `(tag_id, ref_idx, sym_key, role, rel_line, char_s)` | `("tag_id", "ref_idx")` | `False` | `False` | `False` | A tag's references, written once when the tag is new |
| **43** | `m_blob_symbol` | `(blob_id, sym_key, target_ast_id, target_fnid)` | `("blob_id", "sym_key")` | `False` | `False` | `False` | Per-blob resolution of the keys the shared rows use (idempotent) |
| **44** | `m_symbol_own` | `(blob_id, tag_id, kind)` | `("blob_id", "tag_id", "kind")` | `False` | `False` | `False` | Tag placements whose references (1) / definitions (2) are stored per blob instead (idempotent) |
| **45** | `m_ingest_watermark` | `(commit_id, marked_table_id, max_id)` | `("commit_id", "marked_table_id")` | `False` | `False` | `False` | Highest id of each id-allocating table when a version run started (§4.1); no foreign keys, removed when the version is done |
| **46** | `m_ingest_blob` | `(commit_id, blob_id)` | `("commit_id", "blob_id")` | `False` | `False` | `False` | Pre-existing blobs (never parsed, e.g. from intermediate commits) a version run parses (§4.1); removed when the version is done |
| **47** | `m_arch_root` | `(commit_id, arch, role, priority, path, target)` | `("commit_id", "arch", "role", "path")` | `False` | `False` | `False` | Build layout of every ARCH value at a version's head commit, evaluated from its Makefiles (`parser/arch_layout.py`, `parser/kconfig_ast/KCONFIG_API.md` §6.5): role 1 source directory, 2 include directory (`priority` = `-I` order), 3 symlink the build creates (`path` -> `target`), 4 generated asm-generic wrapper (`path` -> the header it includes), 5 forced `-include` file; directories end with `/`. Written by Step 6.8 |

> [!NOTE]
> **Symbol views** (`core/SymbolRows.py`, created by `main.ensure_db_views()`): `m_symbol_def` and `m_symbol_ref` are SQL views returning, in the former per-blob shape, the per-blob rows plus the shared rows placed through `m_bridge_tag` (`line = bt.line_s + rel_line`, target from `m_blob_symbol`), skipping placements marked in `m_symbol_own`. A shared definition's `def_id` is computed (`1 << 62 | blob_id << 37 | tag_id << 6 | def_idx`) and a shared reference's `ref_id` is NULL, so neither can be looked up by id efficiently. `symbol_view_rows()` computes the same rows from in-memory tables (MockDB, tests). Filtering a view by `ast_id` is slow (the database joins each blob using the target with every tag holding its key); `rows_of_targets_sql(view, n)` returns the same rows for `n` target AST ids starting from their keys (`m_blob_symbol.target_ast_id` → `m_tag_ref` / `m_tag_def` by `sym_key` → `m_bridge_tag` → `m_blob_symbol`), for use as a derived table. Keys come from the tag's text (`parser/c_ast/CAST_API.md` §9.3); `m_tag_def` and `m_tag_ref` are indexed on `(sym_key, tag_id)`. A database that still has `m_symbol_def` / `m_symbol_ref` as tables is refused (run `--reset`; the reset drops them).

> [!IMPORTANT]
> **ChangeSet Tag Reference Order Invariant**: When staging tags in ChangeSets (`with CS(REF_POS):`), `m_tag.set` MUST be the first operation inside the block (`m_tag.set(None, code_hash, ast_ref, 0, 0)`) so that `tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))` points directly to `m_tag`. Auxiliary deduplication tables (such as `m_tag_code.get_set`) must always be staged after `m_tag.set` within the block.

> [!NOTE]
> **Foreign Key Handling During Setup**: Table creation wraps execution in `SET FOREIGN_KEY_CHECKS = 0;` and restores checks in a `finally` block, safely supporting forward and circular foreign key relationships.

### 4.1. Version Ingestion State & Recovery of Interrupted Runs (`core/ingest_state.py`)
The TableEngine commits each table atomically but tables independently, so a run stopped in the middle (crash, kill, `--duration` limit) can leave half-written rows: a blob with tags but without its symbol rows, a tag without its definitions, an AST node without its children. Content-addressed deduplication (`_reuse_parsed_blobs()`, tag and AST hashes) would reuse them in every later version. Each version run is therefore bracketed:
- **Begin** (`ingest_state.begin()`, end of `ingest_git_dag()` once the head commit, its DAG and its `m_git_ref` row exist): `m_ingest_state` = `INGEST_PARSING` and one `m_ingest_watermark` row per id-allocating table (`marked_tables()`: single INT primary key that is not a foreign key, outside the static registries `m_type_descriptor`, `m_project`, `m_git_network`, `m_repository`, `m_git_ref`). Ids are allocated increasingly by a single writer, so every row above a watermark belongs to the run.
- **Step 4.5** (`_record_reparsed_blobs()`): blobs that already exist but are parsed now (they are not reused) go to `m_ingest_blob` before any of their rows are written.
- **Done** (`finish_version_ingest()`, end of `update()`, also after a graceful `q` quit, which finishes the version): `INGEST_DONE`, and the version's watermark and blob rows are removed. Early returns on the duration limit leave the version `INGEST_PARSING`.
- **Next start** (`_previous_ingest()` in `ingest_git_dag()`): `done` skips the version (registering an alias ref when another ref ingested the commit); `interrupted` (`INGEST_PARSING`) calls `rollback_version()` and parses the version again from Step 1. Refs without a state row predate this mechanism: they count as done when their checkpoint covers at least 90% of the tree (`INGEST_DONE` is then backfilled), otherwise their version-scoped rows (`m_bridge_checkpoint`, `m_commit_file`, `m_bridge_commit_tag`, `m_bridge_commit_person` of the head commit) are purged as before; their blob-level rows cannot be identified.
- **Rollback** (`rollback_plan()` + `rollback_version()`): first `precheck_queries()` look for rows older than the run (id at or below their table's watermark) that reference rows of the run; any hit raises `RecoveryError` instead of deleting older data. The plan then deletes, children before parents along the declared foreign keys: the version's `m_bridge_checkpoint` and `m_arch_root` rows; every row derived from the `m_ingest_blob` blobs in the tables referencing `m_file_blob` (except the commit-scoped `m_bridge_checkpoint`, `m_commit_file`, `m_bridge_commit_tag`, `m_blob_transition`), including the rows hanging off them (e.g. `m_maintainer_member` of a deleted section); and for each watermarked table, in child-first order, the link-table rows referencing ids above its mark, then its own rows above the mark (a watermarked child's new rows go with its own mark). Statements run as `DELETE ... LIMIT 50000` loops, each chunk committed; every one uses the foreign-key indexes, and re-running a partially executed rollback is harmless. The TableEngine then drops its carried tables (`discard_carried()`) and `main.py` clears its in-process caches (`_forget_rolled_back_rows()`).

---

## 5. Data Staging, Routing & Reference Resolution (`core/TableHandling.py`)

### 5.1. Data Sanitization Helpers
- `to_safe_data(val: Any) -> SafeDataType`: Coerces Enums/IntEnums, booleans, and custom objects to native Python `int`, `str`, or `None`.
- `is_data_unsafe(data: tuple) -> bool`: Returns `True` if any element in `data` is a reference tuple.
- `normalize_data_tuple(data: tuple) -> tuple[UnSafeDataType, ...]`: Converts primitive elements via `to_safe_data()` while preserving reference tuples intact.

### 5.2. `Table` Class Interface
- **Dynamic Pointer Attributes**: On init, sets `self.<col_name> = (table_id, col_idx)` (e.g. `m_file_name.fnid = (1, 0)`). Injects table instance into `parser.c_ast.c_ast` and `parser.c_ast`.
- **Extended Schema Attributes**:
  - `table.indexes`: Secondary multi-column indices: `(("idx_name", ("col_a", "col_b")), ...)`.
  - `table.unique_keys`: Explicit unique key constraints: `(("col_a", "col_b"), ...)`.
  - `table.idempotent_pk`: Flag enabling deduplicated insertion: rows whose primary key already exists are skipped individually (`MariaDB.insert` appends a no-op `ON DUPLICATE KEY UPDATE`).
  - `table.blob_scoped`: Flag marking blob-scoped content-addressable preloading.
  - Backward-compatible column aliases: `m_file_blob.fid = m_file_blob.blob_id`, `m_git_ref.vid = m_git_ref.commit_id`, `m_git_ref.vname = m_git_ref.ref_name`.
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
     - **Step 0b (File-Scoped Shared Symbols)**: Queries the file-scoped shared table (`_phase2_symbols` set by `pre_resolve_operations()` in workers, else `gp._shared_symbols`) for `(sym_name, sym_type)` then `sym_name` defined in `rel_file`, the same semantics as step 0. Plain `(name, type)` snapshot dicts passed to `pre_resolve_operations()` are still matched globally.
     - **Step 1 (Active Batch / Evacuated & LRU Stores)**: Inspects `batch_cs_dict`, `ChangeSet_Dict._evacuated_store[rel_file]`, or `ChangeSet_Dict._lru_cache[rel_file]`. For evacuated ChangeSets, reads from `foreign_cs.resolved_symbols` in $O(1)$ memory without deserialization or DB queries.
     - **Step 1b (In-Flight Check)**: `rel_file` is in flight when it is in `gp._changed_paths_set` or `ChangeSet_Dict` and in neither `gp.completed_paths` nor `gp.file_symbols`. An in-flight file is never looked up in the DB (the DB still holds its previous version), and for AST queries with `force_stubs=False` `resolve_ref()` sets `self.blocked_on = rel_file` and returns `None`.
     - **Step 1c (Database Lookup)**: For files not in flight, `gp.get_file_symbols_from_db(rel_file)` returns the file's symbols, cached into `gp.file_symbols` / `gp.file_names`.
     - **Step 2 (Global Fallback & Negative Caching)**: Checks `gp._ast_staged_symbols`, then `gp._ast_negative_cache` before global `G.TE.get(m_ast.table_id, ...)` lookups by `(name, type)`, notbind type and name. Misses are recorded in `gp._ast_negative_cache`.
     - **Step 3 (Stubs)**: With `force_stubs=True`, or for a file that is not in flight, stages a canonical `notbind` stub symbol (discarding negative cache entries). Parsing workers (`gp._is_worker`) never stage stubs: their TableEngine is never committed, so the reference is left for the parent.
  3. `parsed_route[0] == REF_POS`: Fetches directly from `self.cs[parsed_route[1]]` column `query[1]`.
  4. `parsed_route[0] == REF_MULTI`: Fetches list of column values across `store_dict[REF_MULTI][parsed_route[1]]`.
  5. Default: Looks up `pos = store_dict[parsed_route][query[0]]` and extracts column value `query[1]`.
- **Worker Optimization & View Preprocessing**:
  - `preprocess_ref_views() -> None`: Pre-unpacks intra-file `OP_REF_VIEW` operations into concrete `OP_VIEW_SET` on worker cores before IPC serialization, distributing schema pattern matching across CPU cores.
  - `prune_unchanged_dependencies() -> None`: Queries `gp.get_file_symbols_from_db()` in workers during Phase 1 to pre-resolve references targeting unchanged files in a single unified pass over `self.cs` and prunes satisfied entries from `foreign_deps`.
  - `pre_resolve_operations(resolved_symbols_map, resolved_names_map=None) -> list`: Worker-side method invoked during Stage 2 to resolve foreign `REF_FILE` references and unpack dynamic views in a fused single iteration over `self.cs`, returning a pre-resolved operation list to the main process for sequential lock-free staging. `resolved_symbols_map` is the file-scoped `SharedSymbolChain` / `SharedSymbolReader` (queried per target file), or a plain `{(name, type): ast_id}` snapshot.
- **Dynamic AST Views (`_unpack_ref_view(operation) -> OperationType | None`)**:
  - Evaluates AST rule schemas against records in `store_dict`, matches conditional rules (`schema_ifs`), dynamically constructs joined table graph (`schema_thens`), and converts `OP_REF_VIEW` into concrete `(joins_tuple, OP_VIEW_SET, data_tuple)`.
  - Queries candidate rows via `CS.get_available_data(route, target_table_id)`, which filters candidate operations strictly by `tableid` (handling both integer IDs and joined view tuples) to isolate `m_ast` records from co-located tag and bridge operations.
- **Pipeline Execution (`execute(force_stubs=False) -> bool`)**:
  - Iterates over `CS.cs` starting at `len(CS.cs_result)`.
  - Unpacks dynamic views (`OP_REF_VIEW` &rarr; `OP_VIEW_SET`).
  - Converts all reference tuples in data to `SafeDataType` via `_resolve_ref_from_tuple()`.
  - Dispatches operations downstream directly to `G.TE.set()`, `G.TE.update()`, or `G.TE.view_set()` (avoiding redundant preliminary `view_get` queries since `te_view_set` deduplicates automatically).
  - Appends resulting rows to `CS.cs_result` and marks `cs_processed = True`.
- **IPC Sanitization (`clear_bloat() -> None`)**:
  - Drops unpicklable object handles (`self.gp = None`, `self.mf = None`, `self.file = None`, `self.debug = []`, `self.parsers = {}`, `self.prior_tags = None`, `self.prior_tags_map = None`, `self.active_tag_list = None`, `self.pending_symbol_refs = []`, `self.last_tag_ref = None`, `self._bridge_maps = set()`, `self.batch_cs_dict = None`, `self.blocked_on = None`) before worker IPC serialization.

### 5.4. Subsystem Verification & Regression Suite (`unit_test/test_table_handling.py`)
Comprehensive unit testing and behavioral verification of `TableHandling.py` is implemented in `unit_test/test_table_handling.py` using the reusable harness in `unit_test/harness.py`:
- **Pure Unit Isolation**: Backed by a mock `RecordingTableEngine` stand-in, eliminating database socket and filesystem dependencies.
- **Prefix Isolation**: Uses `TestTableCollection` with `t_*` schemas to ensure test execution never touches production database tables.
- **Granular Test Coverage**: Divided across 5 focused test classes:
  1. `Test_DataSanitization`: Primitives, Enum/IntEnum unboxing, boolean conversion, `is_data_unsafe` detection, `normalize_data_tuple`.
  2. `Test_TableSchemaAndOperations`: Dynamic column pointers, auto-increment detection, `set` (and `no_duplicate` delegation), `update` backfill, `get` unsafe crash guard, `get_set`, relational views (`view`, `view_get`, `view_get_multiple`, `ref_view`).
  3. `Test_ChangeSetRoutingAndStaging`: Git diff parsing, context manager routing (`with cs(...)`), route stack unwinding on exceptions, route canonicalization (`route_parse`), bucketed storing (`REF_MULTI`), `ref` generation, and `last_not_none`.
  4. `Test_ChangeSetReferenceResolution`: `REF_NO_REF`, direct numerical `REF_POS`, array `REF_MULTI`, context route resolution, foreign `REF_FILE` fast-paths, in-flight dependency blocking (`blocked_on`), and circular dependency breaking (`force_stubs`).
  5. `Test_ChangeSetExecutionAndLifecycle`: Multi-pass execution resolution loops, operation dispatch, unresolved dependency detection, `preprocess_ref_views`, `get_file_type` classification, `clear_bloat` IPC pickling, and bridge map deduplication.

---

## 6. End-to-End Inter-Module Execution Lifecycle

```
==================================================================================================
[1. INITIALIZATION, GIT NETWORK DISCOVERY & CONTENT DEDUPLICATION]
--------------------------------------------------------------------------------------------------
MasterFile (MF)                   GreatProcessor (gp)               TableEngine (G.TE) / DB
  │                                       │                                 │
  ├─ resolve_git_network_and_repo() ─────►│ Ingests m_project, m_git_network (root_commit_hash),
  │                                       │ and m_repository for target git tree
  ├─ ingest_git_dag(version_or_ref) ─────►│ Registers commit DAG, parents (m_commit_parent),
  │                                       │ contributors, and m_git_ref (m_v_main); gp.VID = head_cid
  │                                       │ Done version: skipped. Interrupted one: rolled back (§4.1).
  │                                       │ ingest_state.begin(): INGEST_PARSING + id watermarks
  ├─ add_version(vname)                   ├─ init_db_layout(gp)             │
  │  (Clones git tree to /dev/shm)        │  (Populates gp.Table_Array)     │
  ├─ generate_change_list(gp) ───────────►│                                 │
  │  (Populates gp.Change_List)           │                                 │
  ├─ Content Blob Hash Enumeration:       │                                 │
  │  └── git ls-tree -r {version} ───────►│ Populates gp.file_blob_hashes   │
  │                                       │                                 │
  ├─ G.TE.start(gp.Table_Array) ──────────┼────────────────────────────────►
  │  (Step 4.4, timed: preloads te_cached tables & packs 1GB/2MB/THP shared buffer)
  │                                       │                                 │
  └─ Content-Addressable Blob Dedup (Step 4.5, _reuse_parsed_blobs):       │
     └── Looks (blob_hash, ftype) up in the TableEngine m_file_blob cache; one query on m_bridge_tag
         - Reused files: m_file_name / m_bridge_checkpoint / m_commit_file staged via the TableEngine
         - Remaining new/modified files: dispatched to multicore workers (pre-existing blobs among
           them recorded in m_ingest_blob first, §4.1)
==================================================================================================
[2. MULTICORE PARTITIONING & TWO-STAGE PARALLEL WORKER PARSING]
--------------------------------------------------------------------------------------------------
Main Process (trigger_multicore)          Worker Process 1..N
  │                                       │ (Inherits shared huge-page mmap across fork;
  │                                       │  calls G.TE.start_new_db(is_worker=True))
  ├─ Partition gp.worker_change_list (or gp.Change_List):
  │  ├── order_changed_files()
  │  │   └── Kahn's topological sort on header '#include' dependencies
  │  ├── Stage 1: header_files (.h, .hpp) ──►│ ChangeSet(header_diff_line)
  │  │                                       │ ├── CS.blob_hash = gp.file_blob_hashes[path]
  │  │                                       │ ├── CS.parse() -> C/ASM/Kconfig/Rust Parsers
  │  │                                       │ ├── Table.<op>() builders (m_ast.view, m_tag.set)
  │  │                                       │ ├── CS.preprocess_ref_views()
  │  │                                       │ ├── CS.prune_unchanged_dependencies()
  │  │                                       │ ├── CS.store() (Buffers into CS.cs)
  │  │                                       │ └── CS.clear_bloat()
  │  │                                       └─► Per-worker pipe: one pickled result frame per batch, then a done frame
  │  ├── processing_dirs() (Returns new directory ChangeSets, ingested while workers parse)
  │  ├── processing_unchanges() (Propagates unchanged files into m_bridge_checkpoint; returns forgotten /
  │  │   missing files, parsed by workers as an extra Stage 1 batch set before header resolution)
  │  ├── scheduler.resolve_headers() (Resolves & evacuates header ChangeSets; Stage 1 workers terminate & Clang memory is reclaimed)
  │  │
  │  ├── Stage 2: source_files (.c, .S) ───►│ Spawn Stage 2 Workers per epoch with file-scoped shared symbol segments
  │  │                                       │ ├── CS.blob_hash = gp.file_blob_hashes[path]
  │  │                                       │ ├── CS.parse() -> C/ASM/Kconfig/Rust Parsers
  │  │                                       │ ├── CS.preprocess_ref_views()
  │  │                                       │ ├── CS.pre_resolve_operations(shared_symbols) (Worker-side view unpacking & ref resolution)
  │  │                                       │ ├── CS.clear_bloat()
  │  │                                       │ └── Per-worker pipe -> reader thread (holds at most 2 serialized batches)
  │  │                                       └─► DependencyScheduler.ingest_batch() (Sub-millisecond lock-free staging into TableEngine)
  │  └── symlink_files
  │      (Appended to gp.Symlink_List for zero-duplication deferred alias)
  │
  └─ scheduler.finalize() (Drains deferred queue, runs Phase 2 waves, unmaps the shared symbol segments)
[3. TWO-PHASE EXECUTION: STREAMING LEAF RESOLUTION & WAVE-BASED MULTICORE STAGING]
--------------------------------------------------------------------------------------------------
PHASE 1: Streaming Ingestion & Opportunistic Leaf Execution (main.py:trigger_multicore)
  │
  ├─ Worker Pool (G.CPUS) parses files, populating CS.foreign_deps in CS.ref()
  ├─ Streams uncompressed batches into DependencyScheduler.ingest_batch()
  ├─ Single-Compression Streaming Ingestion: Source ChangeSets execute directly on arrival, evacuating internal
  │  AST buffers before storing into gp.ChangeSet_Dict (eliminating double-compression of bloated un-evacuated ASTs).
  ├─ Event-Driven Wakeups: a ChangeSet blocked on a file of this update that is still in flight waits on it
  │  (headers wait on every in-flight file they reference before running); when that file executes, its waiters
  │  run immediately, cascading, so most files resolve while the workers are still parsing.
  │  ├─ Success: extract_tags_and_evacuate_cs(CS) & registers resolved_symbols, file_pre_extracted_tags, file_blob_ids
  │  └─ Blocked on nothing that will complete: deferred (retried at epoch boundaries, stubbed by Phase 2)
  ├─ As soon as parsing workers finish, trigger_multicore() exits immediately
  └─ Phase 1 worker pool terminates cleanly -> Clang C-heap memory purged via reclaim_system_memory()
--------------------------------------------------------------------------------------------------
PHASE 2: Wave-Based Resolution & Zero-Copy Staging (main.py:execute_phase2_parallel_waves)
Main Process (In-Process Wave Resolution)       TableEngine & Database
  │                                               │
  ├─ Extracts remaining unexecuted ChangeSets     │
  ├─ Builds wave DAG:                             │
  │  deps[path] = {target in cs.foreign_deps if target in remaining}
  │                                               │
  ├─ Loop while remaining:                        │
  │  ├─ Wave Selection (ready queue, sorted):      │
  │  │  wave = files whose deps all executed; completions feed the next wave
  │  ├─ [PRECISION CYCLE BREAKING] If wave empty: │
  │  │  ├─ Selects single top candidate with most │
  │  │  │  dependents in remaining DAG            │
  │  │  ├─ Executes with force_stubs=True         │
  │  │  └─ Evacuates buffers, updates symbols,    │
  │  │     unlocking unblocked dependents in wave │
  │  │                                            │
  │  └─ [IN-PROCESS WAVE RESOLUTION]              │
  │     ├─ Iterates over wave directly in-process (zero IPC, zero pickling)
  │     ├─ cs.execute() resolves against in-memory symbols & TableEngine
  │     ├─ Still blocked although every dependency executed (e.g. a symbol the changed
  │     │  header never defines, such as a Clang implicit declaration): re-executes with
  │     │  force_stubs=True; a ChangeSet is never evacuated with unresolved operations
  │     ├─ extract_tags_and_evacuate_cs(cs) evacuates AST buffers
  │     ├─ Preserves negative query cache across micro-commits
  │     └─ G.TE.commit_all() every chunk_commit_interval (e.g. 1000 files)
  ▼
Phase 2 Wave Resolution completes at 350-700 files/s
--------------------------------------------------------------------------------------------------
Main Process (STEP 6.1+: Post-Processing Subsystems) TableEngine (G.TE)          Database (G.DB)
  │                                               │                                 │
  ├─ STEP 6.1: processing_symlinks()              │                                 │
  │  ├─ norm_target = normpath(target)            │                                 │
  │  ├─ target_fid = get_fid_for_path(norm_target)│                                 │
  │  ├─ m_bridge_file.set(vid, sym_fnid, fid) ──►│ (Direct zero-duplication alias) │
  │  ├─ Broken symlink whose blob is already      │                                 │
  │  │  parsed (tagged) -> relinked to that blob  │                                 │
  │  │  (_reuse_parsed_symlink_blobs, no re-parse)│                                 │
  │  └─ Other broken symlinks -> file_processing  │                                 │
  │                                               │                                 │
  ├─ Post-Processing Subsystems:                  │                                 │
  │  ├─ processing_git_commits(version) ─────────►│ (Zero-decompression tag mapping │
  │  │                                            │  via file_pre_extracted_tags)   │
  │  ├─ processing_maintainer_files(version) ────►│                                 │
  │  ├─ processing_kbuild(version) ──────────────►│                                 │
  │  └─ processing_arch_layouts(version) (Step 6.8: m_arch_root rows of every ARCH, raw SQL) ──►│
  │                                               │                                 │
  ├─ Final Teardown Commit:                       │                                 │
  │  └─ G.TE.commit_all(update_in_mem_indexes=False) ──────────────────────────────►│
==================================================================================================
```

---

## 7. Configuration & SSH Port Forwarding Subsystem (`core/config.py` & `core/ssh_tunnel.py`)

### 7.1. Unified Configuration Hierarchy
- Configuration settings follow strict precedence: `CLI Flags > Environment Variables > config.json > Built-in Defaults`.
- When configuring or synchronizing database parameters, `DB_*` and `MYSQL_*` environment variables (including `DB_SOCKET`, `MYSQL_UNIX_PORT`, `MYSQL_SOCKET`) remain synchronized via `sync_environ()`, tracking internally exported keys in `_EXPORTED_ENV_VARS` via `_get_user_env()` to prevent environment self-pollution during configuration reloads.
- Sections:
  - `"database"`: MariaDB/MySQL connection settings (`host`, `port`, `user`, `password`, `database`, `timeout`, `engine`, `unix_socket`), `server_tuning` (`innodb_buffer_pool_size`, `innodb_log_file_size`, `innodb_flush_log_at_trx_commit`, `innodb_io_capacity`), and embedded feature `ssh_tunnel` (`enabled`, `host`, `port`, `user`, `password`, `key_file`, `remote_host`, `remote_port`, `local_port`, `strict_host_key_checking`, `connect_timeout`).
    - **Unix Domain Socket Support**: When `ssh_tunnel` is inactive, MariaDB connections on local hosts (`localhost`, `127.0.0.1`) can connect via low-latency Unix domain sockets (`/run/mysqld/mysqld.sock`, `/var/run/mysqld/mysqld.sock`, `/tmp/mysql.sock`), bypassing the TCP loopback network stack. Controlled by `"unix_socket"` (`"auto"`, explicit path, or `"disabled"`/`"none"` to force TCP), environment variables (`DB_SOCKET` / `MYSQL_UNIX_PORT`), or CLI flag `--db-socket`.
  - `"webapp"`: FastAPI server settings (`host`, `port`, `reload`).
  - `"parser"`: TableEngine, memory mode, fidelity auditing, memory limits (`table_engine`, `memory_mode`, `fidelity`, `mem_max`, `hugepages`) and `symbol_layout` (`"tag"`, default: symbol rows stored once per tag; `"blob"`: per blob, sets `G.SYMBOL_LAYOUT`).
  - `"mailing_list"`: Mailing list ingestion feature configuration, including dedicated `ssh_tunnel` settings.
  - `"ssh_tunnel"`: Backward compatibility alias mirroring `database.ssh_tunnel` for legacy configurations.
  - `"workspace"`: `repos_dir` (default `"repos"`, relative to the project root) holding the Git repositories to parse, and `auto_detect_repos` (default `true`).
  - `"repositories"`: List of tracked repositories. Each entry carries `name`, `path` (relative to the project root; symlinked repositories keep their `repos/<name>` path), `project`, `default_branch`, `git_url`, `root_commit`, `start_ref` (default: `root_commit`), `enabled`, and optionally `"default": true`.
- Functions: `init_config(path)`, `get_config()`, `get_db_config()`, `get_webapp_config()`, `get_parser_config()`, `get_mailing_list_config()`, `get_ssh_tunnel_config(feature="database")`, `get_workspace_config()`, `sync_environ(cfg)`, `resolve_unix_socket(socket_path, host)`.

### 7.1.1. Repository Auto-Detection & Selection
- `find_repository_dirs()` lists Git repositories (work trees, gitfiles, or bare repositories) directly under `workspace.repos_dir`; `detect_repository(path)` describes one (`git symbolic-ref` for the branch, falling back to `origin/HEAD`, then `master`/`main`; `remote.origin.url`; `rev-list --max-parents=0 --first-parent <branch>` for `root_commit`). Results are cached per path for the process.
- `sync_repositories(write=True)` adds newly found repositories and fills keys missing from known entries without overwriting user-set values, then atomically rewrites the loaded config file (`config.json`), preserving all other keys. `main.py` runs it on every start when `auto_detect_repos` is true; `python3 main.py --detect-repos` runs it, prints the repositories, and exits.
- `get_repositories_config()` returns enabled repositories, merging undetected-but-present ones in memory (no write). `get_repository(name_or_path=None)` looks a repository up by name or path, or returns the default (`"default": true`, else the first). `get_default_repo_path()` / `resolve_repo_path(entry)` return absolute paths; the webapp's `LINUX_REPO_DIR` is `get_default_repo_path()`.
- **Commit DAG**: `ingest_git_dag()` ingests the complete history of the first version (no implicit 1,000-commit cap) and every commit of later ranges, merges included (see `parser/git_ast/GIT_API.md` §6); `--limit N` bounds the commits recorded per range.
- **Default parse target**: Without `--ref`, `main()` starts at the selected repository's `start_ref` (its first commit by default) and continues through every subsequent release tag (`get_version_tags`). `--repo` accepts a configured repository name or a filesystem path.

### 7.2. SSH Tunnel Manager (`SSHTunnelManager`)
- **Purpose**: Encrypts and forwards remote connections across hosts (e.g. MariaDB database or mailing list services), allowing local orchestration workers to reach remote instances securely without exposing ports over public networks. Disabled by default (`enabled: false`).
- **Per-Feature Architecture**:
  - Tunnels are configured per feature (embedded within each feature's configuration section such as `database.ssh_tunnel` and `mailing_list.ssh_tunnel`).
  - Multiple feature tunnels can run concurrently without collision, each binding to its own configured or dynamically assigned local ephemeral port.
  - Backward compatibility: Legacy flat `ssh_tunnel` definitions in configuration files automatically migrate into `database.ssh_tunnel`.
- **Subprocess Execution**: Uses system OpenSSH binary (`/usr/bin/ssh`) via `subprocess.Popen` with `-N -L <local_port>:<remote_host>:<remote_port>`, `-o ExitOnForwardFailure=yes`, `-o ServerAliveInterval=15`, `-o ServerAliveCountMax=3`.
- **Dynamic Port Binding**: If `local_port` is 0 or already occupied, dynamically allocates an ephemeral localhost port to avoid collisions with any local services.
- **Authentication**:
  - Private key: Configurable path via `key_file` (`-i <key> -o IdentitiesOnly=yes`).
  - Password: Supports non-interactive authentication via `sshpass` (if installed) or dynamically provisioned secure `SSH_ASKPASS` helper scripts (with `SSH_ASKPASS_REQUIRE=force`).
  - User: Custom login username via `-l <user>` / `user@host`.
  - Host Key Verification: Defaults to `accept-new`, configurable to `no` or `yes`.
- **Lifecycle Integration**:
  - Invoked during startup via `start_ssh_tunnel(cfg=None, feature="database")`.
  - Probes local port socket connectivity until listening (up to `connect_timeout` seconds) before declaring readiness.
  - Automatically updates active configuration: for `"database"`, sets `db_cfg["host"] = "127.0.0.1"`, `db_cfg["port"] = local_port` and calls `sync_environ()`, transparently routing `G.DB` and all multiprocessing workers through the tunnel. For other features (e.g. `"mailing_list"`), updates the feature's active host and port settings.
  - Individual teardown via `stop_ssh_tunnel(feature)` or comprehensive teardown via `stop_ssh_tunnel()` (registered with `atexit` and `G.emergency_shutdown()`).

### 7.3. Cross-Chunk Deduplication & Multiprocessing Fork Safety
- **Cross-Chunk Deduplication**: Intermediate chunk commits (`G.TE.commit_all()`) flush and clear staged queues (`queued_set`). To prevent subsequent ChangeSets or waves from attempting duplicate `INSERT INTO` queries for already-committed rows:
  - TableEngine tracks committed records across chunks using `_committed_nodup_keys` (mapping unique column keys to assigned sequence IDs for `no_duplicate=True` tables such as `m_file_name`), `_committed_pks` (tracking 32-byte cryptographic hashes for `m_tag_code` and `m_ast_hash`), and `_committed_unique_keys` (tracking composite/single unique key values for tables with `unique_keys` such as `m_file_reference`).
  - In `TEDirectDB.set()`, when auto-increment generation is requested (`columns[0] is None`), if `table.unique_keys` is configured and matches an already staged row or a committed unique key in `_committed_unique_keys`, `set()` returns the existing record immediately without generating a duplicate sequence ID or queuing a duplicate row.
  - During `commit()` and `commit_all()`, staged rows matching previously committed keys/hashes are filtered out before dispatching batch inserts.
  - In `trigger_multicore()`, all file paths (`gp.Change_List`, plus the forgotten / missing files returned by `processing_unchanges()`) are registered into `m_file_name` and committed (`_register_file_names()`) before the workers that parse them are forked, ensuring stable sequence ID assignment.
- **Content-Addressable Blob Deduplication (Step 4.5)**:
  - Before worker dispatch (and after `G.TE.start()`), `_reuse_parsed_blobs()` looks each changed file's `(blob_hash, ftype)` up in the TableEngine's `m_file_blob` cache and confirms which matches already have tags with one targeted `m_bridge_tag` query (no full-table loads). Files whose exact content was already parsed in any prior version or tree are registered into `m_file_name`, `m_bridge_checkpoint` and `m_commit_file` through the TableEngine, skipping redundant worker translation units; their paths go to `gp.deduped_paths` so Step 6.5 still links their tags to this range's commits.
- **Referential Integrity & Staged Pipeline Commit Boundaries**:
  - Multicore Stage 2 parsing (`scheduler.finalize()`) and git commit parsing (`processing_git_commits`) enforce full TableEngine commits prior to initiating downstream child table staging (`m_file_reference`, `m_kconfig_kbuild`, `m_maintainer_file`). Both commits at the end of Stage 2 (inside `scheduler.finalize()` and the one after it) keep the in-memory caches (`G.TE.commit_all()`): Step 6.1 stages `m_file_name` / `m_file_blob` rows, and a cleared cache would mint new ids for paths and blobs already in the database (duplicate `m_file_name` rows, `m_file_blob` unique-key violations on `(blob_hash, ftype)`). The commit after git commit parsing clears them (`update_in_mem_indexes=False`), from where no step stages into those parent tables any more. Independently of the order, a `no_duplicate` key the purged caches miss is resolved against the database before a new id is assigned (`_committed_nodup_id()`, `table_engine/TE_API.md` §3.4).
  - Step 6.6 (`processing_file_references`) commits all pending parent table rows (`m_file_blob`, `m_file_name`, `m_bridge_checkpoint`), reads this version's blob ids from its checkpoint (valid source blobs), and only clears and recomputes the references of blobs that are new since `gp.Old_VID` (Makefile references, documentation scans and the include `INSERT IGNORE ... SELECT`): rows are keyed by source blob, so carried-over blobs keep theirs. Kbuild symbol mappings (`m_kconfig_kbuild`, idempotent) are still staged for every Makefile. Step 6.7 (`processing_maintainer_files`) skips files already mapped for the same MAINTAINERS blob. Step 6.8 (`processing_arch_layouts`) evaluates the version's Makefiles for every ARCH value (`parser/arch_layout.py` over the Step 4 tree listing and the RAMDISK files) and replaces the head commit's `m_arch_root` rows (about 90 rows at v2.6.12, 1,600 at v6.1).
  - Milestone checkpointing correctly targets active version `gp.VID` (falling back to HEAD commit id) rather than oldest ancestor commits.
- **Milestone Release Checkpointing**:
  - Snapshot registration into `m_bridge_checkpoint` uses a one-shot count check (`COUNT(*) FROM m_bridge_checkpoint WHERE commit_id = %s`). If already complete, snapshot staging is bypassed in $0\text{ms}$; if partial or fresh, existing file IDs are filtered in memory and missing entries are staged directly without row-by-row `G.TE.get()` database roundtrips.
- **Multiprocessing Socket Refresh**:
  - The parent process must never call `start_new_db(..., is_worker=True)` after forking workers, as this resets in-memory caches and destroys preloaded table state.
  - The parent process refreshes its dedicated database socket (`if G.TE.db: G.TE.db.close(); G.TE.db = G.DB()`) to avoid socket descriptor sharing across `fork()` without altering cached state or staging queues.

---

## 8. Mailing List Commit Resolver Subsystem (`parser/mailing_list/`)

### 8.1. High-Level Architecture
The Mailing List subsystem maps Git revisions to corresponding patch submissions, series cover letters, and pull requests on the Linux Kernel Mailing List (LKML) via public-inbox v2 / extindex archives (`/srv/lore`).

```
==================================================================================================
MAILING LIST COMMIT RESOLUTION PIPELINE
--------------------------------------------------------------------------------------------------
Kernel Git Revisions (m_commit)
  │
  ▼ [main.py: processing_git_commits(version) (Step 6.5) / standalone_resolve_mailing_list()]
Feature Extraction:
  ├── Message trailers (Link: https://lore.kernel.org/r/<mid>, Message-Id: <mid>)
  ├── Cryptographic patch-id (git show <sha> | git patch-id --stable)
  ├── Pre/post-image diff blob hashes (git diff-tree -r --no-commit-id <sha>)
  └── Merge metadata (PULL_FROM_RE, BRANCH_MERGE_RE)
  │
  ▼ [parser/mailing_list/ml_client.py: MailingListClient]
Execution Routing:
  ├── Co-located: Direct in-process PublicInboxLocalStore & KernelCommitEmailResolver
  └── Remote: HTTP batch resolution over SSH tunnel to ml_server.py daemon (port 8192)
  │
  ▼ [parser/mailing_list/ml_resolver.py: KernelCommitEmailResolver (5-Tier Waterfall)]
Tier 1: Trailer Message-ID (Link: or Message-Id:)
Tier 2: Stable Patch-ID match (XPATCHID)
Tier 3: Post/Pre-Image blob hash match (XDFPOST / XDFPRE)
Tier 4: Reverse commit SHA lookup in maintainer bot replies (XNQ)
Tier 5: Constrained author, timestamp, and normalized subject window fallback
Tier 6: Pull request ([GIT PULL]) thread match for merge commits
  │
  ▼ [TableEngine Staging]
Relational Database Mapping:
  └── m_commit_email (Table 48):
        (commit_id, message_id, thread_id, relation_type, match_tier, patch_version, patch_idx, patch_total)
  │
  ▼ [Pull Request Propagation]
Merge commit PR links propagated across all child commits (m_parents[0]..m_hash)
==================================================================================================
```

### 8.2. Relational Schema Mapping (Table 48: `m_commit_email`)
| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **48** | `m_commit_email` | `(commit_id, message_id, thread_id, relation_type, match_tier, patch_version, patch_idx, patch_total)` | `("commit_id", "message_id", "relation_type")` | `False` | Bridge linking Git commits to public-inbox mailing list articles. Foreign key to `m_commit(commit_id)`. |

- **Relation Types (`MLRelationType`)**:
  - `1`: `Patch` — The specific patch email introducing this commit.
  - `2`: `CoverLetter` — Series cover letter (`[PATCH 0/N]`).
  - `3`: `SiblingPatch` — Other patches in the same patch series thread.
  - `4`: `PullRequest` — Subsystem maintainer pull request thread (`[GIT PULL]`).
- **Match Tiers (`MLMatchTier`)**:
  - `1`: `Trailer` — Authoritative `Link:` or `Message-Id:` commit trailer.
  - `2`: `PatchID` — Cryptographic `git patch-id --stable` hash match.
  - `3`: `BlobHash` — Post-image or pre-image blob SHA-1 match.
  - `4`: `BotReply` — Maintainer/bot reply referencing applied commit SHA.
  - `5`: `MetadataFallback` — Constrained author, timestamp, and subject fallback.
  - `6`: `PullRequest` — Matched pull request announcement for merge commits and children.

### 8.3. Public-Inbox Storage & Micro-Daemon (`ml_store.py` & `ml_server.py`)
- **Direct Storage Access**: Directly reads public-inbox v2 / extindex files without Perl dependencies:
  - `over.sqlite3` (`?mode=ro`): Thread hierarchy (`tid`), timestamps (`ts`, `ds`), and zlib-compressed overview metadata (`ddd`). Ghost parent rows (`num <= 0`) are filtered.
  - `msgmap.sqlite3` (`?mode=ro`): High-speed mapping of `mid` to article `num`.
  - Sharded Xapian B-trees: Unified database querying with automatic `reopen()` recovery on concurrent writer updates (`DatabaseModifiedError`).
  - `all.git`: Persistent `git cat-file --batch` coprocess for RFC822 raw header verification.
- **Micro-Daemon (`ml_server.py`)**:
  - Standalone multi-threaded HTTP query service (`ThreadingHTTPServer`) listening on localhost port 8192 on the remote storage host (`DAEMON_VERSION = "1.4.1"`).
  - Endpoints:
    - `GET /health`: Storage status, directory paths, daemon version, and Xapian availability.
    - `POST /resolve_commit`: Single commit resolution.
    - `POST /resolve_batch`: Chunked batch commit resolution returning structured `ResolvedCommitContext` dictionaries.
    - `GET /message` & `POST /message`: Fetch full email metadata, headers, body, raw RFC-822, and parsed diff hunks by `mid` or article sequential `num`.
    - `GET /thread` & `POST /thread`: Fetch chronological thread messages by `tid` or starting `mid`, with participant and cover letter metadata.
    - `GET /search` & `POST /search`: Full-text/Xapian search across subjects, authors, and patch metadata with pagination and filter types (`all`, `patch`, `cover`, `pull`).
  - Thread safety: Every request runs in an isolated thread with top-level exception guards ensuring JSON HTTP responses and clean socket closure.
- **Patch Parser (`ml_patch_parser.py`)**:
  - Parses raw unified diffs within commit email bodies into structured AST dictionaries (`files`, `hunks`, `additions`, `deletions`, file modification types `M`, `A`, `D`, `R`, and new line offsets).
  - Integrates directly with `ml_store.py` and `ml_server.py` to deliver pre-parsed, syntax-highlighted diff hunks directly to frontend viewers.
- **Client & Tunnel Management (`ml_client.py`)**:
  - Automatically activates the dedicated `"mailing_list"` SSH tunnel if remote access is required.
  - Verifies local archive presence when tunnel is disabled; returns descriptive errors if neither local archive nor SSH tunnel is available.
  - Negotiates daemon version (`DAEMON_VERSION = "1.4.1"`) against `/health`; if inactive or outdated, synchronizes `parser/mailing_list` package to remote host, frees the target port via `fuser -k`, and detaches the remote daemon (`ssh -f -n` + `nohup`).
  - Automatically terminates SSH tunnel on client close.
  - Materializes pull request linkage across both the merge commit and all child commits in the merged branch lineage for $O(1)$ lookup performance.
- **Store & Concurrency Architecture (`ml_store.py`)**:
  - Automatically resolves extindex archives under `all/` (e.g. `/srv/lore/all/ei15`).
  - Resilient Concurrent Read-Only Architecture: Opens `over.sqlite3` and `msgmap.sqlite3` in read-only URI mode with `immutable=1` (`?mode=ro&immutable=1`), bypassing POSIX file locking (`F_SETLK`) and hot journal recovery to completely avoid write lock contention with background `public-inbox-extindex` processes.
  - Thread-local handles (`threading.local`) and `isolation_level=None` (autocommit mode) ensure zero cursor contention across concurrent worker threads.
  - Exponential backoff retry helper (`_execute_query`) with `PRAGMA busy_timeout = 60000;` and `PRAGMA query_only = ON;`.
  - Serialized `git cat-file --batch` pipeline using `threading.Lock()` to prevent stream interleaving across threads.
  - Chronological search querying indexed via `idx_ds` (`ORDER BY ds DESC`) for instant sub-millisecond retrieval without table-scan locks.
  - Utilizes indexed `xref3.oidbin` lookups for Git blob SHA-to-document ID resolution, avoiding full table scans over multi-million row PublicInbox archives, with direct `xref3` fallback when `rec.git_blob` is missing in overview records.
  - Multi-candidate Message-ID lookups supporting raw, unquoted, angle-bracketed, and space-to-plus (`+` query string space interpretation) variants.
  - Provides thread and cover letter reconstruction via `over.sqlite3` indexed by `tid` and `ds`.



