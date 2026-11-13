# Core Subsystem Rules (`core/`)

> [!TIP] Workflow Skill
> For adding tables, backfills, and derived view pipelines, use `.agents/skills/add-schema-backfill/SKILL.md`.

Always enforce these invariants when modifying `core/`:

1. **Canonical Database Schema Authority (`core/DBLayout.py`)**:
   - `core/DBLayout.py` is the single authoritative source of truth for all 65 tables (`Table_Array` / `TABLES`).
   - The module docstring contains the complete relational entity-relationship graph across git DAG, content-addressable blobs, code tags, symbols, Kconfig, maintainers, and distributed tasks.
   - When adding or altering a table, update the docstring graph, the `Table(...)` definition, and the table's registration in `Table_Array` / `TABLES`.
2. **Database View & Migration Lifecycle (`pipeline/db_setup.py`)**:
   - Tables in `Table_Array` are auto-created by the engine.
   - Joined database views (e.g., `m_symbol_def`, `m_symbol_ref`) are registered in `db_engine/views.py` and created by `db.ensure_views()` (called from `pipeline/db_setup.py` `ensure_db_views`). Never issue manual ad-hoc DDL or SQL text in feature code (`tests/test_db_boundary.py`).
3. **Derived Table Backfills (`core/backfills.py`)**:
   - Derived facts computed across commits or blobs belong in `core/backfills.py` registered in `BACKFILL_HANDLERS`.
   - Handlers must support restartability, idempotence, and progress logging.
4. **TableEngine & ChangeSet Staging (`core/TableHandling.py`, `table_engine/`)**:
   - Stage operations inside `with CS(REF_POS):`. Tag staging (`m_tag.set`) must precede references.
   - Deduplicate AST nodes using `m_ast.view(...)`, never `m_ast.set(...)` or `m_ast.get_set(...)`.
   - Never mutate `Table` column schema tuples at runtime.
5. **Pure Extractor Reuse**:
   - Parsers feeding `m_blob_facts` (calls/ops tables `1`, Kernel API `2`, includes `3`, Kconfig definitions `4`, doc labels `5`, man pages `6`) must keep extraction logic in pure functions under `parser/` so `core/` persists them during ingestion and `webapp/` can invoke them for unparsed revisions.
6. **Distributed Multi-Computer Invariants (`core/distributed.py`, `main.py`)**:
   - **Sequence block leasing**: Always lease primary key ID blocks using `db.reserve_id_block(table, block_size, commit_id)` in distributed mode (`G.distributed == True`, e.g. in `_ingest_commit_range` for `m_commit` and `m_maintainer_person`); never use raw `db.get_next_id()`.
   - **Active worker states**: In `_previous_ingest`, check `m_parse_node.heartbeat_at >= now - 180` before treating `m_ingest_state.status == INGEST_PARSING` as interrupted to prevent premature rollback of in-flight peer runs.
   - **Dependency unlocking**: Master unlocks in `finish_version_ingest()`; Workers unlock exclusively in `mark_task_done()` after verifying lease validity.
   - **Delegable task updates**: `publish_delegable_chains()` must conditionally assign `commit_id` and `base_commit_id` (`IF(VALUES(...) > 0, VALUES(...), ...)`) to preserve resolved IDs.
   - **Heartbeat daemon propagation**: Propagate `role` and `network_id` into `NodeHeartbeatDaemon` to maintain node registration on heartbeat renewals.
7. **Documentation & Tests**:
   - Update `core/API.md` in declarative present-tense (§4 for schema, §5 for ChangeSet routing, §11 for distributed, §12 for backfills).
   - Run targeted unit tests:
     - `python3 -m unittest unit_test.test_table_handling unit_test.test_table_engine`
     - `python3 -m unittest tests.test_symbol_rows`
     - `python3 -m unittest tests.test_facts_ingest tests.test_ingest_state`
     - `python3 -m unittest tests.test_distributed_parsing`
   - Final validation gate: `python3 main.py -u`.
8. **Strict `db_engine` Encapsulation (`db_engine/base.py`)**:
   - Never access raw driver connection attributes (such as `db.cnx`, `cnx.cursor()`, or `cur.execute()`) or issue direct SQL strings outside `db_engine/`.
   - All persistence, queries, transactions, deletes, and state checks in `core/` (`ingest_state.py`, `checkpoints.py`, `distributed.py`, `backfills.py`) must route through dedicated methods on `BaseDBEngine`.

