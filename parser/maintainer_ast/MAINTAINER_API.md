# Linux Kernel Maintainers & Credits Subsystem API Specification (`maintainer_ast`)

Authoritative architectural specification and relational staging reference for `parser/maintainer_ast/`
([maintainer_ast.py](parser/maintainer_ast/maintainer_ast.py),
[maintainer_parser.py](parser/maintainer_ast/maintainer_parser.py),
[credits_parser.py](parser/maintainer_ast/credits_parser.py),
[maintainer_matcher.py](parser/maintainer_ast/maintainer_matcher.py),
[maintainer_types.py](parser/maintainer_ast/maintainer_types.py)). Designed as an exhaustive reference for AI
agents extending, maintaining, or querying kernel subsystem ownership, maintainer personnel, and contributor
records.

## Table of Contents
- **§1**: High-Level Architecture & Pipeline Flow [L22-L71]
- **§2**: Relational Schema Mapping & ChangeSet Operations [L72-L150]
- **§3**: AST Node Classification & Tag Extraction [L151-L183]
- **§4**: Tag Lifecycle & Prior Tag Evolution [L184-L198]
- **§5**: Post-Processing File Matching (`MaintainerMatcher`) [L199-L249]
- **§6**: Performance Invariants & Architectural Rules [L250-L289]

---

## 1. High-Level Architecture & Pipeline Flow

The maintainer subsystem parses two specialized root kernel documentation files:
1. `MAINTAINERS` (File type `T_MAINTAINERS` = 6): Defines subsystem ownership, maintainers, reviewers, mailing
   lists, source code repositories, and file matching patterns (`F:`, `X:`).
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
     ├─ get_prior_tags(CS) (if M or R*: the shared 14-col m_bridge_tag -> m_tag -> m_ast join, tag_tracker.py)
     │
     ├── If MAINTAINERS -> MaintainerManager(CS):
     │     ├── MaintainerParser: Parses 3-line header blocks, section titles, and tags
     │     │     ├── Roles: M (Maintainer), R (Reviewer), P (Profile doc or Person)
     │     │     ├── Subsystem Metadata: L (Mailing List), W (Web), T (SCM Tree), S (Status), Q (Patchwork), B (Bug URL), C (Chat)
     │     │     └── Patterns: F (Files), D (Doc directories), X (Excludes), N (Regex), K (Keywords)
     │     └── Relational Staging:
     │           ├── AST Nodes: m_ast.view [Rule 23]
     │           ├── Code Occurrence Tags: tag_tracker.stage_entry_tag() -> m_tag (5-col, Rule 12 order), m_bridge_tag (6-col), m_map_ast (map_id = tag_id)
     │           ├── Identities: m_maintainer_person (Table 15)
     │           ├── Subsystems: m_maintainer_section (Table 16, 8-col with blob_ref)
     │           ├── Personnel Roles: m_maintainer_member (Table 17)
     │           └── Match Rules: m_maintainer_pattern (Table 18)
     │
     ├── If CREDITS -> CreditsManager(CS):
     │     ├── CreditsParser: Parses contributor blocks (N:, E:, W:, P:, D:, S:)
     │     └── Relational Staging:
     │           ├── Person Registry: m_maintainer_person (Table 15)
     │           └── Contributor Biographies: m_credits_entry (Table 20, 8-col with blob_ref)
  │
  ▼ [Post-Processing: processing_maintainer_files(version) (main.py + maintainer_matcher.py)]
Subsystem File Ownership Resolution (incremental, per section version):
  ├── Sections of the version and of its base: maintainer_store.blob_section_ids() (section 5)
  ├── Nothing new or changed and no new path -> logged, returns without parsing MAINTAINERS
  ├── MaintainerMatcher(changed sections) over every path; MaintainerMatcher(all) over new paths only
  └── Populates file ownership bridge: m_maintainer_file (Table 19: sec_id, fnid)
==================================================================================================
```

---

## 2. Relational Schema Mapping & ChangeSet Operations

`maintainer_ast` stages operations into standard AST/tagging tables (0–15) and subsystem domain tables
(15–20):

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
   CS.store(m_map_ast.set(tag_ref, 1, 1, rel_line_e, rel_char_e, ast_ref))  # map_id = tag_id (no m_bridge_map row)
   ```

### 2.2. Domain Tables (Tables 15–20, 34, 59)

| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **15** | `m_maintainer_person` | `(person_id, name, email)` | `("person_id",)` | `True` | Unique contributor registry deduplicated by email address and name. |
| **16** | `m_maintainer_section`| `(sec_id, blob_id, name, status, scm_tree, web_page, mailing_list, ast_id, patchwork, bug_url, chat_channel, profile_doc)` | `("sec_id",)` | `True` | Subsystem maintainer section definitions, official channels, patchwork, bug tracker, chat, and documentation paths, stored with the blob where the section last changed. Indexes `maint_sec_blob_idx (blob_id)`, `maint_sec_ast_idx (ast_id)`. |
| **17** | `m_maintainer_member` | `(sec_id, person_id, role_type, priority)` | `("sec_id", "person_id", "role_type")` | `False` | Subsystem personnel roles: 1=Maintainer (`M:`), 2=Reviewer (`R:`), 3=Patch author (`P:`), 4=Credit (`C:`). Index `maint_member_person_idx (person_id)`. |
| **18** | `m_maintainer_pattern`| `(sec_id, pat_type, pattern, priority)` | `("sec_id", "pat_type", "pattern", "priority")` | `False` | Path matching rules: 1=File (`F:`), 2=Exclude (`X:`), 3=Keyword (`K:`), 4=Regex (`N:`). |
| **19** | `m_maintainer_file` | `(sec_id, fnid)` | `("sec_id", "fnid")` | `False` | Files a section version covers, written by Step 6.7 (section 5). Index `maint_file_fnid_idx (fnid, sec_id)`. |
| **20** | `m_credits_entry` | `(credit_id, blob_id, person_id, web_page, pgp_key, description, snail_mail, ast_id)` | `("credit_id",)` | `True` | Contributor biography records parsed from `CREDITS`, stored with the blob where the entry last changed. Indexes `credits_blob_idx (blob_id)`, `credits_ast_idx (ast_id)`. |
| **34** | `m_ast_attribute` | `(ast_id, attr_type, attr_value)` | `("ast_id", "attr_type", "attr_value")` | `False` | Extended attributes attached to AST nodes (e.g. maintainer status, subsystem tags). |
| **59** | `m_person_identity` | `(person_id, identity_key)` | `("person_id",)` | `False` | Developer identity mapping grouping author and contributor `(name, email)` aliases into canonical personas (`parser/git_ast/identity.py`), indexed by `person_identity_key_idx (identity_key)`. |

### 2.3. Section versions of a blob (`maintainer_store.py`)

A section row is stored only when the section is new or changed (`MaintainerManager._extract_all()`), so an
unchanged section keeps the row of an earlier MAINTAINERS blob. The AST of a section is keyed by its name
(`m_ast.view(name, Maintainer_Section)`), so one `ast_id` names every stored version of the section.
`parser/maintainer_ast/maintainer_store.py` (SELECT only, shared by Step 6.7, `--backfill-maintainer-files`
and the webapp) picks the version a blob holds through the blob's tags (`m_bridge_tag -> m_tag -> ast_id`),
per lower-cased section name:

1. a row whose own blob places the same tag (identical text), the newest such row;
2. else the newest row stored with a blob no newer than this one (the section changed only in text the parser
   does not compare);
3. else the newest row.

`blob_credit_ids()` applies the same rule to CREDITS entries, one per (entry name, person).

---

## 3. AST Node Classification & Tag Extraction

### 3.1. MAINTAINERS Tag Classification
- **Section Headers**: Emits `ASTT.Maintainer_Section` (100) capturing the subsystem title (e.g. `"EXT4 FILE
  SYSTEM"`, `"ARM/APPLE MACHINE SUPPORT"`).
- **Maintainer Person (`P:`, `M:`, `R:`, `C:`)**: Emits `ASTT.Maintainer_Person` (101). Parsed into `(name,
  email)`. Ingested into `m_maintainer_person` with `person_id` reference linked in `m_maintainer_member`.
- **Patterns (`F:`, `X:`, `N:`, `K:`)**: Emits `ASTT.Maintainer_Pattern` (102). Slices file and exclude paths
  into `m_maintainer_pattern`.
- **Mailing Lists (`L:`)**: Emits `ASTT.Maintainer_List` (103).
- **Web Pages (`W:`)**: Emits `ASTT.Maintainer_Web` (104).
- **SCM Trees (`T:`)**: Emits `ASTT.Maintainer_Tree` (105).
- **Subsystem Status (`S:`)**: Emits `ASTT.Maintainer_Status` (106) with values: `Supported`, `Maintained`,
  `Odd Fixes`, `Orphan`, `Obsolete`.
- **Bug Reporting & Tracking (`B:`)**: Emits `ASTT.Maintainer_Bug` (196) and stored in `m_ast_attribute` on
  the section's AST node (`name='bug_url'`).
- **Chat & IRC Channels (`C:`)**: Emits `ASTT.Maintainer_Chat` (197) and stored in `m_ast_attribute` on the
  section's AST node (`name='chat_channel'`).
- **Patchwork (`Q:`)**: Emits `ASTT.Maintainer_Patchwork` (198) and stored in `m_ast_attribute` on the
  section's AST node (`name='patchwork'`).

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
   All `m_tag` records are immutable and content-addressable; tags are never closed with `vid_e` updates.
2. **Prior Tag Transition (`match_prior_tag_transition`)**:
   When modifying a file, `get_prior_tags` loads prior tags through the shared 14-column join (`m_bridge_tag`
   -> `m_tag` -> `m_ast`), and `match_prior_tag_transition` emits `m_moved_tag(s_tag_id, tag_ref)` if tags
   evolve.
3. **Person Deduplication**:
   `m_maintainer_person` uses `no_duplicate=True` on `(person_id,)` with in-memory caching to guarantee that
   contributors maintaining multiple subsystems share a single canonical `person_id`.

---

## 5. Post-Processing File Matching (`MaintainerMatcher`)

To resolve file ownership across the entire kernel repository:
1. **Execution**: Triggered at Step 6.7 in `main.py:processing_maintainer_files(version)`.
2. **Pattern Matching**:
   - Compiles `F:` file patterns, `X:` exclude patterns, and `N:` file regex patterns into normalized
     matchers.
   - Evaluates each active file path in the tree against all sections in priority order.
   - Specific directory patterns (e.g. `fs/ext4/`) take precedence over broad generic patterns (e.g. `fs/`).
   - Wildcard semantics follow `get_maintainer.pl`: a pattern without a trailing slash only matches paths with
     as many `/` as the pattern, so `drivers/net/*` covers the files of `drivers/net` but not
     `drivers/net/ethernet/...` (a trailing-slash pattern matches recursively).
   - Regex patterns (`N:`) test compiled regular expressions directly against candidate file paths.
   - Indexed lookup: exact paths and directory prefixes are dictionary lookups; sections with wildcard
     patterns are filed under the directory part of each wildcard's literal prefix (`drivers/net/3c505*` ->
     `drivers/net/`), so a file only tests the wildcard sections filed under its ancestor directories plus
     those whose wildcards or regexes can match anywhere (e.g. `*/net/*`, `N:`), still in section order.
     Sections without any `F:` or `N:` pattern are skipped (they cannot match). On v3.0 (36,783 files, 925
     sections) matching takes 0.5 s instead of 6.6 s with identical results.
   - Trailing-slash patterns with wildcards (`F: */` of THE REST, `drivers/*/phy/`) are prefix regexes, as
     `get_maintainer.pl`'s `m@^$pattern@` (`*` becomes `.*` and crosses `/`), so every path matches THE REST.
3. **Incremental mapping** (`main.processing_maintainer_files()`, `maintainer_store.map_files()`):
   - A section matches a path through its own patterns only, and a section version keeps its `sec_id` across
     MAINTAINERS blobs, so `(sec_id, fnid)` holds for every blob containing that version.
   - The sections of the version (`get_fid_for_path("MAINTAINERS")`, also when the file did not change) and of
     its base (`gp.Old_VID`) are read with `maintainer_store.blob_section_ids()`. Changed = this version's
     `sec_id`s the base does not hold. When none of the base's sections has a row yet (first run after the
     migration, or a gap), every section counts as changed.
   - New paths = paths of the version's tree (`MF.git_file_list()`) absent from the base's tree.
   - Nothing changed and no new path: one log line, no parse, no matcher (most stable updates).
   - Otherwise MAINTAINERS is parsed once; changed sections are matched against every path, all sections
     against the new paths only, and the `(sec_id, fnid)` rows are staged through the TableEngine (idempotent
     primary key).
   - The duration limit does not cut the matching short (a section matched against part of the tree would stay
     incomplete); it is checked before Step 6.7 starts.
   - Without SQL access (MockDB) sections are looked up by name in the TableEngine and every section counts as
     changed.
4. **Migration and backfill**: a database whose `m_maintainer_file` still has `maintainers_blob_id` gets the
   table dropped at start (`main.drop_outdated_derived_tables()`) and recreated empty. `python3 main.py
   --backfill-maintainer-files [--from <ref>]` refills it for the finished versions of the repository in
   ingestion order (`core/backfills.py`), with the same rule, MAINTAINERS and trees read from Git, one commit
   per version; it is skipped while an ingestion is interrupted.
5. **Webapp reader** (`webapp/backend/services/maintainer_service.py`): `resolve_subsystems_for_file()` and
   `match_maintainers()` read the sections of each path from `m_maintainer_file` (restricted to the version's
   sections) and order them with a matcher over only those sections, which gives the full matcher's order
   because sections match independently. A path without rows (not computed yet, unknown, an older database
   layout, a revision whose MAINTAINERS was never indexed) uses the full matcher. The section detail lists the
   section's files present in the version's tree.

---

## 6. Performance Invariants & Architectural Rules

1. **Rule 12 Staging Order**:
   In `MaintainerManager._stage_tag()` and `CreditsManager._stage_tag()`, `m_tag.set` MUST be the first
   operation inside `with CS(REF_POS):` so `tag_ref` references `m_tag`. Auxiliary tables (such as
   `m_moved_tag.set`) follow immediately within the block.
2. **Rule 23 View Deduplication**:
   All AST nodes (`_stage_ast_node`) use `m_ast.view(((m_ast.ast_id,),), None, name, type_id)`.
3. **Person Identity Normalization** (`maintainer_types.clean_identity_text`):
   Email addresses are stripped of surrounding angle brackets (`< >`), whitespace, and `mailto:` prefixes
   before deduplication. Both halves of the identity then pass through `clean_identity_text()`, which maps C0
   control characters and `DEL` to a space and collapses every run of (Unicode) whitespace to one, because
   `.strip()` alone leaves such bytes where they sit **inside** the text and each one silently forks a
   contributor into a second `person_id`:
   - commit `a929478f` signs off as `Olof Johansson\x01 <olof@lixom.net\x01>`, which also sorted differently
     under MySQL's collation than the same string does in Python;
   - `Ruslan  Sushko`, `K.  Y.  Srinivasan` and `Giuseppe\tGORGOGLIONE` appear beside their single-spaced
     spellings;
   - `Vitor\xa0Soares` (no-break space) is the same person as `Vitor Soares`.

   The C1 range (`\x80`–`\x9f`) is deliberately **not** treated as control characters: in this corpus those
   bytes are the tail of a UTF-8 sequence that was decoded as latin-1 (`Michał` arriving as `MichaÅ\x82`), so
   rewriting them would mangle the name further and destroy what a later mojibake repair needs.

   Applied at every point an identity is formed: `MaintainerPerson.__post_init__` and
   `CreditsEntry.__post_init__` (MAINTAINERS and CREDITS), and `main.py:_person_key()` for git commit authors,
   committers and trailer contributors. `_ingest_commit_range()` additionally prefers the row already stored
   in normalized form (lowest `person_id` among equals) when loading the existing registry, so rows written
   before normalization map onto the identity a merge would keep.
4. **Batch DB Commit**:
   `processing_maintainer_files()` stages every `(sec_id, fnid)` row of the version through the TableEngine,
   committed with the version's other rows.
5. **Preamble Boundary Invariant & Status Canonicalization**:
   Early Linux kernels (`v2.6.12`–`v2.6.30`) place the separator line before the `Maintainers List` heading,
   followed by instructional notes (`Note: For the hard of thinking...`) and example tag documentation (`P:
   Person`, `S: Status, one of the following:`). `MaintainerParser._find_preamble_end()` scans past preamble
   instructional text and unindented title candidates without email/status anchors to guarantee only genuine
   subsystem sections are staged. Section status values are canonicalized (`Odd fixes` &rarr; `Odd Fixes`,
   `Orphaned` &rarr; `Orphan`), and multi-line or bracketed `E:` lines in `CREDITS` are stripped of `<>`
   delimiters down to canonical bare email addresses.
