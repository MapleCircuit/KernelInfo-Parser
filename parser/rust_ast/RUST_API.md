# Native Rust AST Subsystem API & Architectural Contract Specification (`rust_ast`)

Authoritative architectural specification and relational staging reference for `parser/rust_ast/` ([rust_ast.py](parser/rust_ast/rust_ast.py), [rust_ast_type.py](parser/rust_ast/rust_ast_type.py), [rust_tree_parser.py](parser/rust_ast/rust_tree_parser.py)). Designed as an exhaustive reference for AI agents extending, maintaining, or querying the Linux kernel Rust language parsing subsystem.

---

## 1. High-Level Architecture & Pipeline Flow

The Rust AST subsystem extracts syntax tree nodes, visibility qualifiers, generics, doc comments, structural child hierarchies, code tags, and coordinate regions from Linux kernel Rust source files (`.rs`).

```
==================================================================================================
RUST AST PARSING PIPELINE (rust_ast.py)
--------------------------------------------------------------------------------------------------
Rust Source Code (.rs)
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
     ▼ [Rust_Manager (rust_ast.py)]
Source Ingestion & AST Tree Parsing:
  ├── Loads raw file content via CS.mf.get_file(CS.current_path, Version_Name)
  ├── Slices source lines and extracts Line extent boundaries
  └── parse_rust_ast_tree (rust_tree_parser.py):
        ├── Parses items: structs, enums, functions, traits, impls, modules, macros
        ├── Extracts generics, attributes (#[...]), doc comments (///), visibility (pub, pub(crate))
        └── Instantiates strongly typed Ast_Rust node hierarchy
  │
  ▼ [Ast_Rust.extract() -> Relational ChangeSet Staging (rust_ast_type.py)]
Relational Extraction inside with CS(REF_NO_REF):
  ├── Hierarchical AST Staging:
  │     ├── Single Node: m_ast.view(((m_ast.ast_id,),), None, name, type_id) [Rule 23]
  │     └── With Children: m_ast.view(((m_ast.ast_id, m_ast_container.ast_id, N),), ..., *flat_payload) [Rule 21]
  │
  ├── Spatial Tagging & 5-Tier Prior Tag Evolution Matching:
  │     ├── Tier 1: Exact 32-byte binary SHA-256 hash match -> Recycle existing tag_id
  │     ├── Tier 2: Exact symbol name & construct category match -> Transition evolved tag_id
  │     ├── Tier 3-5: Positional anchors and spatial line proximity fallback
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

`rust_ast` maps Rust constructs into core relational tables using TableEngine joined views:

### 2.1. Atomic AST & Container Hierarchy Staging (Rules 21 & 23)

1. **Leaf Construct (Single AST Node)**:
   ```python
   with CS(REF_POS):
       CS.store(m_ast.view(
           ((m_ast.ast_id,),),
           None,
           symbol_name[:255],
           type_id,
       ))
       ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
   ```

2. **Parent Construct with Children (Flat Positional Scalars per Rule 21)**:
   Container items must be passed as **flat positional scalars** (`*container_payload` where each child contributes `None, priority, child_ast_type, child_ref`), never as nested tuples inside lists:
   ```python
   container_payload = []
   for priority, (child, child_ref) in enumerate(zip(self.children, child_refs)):
       container_payload.extend([None, priority, int(child.ast_type), child_ref])

   with CS(REF_POS):
       CS.store(m_ast.view(
           ((m_ast.ast_id, m_ast_container.ast_id, len(child_refs)),),
           None,
           symbol_name[:255],
           type_id,
           *container_payload,
       ))
       ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
   ```

### 2.2. Occurrence Tagging & Code Deduplication (Rule 12 Order)

Inside `with CS(REF_POS):`, `m_tag.set` **must be the first operation** so that `tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))` points directly to `m_tag`:
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
    CS.store(m_tag_code.get_set(code_hash, self.extent.code))
    if s_tag_id is not None:
        CS.store(m_moved_tag.set(s_tag_id, tag_ref))
```

### 2.3. Spatial Mapping
```python
CS.store(m_bridge_tag.set(
    ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
    tag_ref,
    self.extent.line_pos[0],
    self.extent.line_pos[1],
    self.extent.char_pos[0],
    self.extent.char_pos[1],
))
CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref))
CS.store(m_bridge_map.set(tag_ref, tag_ref))
```

---

## 3. Construct Taxonomy & Grammar

The Rust AST parser defines dedicated `Ast_Rust` classes for every major language construct:

| Construct Class | `ASTT` Constant | Numeric ID | Description |
| :--- | :--- | :---: | :--- |
| **`Ast_Rust_Struct`** | `ASTT.Rust_Struct` | 107 | Struct definitions (`struct Foo { ... }`, tuple structs `struct Bar(u32);`). Encapsulates fields as child container items. |
| **`Ast_Rust_Enum`** | `ASTT.Rust_Enum` | 108 | Enum definitions (`enum Baz { A, B(u64) }`). Variants staged under enum container. |
| **`Ast_Rust_Fn`** | `ASTT.Rust_Fn` | 109 | Free-standing or associated functions (`fn qux<T>(...) -> R { ... }`). Tracks signature, parameters, and return type. |
| **`Ast_Rust_Trait`** | `ASTT.Rust_Trait` | 110 | Trait declarations (`pub trait Driver { ... }`). Child methods linked at priorities 0..N. |
| **`Ast_Rust_Impl`** | `ASTT.Rust_Impl` | 111 | Trait implementations (`impl Driver for MyDriver`) or inherent impl blocks (`impl Foo`). |
| **`Ast_Rust_Mod`** | `ASTT.Rust_Mod` | 112 | In-line module blocks (`mod bar { ... }`) or external module declarations (`mod bar;`). |
| **`Ast_Rust_Macro`** | `ASTT.Rust_Macro` | 113 | Macro declarations (`macro_rules! my_macro { ... }`) or macro invocations. |
| **`Ast_Rust_Type`** | `ASTT.Rust_Type` | 114 | Type aliases (`type Result<T> = core::result::Result<T, Error>;`). |
| **`Ast_Rust_Const`** | `ASTT.Rust_Const` | 115 | Associated or module constants (`const MAX_SIZE: usize = 1024;`). |
| **`Ast_Rust_Static`** | `ASTT.Rust_Static` | 116 | Static memory bindings (`static COUNTER: AtomicU32 = ...;`). |
| **`Ast_Rust_DocComment`**| `ASTT.Rust_DocComment` | 117 | Multi-line and inner doc comments (`///` and `//!`). |

---

## 4. Tag Lifecycle & Prior Tag Evolution

`rust_ast` tracks construct continuity across Git versions using `get_prior_tags` and `match_prior_tag_transition`:

1. **Prior Tag Indexing (`get_prior_tags`)**:
   Queries active tags for `old_fid` in `Old_VID` from `m_bridge_tag` joined with `m_tag`. Builds in-memory lookup map `CS.prior_tags_map[code_hash]`.
2. **Exact Tag Recycling (Tier 1)**:
   If `compute_code_hash(self.extent.code)` matches an unconsumed prior tag, the existing `tag_id` is recycled directly into `m_bridge_tag`, producing zero duplicate entries in `m_tag` or `m_ast`.
3. **Modified Tag Evolution (Tier 2)**:
   If code has changed, `match_prior_tag_transition` matches by symbol name and AST type constant, linking the old tag to the new tag via `m_moved_tag(s_tag_id, tag_ref)`.
4. **Closing Obsolete Tags (`close_prior_tags`)**:
   Un-recycled prior tags are marked closed by updating `vid_e = Old_VID` in `REF_OLD`.

---

## 5. Cross-File Reference Handling

1. **`mod` Declarations**:
   - For external modules (`mod sub_module;`), the parser computes the expected file path (`sub_module.rs` or `sub_module/mod.rs`).
   - Registers target path in `m_file_name.get_set` and links the dependency via `m_ast_include`.
2. **`use` Imports**:
   - `use` statements (e.g. `use kernel::prelude::*;`) record symbol dependencies in `CS.foreign_deps` for resolution in the DependencyScheduler.

---

## 6. Performance Invariants & Architectural Rules

1. **Rule 12 Staging Order**:
   `m_tag.set` MUST be the first operation inside `with CS(REF_POS):` so `tag_ref` references `m_tag`. Auxiliary tables (`m_tag_code.get_set`, `m_moved_tag.set`) follow immediately within the block.
2. **Rule 21 Flat Scalar Container Payload**:
   In `m_ast.view` joined with `m_ast_container`, container arguments MUST be passed as flat positional scalars (`*container_payload`), never as nested tuples inside lists.
3. **Rule 23 View Deduplication**:
   All AST node creation must use `m_ast.view(...)`, never direct `m_ast.set` or `m_ast.get_set`.
4. **Binary SHA-256 Hashes (Rule 14)**:
   Code hashes are always 32-byte binary digests computed via `compute_code_hash(self.extent.code)` for storage in `m_tag.hash` (`BINARY(32)`).
