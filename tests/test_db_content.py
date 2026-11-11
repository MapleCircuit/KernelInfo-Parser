"""tests/test_db_content.py - DB Content Viewer data layer (core/tui/db_content.py), partial-version cleaning
(core/ingest_state.py) and scoped connection environment (core/config.py)."""

from __future__ import annotations

import os
import time
import unittest
from contextlib import contextmanager
from typing import Any
from unittest import mock

from core import config as C
from core import ingest_state as I
from core.DBLayout import TABLES
from core.tui import db_content as D
from db_engine.base import TableStats
from db_engine.spec import Cond

NOW = 1_000_000


def _match(row: dict, where: Any) -> bool:
    for col, cond in (where or {}).items():
        val = row.get(col)
        if isinstance(cond, Cond):
            ops = {"gt": val > cond.value, "ge": val >= cond.value} if val is not None else {}
            if cond.op not in ("gt", "ge"):
                raise AssertionError(f"unsupported op {cond.op}")
            if not ops.get(cond.op, False):
                return False
        elif isinstance(cond, (list, tuple, set)):
            if val not in cond:
                return False
        elif val != cond:
            return False
    return True


class FakeDB:
    """In-memory rows per table implementing the spec reads `core/tui/db_content.py` performs."""

    def __init__(self, stats: list[TableStats] | None = None) -> None:
        self.rows: dict[str, list[dict]] = {}
        self.stats = stats or []
        self.commits = 0
        self.rollbacks = 0
        self.fail: dict[str, Exception] = {}

    def add(self, table: str, **row: Any) -> None:
        self.rows.setdefault(table, []).append(row)

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def select_rows(self, source, columns, where=None, *, as_dict=None, **_kw):
        self._check(source)
        return [tuple(r.get(c) for c in columns) for r in self.rows.get(source, []) if _match(r, where)]

    def select_values(self, source, column, where=None, *, distinct=False, **_kw):
        self._check(source)
        vals = [r.get(column) for r in self.rows.get(source, []) if _match(r, where)]
        return list(dict.fromkeys(vals)) if distinct else vals

    def select_one(self, source, columns, where=None, *, as_dict=None, **_kw):
        rows = self.select_rows(source, columns, where, as_dict=as_dict)
        return rows[0] if rows else None

    def row_exists(self, source, where=None, **_kw):
        self._check(source)
        return any(_match(r, where) for r in self.rows.get(source, []))

    def table_stats(self, names=None):
        self._check("table_stats")
        return list(self.stats)

    def existing_tables(self, names, *, include_views=False):
        return {n for n in names if n in self.rows}

    def delete_rows(self, table, where, *, limit=None, all_rows=False):
        before = self.rows.get(table, [])
        kept = [r for r in before if not _match(r, where)]
        self.rows[table] = kept
        return len(before) - len(kept)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def seeded() -> FakeDB:
    db = FakeDB()
    db.add("m_git_network", network_id=1, name="linux")
    db.add("m_git_network", network_id=2, name="other")
    db.add("m_repository", repo_id=0, network_id=0, name="default", git_url="", default_branch="main")
    db.add("m_repository", repo_id=1, network_id=1, name="linux", git_url="u1", default_branch="master")
    db.add("m_repository", repo_id=2, network_id=2, name="tool", git_url="u2", default_branch="main")
    refs = [
        (1, 1, "v3.10", 10),   # done
        (1, 1, "v3.2", 20),    # interrupted (stale worker)
        (1, 1, "v3.1", 30),    # in progress (live worker)
        (1, 1, "v3.0", 40),    # legacy (checkpoint rows, no state)
        (1, 1, "v2.6", 50),    # new
        (1, 2, "master", 10),  # alias of the done commit
        (1, 3, "root", 60),    # bare commit, new
        (2, 2, "main", 70),    # other network, interrupted, no worker
        (1, 1, "zero", 0),     # commit_id 0: not registered
    ]
    for repo_id, rtype, name, cid in refs:
        db.add("m_git_ref", repo_id=repo_id, ref_type=rtype, ref_name=name, commit_id=cid)
    db.add("m_ingest_state", network_id=1, commit_id=10, status=I.INGEST_DONE, updated_at=5, worker_id="")
    db.add("m_ingest_state", network_id=1, commit_id=20, status=I.INGEST_PARSING, updated_at=6, worker_id="stale")
    db.add("m_ingest_state", network_id=1, commit_id=30, status=I.INGEST_PARSING, updated_at=7, worker_id="live")
    db.add("m_ingest_state", network_id=2, commit_id=70, status=I.INGEST_PARSING, updated_at=8, worker_id="")
    db.add("m_ingest_state", network_id=1, commit_id=99, status=I.INGEST_PARSING, updated_at=9, worker_id="")  # orphan
    db.add("m_parse_node", node_id="live", heartbeat_at=NOW - 10)
    db.add("m_parse_node", node_id="stale", heartbeat_at=NOW - D.HEARTBEAT_WINDOW_S - 1)
    db.add("m_bridge_checkpoint", commit_id=40, blob_id=1)
    db.add("m_bridge_checkpoint", commit_id=40, blob_id=2)
    db.add("m_commit_email", commit_id=10, message_id="a@b")
    db.add("m_commit_link", commit_id=10, message_id="a@b")
    db.rows["m_commit_link"].clear()  # exists but empty
    db.add("m_cve_source", name="cvelist", layout="v5", head=b"\xab\xcd", format=3, records=42, built_at=11)
    db.add("m_cve", cve_id=1)
    schema = [t.table_name for t in TABLES]
    db.stats = [TableStats(n, 1, 100, 10) for n in schema if n not in ("m_cve_file", "m_tag", "m_bridge_checkpoint")]
    db.stats.append(TableStats("m_bridge_checkpoint", 900, 5000, 3000))
    db.stats.append(TableStats("legacy_extra", 3, 50, 0))
    return db


class CollectVersionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snap = D.collect_snapshot(seeded(), target="t", now=NOW)
        self.by_name = {(v.repo, v.ref_name): v for v in self.snap.versions}

    def test_no_errors(self) -> None:
        self.assertEqual(self.snap.errors, [])
        self.assertEqual(self.snap.target, "t")

    def test_classification(self) -> None:
        expect = {
            "v3.10": D.STATE_DONE,
            "master": D.STATE_DONE,
            "v3.2": D.STATE_INTERRUPTED,
            "v3.1": D.STATE_IN_PROGRESS,
            "v3.0": D.STATE_LEGACY,
            "v2.6": D.STATE_NEW,
            "root": D.STATE_NEW,
        }
        for name, state in expect.items():
            self.assertEqual(self.by_name[("linux", name)].state, state, name)
        self.assertEqual(self.by_name[("tool", "main")].state, D.STATE_INTERRUPTED)
        self.assertNotIn(("linux", "zero"), self.by_name)

    def test_ref_types_and_network(self) -> None:
        self.assertEqual(self.by_name[("linux", "master")].ref_type, "branch")
        self.assertEqual(self.by_name[("linux", "root")].ref_type, "commit")
        self.assertEqual(self.by_name[("tool", "main")].network_id, 2)

    def test_orphan_state_row(self) -> None:
        orphan = self.by_name[("linux", "commit_id 99")]
        self.assertEqual(orphan.ref_type, "no ref")
        self.assertEqual(orphan.state, D.STATE_INTERRUPTED)
        self.assertEqual(orphan.updated_at, 9)

    def test_natural_sort(self) -> None:
        tags = [v.ref_name for v in self.snap.versions if v.repo == "linux" and v.ref_type == "tag"]
        self.assertEqual(tags, ["v2.6", "v3.0", "v3.1", "v3.2", "v3.10"])

    def test_repo_totals(self) -> None:
        repos = {r.name: r for r in self.snap.repos}
        self.assertNotIn("default", repos)
        linux = repos["linux"]
        self.assertEqual((linux.tags, linux.branches, linux.commits), (5, 1, 1))
        self.assertEqual((linux.parsed, linux.partial), (2, 3))
        self.assertEqual(linux.branch_names, ["master"])
        self.assertEqual(linux.network, "linux")
        self.assertEqual(repos["tool"].network, "other")

    def test_state_counts_and_cleanable(self) -> None:
        counts = self.snap.state_counts()
        self.assertEqual(counts[D.STATE_INTERRUPTED], 3)
        cleanable = self.snap.cleanable_versions()
        self.assertEqual(sorted((v.network_id, v.commit_id) for v in cleanable), [(1, 20), (1, 99), (2, 70)])

    def test_cleanable_dedup_by_commit(self) -> None:
        v = D.VersionRow(1, "r", "a", "tag", 5, D.STATE_INTERRUPTED)
        snap = D.DBSnapshot(target="", versions=[v, D.VersionRow(1, "r", "b", "branch", 5, D.STATE_INTERRUPTED),
                                                  D.VersionRow(2, "r", "c", "tag", 5, D.STATE_INTERRUPTED)])
        self.assertEqual(len(snap.cleanable_versions()), 2)


class CollectOtherSectionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snap = D.collect_snapshot(seeded(), now=NOW)

    def test_feeds(self) -> None:
        ml = {f.name: f for f in self.snap.mailing_list}
        self.assertTrue(ml["m_commit_email"].exists and ml["m_commit_email"].has_rows)
        self.assertTrue(ml["m_commit_link"].exists)
        self.assertFalse(ml["m_commit_link"].has_rows)
        cve = {f.name: f for f in self.snap.cve}
        self.assertFalse(cve["m_cve_file"].exists)
        self.assertFalse(cve["m_cve_file"].has_rows)
        self.assertTrue(cve["m_cve"].has_rows)
        self.assertEqual(cve["m_cve"].data_bytes, 110)

    def test_cve_sources(self) -> None:
        self.assertEqual(self.snap.cve_sources, [
            {"name": "cvelist", "layout": "v5", "head": "abcd", "format": 3, "records": 42, "built_at": 11}
        ])

    def test_tables(self) -> None:
        self.assertEqual(self.snap.tables[0].name, "m_bridge_checkpoint")
        self.assertEqual(self.snap.tables[0].total_bytes, 8000)
        extra = next(t for t in self.snap.tables if t.name == "legacy_extra")
        self.assertFalse(extra.in_schema)
        self.assertEqual(sorted(self.snap.missing_tables), ["m_cve_file", "m_tag"])
        rows, data, index = self.snap.totals()
        self.assertEqual(rows, sum(t.rows for t in self.snap.tables))
        self.assertEqual(data + index, sum(t.total_bytes for t in self.snap.tables))

    def test_failing_section_is_reported(self) -> None:
        db = seeded()
        db.fail["m_git_ref"] = RuntimeError("boom")
        snap = D.collect_snapshot(db, now=NOW)
        self.assertEqual(len(snap.errors), 1)
        self.assertIn("boom", snap.errors[0])
        self.assertEqual(snap.versions, [])
        self.assertTrue(snap.tables)
        self.assertTrue(snap.mailing_list)

    def test_failing_stats_still_loads_feeds(self) -> None:
        db = seeded()
        db.fail["table_stats"] = RuntimeError("no catalog")
        snap = D.collect_snapshot(db, now=NOW)
        self.assertTrue(any("no catalog" in e for e in snap.errors))
        ml = {f.name: f for f in snap.mailing_list}
        self.assertTrue(ml["m_commit_email"].has_rows)
        self.assertTrue(snap.cve_sources)


class FormatTest(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(D.format_bytes(0), "0 B")
        self.assertEqual(D.format_bytes(1023), "1023 B")
        self.assertEqual(D.format_bytes(1536), "1.5 KiB")
        self.assertEqual(D.format_bytes(3 * 1024**3), "3.0 GiB")
        self.assertEqual(D.format_bytes(5 * 1024**5), "5120.0 TiB")

    def test_format_count(self) -> None:
        self.assertEqual(D.format_count(1234567), "1,234,567")


class CleanVersionsTest(unittest.TestCase):
    def test_skips_and_captures(self) -> None:
        a = D.VersionRow(1, "r", "a", "tag", 5, D.STATE_INTERRUPTED)
        alias = D.VersionRow(1, "r", "alias", "branch", 5, D.STATE_INTERRUPTED)
        done = D.VersionRow(1, "r", "d", "tag", 6, D.STATE_DONE)
        bad = D.VersionRow(1, "r", "b", "tag", 7, D.STATE_INTERRUPTED)
        db = FakeDB()
        for cid in (5, 7):
            db.add("m_ingest_state", network_id=1, commit_id=cid, status=I.INGEST_PARSING, worker_id="")

        def fake_clean(_db, network_id, commit_id, tables):
            if commit_id == 7:
                raise I.RecoveryError("refs")
            return "rollback"

        with mock.patch("core.ingest_state.clean_interrupted_version", side_effect=fake_clean) as m:
            results = D.clean_versions(db, [a, alias, done, bad])
        self.assertEqual(m.call_count, 2)
        self.assertEqual([(r.version.ref_name, r.ok) for r in results], [("a", True), ("d", False), ("b", False)])
        self.assertEqual(results[0].method, "rollback")
        self.assertIn("parsed", results[1].error)
        self.assertIn("refs", results[2].error)
        self.assertEqual(db.rollbacks, 1)


    def test_rechecks_state_before_cleaning(self) -> None:
        finished = D.VersionRow(1, "r", "fin", "tag", 8, D.STATE_INTERRUPTED)
        resumed = D.VersionRow(1, "r", "res", "tag", 9, D.STATE_INTERRUPTED)
        stale = D.VersionRow(1, "r", "old", "tag", 11, D.STATE_INTERRUPTED)
        db = FakeDB()
        db.add("m_ingest_state", network_id=1, commit_id=8, status=I.INGEST_DONE, worker_id="")
        db.add("m_ingest_state", network_id=1, commit_id=9, status=I.INGEST_PARSING, worker_id="w1")
        db.add("m_ingest_state", network_id=1, commit_id=11, status=I.INGEST_PARSING, worker_id="w2")
        now = int(time.time())
        db.add("m_parse_node", node_id="w1", heartbeat_at=now)
        db.add("m_parse_node", node_id="w2", heartbeat_at=now - 10 * D.HEARTBEAT_WINDOW_S)

        with mock.patch("core.ingest_state.clean_interrupted_version", return_value="purge") as m:
            results = D.clean_versions(db, [finished, resumed, stale])
        self.assertEqual([r.ok for r in results], [False, False, True])
        self.assertIn("state changed", results[0].error)
        self.assertIn("w1", results[1].error)
        m.assert_called_once()
        self.assertEqual(m.call_args.args[2], 11)


class IngestStateCleanTest(unittest.TestCase):
    def _db(self) -> FakeDB:
        db = FakeDB()
        for table in I.PARTIAL_VERSION_TABLES:
            db.add(table, commit_id=5)
            db.add(table, commit_id=6)
        db.add("m_ingest_state", network_id=1, commit_id=5, status=I.INGEST_PARSING)
        db.add("m_ingest_state", network_id=1, commit_id=6, status=I.INGEST_DONE)
        return db

    def test_purge_partial_version(self) -> None:
        db = self._db()
        with mock.patch("core.checkpoints.expand_dependents") as exp:
            n = I.purge_partial_version(db, 5)
        exp.assert_called_once_with(db, 5)
        self.assertEqual(n, len(I.PARTIAL_VERSION_TABLES))
        for table in I.PARTIAL_VERSION_TABLES:
            self.assertEqual([r["commit_id"] for r in db.rows[table]], [6])
        self.assertGreaterEqual(db.commits, 1)
        self.assertEqual(I.purge_partial_version(db, 0), 0)
        self.assertEqual(I.purge_partial_version(None, 5), 0)

    def test_clean_rollback_path(self) -> None:
        db = self._db()
        with mock.patch.object(I, "rollback_version", return_value=True) as rb, \
                mock.patch.object(I, "purge_partial_version") as purge:
            self.assertEqual(I.clean_interrupted_version(db, 1, 5, TABLES), "rollback")
        rb.assert_called_once_with(db, 1, 5, TABLES)
        purge.assert_not_called()
        self.assertEqual([r["commit_id"] for r in db.rows["m_ingest_state"]], [6])

    def test_clean_purge_path(self) -> None:
        db = self._db()
        with mock.patch.object(I, "rollback_version", return_value=False), \
                mock.patch.object(I, "purge_partial_version") as purge:
            self.assertEqual(I.clean_interrupted_version(db, 1, 5, TABLES), "purge")
        purge.assert_called_once_with(db, 5)
        self.assertEqual([r["commit_id"] for r in db.rows["m_ingest_state"]], [6])

    def test_done_state_row_kept(self) -> None:
        db = self._db()
        with mock.patch.object(I, "rollback_version", return_value=True):
            I.clean_interrupted_version(db, 1, 6, TABLES)
        self.assertEqual(len(db.rows["m_ingest_state"]), 2)


def _cfg(host: str = "db.example", port: int = 3307, tunnel: bool = False) -> dict:
    return {
        "database": {
            "engine": "mariadb",
            "host": host,
            "port": port,
            "user": "u",
            "password": "p",
            "database": "kip",
            "unix_socket": "disabled",
            "ssh_tunnel": {"enabled": tunnel, "host": "jump", "user": "me"},
        }
    }


class _EnvGuard(unittest.TestCase):
    KEYS = ("DB_HOST", "DB_PORT", "DB_NAME", "MYSQL_HOST", "DB_SOCKET", "SSH_TUNNEL_ENABLED")

    def setUp(self) -> None:
        self._env = dict(os.environ)
        self._exported = dict(C._EXPORTED_ENV_VARS)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        C._EXPORTED_ENV_VARS.clear()
        C._EXPORTED_ENV_VARS.update(self._exported)


class ScopedEnvironTest(_EnvGuard):
    def test_exports_and_restores(self) -> None:
        os.environ["DB_HOST"] = "before-host"
        os.environ.pop("DB_NAME", None)
        C._EXPORTED_ENV_VARS["DB_HOST"] = "before-host"
        before_env = dict(os.environ)
        before_exported = dict(C._EXPORTED_ENV_VARS)
        with C.scoped_environ(_cfg()):
            self.assertEqual(os.environ["DB_HOST"], "db.example")
            self.assertEqual(os.environ["DB_PORT"], "3307")
            self.assertEqual(os.environ["DB_NAME"], "kip")
        self.assertEqual(dict(os.environ), before_env)
        self.assertEqual(C._EXPORTED_ENV_VARS, before_exported)
        self.assertNotIn("DB_NAME", os.environ)

    def test_restores_on_error(self) -> None:
        before_env = dict(os.environ)
        with self.assertRaises(ValueError), C.scoped_environ(_cfg()):
            raise ValueError("x")
        self.assertEqual(dict(os.environ), before_env)

    def test_cfg_not_mutated(self) -> None:
        cfg = _cfg()
        with C.scoped_environ(cfg):
            pass
        self.assertNotIn("resolved_unix_socket", cfg["database"])


class FakeEngine:
    instances: list[FakeEngine] = []

    def __init__(self) -> None:
        self.host = os.environ.get("DB_HOST")
        self.port = os.environ.get("DB_PORT")
        self.closed = False
        self.entered = False
        FakeEngine.instances.append(self)

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        return None

    def close(self) -> None:
        self.closed = True


class OpenDBSessionTest(_EnvGuard):
    def setUp(self) -> None:
        super().setUp()
        FakeEngine.instances = []
        self.tunnel_calls: list[tuple[dict, str]] = []

        @contextmanager
        def fake_tunnel(ssh_cfg, feature="database"):
            self.tunnel_calls.append((ssh_cfg, feature))
            yield 45678

        self.patches = [
            mock.patch("db_engine.get_db_engine", return_value=FakeEngine),
            mock.patch("core.tui.testing.open_test_tunnel", side_effect=fake_tunnel),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_direct(self) -> None:
        before = dict(os.environ)
        cfg = _cfg()
        with D.open_db_session(cfg) as db:
            self.assertTrue(db.entered)
            self.assertEqual((db.host, db.port), ("db.example", "3307"))
        self.assertEqual(self.tunnel_calls, [])
        self.assertTrue(FakeEngine.instances[0].closed)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(cfg, _cfg())

    def test_tunnel(self) -> None:
        before = dict(os.environ)
        cfg = _cfg(tunnel=True)
        with D.open_db_session(cfg) as db:
            self.assertEqual((db.host, db.port), ("127.0.0.1", "45678"))
        self.assertEqual(len(self.tunnel_calls), 1)
        self.assertEqual(self.tunnel_calls[0][1], "database")
        self.assertTrue(FakeEngine.instances[0].closed)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(cfg, _cfg(tunnel=True))

    def test_closed_on_error(self) -> None:
        with self.assertRaises(RuntimeError), D.open_db_session(_cfg()):
            raise RuntimeError("x")
        self.assertTrue(FakeEngine.instances[0].closed)

    def test_describe_target(self) -> None:
        self.assertIn("kip", D.describe_target(_cfg()))
        self.assertIn("via SSH", D.describe_target(_cfg(tunnel=True)))


if __name__ == "__main__":
    unittest.main()
