"""unit_test/test_sqlite_store.py - Unit Tests for SQLiteStore Engine."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from db_engine.spec import (
    Agg,
    AnyOf,
    Col,
    Cond,
    Const,
    Exists,
    Inc,
    InSelect,
    Join,
    NotExists,
    Select,
    Value,
    between,
    ge,
    gt,
    hash_prefix,
    ieq,
    is_null,
    le,
    len_lt,
    like,
    lt,
    ne,
    not_in,
    not_null,
)
from db_engine.sqlite_store import (
    MissingTableError,
    SQLiteStore,
    StoreError,
    TableSpec,
)


class TestSQLiteStore(unittest.TestCase):
    """Test suite for SQLiteStore and SQLiteSession."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"

        self.user_table = TableSpec(
            name="users",
            columns=(
                ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
                ("name", "TEXT NOT NULL"),
                ("email", "TEXT UNIQUE"),
                ("score", "INTEGER DEFAULT 0"),
            ),
            indexes=(("idx_users_score", ("score",)),),
        )

        self.post_table = TableSpec(
            name="posts",
            columns=(
                ("post_id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
                ("user_id", "INTEGER NOT NULL"),
                ("title", "TEXT NOT NULL"),
                ("views", "INTEGER DEFAULT 0"),
            ),
            indexes=(("idx_posts_user", ("user_id",)),),
        )

        self.store = SQLiteStore(
            self.db_path,
            tables=(self.user_table, self.post_table),
            autocommit=True,
        )

    def tearDown(self) -> None:
        self.store.close_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_exists_and_has_table(self) -> None:
        self.assertTrue(self.store.exists())
        self.assertTrue(self.store.has_table("users"))
        self.assertTrue(self.store.has_table("posts"))
        self.assertFalse(self.store.has_table("non_existent_xyz"))

    def test_meta_get_and_set(self) -> None:
        self.assertIsNone(self.store.meta_get("version"))
        self.store.meta_set(version="1.0", author="KernelInfo")
        self.assertEqual(self.store.meta_get("version"), "1.0")
        self.assertEqual(self.store.meta_get("author"), "KernelInfo")
        # Overwrite
        self.store.meta_set(version="2.0")
        self.assertEqual(self.store.meta_get("version"), "2.0")

    def test_insert_and_select_rows(self) -> None:
        rows = [
            ("Alice", "alice@example.com", 100),
            ("Bob", "bob@example.com", 200),
            ("Charlie", "charlie@example.com", 300),
        ]
        inserted = self.store.insert_rows("users", ("name", "email", "score"), rows)
        self.assertEqual(inserted, 3)

        # Basic select
        all_users = self.store.select_rows("users", ("id", "name", "score"), order_by=("id",))
        self.assertEqual(len(all_users), 3)
        self.assertEqual(all_users[0][1], "Alice")

        # Select as_dict
        dict_users = self.store.select_rows("users", {"user_name": "name", "user_score": "score"}, as_dict=True)
        self.assertEqual(len(dict_users), 3)
        self.assertIn("user_name", dict_users[0])
        self.assertIn("user_score", dict_users[0])

    def test_conditions_and_filters(self) -> None:
        self.store.insert_rows(
            "users",
            ("name", "email", "score"),
            [
                ("Alice", "alice@example.com", 10),
                ("Bob", "bob@example.com", 20),
                ("Charlie", "charlie@example.com", 30),
                ("Dave", "dave@example.com", 40),
            ],
        )

        # gt, lt, between, ne
        gt_rows = self.store.select_rows("users", ("name",), where={"score": gt(20)})
        self.assertEqual(len(gt_rows), 2)

        btw_rows = self.store.select_rows("users", ("name",), where={"score": between(15, 35)})
        self.assertEqual(len(btw_rows), 2)

        ne_rows = self.store.select_rows("users", ("name",), where={"name": ne("Alice")})
        self.assertEqual(len(ne_rows), 3)

        # like and ieq
        like_rows = self.store.select_rows("users", ("name",), where={"email": like("example.com", "contains")})
        self.assertEqual(len(like_rows), 4)

        ieq_rows = self.store.select_rows("users", ("name",), where={"name": ieq("aLiCe")})
        self.assertEqual(len(ieq_rows), 1)

        # AnyOf
        or_rows = self.store.select_rows(
            "users",
            ("name",),
            where=AnyOf({"name": "Alice"}, {"name": "Bob"}),
        )
        self.assertEqual(len(or_rows), 2)

    def test_in_chunking_and_empty_in(self) -> None:
        # Empty IN sends no query and returns []
        empty_res = self.store.select_rows("users", ("id",), where={"id": []})
        self.assertEqual(empty_res, [])

        # Auto-chunking > 500 values
        large_ids = list(range(1, 1201))
        # Insert 10 rows
        for i in range(1, 11):
            self.store.insert_rows("users", ("id", "name", "email"), [(i, f"User{i}", f"user{i}@test.com")])

        chunked_res = self.store.select_rows("users", ("id", "name"), where={"id": large_ids}, distinct=True)
        self.assertEqual(len(chunked_res), 10)

    def test_on_conflict_modes(self) -> None:
        self.store.insert_rows("users", ("id", "name", "email"), [(1, "Alice", "alice@test.com")])

        # Ignore
        rc_ignore = self.store.insert_rows(
            "users",
            ("id", "name", "email"),
            [(1, "Alice2", "alice@test.com")],
            on_conflict="ignore",
        )
        self.assertEqual(rc_ignore, 0)
        self.assertEqual(self.store.select_value("users", "name", where={"id": 1}), "Alice")

        # Replace
        rc_replace = self.store.insert_rows(
            "users",
            ("id", "name", "email"),
            [(1, "AliceUpdated", "alice@test.com")],
            on_conflict="replace",
        )
        self.assertGreater(rc_replace, 0)
        self.assertEqual(self.store.select_value("users", "name", where={"id": 1}), "AliceUpdated")

    def test_update_and_delete_rows(self) -> None:
        self.store.insert_rows(
            "users",
            ("id", "name", "email", "score"),
            [(1, "Alice", "alice@test.com", 10), (2, "Bob", "bob@test.com", 20)],
        )

        # Update with Inc
        rc_up = self.store.update_rows("users", {"score": Inc(5)}, where={"id": 1})
        self.assertEqual(rc_up, 1)
        self.assertEqual(self.store.select_value("users", "score", where={"id": 1}), 15)

        # Delete with all_rows guard
        with self.assertRaises(ValueError):
            self.store.delete_rows("users", where=None, all_rows=False)

        rc_del = self.store.delete_rows("users", where={"id": 1})
        self.assertEqual(rc_del, 1)
        self.assertEqual(self.store.count_rows("users"), 1)

    def test_transactions_and_rollback(self) -> None:
        self.store.insert_rows("users", ("name", "email"), [("User1", "u1@test.com")])
        try:
            with self.store.transaction():
                self.store.insert_rows("users", ("name", "email"), [("User2", "u2@test.com")])
                raise RuntimeError("Trigger rollback")
        except RuntimeError:
            pass

        # User2 rolled back
        self.assertEqual(self.store.count_rows("users"), 1)
        self.assertFalse(self.store.row_exists("users", where={"name": "User2"}))

    def test_joins_and_attach_session(self) -> None:
        self.store.insert_rows("users", ("id", "name", "email"), [(1, "Alice", "alice@test.com")])
        self.store.insert_rows("posts", ("user_id", "title"), [(1, "Post 1"), (1, "Post 2")])

        # Inner join
        rows = self.store.select_rows(
            ("posts", "p"),
            ("p.title", "u.name"),
            joins=[Join(("users", "u"), on={"u.id": "p.user_id"})],
            order_by=("p.title",),
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "Post 1")
        self.assertEqual(rows[0][1], "Alice")

        # Attached database session
        other_path = Path(self.temp_dir) / "other.db"
        other_store = SQLiteStore(
            other_path,
            tables=[TableSpec("ratings", (("post_id", "INTEGER"), ("stars", "INTEGER")))],
            autocommit=True,
        )
        other_store.insert_rows("ratings", ("post_id", "stars"), [(1, 5)])
        other_store.close_all()

        with self.store.session(attach={"oth": other_path}) as s:
            joined = s.select_rows(
                ("posts", "p"),
                ("p.title", "r.stars"),
                joins=[Join(("oth.ratings", "r"), on={"r.post_id": "p.post_id"})],
            )
            self.assertEqual(len(joined), 1)
            self.assertEqual(joined[0][1], 5)

    def test_missing_table_error(self) -> None:
        with self.assertRaises(MissingTableError):
            self.store.select_rows("no_such_table", ("id",))


if __name__ == "__main__":
    unittest.main()

