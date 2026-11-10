# Fallback Raw AST Subsystem API & Architectural Contract Specification

Authoritative architectural contract and relational staging reference for `parser/raw_ast/raw_ast.py`.
Designed as an exhaustive reference for AI agents extending or maintaining fallback raw content handling.

## Table of Contents
- **§1**: High-Level Architecture & Purpose [L15-L69]
- **§2**: API Specifications [L70-L136]
- **§3**: Critical Invariants & Rules [L137-L154]
- **§4**: Unit Testing & Snapshot Verification Contract [L155-L184]
- **§5**: Documentation Reference & Label Subsystem [L185-L199]

---

## 1. High-Level Architecture & Purpose

The Fallback Raw AST parser (`raw_ast.py`) serves as the universal parser for files in the Linux kernel tree
that do not have dedicated AST grammar engines (e.g. documentation, text files, READMEs, licenses, build
scripts, firmwares, configuration snippets, and auxiliary data files).

### Core Responsibilities
1. **Full-Extent Tagging**: Wraps the entire content of the raw file in a single cohesive occurrence tag
   (`m_tag`) spanning from line 1, char 1 to `line_count`, `last_char_count`.
2. **Cryptographic Identity**: Computes deterministic SHA-256 hashes (`BINARY(32)` digest for `m_tag.hash` and
   hexadecimal string for `m_ast.name` with `ASTT.Raw_Content`).
3. **Cross-Version Tag Recycling**: Detects identical content across kernel releases and recycles existing
   `tag_id`s into new `m_bridge_checkpoint` / `m_bridge_tag` links with zero duplicate `m_tag` or `m_ast` database
   entries.
4. **History & Evolution Tracking**: When content changes across versions, records transitional links via
   `m_moved_tag(s_tag_id, tag_ref)`. Under the immutable Git DAG architecture, prior tags are permanent
   content records; tag closure is a no-op.

```
==================================================================================================
RAW AST PIPELINE (raw_ast.py)
--------------------------------------------------------------------------------------------------
Raw / Fallback File (T_RAW, T_MAINTAINERS, T_CREDITS, etc.)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op (Reuses old blob automatically)
  │
  ├─ Git Operation: "D" (Deleted) ──► No-op (tags are immutable content records; no prior tags are loaded)
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
     │  │     └─ m_bridge_tag.set(blob_id, recycled_tag_id, 1, line_count, 1, last_char_count)
     │  │        [Zero duplicate m_tag or m_ast created]
     │  │
     │  └─ New Content / Modified Tag:
     │     ├─ m_ast.view(((m_ast.ast_id,),), None, content_hex, ASTT.Raw_Content) [Rule 23]
     │     ├─ with CS(REF_POS):
     │     │  ├─ m_tag.set(None, content_hash, ast_ref, 0, 0) [5-col, Rule 12]
     │     │  └─ m_moved_tag.set(s_tag_id, tag_ref) (if modifying prior tag)
     │     ├─ m_bridge_tag.set(blob_id, tag_ref, 1, line_count, 1, last_char_count)
     │     └─ m_map_ast.set(tag_ref, 1, 1, line_count, last_char_count, ast_ref)
     │
==================================================================================================
```

---

## 2. API Specifications

### 2.1. `raw_ast_parse(CS: ChangeSet) -> None`
Primary entry point dispatched by `main.py` when processing files typed as `T_RAW` or unhandled extensions.

- **Signature**: `def raw_ast_parse(CS: Any) -> None`
- **Parameters**: `CS` &mdash; Active `ChangeSet` instance representing the current file operation.
- **Dispatch Logic**:
  - `CS.file_operation == "R100"`: Exits immediately. File renaming with identical content is resolved at the
    `m_bridge_checkpoint` layer without recreating or re-evaluating tags.
  - `CS.file_operation == "A"`: Executes `RawManager(CS)` inside `with CS(REF_NO_REF):`.
  - `CS.file_operation == "M"` or partial rename (`"R*"`):
    1. `get_prior_tags(CS)`: Queries and indexes tags from `Old_VID` (or base commit).
    2. `RawManager(CS)`: Reconciles content hash; recycles existing tag or creates replacement tag with
       `m_moved_tag` link.
  - `CS.file_operation == "D"` (or `"U"`): stages nothing; tags are immutable content records.

---

### 2.2. `get_prior_tags(CS: ChangeSet) -> None`
Queries TableEngine for existing active tags registered for the file in the preceding version
(`CS.gp.Old_VID` / base commit).

- **Query Path**:
  1. Resolves filename: uses `CS.old_path` for renames (`R*`), otherwise `CS.current_path`.
  2. Resolves `fnid`: `m_file_name.get(None, lookup_path)`.
  3. Resolves prior blob: `m_bridge_checkpoint.get(old_commit_id, fnid, None)`.
  4. Loads prior tags through `tag_tracker.get_prior_tags(CS, with_indexes=False, with_symbols=False)`: the
     shared 14-column `m_bridge_tag` -> `m_tag` -> `m_ast` join (`query_prior_tags()`), served from the
     parsing worker's batch prefetch when present (no second query).
  5. Populates `CS.prior_tags_map = {code_hash: [(idx, tag_id), ...]}` where `code_hash = tag[7]`.

---

### 2.4. `RawManager`
Class orchestrating file content loading, hashing, line coordinate calculations, tag recycling, and relational
staging.

#### Coordinates & Extent Definition
- Starts at line 1, column 1 (`line_s = 1`, `char_s = 1`).
- Line coordinates are computed using `safe_byte_coordinates(content_bytes)` from `core.encoding` without substring allocations.
- Binary files (null byte detected in first 4KB) bypass text coordinate parsing (`line_e = 1`, `char_e = len(content_bytes)`).
- Trailing newline off-by-one is guarded: trailing newline characters are accounted without allocating empty trailing line records.

#### Relational Staging Contract
```python
# 1. AST Symbol definition (Rule 23 view deduplication)
with CS(REF_POS):
    CS.store(m_ast.view(
        ((m_ast.ast_id,),),
        None,
        content_hex,
        ASTT.Raw_Content,
    ))
    ast_ref = ((m_ast.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))

# 2. Tier 1 recycling (check_exact_match) or a new tag (Rule 12 order, tag_tracker.stage_new_tag)
tag_ref = stage_new_tag(CS, content_hash, ast_ref, s_tag_id)

# 3. Spatial file coordinates
stage_bridge_tag(CS, tag_ref, (1, line_count), (1, last_char_count), positional=True)

# 4. Spatial AST map, keyed by the tag (map_id = tag_id; m_bridge_map is no longer written)
stage_tag_map(CS, tag_ref, ast_ref, line_count, last_char_count, positional=True)
```

---

## 3. Critical Invariants & Rules

1. **Tag Reference Invariant (Rule 12)**: Inside `with CS(REF_POS):`, `m_tag.set` must be the first stored
   operation so `tag_ref` points directly to `m_tag`. Auxiliary deduplication tables (`m_moved_tag.set`) must
   be stored immediately after if evolving a prior tag.
2. **Binary Digest Invariant (Rule 14)**: `m_tag.hash` requires raw 32-byte binary SHA-256 digests
   (`hashlib.sha256(content_bytes).digest()`). The AST symbol name (`m_ast.name`) uses the 64-character
   hexadecimal digest string (`hexdigest()`).
3. **Zero-Duplication Exact Renames**: `"R100"` operations are strict no-ops in `raw_ast_parse()` to avoid
   duplicate AST tags.
4. **Latin-1 Safety**: Content must always be decoded/encoded using `latin-1` to prevent UTF-8 decode errors
   on binary or non-UTF8 source tree files.
5. **Public Engine Queries (Rule 15)**: Tag lookups must strictly query public TableEngine APIs
   (`m_bridge_tag.view_get_multiple()`, `m_bridge_checkpoint.get()`, `m_file_name.get()`) without assuming in-memory
   table structures.

---

## 4. Unit Testing & Snapshot Verification Contract

The fallback raw AST parser is verified through the reusable snapshot testing harness:

- **Harness Module**: `unit_test/parser_harness.py`
  - `stage_file_prelude(CS, gp)`: Reusable file lifecycle prelude (`m_file_name`, `m_file_blob`, `m_bridge_checkpoint`)
    matching `main.py default_processing()`.
  - `extract_changeset_snapshot(CS | [CS, ...], test_conf=...)`: Extracts all executed operations from single
    or multi-version ChangeSets (`[cs_v1, cs_v2]`). Rows within each table preserve strict operational
    insertion order. Surrogate monotonic IDs (`tag_id`, `ast_id`, `blob_id`, `fnid`, `map_id`) are canonicalized
    into symbolic identifiers (`$tag_0`, `$ast_0`, `$blob_0`) sequentially based on first-appearance order,
    rendering snapshots deterministic while preserving Git DAG commit sequences. Updates on primary keys
    (`OP_UPDATE`) update existing rows in place. Table rows preserve exact database column definitions
    (`table.init_columns`), `type_id` values are formatted as `<id> (<name>)` (e.g., `"144 (Raw_Content)"`),
    and `ftype` values are formatted as `<id> (<name>)` (e.g., `"7 (T_RAW)"`).
  - `assert_snapshot_matches(test_case, parser_name, test_name, snapshot)`: Verifies extracted ChangeSet
    tables and `test_conf` metadata against verified JSON baselines. Set `UPDATE_SNAPSHOTS=1` in the
    environment to re-generate baselines after human verification.
- **Test Suite**: `unit_test/test_raw_parser.py`
  - `test_01_added_raw_file`: Added (`A`) file parsing, verifying creation of `m_v_main`, file prelude,
    `m_ast`, `m_tag`, `m_bridge_tag`, and `m_map_ast` (no `m_bridge_map`).
  - `test_02_modified_changed_raw_file`: Modified (`M`) file parsing across `v2.6.39` -> `v3.0`, validating
    evolutionary links via `m_moved_tag` and tag continuity.
  - `test_03_exact_rename_raw_file`: Exact rename (`R100`) file parsing across `v2.6.39` -> `v3.0`, verifying
    `m_v_main` progression, old blob reuse, `m_bridge_checkpoint` links, and zero duplicate AST tags.
- **Snapshot Storage**: `unit_test/snapshots/raw_ast/*.json` containing `test_conf` metadata and `tables`
  mapping.

---

## 5. Documentation Reference & Label Subsystem (`doc_parser.py`, `doc_labels.py`)

Raw and text documentation files are analyzed for cross-file links and section anchors:

### 5.1. Documentation Reference Scanning (`parser/doc_parser.py`)
- **Scanner**: `DocReferenceScanner` scans documentation and text files (`Documentation/`, `*.rst`, `*.txt`, `README*`) for references to repository files and source paths in $O(1)$ time per candidate.
- **Reference Types**: Identifies explicit paths, extensionless build files (`Makefile`, `Kbuild`, `MAINTAINERS`), Sphinx `:doc:` roles (with `.rst` / `.txt` extension fallback), Sphinx `.. kernel-doc::` directives (extracting `:functions:` and `:identifiers:` into details), legacy DocBook directives (`!E`, `!I`, `!F`, `!P`, `!C`), and source references.
- **Storage**: Emits cross-file references of type `FileRefType.Documentation` (5) into Table 28 `m_file_reference` (`source_blob_id`, `source_fnid`, `target_fnid`, `ref_type = 5`, `line_no`, `details`), executed at Step 6.6 (`processing_file_references`) and backfilled via `--backfill-doc-references`.

### 5.2. Documentation Label Extraction (`parser/doc_labels.py`)
- **Extractor**: Extracts Sphinx/reStructuredText `.. _label:` targets and their succeeding section titles from `Documentation/**/*.rst` blobs using single-pass regex `finditer` directly over raw text.
- **Storage**: Ingested at Step 6.9 (`parser/facts_ingest.py`) into Table 48 `m_blob_facts` under `family = 5` (`doc_labels.FAMILY`), encoded as zlib-compressed JSON (`doc_labels.FORMAT`).
- **Consumers**: Powers the web application's documentation label index and search navigators (`webapp/backend/services/docs_service.py`), resolving cross-document anchors without full-tree disk traversals.
