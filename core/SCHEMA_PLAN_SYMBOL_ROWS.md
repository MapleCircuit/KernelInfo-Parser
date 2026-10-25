# Plan: store symbol definitions and references once per tag

Status: implemented (`core/SymbolRows.py`, tables 41–44 in `core/DBLayout.py`); see §7 for what the
implementation changed against this plan and what it measured. Schema reference: `core/API.md` (table catalogue).

## 1. Problem

A changed file is a new blob, and `m_symbol_ref`, `m_symbol_def` and `m_bridge_tag` are keyed by blob, so every
row of every tag of that file is written again, including the tags (code snippets) that did not change.

Measured on the database after `--reset` + v2.6.12-rc2 … v2.6.12 (read-only queries, October 2026):

| Version step | New blobs | `m_symbol_ref` rows (per blob) | in unchanged tags | `m_symbol_def` rows | in unchanged tags | `m_bridge_tag` rows | unchanged tags |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rc5 → rc6 | 332 | 227,868 (686) | 85% | 31,423 | 81% | 50,644 | 87% |
| rc6 → v2.6.12 | 201 | 110,433 (549) | 92% | 18,420 | 72% | 28,325 | 82% |

- These three tables were 157k of the 216k rows v2.6.12 committed (73%). `m_symbol_ref` is already the largest
  table (5.5M rows, 737 MB with indexes after 6 versions).
- For 941 versions this is most of the database growth and most of each small version's commit time.

## 2. What can be shared (measured on v2.6.12, all 201 new blobs)

| Candidate | Ref rows that could be shared |
| :--- | ---: |
| A. Whole resolved ref list per tag (`ast_id`, `target_fnid`, role, position) | 41,105 (37%) |
| B. Ref positions and roles per tag, resolution kept separately | 100,389 (91%) |

- A shares little because a reference stores the **target's definition AST id**, which is content-addressed: when a
  referenced struct or function changes anywhere, every unchanged tag that uses it gets new ref rows (38% of
  reused tags in a sample differed only in `ast_id`).
- Definitions are stable: 96.5% of sampled reused tags have identical definition rows relative to the tag start.
- A blob references 129 distinct targets on average, against 549 reference rows.
- 11.6% of reference rows lie outside the lines of the tag they are attributed to (`last_tag` in
  `parser/c_ast/cs_extractor.py`), so their offset from that tag is not stable; they need a proper owner (§4.1).

Recommendation: candidate B.

## 3. Proposed schema

| Table | Columns | Key / indexes | Written |
| :--- | :--- | :--- | :--- |
| `m_tag_def` | `(tag_id, def_idx, ast_id, name, type_id, linkage, rel_line_s, rel_line_e)` | PK `(tag_id, def_idx)`; idx `(name)`, `(ast_id)` | once per new tag |
| `m_tag_ref` | `(tag_id, ref_idx, sym_id, role, rel_line, char_s)` | PK `(tag_id, ref_idx)`; idx `(sym_id, tag_id)` | once per new tag |
| `m_blob_symbol` | `(blob_id, sym_id, target_ast_id, target_fnid)` | PK `(blob_id, sym_id)`; idx `(target_ast_id, blob_id)` | per new blob, one row per distinct referenced symbol |
| `m_bridge_tag` | unchanged `(blob_id, tag_id, line_s, line_e, char_s, char_e)` | add idx `(tag_id, blob_id)` | per new blob (positions shift) |

- `sym_id` is the resolution-independent symbol key: the `(name, type)` AST node the parser already creates with
  `m_ast.view(((m_ast.ast_id,),), None, name, type)`, stable across versions.
- `rel_*` lines are relative to the tag's `m_bridge_tag.line_s` in each blob.
- The absolute values of today are recovered as `line = bt.line_s + rel_line` and
  `ast_id = m_blob_symbol.target_ast_id` joined on `(bt.blob_id, r.sym_id)`.
- `m_symbol_def` and `m_symbol_ref` stay available as views (the way `ensure_db_views()` in `main.py` already
  serves `m_bridge_file` and `m_file`). Their `def_id` / `ref_id` become composite ids
  (`blob_id`, `tag_id`, index packed into one BIGINT), so the webapp must treat them as opaque.

Estimated rows for v2.6.12 with this schema:

| Table | Today | Planned |
| :--- | ---: | ---: |
| `m_symbol_ref` | 110k | `m_tag_ref` about 10k (new tags only) + `m_blob_symbol` 26k |
| `m_symbol_def` | 18k | `m_tag_def` about 5k |
| `m_bridge_tag` | 28k | 28k |
| All three | 157k | about 69k (−56%) |
| Whole version | 216k | about 128k (−41%) |

## 4. Changes

### 4.1 Parser (`parser/c_ast/`, `parser/rust_ast/rust_ast_type.py`)
- `cs_extractor.py` and `ast_nodes.py` stage references with `m_symbol_ref.set(...)`; `cursor_tree.py` stages
  definitions at seven sites. They become:
  - per tag: definition / reference items with offsets from the tag's first line;
  - per file: one `m_blob_symbol` entry per distinct referenced symbol, deduplicated in the ChangeSet, whose
    target is the existing `CS.ref(...)` (resolved by the parent's `resolve_ref()` exactly as today).
- References that are outside every tag get an owner that brackets them: the enclosing top-level tag when there
  is one, otherwise a per-file "file scope" pseudo-tag, so offsets stay stable.

### 4.2 Execution and TableEngine
- Tag items are written only when the tag itself is new. The execution already knows this: `m_tag` is
  content-addressed and an existing tag returns its id.
- `m_blob_symbol` rows are written for each new blob, whether or not its tags are new: resolution depends on the
  blob's includes, not on the tag.
- No new hashing table is needed: tag reuse comes from `m_tag`.
- The parent stages about 40% fewer rows per small version. Workers parse the same files as before.

### 4.3 Webapp (15 queries in `webapp/backend/services/`)

| Service | Use | Plan |
| :--- | :--- | :--- |
| `symbol_service.py` | defs by name/prefix in a tree; picks by `def_id`; refs by target `ast_id` | `m_tag_def(name)` → `m_bridge_tag(tag_id, blob_id ∈ tree)`; refs via `m_blob_symbol(target_ast_id, blob_id)` → `m_tag_ref(sym_id, tag_id)` |
| `callgraph_service.py` | callers (refs inside a def's tag); callees by `ast_id` | refs of a tag: `m_tag_ref(tag_id)` + `m_blob_symbol` of the caller's blob |
| `history_service.py` | `is_def` per (blob, tag); defs across versions | `EXISTS m_tag_def(tag_id)` (no blob needed) |
| `diff_service.py` | defs per blob | `m_bridge_tag(blob)` ⋈ `m_tag_def` |
| `tree_resolver.py` | docs/notes on content-addressed defs | wording only |

- Each query gets an `EXPLAIN` check on a real database before it switches. The views keep the others working
  meanwhile.
- `webapp/API.md` documents the result shapes, which stay the same apart from the opaque ids.

## 5. Phases

0. **Measure on more data (read-only)**: repeat §1–§2 on a full release step (e.g. v2.6.12 → v2.6.13). Confirm
   definition stability and the per-blob distinct-symbol count, and size the out-of-tag references.
1. **Schema and dual write**: add the tables and indexes, and write both layouts behind a flag.
   Integrity check: the views over the new tables equal the old tables row for row (for the 11.6% out-of-tag
   references, after the owner fix).
2. **Parser and execution switch**: new layout only. The old tables become views.
3. **Webapp**: queries move to the new tables where `EXPLAIN` shows the views are slow; `def_id` / `ref_id` become
   opaque.
4. **Measure**: a `--reset -p` run over v2.6.12-rc2 … v2.6.13, comparing commit time, rows per version and
   database size with today.

The schema changes the meaning of stored rows, so a database is rebuilt with `--reset` rather than migrated.

## 6. Risks and open questions

- **Read cost**: "every reference to X in version V" becomes `m_blob_symbol` → `m_bridge_tag` → `m_tag_ref`,
  three indexed steps instead of one, but over far fewer rows. It needs the `(target_ast_id, blob_id)` and
  `(sym_id, tag_id)` indexes.
- **Tag identity**: tag reuse assumes a tag id implies identical definitions and reference positions. 96.5%
  (definitions) and 91% (reference rows) hold today; phase 0 must explain the remainder (e.g. macro context,
  `#ifdef` state) before relying on it, or keep a per-blob fallback for tags whose items differ.
- **Rust and other parsers** stage `m_symbol_def` too (`parser/rust_ast/rust_ast_type.py`); they move in the same
  phase.
- **Out of scope**: `m_bridge_checkpoint` grows by every file of every version (18k–60k rows per version). That
  is a separate delta-encoding question.

## 7. Implementation notes

- **Keys** (`_KeyContext.ref_key()` / `def_key()` in `core/SymbolRows.py`): a digest of the tag's text with the
  identifier at the reference and its role, or with the definition's name and type.
  - The first implementation described the target instead (its `REF_FILE` route or the operation creating it). The
    first `--reset -p` run showed that description changes once the database holds the previous version:
    `CS.ref()` then resolves many targets to AST ids at parse time. 84–89% of reused header placements and
    34–39% of `.c` placements failed the comparison with their stored items and were written per blob (977k of the
    1.03M `m_blob_symbol_ref` rows after six versions).
  - Measured on 400 blobs, (identifier, role, tag) means two targets for 3.5% of references; those keys are
    refined by the item's position in the tag. A blob holds about 163 keys for 328 references (2x fewer
    `m_blob_symbol` rows than references).
  - `tests/test_symbol_rows.py` parses the second version with every positional target resolved to an id.
- **Out-of-tag items**: references were flushed to the next tag created, often one past them (42% of `kernel/fork.c`
  references lay outside their tag). They are now attributed to the innermost tag containing their line, in both
  layouts, which changes `tag_id` of those rows (golden snapshots regenerated for that). Only items no tag contains
  stay per blob.
- **Reused tags** are compared with their stored items (`load_prior_items()` in `get_prior_tags()`); a mismatch stores
  that placement per blob and marks it in `m_symbol_own`.
- **Bug found on the way**: `get_rel_file_for_cursor(None, …)` fell back to the v3.0 workspace, so foreign types
  outside v3.0 got paths like `slab_def.h` (fixed; `parser/c_ast/CAST_API.md` §9.3).
- **Validation**: 10 file pairs (v2.6.12→v2.6.13, v2.6.39→v3.0, v2.6.28→v2.6.29) give identical view rows in both
  layouts; the SQL views returned the same rows as `symbol_view_rows()` on a scratch MariaDB database.
  `tests/test_symbol_rows.py` repeats the layout comparison on two files.
- **Measured rows written by a file's second version** (symbol tables only, blob → tag layout):
  `fs/namei.c` 2,761 → 903, `mm/memory.c` 1,537 → 788, `kernel/fork.c` 1,244 → 929, `kernel/sched.c` 3,978 → 3,145
  (v2.6.12 → v2.6.13); `net/ipv4/tcp.c` 3,658 → 1,293 (v2.6.39 → v3.0). A file's first parse writes somewhat
  more than before (tag rows plus `m_blob_symbol`), so the initial import gains nothing; RC-sized steps gain most.
- **Webapp reads** (after six versions): filtering the views by blob or name is instant, by `ast_id` it is not
  (28.7s for the references of `kmalloc`, which `rows_of_targets_sql()` returns in 2.8s); the reference, caller and
  callee queries use it, and the `is_def` flag of the history anchor reads the blob's definition tags once.
- **Remaining**: phase 4 (a `--reset -p` run to measure commit time and database size with the new keys).
