# Next-Gen C AST Subsystem API & Architectural Contract Specification (`c_ast`)

Authoritative, dense architectural specification and technical contract across `parser/c_ast/` (`ctypes_bindings.py`, `tokenizer.py`, `ast_nodes.py`, `cursor_tree.py`, `tag_tracker.py`, `cs_extractor.py`, `c_ast.py`, `__init__.py`). Designed as an exhaustive, non-verbose reference for AI agents extending, maintaining, or interfacing with the next-generation C & Assembly AST parsing engine.

---

## 1. High-Level Architecture & 2-Stage Parsing Pipeline

`c_ast` transforms C source files (`.c`, `.h`) and Assembly source files (`.S`, `.s`, `.h`) into relational database operations staged in a `ChangeSet` (`CS`).

```
==================================================================================================
STAGE 1: FAST CTYPES TOKENIZATION & SEMANTIC AST PARTITIONING
--------------------------------------------------------------------------------------------------
C / ASM Source Code (.c / .h / .S)
  │
  ▼ [Ast_Manager (c_ast.py)]
Clang TranslationUnit (cc.Index -> cc.TranslationUnit)
  │ (with -D__KERNEL__, kernel include paths, PARSE_DETAILED_PROCESSING_RECORD + 32768)
  │ (optional comment_remover() pre-filter if G.OVERRIDE_CPPRO_CINDEX_INPUT is active)
  ▼ [TokenStream / TokenList (tokenizer.py)]
Low-Level Foreign Ctypes Tokenization & Bulk Annotation:
  ├── clang_tokenize() -> tokens_memory (Contiguous C array of cc.Token)
  ├── clang_annotateTokens() -> temp_cursors_array (Contiguous C array of cc.Cursor)
  ├── Class-level cc.Cursor._tu configuration eliminates per-instance dynamic __dict__ overhead
  ├── Slot-optimized ParsedToken carrier (__slots__) eliminates cc.Token __dict__ overhead
  ├── Fast ctypes Line/Col extraction via _CLANG_GET_SPELLING_LOC into reusable buffers
  ├── Direct slice token.spelling_str from Latin-1 rawfile[line - 1] (bypasses Python string alloc)
  └── Memory pinned via cc.TokenGroup(parsed_tu, tokens_memory, tokens_count)
  │
  ▼ [SemanticPartitioner / Zone (cursor_tree.py)]
filter_enclosing_cursors(cursors) -> get_top_level_cursors(parsed_tu, fullfilename)
Zone(Zone_Type.Full_File, top_cursors, tokens_array).check_exec(token, cursor, ast_kind)
  ├── Spatial scope boundaries: Zone (Function_Args, Declared_Args, Compound_Stmt, etc.)
  ├── Delimiter encapsulation: absorbs ';' and '}' directly into active AST node extents
  ├── Type & declarator parsing: C_Type (TypeSegment, TypeToken, CQual)
  │     ├── Multi-declarator splitting: root_type + declarators -> final_types
  │     └── Strict container isolation: return-type structs never claim Function_Args
  ├── Statement & expression nodes: Ast_Statement, Ast_CompoundStmt, Ast_IfStmt, etc.
  ├── Sub-construct containers: AST_Array, AST_Initializer, AST_Enum_Equal
  ├── Preprocessor directives: CPPro dynamic class morphing via ccpro_start_flip
  └── Assembly directives/instructions: Ast_ASM_Directive, Ast_ASM_Macro, Ast_ASM_Instruction
  │
  ▼ Post-Processing Passes:
  ├── Zone.gen_lined_dict() (no-op; dead A_Line_Dict bypassed to eliminate memory allocation)
  └── Zone.resolve_cppro_scopes() (LIFO stack links #if/#elif/#else to terminating #endif)
==================================================================================================
STAGE 2: RELATIONAL CHANGESET EXTRACTION & 5-TIER TAG EVOLUTION
--------------------------------------------------------------------------------------------------
CSExtractor.extract_zone(CS, main_zone) -> main_zone.extract(CS) (cs_extractor.py, cursor_tree.py)
  │
  ├── Tag Lifecycle Initialization: get_prior_tags(CS) (tag_tracker.py)
  │     └── Resolves prior tags & symbol items via worker batch pre-fetch cache (`CS._batch_prior_tags`, `CS._batch_prior_items`) or queries m_bridge_tag + m_tag -> Inverted hash, name, pos, anchor, & symbol indices
  │
  ├── AST Definition & Child Container Staging:
  │     ├── C_Type:
  │     │     ├── Definitions: m_ast.ref_view -> dynamic child container links from child zones
  │     │     └── Variables: m_ast.view -> C_Compound joined view with qualifiers & types
  │     ├── CPPro_include: m_file_name.get_set -> m_ast.view joined with m_ast_include
  │     ├── Ast_Statement: _extract_nested(CS) -> m_ast.view(((m_ast.ast_id,),), None, name, type_id)
  │     └── Ast.extract_1arg: m_ast.view(((m_ast.ast_id,),), None, name[:255], type_id)
  │
  ├── Spatial Tagging & 5-Tier Prior Tag Evolution Matching (Ast.tag / CSExtractor.stage_tag):
  │     ├── Tier 1: Exact 32-byte binary SHA-256 hash match -> Recycle existing tag_id
  │     ├── Tier 2: Exact symbol name & construct category match -> Transition evolved tag_id
  │     ├── Tier 3: Bulk prefix/suffix rename refactoring detection -> Transition evolved tag_id
  │     ├── Tier 4: Positional context anchors between enclosing symbols -> Transition tag_id
  │     ├── Tier 5: Spatial line overlap & proximity fallback (<= 2 lines) -> Transition tag_id
  │     │
  │     ├── Rule 12 Staging Order inside with CS(REF_POS):
  │     │     ├── 1. m_tag.set(None, code_hash, ast_ref, 0, 0)          [MUST BE FIRST, 5 columns: tag_id, hash, ast_id, hl_s, hl_l]
  │     │     │      tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
  │     │     ├── 2. m_tag_code.get_set(code_hash, code_str)             [Auxiliary hash table]
  │     │     └── 3. m_moved_tag.set(s_tag_id, tag_ref)                  [If evolved tag match]
  │     │
  │     ├── File-to-Tag Bridge: m_bridge_tag.set(blob_id, tag_ref, line_s, line_e, char_s, char_e)
  │     ├── Spatial AST Map: m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref)
  │     │     └── (rel_line_e and rel_char_e computed against self.endif for preprocessor branches)
  │     ├── Bridge Map Link: m_bridge_map.set(tag_ref, tag_ref) (deduped via register_bridge_map)
  │     └── Debug serialization: m_ast_debug.set (when G.OVERRIDE_FORCE_AST_DEBUG is True)
  │
  └── Tag Lifecycle Finalization: close_prior_tags(CS) (tag_tracker.py)
        └── Immutable zero-VID DAG: tags remain permanent content records; tag retirement/closure is a no-op
==================================================================================================
```

---

## 2. File Lifecycle Dispatch & ChangeSet Lifecycle (`c_ast.py`)

### 2.1 `c_ast_parse(CS: ChangeSet) -> None`
Dispatches parsing workflow inside `with CS(REF_C_AST):` according to Git diff status `CS.file_operation`:
- **`"R100"`** (Exact rename): Immediate return. File lifecycle and AST tags are preserved automatically.
- **`"A"`** (Added file): Calls `process_c_ast(CS)`.
- **`"M"` / `"R*"`** (Modified / Partial Rename): Executes `get_prior_tags(CS)` &rarr; `process_c_ast(CS)` &rarr; `close_prior_tags(CS)`.
- **`"D"`** (Deleted file): Executes `get_prior_tags(CS)` &rarr; `close_prior_tags(CS)`.

### 2.2 `comment_remover(text: str) -> str`
Strips C line/block comments from source text while preserving exact line numbering by substituting comment spans with equivalent counts of newline characters (`\n`). Used during pre-filtering when `G.OVERRIDE_CPPRO_CINDEX_INPUT` is enabled.

### 2.3 `process_c_ast(CS: ChangeSet) -> None`
Instantiates `Ast_Manager(CS)` to drive translation unit parsing and extraction.

### 2.4 `Ast_Manager`
Coordinates Clang TranslationUnit instantiation, tokenization, and processing:
- Reads file content with `encoding="latin-1"`.
- Sets `G.CURRENT_PARSING_FILE = self.filename`.
- **Line Splitting Invariant**: Splits raw content via `.replace("\r\n", "\n").split("\n")`. Libclang treats `\r\n` and `\n` as newlines and `\x0c` (Form Feed) as whitespace. Never use `str.splitlines()` to prevent line desynchronization.
- Reuses worker-global `_WORKER_CLANG_INDEX = cc.Index.create()`.
- Translation unit compilation arguments:
  ```python
  args = [
      "-ferror-limit=0",
      "-w",
      *_CLANG_KERNEL_MACRO_DEFINES,
      *cppro_cindex_input,
      f"-I{self.mfdir}/arch/x86/include",
      f"-I{self.mfdir}/arch/x86/include/generated",
      f"-I{self.mfdir}/{inc_dir}",
      f"-I{self.mfdir}/include",
      f"-I{self.mfdir}/include/uapi",
  ]
  options = cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768 + 16384
  ```
  `_CLANG_KERNEL_MACRO_DEFINES` neutralizes kernel-specific attributes and lockdep annotations (`-D__KERNEL__`, `-D__user=`, `-D__init=`, `-D__exit=`, `-D__sched=`, `-D__weak=`, `-D__read_mostly=`, `-D__maybe_unused=`, `-D__always_unused=`, `-D__percpu=`, `-D__rcu=`, `-D__force=`, `-D__bitwise=`, `-D__bitwise__=`, `-D__nocast=`, `-D__iomem=`, `-D__must_check=`, `-D__deprecated=`, `-D__cpuinit=`, `-D__cpuinitdata=`, `-D__meminit=`, `-D__meminitdata=`, `-Dnoinline=`, `-D__always_inline=`, `-Dnotrace=`, `-D__kprobes=`, `-D__ref=`, `-D__pure=`, `-D__const=`, `-D__noreturn=`, `-D__malloc=`, `-Dasmlinkage=`, `-DFASTCALL=`, `-Dfastcall=`, `-D__cold=`, `-D__hot=`, `-D__visible=`, `-D__releases(x)=`, `-D__acquires(x)=`, `-D__acquire(x)=`, `-D__release(x)=`, `-D__cond_lock(x,c)=(c)`, `-D__cacheline_aligned=`, `-D__cacheline_aligned_in_smp=`, `-D____cacheline_aligned=`, `-D____cacheline_aligned_in_smp=`, `-D__aligned(x)=`, `-Daligned(x)=`).
  `-Dfastcall=` is required alongside `-DFASTCALL=`: 2.6.x `include/linux/linkage.h` defines `fastcall` only under `#ifndef FASTCALL`, so without it every `<type> fastcall f(...)` definition is a syntax error and its body statements (e.g. `schedule();`) are staged as file-scope `C_Compound` definitions. `fastcall` is also listed in `_KNOWN_KERNEL_ATTRIBUTES`.
  Options combine `PARSE_DETAILED_PROCESSING_RECORD` (1), `CXTranslationUnit_RetainExcludedConditionalBlocks` (32768), and `CXTranslationUnit_IgnoreNonErrorsFromIncludedFiles` (16384) to eliminate unused diagnostic generation for foreign headers.
- Collects profiler metrics: `prof.clang_parse_tu_s` (libclang's parse only), `prof.include_symbols_s` (`extract_include_symbols()`), `prof.clang_tokenize_s`.
- Registers `CS.parsers["C_AM"] = self`.
- Instantiates `TokenList` (subclass of `TokenStream`) and calls `TL.process_tokens(CS)`.

### 2.5 `TokenList`
Compatibility adapter connecting `TokenStream` with `Zone` execution:
- Receives pre-filtered `top_cursors` from `extract_include_symbols` (or extracts file-scoped cursors via `get_top_level_cursors(self.parsed_tu, self.fullfilename)` if unprovided).
- Initializes `self.main_zone = Zone(Zone_Type.Full_File, top_cursors, tokens_array=self.tokens_array)`.
- Sets `G.CURRENT_RAWFILE = self.rawfile`.
- Loops tokens directly over `self.tokens_array` (bypassing `zip()` tuple allocation via pre-associated `token._cursor`):
  - Invokes `self.main_zone.check_exec(token, token._cursor, token.ast_kind)`.
- Invokes `self.main_zone.resolve_cppro_scopes()`.
- Records `prof.token_processing_s`.
- Dispatches `CSExtractor.extract_zone(self.CS, self.main_zone)`.
- Immediate Teardown: Clears `self.main_zone`, `self.tokens_array`, `self.token_group`, `self.parsed_tu`, and pops `CS.parsers["C_AM"]` to immediately release all AST/token memory.

### 2.6 High-Throughput Worker Optimizations
- **C-Level Visitor Include Symbol & Top Cursor Traversal (`extract_include_symbols`)**: Direct inclusion headers are mapped to direct include lines via `CXFile` pointer integers in an inverted $O(1)$ dictionary (`ptr_to_direct_line`). The AST walk traverses descendants at C speed via `clang_visitChildren` (`_CLANG_VISIT_CHILDREN`), pruning node visitation strictly to symbol-referencing cursor kinds (`_REFERENCING_KIND_IDS`) and raw integer kinds (`_CURSOR_KIND_ID_MAP`), while checking null references with `_CLANG_CURSOR_IS_NULL`. Top-level cursors are filtered strictly via low-level ctypes `clang_getFileLocation` pointer matching against `target_ptr`, completely eliminating Python list allocations, string allocations, `os.path.realpath`, and redundant second passes over the TU's child cursors in `TokenList`. The TU's top-level cursors (thousands per kernel TU, mostly from headers) are walked with one `clang_visitChildren` callback that keeps only the target file's cursors, instead of `Cursor.get_children()`, which builds the full list and spends two extra C calls per child on a null-cursor assertion; each referenced declaration is resolved once per TU (`resolved_refs`, keyed by the raw `CXCursor` bytes). Output is unchanged; the walk takes about half the time, roughly 7% of a source file's parse.
- **Token Ingestion & Cursor Deduplication (`tokenizer.py`)**: `TokenStream.__init__` deduplicates adjacent cursor references via fast integer kind and raw byte matching (`k_id == prev_kind and bytes(raw_c) == prev_bytes`), eliminating 5-attribute struct access overhead. Single-line tokens memoize the active source line string across consecutive tokens on the same line. `Line.grow`, `Line.new_end`, and `Line.new_end_reversed` perform direct type checks (`type(target) is Line`) bypassing MRO and dynamic `hasattr` checks. `Line.is_inside` unpacks coordinate tuples once to eliminate repeated tuple indexing. Escaped newline string checks are scoped strictly to multi-line tokens.
- **C-Level Traversal for Used Types (`collect_cursor_used_types`)**: Employs static module-level ctypes callback `_CB_COLLECT_USED_TYPES` dispatched directly via `_CLANG_VISIT_CHILDREN`, eliminating recursive Python function calls and intermediate child list allocations. Type cleanups are memoized through `_CLEAN_TYPE_CACHE`.
- **Relative Path Resolution Cache (`get_rel_file_for_cursor`)**: Caches resolved repository-relative file paths by `file_path` on `CS._rel_file_cache` and `_GLOBAL_REL_FILE_CACHE`, bypassing multi-branch string slicing, prefix testing, and path normalization across tens of thousands of cursor lookups.
- **Direct FFI AST Reference Resolution (`resolve_cursor_type_ast`)**: Uses low-level `_CLANG_GET_CURSOR_REFERENCED` and `_CLANG_CURSOR_IS_NULL` direct bindings with raw integer `_kind_id` branching, eliminating `CursorKind` enum instantiation on high-frequency reference lookups.
- **Foreign Symbol & File Resolution Cache (`resolve_cursor_foreign_info`)**: Caches resolved `(foreign_file, foreign_name)` tuples across all `TypeToken` branches (`struct`, `union`, `enum`, `typedef`, `functionproto`) in `_tu_foreign_type_cache` keyed by `(tspelling, current_file)` to eliminate redundant Clang AST pointee and declaration resolution. To guarantee strict translation unit hermeticity and prevent cross-file state leakage (e.g. between isolated sandbox parses and full-tree parses), `clear_tu_caches()` clears this cache before and after every `c_ast_parse` cycle, in `Ast_Manager` teardown, and in test harness `tearDown` methods.

---

## 3. Libclang Ctypes FFI Fast-Path (`ctypes_bindings.py`)

Bypasses Python-C API boundary overhead in `clang.cindex` by binding directly to shared library foreign function pointers with pre-allocated ctypes buffers.

### 3.1 Direct Ctypes Function Signatures
```python
_CLANG_GET_EXTENT = cc.conf.lib.clang_getTokenExtent
_CLANG_GET_EXTENT.argtypes = [cc.c_object_p, cc.Token]
_CLANG_GET_EXTENT.restype = cc.SourceRange

_CLANG_GET_CURSOR_EXTENT = cc.conf.lib.clang_getCursorExtent
_CLANG_GET_CURSOR_EXTENT.argtypes = [cc.Cursor]
_CLANG_GET_CURSOR_EXTENT.restype = cc.SourceRange

_CLANG_GET_RANGE_START = cc.conf.lib.clang_getRangeStart
_CLANG_GET_RANGE_START.argtypes = [cc.SourceRange]
_CLANG_GET_RANGE_START.restype = cc.SourceLocation

_CLANG_GET_RANGE_END = cc.conf.lib.clang_getRangeEnd
_CLANG_GET_RANGE_END.argtypes = [cc.SourceRange]
_CLANG_GET_RANGE_END.restype = cc.SourceLocation

_CLANG_GET_SPELLING_LOC = cc.conf.lib.clang_getSpellingLocation
_CLANG_GET_SPELLING_LOC.argtypes = [
    cc.SourceLocation,
    ctypes.c_void_p,
    ctypes.POINTER(cc.c_uint),
    ctypes.POINTER(cc.c_uint),
    ctypes.c_void_p,
]
_CLANG_GET_SPELLING_LOC.restype = None

_CLANG_GET_FILE_LOC = cc.conf.lib.clang_getFileLocation
_CLANG_GET_FILE_LOC.argtypes = [
    cc.SourceLocation,
    ctypes.c_void_p,
    ctypes.POINTER(cc.c_uint),
    ctypes.POINTER(cc.c_uint),
    ctypes.c_void_p,
]
_CLANG_GET_FILE_LOC.restype = None

_CLANG_GET_TOKEN_KIND = cc.conf.lib.clang_getTokenKind
_CLANG_GET_TOKEN_KIND.argtypes = [cc.Token]
_CLANG_GET_TOKEN_KIND.restype = ctypes.c_uint

_CLANG_CURSOR_IS_NULL = cc.conf.lib.clang_Cursor_isNull
_CLANG_CURSOR_IS_NULL.argtypes = [cc.Cursor]
_CLANG_CURSOR_IS_NULL.restype = ctypes.c_int

_CLANG_GET_CURSOR_REFERENCED = cc.conf.lib.clang_getCursorReferenced
_CLANG_GET_CURSOR_REFERENCED.argtypes = [cc.Cursor]
_CLANG_GET_CURSOR_REFERENCED.restype = cc.Cursor

_CLANG_VISIT_CHILDREN = cc.conf.lib.clang_visitChildren
_CLANG_VISIT_CHILDREN.argtypes = [cc.Cursor, cc.cursor_visit_callback, ctypes.py_object]
_CLANG_VISIT_CHILDREN.restype = ctypes.c_uint
```

### 3.2 Direct C-Pointer Ingestion & NULL FileID Bypass
Coordinates are retrieved in **2 C-calls** per token (start & end location) into reusable buffers:
- **`from_param` Elimination**: `_CLANG_GET_EXTENT` binds `argtypes = [cc.c_object_p, cc.Token]`. Passing `parsed_tu._as_parameter_` directly bypasses Python's `TranslationUnit.from_param()` invocation on every token.
- **NULL FileID Bypass**: `clang_getSpellingLocation(loc, NULL, line, col, NULL)` passes `NULL` (`None` in ctypes) for file and offset, instructing Libclang to skip SourceLocation FileID resolution and offset calculations.

```python
_CTYPES_F_PTR = cc.c_object_p()
_CTYPES_S_LINE = cc.c_uint()
_CTYPES_S_COL = cc.c_uint()
_CTYPES_S_OFF = cc.c_uint()
_CTYPES_E_LINE = cc.c_uint()
_CTYPES_E_COL = cc.c_uint()
_CTYPES_E_OFF = cc.c_uint()

_BYREF_F_PTR = ctypes.byref(_CTYPES_F_PTR)
_BYREF_S_LINE = ctypes.byref(_CTYPES_S_LINE)
_BYREF_S_COL = ctypes.byref(_CTYPES_S_COL)
_BYREF_S_OFF = ctypes.byref(_CTYPES_S_OFF)
_BYREF_E_LINE = ctypes.byref(_CTYPES_E_LINE)
_BYREF_E_COL = ctypes.byref(_CTYPES_E_COL)
_BYREF_E_OFF = ctypes.byref(_CTYPES_E_OFF)
_CTYPES_BYREF = ctypes.byref
_CTYPES_REUSABLE_LOC = cc.SourceLocation()
```

### 3.3 Constants & Helper Functions
- **`AST_KIND(IntEnum)`**:
  - `punctuation = 0`
  - `keyword = 1`
  - `identifier = 2`
  - `literal = 3`
  - `comment = 4`
- **`_CLANG_TOKEN_KIND_MAP`**: Tuple mapping index from `token.int_data[0]` (Clang's internal `CXTokenKind` byte) directly to `AST_KIND`, bypassing foreign C function calls.
- **`safe_spelling(token: Any) -> str`**: Returns `token.spelling_str` or `token.spelling`, caching in `token.spelling_str`.
- **`safe_cursor_spelling(cursor: Any) -> str`**: Returns `cursor._spelling_str` or `cursor.spelling`, defensively catching `UnicodeDecodeError` and sanitizing Libclang anonymous/unnamed declaration paths (`(unnamed at ...)` / `(anonymous at ...)`) down to relative git repository paths via `clean_unnamed_spelling`.

---

## 4. Spatial Coordinate Model & Token Streaming (`tokenizer.py`)

### 4.1 `Line` Class Contract
Slot-optimized spatial coordinate carrier (`__slots__ = ("line_pos", "char_pos", "code")`):
- `line_pos: tuple[int, int]`: 1-based start line and end line.
- `char_pos: tuple[int, int]`: 1-based start column and end column.
- `code: str`: Extracted raw source code string.

#### Core Methods:
| Method | Signature | Description |
| :--- | :--- | :--- |
| `__init__` | `(arg0=0, arg1=0, arg2=0, arg3=0)` | Polymorphic constructor with integer fast-path `type(arg0) is int` (bypassing 4 failed `isinstance` checks), accepting `(s_l, e_l, s_c, e_c)`, `Line`, `cc.SourceRange`, or objects with `.line`. |
| `cc` | `(rawfile: tuple[str, ...]) -> Self` | Slices and populates `self.code` spanning single- or multi-line bounds using 1-based coordinates. |
| `new_end` | `(target: Any) -> Self` | Updates ending coordinates `(line_pos[1], char_pos[1])` to match target's end. |
| `new_end_reversed` | `(target: Any) -> Self` | Updates ending coordinates to target's **start** position. |
| `grow` | `(target: Any) -> Self` | Expands the bounding box to enclose target. Features $O(1)$ fast-path short-circuiting when already enclosed and skips tuple allocations when boundaries are unchanged. |
| `is_inside` | `(extent: Line) -> bool` | Returns `True` if `extent` is fully contained within `self` boundaries. |
| `__eq__` | `(other: object) -> bool` | Equality check on `(line_pos, char_pos)`. |

### 4.2 Coordinate Fast-Path: `get_cursor_line(cursor: cc.Cursor) -> Line`
Bypasses Python object allocation when cursor is already annotated; retrieves coordinates via `_CLANG_GET_FILE_LOC` (`clang_getFileLocation`) in 2 C-calls passing `None` for file and offset pointers. Using physical file locations rather than macro expansion spelling locations prevents cross-file coordinate extent drift across macros, while clamping character columns when lines wrap, and caches directly on `cursor._cached_line`.

### 4.3 Fast Delimiter Encapsulation: `encapsulate_trailing_delimiter`
Expands the target `Line` extent to encapsulate trailing punctuation delimiters (`;`, `,`) on the current line or column <= 2 of the immediate next line.
- **$O(\log N)$ Binary Search Optimization**: Uses `bisect.bisect_left(tokens, e_l, key=_get_tok_start_line)` to directly jump to the first token starting at or after the extent end line `e_l`.
- Inspects at most 1 to 3 trailing tokens on line `e_l` or `e_l + 1`, eliminating $O(N)$ linear scans from index 0 across thousands of translation unit tokens.

### 4.4 `TokenStream` Architecture
- **Initialization Sequence**:
  1. Computes source file range `cc.SourceRange(1:1, filesize_offset)`.
  2. Foreign call `clang_tokenize` fills contiguous C array `tokens_memory`.
  3. Pre-allocates `temp_cursors_array = (cc.Cursor * self.count)()` and annotates in a single foreign call `clang_annotateTokens`.
  4. Configures `cc.Cursor._tu = parsed_tu` at the class level to eliminate per-instance dynamic `__dict__` overhead on tens of thousands of cursor objects.
  5. Iterates contiguous tokens using reusable `_CTYPES_REUSABLE_LOC`:
     - Bypasses `clang_getRangeStart` and `clang_getRangeEnd` by copying `ext.ptr_data` and setting `ext.begin_int_data` / `ext.end_int_data` directly onto the reusable `CXSourceLocation`.
     - Extracts coordinates into lightweight `Line` instance `token.line`.
     - Assigns `token.spelling_str`: direct slice from latin-1 `rawfile[line - 1][s_col - 1 : e_col - 1]` for single-line tokens.
     - Maps `token.ast_kind = _CLANG_TOKEN_KIND_MAP[token.int_data[0]]` without foreign call.
     - Wraps token payload in slot-optimized `ParsedToken(line, spelling_str, ast_kind, cursor)` (`__slots__`), completely bypassing `ctypes.Structure.__dict__` allocation.
  6. Exposes `self.cursors_array` via dynamic property for backward compatibility with `parser/asm_ast/asm_ast.py`.
  7. Retains foreign memory pointer: `self.token_group = cc.TokenGroup(parsed_tu, tokens_memory, tokens_count)`. Prevents premature C memory free during parsing.

---

## 5. Intermediate AST Node Hierarchy (`ast_nodes.py`, `cursor_tree.py`)

### 5.1 Base Interfaces

#### `Ast` (Root Intermediate Element)
- **Attributes**: `extent: Line`, `end_mode: int`, `need_processing: bool`, `name: str`, `type_id: int`, `endif: Line | None`.
- **Class-Level Slot Defaults**: `zones: tuple[Any, ...] = ()`, `paren_depth: int = 0`, `brace_depth: int = 0`, `bracket_depth: int = 0`, `need_processing: bool = True`. Guarantees attribute availability without requiring expensive `getattr()` lookups during high-frequency token dispatch.
- **`End_Mode(IntEnum)`**:
  - `No_Check = 0`: Always grows extent and accepts tokens.
  - `Auto = 1` / `Semicolon = 2`: Accepts tokens until `;` punctuation is encountered.
  - `Comma = 3`: Accepts tokens until `,` punctuation is encountered.
  - `Extent = 4`: Accepts tokens while inside `self.extent`.
- **Core Methods**:
  - `within_range(token, ast_kind) -> bool`: Evaluates boundary enclosure against `end_mode`.
  - `exec_filter(token, cursor, kind)`: Dispatches token to appropriate handler.
  - `extract_1arg(CS, type_id: int, name: int | str, extent: Line | None = None) -> None`: Emits `m_ast.view(((m_ast.ast_id,),), None, str(name)[:255], type_id)` inside `with CS(REF_POS):` and dispatches `self.tag`.
  - `tag(CS, ast_id_route, extent=None, ast_name=None, ast_type=None) -> None`: Primary tag stager. Evaluates Tier 1 exact hash match, Tier 2-5 evolution transitions, emits `m_tag.set` (Rule 12 first in `REF_POS`), `m_tag_code.get_set`, `m_moved_tag.set`, `m_bridge_tag.set`, and invokes `self.map_ast`.
  - `map_ast(CS, ast_id_route, tag_route, extent=None) -> None`: Computes relative coordinates (using `self.endif` for preprocessor conditional branches) and stages `m_map_ast.set` and deduplicated `m_bridge_map.set`.

#### `Ast_Statement(Ast)` (Statement Interface)
Base class in `cursor_tree.py` for executable statements (`Ast_CompoundStmt`, `Ast_IfStmt`, `Ast_SwitchStmt`, etc.):
- Contains child collections: `zones: list[Zone]`, `operands: list[str]`, `call_exprs: list[Ast_CallExpr]`, `member_refs: list[Ast_MemberRefExpr]`, `decl_refs: list[Ast_DeclRefExpr]`, `macro_refs: list[Ast_MacroRefExpr]`.
- Attributes: `self.type_id: int` (initialized from `self.__class__.type_id`), `self.ast_ref: Any = None`.
- **Compound Body Zone Postponement**: `Ast_Statement` avoids pre-allocating a `Zone(Zone_Type.Compound_Stmt)` in `__init__`, postponing body zone creation until `{` is received in `exec_punctuation`. This ensures that condition tokens, expressions, and headers (`if (cond)`, `while (expr)`) remain within the statement scope without prematurely populating child body zones.
- **Statement Keyword Consumption**: In `exec_keyword`, statement keywords (`if`, `while`, `for`, `switch`, etc.) set `self.name` directly on the active statement before delegating to child zones, preventing duplicate phantom child statements.
- **Statement Semicolon & Depth Delimitation**: `within_range` tracks `self.paren_depth` and `self.brace_depth`. For `End_Mode.Auto` and `End_Mode.Semicolon`, encountering `;` only terminates the statement when `self.paren_depth <= 0` and `self.brace_depth <= 0`. On `}`, the statement terminates cleanly when `self.brace_depth <= 0` and all child zones are completed, allowing enclosing functions to close without swallowing subsequent declarations.
- `_extract_nested(CS)`: Iterates and extracts child zones inside `with CS(REF_MULTI):` and extracts collected expressions inside `with CS(REF_NO_REF):`.
- `extract(CS)`: Default statement extraction calling `self._extract_nested(CS)` followed by `self.extract_1arg(CS, type_id, name, self.extent)`.

#### `Ast_CompoundStmt(Ast_Statement)`
Compound block statement (`{ ... }`). Emits `{}` as name:
- Attributes: `self.used_types: set[tuple[str, int, Any]]` — collects unique `(type_name, ast_type, ref_ast_id)` tuples used throughout the body.
- When extracted within a `Zone(Zone_Type.Compound_Stmt)`, bundles child statements and used types into an atomic joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, total_containers),), None, "{}", ASTT.C_CompoundStmt, *container_args)`, retaining its `ast_ref` for parent function container linking without separate `m_ast_container.set` calls.

#### `Ast_Expression(Ast)` / `AST_Expression` (Expression & Scope Container Interface)
Base class in `cursor_tree.py` for expression and sub-construct containers (`AST_Array`, `AST_Initializer`, `AST_Enum_Equal`).

### 5.2 Statement Classes (`cursor_tree.py`)
| Class | `ASTT` Type Constant | Symbolic Value | Description |
| :--- | :--- | :---: | :--- |
| `Ast_CompoundStmt` | `ASTT.C_CompoundStmt` | 46 | Compound block statement (`{ ... }`). Emits `{}` as name. |
| `Ast_IfStmt` | `ASTT.C_IfStmt` | 47 | `if` branching construct. |
| `Ast_SwitchStmt` | `ASTT.C_SwitchStmt` | 48 | `switch (...)` control flow statement. |
| `Ast_CaseStmt` | `ASTT.C_CaseStmt` | 49 | `case ...:` statement within switch block. |
| `Ast_DefaultStmt` | `ASTT.C_DefaultStmt` | 50 | `default:` statement within switch block. |
| `Ast_WhileStmt` | `ASTT.C_WhileStmt` | 51 | `while (...)` iteration statement. |
| `Ast_DoStmt` | `ASTT.C_DoStmt` | 52 | `do ... while (...)` iteration statement. |
| `Ast_ForStmt` | `ASTT.C_ForStmt` | 53 | `for (...; ...; ...)` loop construct. |
| `Ast_ReturnStmt` | `ASTT.C_ReturnStmt` | 54 | `return ...;` statement. |
| `Ast_BreakStmt` | `ASTT.C_BreakStmt` | 55 | `break;` statement. |
| `Ast_ContinueStmt` | `ASTT.C_ContinueStmt` | 56 | `continue;` statement. |
| `Ast_GotoStmt` | `ASTT.C_GotoStmt` | 57 | `goto <label>;` jump statement. |
| `Ast_LabelStmt` | `ASTT.C_LabelStmt` | 58 | Jump target label identifier (`<label>:`). Captures and stores the bare label identifier name (e.g. `'err_out'`) in `m_ast.name` (type 58), ignoring following statement keywords (such as `return`, `if`, etc.) that share cursor extents. |
| `Ast_AsmStmt` | `ASTT.C_AsmStmt` | 59 | Inline assembly construct (`asm volatile (...)`). |
| `Ast_BinaryOperator` | `ASTT.C_BinaryOperator`| 63 | Binary operator expression in statement context. |
| `Ast_UnaryOperator` | `ASTT.C_UnaryOperator` | 64 | Unary operator expression in statement context. |
| `Ast_CastExpr` | `ASTT.C_CastExpr` | 154 | C-style cast expression `(type)expr`. |
| `Ast_ConditionalOperator` | `ASTT.C_ConditionalOperator` | 155 | Ternary conditional operator expression `? :`. |
| `Ast_ArraySubscriptExpr` | `ASTT.C_ArraySubscriptExpr` | 156 | Array subscript indexing expression `a[i]`. |
| `Ast_DesignatedInitExpr` | `ASTT.C_DesignatedInitExpr` | 157 | Designated initializer field expression `.field = val`. |
| `Ast_StaticAssert` | `ASTT.C_StaticAssert` | 183 | Compile-time assertion `static_assert(cond, msg)`. |
| `Ast_TypeofExpr` | `ASTT.C_TypeofExpr` | 184 | GNU C type deduction expression `typeof(...)`. |
| `Ast_OffsetofExpr` | `ASTT.C_OffsetofExpr` | 185 | Built-in offset expression `__builtin_offsetof(type, member)`. |
| `Ast_TypesCompatiblePExpr` | `ASTT.C_TypesCompatiblePExpr` | 186 | Type compatibility check `__builtin_types_compatible_p(t1, t2)`. |
| `Ast_ChooseExpr` | `ASTT.C_ChooseExpr` | 187 | Constant conditional selection `__builtin_choose_expr(...)`. |
| `Ast_AlignofExpr` | `ASTT.C_AlignofExpr` | 192 | Alignment inquiry `alignof(type)` / `_Alignof(type)` / `__alignof__(expr)`. |
| `Ast_AtomicExpr` | `ASTT.C_AtomicExpr` | 193 | Atomic expression or operation `_Atomic(...)` / atomic built-in. |

### 5.3 Expression & Sub-Construct Scope Containers (`cursor_tree.py`)
| Class | Base Class | Associated `Zone_Type` | Description |
| :--- | :--- | :--- | :--- |
| `AST_Array` | `AST_Expression` | `Zone_Type.Array_Content` | Scopes array dimension bounds `[ ... ]`, tracking `bracket_depth`. |
| `AST_Initializer` | `AST_Expression` | `Zone_Type.Initializer_Expr`| Scopes variable/struct initializer blocks `= { ... }`, tracking nested `brace_depth`, `paren_depth`, and `bracket_depth`. Parses designated initializer entries (`.member = value`) and bundles members (`C_MemberRefExpr` 61) and assigned values (`C_DeclRefExpr` 62) directly into an atomic joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, total_containers),), None, "{}", ASTT.C_InitListExpr, *container_args)` in `with CS(REF_POS):`, eliminating separate `m_ast_container.set` calls and preventing duplicate primary key collisions. |
| `AST_Enum_Equal` | `AST_Expression` | `Zone_Type.Enum_Equal` | Scopes enum constant assignments (`= expr`), terminating on `}` or `,`. |

### 5.4 Expressions & Relational Symbol Linking (`ast_nodes.py`, `cursor_tree.py`)
- **Symbol Resolution (`resolve_cursor_type_ast(CS, cursor) -> tuple[int, Any]`)**:
  Inspects the Clang cursor's referenced symbol or definition to resolve its AST type and relational link:
  - Function references (`FUNCTION_DECL`, `CXX_METHOD`) &rarr; stages `m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_functionprotnotbind)` and returns `(ASTT.C_functionproto, CS.ref(...))`.
  - Field references (`FIELD_DECL`) &rarr; stages `m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_structnotbind)` and returns `(ASTT.C_struct, CS.ref(...))`.
  - Record types (`RECORD`) &rarr; stages `m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_structnotbind)` and returns `(ASTT.C_struct, CS.ref(...))`.
  - Enum types (`ENUM`) &rarr; stages `m_ast.view(((m_ast.ast_id,),), None, safe_name, ASTT.C_enumnotbind)` and returns `(ASTT.C_enum, CS.ref(...))`.
  - Primitives (`INT`, `CHAR_S`, `LONG`, `POINTER`, etc.) &rarr; returns corresponding primitive `ASTT` enum value with `ref_ast_id = 0`.
- **Expression Classes (`Ast_CallExpr`, `Ast_MemberRefExpr`, `Ast_DeclRefExpr`, `Ast_MacroRefExpr`)**:
  - `Ast_CallExpr`: Function invocation `callee(...)`. If `ref_ast_id != 0`, stages joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, 1),), None, name, ASTT.C_CallExpr, None, 0, t_id, ref_ast_id)`.
  - `Ast_MemberRefExpr`: Field access `x.y` / `x->y`. Links member identifier to parent record container.
  - `Ast_DeclRefExpr`: Variable or parameter reference. Links identifier to declaration reference.
  - `Ast_MacroRefExpr`: Macro expansion or instantiation reference (`SymbolRole.MacroExpansion = 6`). Resolves macro definition and stages `m_symbol_ref`.

### 5.5 Comments, Assembly & Macros (`ast_nodes.py`)
| Class | `ASTT` Type Constant | Symbolic Value | Stored Payload | Description |
| :--- | :--- | :---: | :--- | :--- |
| `Ast_Comment` | `ASTT.C_Comment` | 2 | `name[:255]` | C comment (`//...` or `/*...*/`) truncated to 255 chars. |
| `Ast_ASM_Comment` | `ASTT.ASM_Comment` | 87 | `extent.code[:255]` | Assembly comment or preprocessor comment line. |
| `Ast_ASM_Directive`| `ASTT.ASM_Directive` | 84 | `payload[:255]` | Assembly directive (`.section`, `.align`, `.globl`). |
| `Ast_ASM_Macro` | `ASTT.ASM_Macro` | 83 | `name[:255]` | Assembly macro definition (`.macro ... .endm`). |
| `Ast_ASM_Instruction`| `ASTT.ASM_Instruction`| 85 | `mnemonic[:255]` | Assembly instruction mnemonic and operands. |
| `Ast_ASM_Label` | `ASTT.ASM_Label` | 86 | `label[:255]` | Assembly jump or symbol label. |
| `Ast_MACRO_INSTANTIATION`| &mdash; | &mdash; | Extent bounds | Macro instantiation carrier node. |

### 5.6 C Preprocessor Subsystem (`CPPro`) (`ast_nodes.py`)
All preprocessor directives begin as `CPPro(extent)`. Upon reading the directive token, `ccpro_start_flip(TargetClass, cline)` dynamically morphs the instance in place (`self.__class__ = TargetClass`).

#### Directive Classes & Behaviors:
| Class | `ASTT` Type Constant | Symbolic Value | Stored Data & Extraction Behavior |
| :--- | :--- | :---: | :--- |
| `CPPro_if` | `ASTT.CPPro_if` | 67 | Evaluated condition expression string. Extent linked to terminating `endif`. |
| `CPPro_elif` | `ASTT.CPPro_elif` | 68 | Branch condition expression string. Linked to terminating `endif`. |
| `CPPro_else` | `ASTT.CPPro_else` | 69 | Empty expression. Linked to terminating `endif`. |
| `CPPro_endif` | `ASTT.CPPro_endif` | 70 | Closes active preprocessor conditional scope in `resolve_cppro_scopes()`. |
| `CPPro_ifdef` | `ASTT.CPPro_ifdef` | 71 | Conditional macro identifier string. |
| `CPPro_ifndef` | `ASTT.CPPro_ifndef` | 72 | Conditional macro identifier string. |
| `CPPro_elifdef` | `ASTT.CPPro_elifdef`| 73 | Conditional macro identifier string. |
| `CPPro_elifndef` | `ASTT.CPPro_elifndef`| 74 | Conditional macro identifier string. |
| `CPPro_define` | `ASTT.CPPro_define` / `CPPro_define_macro` | 75 / 76 | Macro name and replacement body. Expands `extent` if trailing `\` continuation. |
| `CPPro_undef` | `ASTT.CPPro_undef` | 77 | Undefined macro identifier name. |
| `CPPro_include` | `ASTT.CPPro_include`| 78 | Resolves include target via `cursor.get_included_file()`. Strips enclosing angle brackets (`<>`) and quotes (`""`) when registering the target path in `m_file_name.get_set`, extracts external symbols used in the file attributed to this include, stages child symbol nodes in `m_ast` in `REF_POS`, and emits joined view `m_ast.view(((m_ast.ast_id, m_ast_include.ast_id, 1), (m_ast.ast_id, m_ast_container.ast_id, N)), None, w_include[:255], ASTT.CPPro_include, None, CS.ref(m_file_name.fnid, *fnid_route), *flat_container_args)`. Preserves verbatim written include syntax in `m_ast.name`. Falls back to single-relation view if `N == 0`. |
| `CPPro_include_next` | `ASTT.CPPro_include_next` | 194 | GNU `#include_next` directive for header search wrapping. |
| `CPPro_ident` | `ASTT.CPPro_ident` | 195 | `#ident` compiler identification directive string. |
| `CPPro_line` | `ASTT.CPPro_line` | 79 | Source line override: `"{lineno} {filename}"`. |
| `CPPro_error` | `ASTT.CPPro_error` | 80 | `#error` message string. |
| `CPPro_warning` | `ASTT.CPPro_warning`| 81 | `#warning` message string. |
| `CPPro_pragma` | `ASTT.CPPro_pragma` | 82 | `#pragma` directive body string. |
| `CPPro_embed` | &mdash; | &mdash; | `#embed` directive placeholder. |
| `CPPro_defined` | &mdash; | &mdash; | `defined(...)` operator placeholder. |

---

## 6. C Type System, Qualifiers & Declarations (`ast_nodes.py`, `cursor_tree.py`)

### 6.1 `CQual` Bit-Flag Qualifier Model (`ast_nodes.py`)
```python
class CQual(Flag):
    Empty = 0
    const = 1       # ASTT.C_Qconst (16)
    volatile = 2    # ASTT.C_Qvolatile (17)
    restrict = 4    # ASTT.C_Qrestrict (18)
    _Atomic = 8     # ASTT.C_Q_Atomic (19)

    def output_ast(self) -> tuple[int, ...] | None:
        """Returns enabled ASTT constants."""
```

### 6.2 `TypeToken` & `TypeSegment` (`ast_nodes.py`)
- **`TypeToken`**: Slot-optimized token carrier (`extent`, `code`, `type`, `is_definition`, `foreign_name`, `foreign_file`, `foreign_extent`).
- **`TSRef(IntEnum)`**: `No_Ref = 0`, `AST_Ref = 1`, `Route_Ref = 2`.
- **`TypeSegment`**:
  - Aggregates `content: list[TypeToken]`, `cqual: CQual`, `ref_type: TSRef`.
  - `generate_ast(CS: ChangeSet)`:
    - Single unqualified primitive: sets `type_id = content[0].type`.
    - Single unqualified typedef (`content[0].type == ASTT.C_SCtypedef`, `code != "typedef"`): resolves `sym_name` and `foreign_file` via `REF_FILE` (`CS.ref(m_ast.ast_id, REF_FILE, f_file, sym_name, int(ASTT.C_SCtypedef))`), local `CS.symbol_dict[(sym_name, int(ASTT.C_SCtypedef))]`, or unbound stub in `REF_NO_REF`, setting `ref_type = TSRef.Route_Ref` and staging `self.symbol_refs.append((target_ref, line, col))` for `SymbolRole.TypeUsage`.
    - Unbound forward struct/union/enum/proto (e.g. `struct foo *`): emits `m_ast.view(((m_ast.ast_id,),), None, "foo", notbind_type)` in `REF_NO_REF` and sets `ref_type = TSRef.Route_Ref`.
    - Qualified or compound types: constructs joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, count),), None, "", ASTT.C_Compound, ...)` mapping all qualifier and type tokens into `m_ast_container`. Compound `C_SCtypedef` tokens (e.g. `const __be32` or function pointer return types) resolve `target_ref`, append `(typetoken.type, target_ref)`, and stage `SymbolRole.TypeUsage` in `m_symbol_ref`.

### 6.3 Forward Declaration Resolution: `get_notbind_type(ast_type: int) -> int` (`ast_nodes.py`)
Maps bound declaration type constants to unbound forward reference constants:
- `ASTT.C_struct` / `ASTT.C_structdecl` &rarr; `ASTT.C_structnotbind` (29)
- `ASTT.C_union` / `ASTT.C_uniondecl` &rarr; `ASTT.C_unionnotbind` (35)
- `ASTT.C_enum` / `ASTT.C_enumdecl` &rarr; `ASTT.C_enumnotbind` (32)
- `ASTT.C_functionproto` / `ASTT.C_functionprotodecl` &rarr; `ASTT.C_functionprotnotbind` (22)
- `ASTT.C_SCtypedef` &rarr; `ASTT.C_SCtypedef` (10)

### 6.4 Declaration Type Resolution: `get_decl_type(ast_type: int) -> int` (`cursor_tree.py`)
Maps base construct type constants to definition declaration type constants:
- `ASTT.C_struct` &rarr; `ASTT.C_structdecl` (28)
- `ASTT.C_union` &rarr; `ASTT.C_uniondecl` (34)
- `ASTT.C_enum` &rarr; `ASTT.C_enumdecl` (31)
- `ASTT.C_functionproto` &rarr; `ASTT.C_functionprotodecl` (21)
- `ASTT.C_SCtypedef` &rarr; `ASTT.C_SCtypedef` (10)

---

## 7. Semantic Scope & AST Partitioning (`cursor_tree.py`, `ast_nodes.py`)

### 7.1 `Zone_Type` Enumeration (`ast_nodes.py`)
```python
class Zone_Type(IntEnum):
    Unset = 0
    Function_Args = 1     # Scopes function parameter lists: (int a, char *b)
    Declared_Args = 2     # Scopes struct/union field bodies: { int x; char y; }
    Compound_Stmt = 3     # Scopes executable block/function bodies: { ... }
    Array_Content = 4     # Scopes array subscript expressions: [ 1024 ]
    Enum_Content = 5      # Scopes enum member definitions: { A, B = 2 }
    Enum_Equal = 6        # Scopes enum assignment values: = 1 << 3
    Full_File = 7         # Top-level translation unit file scope
    Initializer_Expr = 8  # Scopes variable initialization expressions: = { 0 }
```

### 7.2 `Zone` Class Contract (`cursor_tree.py`)
- **Scope Boundary Delimitation**:
  - `paren_depth`: Tracked across `(` and `)`. Completes `Function_Args` when `paren_depth <= 0`.
  - `brace_depth`: Tracked across `{` and `}` for `_BRACE_ZONE_TYPES` (`Declared_Args`, `Enum_Content`, `Compound_Stmt`). Extent dynamically expands across comments and preprocessor directives until `}` closes the zone.
  - `bracket_depth`: Tracked across `[` and `]` for `Array_Content`.
- **Pre-Allocated Scope Sub-Containers**:
  - `Zone_Type.Array_Content`: Automatically appends `AST_Array(self.extent)` to `self.children`.
  - `Zone_Type.Initializer_Expr`: Automatically appends `AST_Initializer(self.extent)` to `self.children`.
  - `Zone_Type.Enum_Equal`: Automatically appends `AST_Enum_Equal(self.extent)` to `self.children`.
- **Cursor-Extent Partitioning & Delimiter Growth (Rule 18)**:
  - `Zone(Zone_Type.Full_File, top_cursors)`: Top-level translation unit cursors are pre-filtered via `get_top_level_cursors(parsed_tu, fullfilename)` to exclude macro expansions, included header declarations, and cursor artifacts outside the current file.
  - Child AST nodes created under `Zone_Type.Full_File` (or inside structs) receive `end_mode = End_Mode.Extent` bounded strictly by their Clang cursor extents. This prevents inline functions (which terminate with `}`) from swallowing subsequent top-level declarations.
  - Trailing punctuation delimiters in `_PUNCT_IGNORED` (`{",", ";"}`) trigger delimiter growth on the active child node (`self.children[-1].extent.grow(tline)`), absorbing semicolons while maintaining 100.00% character code coverage.
- **Factory `_create_child_node(cursor, extent) -> Ast`**:
  Maps Clang cursor kinds to specific Statement, Expression, or `C_Type` instances:
  - `DECL_STMT`, `VAR_DECL`, `FIELD_DECL`, `PARM_DECL` &rarr; `C_Type`
  - `IF_STMT` &rarr; `Ast_IfStmt`, `SWITCH_STMT` &rarr; `Ast_SwitchStmt`, `CASE_STMT` &rarr; `Ast_CaseStmt`, `DEFAULT_STMT` &rarr; `Ast_DefaultStmt`
  - `WHILE_STMT` &rarr; `Ast_WhileStmt`, `DO_STMT` &rarr; `Ast_DoStmt`, `FOR_STMT` &rarr; `Ast_ForStmt`
  - `RETURN_STMT` &rarr; `Ast_ReturnStmt`, `BREAK_STMT` &rarr; `Ast_BreakStmt`, `CONTINUE_STMT` &rarr; `Ast_ContinueStmt`
  - `GOTO_STMT` &rarr; `Ast_GotoStmt`, `LABEL_STMT` &rarr; `Ast_LabelStmt`, `ASM_STMT` &rarr; `Ast_AsmStmt`
  - `CALL_EXPR` &rarr; `Ast_CallExpr`, `MEMBER_REF_EXPR` &rarr; `Ast_MemberRefExpr`, `DECL_REF_EXPR` &rarr; `Ast_DeclRefExpr`
  - `BINARY_OPERATOR` &rarr; `Ast_BinaryOperator`, `UNARY_OPERATOR` &rarr; `Ast_UnaryOperator`
  - `COMPOUND_STMT` &rarr; `Ast_CompoundStmt`
  - `CSTYLE_CAST_EXPR` &rarr; `Ast_CastExpr`, `CONDITIONAL_OPERATOR` &rarr; `Ast_ConditionalOperator`
  - `ARRAY_SUBSCRIPT_EXPR` &rarr; `Ast_ArraySubscriptExpr`, `DESIGNATED_INIT_EXPR` &rarr; `Ast_DesignatedInitExpr`
- **Spatial Indexing & Scope Linking**:
  - `gen_lined_dict()`: Indexes child nodes by start line into `self.A_Line_Dict: dict[int, list[Ast]]`.
  - `resolve_cppro_scopes()`: Links preprocessor conditional branches to terminating `#endif`.
- **Extraction Protocol (`Zone.extract(CS)`)**:
  - Suppresses dummy parameter container creation for explicit empty parameter lists `(void)` when parameter type is `C_void` and has no identifier name.
  - For `C_Type` child nodes, executes `item.extract(CS, create_tag=create_tags)` directly (allowing `C_Type` to stage internal `REF_POS`/`REF_NO_REF`/`REF_MULTI` routes).
  - For all other child nodes (`Ast_Statement`, `CPPro`, comments, assembly), wraps execution in `with CS(REF_NO_REF): item.extract(CS, create_tag=create_tags)`. All AST node subclasses accept `create_tag: bool = True` and pass it down to `extract_1arg` and `tag()`, preventing 1-line tag creation inside nested function and struct zones when `create_tags=False`.
- **Hot-Loop Token Dispatch & Scope Optimization**:
  - `check_exec` and `within_range` avoid temporary list allocations (`[z for z in self.zones if not z.completed]`) by checking the tail zone (`self.zones[-1]`) first and short-circuiting generator expressions `any(not z.completed for z in self.zones)`.
  - Child attributes (`need_processing`, `paren_depth`, `zones`) are accessed directly rather than through `getattr()`.
  - `Zone_Type.Full_File` implements an immediate early child dispatch check at the entrance of `check_exec`, immediately routing >95% of tokens directly into active declarations and bypassing ~150 lines of dead delimiter checks.
  - In `C_Type.exec_punctuation`, `exec_identifier`, `exec_literal`, `exec_keyword`, and `exec_comment`, the tail zone `self.zones[-1]` is checked first before falling back to `reversed(self.zones)` iteration.
  - In `C_Type.within_range`, delimiter completion and auto-completion tests evaluate directly against `self.zones[-1]` when uncompleted, bypassing iterations across completed parent zones, and fallback zone checks use generator-free reverse loops `for z in reversed(self.zones[:-1]):` rather than allocating generator expressions.
  - In `C_Type.exec_identifier`, preceding compound type specifiers are evaluated in $O(1)$ via `self.struct_union_enum_type` updated during `swap_out()`, eliminating $O(N)$ linear scans across `self.typedata` (saving 100M+ iterations on large initializers).
  - In `C_Type.exec_punctuation`, prototype detection on `(` evaluates `self.has_functionproto` in $O(1)$, eliminating nested generator expressions over `self.typedata` and saving 30.5M+ generator iterations.
  - In `Zone.check_exec`, child zone activity checks evaluate directly via `bool(ch_zones and not ch_zones[-1].completed)` rather than allocating generator expressions across child statement nodes.
  - In `Zone.check_exec`, the `preset_extents` queue search breaks early if `p_extent.line_pos[0] > tline.line_pos[1]`, skipping lookahead over subsequent cursors that cannot contain `tline`.
  - In `Ast_Statement`, redundant calls to `self.extent.grow(token.line)` within `exec_keyword`, `exec_punctuation`, `exec_identifier`, and `exec_literal` are eliminated since `within_range` already expands the bounding box upon accepting tokens.
  - In `C_Type.extract`, variable/pointer checks are hoisted above the `typesegment` iteration, definition/category flags are evaluated in a single pass, and candidate token filtering uses $O(1)$ `_NON_NAME_TOKENS` set lookups.
  - In `filter_enclosing_cursors`, an $O(N \log N)$ monotonic sweep-line interval scan replaces $O(N^2)$ brute-force comparison.
  - In `get_top_level_cursors`, an in-memory `resolved_cache` eliminates repetitive filesystem `stat`/`realpath` syscalls across header declarations.

### 7.3 `C_Type` Declarator State Machine (`cursor_tree.py`)
- **Segment Swapping (`swap_out`)**:
  Buffers incoming tokens in `self.content: TypeSegment`. Moves to `self.typedata: list[TypeSegment]` when encountering qualifiers with identical flags set, pointers, array brackets, or identifiers.
- **Multi-Declarator Splitting**:
  Pairs shared base `root_type` with each subsequent declarator in `typedata`, producing `final_types = [(root_type, decl_1), (root_type, decl_2), ...]`.
- **Strict Container Isolation Invariant**:
  - `Zone_Type.Declared_Args` strictly attaches to `C_struct` / `C_union` definitions.
  - `Zone_Type.Enum_Content` strictly attaches to `C_enum` definitions.
  - `Zone_Type.Function_Args` strictly attaches to `C_functionproto`.
  - Return-type struct specifiers (`struct foo *func(...)`) have NO `Declared_Args` zone and MUST NEVER claim `Function_Args`, preventing cross-container pollution and duplicate primary key collisions in MariaDB.
- **Function Extent & Sub-Tag Elimination Invariant**:
  - Function definition tags span the entire definition from the return type to the terminating `}` (`self.extent`), rather than splitting between a signature tag and a compound body tag.
  - Sub-tags for function parameters, compound statement blocks (`{ ... }`), inner statements, and expressions are suppressed (`create_tags=False`), preventing nested recursive tags while maintaining 100.00% character code coverage across the translation unit.
  - The function body `{ ... }` is parsed in `Zone(Zone_Type.Compound_Stmt)` and emitted as `ASTT.C_CompoundStmt` with full `m_ast_container` hierarchy intact.
- **Function Container Priority Hierarchy**:
  Inside `C_Type.extract()` for function definitions:
  - **Priority 0**: Function return type (`ref_ast_id` or `0` for primitive types like `int`/`void`).
  - **Priority 1..N**: Function parameters (`PARM_DECL`) linked in parameter list declaration order.
  - **Priority N+1**: Function body compound statement (`ASTT.C_CompoundStmt`, linking `compound_ref`).
  - **Atomic View Bundling**: When a compound body exists (`Zone_Type.Compound_Stmt`), it is bundled directly into the function's joined view (`m_ast.ref_view`) via `trailing_items = ((((m_ast.ast_id, m_ast_container.ast_id, 1),), (None, ("rank",), int(ASTT.C_CompoundStmt), compound_ref)),)`. This guarantees atomic staging and decomposition by `TableEngine.view_set`, hashing the full definition (signature + body) and eliminating separate `m_ast_container.set` calls, preventing duplicate primary key collisions in MariaDB across identical signatures in different files or versions.
- **Compound Statement Scope & Atomic View Bundling (`Zone_Type.Compound_Stmt`)**:
  Inside `Zone(Zone_Type.Compound_Stmt).extract(CS)`:
  - Collects statement children `(stmt_priority, c_type, c_ref)` at priorities `1..M`.
  - Discovers all types referenced inside the function body via `collect_cursor_used_types(cursor, self.compound_stmt.used_types)` and stages them at priorities `M+1..M+K` with `type_id = ASTT.C_TypeRef`.
  - Bundles all child statements and types into an atomic joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, total_containers),), None, "{}", ASTT.C_CompoundStmt, *container_args)` in `with CS(REF_POS):`, setting `compound_ref = CS.ref(m_ast.ast_id, *ast_id_route)`.
  - Guarantees each distinct function body receives a unique structural hash in `m_ast_hash`, while identical function bodies across files share the same `ast_id` and are deduplicated without re-inserting into `m_ast_container`, completely preventing `Duplicate entry '<ast_id>-<priority>' for key 'PRIMARY'` collisions.
- **Struct Function Pointer Member Isolation (Rule 19)**:
  - For struct function pointer fields (e.g. `void (*enqueue_task)(struct rq *rq, ...);` or `__be32 (*fopen)(...);`):
    - When parsing `*` inside `self.func_proto`, `self.name` is cleared, and any preceding undeclared return type identifier is converted to `C_SCtypedef`.
    - When encountering `(` for the parameter list, a `Zone(Zone_Type.Function_Args, arg_children)` is spawned and linked directly to the field cursor's `PARM_DECL` children so parameters receive their own coordinate tags without duplicating or overwriting the parent field extent.
    - Field name identifiers encountered under `ASTT.C_functionproto` are extracted as members into `m_ast_container` records referencing the parent struct/union container.
- **Function Pointer Declarator Isolation (Rule 20)**:
  - Function pointer declarators (e.g. `typedef void (*nlm_host_match_fn_t)(struct nlm_host *host)` or struct function pointer members) contain an inner pointer grouping `(*<name>)` preceding the parameter list `(<args>)`.
  - Token dispatch treats `(` as the pointer declarator grouping whenever `C_functionproto` has not yet been registered on the node, ensuring `Zone(Zone_Type.Function_Args)` is only spawned for the actual argument list, preventing phantom `*<name>` declarator tags and guaranteeing all parameter tags are correctly extracted and linked to the parent symbol at Container Level 1 in `m_ast_container`.
- **Standalone Forward Declarations**:
  Standalone declarations (e.g. `struct svc_rqst;`) remain `ASTT.C_struct` (27) and are not marked as definitions (`C_structdecl`), preventing orphan empty container rows.
- **Typedef Declarator Extraction & Chained Underlying Type Linking**:
  - Declarator token isolation: When executing identifiers under `cc.CursorKind.TYPEDEF_DECL` matching `safe_cursor_spelling(cursor)`, the declarator receives default type `0` (avoiding phantom child container creation) and is assigned to `self.name`.
  - Function pointer & function type typedefs: Both function pointer typedefs (`typedef int (*get_block_t)(...);`) and direct function type typedefs (`typedef void compound_page_dtor(struct page *);`) are recognized in `C_Type.exec_punctuation`. When `(` follows an identifier under `cc.CursorKind.TYPEDEF_DECL` (or when `self.func_proto` is set), `ASTT.C_functionproto` is appended, `self.has_functionproto = True`, and `Zone(Zone_Type.Function_Args)` is spawned to parse parameter definitions.
  - Typedef definition categorization: Staged with `type_id = ASTT.C_SCtypedef` (10) in `m_ast` and registered in `m_symbol_def` with `type_id = ASTT.C_SCtypedef` (for primitives, structs, function pointers, and function types), while Section 3 suppresses duplicate symbol references.
  - Chained alias hierarchy: The underlying type is evaluated via `typesegment.generate_ast(CS)` and linked at Priority 0 in `m_ast_container`, with `ref_ast_id` pointing to the immediate underlying type's AST (forming an alias chain `__be32 -> __u32 -> unsigned int`).
  - Usage tracking: All typed declarations (struct/union fields, function return types, parameters, variables, and typedef aliases) that reference a typedef resolve `target_ref` (via `REF_FILE` for foreign headers or `CS.symbol_dict` locally) and stage `(target_ref, SymbolRole.TypeUsage, line, col)` in `m_symbol_ref`.

- **System Call Definition Extraction (`ASTT.C_SyscallDef = 147`)**:
  - `SYSCALL_DEFINE*` and `COMPAT_SYSCALL_DEFINE*` macros define Linux kernel system calls whose actual C entry points are named `sys_<name>` or `compat_sys_<name>`.
  - When encountering `SYSCALL_DEFINE*` / `COMPAT_SYSCALL_DEFINE*`, `C_Type.extract()` isolates the first argument from child `Zone(Zone_Type.Function_Args)` (or fallback cursor spelling) to construct the canonical syscall identifier (`sys_<name>` or `compat_sys_<name>`).
  - Staged into `m_ast` and `m_symbol_def` with `type_id = ASTT.C_SyscallDef` (147), and indexed in `CS.symbol_dict` under both its canonical name and macro spelling, enabling unified symbol lookup and cross-referencing.

- **Macro Invocation Demotion & Module Metadata**:
  - Macro demotion: `_KNOWN_MACRO_FUNCS` (`EXPORT_SYMBOL*`, `EXPORT_TRACEPOINT_SYMBOL*`, `__setup`, `early_param`, `core_param`, `module_param*`, `*initcall`, `MODULE_AUTHOR`, `MODULE_DESCRIPTION`, `MODULE_LICENSE`, `MODULE_VERSION`, `MODULE_ALIAS`, `MODULE_DEVICE_TABLE`, `MODULE_FIRMWARE`, `MODULE_INFO`, `MODULE_PARM_DESC`, `RESERVE_BRK`, `LOOP_ATTR_RO`, `ENDPROC`, `END`, `WEAK`, `_VAR_DECL_MACROS`, `_PERCPU_MACROS`, `_TRACE_MACROS`, `_MODULE_PARAM_MACROS`, `_ASM_ENTRY_MACROS`) are excluded from function definitions in `m_symbol_def`, preventing kernel macro invocations, module metadata, and tracepoint definitions from masquerading as functions. Furthermore, demoted macros outside `_VAR_DECL_MACROS`, `_PERCPU_MACROS`, `_TRACE_MACROS`, `_MODULE_PARAM_MACROS`, and `_ASM_ENTRY_MACROS` (e.g. `module_init`, `__initcall`, `EXPORT_SYMBOL`) are excluded from variable definitions in Section 4 of `C_Type.extract()`, preventing their argument tokens from registering as false `C_Compound` variable definitions in `m_symbol_def` or polluting `CS.symbol_dict`.
  - Kernel Variable Declaration Macros (`ASTT.C_MacroVarDef = 150`):
    - `_VAR_DECL_MACROS` in `parser/c_ast/cursor_tree.py` (`DEFINE_SPINLOCK`, `DEFINE_RAW_SPINLOCK`, `DEFINE_RWLOCK`, `DEFINE_MUTEX`, `DEFINE_SEQLOCK`, `DECLARE_RWSEM`, `DECLARE_MUTEX`, `DECLARE_SEMAPHORE`, `DECLARE_BITMAP`, `LIST_HEAD`, `HLIST_HEAD`, `BLOCKING_NOTIFIER_HEAD`, `RAW_NOTIFIER_HEAD`, `ATOMIC_NOTIFIER_HEAD`, `SRCU_NOTIFIER_HEAD`, `DEFINE_TIMER`, `DECLARE_WORK`, `DECLARE_DELAYED_WORK`, `DECLARE_TASKLET`, `DECLARE_WAIT_QUEUE_HEAD`, `DECLARE_WAITQUEUE`, `DEFINE_WAIT`, `DEFINE_IDR`, `DEFINE_IDA`, `DECLARE_COMPLETION`, `DECLARE_COMPLETION_ONSTACK`, `DEFINE_SIMPLE_ATTRIBUTE`) extract the declared variable name (`p_var`) from child cursors or argument zones.
    - Fallback Token Inspection: When standalone Clang parses without header expansion (`self.cursor is None`), parameter names often reside within child `typedata` (`ch.typedata[0].content[0].code`) or `ch.content` rather than `ch.name`. `C_Type.extract()` inspects child tokens to guarantee variable extraction.
    - Staged into `m_ast` and `m_symbol_def` with `type_id = ASTT.C_MacroVarDef` (150), and indexed in `CS.symbol_dict` under both `(p_var, ASTT.C_MacroVarDef)` and `(p_var, ASTT.C_DeclRefExpr)` to support cross-referencing and jump-to-definition.
  - Kernel Per-CPU Variable Declarations (`ASTT.C_PerCpuVarDef = 152`):
    - `_PERCPU_MACROS` in `parser/c_ast/cursor_tree.py` (`DEFINE_PER_CPU*`, `DECLARE_PER_CPU*`, including shared, aligned, page_aligned, read_mostly, and first variants) declare kernel per-CPU variables.
    - Extraction targets the variable name argument (argument 1 following the variable type specifier). Staged into `m_symbol_def` with dedicated `type_id = ASTT.C_PerCpuVarDef` (152), separating per-CPU variables from generic lock/primitive declarations while indexing under both `ASTT.C_PerCpuVarDef` and `ASTT.C_DeclRefExpr`.
  - Kernel Module & Boot Parameter Definitions (`ASTT.C_ModuleParamDef = 153`):
    - `_MODULE_PARAM_MACROS` in `parser/c_ast/cursor_tree.py` (`module_param`, `module_param_named`, `module_param_array`, `module_param_array_named`, `module_param_string`, `core_param`, `early_param`, `__setup`) configure driver options and kernel boot parameters.
    - Dedicated Extraction Pathway: In `C_Type.extract()`, nodes matching `_MODULE_PARAM_MACROS` extract the parameter name identifier from `self.typedata` or child zones (Arg 0), wrap tag storage in `with CS(REF_NO_REF):`, stage into `m_ast` and `m_tag`, and stage the parameter symbol into `m_symbol_def` (`ASTT.C_ModuleParamDef = 153`) and `CS.symbol_dict`. Returns immediately before variable extraction, ensuring intermediate type tokens (`int`, `charp`, `bool`), permission masks (`0644`), and setup callbacks never leak into `m_symbol_def`.
  - Low-Level Assembly Entry Points (`ASTT.C_AsmEntry = 148`):
    - `_ASM_ENTRY_MACROS` in `parser/c_ast/cursor_tree.py` (`ENTRY`, `GLOBAL`, `DO_ERROR`, `DO_ERROR_INFO`) mark global assembly labels, traps, and entry points.
    - Dedicated Extraction Pathway: In `C_Type.extract()`, nodes matching `_ASM_ENTRY_MACROS` extract the entry identifier (`p_entry`) from `self.typedata` or child zones (Arg 3 for `DO_ERROR*`, Arg 0 for `ENTRY`/`GLOBAL`), wrap tag storage in `with CS(REF_NO_REF):`, and stage into `m_symbol_def` (`ASTT.C_AsmEntry = 148`) and `CS.symbol_dict`.
  - Kernel Tracepoint Definitions (`ASTT.C_TracepointDef = 151`):
    - `_TRACE_MACROS` in `parser/c_ast/cursor_tree.py` (`TRACE_EVENT*`, `DECLARE_EVENT_CLASS`, `DEFINE_EVENT*`, `DECLARE_TRACE*`, `DEFINE_TRACE*`, `DEFINE_WRITEBACK_EVENT*`, `DEFINE_WBC_EVENT*`, etc.) define Linux kernel static tracepoints.
    - Dedicated Extraction Pathway: In `C_Type.extract()`, nodes matching `_TRACE_MACROS` extract the tracepoint identifier from `self.typedata` or child zones (`target_idx = 1` for `DEFINE_EVENT*` templates; `target_idx = 0` for `TRACE_EVENT*`, `DECLARE_EVENT_CLASS`, `DECLARE_TRACE*`, and `DEFINE_TRACE*`), stage the macro in `m_ast` and `m_tag` (wrapped in `with CS(REF_NO_REF):`), and stage the tracepoint symbol into `m_symbol_def` (`ASTT.C_TracepointDef = 151`) and `CS.symbol_dict`. Trace macros return immediately before variable extraction, guaranteeing that intermediate parameter tokens (`TP_PROTO`, `TP_ARGS`, `TP_STRUCT__entry`, etc.) never leak as dummy compound variables.
    - Macro names (`TRACE_EVENT`, `DEFINE_EVENT`, etc.) are demoted via `_KNOWN_MACRO_FUNCS` so they never masquerade as dummy function declarations.
  - Synthesized parameter zones for macro-generated functions: For functions generated via token-pasting macros lacking source token parameter lists, parameter zones and container links are synthesized from Libclang `PARM_DECL` cursors into `m_ast_container`.
  - Semicolonless Macro Invocation Termination & Parentheses Lifecycle (`_macro_paren_opened`):
    Kernel macros such as `module_init(fn)`, `module_exit(fn)`, `*initcall(fn)`, `RESERVE_BRK(...)`, `LOOP_ATTR_RO(...)`, `DO_ERROR(...)` are frequently written at file scope without trailing semicolons. When standalone Clang parses files without full header expansion, an identifier followed by parentheses without a semicolon followed by declarations could be misinterpreted by Clang as an old-style K&R C function definition header, causing subsequent function definitions to be swallowed. To prevent this:
    1. `_CLANG_KERNEL_MACRO_DEFINES` in `parser/c_ast/c_ast.py` defines initcall and reservation macros (`-Dmodule_init(x)=`, `-Dmodule_exit(x)=`, `-D*initcall(x)=`, `-DRESERVE_BRK(name,sz)=`) to empty strings.
    2. `_KNOWN_MACRO_FUNCS` in `parser/c_ast/cursor_tree.py` tracks all such kernel macros.
    3. `C_Type` maintains `self._macro_paren_opened: bool = False`. In `exec_identifier`, if `tspelling in _KNOWN_MACRO_FUNCS and not self.name`, `self.name` is recorded. In `exec_punctuation`, `(` sets `self._macro_paren_opened = True`.
    4. In `C_Type.within_range`, when `getattr(self, "name", "") in _KNOWN_MACRO_FUNCS and self._macro_paren_opened and self.paren_depth <= 0`:
       - If the next token is a semicolon `;`, `self.extent.grow(tline); self.need_processing = False; return False`, cleanly absorbing the semicolon into the macro extent.
       - If the next token is NOT a semicolon (e.g. the return type or identifier of a subsequent function definition), `self.need_processing = False; return False`, terminating the macro scope immediately without swallowing the next declaration.

- **Kernel Section Macro Defines**:
  - `_CLANG_KERNEL_MACRO_DEFINES` in `parser/c_ast/c_ast.py` expands kernel section markers (`-D__devinit=`, `-D__devinitdata=`, `-D__devinitconst=`, `-D__devexit=`, `-D__devexitdata=`, `-D__devexitconst=`, `-D__net_init=`, `-D__net_initdata=`, `-D__net_exit=`, `-D__net_exitdata=`, `-D__paginginit=`, `-D__init_refok=`, `-D__initdata_refok=`, `-D__section(x)=`, `-Dmodule_init(x)=`, `-Dmodule_exit(x)=`, `-D*initcall(x)=`, `-DRESERVE_BRK(name,sz)=`) to empty strings during Libclang translation unit compilation, preventing Clang syntax errors and preventing parser confusion on annotated functions.

- **Phase 2 Multiline Macro Continuation Splicing**:
  - In `parser/c_ast/tokenizer.py`, `TokenStream` splices line-continuation backslash-newlines (`\\\n`, `\\\r\n`) within macro definitions per C translation Phase 2 rules, preventing trailing brace tokens from sticking to backslashes (e.g. `\\\n}`).

- **Enum Content Unconditional Termination & Semicolon Scope Cleanup**:
  - In `parser/c_ast/cursor_tree.py` (`Zone_Type.Enum_Content`), encountering `}` terminates the enum content scope when `brace_depth <= 1` without waiting for child constants, preventing following struct or function definitions from being swallowed into preceding enum zones.
  - In `C_Type.within_range`, encountering top-level semicolons `;` outside braces and parentheses automatically closes and completes unclosed child zones (`Function_Args`, `Initializer_Expr`), preventing local cast expressions and unclosed zones from swallowing succeeding declarations.

- **Enum & Enumerator Constant Extraction & Symbol Tracking**:
  - **Standardized Enumerator Type (`ASTT.C_enumequal`)**:
    All enumerator constants (`ENUM_CONSTANT_DECL`), whether initialized with an explicit value (e.g. `FOO = 1`) or auto-incremented (e.g. `FOO`), are standardized to `ASTT.C_enumequal` (25) in `m_ast`.
  - **Single Enclosing Composite Tag**:
    The enclosing `enum` construct produces a single top-level tag spanning the enum declaration (from `enum` through the closing `}` or `;`), adhering to Rule 19's composite construct tag invariant. Inner enumerator constants do not emit recursive sub-tags.
  - **Enumerator Symbol Definition Staging (`m_symbol_def`)**:
    After emitting the enclosing enum's `tag_ref`, `C_Type.extract()` iterates through `Zone(Zone_Type.Enum_Content)` children and stages `m_symbol_def.set(None, blob_ref, tag_ref, child_ref, const_name, ASTT.C_enumequal, SymbolLinkage.NoLinkage, line_s, line_e)` for each enumerator constant, linking it to the enclosing enum's code tag while preserving its exact definition line coordinates.
  - **Anonymous Enum Container Suppression**:
    For anonymous enums (`enum { FLAG_A, FLAG_B };`), Libclang generates synthetic container names (e.g. `(unnamed at ...)` or `(anonymous at ...)`). The parser suppresses anonymous enum containers from `m_symbol_def`, while registering and indexing all named enumerator constants.
  - **Enumerator Expression Usage Tracking (`m_symbol_ref`)**:
    In statements and initializers, identifier tokens referencing `cc.CursorKind.ENUM_CONSTANT_DECL` are collected as `Ast_DeclRefExpr` (62), emitting `m_symbol_ref` records with `SymbolRole.DeclRef` (5).
  - **Cross-File & Local Symbol Resolution**:
    In `resolve_cursor_type_ast`, `ENUM_CONSTANT_DECL` references check foreign header origins via `REF_FILE` (`CS.ref(m_ast.ast_id, REF_FILE, rel_file, safe_name, int(ASTT.C_enumequal))`), local batch scope via `CS.symbol_dict[(safe_name, ASTT.C_enumequal)]`, and fall back to unbound `m_ast.view` nodes.

- **Compiler Attribute Extraction & Relational Storage (`m_ast_attribute`)**:
  - Compiler annotations (`__attribute__((...))`) and kernel macro attributes (`_KNOWN_KERNEL_ATTRIBUTES`, e.g. `__init`, `__read_mostly`, `__aligned(64)`, `__section(...)`, `__user`, `__percpu`) are parsed into `self.attributes` as `(name, val)` pairs.
  - Staging uses `m_ast.view(...)` joined views (`((m_ast.ast_id, m_ast_attribute.ast_id, N),)`) and **NEVER** direct `.set()` calls on `m_ast_attribute`. This ensures composite hashing into `m_ast_hash` and preserves TableEngine AST deduplication invariants.
  - `fn_cur` Resolution: In `exec_identifier`, checks `self.cursor` when it is a `FUNCTION_DECL`/`CXX_METHOD` to prevent intermediate `UNEXPOSED_ATTR` wrapper cursors from masking function prototype declarations.
  - `in_attribute` Scope Tracking: In `C_Type.exec_filter`, encountering `__attribute__`, `__attribute`, or `__declspec` sets `self.in_attribute = True` and tracks `self.attribute_paren_depth` across `(` and `)`. Inner attribute tokens and parentheses (at depth > 2) are collected into `self._current_attr_tokens` and parsed into `self.attributes` via `_parse_attribute_tokens()`.
  - Parameter List Delimitation Guard: In `C_Type.exec_punctuation`, `case "("` requires `has_func_proto` to be `True` before spawning `Zone(Zone_Type.Function_Args)`, guaranteeing that parenthesized prefixes preceding the function declarator are never misinterpreted as the parameter list.

- **Statement & Expression AST Construct Dispatches (`Zone._create_child_node`)**:
  - Dispatches `COMPOUND_ASSIGNMENT_OPERATOR` (`ASTT.C_CompoundAssignOperator` = 158), `COMPOUND_LITERAL_EXPR` (`ASTT.C_CompoundLiteralExpr` = 159), `INDIRECT_GOTO_STMT` (`ASTT.C_IndirectGotoStmt` = 160), `ADDR_LABEL_EXPR` (`ASTT.C_AddrLabelExpr` = 161), `NULL_STMT` (`ASTT.C_NullStmt` = 162), `PAREN_EXPR` (`ASTT.C_ParenExpr` = 65), `CXX_UNARY_EXPR` (`ASTT.C_SizeofExpr` = 88), `StmtExpr` (`ASTT.C_StmtExpr` = 167), `GENERIC_SELECTION_EXPR` (`ASTT.C_GenericSelectionExpr` = 168), `VA_ARG_EXPR` (`ASTT.C_VaArgExpr` = 169), `LABEL_REF` (`ASTT.C_LabelRef` = 170), `INTEGER_LITERAL` (`ASTT.C_IntegerLiteral` = 171), `FLOATING_LITERAL` (`ASTT.C_FloatingLiteral` = 172), `STRING_LITERAL` (`ASTT.C_StringLiteral` = 173), and `CHARACTER_LITERAL` (`ASTT.C_CharacterLiteral` = 174) into dedicated AST classes inheriting from `Ast_Statement`.
  - For literal nodes without cursor spellings, token streams are inspected to capture concrete literal values (`42`, `3.14`, `'c'`).

- **Parameterized Macro Argument Extraction in `m_ast_container`**:
  - Parameterized macros (`#define MACRO(a, b, c) ...`) parse parameters into child AST nodes (`ASTT.C_MacroParam` = 177).
  - The parent macro node stages joined container records into `m_ast_container` at priorities 0..N-1 using `m_ast.view(...)` multi-table views (`((m_ast.ast_id, m_ast_container.ast_id, len(param_refs)),)`), strictly preserving view deduplication without direct `.set()` invocations.
  - Preprocessor operators and directives `#embed` (`ASTT.CPPro_embed` = 165) and `defined` (`ASTT.CPPro_defined` = 166) are fully mapped.

- **Kernel Semantic Initialization, Export, Device Table & Sysfs Extraction**:
  - Initcall macros (`_INITCALL_MACROS`: `module_init`, `module_exit`, `*initcall`) are extracted into `m_symbol_def` as `ASTT.C_InitcallDef` (163) and indexed in `CS.symbol_dict[(p_init, ASTT.C_InitcallDef)]` without overriding `ASTT.C_DeclRefExpr`.
  - Symbol export macros (`_EXPORT_MACROS`: `EXPORT_SYMBOL*`) are extracted into `m_symbol_def` as `ASTT.C_ExportSymbolDef` (164) and indexed in `CS.symbol_dict[(p_sym, ASTT.C_ExportSymbolDef)]` without overriding `ASTT.C_DeclRefExpr`, ensuring variable and function reference linking resolves to primary declarations.
  - Driver device match tables (`_DEVICE_TABLE_MACROS`: `MODULE_DEVICE_TABLE`) are extracted into `m_symbol_def` as `ASTT.C_DeviceTableDef` (175) and indexed in `CS.symbol_dict[(p_sym, ASTT.C_DeviceTableDef)]`.
  - Sysfs attribute definitions (`_SYSFS_ATTR_MACROS`: `DEVICE_ATTR_*`, `SYSFS_ATTR_*`, `CLASS_ATTR_*`, `BUS_ATTR_*`, `DRIVER_ATTR_*`, `__ATTR*`) are extracted into `m_symbol_def` as `ASTT.C_SysfsAttrDef` (176) and dual-indexed in `CS.symbol_dict`.
  - Inside compound statements (`Zone_Type.Compound_Stmt`), inner preprocessor directives inherit `create_tags=False`, ensuring clean single-tag extents for enclosing function definitions without inner tag overlaps.
  - Local declarations and type definitions assign `self.type_id` so that `m_ast_container` preserves non-zero child construct types (`ASTT`).

- **Variable & Parameter Reference Linking (`SymbolRole.DeclRef = 5`)**:
  - `resolve_cursor_type_ast` resolves `VAR_DECL` and `PARM_DECL` against `CS.symbol_dict[(safe_name, ASTT.C_DeclRefExpr)]` and foreign files (`REF_FILE`), activating `SymbolRole.DeclRef = 5` in `m_symbol_ref` for variable usages within function bodies.

- **Cross-File Declaration Pointer Unwinding**:
  - In `C_Type.exec_identifier`, pointer and array indirection types (`POINTER`, `CONSTANTARRAY`, `INCOMPLETEARRAY`) are unwound via `t.get_pointee()` before `get_declaration()` to resolve underlying `struct` or `typedef` declarations across header boundaries.

---

## 8. 4-Tier Prior Tag Evolution Matching Engine (`tag_tracker.py`)

`tag_tracker.py` maintains code tag continuity across Git commit versions by tracking tag identity evolution, recycling unchanged tags, and emitting `m_moved_tag` records for modified constructs.

### 8.1 Prior Tag Index Construction (`get_prior_tags`)
1. Queries `m_bridge_tag` joined via a single 3-way relational view query with `m_tag` and `m_ast` for `old_fid` in previous release `old_vid`. This retrieves coordinates, version lifecycle, code snippet hash, and AST symbol name/type in one batched database operation.
2. Constructs multi-dimensional inverted lookup indices on `CS`:
   - `prior_tags_map`: `dict[bytes, list[tuple[int, int]]]` &mdash; Maps 32-byte binary SHA-256 hash to `[(idx, tag_id), ...]`.
   - `prior_tags_by_name`: `dict[tuple[str, int] | str, list[tuple[int, int, int, int]]]` &mdash; Maps `(symbol_name, type_id)` to prior tags `[(idx, tag_id, line_s, line_e), ...]`.
   - `prior_tags_by_pos`: `list[tuple[int, int, int, int, str, Any]]` &mdash; Spatial line index `(line_s, line_e, idx, tag_id, name, type)`.
   - `prior_unmatched_symbols`: `list[tuple[int, int, str, int, int, int]]` &mdash; Pool of unconsumed named constructs for bulk rename detection.
3. Loads the stored symbol items of those tags (`core.SymbolRows.load_prior_items()`: one `m_tag_ref` and one `m_tag_def` query through `G.TE.get_in()`), so a tier-1 recycled tag's current definitions and references can be compared with what it stored.

### 8.2 The 4-Tier Transition Matching Pipeline (`match_prior_tag_transition`)

```python
def match_prior_tag_transition(
    CS: Any,
    extent: Any,
    ast_name: str | None = None,
    ast_type: Any = None,
    prev_anchor: str | None = None,
    next_anchor: str | None = None,
) -> int | None:
```

```
Incoming Modified AST Node Extent & Metadata
  │
  ├─► TIER 1: Exact Snippet Hash Match (check_exact_match)
  │   └── 32-byte binary SHA-256 matches prior tag -> Reuses prior tag_id via m_bridge_tag.set
  │
  ├─► TIER 2: Exact Symbol Name & Construct Category Match (prior_tags_by_name)
  │   └── Named construct (function, struct, enum, macro) matches name & type -> Emits m_moved_tag
  │
  ├─► TIER 3: Bulk Prefix / Suffix Rename Refactoring Detection (_detect_bulk_rename_candidate)
  │   └── Evaluates prefix/suffix overlap (>= 3 chars) + line distance -> Emits m_moved_tag
  │
  └─► TIER 4: Spatial Proximity & Line Overlap Fallback (prior_tags_by_pos)
      └── Matches modified anonymous blocks based on line overlap or <= 2 line distance -> Emits m_moved_tag
```

### 8.3 Prior Tag Lifecycle (`close_prior_tags`)
Under the immutable Git DAG architecture, `m_tag` records are permanent, content-addressable entities; `close_prior_tags(CS)` operates as a clean no-op without mutating existing tag entries.

---

## 9. Relational ChangeSet Extraction & Engine Invariants (`cs_extractor.py`, `ast_nodes.py`)

### 9.1 `Ast.tag` vs `CSExtractor.stage_tag`
- **`Ast.tag(CS, ast_id_route, extent=None, ast_name=None, ast_type=None)`**: Primary instance method on intermediate AST nodes. Slices code from `rawfile` if missing, checks Tier 1 hash recycling, dispatches Tier 2–5 evolution transitions, emits Rule 12 ChangeSet operations in `REF_POS`, creates file-to-tag bridges, and calls `self.map_ast(CS, ast_ref, tag_ref, ext)` (which dynamically incorporates `self.endif` to calculate relative preprocessor branch line counts).
- **`CSExtractor.stage_tag(CS, ast_id_route, extent, ast_name=None, ast_type=None) -> tag_ref`**: Standalone static helper method in `cs_extractor.py` performing identical Rule 12 staging for standalone extent objects outside the `Ast` class hierarchy.

### 9.2 Rule 12 & Rule 14 ChangeSet Staging Sequence
```python
# 1. Slice exact source code & compute 32-byte binary SHA-256 digest (Rule 14)
code_str = extent.code if extent else ""
code_hash = compute_code_hash(code_str)

# 2. Check Tier 1 exact match
recycled_tag_id = check_exact_match(CS, code_hash)
if recycled_tag_id is not None:
    CS.store(m_bridge_tag.set(
        ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
        recycled_tag_id,
        extent.line_pos[0], extent.line_pos[1],
        extent.char_pos[0], extent.char_pos[1],
    ))
    return recycled_tag_id

# 3. Check Tier 2-5 evolution match
s_tag_id = match_prior_tag_transition(CS, extent, ast_name, ast_type)
ast_ref = (
    CS.ref(m_ast.ast_id, *ast_id_route)
    if not (isinstance(ast_id_route, tuple) and len(ast_id_route) == 3 and ast_id_route[1] == OP_REF)
    else ast_id_route
)

# 4. Rule 12 Staging Invariant: m_tag.set MUST BE FIRST inside with CS(REF_POS):
with CS(REF_POS):
    CS.store(m_tag.set(None, code_hash, ast_ref, 0, 0))
    tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
    # Auxiliary tables staged after m_tag.set
    CS.store(m_tag_code.get_set(code_hash, code_str))
    if s_tag_id is not None:
        CS.store(m_moved_tag.set(s_tag_id, tag_ref))

# 5. File-to-tag bridge (source coordinates: 1-based, inclusive start, exclusive end)
CS.store(m_bridge_tag.set(
    ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
    tag_ref,
    extent.line_pos[0], extent.line_pos[1],
    extent.char_pos[0], extent.char_pos[1],
))

# 6. Intra-tag spatial map & bridge map (deduplicated via CS.register_bridge_map)
rel_line_e = max(1, extent.line_pos[1] - extent.line_pos[0] + 1)
rel_char_e = extent.char_pos[1]
CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref))

if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_ref, tag_ref):
    CS.store(m_bridge_map.set(tag_ref, tag_ref))

return tag_ref
```

---

### 9.3 Symbol Definitions & References (`core/SymbolRows.py`)
- Parsers no longer store `m_symbol_def` / `m_symbol_ref` rows while extracting. Every former staging site records the item instead: `record_symbol_def(CS, tag_ref, ast_ref, name, type_id, linkage, line_s, line_e)` and `record_symbol_ref(CS, tag_ref, target, target_fnid, role, line, char_s)` (the surrounding `with CS(REF_POS):` stays, since some arguments are positional references). Pending references go through `flush_pending_symbol_refs(CS, tag_ref)` (same per-flush deduplication as before), and `Ast.tag()` / `CSExtractor.stage_tag()` register every placed tag with its lines, start column and text (`register_symbol_tag(CS, tag_ref, line_s, line_e, char_s, code)`, both the tier-1 recycled and the new branch).
- `c_ast_parse()`, `asm_ast_parse()` and `ChangeSet.parse()` end with `finish_symbol_rows(CS)` (idempotent), which emits all symbol operations at the end of the file:
  - Items recorded against a tag that does not contain their lines (pending references are flushed to the next tag created) move to the innermost tag that does; items no tag contains stay per blob.
  - Each item inside its tag gets a `sym_key` from the tag's text only: a digest of the tag's code with the identifier at the reference and its role, or with the definition's name and type. It never depends on the target's form (an unresolved route in the first parse, an AST id once `CS.ref()` can resolve it against the database), so a recycled tag compares equal in every version. A key that means two different targets in this file (shadowed locals; targets compared by Merkle fingerprint) is refined by the item's position in the tag; a key that still means two targets (the same tag placed twice and resolved differently) keeps those placements per blob.
  - New tag: `m_tag_def` / `m_tag_ref` rows with lines relative to the tag. Recycled tag whose current items equal the stored ones: nothing. Otherwise (changed items, or no stored items): `m_symbol_own` marker plus per-blob `m_blob_symbol_def` / `m_blob_symbol_ref` rows.
  - One `m_blob_symbol(blob, sym_key, target_ast_id, target_fnid)` row per key used by shared rows: the target resolved in this blob.
- Emitting at the end moves the symbol operations after the AST/tag operations; stubs created when a reference cannot be resolved therefore get their ids later (content unchanged).
- `G.SYMBOL_LAYOUT = "blob"` (config `parser.symbol_layout`) stores every item per blob, still giving the same view rows.
- `get_rel_file_for_cursor(None, cursor)` (e.g. from `resolve_cursor_foreign_info()`) resolves paths against the version being parsed (`G.CURRENT_PARSING_DIR`, set by `Ast_Manager`). It used to fall back to the `"v3.0"` workspace and then to the part after `/linux/`, so outside v3.0 foreign types got truncated paths such as `slab_def.h`.

## 10. Complete `ASTT` Type System & Relational Database Mappings

### 10.1 Authoritative `ASTT` Enumeration Mapping (`core/globalstuff.py`)
`ASTT` inherits from `IntEnum` and is populated sequentially via `auto()`, starting from `Undefined = 0`.

> [!IMPORTANT]
> **Code Invariant**: Code and tests must ALWAYS reference symbolic constants (e.g. `ASTT.C_struct`, `ASTT.CPPro_if`, `ASTT.ASM_Directive`) rather than hardcoded integer literals.

| `ASTT` Category | Constants, Identifiers & Exact Integer Values |
| :--- | :--- |
| **Primitives** | `C_void (36)`, `C_unsigned (37)`, `C_signed (38)`, `C_char (39)`, `C_short (40)`, `C_int (41)`, `C_long (42)`, `C_longlong (180)`, `C_int128 (181)`, `C_complex (182)`, `C_bool (43)`, `C_float (44)`, `C_double (45)`, `C_pointer (26)`, `C_array (23)`, `C_arrayempty (24)` |
| **Qualifiers** | `C_Qconst (16)`, `C_Qvolatile (17)`, `C_Qrestrict (18)`, `C_Q_Atomic (19)` |
| **Declarations** | `C_functionproto (20)`, `C_functionprotodecl (21)`, `C_functionprotnotbind (22)`, `C_SyscallDef (147)`, `C_AsmEntry (148)`, `C_ModuleAttr (149)`, `C_MacroVarDef (150)`, `C_TracepointDef (151)`, `C_PerCpuVarDef (152)`, `C_ModuleParamDef (153)`<br>`C_struct (27)`, `C_structdecl (28)`, `C_structnotbind (29)`<br>`C_enum (30)`, `C_enumdecl (31)`, `C_enumnotbind (32)`, `C_enumequal (25)`<br>`C_union (33)`, `C_uniondecl (34)`, `C_unionnotbind (35)` |
| **Storage Class** | `C_SCauto (4)`, `C_SCregister (5)`, `C_SCstatic (6)`, `C_SCextern (7)`, `C_SC_Thread_local (8)`, `C_SCthread_local (9)`, `C_SCtypedef (10)`, `C_SCconstexpr (11)` |
| **Function Spec** | `C_FSinline (12)`, `C_FS_Noreturn (13)` |
| **Align / Misc** | `C_AS__Alignas (14)`, `C_AS_alignas (15)`, `C_Compound (1)`, `C_Comment (2)`, `C_Keyword (3)` |
| **Statements** | `C_CompoundStmt (46)`, `C_IfStmt (47)`, `C_SwitchStmt (48)`, `C_CaseStmt (49)`, `C_DefaultStmt (50)`, `C_WhileStmt (51)`, `C_DoStmt (52)`, `C_ForStmt (53)`, `C_ReturnStmt (54)`, `C_BreakStmt (55)`, `C_ContinueStmt (56)`, `C_GotoStmt (57)`, `C_LabelStmt (58)`, `C_AsmStmt (59)`, `C_StaticAssert (183)` |
| **Expressions** | `C_CallExpr (60)`, `C_MemberRefExpr (61)`, `C_DeclRefExpr (62)`, `C_BinaryOperator (63)`, `C_UnaryOperator (64)`, `C_ParenExpr (65)`, `C_InitListExpr (66)`, `C_SizeofExpr (88)`, `C_TypeRef (89)`, `C_CastExpr (154)`, `C_ConditionalOperator (155)`, `C_ArraySubscriptExpr (156)`, `C_DesignatedInitExpr (157)`, `C_TypeofExpr (184)`, `C_OffsetofExpr (185)`, `C_TypesCompatiblePExpr (186)`, `C_ChooseExpr (187)` |
| **Preprocessor** | `CPPro_if (67)`, `CPPro_elif (68)`, `CPPro_else (69)`, `CPPro_endif (70)`, `CPPro_ifdef (71)`, `CPPro_ifndef (72)`, `CPPro_elifdef (73)`, `CPPro_elifndef (74)`, `CPPro_define (75)`, `CPPro_define_macro (76)`, `CPPro_undef (77)`, `CPPro_include (78)`, `CPPro_line (79)`, `CPPro_error (80)`, `CPPro_warning (81)`, `CPPro_pragma (82)` |
| **Assembly** | `ASM_Macro (83)`, `ASM_Directive (84)`, `ASM_Instruction (85)`, `ASM_Label (86)`, `ASM_Comment (87)` |

### 10.2 Standard C Keywords Mapping (`STANDARD_C_KEYWORDS`)
Defined in `core/globalstuff.py`, `STANDARD_C_KEYWORDS: dict[str, ASTT]` provides complete, bidirectional mapping from all standard C keywords to their canonical `ASTT` enum types:
- **Primitives**: `void` &rarr; `C_void`, `char` &rarr; `C_char`, `short` &rarr; `C_short`, `int` &rarr; `C_int`, `long` &rarr; `C_long`, `float` &rarr; `C_float`, `double` &rarr; `C_double`, `signed` &rarr; `C_signed`, `unsigned` &rarr; `C_unsigned`, `_Bool` / `bool` &rarr; `C_bool`.
- **Qualifiers & Specifiers**: `const` &rarr; `C_Qconst`, `volatile` &rarr; `C_Qvolatile`, `restrict` &rarr; `C_Qrestrict`, `_Atomic` &rarr; `C_Q_Atomic`, `inline` &rarr; `C_FSinline`, `_Noreturn` &rarr; `C_FS_Noreturn`.
- **Storage Classes**: `auto` &rarr; `C_SCauto`, `register` &rarr; `C_SCregister`, `static` &rarr; `C_SCstatic`, `extern` &rarr; `C_SCextern`, `typedef` &rarr; `C_SCtypedef`, `_Thread_local` &rarr; `C_SC_Thread_local`, `thread_local` &rarr; `C_SCthread_local`, `constexpr` &rarr; `C_SCconstexpr`.
- **Constructs & Operators**: `struct` &rarr; `C_struct`, `union` &rarr; `C_union`, `enum` &rarr; `C_enum`, `sizeof` &rarr; `C_SizeofExpr`, `_Alignas` &rarr; `C_AS__Alignas`, `alignas` &rarr; `C_AS_alignas`.
- **Control Flow Statements**: `if` &rarr; `C_IfStmt`, `else` &rarr; `C_IfStmt`, `switch` &rarr; `C_SwitchStmt`, `case` &rarr; `C_CaseStmt`, `default` &rarr; `C_DefaultStmt`, `while` &rarr; `C_WhileStmt`, `do` &rarr; `C_DoStmt`, `for` &rarr; `C_ForStmt`, `return` &rarr; `C_ReturnStmt`, `break` &rarr; `C_BreakStmt`, `continue` &rarr; `C_ContinueStmt`, `goto` &rarr; `C_GotoStmt`.

### 10.3 Relational Database Table Map (`core/DBLayout.py`)
```
+----------------------------------------------------------------------------------------------------+
|                                    DATABASE TABLE SCHEMA MAP                                       |
+----------------------------------------------------------------------------------------------------+
| Table Name          | Table ID | Schema Columns                                                     |
| ------------------- | :------: | ------------------------------------------------------------------ |
| m_project           |    0     | (project_id, name, slug)                                           |
| m_file_name         |    1     | (fnid, fname)                                                      |
| m_file_blob         |    2     | (blob_id, blob_hash, ftype) [aliased as m_file]                    |
| m_bridge_checkpoint |    3     | (commit_id, fnid, blob_id) [aliased as m_bridge_file]              |
| m_blob_transition   |    4     | (old_blob_id, new_blob_id) [aliased as m_moved_file]               |
| m_type_descriptor   |    5     | (type_id, name)                                                    |
| m_ast               |    6     | (ast_id, name, type_id)                                            |
| m_ast_container     |    7     | (ast_id, priority, type_id, ref_ast_id)                            |
| m_ast_include       |    8     | (ast_id, fnid)                                                     |
| m_ast_debug         |    9     | (ast_id, ast_raw)                                                  |
| m_tag_code          |   10     | (hash, code)                                                       |
| m_tag               |   11     | (tag_id, hash, ast_id, hl_s, hl_l)                                 |
| m_bridge_tag        |   12     | (blob_id, tag_id, line_s, line_e, char_s, char_e)                  |
| m_map_ast           |   13     | (map_id, line_s, char_s, line_e, char_e, ast_id)                   |
| m_bridge_map        |   14     | (tag_id, map_id)                                                   |
| m_ast_hash          |   15     | (hash, ast_id)                                                     |
| m_moved_tag         |   30     | (s_tag_id, e_tag_id)                                               |
| m_symbol_def        |   31     | (def_id, blob_id, tag_id, ast_id, name, type_id, linkage, line_s, line_e) |
| m_symbol_ref        |   32     | (ref_id, blob_id, tag_id, ast_id, target_fnid, role, line, char_s)  |
| m_file_reference    |   33     | (ref_id, source_blob_id, target_fnid, ref_type, line_no, details)  |
| m_ast_attribute     |   35     | (ast_id, priority, name, val)                                      |
+----------------------------------------------------------------------------------------------------+
```

---

## 11. Critical Engine Invariants & AI Development Guidelines

1. **Relative File Paths**: All file links and documentation references must use relative paths from git root (e.g. `parser/c_ast/cursor_tree.py`). Never use absolute machine file URIs.
2. **Tag Reference Order Invariant**: Inside `with CS(REF_POS):`, `m_tag.set(None, code_hash, ast_ref, 0, 0)` (5 columns: `tag_id, hash, ast_id, hl_s, hl_l`) MUST be the first staged operation so `tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))` points directly to `m_tag`. Auxiliary tables (`m_tag_code.get_set`, `m_moved_tag.set`) must follow `m_tag.set`.
3. **Cryptographic Hashing Standard**: Code snippets must be hashed using `compute_code_hash(code)` from `core.globalstuff`, returning a 32-byte binary SHA-256 digest (`BINARY(32)`).
4. **TableEngine Agnosticism**: Tests and workflows must never access private engine internals (e.g. `_cached_rows`, `_pk_index`). Queries must use public TableEngine APIs (`Table.get()`, `G.TE.get()`) or assert against database state (`MockDB._global_store`).
5. **Form Feed & Line Splitting**: Never use `splitlines()` on source text. Use `raw_content.replace("\r\n", "\n").split("\n")` to preserve 1:1 line index parity with Libclang across files containing form-feed characters (`\x0c`).
6. **Delimiter Encapsulation & 100.00% Coverage**: Semicolons (`;`), closing braces (`}`), and macro continuation backslashes (`\`) must be encapsulated within AST node extents to ensure 100.00% character coverage and zero orphan characters.
7. **Bridge Map Deduplication**: Always register tag bridge map links via `CS.register_bridge_map(tag_ref, tag_ref)` before emitting `m_bridge_map.set` to prevent duplicate primary key crashes in MariaDB.
8. **Schema String Bounds**: Every string written to `m_ast.name` must be bounded to 255 characters (`safe_name = str(name)[:255]`) to satisfy database column constraints.
9. **Import Order Guard**: Always ensure `core` modules (`core.globalstuff`, `core.DBLayout`) are imported before `parser.c_ast` components to prevent circular import deadlocks.
10. **Exact Rename Optimization**: When `CS.file_operation == "R100"`, `c_ast_parse` must return immediately without emitting records.
11. **Testing Verification**: Always validate changes using `python3 -m unittest tests/test_c_ast.py` and run `python3 main.py -u` once.
12. **Cursor Extent Boundary Partitioning**: Top-level translation unit declarations and struct members must be bounded by their Clang cursor extents (`End_Mode.Extent`). Trailing delimiters (`;`, `}`) must be encapsulated via `_PUNCT_IGNORED` delimiter growth (`child.extent.grow(tline)`) rather than unbounded semicolon seeking (`End_Mode.Auto`), preventing declaration swallowing while guaranteeing 100.00% character code coverage.
13. **Struct Function Pointer Member Isolation**: Function pointer fields within structs/unions (`void (*fn)(params)`) must scope their parameter list in `Zone(Zone_Type.Function_Args, arg_children)` attached to child `PARM_DECL` cursors, clear `self.name` upon pointer indirection (`*`), and extract the member declarator into `m_ast_container` pointing to the parent struct, ensuring parameters do not overwrite or duplicate over the parent field tag extent.
14. **Function Pointer Declarator Isolation**: Function pointer declarators (e.g. `typedef void (*nlm_host_match_fn_t)(struct nlm_host *host)` or struct function pointer members) contain an inner pointer grouping `(*<name>)` preceding the parameter list `(<args>)`. Token dispatch must treat `(` as the pointer declarator grouping whenever `C_functionproto` has not yet been registered on the node, ensuring `Zone(Zone_Type.Function_Args)` is only spawned for the actual argument list, preventing phantom `*<name>` declarator tags and guaranteeing all parameter tags are correctly extracted and linked to the parent symbol at Container Level 1 in `m_ast_container`.
15. **Hot-Loop State Verification & $O(\log N)$ Delimiter Lookup**: Trailing punctuation search (`encapsulate_trailing_delimiter`) must use `bisect.bisect_left` over tokens by start line, reducing delimiter absorption from $O(N)$ to $O(\log N)$. Ast node dispatch must use class-level default attributes (`zones`, `paren_depth`, `need_processing`) and short-circuiting generator checks (`any(...)`) rather than `getattr` or temporary list allocations.
16. **Macro Pipeline & Scope Dispatch Invariants (Phases 3 & 4)**:
    - Translation unit cursor extraction (`get_top_level_cursors`) must cache filename resolution in `resolved_cache` to prevent redundant OS filesystem `realpath`/`stat` syscalls during header traversal.
    - Cursor enclosure filtering (`filter_enclosing_cursors`) must employ an $O(N \log N)$ monotonic sweep-line interval scan tracking active enclosing ranges, preventing $O(N^2)$ brute-force comparison loops across large header files.
    - Token streaming (`TokenList.process_tokens`) must associate `token._cursor = cursors_array[i]` during `TokenStream` ingestion to stream tokens directly in a single loop without allocating temporary 2-tuples on the heap.
    - Root zone dispatch (`Zone_Type.Full_File`) must check the active child construct at the entrance of `check_exec`, bypassing dead delimiter checks on the 95%+ of tokens residing inside functions, structs, and arrays.
    - Code tag staging (`Ast.tag`) must hoist transition matching functions to module level and avoid re-slicing text from `rawfile` if `extent.code` is already set.
17. **Hot-Path Type Invariant & Foreign FFI Specialization (Phase 5)**:
    - Spatial coordinate construction (`Line.__init__`) must evaluate integer coordinate inputs (`type(arg0) is int`) directly at entry, bypassing cascading `isinstance()` and `hasattr()` checks.
    - Cursor coordinate queries (`get_cursor_line`) must pass `None` for file and offset pointers to `_CLANG_GET_SPELLING_LOC`, instructing Libclang to bypass FileID table queries.
    - Scope completion in `C_Type.within_range` and child zone queries in `Zone.check_exec` must evaluate active tail zones directly, avoiding generator allocations on inactive or statement children.
18. **Compound Declarator & Function Prototype State Invariants (Phase 6)**:
    - Multi-declarator and large array initializer tokens (`C_Type.exec_identifier`) must evaluate preceding compound type specifiers via $O(1)$ tracked state (`self.struct_union_enum_type`) updated during `swap_out()`, completely bypassing $O(N)$ linear scans over `self.typedata` (eliminating 100M+ redundant loop iterations on large opcode tables).
    - Function prototype detection (`C_Type.exec_punctuation` under `case "("`) must evaluate $O(1)$ tracked state (`self.has_functionproto`) updated during function declaration registration and pointer indirection, completely eliminating nested generator expressions over `self.typedata` and `self.content` (eliminating 30.5M+ generator iterations).
    - Scope containment queries in `C_Type.within_range` must use generator-free reverse loops `for z in reversed(self.zones[:-1]): if not z.completed:` rather than `any(...)` expressions.
19. **Function Extent & Composite Construct Tagging Invariant**:
    Composite constructs (functions, structs, unions, enums) produce a single enclosing top-level tag spanning their entire syntactic bounds (for functions: return type through the terminating `}`; for structs/unions/enums: type keyword through trailing `;`). Inner child zones (`Declared_Args`, `Enum_Content`, `Function_Args`, `Compound_Stmt`) suppress recursive sub-tag creation while fully preserving their `m_ast` representations and `m_ast_container` hierarchies.
20. **Function & Compound Statement Container Priority Hierarchy**:
    In `m_ast_container`, Priority 0 is strictly reserved for the return type (`ref_ast_id` or `0` for primitive types), Priorities 1..N link function parameters in declaration order, and Priority N+1 links the compound statement body (`ASTT.C_CompoundStmt`, `compound_ref`). Within the compound statement container, child statements are linked at Priorities 1..M, and used types within the function body (collected via recursive Clang cursor inspection for `TYPE_REF`, `VAR_DECL`, `MEMBER_REF_EXPR`, `CALL_EXPR`) are staged at Priorities M+1..M+K using `ASTT.C_TypeRef`.
21. **Struct Designated Initializer and Jump Label Extraction**:
    - Jump target labels (`Ast_LabelStmt`) capture the label name directly from cursor spelling or leading identifier, preventing following statement keywords (e.g. `return`) from overwriting the label name.
    - Struct variables initialized with designated initializers (`= { .field = value, ... }`) scope the initializer block as `Zone(Zone_Type.Initializer_Expr)` with `brace_depth` tracking to preserve commas between fields.
    - The enclosing `{}` emits `C_InitListExpr` (66), each designated field `.field` emits `C_MemberRefExpr` (61), and the assigned value emits `C_DeclRefExpr` (62).
    - In `m_ast_container`:
      - Struct Variable &rarr; Priority 0 &rarr; struct type definition, Priority `N` &rarr; `C_InitListExpr` `{}`
      - `C_InitListExpr` `{}` &rarr; Priorities `0..k` &rarr; designated `C_MemberRefExpr` fields
      - Designated `C_MemberRefExpr` &rarr; Priority 0 &rarr; assigned value (`C_DeclRefExpr`, `C_MemberRefExpr`, etc.)
    - Spatial tags and coordinate mappings (`m_map_ast`) are emitted for `{}` and designated fields/values.
22. **Cross-File Symbol Origin Tracking and Co-Declared Struct Extraction**:
    - `CS.symbol_dict`: During AST parsing, `CS.symbol_dict[(name, type_id)] = op_pos` records the operational position of symbol declarations, definitions, and types.
    - `REF_FILE` Deferred Resolution Invariant: `ChangeSet.ref()` must never eagerly evaluate `REF_FILE` routes or invoke `safe_get_cs` during AST parsing, returning `(query, OP_REF, parsed_route)` immediately to eliminate recursive Clang translation unit compilation cascades.
    - During parallel multicore parsing, `CS.batch_cs_dict` provides worker-local intra-batch scope for inspecting sibling ChangeSets parsed in the active batch.
    - During `ChangeSet.execute()`, `resolve_ref()` resolves `REF_FILE` references via TableHandling: checking `gp.ChangeSet_Dict` (or `batch_cs_dict`) if the defining file was modified in the active version, executing operations out-of-order so independent symbols publish immediately, tracking blocking dependencies in `self.blocked_on`, and otherwise staging `notbind` stubs directly in TableHandling (`m_ast`) via `force_stubs=True` to break circular dependency deadlocks with warning logs.
    - `m_symbol_def` (Table 31): Staged during definition extraction for functions (`ASTT.C_functionproto`), structs (`ASTT.C_struct`, `ASTT.C_structdecl`), unions, enums, typedefs (`ASTT.C_SCtypedef`), preprocessor macros (`ASTT.CPPro_define`, `ASTT.CPPro_define_macro`), assembly labels/macros, and jump labels. Records `(def_id, blob_id, tag_id, ast_id, name, type_id, linkage, line_s, line_e)` (9 columns), where `linkage` is typed as `SymbolLinkage` (`ExternalDef = 0`, `InternalStaticDef = 1`, `PrototypeDecl = 2`, `InlineHeaderDef = 3`, `WeakOrAliasDef = 4`, `MacroOrKernelDef = 5`).
    - `m_symbol_ref` (Table 32): Staged for every occurrence of a symbol: declarations (`SymbolRole.Declaration = 1`), type usages (`SymbolRole.TypeUsage = 2`), function calls (`SymbolRole.Call = 3`), struct member references (`SymbolRole.MemberRef = 4`), identifier references (`SymbolRole.DeclRef = 5`), and macro expansions (`SymbolRole.MacroExpansion = 6`). Records `(ref_id, blob_id, tag_id, ast_id, target_fnid, role, line, char_s)` (8 columns), where `target_fnid` tracks the declaring file ID or 0 if local.
    - Co-declared structs and variables (e.g. `struct foo { int a; } my_var;`): The parser isolates the struct extent up through the closing brace `}` of `Declared_Args`, stages `m_symbol_def` for the canonical struct type, and then extracts the declared variable extent linking its container Priority 0 to the struct's `ast_id` and staging an `m_symbol_ref` with `SymbolRole.TypeUsage`.
    - Struct Bitfield Extraction & Pahole Layout Attributes:
      - Bitfields (`type name : width;`) are parsed in `C_Type` with bitfield absorption in `exec_punctuation` (`:` handler), setting `in_bitfield` and `bitfield_width` without allowing the width literal to overwrite the declarator identifier name.
      - Bitfield width is staged as `ASTT.C_Bitfield` in `m_ast`, and stored in `m_ast_attribute` with attributes `bitfield_width`, `offset_bits`, and `size_bytes`.
    - Comprehensive Libclang Type Dispatch (`resolve_cursor_type_ast`):
      - Recursively unwraps `ELABORATED` and `ATTRIBUTED` cursor type wrappers down to canonical underlying types.
      - Maps scalar primitives, multi-token specifiers (`long long`, `long double`, `_Float128`, `_Float16`), vector extensions (`ASTT.C_Vector`), variable-length arrays (`ASTT.C_vla`), complex numbers, and atomic types.
    - Tag Tracker 14-Column 3-Way Join & Immutable Tag Recycling:
      - `get_prior_tags(CS)` joins `m_bridge_tag` (6 cols) &rarr; `m_tag` (5 cols) &rarr; `m_ast` (3 cols) = 14 columns total.
      - Exact field offsets: `tag_hash` (index 7), `ast_id` (index 8), `ast_name` (index 12), `ast_type` (index 13).
      - In zero-VID DAG, tags are permanent content-addressed entities; tag recycling never closes or mutates previous tags.
23. **Slot-Optimized Token, Deduplicated Streaming & Immediate Post-Extraction Teardown Invariant**:
    - Tokens streamed through `TokenStream` must use `ParsedToken` with `__slots__ = ("line", "spelling_str", "ast_kind", "_cursor")` to eliminate Python instance `__dict__` overhead on ctypes objects.
    - Cursors must inherit translation unit reference via class-level `cc.Cursor._tu = parsed_tu` rather than per-instance dictionary assignment.
    - Consecutive tokens residing on the same line must reuse `(s_line, e_line)` tuples, eliminating tens of thousands of redundant tuple allocations.
    - Adjacent tokens sharing identical cursor kinds and raw ctypes bytes (`k_id == prev_kind and bytes(raw_c) == prev_bytes`) must reuse the preceding `Cursor` instance, bypassing 5-attribute field comparisons and cutting Python cursor allocations by >50%.
    - Single-line token slicing caches the current raw source line (`prev_raw_line`), eliminating repeated tuple indexing into `rawfile`.
    - Identifier token dispatch in `Ast_Statement.exec_identifier` and `AST_Initializer.exec_identifier` must fast-path known expressions (`CALL_EXPR`, `MEMBER_REF_EXPR`) and filter out macros/directives (`_SKIP_REF_KINDS`), only lazily evaluating `cursor.referenced` when resolving declaration targets to eliminate tens of thousands of Libclang internal Python wrapper/dictionary allocations.
    - During pre-tokenization include symbol discovery (`extract_include_symbols`), traversal is executed via `_CLANG_VISIT_CHILDREN` filtering to `_REFERENCING_KIND_IDS` and checking `_CLANG_CURSOR_IS_NULL`, bypassing recursive Python calls and skipping statement nodes.
    - During AST extraction, `collect_cursor_used_types` traverses descendants via `_CLANG_VISIT_CHILDREN` with static callback `_CB_COLLECT_USED_TYPES` and memoized `_clean_type_name`, while `get_rel_file_for_cursor` caches resolved repository paths in `_rel_file_cache`.
    - At the completion of `CSExtractor.extract_zone(CS, main_zone)`, `Ast_Manager` and `TokenList` must immediately clear `main_zone`, `tokens_array`, `token_group`, `parsed_tu`, `top_cursors`, delete `translation_unit`, pop `CS.parsers["C_AM"]`, and invoke `clear_tu_caches()`, breaking circular references, preventing cross-file state leakage, and ensuring per-file heap memory drops immediately upon completion of relational staging.
24. **Enum & Enumerator Constant Definition and Usage Tracking**:
    - All enumerator constants (e.g. `FOO_A`, `FOO_B`), regardless of explicit `= value` assignment, are extracted with `type_id = ASTT.C_enumequal` (25) in `m_ast` and registered in `CS.symbol_dict[(name, ASTT.C_enumequal)]`.
    - Enumerator definitions are staged in `m_symbol_def` with `type_id = ASTT.C_enumequal` and exact definition lines, linked to the enclosing enum's top-level code tag (`tag_ref`).
    - Anonymous enum container labels (`(unnamed at ...)` / `(anonymous at ...)`) are suppressed from `m_symbol_def`, indexing only the named enumerators.
    - References to enumerator constants in expressions and initializers are dispatched as `Ast_DeclRefExpr` and recorded in `m_symbol_ref` with `SymbolRole.DeclRef` (5).
    - Foreign enumerator constant references resolve via `REF_FILE` pointing to the declaring header file.
25. **System Call Definition Extraction (`ASTT.C_SyscallDef = 147`)**:
    - System calls defined via `SYSCALL_DEFINE*` or `COMPAT_SYSCALL_DEFINE*` extract the canonical function name (`sys_<name>` / `compat_sys_<name>`) from their first argument child or cursor spelling.
    - Staged into `m_ast` and `m_symbol_def` with `type_id = ASTT.C_SyscallDef` (147), and indexed in `CS.symbol_dict` under both canonical syscall name and macro name.
26. **Kernel Section Attributes & Multiline Macro Delimitation**:
    - Kernel section annotations (`__devinit`, `__devexit`, `__net_init`, `__paginginit`, etc.) are pre-expanded in `_CLANG_KERNEL_MACRO_DEFINES` to prevent syntax parsing aborts.
    - Multiline macro line-continuations (`\\\n`) are spliced during tokenization to prevent trailing punctuation sticking.
27. **Semicolon Child Zone Cleanup & Scope Containment**:
    - Semicolons outside parenthesized/braced expressions immediately close active child argument and initializer zones in `C_Type.within_range`, preventing scope leakage across top-level declarations or inside compound statements.
28. **Macro Scope Termination and Semicolonless Invocation Invariant**:
    - Top-level kernel macros (`_KNOWN_MACRO_FUNCS`, e.g. `module_init`, `*initcall`, `RESERVE_BRK`, `LOOP_ATTR_RO`, `DO_ERROR`, `DO_ERROR_INFO`) that may appear without trailing semicolons must track parameter parenthesis closure (`_macro_paren_opened`, `paren_depth <= 0`). In `C_Type.within_range`, once the argument parenthesis is closed, encountering a semicolon absorbs it into the extent, while encountering non-semicolon tokens immediately terminates the node scope (`need_processing = False; return False`), guaranteeing that trailing functions and declarations are never swallowed into preceding macro invocations.
    - Demoted macros outside `_VAR_DECL_MACROS` and `_TRACE_MACROS` (such as `module_init`, `*initcall`, `EXPORT_SYMBOL`) are excluded from `m_symbol_def` and `CS.symbol_dict` during both function and variable extraction, preventing their arguments from registering as fake symbol definitions.
29. **Kernel Variable Declaration Macro Extraction (`ASTT.C_MacroVarDef = 150`)**:
    - Variables defined via kernel declaration macros (`_VAR_DECL_MACROS`, e.g. `DEFINE_SPINLOCK`, `DEFINE_MUTEX`, `LIST_HEAD`, etc.) must be extracted as variable definitions rather than function declarations.
    - When `self.cursor` lacks child cursors, `C_Type.extract()` must inspect argument child zone `typedata` and `content` tokens for the declared variable identifier.
    - Definitions must be staged in `m_symbol_def` with `type_id = ASTT.C_MacroVarDef` (150) and dual-indexed in `CS.symbol_dict` under `ASTT.C_MacroVarDef` and `ASTT.C_DeclRefExpr`.
30. **Kernel Tracepoint Definition Extraction (`ASTT.C_TracepointDef = 151`)**:
    - Kernel tracepoints defined via macros (`_TRACE_MACROS`, e.g. `TRACE_EVENT`, `DEFINE_EVENT`, `DECLARE_EVENT_CLASS`, `DECLARE_TRACE`, `DEFINE_TRACE`) must be extracted directly via dedicated tracepoint pathway in `C_Type.extract()`.
    - The tracepoint symbol must be extracted from the appropriate argument position (Arg 1 for `DEFINE_EVENT*`, Arg 0 for all other trace macros) via `typedata` or child zones.
    - Definitions must be staged in `m_symbol_def` with `type_id = ASTT.C_TracepointDef` (151) and dual-indexed in `CS.symbol_dict` under `ASTT.C_TracepointDef` and `ASTT.C_DeclRefExpr`.
    - Extraction must terminate before variable declarator extraction to guarantee zero parameter token leakage (`TP_PROTO`, `TP_ARGS`, `skbaddr`, etc.) into `m_symbol_def`.
31. **Kernel Per-CPU Variable Definition Extraction (`ASTT.C_PerCpuVarDef = 152`)**:
    - Per-CPU variables defined via kernel macros (`_PERCPU_MACROS`, e.g. `DEFINE_PER_CPU*`, `DECLARE_PER_CPU*`) must be extracted into `m_symbol_def` with dedicated `type_id = ASTT.C_PerCpuVarDef` (152), separating per-CPU variables from generic locks.
    - Extraction targets argument 1 (the variable name following the type), skipping type tokens, and dual-indexes into `CS.symbol_dict` under `ASTT.C_PerCpuVarDef` and `ASTT.C_DeclRefExpr`.
32. **Kernel Module & Boot Parameter Definition Extraction (`ASTT.C_ModuleParamDef = 153`)**:
    - Driver and boot parameters defined via macros (`_MODULE_PARAM_MACROS`, e.g. `module_param*`, `core_param`, `early_param`, `__setup`) must be extracted directly via dedicated parameter pathway in `C_Type.extract()`.
    - Parameter name identifier must be isolated from Arg 0 (`typedata` or child tokens), while tag storage is wrapped in `with CS(REF_NO_REF):` to prevent route collisions on the root route `()`.
    - Staged into `m_symbol_def` with `type_id = ASTT.C_ModuleParamDef` (153) and dual-indexed in `CS.symbol_dict` under `ASTT.C_ModuleParamDef` and `ASTT.C_DeclRefExpr`.
33. **Assembly Entry Point Extraction (`ASTT.C_AsmEntry = 148`)**:
    - Global assembly entry points, traps, and handler macros (`_ASM_ENTRY_MACROS`, e.g. `ENTRY`, `GLOBAL`, `DO_ERROR`, `DO_ERROR_INFO`) must be extracted directly via dedicated assembly entry pathway in `C_Type.extract()`.
    - Symbol identifier is extracted from the appropriate argument position (Arg 3 for `DO_ERROR*`, Arg 0 for `ENTRY`/`GLOBAL`), tag storage is wrapped in `with CS(REF_NO_REF):`, and staged into `m_symbol_def` with `type_id = ASTT.C_AsmEntry` (148) and dual-indexed in `CS.symbol_dict`.
34. **Parameterized Macro Argument Extraction (`m_ast_container`)**:
    - Parameterized macros (`#define MACRO(a, b, c) ...`) parse parameters into individual child AST nodes (`ASTT.C_MacroParam = 177`).
    - The macro definition stages into `m_ast` with joined `m_ast_container` rows (`((m_ast.ast_id, m_ast_container.ast_id, len(param_refs)),)`) at priorities 0..N-1 using `m_ast.view(...)`, strictly adhering to view deduplication and SHA-256 AST hashing without direct `.set()` invocations.
    - Preprocessor operators and directives `#embed` (`ASTT.CPPro_embed = 165`) and `defined` (`ASTT.CPPro_defined = 166`) are captured as distinct preprocessor AST constructs.
35. **Device Table & Sysfs Attribute Extraction (`ASTT.C_DeviceTableDef = 175`, `ASTT.C_SysfsAttrDef = 176`)**:
    - Driver device match tables (`_DEVICE_TABLE_MACROS`, e.g. `MODULE_DEVICE_TABLE(bus, table)`) are extracted into `m_symbol_def` with `type_id = ASTT.C_DeviceTableDef` (175) and dual-indexed in `CS.symbol_dict`.
    - Sysfs attribute declarations (`_SYSFS_ATTR_MACROS`, e.g. `DEVICE_ATTR_*`, `SYSFS_ATTR_*`, `CLASS_ATTR_*`, `BUS_ATTR_*`, `DRIVER_ATTR_*`, `__ATTR*`) are extracted into `m_symbol_def` with `type_id = ASTT.C_SysfsAttrDef` (176) and dual-indexed in `CS.symbol_dict`.

---

## 12. Unit Testing & Snapshot Verification Contract

The C & Preprocessor AST parser is verified through the reusable snapshot testing harness:

- **Harness Module**: `unit_test/parser_harness.py`
  - `stage_file_prelude(CS, gp)`: Reusable file lifecycle prelude (`m_file_name`, `m_file`, `m_bridge_file`, `m_moved_file`) matching `main.py default_processing()`. Detects file language category (`T_C = 1` for `.c`/`.h`, `T_ASM = 4` for `.S`).
  - `extract_changeset_snapshot(CS | [CS, ...], test_conf=...)`: Extracts all executed operations from single or multi-version ChangeSets (`[cs_v1, cs_v2]`). Rows within each table preserve strict operational insertion order. Surrogate monotonic IDs (`tag_id`, `ast_id`, `fid`, `fnid`, `map_id`, `def_id`, `ref_id`) are canonicalized into symbolic identifiers (`$tag_0`, `$ast_0`, `$fid_0`, `$def_0`, `$ref_0`) sequentially based on first-appearance order, rendering snapshots deterministic while preserving concrete `vid` sequences. Updates on primary keys (`OP_UPDATE`) update existing rows in place. Table rows preserve exact database column definitions (`table.init_columns`), `type_id` values are formatted as `<id> (<name>)` (e.g., `"21 (C_functionprotodecl)"`), `ftype` values as `<id> (<name>)` (e.g., `"1 (T_C)"`), and `role` values as `<id> (<name>)` (e.g., `"2 (TypeUsage)"`).
  - `assert_snapshot_matches(test_case, parser_name, test_name, snapshot)`: Verifies extracted ChangeSet tables and `test_conf` metadata against verified JSON baselines. Set `UPDATE_SNAPSHOTS=1` in the environment to re-generate baselines after human verification.
- **Test Suites**: `unit_test/test_c_parser.py`
  - **Lifecycle Suite (`Test_CParserLifecycle`)**:
    - `test_01_added_c_file`: Added (`A`) file parsing of `include/linux/lockd/bind.h` on `v3.0`, verifying creation of `m_v_main`, file prelude, `m_ast`, `m_tag`, `m_tag_code`, `m_bridge_tag`, `m_map_ast`, `m_bridge_map`, `m_symbol_def`, and `m_symbol_ref`.
    - `test_02_modified_changed_c_file`: Modified (`M`) file parsing across `v2.6.28` -> `v2.6.29` (kernel commit `0cb2659b` adding `noresvport` to `struct nlmclnt_initdata`), validating evolutionary links via `m_moved_tag: 1`, recycling of unchanged tags, retention of both struct tag codes, and prior struct tag closure (`vid_e = 1`).
    - `test_03_exact_rename_c_file`: Exact rename (`R100`) file parsing across `v2.6.39` -> `v3.0`, verifying `m_v_main` progression, old `fid` reuse, dual `m_bridge_file` links, and zero duplicate AST tags.
  - **Corner Cases Suite (`Test_CParserCornerCases`)**:
    - `test_04_syscall_file`: System call parsing (`SYSCALL_DEFINE1`) in `kernel/exec_domain.c` on `v3.0`, validating `ASTT.C_SyscallDef` (147) for `sys_personality`, `ASTT.C_MacroVarDef` (150) for `exec_domains_lock`, and clean exclusion of demoted `module_init` and `EXPORT_SYMBOL` from `m_symbol_def`.
    - `test_05_macro_variable_file`: Macro variable declarations (`DEFINE_SPINLOCK`) in `kernel/dma.c` on `v3.0`, validating `ASTT.C_MacroVarDef` (150) for `dma_spin_lock`, `ASTT.C_structdecl` (28) for `dma_chan`, designated struct initializers, and demotion of `__initcall` and `EXPORT_SYMBOL`.
    - `test_06_tracepoint_file`: Kernel tracepoint definitions in `include/trace/events/net.h` on `v3.0`, validating `ASTT.C_TracepointDef` (151) across `TRACE_EVENT` (`net_dev_xmit`), `DECLARE_EVENT_CLASS` (`net_dev_template`), and `DEFINE_EVENT` (`net_dev_queue`, `netif_receive_skb`, `netif_rx`) with semicolonless scoping and zero parameter leakage.
    - `test_07_assembly_file`: Low-level assembly source parsing (`.S`) in `arch/alpha/lib/clear_page.S` on `v3.0`, validating `ftype = 4 (T_ASM)`, assembly directives, instructions, labels, and zero symbol hallucination in `m_symbol_def`.
- **Snapshot Storage**: `unit_test/snapshots/c_ast/*.json` containing `test_conf` metadata and canonical `tables` mapping.



