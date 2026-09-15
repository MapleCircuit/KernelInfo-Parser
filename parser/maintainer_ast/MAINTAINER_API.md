# Linux Kernel Maintainers & Credits Subsystem API Specification (`maintainer_ast`)

Authoritative architectural specification and relational staging reference for `parser/maintainer_ast/` ([maintainer_ast.py](parser/maintainer_ast/maintainer_ast.py), [maintainer_parser.py](parser/maintainer_ast/maintainer_parser.py), [credits_parser.py](parser/maintainer_ast/credits_parser.py), [maintainer_matcher.py](parser/maintainer_ast/maintainer_matcher.py), [maintainer_types.py](parser/maintainer_ast/maintainer_types.py)). Designed as an exhaustive reference for AI agents extending, maintaining, or querying kernel subsystem ownership, maintainer personnel, and contributor records.

---

## 1. High-Level Architecture & Pipeline Flow

The maintainer subsystem parses two specialized root kernel documentation files:
1. `MAINTAINERS` (File type `T_MAINTAINERS` = 6): Defines subsystem ownership, maintainers, reviewers, mailing lists, source code repositories, and file matching patterns (`F:`, `X:`).
2. `CREDITS` (File type `T_CREDITS` = 7): Biographical directory of historical Linux kernel contributors.

```
==================================================================================================
MAINTAINERS & CREDITS PARSING PIPELINE
--------------------------------------------------------------------------------------------------
Kernel Root Documentation (MAINTAINERS / CREDITS)
  │
  ├─ Git Operation: "R100" (Exact Rename) ──► No-op
  │
  ├─ Git Operation: "D" (Deleted)
  │  ├─ get_prior_tags(CS) ──► Queries old_vid tags via m_bridge_tag.view_get_multiple
  │  └─ close_prior_tags(CS) ─► Closes unreferenced prior tags via m_tag.update(vid_e=Old_VID)
  │
  └─ Git Operation: "A" / "M" / "R*" (Added / Modified / Content Rename)
     ├─ get_prior_tags(CS) (if M or R*)
     │
     ├── If MAINTAINERS -> MaintainerManager(CS):
     │     ├── MaintainerParser: Parses 3-line header blocks, section titles, and tags
     │     │     ├── Roles: P (Person), M (Maintainer), R (Reviewer)
     │     │     ├── Subsystem Metadata: L (Mailing List), W (Web), T (SCM Tree), S (Status)
     │     │     └── Patterns: F (Files), X (Excludes), N (Regex), K (Keywords)
     │     └── Relational Staging:
     │           ├── AST Nodes: m_ast.view [Rule 23]
     │           ├── Code Occurrence Tags: m_tag.set (Rule 12 order), m_tag_code, m_bridge_tag
     │           ├── Identities: m_maintainer_person (Table 20)
     │           ├── Subsystems: m_maintainer_section (Table 21)
     │           ├── Personnel Roles: m_maintainer_member (Table 22)
     │           └── Match Rules: m_maintainer_pattern (Table 23)
     │
     ├── If CREDITS -> CreditsManager(CS):
     │     ├── CreditsParser: Parses contributor blocks (N:, E:, W:, P:, D:, S:)
     │     └── Relational Staging:
     │           ├── Person Registry: m_maintainer_person (Table 20)
     │           └── Contributor Biographies: m_credits_entry (Table 25)
     │
     └─ close_prior_tags(CS) (if M or R*)
  │
  ▼ [Post-Processing: processing_maintainer_files(version) (main.py + maintainer_matcher.py)]
Subsystem File Ownership Resolution:
  ├── MaintainerMatcher: Compiles all active 'F:' and 'X:' patterns from m_maintainer_pattern
  ├── Evaluates globs against all files active in m_bridge_file for target release version
  └── Populates file ownership bridge: m_maintainer_file (Table 24: vid, fid, sec_id)
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

3. **Spatial Coordinate Mapping**:
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

### 2.2. Domain Tables (Tables 20–25)

| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **20** | `m_maintainer_person` | `(person_id, name, email)` | `("person_id",)` | `True` | Unique contributor registry deduplicated by email address and name. |
| **21** | `m_maintainer_section`| `(sec_id, vid_s, vid_e, name, status, scm_tree, web_page, mailing_list, ast_id)` | `("sec_id", "vid_s")` | `True` | Subsystem maintainer section definitions and official channels. |
| **22** | `m_maintainer_member` | `(sec_id, person_id, role_type, priority)` | `("sec_id", "person_id", "role_type")` | `False` | Subsystem personnel roles: 1=Maintainer (`M:`), 2=Reviewer (`R:`), 3=Patch author (`P:`), 4=Credit (`C:`). |
| **23** | `m_maintainer_pattern`| `(sec_id, pat_type, pattern, priority)` | `("sec_id", "pat_type", "pattern", "priority")` | `False` | Path matching rules: 1=File (`F:`), 2=Exclude (`X:`), 3=Regex (`N:`), 4=Keyword (`K:`). |
| **24** | `m_maintainer_file` | `(vid, fid, sec_id)` | `("vid", "fid", "sec_id")` | `False` | Resolved subsystem file ownership bridge populated in post-processing. |
| **25** | `m_credits_entry` | `(credit_id, vid_s, vid_e, person_id, web_page, pgp_key, description, snail_mail, ast_id)` | `("credit_id", "vid_s")` | `True` | Contributor biography records parsed from `CREDITS`. |

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

`maintainer_ast` tracks section and contributor evolution across kernel releases:

1. **Section Continuity**:
   If a subsystem section's title remains identical, its `sec_id` is preserved across versions. Changes to maintainer lists or file patterns emit an updated section record in `m_maintainer_section` with `vid_s = VID`, while the previous version's record is closed with `vid_e = Old_VID`.
2. **Prior Tag Transition (`match_prior_tag_transition`)**:
   Individual section tags that modify their patterns or members transition gracefully with `m_moved_tag(s_tag_id, tag_ref)`.
3. **Person Deduplication**:
   `m_maintainer_person` uses `no_duplicate=True` on `(person_id,)` with in-memory caching to guarantee that contributors maintaining multiple subsystems share a single canonical `person_id`.

---

## 5. Post-Processing File Matching (`MaintainerMatcher`)

To resolve file ownership across the entire kernel repository:
1. **Execution**: Triggered at Step 6.7 in `main.py:processing_maintainer_files(version)`.
2. **Pattern Matching**:
   - Compiles `F:` file patterns and `X:` exclude patterns into normalized globs.
   - Evaluates each active file path in `m_bridge_file` against all sections in priority order.
   - Specific directory patterns (e.g. `fs/ext4/`) take precedence over broad generic patterns (e.g. `fs/`).
3. **Bridge Population**:
   - Inserts resolved `(vid, fid, sec_id)` triplets into `m_maintainer_file`.
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
   `processing_maintainer_files()` aggregates thousands of file-to-subsystem mappings into single bulk `m_maintainer_file` operations to minimize database write transactions.
