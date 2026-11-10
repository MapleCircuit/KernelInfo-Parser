# Parser Subsystem Rules (`parser/`)

> [!TIP] Workflow Skill
> For full snapshot update workflows, parser test mappings, and golden regeneration commands, use `.agents/skills/parser-snapshot-update/SKILL.md`.

Always enforce these invariants when modifying any AST or file parser (`c_ast`, `asm_ast`, `kconfig_ast`, `rust_ast`, `maintainer_ast`, `raw_ast`, `git_ast`):

1. **Rule 12 (Tag Staging Order)**: Inside `with CS(REF_POS):`, `m_tag.set(None, code_hash, ast_ref, 0, 0)` MUST be the first stored operation so `tag_ref = ((m_tag.table_id, 0), OP_REF, (REF_POS, CS.route[-1]))` references `m_tag`. Stage `m_moved_tag.set(s_tag_id, tag_ref)` immediately after inside the same block when transitioning a prior tag.
2. **Rule 14 (Binary Digests)**: `m_tag.hash` is a 32-byte `BINARY(32)` SHA-256 digest (`compute_code_hash`). `m_ast_hash.hash` is a 16-byte `BINARY(16)` SHA-256 prefix (`compute_ast_hash`). `m_commit.commit_hash` is a 20-byte `BINARY(20)` SHA-1 digest.
3. **Rule 21 (Joined View Parameter Flattening)**: Pass `m_ast_container` children to `m_ast.view(...)` as flat positional scalars (`None, priority, type_id, ref_ast_id`), never as nested tuples/lists (which `is_data_unsafe()` treats as unresolved `OP_REF` tuples). When combined with `m_ast_include`, the include columns (`None, fnid_ref`) precede container scalars.
4. **Rule 23 (AST View Deduplication)**: Always create AST nodes with `m_ast.view(...)`, never `m_ast.set(...)` or `m_ast.get_set(...)`, so TableEngine (`m_ast_hash`) deduplicates structures.
5. **Encoding & Line Splitting**: Read source blobs as `latin-1` (1 char per byte) and split lines with `.replace("\r\n", "\n").split("\n")` (never `str.splitlines()`, which splits on `\x0c` form feeds and desynchronizes libclang line numbers).
6. **Immutable Zero-VID Tags & Exact Renames**: `"R100"` exact renames and `"D"` deletions stage no AST/tag rows. Tags are content-addressed; `m_map_ast.map_id` equals `tag_id` (`m_bridge_map` is never written).
7. **Translation Unit Hermeticity**: In `c_ast`, always clear `_tu_foreign_type_cache` via `clear_tu_caches()` before/after `c_ast_parse` and in teardown.
8. **Parser-First Features & Shared Pure Extractors (`webapp/API.md` §2.5, `core/API.md` §12)**: Features consumed by `webapp/` that require database writes or source/commit parsing belong in the parser pipeline (`main.py`, `core/DBLayout.py`, `core/backfills.py`), not in `webapp/`. Keep core extraction logic in pure functions under `parser/` (free of `TableEngine` / `GreatProcessor` state) so the parser persists rows into the database for parsed versions while `webapp/` can invoke the same pure extractor as a `"git"` fallback for revisions not yet parsed.
9. **Strict `db_engine` Routing**: Parsers and ingest modules (`cve_ingest.py`, `facts_ingest.py`, `release_stats.py`, `identity.py`, `maintainer_store.py`) must never accept raw database connections (`cnx`) or execute raw SQL strings (`cnx.cursor()`, `cur.execute()`). All persistence and relational reads must be delegated to formal methods on `BaseDBEngine`.

