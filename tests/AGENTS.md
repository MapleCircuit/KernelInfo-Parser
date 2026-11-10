# Test Execution & Guardrails (`tests/`)

Guidelines for running and writing unit tests across the repository:

1. **Targeted Iterative Testing**:
   - During iterative development or debugging, execute only the targeted test module touched by the changes:
     `python3 -m unittest tests.<test_module>` (e.g. `python3 -m unittest tests.test_distributed_parsing`).
   - For webapp tests: `python3 -m unittest tests.test_webapp_system`.
   - Never run `python3 main.py -u` on every iteration—it runs 1,010 tests and takes ~5.5 minutes.

2. **Single Database / Tunnel Isolation**:
   - Web application and database tests initialize an SSH tunnel to the database on default port 3308.
   - Never run multiple test suites or background commands that initialize database connections concurrently; wait for active runs to exit before dispatching another to avoid port collisions and socket resets.

3. **Subagent Test Handoff**:
   - If a delegated subagent has already executed `python3 main.py -u` and verified clean test suite completion in its turn, the parent agent must not re-run the full 5-minute suite. Verify git status/diff and summarize directly.

4. **Snapshot & Golden Testing**:
   - Tests touching C-AST golden snapshots (`tests/test_cast_golden.py`) must adhere to `parser-snapshot-update` skill guidelines.
   - When golden snapshots change, inspect JSON diffs before updating fixtures.

