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
  │     └── Queries m_bridge_tag + m_tag -> Inverted hash, name, pos, anchor, & symbol indices
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
  │     │     ├── 1. m_tag.set(None, VID, 0, code_hash, ast_ref, 0, 0)  [MUST BE FIRST]
  │     │     │      tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
  │     │     ├── 2. m_tag_code.get_set(code_hash, code_str)             [Auxiliary hash table]
  │     │     └── 3. m_moved_tag.set(s_tag_id, tag_ref)                  [If evolved tag match]
  │     │
  │     ├── File-to-Tag Bridge: m_bridge_tag.set(fid, tag_ref, line_s, line_e, char_s, char_e)
  │     ├── Spatial AST Map: m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref)
  │     │     └── (rel_line_e and rel_char_e computed against self.endif for preprocessor branches)
  │     ├── Bridge Map Link: m_bridge_map.set(tag_ref, tag_ref) (deduped via register_bridge_map)
  │     └── Debug serialization: m_ast_debug.set (when G.OVERRIDE_FORCE_AST_DEBUG is True)
  │
  └── Tag Lifecycle Finalization: close_prior_tags(CS) (tag_tracker.py)
        └── Marks unrecycled prior tags closed: m_tag.update(vid_e = Old_VID) in REF_OLD
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
      "-D__KERNEL__",
      *cppro_cindex_input,
      f"-I{self.mfdir}/{inc_dir}",
      f"-I{self.mfdir}/include",
      f"-I{self.mfdir}/include/uapi",
  ]
  options = cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768
  ```
- Collects profiler metrics: `prof.clang_parse_tu_s`, `prof.clang_tokenize_s`.
- Registers `CS.parsers["C_AM"] = self`.
- Instantiates `TokenList` (subclass of `TokenStream`) and calls `TL.process_tokens(CS)`.

### 2.5 `TokenList`
Compatibility adapter connecting `TokenStream` with `Zone` execution:
- Extracts file-scoped cursors via `top_cursors = get_top_level_cursors(self.parsed_tu, self.fullfilename)`.
- Initializes `self.main_zone = Zone(Zone_Type.Full_File, top_cursors, tokens_array=self.tokens_array)`.
- Sets `G.CURRENT_RAWFILE = self.rawfile`.
- Loops tokens directly over `self.tokens_array` (bypassing `zip()` tuple allocation via pre-associated `token._cursor`):
  - Invokes `self.main_zone.check_exec(token, token._cursor, token.ast_kind)`.
- Invokes `self.main_zone.resolve_cppro_scopes()`.
- Records `prof.token_processing_s`.
- Dispatches `CSExtractor.extract_zone(self.CS, self.main_zone)`.
- Immediate Teardown: Clears `self.main_zone`, `self.tokens_array`, `self.token_group`, `self.parsed_tu`, and pops `CS.parsers["C_AM"]` to immediately release all AST/token memory.

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

_CLANG_GET_TOKEN_KIND = cc.conf.lib.clang_getTokenKind
_CLANG_GET_TOKEN_KIND.argtypes = [cc.Token]
_CLANG_GET_TOKEN_KIND.restype = ctypes.c_uint
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
Bypasses Python object allocation when cursor is already annotated; retrieves coordinates via `_CLANG_GET_SPELLING_LOC` in 2 C-calls passing `None` for file and offset pointers (bypassing Libclang FileID resolution) and caches directly on `cursor._cached_line`.

### 4.3 Extent Token Retrieval: `get_tokens_in_extent(tokens: list[Any], extent: Line) -> list[Any]`
Performs binary search over contiguous token list using `(s_l, s_c)` coordinates to locate the starting token index in $O(\log N)$ time, followed by a linear sweep bounded by `(e_l, e_c)` to return all tokens overlapping the given spatial extent.

### 4.4 Fast Delimiter Encapsulation: `encapsulate_trailing_delimiter`
Expands the target `Line` extent to encapsulate trailing punctuation delimiters (`;`, `,`) on the current line or column <= 2 of the immediate next line.
- **$O(\log N)$ Binary Search Optimization**: Uses `bisect.bisect_left(tokens, e_l, key=_get_tok_start_line)` to directly jump to the first token starting at or after the extent end line `e_l`.
- Inspects at most 1 to 3 trailing tokens on line `e_l` or `e_l + 1`, eliminating $O(N)$ linear scans from index 0 across thousands of translation unit tokens.

### 4.5 `TokenStream` Architecture
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
- Contains child collections: `zones: list[Zone]`, `operands: list[str]`, `call_exprs: list[Ast_CallExpr]`, `member_refs: list[Ast_MemberRefExpr]`, `decl_refs: list[Ast_DeclRefExpr]`.
- Attributes: `self.type_id: int` (initialized from `self.__class__.type_id`), `self.ast_ref: Any = None`.
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
- **Expression Classes (`Ast_CallExpr`, `Ast_MemberRefExpr`, `Ast_DeclRefExpr`)**:
  - `Ast_CallExpr`: Function invocation `callee(...)`. If `ref_ast_id != 0`, stages joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, 1),), None, name, ASTT.C_CallExpr, None, 0, t_id, ref_ast_id)`.
  - `Ast_MemberRefExpr`: Field access `x.y` / `x->y`. Links member identifier to parent record container.
  - `Ast_DeclRefExpr`: Variable or parameter reference. Links identifier to declaration reference.

### 5.5 Comments, Assembly & Macros (`ast_nodes.py`)
| Class | `ASTT` Type Constant | Symbolic Value | Stored Payload | Description |
| :--- | :--- | :---: | :--- | :--- |
| `Ast_Comment` | `ASTT.C_Comment` | 2 | `name[:255]` | C comment (`//...` or `/*...*/`) truncated to 255 chars. |
| `Ast_ASM_Comment` | `ASTT.ASM_Comment` | 87 | `extent.code[:255]` | Assembly comment or preprocessor comment line. |
| `Ast_ASM_Directive`| `ASTT.ASM_Directive` | 84 | `payload[:255]` | Assembly directive (`.section`, `.align`, `.globl`). |
| `Ast_ASM_Macro` | `ASTT.ASM_Macro` | 83 | `name[:255]` | Assembly macro definition (`.macro ... .endm`). |
| `Ast_ASM_Instruction`| `ASTT.ASM_Instruction`| 85 | `mnemonic[:255]` | Assembly instruction mnemonic and operands. |
| `Ast_ASM_Label` | `ASTT.ASM_Label` | 86 | `label[:255]` | Assembly jump or symbol label. |
| `Ast_Keyword` | `ASTT.C_Keyword` | 3 | None | Statement keyword placeholder. |
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
| `CPPro_include` | `ASTT.CPPro_include`| 78 | Resolves include target via `cursor.get_included_file()`. Stages `m_file_name.get_set` followed by multi-table view `m_ast.view(((m_ast.ast_id, m_ast_include.ast_id, 1),), None, w_include[:255], ASTT.CPPro_include, None, CS.ref(m_file_name.fnid, *fnid_route))`. |
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
    - Unbound forward struct/union/enum/proto (e.g. `struct foo *`): emits `m_ast.view(((m_ast.ast_id,),), None, "foo", notbind_type)` in `REF_NO_REF` and sets `ref_type = TSRef.Route_Ref`.
    - Qualified or compound types: constructs joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, count),), None, "", ASTT.C_Compound, ...)` mapping all qualifier and type tokens into `m_ast_container`.

### 6.3 Forward Declaration Resolution: `get_notbind_type(ast_type: int) -> int` (`ast_nodes.py`)
Maps bound declaration type constants to unbound forward reference constants:
- `ASTT.C_struct` / `ASTT.C_structdecl` &rarr; `ASTT.C_structnotbind` (29)
- `ASTT.C_union` / `ASTT.C_uniondecl` &rarr; `ASTT.C_unionnotbind` (35)
- `ASTT.C_enum` / `ASTT.C_enumdecl` &rarr; `ASTT.C_enumnotbind` (32)
- `ASTT.C_functionproto` / `ASTT.C_functionprotodecl` &rarr; `ASTT.C_functionprotnotbind` (22)

### 6.4 Declaration Type Resolution: `get_decl_type(ast_type: int) -> int` (`cursor_tree.py`)
Maps base construct type constants to definition declaration type constants:
- `ASTT.C_struct` &rarr; `ASTT.C_structdecl` (28)
- `ASTT.C_union` &rarr; `ASTT.C_uniondecl` (34)
- `ASTT.C_enum` &rarr; `ASTT.C_enumdecl` (31)
- `ASTT.C_functionproto` &rarr; `ASTT.C_functionprotodecl` (21)

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
- **Spatial Indexing & Scope Linking**:
  - `gen_lined_dict()`: Indexes child nodes by start line into `self.A_Line_Dict: dict[int, list[Ast]]`.
  - `resolve_cppro_scopes()`: Links preprocessor conditional branches to terminating `#endif`.
- **Extraction Protocol (`Zone.extract(CS)`)**:
  - Suppresses dummy parameter container creation for explicit empty parameter lists `(void)` when parameter type is `C_void` and has no identifier name.
  - For `C_Type` child nodes, executes `item.extract(CS)` directly (allowing `C_Type` to stage internal `REF_POS`/`REF_NO_REF`/`REF_MULTI` routes).
  - For all other child nodes (`Ast_Statement`, `CPPro`, comments, assembly), wraps execution in `with CS(REF_NO_REF): item.extract(CS)`.
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

### 8.3 Obsolete Tag Closure (`close_prior_tags`)
Iterates prior tags from previous version; any tag index not present in `CS.active_tag_list` is marked closed by emitting:
```python
with CS(REF_OLD):
    for x, tag in enumerate(CS.prior_tags):
        if x in active_set:
            continue
        if len(tag) >= 13:
            with CS(REF_POS):
                CS.store(m_tag.update(
                    tag[6],      # m_tag.tag_id
                    tag[7],      # m_tag.vid_s
                    old_vid,     # m_tag.vid_e (marked closed)
                    tag[9],      # m_tag.hash
                    tag[10],     # m_tag.ast_id
                    tag[11],     # m_tag.hl_s
                    tag[12],     # m_tag.hl_l
                ))
```

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
    CS.store(m_tag.set(None, CS.gp.VID, 0, code_hash, ast_ref, 0, 0))
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

## 10. Complete `ASTT` Type System & Relational Database Mappings

### 10.1 Authoritative `ASTT` Enumeration Mapping (`core/globalstuff.py`)
`ASTT` inherits from `IntEnum` and is populated sequentially via `auto()`, starting from `Undefined = 0`.

> [!IMPORTANT]
> **Code Invariant**: Code and tests must ALWAYS reference symbolic constants (e.g. `ASTT.C_struct`, `ASTT.CPPro_if`, `ASTT.ASM_Directive`) rather than hardcoded integer literals.

| `ASTT` Category | Constants, Identifiers & Exact Integer Values |
| :--- | :--- |
| **Primitives** | `C_void (36)`, `C_unsigned (37)`, `C_signed (38)`, `C_char (39)`, `C_short (40)`, `C_int (41)`, `C_long (42)`, `C_bool (43)`, `C_float (44)`, `C_double (45)`, `C_pointer (26)`, `C_array (23)`, `C_arrayempty (24)` |
| **Qualifiers** | `C_Qconst (16)`, `C_Qvolatile (17)`, `C_Qrestrict (18)`, `C_Q_Atomic (19)` |
| **Declarations** | `C_functionproto (20)`, `C_functionprotodecl (21)`, `C_functionprotnotbind (22)`<br>`C_struct (27)`, `C_structdecl (28)`, `C_structnotbind (29)`<br>`C_enum (30)`, `C_enumdecl (31)`, `C_enumnotbind (32)`, `C_enumequal (25)`<br>`C_union (33)`, `C_uniondecl (34)`, `C_unionnotbind (35)` |
| **Storage Class** | `C_SCauto (4)`, `C_SCregister (5)`, `C_SCstatic (6)`, `C_SCextern (7)`, `C_SC_Thread_local (8)`, `C_SCthread_local (9)`, `C_SCtypedef (10)`, `C_SCconstexpr (11)` |
| **Function Spec** | `C_FSinline (12)`, `C_FS_Noreturn (13)` |
| **Align / Misc** | `C_AS__Alignas (14)`, `C_AS_alignas (15)`, `C_Compound (1)`, `C_Comment (2)`, `C_Keyword (3)` |
| **Statements** | `C_CompoundStmt (46)`, `C_IfStmt (47)`, `C_SwitchStmt (48)`, `C_CaseStmt (49)`, `C_DefaultStmt (50)`, `C_WhileStmt (51)`, `C_DoStmt (52)`, `C_ForStmt (53)`, `C_ReturnStmt (54)`, `C_BreakStmt (55)`, `C_ContinueStmt (56)`, `C_GotoStmt (57)`, `C_LabelStmt (58)`, `C_AsmStmt (59)` |
| **Expressions** | `C_CallExpr (60)`, `C_MemberRefExpr (61)`, `C_DeclRefExpr (62)`, `C_BinaryOperator (63)`, `C_UnaryOperator (64)`, `C_ParenExpr (65)`, `C_InitListExpr (66)`, `C_SizeofExpr (88)`, `C_TypeRef (89)` |
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
| m_v_main            |    0     | (vid, vname)                                                       |
| m_file_name         |    1     | (fnid, fname)                                                      |
| m_file              |    2     | (fid, vid_s, vid_e, ftype, s_stat, e_stat)                         |
| m_bridge_file       |    3     | (vid, fnid, fid)                                                   |
| m_type_descriptor   |    5     | (type_id, name)                                                    |
| m_ast               |    6     | (ast_id, name, type_id)                                            |
| m_ast_container     |    7     | (ast_id, priority, type_id, ref_ast_id)                            |
| m_ast_include       |    8     | (ast_id, fnid)                                                     |
| m_ast_debug         |    9     | (ast_id, ast_raw)                                                  |
| m_tag_code          |   10     | (hash, code)                                                       |
| m_tag               |   11     | (tag_id, vid_s, vid_e, hash, ast_id, hl_s, hl_l)                  |
| m_bridge_tag        |   12     | (fid, tag_id, line_s, line_e, char_s, char_e)                      |
| m_map_ast           |   13     | (map_id, line_s, char_s, line_e, char_e, ast_id)                   |
| m_bridge_map        |   14     | (tag_id, map_id)                                                   |
| m_moved_tag         |   30     | (s_tag_id, e_tag_id)                                               |
+----------------------------------------------------------------------------------------------------+
```

---

## 11. Critical Engine Invariants & AI Development Guidelines

1. **Relative File Paths (Rule 11)**: All file links and documentation references must use relative paths from git root (e.g. `parser/c_ast/cursor_tree.py`). Never use absolute machine file URIs.
2. **Tag Reference Order Invariant (Rule 12)**: Inside `with CS(REF_POS):`, `m_tag.set` MUST be the first staged operation so `tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))` points directly to `m_tag`. Auxiliary tables (`m_tag_code.get_set`, `m_moved_tag.set`) must follow `m_tag.set`.
3. **Cryptographic Hashing Standard (Rule 14)**: Code snippets must be hashed using `compute_code_hash(code)` from `core.globalstuff`, returning a 32-byte binary SHA-256 digest (`BINARY(32)`).
4. **TableEngine Agnosticism (Rule 15)**: Tests and workflows must never access private engine internals (e.g. `_cached_rows`, `_pk_index`). Queries must use public TableEngine APIs (`Table.get()`, `G.TE.get()`) or assert against database state (`MockDB._global_store`).
5. **Form Feed & Line Splitting**: Never use `splitlines()` on source text. Use `raw_content.replace("\r\n", "\n").split("\n")` to preserve 1:1 line index parity with Libclang across files containing form-feed characters (`\x0c`).
6. **Delimiter Encapsulation & 100.00% Coverage**: Semicolons (`;`), closing braces (`}`), and macro continuation backslashes (`\`) must be encapsulated within AST node extents to ensure 100.00% character coverage and zero orphan characters.
7. **Bridge Map Deduplication**: Always register tag bridge map links via `CS.register_bridge_map(tag_ref, tag_ref)` before emitting `m_bridge_map.set` to prevent duplicate primary key crashes in MariaDB.
8. **Schema String Bounds**: Every string written to `m_ast.name` must be bounded to 255 characters (`safe_name = str(name)[:255]`) to satisfy database column constraints.
9. **Import Order Guard (Rule 9)**: Always ensure `core` modules (`core.globalstuff`, `core.DBLayout`) are imported before `parser.c_ast` components to prevent circular import deadlocks.
10. **Exact Rename Optimization**: When `CS.file_operation == "R100"`, `c_ast_parse` must return immediately without emitting records.
11. **Testing Verification (Rule 8)**: Always validate changes using `python3 -m unittest tests/test_c_ast.py` and run `python3 main.py -u` once.
12. **Cursor Extent Boundary Partitioning (Rule 18)**: Top-level translation unit declarations and struct members must be bounded by their Clang cursor extents (`End_Mode.Extent`). Trailing delimiters (`;`, `}`) must be encapsulated via `_PUNCT_IGNORED` delimiter growth (`child.extent.grow(tline)`) rather than unbounded semicolon seeking (`End_Mode.Auto`), preventing declaration swallowing while guaranteeing 100.00% character code coverage.
13. **Struct Function Pointer Member Isolation (Rule 19)**: Function pointer fields within structs/unions (`void (*fn)(params)`) must scope their parameter list in `Zone(Zone_Type.Function_Args, arg_children)` attached to child `PARM_DECL` cursors, clear `self.name` upon pointer indirection (`*`), and extract the member declarator into `m_ast_container` pointing to the parent struct, ensuring parameters do not overwrite or duplicate over the parent field tag extent.
14. **Function Pointer Declarator Isolation (Rule 20)**: Function pointer declarators (e.g. `typedef void (*nlm_host_match_fn_t)(struct nlm_host *host)` or struct function pointer members) contain an inner pointer grouping `(*<name>)` preceding the parameter list `(<args>)`. Token dispatch must treat `(` as the pointer declarator grouping whenever `C_functionproto` has not yet been registered on the node, ensuring `Zone(Zone_Type.Function_Args)` is only spawned for the actual argument list, preventing phantom `*<name>` declarator tags and guaranteeing all parameter tags are correctly extracted and linked to the parent symbol at Container Level 1 in `m_ast_container`.
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
    - `m_symbol_def` (Table 31): Staged during definition extraction for functions (`ASTT.C_functionproto`), structs (`ASTT.C_struct`, `ASTT.C_structdecl`), unions, enums, assembly labels/macros, and jump labels. Records `(def_id, vid, fid, tag_id, ast_id, name, type_id, line_s, line_e)`.
    - `m_symbol_ref` (Table 32): Staged for every occurrence of a symbol: declarations (`SymbolRole.Declaration = 1`), type usages (`SymbolRole.TypeUsage = 2`), function calls (`SymbolRole.Call = 3`), struct member references (`SymbolRole.MemberRef = 4`), and identifier references (`SymbolRole.DeclRef = 5`). Records `(ref_id, vid, fid, tag_id, ast_id, role, line, char_s)`.
    - Co-declared structs and variables (e.g. `struct foo { int a; } my_var;`): The parser isolates the struct extent up through the closing brace `}` of `Declared_Args`, stages `m_symbol_def` for the canonical struct type, and then extracts the declared variable extent linking its container Priority 0 to the struct's `ast_id` and staging an `m_symbol_ref` with `SymbolRole.TypeUsage`.
23. **Slot-Optimized Token, Deduplicated Streaming & Immediate Post-Extraction Teardown Invariant**:
    - Tokens streamed through `TokenStream` must use `ParsedToken` with `__slots__ = ("line", "spelling_str", "ast_kind", "_cursor")` to eliminate Python instance `__dict__` overhead on ctypes objects.
    - Cursors must inherit translation unit reference via class-level `cc.Cursor._tu = parsed_tu` rather than per-instance dictionary assignment.
    - Consecutive tokens residing on the same line must reuse `(s_line, e_line)` tuples, eliminating tens of thousands of redundant tuple allocations.
    - Consecutive tokens sharing identical underlying ctypes cursor struct fields (`_kind_id`, `xdata`, `data[0..2]`) must reuse the preceding `Cursor` instance, cutting Python cursor allocations by >50%.
    - Identifier token dispatch in `Ast_Statement.exec_identifier` and `AST_Initializer.exec_identifier` must fast-path known expressions (`CALL_EXPR`, `MEMBER_REF_EXPR`) and filter out macros/directives (`_SKIP_REF_KINDS`), only lazily evaluating `cursor.referenced` when resolving declaration targets to eliminate tens of thousands of Libclang internal Python wrapper/dictionary allocations.
    - Dead data structures such as `A_Line_Dict` must be bypassed.
    - At the completion of `CSExtractor.extract_zone(CS, main_zone)`, `Ast_Manager` and `TokenList` must immediately clear `main_zone`, `tokens_array`, `token_group`, `parsed_tu`, and pop `CS.parsers["C_AM"]`, ensuring per-file heap memory drops immediately to ~2 MB upon completion of relational staging.

