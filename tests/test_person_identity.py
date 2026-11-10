"""tests/test_person_identity.py - Stored developer identity of each person alias (m_person_identity, D08)."""
from __future__ import annotations

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ingest_state as I
from core.DBLayout import TABLES
from parser.git_ast.identity import build_identity_index, refresh_person_identities


class FakeDB:
    """m_maintainer_person and m_person_identity in memory, for the statements refresh_person_identities runs."""

    def __init__(self, persons: list[tuple[int, str, str]]) -> None:
        self.persons = list(persons)
        self.identity: dict[int, str] = {}
        self.writes = 0
        self.commits = 0

    def query(self, sql: str, params: tuple = (), dictionary: bool = True) -> list:
        if sql.startswith("SELECT person_id, name, email FROM m_maintainer_person"):
            return [p for p in self.persons if p[0] > 0]
        elif sql.startswith("SELECT person_id, identity_key FROM m_person_identity"):
            return sorted(self.identity.items())
        raise AssertionError(sql)

    def execute(self, sql: str, params: tuple = ()) -> int:
        if sql.startswith("DELETE FROM m_person_identity"):
            self.writes += len(params)
            for pid in params:
                self.identity.pop(pid, None)
            return len(params)
        raise AssertionError(sql)

    def executemany(self, sql: str, params_seq: list) -> int:
        if sql.startswith("INSERT INTO m_person_identity"):
            self.writes += len(params_seq)
            for pid, key in params_seq:
                self.identity[pid] = key
            return len(params_seq)
        raise AssertionError(sql)

    def cursor(self):
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1


FakeCnx = FakeDB


class _Cur:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.rows: list = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        if sql.startswith("SELECT person_id, name, email FROM m_maintainer_person"):
            self.rows = [p for p in self.db.persons if p[0] > 0]
        elif sql.startswith("SELECT person_id, identity_key FROM m_person_identity"):
            self.rows = sorted(self.db.identity.items())
        elif sql.startswith("INSERT INTO m_person_identity"):
            self.db.writes += len(params) // 2
            for i in range(0, len(params), 2):
                self.db.identity[params[i]] = params[i + 1]
        elif sql.startswith("DELETE FROM m_person_identity"):
            self.db.writes += len(params)
            for pid in params:
                self.db.identity.pop(pid, None)
        else:
            raise AssertionError(sql)

    def fetchall(self) -> list:
        return self.rows

    def close(self) -> None:
        pass


PERSONAS = [
    (1, "Ann Dev", "ann@x.org"),
    (2, "ann dev", "ann@corp.com"),          # same name, other address: same identity
    (3, "", "ann@x.org"),                     # name-less: joins the identity owning its address
    (4, "Bob", "shared@lists.org"),
    (5, "Carl", "shared@lists.org"),          # contested address
    (6, "", "shared@lists.org"),
]


class TestRefresh(unittest.TestCase):
    def test_refresh_stores_the_computed_index_and_is_idempotent(self) -> None:
        db = FakeDB(PERSONAS)
        self.assertEqual(refresh_person_identities(db), 6)
        self.assertEqual(db.identity, build_identity_index(PERSONAS))
        self.assertEqual(db.identity[3], "n:ann dev")
        self.assertEqual(db.identity[6], "n:carl")   # tie on uses and size: the larger key wins, as today
        db.writes = 0
        self.assertEqual(refresh_person_identities(db), 0)
        self.assertEqual(db.writes, 0)

    def test_new_alias_moves_a_name_less_alias(self) -> None:
        db = FakeDB(PERSONAS)
        refresh_person_identities(db)
        db.persons.append((7, "Bob", "shared@lists.org"))   # Bob now uses the address on two personas
        self.assertEqual(refresh_person_identities(db), 2)  # the new row and the moved name-less one
        self.assertEqual(db.identity[6], "n:bob")

    def test_rows_of_removed_persons_are_deleted(self) -> None:
        db = FakeDB(PERSONAS)
        refresh_person_identities(db)
        db.persons = [p for p in db.persons if p[0] != 5]
        refresh_person_identities(db)
        self.assertNotIn(5, db.identity)

    def test_rollback_removes_rows_of_rolled_back_persons(self) -> None:
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        plan = I.rollback_plan(TABLES, marks, 10, [])
        self.assertIn(("m_person_identity", "person_id > %s"), {(d.table, d.where) for d in plan})


class TestWebappIndex(unittest.TestCase):
    """MaintainerService._identity_index reads m_person_identity when it has rows."""

    def _cursor(self, stored: dict[int, str] | None):
        persons = PERSONAS

        class Cur:
            def __init__(self) -> None:
                self.rows: list = []
                self.sql: list[str] = []

            def execute(self, sql: str, params: tuple = ()) -> None:
                self.sql.append(sql)
                if "FROM m_maintainer_person;" in sql and "COUNT" in sql:
                    self.rows = [{"cnt": len(persons), "max_id": max(p[0] for p in persons)}]
                elif "FROM m_person_identity;" in sql and "COUNT" in sql:
                    if stored is None:
                        raise RuntimeError("Table 'm_person_identity' doesn't exist")
                    self.rows = [{"cnt": len(stored), "max_id": max(stored) if stored else None}]
                elif sql.startswith("SELECT person_id, identity_key"):
                    self.rows = [{"person_id": k, "identity_key": v} for k, v in stored.items()]
                elif "WHERE person_id > %s" in sql:
                    self.rows = [{"person_id": p, "name": n, "email": e} for p, n, e in persons if p > params[0]]
                elif "WHERE person_id > 0" in sql:
                    self.rows = [{"person_id": p, "name": n, "email": e} for p, n, e in persons]
                else:
                    raise AssertionError(sql)

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return self.rows

        return Cur()

    def setUp(self) -> None:
        from webapp.backend.services import maintainer_service as ms
        self.ms = ms
        ms._IDENTITY_CACHE.clear()

    def test_stored_index_equals_the_computed_one(self) -> None:
        stored = build_identity_index(PERSONAS)
        cur = self._cursor(stored)
        pid_to_key, members = self.ms.MaintainerService._identity_index(cur)
        self.assertEqual(pid_to_key, stored)
        self.assertEqual(members["n:ann dev"], [1, 2, 3])
        self.assertFalse(any("WHERE person_id > 0" in s for s in cur.sql))   # the registry is not read whole

    def test_persons_after_the_last_refresh_get_their_own_key(self) -> None:
        stored = {k: v for k, v in build_identity_index(PERSONAS[:4]).items()}
        pid_to_key, _ = self.ms.MaintainerService._identity_index(self._cursor(stored))
        self.assertEqual(pid_to_key[5], "n:carl")
        self.assertEqual(pid_to_key[6], "e:shared@lists.org")   # no name-less merge until the next refresh

    def test_without_the_table_the_index_is_computed(self) -> None:
        for stored in (None, {}):
            self.ms._IDENTITY_CACHE.clear()
            pid_to_key, _ = self.ms.MaintainerService._identity_index(self._cursor(stored))
            self.assertEqual(pid_to_key, build_identity_index(PERSONAS))


class TestCallSites(unittest.TestCase):
    def test_workers_do_not_refresh(self) -> None:
        import main
        db = mock.MagicMock()
        with mock.patch.object(main.G, "distributed_role", main.distributed.ROLE_WORKER, create=True):
            self.assertIsNone(main.refresh_person_identities(db, "test"))
        db.cursor.assert_not_called()

    def test_start_refresh_only_when_incomplete(self) -> None:
        import main
        db = mock.MagicMock()
        db.query_one.return_value = (6, 6)
        with mock.patch.object(main.G, "distributed_role", "standalone", create=True), \
                mock.patch("parser.git_ast.identity.refresh_person_identities") as refresh:
            self.assertEqual(main.refresh_person_identities(db, "start", only_if_incomplete=True), 0)
            refresh.assert_not_called()
        self.assertTrue(re.search(r"COUNT\(\*\) FROM m_person_identity", db.query_one.call_args[0][0]))


if __name__ == "__main__":
    unittest.main()
