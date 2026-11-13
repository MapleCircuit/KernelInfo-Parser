"""Database boundary enforcement: only `db_engine/` touches the database.

Scans every git-tracked or new untracked (not ignored) `.py` file outside `db_engine/` (AST based; `tool/`, `repos/` and `__pycache__`
are not tracked / excluded).

Rules
-----
1. Driver imports (`sqlite3`, `mysql`, `mysql.connector`, `pymysql`, `MySQLdb`, `psycopg*`) are forbidden
   everywhere outside `db_engine/`.  Exception: `sqlite3` inside `cache_engine/` (local cache files).
2. Concrete engine internals are never imported outside `db_engine/`: `db_engine.DBHandling` (and the `db_engine.mariadb_*` mixins),
   `db_engine.mock_db`, `db_engine.sqlite_db`, `db_engine.pool`, `db_engine.sqlite_store`, `db_engine.foreign`.
   Production code uses the factory / public base API (`db_engine.get_db_engine`, `BaseDBEngine`, the spec
   helpers).  It must not name the concrete classes `MariaDB`, `MockDB`, `SQLiteDB` at all.
   Tests may import `MockDB`, `SQLiteDB`, `SQLiteStore`, the foreign-SQLite helpers and `MariaDB` (opt-in
   live tests) from the PUBLIC `db_engine` package as fixtures; they may not reach into the submodules.
3. Production code (`main.py`, `pipeline/`, `core/`, `parser/`, `webapp/`, `table_engine/`, `cache_engine/`) contains no
   `.cnx`, `.cursor(`, `.execute(` / `.executemany(` / `.fetchone(` / `.fetchall(`, no `.query(` /
   `.query_one(` / `.query_iter(`, no SQL statement text in string literals (statements starting with
   SELECT/INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/REPLACE/WITH followed by SQL-ish tokens, plus
   `information_schema`, `ON DUPLICATE KEY`, `INSERT IGNORE`) and no `isinstance(..., MockDB|MariaDB|SQLiteDB)`.
   `cache_engine/` may use its own sqlite3 handles (it IS an engine), so the cursor/SQL rules are not applied
   to `cache_engine/sqlite_cache.py`.
4. Tests (everything outside the production tree) must not use driver objects either: the cursor/connection
   attribute rule (`.cnx`, `.cursor(`) and the `isinstance(..., MariaDB)` rule apply to them, except in the
   allowlisted db_engine-internals test modules (they fake drivers on purpose).

Allowlist: `ALLOW` below, keyed by (file, rule) with a reason.  Keep it minimal.
"""
from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRODUCTION_ROOTS = ("main.py", "pipeline/", "core/", "parser/", "webapp/", "table_engine/", "cache_engine/")
EXCLUDED_PREFIXES = ("db_engine/", "tool/", "repos/")

DRIVER_MODULES = ("sqlite3", "mysql", "pymysql", "MySQLdb", "psycopg", "psycopg2", "psycopg3")
ENGINE_INTERNAL_MODULES = (
    "db_engine.DBHandling",
    "db_engine.mariadb_connection",
    "db_engine.mariadb_tables",
    "db_engine.mariadb_indexes",
    "db_engine.mariadb_views",
    "db_engine.mariadb_spec",
    "db_engine.mariadb_support",
    "db_engine.mock_db",
    "db_engine.sqlite_db",
    "db_engine.pool",
    "db_engine.sqlite_store",
    "db_engine.foreign",
    "db_engine.sql_render",
    "db_engine.read_intents",
    "db_engine.views",
)
CONCRETE_NAMES = {"MockDB", "MariaDB", "SQLiteDB"}
# concrete classes a TEST may import from the public package (fixtures)
TEST_FIXTURE_NAMES = {"MockDB", "SQLiteDB", "MariaDB", "MockReadSession", "SQLiteReadSession", "ReadSession",
                      "SQLiteStore", "StoreError", "TableSpec", "ForeignReader", "ForeignWriter",
                      "create_foreign_sqlite", "open_foreign_sqlite", "get_db_reader"}

# (file, rule) -> reason.  Rules: driver-import, engine-internal, raw-call, sql-text, concrete-isinstance,
# concrete-name
ALLOW: dict[tuple[str, str], str] = {
    # db_engine-internals test modules fake / exercise the real drivers on purpose
    ("unit_test/test_db_engine.py", "driver-import"): "fakes mysql.connector to test the MariaDB driver",
    ("unit_test/test_db_engine.py", "engine-internal"): "tests driver internals",
    ("unit_test/test_db_engine.py", "raw-call"): "fake cursors for the MariaDB driver",
    ("unit_test/test_sqlite_store.py", "engine-internal"): "tests driver internals",
    ("unit_test/test_db_spec_ext.py", "driver-import"): "maps mysql.connector errors to neutral errors; sqlite3 oracle",
    ("unit_test/test_db_spec_ext.py", "engine-internal"): "spec rendering / error translation tests",
    ("unit_test/test_ddl_render.py", "driver-import"): "executes rendered SQLite DDL against sqlite3",
    ("unit_test/test_cache_engine.py", "driver-import"): "inspects the sqlite cache files",
    ("unit_test/test_db_spec_ext.py", "raw-call"): "fake driver connections for the error-translation tests",
    ("unit_test/test_ddl_render.py", "engine-internal"): "DDL rendering tests",
    # production false positives
    ("core/TableHandling.py", "raw-call"): "ChangeSet `foreign_cs.execute()` (file/blob change set), not a DB cursor",
    ("pipeline/changesets.py", "raw-call"): "ChangeSet `CS.execute()` (file/blob change sets), not a DB cursor",
    ("pipeline/scheduler.py", "raw-call"): "ChangeSet `CS.execute()` (file/blob change sets), not a DB cursor",
    ("pipeline/unchanged.py", "raw-call"): "ChangeSet `CS.execute()` (file/blob change sets), not a DB cursor",
}

RAW_ATTR_CALLS = {"execute", "executemany", "fetchone", "fetchall", "cursor", "query", "query_one", "query_iter"}
RAW_ATTRS = {"cnx"}

_SQL_RE = re.compile(
    r"^\s*(?:"
    r"SELECT\s+(?:[\w\*`\(\.]|DISTINCT\b|COUNT\b)[^;]*?\bFROM\b"
    r"|INSERT\s+(?:IGNORE\s+|OR\s+\w+\s+)?INTO\s+\S+"
    r"|UPDATE\s+\S+\s+SET\b"
    r"|DELETE\s+FROM\s+\S+"
    r"|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMPORARY\s+)?(?:TABLE|VIEW|INDEX|UNIQUE)\b"
    r"|DROP\s+(?:TABLE|VIEW|INDEX)\b"
    r"|ALTER\s+TABLE\b"
    r"|REPLACE\s+INTO\b"
    r"|WITH\s+\w+\s+AS\s*\("
    r")",
    re.DOTALL,  # statement keywords are upper case in code; prose like "Select database backend" must not match
)
_SQL_FRAGMENTS = re.compile(r"information_schema|ON\s+DUPLICATE\s+KEY|INSERT\s+IGNORE", re.IGNORECASE)


def _tracked_py() -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    files = []
    for line in out.splitlines():
        if line.startswith(EXCLUDED_PREFIXES) or "__pycache__" in line:
            continue
        if (ROOT / line).exists():
            files.append(line)
    return sorted(files)


def _is_production(rel: str) -> bool:
    return rel == "main.py" or rel.startswith(PRODUCTION_ROOTS)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _joined_text(node: ast.JoinedStr) -> str:
    return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "?" for v in node.values)


def _is_driver(mod: str) -> bool:
    top = mod.split(".")[0]
    return any(top == d or top.startswith("psycopg") for d in DRIVER_MODULES)


def scan_file(rel: str) -> list[tuple[str, str]]:
    """Return (rule, detail) violations for one file (before the allowlist)."""
    return scan_source(rel, (ROOT / rel).read_text())


def scan_source(rel: str, src: str) -> list[tuple[str, str]]:
    tree = ast.parse(src, filename=rel)
    prod = _is_production(rel)
    in_cache = rel.startswith("cache_engine/")
    found: list[tuple[str, str]] = []
    docs = _docstring_nodes(tree)
    for node in ast.walk(tree):
        # ---- imports
        if isinstance(node, ast.Import):
            for a in node.names:
                if _is_driver(a.name) and not (in_cache and a.name == "sqlite3"):
                    found.append(("driver-import", f"{node.lineno}: import {a.name}"))
                if a.name.startswith(ENGINE_INTERNAL_MODULES):
                    found.append(("engine-internal", f"{node.lineno}: import {a.name}"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mod = node.module
            if _is_driver(mod) and not (in_cache and mod == "sqlite3"):
                found.append(("driver-import", f"{node.lineno}: from {mod} import ..."))
            if mod.startswith(ENGINE_INTERNAL_MODULES):
                found.append(("engine-internal", f"{node.lineno}: from {mod} import ..."))
            if mod == "db_engine":
                for a in node.names:
                    if a.name in CONCRETE_NAMES and prod:
                        found.append(("concrete-name", f"{node.lineno}: from db_engine import {a.name}"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is None:
            pass
        # ---- isinstance(x, <Concrete>)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"
                and len(node.args) == 2):
            names = {n.id for n in ast.walk(node.args[1]) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(node.args[1]) if isinstance(n, ast.Attribute)}
            if names & CONCRETE_NAMES and (prod or "MariaDB" in names):
                found.append(("concrete-isinstance", f"{node.lineno}: isinstance(..., {sorted(names & CONCRETE_NAMES)})"))
        # ---- raw handles / calls
        if in_cache and rel.endswith("sqlite_cache.py"):
            continue
        if isinstance(node, ast.Attribute):
            if node.attr in RAW_ATTRS:
                found.append(("raw-call", f"{node.lineno}: .{node.attr}"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in RAW_ATTR_CALLS and (prod or node.func.attr == "cursor"):
                found.append(("raw-call", f"{node.lineno}: .{node.func.attr}("))
        # ---- SQL text in string literals (production only)
        if prod:
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = _joined_text(node)
            if text and (_SQL_RE.match(text) or _SQL_FRAGMENTS.search(text)):
                found.append(("sql-text", f"{getattr(node, 'lineno', 0)}: {text.strip()[:60]!r}"))
    return found


# rule -> the replacement API and the db_engine/DB_API.md section (by name) shown in failure messages
RULE_HINTS = {
    "driver-import": "route the access through G.DB / BaseDBEngine - see DB_API.md 'Encapsulation & Query Authority Contract'",
    "engine-internal": "import from the public `db_engine` package (get_db_engine, spec helpers) - see DB_API.md 'Core Drivers & Subsystem Components'",
    "concrete-name": "use get_db_engine() / BaseDBEngine instead of a concrete engine class - see DB_API.md 'Factory Resolver'",
    "concrete-isinstance": "test a capability (`'cap' in db.capabilities`) instead of the engine class - see DB_API.md 'Capability Flags'",
    "raw-call": "use select_rows / insert_rows / update_rows / delete_rows - see DB_API.md 'Spec Read Primitives' and 'Spec Write Primitives'",
    "sql-text": "express the query as a Select / Where spec (select_rows, copy_rows) - see DB_API.md 'Declarative Query Specification Primitives'",
}


def _hint(rule: str) -> str:
    return RULE_HINTS.get(rule, "see DB_API.md 'Encapsulation & Query Authority Contract'")


def _effective(rel: str) -> list[tuple[str, str]]:
    return [(r, d) for r, d in scan_file(rel) if (rel, r) not in ALLOW]


class TestDbBoundary(unittest.TestCase):
    def test_files_are_scanned(self):
        files = _tracked_py()
        self.assertGreater(len(files), 100)
        self.assertIn("main.py", files)
        self.assertFalse([f for f in files if f.startswith("db_engine/")])

    def test_no_violations_outside_db_engine(self):
        problems = []
        for rel in _tracked_py():
            for rule, detail in _effective(rel):
                problems.append(f"{rel} [{rule}] {detail}\n    fix: {_hint(rule)}")
        self.assertEqual(problems, [], "database boundary violations (the allowlist `ALLOW` is for reviewed exceptions only):\n" + "\n".join(problems))

    def test_allowlist_entries_are_still_needed(self):
        """Stale allowlist entries must be removed so the list stays minimal."""
        stale = []
        tracked = set(_tracked_py())
        for (rel, rule), why in ALLOW.items():
            if rel not in tracked:
                stale.append(f"{rel}: file missing")
                continue
            if not any(r == rule for r, _ in scan_file(rel)):
                stale.append(f"{rel} [{rule}] ({why}) - delete this ALLOW entry, the violation is gone")
        self.assertEqual(stale, [], "unused allowlist entries:\n" + "\n".join(stale))

    def test_scanner_detects_violations(self):
        """Self-check of the detectors on synthetic sources."""
        samples = {
            "import sqlite3\n": "driver-import",
            "from mysql.connector import Error\n": "driver-import",
            "from db_engine.DBHandling import MariaDB\n": "engine-internal",
            "db.cnx.commit()\n": "raw-call",
            "cur.execute('x')\n": "raw-call",
            "q = 'SELECT a, b FROM t WHERE x = 1'\n": "sql-text",
            "q = 'INSERT INTO t VALUES (1)'\n": "sql-text",
            "q = 'x ON DUPLICATE KEY UPDATE a=1'\n": "sql-text",
            "isinstance(db, MockDB)\n": "concrete-isinstance",
        }
        for code, rule in samples.items():
            rules = {r for r, _ in scan_source("core/sample.py", code)}
            self.assertIn(rule, rules, code)


if __name__ == "__main__":
    unittest.main()
