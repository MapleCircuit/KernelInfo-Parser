# C AST Subsystem API & Architectural Contract Specification

Authoritative, dense architectural specification and technical contract across `parser/c_ast/c_ast.py` and `parser/c_ast/c_ast_type.py`. Designed as an exhaustive reference for AI agents extending or upgrading the C & Assembly parsing engine.

---

## 1. High-Level Architecture & Parsing Pipeline

The C AST parser transforms C source files (`.c`, `.h`) and Assembly source files (`.S`, `.h`) into relational database operations staged in a `ChangeSet` (`CS`).

```
==================================================================================================
STAGE 1: INTERMEDIATE TREE GENERATION (libclang + ctypes + TokenList + Zone / AST Tree)
--------------------------------------------------------------------------------------------------
C Source Code (.c / .h / .S)
  │
  ▼ [Ast_Manager.Init_Parse]
clang.cindex (cc.Index -> cc.TranslationUnit)
  │ (with -D__KERNEL__, kernel include paths, PARSE_DETAILED_PROCESSING_RECORD)
  ▼ [TokenList]
Low-level ctypes Tokenization & Bulk Annotation:
  ├── clang_tokenize() -> tokens_memory (C array of cc.Token)
  ├── clang_annotateTokens() -> temp_cursors_array (C array of cc.Cursor)
  └── Fast ctypes Line/Col extraction via clang_getSpellingLocation (cached in Line objects)
  │
  ▼ [TokenList.process_tokens]
Zone(Zone_Type.Full_File).check_exec(token, cursor, ast_kind)
  ├── Dispatch to active child AST nodes (within_range / exec_filter)
  ├── Spatial scope boundaries: Zone (Function_Args, Declared_Args, Compound_Stmt, etc.)
  ├── Type and declarator parsing: C_Type (TypeSegment, TypeToken, CQual)
  ├── Preprocessor directives: CPPro -> CPPro_if/ifdef/ifndef/define/include/...
  └── Assembly directives/instructions: Ast_ASM_Directive, Ast_ASM_Macro, Ast_ASM_Instruction
  │
  ▼ Post-Processing Passes:
  ├── Zone.gen_lined_dict() (Index children by start line)
  └── Zone.resolve_cppro_scopes() (Resolve branch boundaries & attach endif Line coordinates)
==================================================================================================
STAGE 2: RELATIONAL CHANGESET EXTRACTION (Zone / Ast / C_Type -> ChangeSet Database Operations)
--------------------------------------------------------------------------------------------------
Zone.extract(CS)
  │
  ├── C_Type.extract(CS):
  │     ├── Zone recursion in CS(REF_MULTI)
  │     ├── Multi-declarator splitting (root_type + declarators -> final_types)
  │     ├── Type definition views via m_ast.ref_view / m_ast.view (C_structdecl, C_uniondecl, etc.)
  │     └── Variable declaration views via m_ast.view (ASTT.C_Compound, type_id, ref_ast_id)
  │
  ├── CPPro_include.extract(CS):
  │     └── Resolves include path -> m_file_name.get_set -> m_ast.view + m_ast_include.set
  │
  ├── CPPro_* / Ast_ASM_* / Ast_Comment:
  │     └── extract_1arg(CS, type_id, name, extent)
  │
  └── AST Tagging & Spatial Coordinate Mapping:
        ├── Prior tag recycling or new tag creation via m_tag.set / get_set
        ├── File-to-tag bridge coordinate link via m_bridge_tag.set(fid, tag_id, line_s, line_e, ...)
        ├── Spatial AST mapping via m_map_ast.set(tag_id, line_s, char_s, line_e, char_e, ast_id)
        ├── Bridge map link via m_bridge_map.set(tag_id, tag_id)
        └── Debug serialization via m_ast_debug.set (when G.OVERRIDE_FORCE_AST_DEBUG is True)
==================================================================================================
```

---

## 2. File Lifecycle Dispatch & Tag Tracking (`c_ast.py`)

### 2.1. `c_ast_parse(CS: ChangeSetType) -> None`
Dispatches parsing workflow inside `with CS(REF_C_AST):` according to Git diff status `CS.file_operation`:
- **`"R100"`** (Exact rename): No-op. File lifecycle and AST tags are preserved automatically.
- **`"A"`** (Added file): Calls `process_c_ast(CS)`.
- **`"M"` / `"R*"`** (Modified / Partial Rename): Executes `get_prior_tags(CS)` &rarr; `process_c_ast(CS)` &rarr; `close_prior_tags(CS)`.
- **`"D"`** (Deleted file): Executes `get_prior_tags(CS)` &rarr; `close_prior_tags(CS)`.

### 2.2. Tag Version Lifecycle Management
- **`get_prior_tags(CS: ChangeSetType) -> None`**:
  1. Reads `old_vid = getattr(CS.gp, "Old_VID", 0)`. Exits if `old_vid <= 0`.
  2. Resolves filename: uses `CS.old_path` if rename, otherwise `CS.current_path`.
  3. Queries `m_file_name` for `fnid`, then `m_bridge_file` for `(old_vid, fnid)` &rarr; retrieves `old_fid`.
  4. Queries prior tags via `m_bridge_tag.view_get_multiple(((m_bridge_tag.tag_id, m_tag.tag_id, 1),), old_fid, ...)`.
  5. Builds index `CS.prior_tags_map = {code: [(idx, tag_id), ...]}` and initializes `CS.active_tag_list = set()`.
- **`close_prior_tags(CS: ChangeSetType) -> None`**:
  - Iterates prior tags in `CS.prior_tags`.
  - For any tag index `x` not in `CS.active_tag_list`, emits `m_tag.update(tag_id, vid_s, CS.gp.Old_VID, code, ast_id, hl_s, hl_l)` inside `with CS(REF_OLD):` to mark the tag closed as of the previous version.

---

## 3. Libclang Clang Ctypes Fast-Path & `TokenList` Subsystem

### 3.1. Direct Ctypes Function Handles
To bypass Python-C API boundary overhead in `clang.cindex`, direct ctypes foreign function pointers are bound:
- `_CLANG_GET_EXTENT = cc.conf.lib.clang_getTokenExtent`
- `_CLANG_GET_CURSOR_EXTENT = cc.conf.lib.clang_getCursorExtent` (`argtypes=[cc.Cursor]`, `restype=cc.SourceRange`)
- `_CLANG_GET_RANGE_START = cc.conf.lib.clang_getRangeStart` (`argtypes=[cc.SourceRange]`, `restype=cc.SourceLocation`)
- `_CLANG_GET_RANGE_END = cc.conf.lib.clang_getRangeEnd` (`argtypes=[cc.SourceRange]`, `restype=cc.SourceLocation`)
- `_CLANG_GET_SPELLING_LOC = cc.conf.lib.clang_getSpellingLocation`
- `_CLANG_GET_TOKEN_KIND = cc.conf.lib.clang_getTokenKind` (`argtypes=[cc.Token]`, `restype=ctypes.c_uint`)

Pre-allocated ctypes buffers (`_BYREF_F_PTR`, `_BYREF_S_LINE`, `_BYREF_S_COL`, `_BYREF_S_OFF`, `_BYREF_E_LINE`, `_BYREF_E_COL`, `_BYREF_E_OFF`) allow coordinate extraction in **2 C-calls** instead of 5+.

### 3.2. Coordinate Fast-Path Helper: `get_cursor_line(cursor) -> Line`
Extracts 1-indexed `line_pos` `(start_line, end_line)` and `char_pos` `(start_col, end_col)` via `_CLANG_GET_SPELLING_LOC` and caches directly on `cursor._cached_line`.

### 3.3. `TokenList` Architecture
- **`__init__(parsed_tu, fullfilename, rawfile)`**:
  1. Computes file extent `cc.SourceRange(1:1, filesize_offset)`.
  2. Calls `clang_tokenize` to populate contiguous C array `tokens_memory`.
  3. Pre-allocates `temp_cursors_array = (cc.Cursor * count)()` and annotates in a single call via `clang_annotateTokens`.
  4. Iterates tokens:
     - Extracts coordinates into lightweight `Line` instance `token.line`.
     - Assigns `token.spelling_str`: direct slice from latin-1 `rawfile[line - 1]` for single-line tokens, fallback to `token.spelling`.
     - Maps `token.ast_kind = _CLANG_TOKEN_KIND_MAP[_CLANG_GET_TOKEN_KIND(token)]` (`AST_KIND`).
  5. Retains underlying token memory via `self.token_group = cc.TokenGroup(parsed_tu, tokens_memory, tokens_count)`.
- **`process_tokens(CS)`**:
  1. Instantiates root `self.main_zone = Zone(Zone_Type.Full_File, None)`.
  2. Iterates zipped `(tokens_array, cursors_array)`, calling `main_zone.check_exec(token, cursor, token.ast_kind)`.
  3. Executes `main_zone.gen_lined_dict()` and `main_zone.resolve_cppro_scopes()`.
  4. Dispatches `main_zone.extract(CS)`.

### 3.4. `Ast_Manager`
- Reads file content with `encoding="latin-1"`.
- Reuses worker-global `_WORKER_CLANG_INDEX = cc.Index.create()`.
- Parses translation unit with kernel arguments: `"-ferror-limit=0"`, `"-w"`, `"-D__KERNEL__"`, `"-I{mfdir}/include"`, `"-I{mfdir}/include/uapi"`, `"-I{mfdir}/{file_dir}"`, with options `cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768`.

---

## 4. Coordinate & Spatial Model (`Line`)

### 4.1. `Line` Class Contract
Slot-optimized spatial coordinate carrier (`__slots__ = ("line_pos", "char_pos", "code")`):
- `line_pos: tuple[int, int]`: 1-based start line and end line.
- `char_pos: tuple[int, int]`: 1-based start column and end column.
- `code: str`: Extracted raw source code string.

### 4.2. Core Methods
| Method | Signature | Description |
| :--- | :--- | :--- |
| `__init__` | `(arg0=0, arg1=0, arg2=0, arg3=0)` | Polymorphic constructor accepting ints `(s_l, e_l, s_c, e_c)`, `Line`, `cc.SourceRange`, or objects with `.line`. |
| `cc` | `(rawfile: tuple[str]) -> Self` | Slices and populates `self.code` spanning single- or multi-line bounds. |
| `new_end` | `(*args)` | Updates ending line/column coordinates to match target `Line` or `cc.SourceRange`. |
| `new_end_reversed`| `(*args)` | Updates ending coordinates using the **start** position of target range. |
| `grow` | `(*args)` | Expands the bounding box to enclose the target `Line` / `cc.SourceRange`. |
| `is_inside` | `(extent) -> bool` | Returns `True` if `extent` is fully contained within `self` boundaries. |
| `__eq__` | `(other: object) -> bool` | Equality check on `(line_pos, char_pos)`. |

---

## 5. Intermediate AST Node Hierarchy (`Ast`)

### 5.1. Base `Ast` Class Interface
Root class for all intermediate tree elements:
- **Attributes**: `extent: Line`, `need_processing: bool`, `end_mode: End_Mode`.
- **Methods**:
  - `within_range(token, ast_kind) -> bool`: Evaluates boundary enclosure against `End_Mode`:
    - `End_Mode.No_Check (0)`: Always grows extent and returns `True`.
    - `End_Mode.Auto (1)` / `End_Mode.Semicolon (2)`: Accepts tokens until `;` punctuation is encountered.
    - `End_Mode.Comma (3)`: Accepts tokens until `,` punctuation is encountered.
    - `End_Mode.Extent (4)`: Accepts tokens while inside `self.extent`.
  - `exec_filter(token, cursor, kind)`: Dispatches to `exec_comment`, `exec_punctuation`, `exec_keyword`, `exec_identifier`, or `exec_literal`.
  - `extract_1arg(CS, type_id: int, name: int | str, extent: Line | None = None)`:
    - Emits `m_ast.view(((m_ast.ast_id,),), None, name, type_id)` inside `with CS(REF_POS):`.
    - Stores debug entry in `m_ast_debug` (if forced) and invokes `self.tag(CS, ast_id_route, extent)` inside `with CS(REF_NO_REF):`.
  - `tag(CS, ast_id_route, extent)`:
    - Extracts `extent.code` from `rawfile`.
    - Recycles matching tag from `CS.prior_tags` / `CS.prior_tags_map` if found: adds to `CS.active_tag_list` and emits `m_bridge_tag.set(fid_ref, tag_id, line_s, line_e, char_s, char_e)`.
    - If new: creates `m_tag.set(None, VID, 0, code, ast_ref, 0, 0)` in `REF_POS`, creates `m_bridge_tag`, and invokes `self.map_ast()`.
  - `map_ast(CS, ast_id_route, tag_route, extent)`:
    - Calculates 1-based relative lines/columns from `extent` (or `endif` line for preprocessor conditionals).
    - Stages spatial mapping: `m_map_ast.set(tag_target, line_s, char_s, line_e, char_e, ast_target)`.
    - Stages deduplicated link: `m_bridge_map.set(tag_target, tag_target)`.

---

## 6. Concrete AST & Preprocessor Node Reference

### 6.1. Comment & Assembly AST Nodes

| Class | `ASTT` Type Constant | `type_id` | Description |
| :--- | :--- | :--- | :--- |
| `Ast_Comment` | `ASTT.C_Comment` | 1 | C source comment (`//...` or `/*...*/`) truncated to 255 chars. |
| `Ast_Keyword` | N/A | 2 | Preserved statement keyword placeholder. |
| `Ast_ASM_Directive` | `ASTT.ASM_Directive` | 400 | Assembly directive (`.section`, `.align`, `.globl`). Auto-morphs to `Ast_ASM_Macro` on `.macro`. |
| `Ast_ASM_Macro` | `ASTT.ASM_Macro` | 401 | Assembly macro definition spanning `.macro` through `.endm`. |
| `Ast_ASM_Comment` | `ASTT.ASM_Comment` | 402 | Assembly comment line or unrecognized preprocessor fallback. |
| `Ast_ASM_Instruction`| `ASTT.ASM_Instruction`| 403 | Assembly mnemonic instruction with operands. |
| `Ast_ASM_Label` | `ASTT.ASM_Label` | 404 | Assembly jump/function label. |

### 6.2. C Preprocessor Subsystem (`CPPro`)

All preprocessor directives begin as root `CPPro(tline)`. Upon processing the directive name identifier, `ccpro_start_flip(TargetClass, cline)` dynamically morphs the instance into its concrete directive class:

| Class | `ASTT` Type Constant | `type_id` | Stored Data & Extraction Behavior |
| :--- | :--- | :--- | :--- |
| `CPPro_if` | `ASTT.CPPro_if` | 102 | Evaluated expression string. Tracks `highlight` and `endif` line coordinates. |
| `CPPro_elif` | `ASTT.CPPro_elif` | 103 | Branch expression string. Linked to previous branch and terminating `endif`. |
| `CPPro_else` | `ASTT.CPPro_else` | 104 | Empty expression. Linked to terminating `endif`. |
| `CPPro_endif` | `ASTT.CPPro_endif` | 105 | Closes active preprocessor conditional scope in `resolve_cppro_scopes()`. |
| `CPPro_ifdef` | `ASTT.CPPro_ifdef` | 100 | Conditional identifier string. |
| `CPPro_ifndef` | `ASTT.CPPro_ifndef` | 101 | Conditional identifier string. |
| `CPPro_elifdef` | `ASTT.CPPro_elifdef`| 999 | Conditional identifier string. |
| `CPPro_elifndef` | `ASTT.CPPro_elifndef`| 999 | Conditional identifier string. |
| `CPPro_define` | `ASTT.CPPro_define` / `CPPro_define_macro` | 106 / 112 | Macro name and replacement body. Tracks `func_args` if function-like macro. |
| `CPPro_undef` | `ASTT.CPPro_undef` | 107 | Undefined identifier name. |
| `CPPro_include` | `ASTT.CPPro_include`| 108 | Resolves include via `cursor.get_included_file()`. Stages `m_file_name.get_set()` and joined view `((m_ast.ast_id, m_ast_include.ast_id, 1),)`. |
| `CPPro_line` | `ASTT.CPPro_line` | 109 | Source line override: `"{lineno} {filename}"`. |
| `CPPro_error` | `ASTT.CPPro_error` | 110 | `#error` message payload string. |
| `CPPro_warning` | `ASTT.CPPro_warning`| 999 | `#warning` message payload string. |
| `CPPro_pragma` | `ASTT.CPPro_pragma` | 111 | `#pragma` directive body string. |

### 6.3. C Statement AST Nodes

| Class | `ASTT` Type Constant | Description |
| :--- | :--- | :--- |
| `Ast_CompoundStmt` | `ASTT.C_CompoundStmt` (46) | Scoped block statement (`{ ... }`). Populates `Zone_Type.Compound_Stmt`. |
| `Ast_IfStmt` | `ASTT.C_IfStmt` (47) | Conditional `if (...) { ... } [else { ... }]`. Bounded by statement extent. |
| `Ast_SwitchStmt` | `ASTT.C_SwitchStmt` (48) | Multi-branch `switch (expr) { ... }`. |
| `Ast_CaseStmt` | `ASTT.C_CaseStmt` (49) | Branch label `case value:`. |
| `Ast_DefaultStmt` | `ASTT.C_DefaultStmt` (50) | Default branch label `default:`. |
| `Ast_WhileStmt` | `ASTT.C_WhileStmt` (51) | Loop statement `while (cond) { ... }`. |
| `Ast_DoStmt` | `ASTT.C_DoStmt` (52) | Loop statement `do { ... } while (cond);`. |
| `Ast_ForStmt` | `ASTT.C_ForStmt` (53) | Iteration loop `for (init; cond; step) { ... }`. |
| `Ast_ReturnStmt` | `ASTT.C_ReturnStmt` (54) | Return statement `return expr;`. |
| `Ast_BreakStmt` | `ASTT.C_BreakStmt` (55) | Break statement `break;`. |
| `Ast_ContinueStmt` | `ASTT.C_ContinueStmt` (56) | Continue statement `continue;`. |
| `Ast_GotoStmt` | `ASTT.C_GotoStmt` (57) | Jump statement `goto label;`. |
| `Ast_LabelStmt` | `ASTT.C_LabelStmt` (58) | Jump destination label `label:`. |
| `Ast_AsmStmt` | `ASTT.C_AsmStmt` (59) | Inline assembly block `asm(...)` / `__asm__(...)`. |

### 6.4. Expression AST Nodes & Relational Type Linking

| Class | `ASTT` Type Constant | Description | Relational Foreign Key |
| :--- | :--- | :--- | :--- |
| `Ast_CallExpr` | `ASTT.C_CallExpr` (60) | Function invocation `callee(...)`. | Stored in `m_ast_container` referencing declared function prototype (`C_functionprotnotbind`). |
| `Ast_MemberRefExpr`| `ASTT.C_MemberRefExpr` (61)| Field/member access `obj.field` / `ptr->field`. | Stored in `m_ast_container` referencing struct/union definition (`C_structnotbind`). |
| `Ast_DeclRefExpr` | `ASTT.C_DeclRefExpr` (62)| Variable/identifier reference. | Resolves primitive type ID (`C_int`, `C_char`, etc.) or struct record reference. |
| `Ast_BinaryOperator`| `ASTT.C_BinaryOperator` (63)| Binary expression (`a + b`, `x = y`, `a == b`). | Cascades extraction to nested call and member expressions via `_extract_nested()`. |
| `Ast_UnaryOperator` | `ASTT.C_UnaryOperator` (64)| Unary expression (`*ptr`, `&val`, `!flag`, `~mask`). | Cascades extraction to nested operands via `_extract_nested()`. |

#### Relational Type Resolution: `resolve_cursor_type_ast(CS, cursor) -> tuple[int, Any]`
Resolves referenced AST definitions from libclang cursor metadata:
1. **Function Prototypes**: Maps `FUNCTION_DECL` / `CXX_METHOD` to `(ASTT.C_functionproto, CS.ref(m_ast.ast_id, REF_POS, op_idx))` via `m_ast.get_set(None, spelling, ASTT.C_functionprotnotbind)`.
2. **Struct Field Members**: Maps `FIELD_DECL` to `(ASTT.C_struct, CS.ref(m_ast.ast_id, REF_POS, op_idx))` via `m_ast.get_set(None, spelling, ASTT.C_structnotbind)`.
3. **Record / Enum Types**: Maps `RECORD` / `ENUM` type instances to their corresponding unbound forward declaration tag routes (`C_structnotbind` / `C_enumnotbind`).
4. **Primitives**: Maps basic C types (`int`, `char`, `long`, `short`, `float`, `double`, `bool`, `void`, `pointer`) directly to their `ASTT` constants.

### 6.5. Unhandled Block Handlers

- **`Not_Implemented`**: Fallback token swallower inside unrecognized compound blocks tracking `brace_depth`. Emits no database records.
- **`AST_Enum_Equal`**: Swallows enum value expressions (`= expr`) until comma `,` or closing brace `}`.
- **`AST_Array`**: Swallows array dimension tokens `[ expr ]` tracking `bracket_depth`.
- **`AST_Initializer`**: Swallows complex variable initializers `= { ... }` or `= (expr)` tracking brace, paren, and bracket depth until top-level `;` or `,`.

---

## 7. Spatial Scope & Zone Management (`Zone`, `Zone_Type`)

### 7.1. `Zone_Type` Enumeration

```python
class Zone_Type(IntEnum):
    Unset = 0
    Function_Args = 1     # Scopes function parameter lists: (int a, char *b)
    Declared_Args = 2     # Scopes struct/union field member bodies: { int x; char y; }
    Compound_Stmt = 3     # Scopes executable block/function bodies: { ... }
    Array_Content = 4     # Scopes array subscript expressions: [ 1024 ]
    Enum_Content = 5      # Scopes enum member definitions: { A, B = 2 }
    Enum_Equal = 6        # Scopes enum assignment values: = 1 << 3
    Full_File = 7         # Top-level translation unit file scope
    Initializer_Expr = 8  # Scopes variable initialization expressions: = { 0 }
```

### 7.2. `Zone` Class State & Protocol
- **State**:
  - `zone_type: Zone_Type`: Category of the code block.
  - `children: list[Ast]`: Sequence of parsed child `Ast` / `C_Type` / `Zone` elements.
  - `preset_extents: deque[Line]`: Expected child cursor boundary extents supplied by libclang.
  - `extent: Line`: Bounding coordinates enclosing the zone.
  - `completed: bool`: Flag set when closing boundary delimiter (e.g. `}`) is reached.
  - `end_mode: End_Mode`: Default delimiter termination mode for new children.
- **Token Processing (`check_exec`)**:
  1. If `ast_kind == AST_KIND.comment`, appends `Ast_Comment` and consumes.
  2. If `zone_type in _BRACE_ZONE_TYPES` (`Declared_Args`, `Enum_Content`, `Compound_Stmt`), the zone remains active across preprocessor directives (`#ifdef`, `#else`, `#endif`, `#define`) and comments until `tspelling == "}"` and `brace_depth <= 0`. Extent dynamically expands (`extent.grow(tline)`), preventing trailing preprocessor directives from leaking into outer declarators.
  3. If `zone_type == Zone_Type.Function_Args`, tracks `paren_depth` across `(` and `)`. When `paren_depth <= 0` at `)`, marks `completed = True` and terminates the active parameter declarator.
  4. If `zone_type == Zone_Type.Initializer_Expr`, tracks `brace_depth`, `paren_depth`, and `bracket_depth` across `{`, `}`, `(`, `)`, `[`, `]`. Marks `completed = True` on `;` (when `brace_depth <= 0`) and on `,` (when `brace_depth <= 0 and paren_depth <= 0 and bracket_depth <= 0`), preventing premature closure within nested struct/array initializers.
  5. If last child `within_range(token, ast_kind)` is `True`, dispatches to `last_child.exec_filter()`.
  6. If `tspelling == "#"`, instantiates `CPPro` (or `CPPro_include`).
  7. If `tspelling == "."` at `Full_File` root, instantiates `Ast_ASM_Directive`.
  8. Pops matching preset extent from `preset_extents` or allocates a new `C_Type(tline, self.end_mode)`.
- **Zone Delegation & In-Place Pruning (`C_Type.zones`)**:
  - `C_Type` maintains active child zones (e.g. `Function_Args`, `Declared_Args`, `Compound_Stmt`, `Initializer_Expr`).
  - To prevent call-stack explosion in large translation units, `C_Type` actively prunes completed zones (`self.zones = [z for z in self.zones if not z.completed]`) across token filter methods and delegates in reverse order (`reversed(self.zones)`), guaranteeing $O(1)$ stack depth.
- **Preprocessor Scope Resolution (`resolve_cppro_scopes`)**:
  - Scans `self.children` with a LIFO branch stack.
  - Links each `#if`, `#ifdef`, `#ifndef`, `#elif`, `#elifdef`, `#elifndef`, and `#else` node to its subsequent branch or terminating `#endif` line coordinate, setting `node.endif = Line(end_line, end_line)`.

---

## 8. C Type System, Qualifiers & Multi-Declarations (`C_Type`)

### 8.1. Data Structures

#### `CQual(Flag)`
Bit-flag tracking C type qualifiers:
```python
class CQual(Flag):
    Empty = 0
    const = 1       # ASTT.C_Qconst (15)
    volatile = 2    # ASTT.C_Qvolatile (16)
    restrict = 4    # ASTT.C_Qrestrict (17)
    _Atomic = 8     # ASTT.C_Q_Atomic (18)
```
- `output_ast() -> tuple`: Returns tuple of `ASTT` enum values for all enabled bits.

#### `TypeToken`
Slot-optimized type token container:
- `extent: Line`: Coordinate range.
- `code: str`: Raw text token spelling.
- `type: int`: `ASTT` type enum value.
- `is_definition: bool`: `True` if cursor is a type/function definition.
- `foreign_name: str | None`, `foreign_file: str | None`, `foreign_extent: Line | None`: Foreign declaration references retrieved via `cursor.get_definition()`.

#### `TypeSegment`
Aggregated segment of a type declaration:
- `content: list[TypeToken]`: Sequence of type tokens.
- `cqual: CQual`: Qualifier bit-flags.
- `cqual_content: list[TypeToken]`: List of qualifier tokens.
- `ref_type: TSRef`: Reference category (`No_Ref=0`, `AST_Ref=1`, `Route_Ref=2`).
- `ref: RouteType | int`: Stored operation route in `CS.cs`.
- `generate_ast(CS: ChangeSetType)`:
  - If single unqualified primitive, sets `type_id = content[0].type`.
  - If unbound forward struct/union/enum/proto (e.g. `struct foo *`), emits `m_ast.get_set(None, "foo", notbind_type)` in `REF_NO_REF` and sets `ref_type = TSRef.Route_Ref`.
  - If qualified or compound: constructs joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, count),), None, "", ASTT.C_Compound, ...)` mapping all qualifier and type tokens into `m_ast_container`.

### 8.2. `C_Type` Mechanics: `content` &rarr; `swap_out()` &rarr; `typedata`
1. `self.content` (`TypeSegment`): Accumulates tokens for the current type segment.
2. `self.swap_out()`: Moves `self.content` into `self.typedata = [TypeSegment, ...]` and resets `self.content`.
3. Triggers for `swap_out()`:
   - Qualifier encountered when `cqual` already has that flag set.
   - Pointers (`*`), array dimensions (`[`), or primitive type keywords (`char`, `int`, `long`, etc.).
   - Identifiers (variable names, typedefs, struct tags).

### 8.3. Multi-Declarator Splitting & Extraction (`C_Type.extract`)
1. **Child Zone Extraction**: Extracts nested zones inside `with CS(REF_MULTI):` and records `zone_link = CS.route[-2:]`.
2. **Declarator Grouping**:
   - Groups shared base type specifier (`root_type`) with individual variable declarators in `self.typedata`.
   - Generates list of `final_types = [ (root_type, declarator_1), (root_type, declarator_2), ... ]`.
3. **Type Definition Extraction (`struct` / `union` / `enum` / `functionproto`)**:
   - For definition tokens (`is_definition=True`), computes declaration type (`ASTT.C_structdecl`, `C_uniondecl`, `C_enumdecl`, `C_functionprotodecl`).
   - If child zones exist: emits `m_ast.ref_view` to dynamically join child container members from `zone_link`.
   - For `C_functionproto`: computes return type from preceding segments and embeds `(ret_t_id, ret_ref_ast_id)` in `m_ast_container`.
   - Tags and registers definition AST directly via `self.tag(CS, ast_id_route, self.extent)`.
4. **Variable Declaration Insertion**:
   - Emits joined view `m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, len(final_type)),), None, name, main_t_id, *container_tuples)` into `CS.cs`.
   - Emits tag and spatial mapping via `self.tag(CS, ast_id_route, self.extent)`.
5. **Initializer Expression Extraction (`AST_Initializer`)**:
   - Captures nested function calls (`Ast_CallExpr`), struct member accesses (`Ast_MemberRefExpr`), and declaration references (`Ast_DeclRefExpr`) during expression parsing.
   - Stages nested expression AST nodes into `CS` under `REF_NO_REF` during ChangeSet extraction.

---

## 9. Complete `ASTT` Type System Mapping

| `ASTT` Category | Enumeration Identifiers |
| :--- | :--- |
| **Primitives** | `C_void (3)`, `C_char (4)`, `C_short (5)`, `C_int (6)`, `C_long (7)`, `C_float (8)`, `C_double (9)`, `C_signed (10)`, `C_unsigned (11)`, `C_bool (12)`, `C_pointer (13)`, `C_array (14)`, `C_arrayempty (19)` |
| **Qualifiers** | `C_Qconst (15)`, `C_Qvolatile (16)`, `C_Qrestrict (17)`, `C_Q_Atomic (18)` |
| **Declarations** | `C_struct (20)`, `C_structdecl (21)`, `C_structnotbind (22)`<br>`C_union (23)`, `C_uniondecl (24)`, `C_unionnotbind (25)`<br>`C_enum (26)`, `C_enumdecl (27)`, `C_enumnotbind (28)`, `C_enumequal (29)`<br>`C_functionproto (30)`, `C_functionprotodecl (31)`, `C_functionprotnotbind (32)` |
| **Storage Class** | `C_SCauto (33)`, `C_SCregister (34)`, `C_SCstatic (35)`, `C_SCextern (36)`, `C_SC_Thread_local (37)`, `C_SCtypedef (38)`, `C_SCconstexpr (39)` |
| **Function Spec** | `C_FSinline (40)`, `C_FS_Noreturn (41)` |
| **Align / Misc** | `C_AS__Alignas (42)`, `C_Compound (50)`, `C_Comment (1)` |
| **Preprocessor** | `CPPro_ifdef (100)`, `CPPro_ifndef (101)`, `CPPro_if (102)`, `CPPro_elif (103)`, `CPPro_else (104)`, `CPPro_endif (105)`, `CPPro_define (106)`, `CPPro_undef (107)`, `CPPro_include (108)`, `CPPro_line (109)`, `CPPro_error (110)`, `CPPro_pragma (111)`, `CPPro_define_macro (112)` |
| **Assembly** | `ASM_Directive (400)`, `ASM_Macro (401)`, `ASM_Comment (402)`, `ASM_Instruction (403)`, `ASM_Label (404)` |

---

## 10. Database Entity Relationships & Table Layout Integration

The C AST parser generates and references records across 8 core database tables:

```
+----------------------------------------------------------------------------------------------------+
|                                    DATABASE TABLE SCHEMA MAP                                       |
+----------------------------------------------------------------------------------------------------+
| Table Name          | Table ID | Columns (in order)                                                 |
| ------------------- | :------: | ------------------------------------------------------------------ |
| m_v_main            |    0     | (vid, vname)                                                       |
| m_file_name         |    1     | (fnid, fname)                                                      |
| m_file              |    2     | (fid, vid_s, vid_e, ftype, s_stat, e_stat)                         |
| m_bridge_file       |    3     | (vid, fnid, fid)                                                   |
| m_ast               |    6     | (ast_id, name, type_id)                                            |
| m_ast_container     |    7     | (ast_id, priority, type_id, ref_ast_id)                            |
| m_ast_include       |    8     | (ast_id, fnid)                                                     |
| m_ast_debug         |    9     | (ast_id, ast_raw)                                                  |
| m_tag               |   10     | (tag_id, vid_s, vid_e, code, ast_id, hl_s, hl_l)                  |
| m_bridge_tag        |   11     | (fid, tag_id, line_s, line_e, char_s, char_e)                      |
| m_map_ast           |   12     | (map_id, line_s, char_s, line_e, char_e, ast_id)                   |
| m_bridge_map        |   13     | (tag_id, map_id)                                                   |
| m_ast_hash          |   14     | (hash, ast_id)                                                     |
+----------------------------------------------------------------------------------------------------+
```

### 10.1. Relational Staging Patterns

1. **Top-Level AST Insertion with Child Containers (`m_ast` + `m_ast_container`)**:
   ```python
   with CS(REF_POS):
       CS.store(m_ast.view(
           ((m_ast.ast_id, m_ast_container.ast_id, count),),
           None,            # ast_id (assigned by auto-increment or hash deduplication)
           name,            # Symbol or variable name
           main_type_id,    # ASTT type constant
           *container_data, # Slices of (None, priority, type_id, ref_ast_id)
       ))
       ast_id_route = CS.get_route_parse()
   ```

2. **Preprocessor Header Inclusions (`m_ast` + `m_ast_include`)**:
   ```python
   with CS(REF_POS):
       CS.store(m_file_name.get_set(None, resolved_include_path))
       fnid_route = CS.get_route_parse()

   with CS(REF_POS):
       CS.store(m_ast.view(
           ((m_ast.ast_id, m_ast_include.ast_id, 1),),
           None,
           written_include,
           ASTT.CPPro_include,
           None,
           CS.ref(m_file_name.fnid, *fnid_route),
       ))
       ast_id_route = CS.get_route_parse()
   ```

3. **Spatial Tagging & Map Registration**:
   ```python
   # 1. Tag definition
   with CS(REF_POS):
       CS.store(m_tag.set(None, VID, 0, extent.code, CS.ref(m_ast.ast_id, *ast_id_route), 0, 0))
       tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

   # 2. Bridge tag (coordinates in source file)
   CS.store(m_bridge_tag.set(
       ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
       tag_ref,
       extent.line_pos[0], extent.line_pos[1],
       extent.char_pos[0], extent.char_pos[1],
   ))

   # 3. Spatial AST map & bridge map
   CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, CS.ref(m_ast.ast_id, *ast_id_route)))
   CS.store(m_bridge_map.set(tag_ref, tag_ref))
   ```

---

## 11. Critical Invariants & AI Development Guidelines

1. **Import Order Guard**: Always import `core` modules (`core.globalstuff`, `core.DBLayout`) before `parser.c_ast` to avoid circular import deadlocks.
2. **Latin-1 File Encoding**: Linux kernel source contains non-UTF-8 bytes (e.g. `0xe1`, `0xf6`). All file reads, token slicing, and regex utilities must maintain `latin-1` compatibility.
3. **Ctypes Memory Retention**: `TokenList` must hold `self.token_group = cc.TokenGroup(parsed_tu, tokens_memory, tokens_count)` for the duration of processing to prevent garbage collection of underlying libclang memory.
4. **Context Stack Balance**: Any `with CS(link):` block must cleanly balance pushed and popped routes. `REF_MULTI` must enclose child zone extraction, and `REF_POS` must enclose stored operations whose IDs are referenced downstream.
5. **Deduplication Key Ordering**: For all `m_ast.get_set` or `no_duplicate` lookups, column arguments must match canonical table schemas exactly (`(None, name, type_id)` for `m_ast`).
6. **No-Op Exact Renames**: When `CS.file_operation == "R100"`, `c_ast_parse()` must return immediately to avoid generating duplicate AST tags.
7. **Testing Command Rule**: When validating AST parser changes, execute `python3 -m unittest tests/test_c_ast.py` and run `python3 main.py -u` once.
