# Test Execution & Guardrails (`tests/`)

Guidelines for running and writing unit tests across the repository:

1. **Targeted Iterative Testing** (runner: `unit_test/runner.py`, spec `core/API.md` §5.5):
   - Iterate with `python3 main.py -u --changed --summary` (affected modules via `python3 .agents/scripts/affected_tests.py [paths]`, one failure per line, tracebacks in the printed log) or `python3 main.py -u --modules tests.test_x --summary`; `python3 -m unittest tests.<test_module>` still works for a single module.
   - Add `--no-live-db` when no MariaDB is reachable; `--timeout S` bounds each module (default 300 s, hung module = error).
   - Run the full `python3 main.py -u` (about 1,600 tests, ~3.5 minutes isolated plus the C-AST regression of plain `-u`) once as the final gate, never per iteration.
   - A global change (`core/DBLayout.py`, `db_engine/base.py`, `tests/db_fixtures.py`, ...) makes `--changed` run the full list.
   - New test modules: add them to `DEFAULT_MODULES` in `unit_test/runner.py` (or to `EXCLUDED_MODULES` with a reason); `tests/test_test_runner.py` fails for an unregistered `test_*.py`.
   - Generated docs: `tests/test_doc_toc.py` fails on a stale spec TOC (`python3 .agents/scripts/toc.py --write <spec>`), a stale `db_engine/API_SURFACE.md` / `cache_engine/API_SURFACE.md` (`python3 .agents/scripts/api_surface.py --write`), a hand-written line range in a doc, or a `<spec> §N` reference without a matching heading.
   - Tests that read the real MariaDB carry `@requires_live_db("reason")` from `tests/db_fixtures.py` (method, class, or `require_live_db()` call); they skip with one line when the database is unreachable or under `--no-live-db`. `tests.require_indexed_ref(ref)` includes this check.

2. **Single Database / Tunnel Isolation**:
   - Web application and database tests initialize an SSH tunnel to the database on default port 3308.
   - Never run multiple test suites or background commands that initialize database connections concurrently; wait for active runs to exit before dispatching another to avoid port collisions and socket resets.

3. **Subagent Test Handoff**:
   - If a delegated subagent has already executed `python3 main.py -u` and verified clean test suite completion in its turn, the parent agent must not re-run the full suite. Verify git status/diff and summarize directly.

4. **Snapshot & Golden Testing**:
   - Tests touching C-AST golden snapshots (`tests/test_cast_golden.py`) must adhere to `parser-snapshot-update` skill guidelines.
   - When golden snapshots change, inspect JSON diffs before updating fixtures.


5. **Database Fixtures (`tests/db_fixtures.py`)**:
   - The one home of test database helpers: `make_db("mock"|"sqlite", tables=..., rows=...)` (private in-memory database, optionally filled), `patch_reader(db)` (serves the webapp `get_reader()` from `db`; nests, any stop order) and `DistDB` (distributed-parsing coordination tables). Do not hand-write read-session doubles for what a `MockDB` row set can answer: `MockDB` evaluates registry views (`m_symbol_def`, `m_checkpoint_tree`) and creates tables on first use (`db_engine/DB_API.md` §2.15 "MockDB").
   - Assert on stored rows through `MockDB.store` (`{primary key: row}` per table) or the public read API, never on engine internals.
