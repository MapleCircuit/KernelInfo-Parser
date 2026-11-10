# Native Assembly AST Subsystem API & Architectural Contract Specification (`asm_ast`)

Authoritative architectural specification and relational staging reference for `parser/asm_ast/`
([asm_ast.py](parser/asm_ast/asm_ast.py), [asm_ast_type.py](parser/asm_ast/asm_ast_type.py)). Designed as an
exhaustive reference for AI agents extending, maintaining, or interfacing with the native Linux Kernel
Assembly parsing subsystem.

## Table of Contents
- **§1**: High-Level Architecture & Pipeline Flow [L18-L82]
- **§2**: Relational Schema Mapping & ChangeSet Operations [L83-L168]
- **§3**: AST Node Classification & Grammar [L169-L185]
- **§4**: Tag Lifecycle, Prior Tag Evolution & Recycling [L186-L207]
- **§5**: Preprocessor Scope Resolution (`resolve_cppro_scopes`) [L208-L220]
- **§6**: Performance Invariants & Architectural Rules [L221-L237]
- **§7**: Assembly Pure Outline Extractor (`asm_pure_extractor.py`) [L238-L255]

---

## 1. High-Level Architecture & Pipeline Flow

`asm_ast` transforms Architecture Assembly source files (`.S`, `.s`, `.h`) into relational database operations
staged in a `ChangeSet` (`CS`).

```
==================================================================================================
NATIVE ASSEMBLY PARSING PIPELINE (asm_ast.py)
--------------------------------------------------------------------------------------------------
Assembly Source (.S / .s / .h)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op (Reuses old fid automatically)
  │
  ├─ Git Operation: "D" (Deleted) ──► No-op (tags are immutable content records; no prior tags are loaded)
  │
  └─ Git Operation: "A" / "M" / "R*" (Added / Modified / Content Rename)
     ├─ get_prior_tags(CS) (if M or R*)
     │
     ▼ [Asm_Manager (asm_ast.py)]
Clang TranslationUnit (cc.Index -> cc.TranslationUnit)
  │ (Invoked with -w, -x assembler-with-cpp, -D__KERNEL__, -D__ASSEMBLY__, kernel include paths)
  │ (options: cc.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + 32768)
  │
  ▼ [TokenList (parser/c_ast/tokenizer.py)]
Low-Level Foreign Ctypes Tokenization:
  ├── clang_tokenize() -> tokens_memory (Contiguous C array of cc.Token)
  ├── clang_annotateTokens() -> cursors_array (Contiguous C array of cc.Cursor)
  └── Fast Line/Col extraction via _CLANG_GET_SPELLING_LOC
  │
  ▼ [Asm_Manager.process_tokens()]
Token Dispatch Loop:
  ├── Comment: Ast_ASM_Comment(tline, tspelling) (ASTT.ASM_Comment)
  ├── Preprocessor Directive:
  │     ├── Inclusion: CPPro_include (ASTT.CPPro_include -> m_ast_include view)
  │     └── Directives: CPPro, CPPro_if, CPPro_ifdef, CPPro_define, etc.
  ├── Macro Definition: Ast_ASM_Macro (starts with '.macro', ends with '.endm')
  ├── Assembler Directive: Ast_ASM_Directive (starts with '.', e.g. .section, .align, .globl)
  ├── Linkage Macro / Entrypoint: Ast_ASM_Label (ENTRY, GLOBAL, SYM_FUNC_START*, SYM_CODE_START*)
  ├── Assembly Label: Ast_ASM_Label (identifier followed by ':' on same line)
  └── Assembly Instruction: Ast_ASM_Instruction (mnemonic + operands; branch/call instructions record symbol references)
  │
  ▼ [resolve_cppro_scopes()]
LIFO Stack Matching: Links conditional preprocessor branches (#if/#elif/#else) to #endif
  │
  ▼ [Asm_Manager.extract() -> CS.store()]
Relational Extraction inside with CS(REF_NO_REF):
  ├── AST Definition: m_ast.view(((m_ast.ast_id,),), None, name[:255], type_id) [Rule 23]
  ├── Spatial Tagging & 5-Tier Prior Tag Evolution Matching:
  │     ├── Tier 1: Exact 32-byte binary SHA-256 hash match -> Recycle existing tag_id
  │     ├── Tier 2: Exact symbol name & construct category match -> Transition evolved tag_id
  │     ├── Tier 3-5: Proximity & anchor fallback matching
  │     │
  │     ├── Rule 12 Staging Order inside with CS(REF_POS):
  │     │     ├── 1. m_tag.set(None, code_hash, ast_ref, 0, 0)          [MUST BE FIRST, 5 columns]
  │     │     │      tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
  │     │     └── 2. m_moved_tag.set(s_tag_id, tag_ref)                  [If evolved tag match]
  │     │
  │     ├── File-to-Tag Bridge: m_bridge_tag.set(blob_id, tag_ref, line_s, line_e, char_s, char_e)
  │     ├── Spatial AST Map: m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref)
  │     └── (m_map_ast is keyed by the tag (map_id = tag_id); m_bridge_map is no longer written)
==================================================================================================
```

---

## 2. Relational Schema Mapping & ChangeSet Operations

`asm_ast` decomposes assembly source constructs into relational operations staged in `CS.cs`:

### 2.1. Core Table Operations

1. **AST Node Definition (`m_ast.view` per Rule 23)**:
   ```python
   with CS(REF_POS):
       CS.store(m_ast.view(
           ((m_ast.ast_id,),),
           None,
           symbol_name[:255],
           int(self.ast_type),
       ))
       ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
   ```

2. **Occurrence Tagging & Deduplication (Rule 12 Order)**:
   Inside `with CS(REF_POS):`, `m_tag.set` **must be the first operation** so that `tag_ref` points directly
   to the newly allocated tag row:
   ```python
   with CS(REF_POS):
       CS.store(m_tag.set(
           None,            # tag_id (AUTO_INCREMENT)
           code_hash,       # 32-byte binary SHA-256 digest
           ast_ref,         # reference to m_ast node
           0,               # hl_s
           0,               # hl_l
       ))
       tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
       if s_tag_id is not None:
           CS.store(m_moved_tag.set(s_tag_id, tag_ref))
   ```

3. **File-to-Tag Spatial Mapping**:
   ```python
   CS.store(m_bridge_tag.set(
       ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
       tag_ref,
       extent.line_pos[0],
       extent.line_pos[1],
       extent.char_pos[0],
       extent.char_pos[1],
   ))
   CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref))  # map_id = tag_id
   ```

4. **Preprocessor Includes (`CPPro_include`)**:
   Emits `m_file_name.get_set` to register the target path, followed by a joined view linking `m_ast` to
   `m_ast_include`:
   ```python
   CS.store(m_file_name.get_set(None, inc_target))
   fnid_ref = ((m_file_name.table_id, 0), OP_REF, (REF_POS, len(CS.cs) - 1))
   CS.store(m_ast.view(
       ((m_ast.ast_id, m_ast_include.ast_id, 1),),
       None,
       inc_target,
       ASTT.CPPro_include,
       None,
       fnid_ref,
   ))
   ```

### 2.2. Symbol Definitions, Linkage Pairs & Branch Call References (`SymbolRows`)

1. **Linkage Pairs & Start/End Macro Bounds**:
   - Modern Linux kernel assembly standardizes linkage and symbol demarcation through `SYM_*` macro pairs:
     - Functions: `SYM_FUNC_START(name)` / `SYM_FUNC_END(name)`, `SYM_FUNC_START_LOCAL(name)` / `SYM_FUNC_END(name)`, `SYM_FUNC_START_WEAK(name)` / `SYM_FUNC_END(name)`.
     - Non-standard/trampoline code: `SYM_CODE_START(name)` / `SYM_CODE_END(name)`, `SYM_CODE_START_LOCAL(name)` / `SYM_CODE_END(name)`.
     - Typed & data entries: `SYM_TYPED_FUNC_START(name)` / `SYM_FUNC_END(name)`, `SYM_DATA_START(name)` / `SYM_DATA_END(name)`.
     - Legacy kernel routines: `ENTRY(name)` / `ENDPROC(name)` or `GLOBAL(name)` / `END(name)`.
   - **Bounds Resolution**: The opening macro establishes the symbol identifier, initial line coordinate (`line_s`), and linkage type (`GLOBAL`, `LOCAL`, or `WEAK`). The corresponding closing macro (`SYM_FUNC_END`, `ENDPROC`, etc.) determines the terminating line bound (`line_e`). When closing macros are omitted, symbol bounds extend to the next label, directive, or file boundary.

2. **Symbol Staging Contract (`core/SymbolRows.py`)**:
   - Every `Ast_ASM_Label` (including functions, jump labels, and linkage macros) records a definition via `record_symbol_def(CS, tag_ref, ast_ref, name, ASTT.ASM_Label, linkage, line_s, line_e)`:
     - Under tag-scoped layout (`G.SYMBOL_LAYOUT == "tag"`), definitions are written to universal `m_tag_def` (Table 36: `(tag_id, def_idx, sym_key, name, type_id, linkage, rel_line_s, rel_line_e)`).
     - Under blob-scoped layout, rows stage directly into `m_blob_symbol_def` (Table 26).
     - Linkage encoding conforms to `Linkage`: `1` = `GLOBAL` (exported), `2` = `LOCAL` (static/file-scoped), `3` = `WEAK` (overridable).
   - `.macro <name> ... .endm` blocks stage `Ast_ASM_Macro` and record a macro definition.
   - Instructions matching branch/call mnemonics (`call`, `callq`, `jmp`, `b`, `bl`, `jal`, etc.) record call references via `record_symbol_ref(CS, tag_ref, target_name, 0, SymbolRole.Call, line, col)`:
     - Staged in `m_tag_ref` (Table 37) and `m_blob_symbol_ref` (Table 27).
     - Per-blob resolution is governed by `m_blob_symbol` (Table 38) and `m_symbol_own` (Table 39).

---

## 3. AST Node Classification & Grammar

Assembly files contain a mixture of assembler directives, hardware instructions, symbol labels, comments, and
C preprocessor directives:

| Node Class | `ASTT` Category Constant | Numeric ID | Parsing & Bounds Rule |
| :--- | :--- | :---: | :--- |
| **`Ast_ASM_Directive`** | `ASTT.ASM_Directive` | 84 | Initiated by `.` punctuation token (e.g. `.section`, `.globl`, `.align`, `.macro`, `.byte`, `.ascii`). Extends until end-of-line delimiter. |
| **`Ast_ASM_Macro`** | `ASTT.ASM_Macro` | 83 | Sub-type of directive encapsulating `.macro <name>` through `.endm`. |
| **`Ast_ASM_Label`** | `ASTT.ASM_Label` | 86 | Identified by an identifier token followed immediately by `:` on the same line. Encapsulates label extent including the colon. |
| **`Ast_ASM_Instruction`**| `ASTT.ASM_Instruction` | 85 | General architecture instruction (e.g. `mov`, `push`, `jmp`, `add`, `syscall`). Absorbs all operand tokens across the line. |
| **`Ast_Comment`** | `ASTT.C_Comment` | 2 | C-style block (`/* ... */`) or line comments (`// ...`). |
| **`Ast_ASM_Comment`** | `ASTT.ASM_Comment` | 87 | Line comments initiated by `#` or architecture-specific comment markers. |
| **`CPPro_*`** | Various `ASTT.CPPro_*` | 1..19 | Standard preprocessor directives (`#include`, `#define`, `#ifdef`, `#ifndef`, `#if`, `#elif`, `#else`, `#endif`). |

---

## 4. Tag Lifecycle, Prior Tag Evolution & Recycling

`asm_ast` shares the 5-tier tag evolution engine with `c_ast` via
[tag_tracker.py](parser/c_ast/tag_tracker.py):

1. **Prior Tag Querying (`get_prior_tags(CS)`)**:
   - `tag_tracker.get_prior_tags()`: the 14-column `m_bridge_tag` -> `m_tag` -> `m_ast` join for `old_fid` in
     `Old_VID` (`query_prior_tags()`), served from the parsing worker's batch prefetch when present.
   - Indexes prior tags into `prior_tags_map[code_hash]` and `prior_tags_name_map[name]`.
2. **5-Tier Evolution Matching**:
   - **Tier 1 (Exact Hash)**: If `compute_code_hash(extent.code)` matches an unconsumed prior tag, the
     existing `tag_id` is recycled without creating new rows in `m_tag` or `m_ast`.
   - **Tier 2 (Name & AST Type)**: Evolved matching via `match_prior_tag_transition` when labels or macros are
     modified in-place.
   - **Tier 3 (Bulk Renames)**: Refactoring prefix/suffix changes.
   - **Tier 4 (Positional Anchors)**: Relative position between unchanged sibling labels.
   - **Tier 5 (Line Proximity)**: Fallback line overlap (drift $\le 2$ lines).
3. **Immutable Tags**: in the zero-VID DAG architecture tags are immutable content records; nothing closes
   them.

---

## 5. Preprocessor Scope Resolution (`resolve_cppro_scopes`)

Assembly files frequently use preprocessor conditionals (`#ifdef __ASSEMBLY__`, `#if defined(CONFIG_X86_64)`)
to toggle architectural blocks:
- Conditional directives (`CPPro_if`, `CPPro_ifdef`, `CPPro_ifndef`, `CPPro_elif`, `CPPro_else`) are pushed
  onto a LIFO stack.
- When encountering `#endif` (`CPPro_endif`), the scope is resolved, and ending coordinates are recorded on
  the opening directive.
- Coordinate bounding boxes in `m_map_ast` compute relative line and character extents against `self.endif` to
  ensure proper highlighting bounds.

---

## 6. Performance Invariants & Architectural Rules

1. **Clang Index Reuse**:
   - Worker processes maintain a persistent `_WORKER_ASM_CLANG_INDEX` (`cc.Index.create()`) to avoid per-file
     libclang context recreation overhead.
2. **Ctypes Fast Tokenization**:
   - Uses `TokenList` from [tokenizer.py](parser/c_ast/tokenizer.py) for direct ctypes array memory access
     (`_CLANG_GET_SPELLING_LOC`, `_CLANG_GET_TOKEN_KIND`), bypassing Python object allocations.
3. **Rule 12 Staging Order**:
   - `m_tag.set` MUST be the first operation inside `with CS(REF_POS):` so that `tag_ref` references `m_tag`.
     Auxiliary tables (such as `m_moved_tag.set`) must follow.
4. **Rule 23 View Deduplication**:
   - AST node creation must use `m_ast.view(((m_ast.ast_id,),), None, name, type_id)`, never `m_ast.set` or
     `m_ast.get_set`.
5. **Memory Teardown**:
   - Upon completion of `Asm_Manager.Init_Parse()`, AST children are extracted and ephemeral structures are
     cleared before worker IPC serialization.

---

## 7. Assembly Pure Outline Extractor (`asm_pure_extractor.py`)

`parser/asm_ast/asm_pure_extractor.py` provides stateless outline extraction on raw assembly source bytes
without database or TableEngine dependencies:
- **API**: `extract_asm_outline(source_bytes: bytes) -> list[dict]`
- **Extracted Entities**:
  - Functions: `SYM_FUNC_START*`, `SYM_CODE_START*`, `ENTRY` blocks with closing extent matching (`SYM_FUNC_END*`, `ENDPROC`, etc.).
  - Macros: `.macro <name>` definitions.
  - Global labels: non-numeric labels defined with `.globl` / `.global` linkage.
- **Dict Schema**: `{"name": str, "kind": str, "line_s": int, "line_e": int}` (where `kind` is `"function"`, `"macro"`, or `"label"`).
- Used by WebApp services for outline display of assembly files on unparsed commits.
