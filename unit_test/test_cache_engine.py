"""unit_test/test_cache_engine.py - Contract tests of the cache engines (cache_engine/CACHE_API.md).

`CacheEngineContract` is engine-agnostic: a future implementation reuses it with
`class TestMine(CacheEngineContract, unittest.TestCase): def make_engine(self, root): return MyEngine(root)`.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from cache_engine import (BYTES, INT, TEXT, BaseCacheEngine, CacheError, CacheSchemaError, Eq, In, IsNull, Prefix,
                          Range, StoreSpec, UnknownCacheEngine, available_cache_engines, configured_engine_name,
                          get_cache_engine, register_cache_engine)
from cache_engine.ram_cache import RAMCacheEngine
from cache_engine.sqlite_cache import SQLiteCacheEngine

ITEMS = StoreSpec("items", (BYTES("sha"), INT("n"), TEXT("label", null=True)), key=("sha",), indexes=(("n",),))
PAIRS = StoreSpec("pairs", (INT("a"), INT("b"), TEXT("v")), key=("a", "b"), clustered=True)
LOG = StoreSpec("log", (TEXT("k"), INT("x", null=True)), unique=(("k", "x"),), indexes=(("k",),))
SPECS = (ITEMS, PAIRS, LOG)


class CacheEngineContract:
    """Behaviour every cache engine implements. Subclasses provide `make_engine(root)`."""

    def make_engine(self, root: str) -> BaseCacheEngine:  # pragma: no cover - abstract
        raise NotImplementedError

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.engine = self.make_engine(self.tmp)
        self.space = self.engine.open("test-ns", "demo-v1", SPECS)
        self.items, self.pairs, self.log = (self.space.store(n) for n in ("items", "pairs", "log"))

    def tearDown(self) -> None:
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- points
    def test_put_get_replace(self) -> None:
        self.assertIsNone(self.items.get(b"a"))
        self.items.put((b"a", 1, "one"))
        self.assertEqual(self.items.get(b"a"), (b"a", 1, "one"))
        self.items.put((b"a", 2, None))
        self.assertEqual(self.items.get(b"a"), (b"a", 2, None))
        self.assertEqual(self.items.count(), 1)

    def test_put_many_replace_vs_insert_if_absent(self) -> None:
        self.assertEqual(self.items.put_many((bytes([i]), i, str(i)) for i in range(5)), 5)
        self.assertEqual(self.items.put_many([(bytes([1]), 99, "x"), (bytes([9]), 9, "n")], replace=False), 1)
        self.assertEqual(self.items.get(bytes([1]))[1], 1)
        self.assertEqual(self.items.get(bytes([9]))[1], 9)
        self.items.put_many([(bytes([1]), 99, "x")])
        self.assertEqual(self.items.get(bytes([1]))[1], 99)

    def test_get_many_scalar_and_composite_keys(self) -> None:
        self.items.put_many((i.to_bytes(2, "big"), i, None) for i in range(1200))
        got = self.items.get_many([i.to_bytes(2, "big") for i in range(0, 1300)] + [b"zz"])
        self.assertEqual(len(got), 1200)
        self.pairs.put_many([(1, 2, "a"), (1, 3, "b"), (2, 2, "c")])
        self.assertEqual(self.pairs.get_many([(1, 3), (2, 2), (9, 9)]), {(1, 3): (1, 3, "b"), (2, 2): (2, 2, "c")})
        self.assertEqual(self.pairs.get((1, 2)), (1, 2, "a"))
        self.assertTrue(self.pairs.delete((1, 2)))
        self.assertFalse(self.pairs.delete((1, 2)))
        self.assertEqual(self.pairs.delete_many([(1, 3), (2, 2), (5, 5)]), 2)

    def test_large_get_many(self) -> None:
        keys = [i.to_bytes(4, "big") for i in range(2500)]
        self.items.put_many((k, 1, None) for k in keys)
        self.assertEqual(len(self.items.get_many(keys)), 2500)
        self.assertEqual(self.items.delete_many(keys[:1700]), 1700)
        self.assertEqual(self.items.count(), 800)

    # ---------------------------------------------------------------- queries
    def _fill(self) -> None:
        self.items.put_many((bytes([i, 0]), i % 3, f"l{i}") for i in range(30))

    def test_scan_conditions(self) -> None:
        self._fill()
        self.assertEqual(sorted(r[1] for r in self.items.scan(Eq("n", 1))), [1] * 10)
        self.assertEqual(len(list(self.items.scan({"n": [0, 2]}))), 20)
        self.assertEqual(len(list(self.items.scan(In("n", [])))), 0)
        self.assertEqual([r[0] for r in self.items.scan(Range("sha", ge=bytes([5]), lt=bytes([8])), order_by=["sha"])],
                         [bytes([5, 0]), bytes([6, 0]), bytes([7, 0])])
        self.assertEqual(len(list(self.items.scan(Prefix("label", "l1")))), 11)   # l1, l10..l19
        self.assertEqual(len(list(self.items.scan(Prefix("sha", bytes([4]))))), 1)
        self.assertEqual(len(list(self.items.scan([Eq("n", 0), Range("sha", gt=bytes([20]))]))), 3)
        self.items.put((b"nul", 7, None))
        self.assertEqual([r[0] for r in self.items.scan(IsNull("label"))], [b"nul"])
        self.assertEqual(len(list(self.items.scan({"label": None}))), 1)
        self.assertEqual(len(list(self.items.scan(IsNull("label", negate=True)))), 30)

    def test_scan_projection_order_limit(self) -> None:
        self._fill()
        self.assertEqual(list(self.items.scan(Eq("n", 2), columns=("n",), limit=2)), [(2,), (2,)])
        top = list(self.items.scan(columns=("sha", "n"), order_by=["-sha"], limit=2))
        self.assertEqual([r[0] for r in top], [bytes([29, 0]), bytes([28, 0])])
        self.assertEqual([r[0] for r in self.items.scan(order_by=["n", "-sha"], limit=2)], [bytes([27, 0]), bytes([24, 0])])

    def test_scan_large_in_is_chunked(self) -> None:
        keys = [i.to_bytes(4, "big") for i in range(1500)]
        self.items.put_many((k, 1, None) for k in keys)
        self.assertEqual(len(list(self.items.scan(In("sha", keys)))), 1500)
        self.assertEqual(self.items.count(In("sha", keys)), 1500)
        with self.assertRaises(CacheSchemaError):
            list(self.items.scan(In("sha", keys), limit=5))

    def test_keyless_store_scans_in_insertion_order_and_dedupes(self) -> None:
        rows = [("z", 1), ("a", 2), ("m", 3), ("a", 1)]
        self.assertEqual(self.log.put_many(rows, replace=False), 4)
        self.assertEqual(self.log.put_many(rows, replace=False), 0)
        self.assertEqual(list(self.log.scan()), rows)
        self.assertEqual(list(self.log.scan(Eq("k", "a"), columns=("x",))), [(2,), (1,)])

    def test_aggregates(self) -> None:
        self.assertEqual((self.items.count(), self.items.sum("n"), self.items.max("n")), (0, 0, None))
        self._fill()
        self.assertEqual(self.items.count(Eq("n", 1)), 10)
        self.assertEqual(self.items.sum("n"), 30)
        self.assertEqual(self.items.max("n", Eq("n", 1)), 1)

    # ---------------------------------------------------------------- mutation
    def test_delete_where_update_where_clear(self) -> None:
        self._fill()
        self.assertEqual(self.items.update_where([In("sha", [bytes([1, 0]), bytes([2, 0])]), Range("n", lt=2)], {"n": 9}), 1)
        self.assertEqual(self.items.get(bytes([1, 0]))[1], 9)
        self.assertEqual(self.items.get(bytes([2, 0]))[1], 2)  # n=2 not < 2: untouched
        self.assertEqual(self.items.delete_where(Eq("n", 0)), 10)
        self.assertEqual(self.items.count(), 20)
        with self.assertRaises(CacheSchemaError):
            self.items.update_where(None, {"sha": b"x"})
        self.items.clear()
        self.assertEqual(self.items.count(), 0)

    def test_schema_errors(self) -> None:
        with self.assertRaises(CacheSchemaError):
            list(self.items.scan(Eq("nope", 1)))
        with self.assertRaises(CacheSchemaError):
            self.space.store("missing")
        with self.assertRaises(CacheSchemaError):
            self.pairs.get(1)  # composite key needs a tuple
        with self.assertRaises(CacheError):
            self.items.put((b"a", 1))  # wrong arity
        self.assertEqual(self.items.count(), 0)

    # ---------------------------------------------------------------- space
    def test_metadata(self) -> None:
        self.assertIsNone(self.space.get_meta("built_at"))
        self.space.set_meta("built_at", "12.5")
        self.space.set_meta("built_at", "13")
        self.assertEqual(self.space.get_meta("built_at"), "13")
        self.space.delete_meta("built_at")
        self.assertIsNone(self.space.get_meta("built_at"))

    def test_batch_commits_and_rolls_back(self) -> None:
        with self.space.batch():
            self.items.put((b"a", 1, None))
            self.pairs.put((1, 1, "x"))
            self.space.set_meta("k", "v")
            with self.space.batch():  # re-entrant
                self.items.put((b"b", 2, None))
        self.assertEqual((self.items.count(), self.pairs.count(), self.space.get_meta("k")), (2, 1, "v"))
        with self.assertRaises(RuntimeError):
            with self.space.batch():
                self.items.put((b"c", 3, None))
                self.space.set_meta("k", "other")
                raise RuntimeError("boom")
        self.assertEqual((self.items.count(), self.space.get_meta("k")), (2, "v"))

    def test_batch_invisible_to_other_threads_until_done(self) -> None:
        seen: list[int] = []
        inside, proceed = threading.Event(), threading.Event()

        def writer() -> None:
            with self.space.batch():
                self.items.put((b"w", 1, None))
                inside.set()
                proceed.wait(5)

        t = threading.Thread(target=writer)
        t.start()
        inside.wait(5)
        seen.append(self.items.count())
        proceed.set()
        t.join()
        seen.append(self.items.count())
        self.assertEqual(seen, [0, 1])

    def test_space_clear_resets_stores_and_meta(self) -> None:
        self._fill()
        self.pairs.put((1, 1, "x"))
        self.space.set_meta("k", "v")
        self.space.clear()
        self.assertEqual((self.items.count(), self.pairs.count(), self.space.get_meta("k")), (0, 0, None))

    def test_concurrent_writers_and_readers(self) -> None:
        errors: list[BaseException] = []

        def work(i: int) -> None:
            try:
                for j in range(20):
                    self.items.put_many((f"{i}-{j}-{k}".encode(), i, None) for k in range(25))
                    self.items.count(Eq("n", i))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(self.items.count(), 6 * 20 * 25)

    def test_open_is_idempotent_and_checks_schema(self) -> None:
        self.assertIs(self.engine.open("test-ns", "demo-v1", SPECS), self.space)
        self.items.put((b"a", 1, None))
        with self.assertRaises(CacheSchemaError):
            self.engine.open("test-ns", "demo-v1", (ITEMS,))
        with self.assertRaises(CacheSchemaError):
            self.engine.open("Bad Namespace", "x", SPECS)

    def test_data_survives_reopen_and_exists(self) -> None:
        self.assertTrue(self.engine.exists("test-ns", "demo-v1"))
        self.assertFalse(self.engine.exists("test-ns", "other"))
        self.items.put((b"a", 1, "kept"))
        self.space.set_meta("built_at", "1")
        self.space.close()
        again = self.engine.open("test-ns", "demo-v1", SPECS)
        self.assertEqual(again.store("items").get(b"a"), (b"a", 1, "kept"))
        self.assertEqual(again.get_meta("built_at"), "1")
        self.assertIsInstance(again.location, str)

    def test_discard_and_compact_and_size(self) -> None:
        self.items.put_many((bytes([i]), 1, "x" * 100) for i in range(100))
        self.space.compact()
        self.assertEqual(self.items.count(), 100)
        self.assertGreaterEqual(self.space.size_bytes(), 0)
        self.space.discard()
        self.assertFalse(self.engine.exists("test-ns", "demo-v1"))
        fresh = self.engine.open("test-ns", "demo-v1", SPECS)
        self.assertEqual(fresh.store("items").count(), 0)


class TestSQLiteCacheEngine(CacheEngineContract, unittest.TestCase):
    def make_engine(self, root: str) -> BaseCacheEngine:
        return SQLiteCacheEngine(root)

    def test_file_layout_and_no_driver_errors(self) -> None:
        self.assertEqual(self.space.location, os.path.join(self.tmp, "test-ns", "demo-v1.sqlite3"))
        self.assertTrue(os.path.exists(self.space.location))

    def test_corrupt_file_raises_cache_error(self) -> None:
        self.space.close()
        with open(self.space.location, "wb") as fh:
            fh.write(b"this is not a sqlite database" * 100)
        with self.assertRaises(CacheError) as ctx:
            self.engine.open("test-ns", "demo-v1", SPECS)
        self.assertNotIsInstance(ctx.exception, sqlite3.Error)

    def test_opens_files_written_before_the_cache_engine_existed(self) -> None:
        """The relation/release/fact index files keep their layout: tables, columns and index names."""
        path = os.path.join(self.tmp, "legacy", "x-v1.sqlite3")
        os.makedirs(os.path.dirname(path))
        raw = sqlite3.connect(path)
        raw.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE commits (sha BLOB PRIMARY KEY, at INTEGER, subject TEXT) WITHOUT ROWID;
            CREATE TABLE rel (src BLOB NOT NULL, kind INTEGER NOT NULL, dst BLOB, prefix TEXT, p8 TEXT, note TEXT,
                              UNIQUE (src, kind, dst, prefix, note));
            CREATE INDEX rel_src ON rel (src);
            INSERT INTO meta VALUES ('built_at', '5');
            INSERT INTO commits VALUES (x'aa', 7, 'subj');
            INSERT INTO rel VALUES (x'aa', 1, NULL, 'abc', 'abc', NULL);
        """)
        raw.commit()
        raw.close()
        specs = (StoreSpec("commits", (BYTES("sha"), INT("at", null=True), TEXT("subject", null=True)), key=("sha",),
                           clustered=True),
                 StoreSpec("rel", (BYTES("src"), INT("kind"), BYTES("dst", null=True), TEXT("prefix", null=True),
                                   TEXT("p8", null=True), TEXT("note", null=True)),
                           unique=(("src", "kind", "dst", "prefix", "note"),), indexes=(("src",), ("dst",), ("p8",))))
        space = self.engine.open("legacy", "x-v1", specs)
        self.assertEqual(space.get_meta("built_at"), "5")
        self.assertEqual(space.store("commits").get(b"\xaa"), (b"\xaa", 7, "subj"))
        self.assertEqual(list(space.store("rel").scan(Eq("p8", "abc"), columns=("kind", "prefix"))), [(1, "abc")])

    def test_schema_mismatch_with_existing_file(self) -> None:
        self.space.close()
        other = StoreSpec("items", (BYTES("sha"), INT("n")), key=("sha",))
        with self.assertRaises(CacheSchemaError):
            self.engine.open("test-ns", "demo-v1", (other,))


class TestRAMCacheEngine(CacheEngineContract, unittest.TestCase):
    def make_engine(self, root: str) -> BaseCacheEngine:
        return RAMCacheEngine(root)

    def test_location_and_close_vs_discard(self) -> None:
        self.assertEqual(self.space.location, "ram://test-ns/demo-v1")
        self.assertEqual(self.engine.location("test-ns", "demo-v1"), "ram://test-ns/demo-v1")
        self.items.put((b"k", 42, "v"))
        self.space.set_meta("m", "1")
        self.space.close()
        with self.assertRaises(CacheError):
            self.items.get(b"k")
        with self.assertRaises(CacheError):
            self.space.get_meta("m")
        self.assertTrue(self.engine.exists("test-ns", "demo-v1"))
        with self.assertRaises(CacheSchemaError):
            self.engine.open("test-ns", "demo-v1", (ITEMS,))
        reopened = self.engine.open("test-ns", "demo-v1", SPECS)
        self.assertIsNot(reopened, self.space)
        self.assertEqual(reopened.store("items").get(b"k"), (b"k", 42, "v"))
        self.assertEqual(reopened.get_meta("m"), "1")
        reopened.discard()
        self.assertFalse(self.engine.exists("test-ns", "demo-v1"))

    def test_size_bytes_and_compact(self) -> None:
        empty = self.engine.open("test-ns", "empty-v1", SPECS)
        self.assertEqual(empty.size_bytes(), 0)
        self.items.put_many((i.to_bytes(2, "big"), i % 5, "x" * 20) for i in range(3000))
        self.assertGreater(self.space.size_bytes(), 0)
        # Trigger lazy SortedIndex build then delete most rows and compact
        self.assertEqual(len(list(self.items.scan(Range("n", ge=0, le=2)))), 1800)
        self.items.delete_where(Range("n", ge=1))
        self.space.compact()
        self.assertEqual(self.items.count(), 600)
        self.assertEqual(len(list(self.items.scan(Range("n", ge=0, le=2)))), 600)

    def test_sorted_index_duplicates_nulls_range_prefix_limit_zero(self) -> None:
        # Heavy duplicates + nulls + incremental mutations
        rows = [(i.to_bytes(2, "big"), i % 4, None if i % 5 == 0 else f"p{i:04d}") for i in range(2500)]
        self.items.put_many(rows)
        # Range with both ge/gt and le/lt, and limit=0
        self.assertEqual(list(self.items.scan(Range("n", ge=1, lt=3), limit=0)), [])
        self.assertEqual(self.items.count(Range("n", ge=1, gt=1, le=3, lt=3)), 625)  # n == 2
        # Null ordering: None first in ASC, last in DESC
        asc_labels = [r[0] for r in self.items.scan(columns=("label",), order_by=["label"], limit=501)]
        self.assertEqual(asc_labels[:500], [None] * 500)
        self.assertEqual(asc_labels[500], "p0001")
        desc_labels = [r[0] for r in self.items.scan(columns=("label",), order_by=["-label"], limit=2001)]
        self.assertIsNotNone(desc_labels[1999])
        self.assertIsNone(desc_labels[2000])
        # Range and Prefix exclude None; Range with all bounds None is a no-op (includes None)
        self.assertEqual(self.items.count(Range("label")), 2500)
        self.assertEqual(self.items.count(Range("label", ge="p")), 2000)
        self.assertEqual(self.items.count(Prefix("label", "p01")), 80)
        # Incremental add/remove on existing SortedIndex
        self.items.put((b"\xff\xff", 2, None))
        self.items.delete(b"\x00\x00")
        self.assertEqual(self.items.count(Range("label", ge="p")), 2000)

    def test_update_where_indexed_and_unique_and_error_atomicity(self) -> None:
        self.log.put_many([("a", 1), ("a", 2), ("b", None), ("c", None)])
        # NULLs do not collide in unique group ("k", "x")
        self.assertEqual(self.log.count(), 4)
        # Updating unique field to collide raises CacheError without partial mutation
        with self.assertRaises(CacheError):
            self.log.update_where(Eq("k", "a"), {"x": 1})
        self.assertEqual(sorted(r[1] for r in self.log.scan(Eq("k", "a"))), [1, 2])
        # Valid update updates indexed field
        self.assertEqual(self.log.update_where([Eq("k", "a"), Eq("x", 2)], {"k": "z"}), 1)
        self.assertEqual(list(self.log.scan(Eq("k", "z"))), [("z", 2)])
        # put_many error atomicity: bad record at index 2 writes nothing
        with self.assertRaises(CacheSchemaError):
            self.items.put_many([(b"ok1", 1, None), (b"ok2", 2, None), (b"bad",)])
        self.assertEqual(self.items.count(), 0)

    def test_concurrent_scan_snapshot_isolation(self) -> None:
        self.items.put_many((i.to_bytes(2, "big"), i, None) for i in range(100))
        it = self.items.scan(order_by=["sha"])
        first = next(it)
        self.assertEqual(first[1], 0)
        # Mutate while scan iterator is active
        self.items.delete_where(Range("n", ge=50))
        self.items.put((b"\xff\xff", 999, "new"))
        rest = list(it)
        self.assertEqual(len(rest), 99)
        self.assertEqual(self.items.count(), 51)

    def test_large_batch_delta_overlay(self) -> None:
        self.items.put_many((i.to_bytes(4, "big"), i % 10, None) for i in range(4500))
        with self.space.batch():
            self.items.put_many((i.to_bytes(4, "big"), 99, "new") for i in range(4500, 4520))
            self.items.delete(b"\x00\x00\x00\x00")
            self.assertIsNone(self.items.get(b"\x00\x00\x00\x00"))
            self.assertEqual(self.items.get((4505).to_bytes(4, "big")), ((4505).to_bytes(4, "big"), 99, "new"))
            # In-batch scan materializes delta lazily
            self.assertEqual(self.items.count(Eq("n", 99)), 20)
        self.assertEqual(self.items.count(), 4519)
        with self.assertRaises(RuntimeError):
            with self.space.batch():
                self.items.delete((1).to_bytes(4, "big"))
                raise RuntimeError("rollback")
        self.assertIsNotNone(self.items.get((1).to_bytes(4, "big")))

    def test_unstarted_scan_does_not_leak_readers(self) -> None:
        self.items.put_many((i.to_bytes(2, "big"), i, None) for i in range(10))
        it = self.items.scan()
        st = self.space._storage.stores["items"]
        self.assertEqual(st.readers, 1)
        del it
        self.assertEqual(st.readers, 0)

    def test_unhashable_key_and_prefix_type_safety(self) -> None:
        with self.assertRaises(CacheSchemaError):
            self.items.get([1, 2])
        with self.assertRaises(CacheSchemaError):
            self.items.put(([1, 2], 1, None))
        with self.assertRaises(CacheSchemaError):
            self.items.delete([1, 2])
        self.items.put((b"a", 123, "hello"))
        self.assertEqual(list(self.items.scan(Prefix("n", "12"))), [])
        self.assertEqual(list(self.items.scan([Eq("n", 123), Prefix("n", "12")])), [])
        self.assertEqual(list(self.items.scan(Prefix("label", b"he"))), [])

    def test_leading_col_of_composite_unique_is_indexed(self) -> None:
        links_spec = StoreSpec("links", (TEXT("mid"), BYTES("sha")), unique=(("mid", "sha"),))
        sp = self.engine.open("test-ns", "links-v1", (links_spec,))
        links = sp.store("links")
        links.put_many([("m1", b"s1"), ("m1", b"s2"), ("m2", b"s3")])
        st = sp._storage.stores["links"]
        self.assertIn((0,), st.indexes)
        self.assertEqual(sorted(r[1] for r in links.scan(Eq("mid", "m1"))), [b"s1", b"s2"])
        self.assertEqual(links.count(Eq("mid", "m1")), 2)


class TestFactory(unittest.TestCase):
    def test_registry_and_defaults(self) -> None:
        self.assertIn("sqlite", available_cache_engines())
        self.assertIn("ram", available_cache_engines())
        with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_ENGINE": ""}), \
                mock.patch("core.config.get_config", return_value={}):
            self.assertEqual(configured_engine_name(), "ram")
            self.assertIsInstance(get_cache_engine(), RAMCacheEngine)
        self.assertIsInstance(get_cache_engine("sqlite"), SQLiteCacheEngine)
        self.assertIs(get_cache_engine("sqlite"), get_cache_engine("SQLite3"))
        self.assertIsInstance(get_cache_engine("ram"), RAMCacheEngine)
        for alias in ("memory", "mem", "in-memory", "in_memory"):
            self.assertIs(get_cache_engine("ram"), get_cache_engine(alias))
        with self.assertRaises(UnknownCacheEngine):
            get_cache_engine("nope")

    def test_config_selects_engine_and_directory(self) -> None:
        class Dummy(SQLiteCacheEngine):
            name = "dummy"

        register_cache_engine("dummy-test", Dummy)
        try:
            with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_ENGINE": "dummy-test"}):
                self.assertIsInstance(get_cache_engine(), Dummy)
            from cache_engine import cache_root
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": tmp}):
                self.assertEqual(str(cache_root()), tmp)
            cfg = {"cache": {"dir": "/somewhere/else"}}
            with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": ""}), \
                    mock.patch("core.config.get_config", return_value=cfg):
                self.assertEqual(str(cache_root()), "/somewhere/else")
        finally:
            from cache_engine import _FACTORIES, _INSTANCES
            _FACTORIES.pop("dummy-test", None)
            _INSTANCES.pop("dummy-test", None)

    def test_default_root_is_resolved_per_open(self) -> None:
        engine = SQLiteCacheEngine()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": a}):
                sa = engine.open("ns", "n", (ITEMS,))
            with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": b}):
                sb = engine.open("ns", "n", (ITEMS,))
            self.assertIsNot(sa, sb)
            self.assertTrue(sa.location.startswith(a) and sb.location.startswith(b))
            engine.close()

    def test_ram_default_root_is_resolved_per_open(self) -> None:
        engine = RAMCacheEngine()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": a}):
                sa = engine.open("ns", "n", (ITEMS,))
                sa.store("items").put((b"k", 1, "a"))
                self.assertTrue(engine.exists("ns", "n"))
            with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": b}):
                self.assertFalse(engine.exists("ns", "n"))
                sb = engine.open("ns", "n", (ITEMS,))
                self.assertIsNone(sb.store("items").get(b"k"))
                sb.discard()
            self.assertIsNot(sa, sb)
            with mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": a}):
                self.assertTrue(engine.exists("ns", "n"))
            engine.close()


if __name__ == "__main__":
    unittest.main()
