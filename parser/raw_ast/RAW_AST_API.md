# Fallback Raw AST Subsystem API & Architectural Contract Specification

Authoritative architectural contract and relational staging reference for `parser/raw_ast/raw_ast.py`. Designed as an exhaustive reference for AI agents extending or maintaining fallback raw content handling.

---

## 1. High-Level Architecture & Purpose

The Fallback Raw AST parser (`raw_ast.py`) serves as the universal parser for files in the Linux kernel tree that do not have dedicated AST grammar engines (e.g. documentation, text files, READMEs, licenses, build scripts, firmwares, configuration snippets, and auxiliary data files).

### Core Responsibilities
1. **Full-Extent Tagging**: Wraps the entire content of the raw file in a single cohesive occurrence tag (`m_tag`) spanning from line 1, char 1 to `line_count`, `last_char_count`.
2. **Cryptographic Identity**: Computes deterministic SHA-256 hashes (`BINARY(32)` digest for `m_tag.hash` and hexadecimal string for `m_ast.name` with `ASTT.Raw_Content`).
3. **Cross-Version Tag Recycling**: Detects identical content across kernel releases and recycles existing `tag_id`s into new `m_bridge_file` / `m_bridge_tag` links with zero duplicate `m_tag`, `m_ast`, or `m_tag_code` database entries.
4. **History & Evolution Tracking**: When content changes across versions, records transitional links via `m_moved_tag(s_tag_id, tag_ref)` and closes obsolete prior tags with `vid_e = Old_VID`.

```
==================================================================================================
RAW AST PIPELINE (raw_ast.py)
--------------------------------------------------------------------------------------------------
Raw / Fallback File (T_RAW, T_MAINTAINERS, T_CREDITS, etc.)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op (Reuses old fid automatically)
  │
  ├─ Git Operation: "D" (Deleted)
  │  ├─ get_prior_tags(CS) ──► Queries old_vid tags via m_bridge_tag.view_get_multiple
  │  └─ close_prior_tags(CS) ─► Closes unreferenced prior tags via m_tag.update(vid_e=Old_VID)
  │
  └─ Git Operation: "A" / "M" / "R*" (Added / Modified / Content Rename)
     ├─ get_prior_tags(CS) (if M or R*)
     ├─ RawManager(CS):
     │  ├─ CS.mf.get_file(path, version) (Latin-1 safe byte extraction)
     │  ├─ content_hash = sha256(bytes).digest() (32-byte binary digest)
     │  ├─ content_hex = sha256(bytes).hexdigest() (64-character hex string)
     │  ├─ Slices lines: line_count, last_char_count
     │  │
     │  ├─ Check Recycled Hash:
     │  │  └─ If content_hash in prior_tags_map:
     │  │     ├─ Mark prior tag active (CS.active_tag_list.add(x))
     │  │     └─ m_bridge_tag.set(fid, recycled_tag_id, 1, line_count, 1, last_char_count)
     │  │        [Zero duplicate m_tag or m_ast created]
     │  │
     │  └─ New Content / Modified Tag:
     │     ├─ m_ast.get_set(None, content_hex, ASTT.Raw_Content)
     │     ├─ with CS(REF_POS):
     │     │  ├─ m_tag.set(None, VID, 0, content_hash, ast_ref, 0, 0) [Rule 12]
     │     │  ├─ m_tag_code.get_set(content_hash, content)
     │     │  └─ m_moved_tag.set(s_tag_id, tag_ref) (if modifying prior tag)
     │     ├─ m_bridge_tag.set(fid, tag_ref, 1, line_count, 1, last_char_count)
     │     ├─ m_map_ast.set(tag_ref, 1, 1, line_count, last_char_count, ast_ref)
     │     └─ m_bridge_map.set(tag_ref, tag_ref)
     │
     └─ close_prior_tags(CS) (if M or R*)
==================================================================================================
```

---

## 2. API Specifications

### 2.1. `raw_ast_parse(CS: ChangeSet) -> None`
Primary entry point dispatched by `main.py` when processing files typed as `T_RAW` or unhandled extensions.

- **Signature**: `def raw_ast_parse(CS: Any) -> None`
- **Parameters**: `CS` &mdash; Active `ChangeSet` instance representing the current file operation.
- **Dispatch Logic**:
  - `CS.file_operation == "R100"`: Exits immediately. File renaming with identical content is resolved at the `m_bridge_file` layer without recreating or re-evaluating tags.
  - `CS.file_operation == "A"`: Executes `RawManager(CS)` inside `with CS(REF_NO_REF):`.
  - `CS.file_operation == "M"` or partial rename (`"R*"`):
    1. `get_prior_tags(CS)`: Queries and indexes tags from `Old_VID`.
    2. `RawManager(CS)`: Reconciles content hash; recycles existing tag or creates replacement tag with `m_moved_tag` link.
    3. `close_prior_tags(CS)`: Closes any prior tags that were not recycled.
  - `CS.file_operation == "D"`:
    1. `get_prior_tags(CS)`: Retrieves active tags from `Old_VID`.
    2. `close_prior_tags(CS)`: Updates prior tags with `vid_e = Old_VID`.

---

### 2.2. `get_prior_tags(CS: ChangeSet) -> None`
Queries TableEngine for existing active tags registered for the file in the preceding version (`CS.gp.Old_VID`).

- **Query Path**:
  1. Resolves filename: uses `CS.old_path` for renames (`R*`), otherwise `CS.current_path`.
  2. Resolves `fnid`: `m_file_name.get(None, lookup_path)`.
  3. Resolves `old_fid`: `m_bridge_file.get(old_vid, fnid, None)`.
  4. Queries prior tags:
     ```python
     CS.prior_tags = m_bridge_tag.view_get_multiple(
         ((m_bridge_tag.tag_id, m_tag.tag_id, 1),),
         old_fid,
         None,  # 1: m_bridge_tag.tag_id
         None,  # 2: m_bridge_tag.line_s
         None,  # 3: m_bridge_tag.line_e
         None,  # 4: m_bridge_tag.char_s
         None,  # 5: m_bridge_tag.char_e
         None,  # 6: m_tag.tag_id
         None,  # 7: m_tag.vid_s
         None,  # 8: m_tag.vid_e
         None,  # 9: m_tag.code (hash)
         None,  # 10: m_tag.ast_id
         None,  # 11: m_tag.hl_s
         None,  # 12: m_tag.hl_l
     )
     ```
  5. Populates `CS.prior_tags_map = {code_hash: [(idx, tag_id), ...]}`.

---

### 2.3. `close_prior_tags(CS: ChangeSet) -> None`
Scans `CS.prior_tags` and marks any tag index not present in `CS.active_tag_list` as closed by staging an update with `vid_e = CS.gp.Old_VID`.

- **Staging Pattern**:
  ```python
  with CS(REF_OLD):
      for x, tag in enumerate(CS.prior_tags):
          if x in CS.active_tag_list:
              continue
          with CS(REF_POS):
              CS.store(m_tag.update(
                  tag[6],          # m_tag.tag_id
                  tag[7],          # m_tag.vid_s
                  CS.gp.Old_VID,   # m_tag.vid_e
                  tag[9],          # m_tag.hash
                  tag[10],         # m_tag.ast_id
                  tag[11],         # m_tag.hl_s
                  tag[12],         # m_tag.hl_l
              ))
  ```

---

### 2.4. `RawManager`
Class orchestrating file content loading, hashing, line coordinate calculations, tag recycling, and relational staging.

#### Coordinates & Extent Definition
- Starts at line 1, column 1 (`line_s = 1`, `char_s = 1`).
- Ends at total line count and last line character count (`line_e = max(1, len(lines))`, `char_e = max(1, len(lines[-1]))`).

#### Relational Staging Contract
```python
# 1. AST Symbol definition
with CS(REF_POS):
    CS.store(m_ast.get_set(
        None,
        content_hex,
        ASTT.Raw_Content,
    ))
    ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

# 2. Tag registration and code snippet deduplication (Rule 12 order)
with CS(REF_POS):
    CS.store(m_tag.set(
        None,
        CS.gp.VID,
        0,
        content_hash,
        ast_ref,
        0,
        0,
    ))
    tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))
    CS.store(m_tag_code.get_set(content_hash, content))
    if s_tag_id is not None:
        CS.store(m_moved_tag.set(s_tag_id, tag_ref))

# 3. Spatial file coordinates
with CS(REF_POS):
    CS.store(m_bridge_tag.set(
        ((m_file.table_id, 0), OP_REF, (REF_ROOT,)),
        tag_ref,
        1,
        line_count,
        1,
        last_char_count,
    ))

# 4. Spatial AST map
with CS(REF_POS):
    CS.store(m_map_ast.set(
        tag_ref,
        1,
        1,
        line_count,
        last_char_count,
        ast_ref,
    ))

# 5. Bridge map
if not hasattr(CS, "register_bridge_map") or CS.register_bridge_map(tag_ref, tag_ref):
    with CS(REF_POS):
        CS.store(m_bridge_map.set(
            tag_ref,
            tag_ref,
        ))
```

---

## 3. Critical Invariants & Rules

1. **Tag Reference Invariant (Rule 12)**: Inside `with CS(REF_POS):`, `m_tag.set` must be the first stored operation so `tag_ref` points directly to `m_tag`. Auxiliary deduplication tables (`m_tag_code.get_set`) must be stored immediately after.
2. **Binary Digest Invariant (Rule 14)**: `m_tag.hash` and `m_tag_code.hash` require raw 32-byte binary SHA-256 digests (`hashlib.sha256(content_bytes).digest()`). The AST symbol name (`m_ast.name`) uses the 64-character hexadecimal digest string (`hexdigest()`).
3. **Zero-Duplication Exact Renames**: `"R100"` operations are strict no-ops in `raw_ast_parse()` to avoid duplicate AST tags.
4. **Latin-1 Safety**: Content must always be decoded/encoded using `latin-1` to prevent UTF-8 decode errors on binary or non-UTF8 source tree files.
5. **Public Engine Queries (Rule 15)**: Tag lookups must strictly query public TableEngine APIs (`m_bridge_tag.view_get_multiple()`, `m_bridge_file.get()`, `m_file_name.get()`) without assuming in-memory table structures.
