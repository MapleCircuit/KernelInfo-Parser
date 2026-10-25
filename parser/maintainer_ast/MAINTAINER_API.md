# Linux Kernel Maintainers & Credits Subsystem API Specification (`maintainer_ast`)

Authoritative architectural specification and relational staging reference for `parser/maintainer_ast/` ([maintainer_ast.py](parser/maintainer_ast/maintainer_ast.py), [maintainer_parser.py](parser/maintainer_ast/maintainer_parser.py), [credits_parser.py](parser/maintainer_ast/credits_parser.py), [maintainer_matcher.py](parser/maintainer_ast/maintainer_matcher.py), [maintainer_types.py](parser/maintainer_ast/maintainer_types.py)). Designed as an exhaustive reference for AI agents extending, maintaining, or querying kernel subsystem ownership, maintainer personnel, and contributor records.

---

## 1. High-Level Architecture & Pipeline Flow

The maintainer subsystem parses two specialized root kernel documentation files:
1. `MAINTAINERS` (File type `T_MAINTAINERS` = 6): Defines subsystem ownership, maintainers, reviewers, mailing lists, source code repositories, and file matching patterns (`F:`, `X:`).
2. `CREDITS` (File type `T_CREDITS` = 7): Biographical directory of historical Linux kernel contributors.

```
==================================================================================================
MAINTAINERS & CREDITS PARSING PIPELINE (Immutable Git DAG Content-Addressed)
--------------------------------------------------------------------------------------------------
Kernel Root Documentation (MAINTAINERS / CREDITS)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op
  │
  ├─ Git Operation: "D" (Deleted) ──► No-op under immutable Git DAG architecture
  │
  └─ Git Operation: "A" / "M" / "R*" (Added / Modified / Content Rename)
     ├─ get_prior_tags(CS) (if M or R*: queries prior tags via 11-col 2-way join m_bridge_tag -> m_tag)
     │
     ├── If MAINTAINERS -> MaintainerManager(CS):
     │     ├── MaintainerParser: Parses 3-line header blocks, section titles, and tags
     │     │     ├── Roles: P (Person), M (Maintainer), R (Reviewer)
     │     │     ├── Subsystem Metadata: L (Mailing List), W (Web), T (SCM Tree), S (Status)
     │     │     └── Patterns: F (Files), X (Excludes), N (Regex), K (Keywords)
     │     └── Relational Staging:
     │           ├── AST Nodes: m_ast.view [Rule 23]
     │           ├── Code Occurrence Tags: m_tag.set (5-col, Rule 12 order), m_tag_code, m_bridge_tag (6-col)
     │           ├── Identities: m_maintainer_person (Table 20)
     │           ├── Subsystems: m_maintainer_section (Table 21, 8-col with blob_ref)
     │           ├── Personnel Roles: m_maintainer_member (Table 22)
     │           └── Match Rules: m_maintainer_pattern (Table 23)
     │
     ├── If CREDITS -> CreditsManager(CS):
     │     ├── CreditsParser: Parses contributor blocks (N:, E:, W:, P:, D:, S:)
     │     └── Relational Staging:
     │           ├── Person Registry: m_maintainer_person (Table 20)
     │           └── Contributor Biographies: m_credits_entry (Table 25, 8-col with blob_ref)
     │
     └─ close_prior_tags(CS) ──► No-op under immutable Git DAG architecture
  │
  ▼ [Post-Processing: processing_maintainer_files(version) (main.py + maintainer_matcher.py)]
Subsystem File Ownership Resolution:
  ├── MaintainerMatcher: Compiles all active 'F:' and 'X:' patterns from m_maintainer_pattern
  ├── Evaluates globs against all files active in repository tree for target release
  └── Populates file ownership bridge: m_maintainer_file (Table 24: maintainers_blob_id, fnid, sec_id)
==================================================================================================
```

---

## 2. Relational Schema Mapping & ChangeSet Operations

`maintainer_ast` stages operations into standard AST/tagging tables (0–15) and subsystem domain tables (20–25):

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

3. **Spatial Coordinate Mapping**:
   ```python
   CS.store(m_bridge_tag.set(
       self.blob_ref,       # ((m_file.table_id, 0), OP_REF, (REF_ROOT,))
       tag_ref,
       extent.line_pos[0],
       extent.line_pos[1],
       extent.char_pos[0],
       extent.char_pos[1],
   ))
   CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref))
   CS.store(m_bridge_map.set(tag_ref, tag_ref))
   ```

### 2.2. Domain Tables (Tables 20–25)

| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **20** | `m_maintainer_person` | `(person_id, name, email)` | `("person_id",)` | `True` | Unique contributor registry deduplicated by email address and name. |
| **21** | `m_maintainer_section`| `(sec_id, blob_id, name, status, scm_tree, web_page, mailing_list, ast_id)` | `("sec_id",)` | `True` | Subsystem maintainer section definitions and official channels scoped to blob. |
| **22** | `m_maintainer_member` | `(sec_id, person_id, role_type, priority)` | `("sec_id", "person_id", "role_type")` | `False` | Subsystem personnel roles: 1=Maintainer (`M:`), 2=Reviewer (`R:`), 3=Patch author (`P:`), 4=Credit (`C:`). |
| **23** | `m_maintainer_pattern`| `(sec_id, pat_type, pattern, priority)` | `("sec_id", "pat_type", "pattern", "priority")` | `False` | Path matching rules: 1=File (`F:`), 2=Exclude (`X:`), 3=Regex (`N:`), 4=Keyword (`K:`). |
| **24** | `m_maintainer_file` | `(maintainers_blob_id, fnid, sec_id)` | `("maintainers_blob_id", "fnid", "sec_id")` | `False` | Resolved subsystem file ownership bridge populated in post-processing. |
| **25** | `m_credits_entry` | `(credit_id, blob_id, person_id, web_page, pgp_key, description, snail_mail, ast_id)` | `("credit_id",)` | `True` | Contributor biography records parsed from `CREDITS` scoped to blob. |

---

## 3. AST Node Classification & Tag Extraction

### 3.1. MAINTAINERS Tag Classification
- **Section Headers**: Emits `ASTT.Maintainer_Section` (100) capturing the subsystem title (e.g. `"EXT4 FILE SYSTEM"`, `"ARM/APPLE MACHINE SUPPORT"`).
- **Maintainer Person (`P:`, `M:`, `R:`, `C:`)**: Emits `ASTT.Maintainer_Person` (101). Parsed into `(name, email)`. Ingested into `m_maintainer_person` with `person_id` reference linked in `m_maintainer_member`.
- **Patterns (`F:`, `X:`, `N:`, `K:`)**: Emits `ASTT.Maintainer_Pattern` (102). Slices file and exclude paths into `m_maintainer_pattern`.
- **Mailing Lists (`L:`)**: Emits `ASTT.Maintainer_List` (103).
- **Web Pages (`W:`)**: Emits `ASTT.Maintainer_Web` (104).
- **SCM Trees (`T:`)**: Emits `ASTT.Maintainer_Tree` (105).
- **Subsystem Status (`S:`)**: Emits `ASTT.Maintainer_Status` (106) with values: `Supported`, `Maintained`, `Odd Fixes`, `Orphan`, `Obsolete`.
- **Bug Reporting & Tracking (`B:`)**: Emits `ASTT.Maintainer_Bug` (196).
- **Chat & IRC Channels (`C:`)**: Emits `ASTT.Maintainer_Chat` (197).
- **Patchwork (`Q:`)**: Emits `ASTT.Maintainer_Patchwork` (198).

### 3.2. CREDITS Entry Classification
Parsed by `CreditsParser` into structured contributor blocks:
- `N:` Contributor Name (stored in `m_maintainer_person`).
- `E:` Contributor Email (stored in `m_maintainer_person`).
- `W:` Web Page URL.
- `P:` PGP Fingerprint / Key ID.
- `D:` Contribution description (e.g. `"Initial author of the floppy driver"`).
- `S:` Snail-mail physical address.
- Staged into `m_credits_entry` linked to `m_maintainer_person.person_id`.

---

## 4. Tag Lifecycle & Prior Tag Evolution

Under the immutable Git DAG content-addressable model:
1. **Immutable Tags**:
   All `m_tag` records are immutable and content-addressable. `close_prior_tags` is a no-op; tags are never closed with `vid_e` updates.
2. **Prior Tag Transition (`match_prior_tag_transition`)**:
   When modifying a file, `get_prior_tags` queries prior tags using an 11-column 2-way join (`m_bridge_tag` -> `m_tag`), and `match_prior_tag_transition` emits `m_moved_tag(s_tag_id, tag_ref)` if tags evolve.
3. **Person Deduplication**:
   `m_maintainer_person` uses `no_duplicate=True` on `(person_id,)` with in-memory caching to guarantee that contributors maintaining multiple subsystems share a single canonical `person_id`.

---

## 5. Post-Processing File Matching (`MaintainerMatcher`)

To resolve file ownership across the entire kernel repository:
1. **Execution**: Triggered at Step 6.7 in `main.py:processing_maintainer_files(version)`.
2. **Pattern Matching**:
   - Compiles `F:` file patterns and `X:` exclude patterns into normalized globs.
   - Evaluates each active file path in the tree against all sections in priority order.
   - Specific directory patterns (e.g. `fs/ext4/`) take precedence over broad generic patterns (e.g. `fs/`).
   - Indexed lookup: exact paths and directory prefixes are dictionary lookups; sections with wildcard patterns are filed under the directory part of each wildcard's literal prefix (`drivers/net/3c505*` -> `drivers/net/`), so a file only tests the wildcard sections filed under its ancestor directories plus those whose wildcards can match anywhere (e.g. `*/net/*`), still in section order. Sections without any `F:` pattern are skipped (they cannot match). On v3.0 (36,783 files, 925 sections) matching takes 0.5 s instead of 6.6 s with identical results.
   - `processing_maintainer_files()` skips files already mapped for the same MAINTAINERS blob in an earlier version.
3. **Bridge Population**:
   - Inserts resolved `(maintainers_blob_id, fnid, sec_id)` triplets into `m_maintainer_file`.
   - Deduplicates `(fnid, sec_id)` pairs in-memory using a tracking set to prevent duplicate primary key violations when multiple repository paths resolve to the same file.
   - Web application endpoints query `m_maintainer_file` to display the authoritative maintainers, mailing lists, and review channels for any viewed source file.

---

## 6. Performance Invariants & Architectural Rules

1. **Rule 12 Staging Order**:
   In `MaintainerManager._stage_tag()` and `CreditsManager._stage_tag()`, `m_tag.set` MUST be the first operation inside `with CS(REF_POS):` so `tag_ref` references `m_tag`. Auxiliary tables (`m_tag_code.get_set`, `m_moved_tag.set`) follow immediately within the block.
2. **Rule 23 View Deduplication**:
   All AST nodes (`_stage_ast_node`) use `m_ast.view(((m_ast.ast_id,),), None, name, type_id)`.
3. **Person Email Normalization**:
   Email addresses are stripped of surrounding angle brackets (`< >`), whitespace, and `mailto:` prefixes before deduplication.
4. **Batch DB Commit**:
   `processing_maintainer_files()` aggregates file-to-subsystem mappings into single bulk `m_maintainer_file` operations to minimize database write transactions.
