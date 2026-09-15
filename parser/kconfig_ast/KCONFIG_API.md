# Linux Kconfig & Kbuild Subsystem API & Architectural Contract Specification (`kconfig_ast`)

Authoritative architectural specification and relational staging reference for `parser/kconfig_ast/` ([kconfig_ast.py](parser/kconfig_ast/kconfig_ast.py), [kconfig_lexer.py](parser/kconfig_ast/kconfig_lexer.py), [kconfig_parser.py](parser/kconfig_ast/kconfig_parser.py)) and the associated build system parser [kbuild_parser.py](parser/kbuild_parser.py). Designed as an exhaustive reference for AI agents extending, maintaining, or querying the Linux kernel configuration and build dependency graph.

---

## 1. High-Level Architecture & Pipeline Flow

The Kconfig subsystem extracts configuration symbol definitions, dependency expressions (`depends on`), reverse dependencies (`select`, `imply`), menu tree hierarchies, and compiled object bindings into relational database tables.

```
==================================================================================================
KCONFIG & KBUILD PARSING PIPELINE
--------------------------------------------------------------------------------------------------
Kconfig Source File (Kconfig*)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op (Preserves old fid and tags)
  │
  ├─ Git Operation: "D" (Deleted)
  │  ├─ get_prior_tags(CS) ──► Queries old_vid tags via m_bridge_tag.view_get_multiple
  │  └─ close_prior_tags(CS) ─► Closes unreferenced prior tags via m_tag.update(vid_e=Old_VID)
  │
  └─ Git Operation: "A" / "M" / "R*" (Added / Modified / Content Rename)
     ├─ get_prior_tags(CS) (if M or R*)
     │
     ▼ [KconfigManager (kconfig_ast.py)]
Lexical Scanning & Recursive Descent Grammar:
  ├── KconfigLexer (kconfig_lexer.py): Slices source into tokens (keywords, symbols, strings, prompts, help blocks)
  └── KconfigParser (kconfig_parser.py): Builds typed node tree
        ├── Symbols: KconfigConfig (config, menuconfig)
        ├── Menus & Choices: KconfigMenu, KconfigChoice
        ├── Conditionals: KconfigIf
        ├── Sources: KconfigSource (source "path")
        ├── Comments: KconfigComment
        └── Boolean Logic: KconfigExpr (AND, OR, NOT, EQUAL, UNEQUAL)
  │
  ▼ [KconfigManager.extract_item() -> Relational ChangeSet Staging]
Relational Database Mapping:
  ├── AST Symbol Staging: m_ast.view [Rule 23]
  ├── Core Code Tag Staging: m_tag.set (Rule 12 order), m_tag_code, m_bridge_tag, m_map_ast, m_bridge_map
  ├── Symbol Registry: m_kconfig_symbol (Table 16)
  ├── Dependency & Reverse-Dependency Graph: m_kconfig_relation (Table 17)
  └── Menuconfig UI Hierarchy: m_kconfig_tree (Table 18)
  │
  ▼ [Post-Processing: processing_kbuild(version) (main.py + kbuild_parser.py)]
Kbuild / Makefile Integration:
  ├── KbuildParser: Scans all Makefiles & Kbuild files in the kernel tree
  ├── Extracts bindings: obj-$(CONFIG_XYZ) += target.o, composite targets (target-objs := a.o b.o)
  └── Staging: m_kconfig_kbuild (Table 19) links kcid -> (vid, fid, compile_mode, target_obj)
==================================================================================================
```

---

## 2. Relational Schema Mapping & ChangeSet Operations

`kconfig_ast` coordinates between general AST tracking tables (0–15) and Kconfig-specific domain tables (16–19):

### 2.1. Core AST and Tag Staging

1. **AST Node Definition (`m_ast.view` per Rule 23)**:
   ```python
   with CS(REF_POS):
       CS.store(m_ast.view(
           ((m_ast.ast_id,),),
           None,
           symbol_name[:255],
           int(ast_type),
       ))
       ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
   ```

2. **Occurrence Tagging & Deduplication (Rule 12 Order)**:
   `m_tag.set` **must be the first operation** inside `with CS(REF_POS):`:
   ```python
   with CS(REF_POS):
       CS.store(m_tag.set(
           None,            # tag_id (AUTO_INCREMENT)
           CS.gp.VID,       # vid_s
           0,               # vid_e (active = 0)
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

3. **Spatial Mapping**:
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

### 2.2. Domain Tables (Tables 16, 17, 18, 19)

| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **16** | `m_kconfig_symbol` | `(kcid, vid_s, vid_e, name, type, prompt, def_val, help, ast_id)` | `("kcid", "vid_s")` | `True` | Normalized Kconfig symbol definitions. Type codes: 1=bool, 2=tristate, 3=string, 4=hex, 5=int, 0=unknown. |
| **17** | `m_kconfig_relation` | `(rel_id, kcid, target_name, rel_type, cond_ast_id, priority)` | `("rel_id",)` | `True` | Direct dependencies & reverse dependencies. Relation types: `1=depends_on`, `2=select`, `3=imply`, `4=choice_member`. |
| **18** | `m_kconfig_tree` | `(tree_id, vid, parent_id, node_type, title, kcid, priority, dep_ast_id, ast_id)` | `("tree_id", "vid")` | `False` | Hierarchical Menuconfig tree. Node types: `1=root`, `2=menu`, `3=config`, `4=menuconfig`, `5=choice`, `6=if`, `7=comment`. |
| **19** | `m_kconfig_kbuild` | `(kcid, vid, fid, compile_mode, target_obj)` | `("kcid", "vid", "fid", "compile_mode")` | `False` | Compiled file & object mappings. Compile modes: `1=built-in` (`y`), `2=module` (`m`), `3=conditional` (`y/m`). |

#### Example: Staging `m_kconfig_relation`
```python
with CS(REF_POS):
    CS.store(m_kconfig_relation.set(
        None,             # rel_id (AUTO_INCREMENT, PK)
        kcid_ref,         # source symbol kcid reference
        target_sym_name,  # target symbol identifier (e.g. "BLOCK", "NET")
        1,                # rel_type: 1 = depends_on
        cond_ast_ref,     # conditional AST expression reference or 0
        rel_priority,     # sequence priority
    ))
```

---

## 3. AST Node Classification & Grammar

The parser implements a robust grammar for Linux Kconfig constructs:

| Grammar Node | `ASTT` Category | Numeric ID | Responsibilities |
| :--- | :--- | :---: | :--- |
| **`KconfigConfig`** | `ASTT.Kconfig_Config` | 88 | `config <NAME>` or `menuconfig <NAME>`. Captures symbol type (`bool`, `tristate`, `int`, `hex`, `string`), prompt string, default values, `depends on` clauses, `select` clauses, `imply` clauses, and multi-line `help` documentation. |
| **`KconfigMenu`** | `ASTT.Kconfig_Menu` | 89 | `menu "<TITLE>" ... endmenu`. Hierarchical menu container grouping configuration symbols. |
| **`KconfigChoice`** | `ASTT.Kconfig_Choice` | 90 | `choice ... endchoice`. Mutual-exclusion selection block among child symbols. |
| **`KconfigIf`** | `ASTT.Kconfig_If` | 91 | `if <EXPR> ... endif`. Scoped conditional block propagating parent dependencies to nested constructs. |
| **`KconfigSource`** | `ASTT.Kconfig_Source` | 92 | `source "<PATH>"`. Dynamic include directive linking child Kconfig files. |
| **`KconfigComment`** | `ASTT.Kconfig_Comment` | 93 | `comment "<TITLE>"`. Non-configurable section heading displayed in menuconfig. |
| **`KconfigMainmenu`** | `ASTT.Kconfig_Mainmenu` | 94 | `mainmenu "<TITLE>"`. Root title declaration for kernel configuration. |
| **`KconfigExpr`** | Multiple | 95–99 | Boolean logic tree: `ASTT.Kconfig_Op_And` (95), `ASTT.Kconfig_Op_Or` (96), `ASTT.Kconfig_Op_Not` (97), `ASTT.Kconfig_Op_Equal` (98), `ASTT.Kconfig_Op_Unequal` (99). |

---

## 4. Tag Lifecycle & Cross-Version Evolution

Kconfig files track tag continuity across kernel releases using `c_ast` tag tracking utilities ([tag_tracker.py](parser/c_ast/tag_tracker.py)):

1. **Tag Hash Deduplication**:
   Exact binary SHA-256 matches recycle existing `tag_id`s, preserving database space and preventing duplicate rows in `m_tag`.
2. **Prior Tag Evolution (`match_prior_tag_transition`)**:
   When a Kconfig symbol's help text, prompt, or default values change between kernel versions, the transition engine detects the modified symbol and stages `m_moved_tag(s_tag_id, tag_ref)`.
3. **Symbol Lifecycle (`m_kconfig_symbol`)**:
   Unchanged symbols carry over across versions. Modified symbols close their prior record with `vid_e = Old_VID` and insert a new record for `VID`.

---

## 5. Cross-File Reference Handling

1. **`source` Directive Resolution**:
   - `KconfigSource` extracts the target path (e.g. `source "drivers/net/Kconfig"`).
   - Stages `m_file_name.get_set(None, norm_path)` and links the dependency via `m_ast.view` joined with `m_ast_include`.
2. **Symbol Cross-Referencing**:
   - Symbols referenced in `depends on`, `select`, or `imply` statements are stored by name in `m_kconfig_relation.target_name`.
   - TableEngine queries can join `m_kconfig_relation.target_name` against `m_kconfig_symbol.name` to traverse the full directed acyclic dependency graph.

---

## 6. Dedicated Kbuild Subsystem Section (`kbuild_parser.py`)

The Kbuild parser maps Kconfig configuration symbols to concrete C, ASM, and object source files across the kernel directory tree:

### 6.1. Architecture & Execution
- **Location**: [parser/kbuild_parser.py](parser/kbuild_parser.py)
- **Invoked by**: `main.py:processing_kbuild(version)` at Step 6.6 of the update pipeline.
- **Workflow**:
  1. Traverses the kernel source tree for all `Makefile` and `Kbuild` files.
  2. Joins backslash line continuations (`\`) into logical lines.
  3. Evaluates regexes:
     - `OBJ_ASSIGN_RE`: Matches `obj-$(CONFIG_XYZ) += target.o`, `obj-y += ...`, `obj-m += ...`.
     - `COMPOSITE_RE`: Matches composite object rules `target-y := a.o b.o`, `target-objs := ...`.
  4. Resolves `.o` object goals to underlying `.c` and `.S` source files.
  5. Maps relative file paths to `fid` in `m_bridge_file`.
  6. Queries `m_kconfig_symbol` for `kcid` by symbol name.
  7. Stages batches into `m_kconfig_kbuild.set(kcid, vid, fid, compile_mode, target_obj)`.

### 6.2. Compile Modes
- `compile_mode = 1`: Built-in compilation (`y` / `obj-y`).
- `compile_mode = 2`: Loadable module compilation (`m` / `obj-m`).
- `compile_mode = 3`: Configurable compilation (`y/m` depending on active kernel config).

---

## 7. Performance Invariants & Architectural Rules

1. **Rule 12 Staging Order**:
   In `KconfigManager._stage_tag()`, `m_tag.set` MUST be the first operation inside `with CS(REF_POS):` so `tag_ref` references `m_tag`. Auxiliary tables (`m_tag_code.get_set`, `m_moved_tag.set`) follow immediately within the block.
2. **Rule 23 View Deduplication**:
   All AST node creation (`_extract_expr_ast`, `_stage_ast_node`) uses `m_ast.view(((m_ast.ast_id,),), None, name, type_id)`.
3. **Expression Tree Flattening**:
   Deep boolean expressions (e.g. chained `A && B && C && D`) are evaluated iteratively to prevent Python `RecursionError` exceptions on complex platform dependencies.
4. **Zero-Duplication Kbuild Ingestion**:
   `m_kconfig_kbuild` uses primary key `("kcid", "vid", "fid", "compile_mode")` with direct TableEngine caching to prevent redundant insertions across multi-directory Makefiles.
