---
name: add-schema-backfill
description: >-
  Workflow for adding or modifying database schemas in core/DBLayout.py, wiring database views
  in main.py, and implementing derived-table backfills in core/backfills.py. Activate when
  adding tables, columns, database views, or historical parser backfill workers.
---

# Add or Modify Database Schema & Backfills

## 1. Targeted Spec Slice Lookup
Never read `core/API.md` from line 1. Jump directly to the relevant slice or run `python3 .agents/scripts/spec_section.py core/API.md <section>`:
- **Relational Schema Registry & Tables**: `core/API.md` (§4 `[L460-L647]`)
- **Data Staging, Routing & ChangeSets**: `core/API.md` (§5 `[L648-L807]`)
- **Derived Table Backfills**: `core/API.md` (§12 `[L1695-L1731]`)
- **TableEngine Interface & Caching**: `table_engine/TE_API.md` (§1 `[L18-L59]`, §4 `[L211-L530]`, §6 `[L551-L641]`)

## 2. Core Subsystem Fast Lookup & Test Map

| Component / Task | Source Files | Spec Reference | Targeted Unit Test |
| :--- | :--- | :--- | :--- |
| **Schema & Table Definition** | `core/DBLayout.py` | `core/API.md` §4 `[L460-L647]`, `DBLayout.py` [L1-93] | `python3 -m unittest unit_test.test_table_handling unit_test.test_te_db_integrity` |
| **TableEngine & ChangeSet** | `core/TableHandling.py`, `table_engine/` | `core/API.md` §5 `[L648-L807]`, `table_engine/TE_API.md` `[L18-L59]` | `python3 -m unittest unit_test.test_table_engine` |
| **Database Views** | `main.py` (`ensure_db_views`) | `core/API.md` §4 `[L460-L647]` | `python3 -m unittest tests.test_symbol_rows` |
| **Derived Backfills** | `core/backfills.py`, `main.py` | `core/API.md` §12 `[L1695-L1731]` | `python3 -m unittest tests.test_facts_ingest` |
| **Ingest State & Checkpoints** | `core/ingest_state.py`, `core/checkpoints.py` | `core/API.md` §4 `[L460-L647]` | `python3 -m unittest tests.test_ingest_state` |

## 3. Step-by-Step Schema & Backfill Workflow
Enforce the invariants loaded from `core/AGENTS.md` (authoritative schema in `core/DBLayout.py`, pure extractor reuse, restartable backfills):

1. **Schema Declaration (`core/DBLayout.py`)**:
   - Update lines 1–93 docstring entity-relationship diagram with the new table / columns.
   - Define `m_<table_name> = Table(...)` with explicit types, primary keys, and `te_cached` flag if needed.
   - Register the table in `Table_Array` / `TABLES` list at the bottom of `core/DBLayout.py`.
2. **Database View Wiring (`main.py`)**:
   - If the new table is exposed or joined via a SQL view, add/update the view definition inside `ensure_db_views()` in `main.py`.
3. **Derived Backfill Implementation (`core/backfills.py`)**:
   - Add backfill function with signature `def backfill_<name>(db, limit=None) -> int:`.
   - Ensure operations are idempotent (e.g. `ON DUPLICATE KEY UPDATE` or checking existing processed IDs).
   - Register the handler in `BACKFILL_HANDLERS` in `core/backfills.py`.
4. **Targeted Verification**:
   - Run: `python3 -m unittest unit_test.test_table_handling unit_test.test_table_engine`
   - Run the specific domain test (e.g. `python3 -m unittest tests.test_facts_ingest`).
5. **Documentation Sync**:
   - Update `core/API.md` (§4 for schema, §12 for backfills) in declarative present-tense.
6. **Final Gate**:
   - Run `python3 main.py -u` once.
