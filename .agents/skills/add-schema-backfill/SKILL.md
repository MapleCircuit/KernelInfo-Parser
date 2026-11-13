---
name: add-schema-backfill
description: >-
  Workflow for adding or modifying database schemas in core/DBLayout.py, wiring database views
  in db_engine/views.py, and implementing derived-table backfills in core/backfills.py. Activate when
  adding tables, columns, database views, or historical parser backfill workers.
---

# Add or Modify Database Schema & Backfills

## 1. Targeted Spec Slice Lookup
Never read `core/API.md` from line 1. Read a section with `python3 .agents/scripts/spec_section.py core/API.md <section>` (e.g. `4`; TOC: `python3 .agents/scripts/toc.py --write` regenerates it):
- **Relational Schema Registry & Tables**: `core/API.md` (§4)
- **Data Staging, Routing & ChangeSets**: `core/API.md` (§5)
- **Derived Table Backfills**: `core/API.md` (§12)
- **TableEngine Interface & Caching**: `table_engine/TE_API.md` (§1, §4, §6)

## 2. Core Subsystem Fast Lookup & Test Map

| Component / Task | Source Files | Spec Reference | Targeted Unit Test |
| :--- | :--- | :--- | :--- |
| **Schema & Table Definition** | `core/DBLayout.py` | `core/API.md` §4, `core/DBLayout.py` module docstring | `python3 -m unittest unit_test.test_table_handling unit_test.test_te_db_integrity` |
| **TableEngine & ChangeSet** | `core/TableHandling.py`, `table_engine/` | `core/API.md` §5, `table_engine/TE_API.md` | `python3 -m unittest unit_test.test_table_engine` |
| **Database Views** | `db_engine/views.py` (registry), `core/SymbolRows.py`, `pipeline/db_setup.py` (`ensure_db_views` runs `db.ensure_views()`) | `core/API.md` §4 | `python3 -m unittest tests.test_symbol_rows` |
| **Derived Backfills** | `core/backfills.py`, `pipeline/backfill_steps.py` | `core/API.md` §12 | `python3 -m unittest tests.test_facts_ingest` |
| **Ingest State & Checkpoints** | `core/ingest_state.py`, `core/checkpoints.py` | `core/API.md` §4 | `python3 -m unittest tests.test_ingest_state` |

## 3. Step-by-Step Schema & Backfill Workflow
Enforce the invariants loaded from `core/AGENTS.md` (authoritative schema in `core/DBLayout.py`, pure extractor reuse, restartable backfills):

1. **Schema Declaration (`core/DBLayout.py`)**:
   - Update the `core/DBLayout.py` module docstring entity-relationship diagram with the new table / columns.
   - Define `m_<table_name> = Table(...)` with neutral `("name", ColumnSpec)` columns built from the `db_engine.spec` constructors (`INT(unsigned=True)`, `VARCHAR(255, bin_collation=True)`, `BIN(20)`, `TEXT(null=True)`, `auto=True`, `default=`), primary keys, and `te_cached` flag if needed. Never write backend DDL: each driver renders its own `CREATE TABLE` (`unit_test/test_ddl_render.py` pins the MariaDB text).
   - Register the table in `Table_Array` / `TABLES` list at the bottom of `core/DBLayout.py`.
2. **Database View Wiring (`db_engine/views.py`)**:
   - If the new table is exposed or joined via a SQL view, add/update its definition in the view registry of `db_engine/views.py`; `ensure_db_views()` in `pipeline/db_setup.py` creates the registered views through `db.ensure_views()`. Callers outside `db_engine/` never write view SQL.
3. **Derived Backfill Implementation (`core/backfills.py`)**:
   - Add backfill function with signature `def backfill_<name>(db, limit=None) -> int:`.
   - Ensure operations are idempotent (e.g. `insert_rows(..., on_conflict="ignore"|"update")` or checking existing processed IDs); use query specs and `BaseDBEngine` methods only (no SQL text, no cursors: `tests/test_db_boundary.py`).
   - Register the handler in `BACKFILL_HANDLERS` in `core/backfills.py`.
4. **Targeted Verification**:
   - Run: `python3 -m unittest unit_test.test_table_handling unit_test.test_table_engine`
   - Run the specific domain test (e.g. `python3 -m unittest tests.test_facts_ingest`).
5. **Documentation Sync**:
   - Update `core/API.md` (§4 for schema, §12 for backfills) in declarative present-tense.
6. **Final Gate**:
   - Run `python3 main.py -u` once.
