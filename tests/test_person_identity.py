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
from db_engine import MockDB
from parser.git_ast.identity import build_identity_index, identity_key, refresh_person_identities


def person_db(persons: list[tuple[int, str, str]]) -> MockDB:
    """An isolated MockDB holding the given m_maintainer_person rows."""
    db = MockDB(use_global=False)
    db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], persons)
    return db


def stored_identity(db: MockDB) -> dict[int, str]:
    """m_person_identity as {person_id: identity_key}."""
    return {int(pid): key for pid, key in db.select_rows("m_person_identity", ["person_id", "identity_key"])}


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
        db = person_db(PERSONAS)
        self.assertEqual(refresh_person_identities(db), 6)
        identity = stored_identity(db)
        self.assertEqual(identity, build_identity_index(PERSONAS))
        self.assertEqual(identity[3], "n:ann dev")
        self.assertEqual(identity[6], "n:carl")   # tie on uses and size: the larger key wins, as today
        self.assertEqual(refresh_person_identities(db), 0, "nothing changed: nothing is written")
        self.assertEqual(stored_identity(db), identity)

    def test_new_alias_moves_a_name_less_alias(self) -> None:
        db = person_db(PERSONAS)
        refresh_person_identities(db)
        db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(7, "Bob", "shared@lists.org")])
        self.assertEqual(refresh_person_identities(db), 2)  # the new row and the moved name-less one
        self.assertEqual(stored_identity(db)[6], "n:bob")

    def test_rows_of_removed_persons_are_deleted(self) -> None:
        db = person_db(PERSONAS)
        refresh_person_identities(db)
        db.delete_rows("m_maintainer_person", {"person_id": 5})
        self.assertGreaterEqual(refresh_person_identities(db), 1)
        self.assertNotIn(5, stored_identity(db))

    def test_rollback_removes_rows_of_rolled_back_persons(self) -> None:
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        plan = I.rollback_plan(TABLES, marks, 10, [])
        self.assertIn(("m_person_identity", "person_id >"), {(d.table, d.condition) for d in plan})


class TestWebappIndex(unittest.TestCase):
    """MaintainerService._identity_index reads m_person_identity when it has rows."""

    def _reader(self, stored: dict[int, str] | None):
        """A read session over the personas with `stored` as m_person_identity (None: a database without the table)."""
        self.db = person_db(PERSONAS)
        if stored is None:
            self.db.drop_tables(["m_person_identity"])
        else:
            self.db.insert_rows("m_person_identity", ["person_id", "identity_key"], list(stored.items()))
        return self.db.reader()

    def setUp(self) -> None:
        from webapp.backend.services import maintainer_service as ms
        self.ms = ms
        ms._IDENTITY_CACHE.clear()

    def test_stored_index_equals_the_computed_one(self) -> None:
        stored = build_identity_index(PERSONAS)
        pid_to_key, members = self.ms.MaintainerService._identity_index(self._reader(stored))
        self.assertEqual(pid_to_key, stored)
        self.assertEqual(members["n:ann dev"], [1, 2, 3])

    def test_persons_after_the_last_refresh_get_their_own_key(self) -> None:
        stored = {k: v for k, v in build_identity_index(PERSONAS[:4]).items()}
        pid_to_key, _ = self.ms.MaintainerService._identity_index(self._reader(stored))
        self.assertEqual(pid_to_key[5], "n:carl")
        self.assertEqual(pid_to_key[6], "e:shared@lists.org")   # no name-less merge until the next refresh

    def test_persons_missing_below_the_highest_stored_id_get_their_own_key(self) -> None:
        # Persons are not registered in person_id order: a refresh can store 5 but not 2 and 4
        stored = {k: v for k, v in build_identity_index(PERSONAS).items() if k not in (2, 4)}
        pid_to_key, members = self.ms.MaintainerService._identity_index(self._reader(stored))
        expected = {p: identity_key(n, e) for p, n, e in PERSONAS if p in (2, 4)}
        self.assertEqual({p: pid_to_key[p] for p in expected}, expected)
        self.assertNotIn(None, [pid_to_key.get(p) for p, _, _ in PERSONAS])
        # A named alias without a stored row still joins its identity's other aliases
        named = next(p for p in expected if expected[p].startswith("n:"))
        self.assertGreater(len(members[pid_to_key[named]]), 1)

    def test_without_the_table_the_index_is_computed(self) -> None:
        for stored in (None, {}):
            self.ms._IDENTITY_CACHE.clear()
            pid_to_key, _ = self.ms.MaintainerService._identity_index(self._reader(stored))
            self.assertEqual(pid_to_key, build_identity_index(PERSONAS))


class TestCallSites(unittest.TestCase):
    def test_workers_do_not_refresh(self) -> None:
        import main
        import pipeline.git_ingest
        db = mock.MagicMock()
        with mock.patch.object(main.G, "distributed_role", main.distributed.ROLE_WORKER, create=True):
            self.assertIsNone(pipeline.git_ingest.refresh_person_identities(db, "test"))
        self.assertEqual(db.method_calls, [])   # a worker never touches the database

    def test_start_refresh_only_when_incomplete(self) -> None:
        import main
        import pipeline.git_ingest
        from db_engine import MockDB

        db = MockDB(use_global=False)
        db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(0, "", ""), (1, "a", "a@x"), (2, "b", "b@x")])
        db.insert_rows("m_person_identity", ["person_id", "identity_key"], [(1, "k1"), (2, "k2")])
        with mock.patch.object(main.G, "distributed_role", "standalone", create=True), \
                mock.patch("parser.git_ast.identity.refresh_person_identities", return_value=1) as refresh:
            # every person (id > 0) has an identity row: nothing to do
            self.assertEqual(pipeline.git_ingest.refresh_person_identities(db, "start", only_if_incomplete=True), 0)
            refresh.assert_not_called()
            db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(3, "c", "c@x")])
            self.assertEqual(pipeline.git_ingest.refresh_person_identities(db, "start", only_if_incomplete=True), 1)
            refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
