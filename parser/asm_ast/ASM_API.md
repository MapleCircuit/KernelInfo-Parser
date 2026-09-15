# Native Assembly AST Subsystem API & Architectural Contract Specification (`asm_ast`)

Authoritative architectural specification and relational staging reference for `parser/asm_ast/` ([asm_ast.py](parser/asm_ast/asm_ast.py), [asm_ast_type.py](parser/asm_ast/asm_ast_type.py)). Designed as an exhaustive reference for AI agents extending, maintaining, or interfacing with the native Linux Kernel Assembly parsing subsystem.

---

## 1. High-Level Architecture & Pipeline Flow

`asm_ast` transforms Architecture Assembly source files (`.S`, `.s`, `.h`) into relational database operations staged in a `ChangeSet` (`CS`).

```
==================================================================================================
NATIVE ASSEMBLY PARSING PIPELINE (asm_ast.py)
--------------------------------------------------------------------------------------------------
Assembly Source (.S / .s / .h)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op (Reuses old fid automatically)
  │
  ├─ Git Operation: "D" (Deleted)
  │  ├─ get_prior_tags(CS) ──► Queries old_vid tags via m_bridge_tag.view_get_multiple
  │  └─ close_prior_tags(CS) ─► Closes unreferenced prior tags via m_tag.update(vid_e=Old_VID)
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
  ├── Comment: Ast_Comment(tline, tspelling) (ASTT.C_Comment / ASTT.ASM_Comment)
  ├── Preprocessor Directive:
  │     ├── Inclusion: CPPro_include (ASTT.CPPro_include -> m_ast_include view)
  │     └── Directives: CPPro, CPPro_if, CPPro_ifdef, CPPro_define, etc.
  ├── Assembler Directive: Ast_ASM_Directive (starts with '.', e.g. .section, .align, .globl)
  ├── Assembly Label: Ast_ASM_Label (identifier followed by ':' on same line)
  └── Assembly Instruction: Ast_ASM_Instruction (general instruction mnemonic + operands)
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
  │     │     ├── 1. m_tag.set(None, VID, 0, code_hash, ast_ref, 0, 0)  [MUST BE FIRST]
  │     │     │      tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
  │     │     ├── 2. m_tag_code.get_set(code_hash, code_str)             [Auxiliary hash table]
  │     │     └── 3. m_moved_tag.set(s_tag_id, tag_ref)                  [If evolved tag match]
  │     │
  │     ├── File-to-Tag Bridge: m_bridge_tag.set(fid, tag_ref, line_s, line_e, char_s, char_e)
  │     ├── Spatial AST Map: m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref)
  │     └── Bridge Map Link: m_bridge_map.set(tag_ref, tag_ref)
  │
  └─ close_prior_tags(CS) (if M or R*)
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
   Inside `with CS(REF_POS):`, `m_tag.set` **must be the first operation** so that `tag_ref` points directly to the newly allocated tag row:
   ```python
   with CS(REF_POS):
       CS.store(m_tag.set(
           None,            # tag_id (AUTO_INCREMENT)
           CS.gp.VID,       # vid_s
           0,               # vid_e (0 = active)
           code_hash,       # 32-byte binary SHA-256 digest
           ast_ref,         # reference to m_ast node
           0,               # hl_s
           0,               # hl_l
       ))
       tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
       CS.store(m_tag_code.get_set(code_hash, extent.code))
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
   CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref))
   CS.store(m_bridge_map.set(tag_ref, tag_ref))
   ```

4. **Preprocessor Includes (`CPPro_include`)**:
   Emits `m_file_name.get_set` to register the target path, followed by a joined view linking `m_ast` to `m_ast_include`:
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

---

## 3. AST Node Classification & Grammar

Assembly files contain a mixture of assembler directives, hardware instructions, symbol labels, comments, and C preprocessor directives:

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

`asm_ast` shares the 5-tier tag evolution engine with `c_ast` via [tag_tracker.py](parser/c_ast/tag_tracker.py):

1. **Prior Tag Querying (`get_prior_tags(CS)`)**:
   - Queries `m_bridge_tag.view_get_multiple` for `old_fid` in `Old_VID`.
   - Indexes prior tags into `prior_tags_map[code_hash]` and `prior_tags_name_map[name]`.
2. **5-Tier Evolution Matching**:
   - **Tier 1 (Exact Hash)**: If `compute_code_hash(extent.code)` matches an unconsumed prior tag, the existing `tag_id` is recycled without creating new rows in `m_tag`, `m_ast`, or `m_tag_code`.
   - **Tier 2 (Name & AST Type)**: Evolved matching via `match_prior_tag_transition` when labels or macros are modified in-place.
   - **Tier 3 (Bulk Renames)**: Refactoring prefix/suffix changes.
   - **Tier 4 (Positional Anchors)**: Relative position between unchanged sibling labels.
   - **Tier 5 (Line Proximity)**: Fallback line overlap (drift $\le 2$ lines).
3. **Closing Obsolete Tags (`close_prior_tags(CS)`)**:
   - Any prior tags not recycled or transitioned are closed via `m_tag.update(vid_e = Old_VID)` inside `with CS(REF_OLD):`.

---

## 5. Preprocessor Scope Resolution (`resolve_cppro_scopes`)

Assembly files frequently use preprocessor conditionals (`#ifdef __ASSEMBLY__`, `#if defined(CONFIG_X86_64)`) to toggle architectural blocks:
- Conditional directives (`CPPro_if`, `CPPro_ifdef`, `CPPro_ifndef`, `CPPro_elif`, `CPPro_else`) are pushed onto a LIFO stack.
- When encountering `#endif` (`CPPro_endif`), the scope is resolved, and ending coordinates are recorded on the opening directive.
- Coordinate bounding boxes in `m_map_ast` compute relative line and character extents against `self.endif` to ensure proper highlighting bounds.

---

## 6. Performance Invariants & Architectural Rules

1. **Clang Index Reuse**:
   - Worker processes maintain a persistent `_WORKER_ASM_CLANG_INDEX` (`cc.Index.create()`) to avoid per-file libclang context recreation overhead.
2. **Ctypes Fast Tokenization**:
   - Uses `TokenList` from [tokenizer.py](parser/c_ast/tokenizer.py) for direct ctypes array memory access (`_CLANG_GET_SPELLING_LOC`, `_CLANG_GET_TOKEN_KIND`), bypassing Python object allocations.
3. **Rule 12 Staging Order**:
   - `m_tag.set` MUST be the first operation inside `with CS(REF_POS):` so that `tag_ref` references `m_tag`. Auxiliary tables (`m_tag_code.get_set`, `m_moved_tag.set`) must follow.
4. **Rule 23 View Deduplication**:
   - AST node creation must use `m_ast.view(((m_ast.ast_id,),), None, name, type_id)`, never `m_ast.set` or `m_ast.get_set`.
5. **Memory Teardown**:
   - Upon completion of `Asm_Manager.Init_Parse()`, AST children are extracted and ephemeral structures are cleared before worker IPC serialization.
