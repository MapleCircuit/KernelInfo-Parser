"""tests/test_commit_relations.py - Commit relations in the database (m_commit_relation, m_commit_link; D04).

Parser rows (parser/git_ast/commit_relations.py), their writers (Step 1, Step 6.5, the backfill) and the
webapp's database readers, compared with the SQLite relation index on temporary repositories.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import backfills
from core import ingest_state as I
from core.DBLayout import TABLES
from parser.git_ast import commit_relations as pcr

ENV = {**os.environ, "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@x.org", "GIT_COMMITTER_NAME": "Ann",
       "GIT_COMMITTER_EMAIL": "ann@x.org", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV,
                          input=stdin).stdout


class TestRows(unittest.TestCase):
    MSG = ("fix it\n\nThis reverts commit 123456789.\n"            # 9 digits: an odd-length prefix (B12)
           "commit " + "a" * 40 + " upstream.\n"
           "Fixes: abcdef012345 (\"bad\")\nFixes: abcdef012345 (\"bad\")\n"
           "Cc: stable@vger.kernel.org # 3.0+\nCc: stable@vger.kernel.org # 3.0+\nCc: <stable@kernel.org>\n"
           "Link: https://lore.kernel.org/r/Some@Thing\nLink: https://lore.kernel.org/r/" + "x" * 300 + "@y\n")

    def test_relation_rows(self) -> None:
        rels, links = pcr.relation_rows(self.MSG, "b" * 40)
        self.assertEqual(rels, [
            (pcr.KIND_FIXES, 0, "abcdef012345", "bad"),
            (pcr.KIND_REVERTS, 0, "123456789", ""),
            (pcr.KIND_UPSTREAM, 0, "a" * 40, ""),
            (pcr.KIND_STABLE_CC, 0, "", "3.0+"),          # identical annotations count once
            (pcr.KIND_STABLE_CC, 1, "", ""),
        ])
        self.assertEqual(links, ["some@thing"])            # the over-long message-id is dropped
        self.assertEqual(pcr.relation_rows("commit " + "a" * 40 + " upstream.", "a" * 40)[0], [])  # itself

    def test_table_rows_resolve_and_zero(self) -> None:
        rels, links = pcr.relation_rows(self.MSG)
        self.assertEqual(pcr.prefixes_to_resolve(rels), {"abcdef012345", "123456789"})
        rows, link_rows = pcr.table_rows(5, rels, links, {"abcdef012345": "abcdef012345" + "0" * 28})
        by_kind = {(r[1], r[2]): r for r in rows}
        self.assertEqual(by_kind[(pcr.KIND_FIXES, 0)][3], bytes.fromhex("abcdef012345" + "0" * 28))
        self.assertEqual(by_kind[(pcr.KIND_REVERTS, 0)][3], pcr.ZERO_HASH)       # unresolved: zero, prefix kept
        self.assertEqual(by_kind[(pcr.KIND_UPSTREAM, 0)][3], bytes.fromhex("a" * 40))
        self.assertEqual(link_rows, [(5, "some@thing")])

    def test_webapp_reexports_the_parsers(self) -> None:
        from webapp.backend.services import commit_relations as wcr
        self.assertIs(wcr.parse_relations, pcr.parse_relations)

    def test_seven_char_prefixes_and_links(self) -> None:
        """7-character prefixes for Fixes, Revert, and Upstream, plus Closes, Lore, and Patchwork links."""
        msg = (
            "Fixes: 1234567 (\"crash on boot\")\n"
            "This reverts commit 7654321.\n"
            "commit abcdef0 upstream.\n"
            "Closes: https://lore.kernel.org/r/issue-123@kernel.org\n"
            "Lore: https://lore.kernel.org/all/lore-msg@kernel.org\n"
            "Patchwork-Id: 987654\n"
        )
        rels, links = pcr.relation_rows(msg)
        self.assertEqual(rels, [
            (pcr.KIND_FIXES, 0, "1234567", "crash on boot"),
            (pcr.KIND_REVERTS, 0, "7654321", ""),
            (pcr.KIND_UPSTREAM, 0, "abcdef0", ""),
        ])
        self.assertIn("issue-123@kernel.org", links)
        self.assertIn("lore-msg@kernel.org", links)
        self.assertIn("987654", links)

    def test_subject_line_revert(self) -> None:
        """Subject line revert when body lacks 'This reverts commit ...'."""
        msg = "Revert \"1234567\" (\"broken change\")\n\nSome discussion why we reverted."
        rels, _links = pcr.relation_rows(msg)
        self.assertEqual(rels, [(pcr.KIND_REVERTS, 0, "1234567", "")])

    def test_resolve_prefixes_skips_full_shas(self) -> None:
        """Full 40-character SHAs are returned directly without subprocess invocation."""
        from webapp.backend.services import commit_relations as wcr
        full_sha = "e" * 40
        res = pcr.resolve_prefixes("/nonexistent/repo", [full_sha])
        self.assertEqual(res, {full_sha: full_sha})
        self.assertIs(wcr.normalize_mid, pcr.normalize_mid)

    def test_sqlite_pass_keeps_short_targets(self) -> None:
        from webapp.backend.services import commit_relations as wcr
        self.assertEqual(wcr._target_row(b"s", wcr.KIND_REVERTS, "123456789"), (b"s", 2, None, "123456789", "12345678", None))

    def test_rollback_plan_has_both_tables(self) -> None:
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        plan = {(d.table, d.where) for d in I.rollback_plan(TABLES, marks, 10, [])}
        self.assertIn(("m_commit_relation", "commit_id > %s"), plan)
        self.assertIn(("m_commit_link", "commit_id > %s"), plan)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestResolvePrefixes(unittest.TestCase):
    def test_unique_missing_and_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-q", "-b", "master")
            # 1000 commits through fast-import: some share their first four hex digits
            lines = []
            for i in range(1, 1001):
                lines += [f"commit refs/heads/master", f"mark :{i}", "committer A <a@x> 1 +0000", f"data {len(str(i))}", str(i)]
                if i > 1:
                    lines.append(f"from :{i - 1}")
                lines.append("")
            git(repo, "fast-import", "--quiet", stdin="\n".join(lines) + "\n")
            shas = git(repo, "rev-list", "master").split()
            by4: dict[str, list[str]] = {}
            for s in shas:
                by4.setdefault(s[:4], []).append(s)
            shared = next((p for p, v in by4.items() if len(v) > 1), None)
            unique = shas[0][:10]
            got = pcr.resolve_prefixes(repo, [unique, "DEADBEEF00", *( [shared] if shared else [])])
            self.assertEqual(got[unique], shas[0])
            self.assertNotIn("deadbeef00", got)
            if shared:
                self.assertNotIn(shared, got)


def _cursor_class():
    from tests.test_release_stats import _Cursor

    class _Cur(_Cursor):
        """The release statistics' sqlite cursor, plus the backfill state upsert."""

        def execute(self, sql: str, params=()) -> None:
            super().execute(sql.replace("ON DUPLICATE KEY UPDATE note = VALUES(note)",
                                        "ON CONFLICT (commit_id, kind, idx) DO UPDATE SET note = excluded.note"), params)

    return _Cur


class Store:
    """In-memory database with what the parser writes for a repository: m_commit (every commit, ids in
    topological order), relation rows through --backfill-commit-relations, release membership through
    the release statistics."""

    def __init__(self, repo: Path, with_relations: bool = True) -> None:
        from tests.test_release_stats import _Connection
        self.engine = _Connection()
        cur_class = _cursor_class()
        self.engine.cursor = lambda: cur_class(self.engine.db)
        self.cnx = self.engine
        self.db = self.engine.db
        self.db.executescript("""
            CREATE TABLE m_ingest_state (network_id INT, commit_id INT, status INT);
            CREATE TABLE m_commit (commit_id INTEGER PRIMARY KEY, network_id INT, commit_hash BLOB, author_id INT,
                author_date INT, committer_id INT, committer_date INT, subject TEXT, message TEXT);
            CREATE TABLE m_commit_relation (commit_id INT, kind INT, idx INT, target_hash BLOB, target_prefix TEXT,
                note TEXT, PRIMARY KEY (commit_id, kind, idx));
            CREATE TABLE m_commit_link (commit_id INT, message_id TEXT, PRIMARY KEY (commit_id, message_id));
            INSERT INTO m_commit VALUES (0, 0, zeroblob(20), 0, 0, 0, 0, '', '');
        """)
        out = git(repo, "log", "--all", "--topo-order", "--reverse", "--format=%x1e%H%x1f%at%x1f%s%x1f%B")
        for i, rec in enumerate(r for r in out.split("\x1e") if r.strip()):
            sha, at, subject, body = rec.split("\x1f", 3)
            self.db.execute("INSERT INTO m_commit VALUES (?, 1, ?, 1, ?, 1, ?, ?, ?)",
                            (i + 1, bytes.fromhex(sha), int(at), int(at), subject, body.rstrip("\n")))
        if with_relations:
            self.summary = backfills.backfill_commit_relations(self.engine, str(repo))
        from parser.git_ast import release_stats
        release_stats.ingest(self.engine, repo, repo_id=1, workers=2)
        self.statements: list[str] = []

    def dict_cursor(self):
        store = self

        class DictCur:
            def __init__(self) -> None:
                self.rows: list = []

            def execute(self, sql: str, params=()) -> None:
                store.statements.append(sql)
                cur = store.db.execute(sql.replace("%s", "?"), tuple(params))
                cols = [d[0] for d in cur.description or ()]
                self.rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.rows[0] if self.rows else None

        ctx = mock.MagicMock()
        ctx.__enter__.side_effect = lambda: DictCur()
        return ctx

    def patches(self) -> list:
        from webapp.backend.database import helpers, pool
        from webapp.backend.services import commit_relations as wcr
        from webapp.backend.services import release_service as rs
        ctx = self.dict_cursor()
        return [
            mock.patch.object(wcr, "get_db_cursor", return_value=ctx),
            mock.patch.object(wcr, "get_active_network_id", return_value=1),
            mock.patch.object(wcr, "get_active_repo_id", return_value=1),
            mock.patch.object(rs, "get_db_cursor", return_value=ctx),
            mock.patch.object(rs, "get_active_repo_id", return_value=1),
            mock.patch.object(pool, "get_db_cursor", return_value=ctx),
            mock.patch.object(helpers, "get_active_network_id", return_value=1),
            mock.patch.object(helpers, "get_active_repo_id", return_value=1),
        ]


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestOnRepository(unittest.TestCase):
    """The repository of tests/test_webapp_release_index.py: a fix with Fixes:/Cc: stable/Link:, its
    backport on a stable branch, a revert."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_webapp_release_index import TestIndexesOnRepository as T
        T.setUpClass()
        cls.T = T
        cls.repo = T.repo
        cls.store = Store(cls.repo)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.T.tearDownClass()

    def setUp(self) -> None:
        from webapp.backend.services import commit_relations as wcr
        from webapp.backend.services import release_service as rs
        from webapp.backend.services.jobs import JobManager
        self.wcr, self.rs = wcr, rs
        self.patches = [
            mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.T.cache.name}),
            mock.patch.object(rs, "get_repo_dir", lambda: self.repo),
            mock.patch.object(wcr, "get_repo_dir", lambda: self.repo),
            mock.patch.object(rs, "jobs", JobManager(workers=1)),
            mock.patch.object(wcr, "jobs", JobManager(workers=1)),
            *self.store.patches(),
        ]
        for p in self.patches:
            p.start()
        rs._TAGS_CACHE.clear()
        rs._DB_COVERAGE.clear()
        wcr._DB_READY.clear()
        wcr._STALE.clear()
        self.store.statements.clear()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.wcr._DB_READY.clear()

    def all_commits(self) -> list[str]:
        return git(self.repo, "rev-list", "--all").split()

    def test_backfill_is_idempotent_and_resumable(self) -> None:
        db = self.store.db
        rows = db.execute("SELECT COUNT(*) FROM m_commit_relation").fetchone()[0]
        state = db.execute("SELECT note FROM m_commit_relation WHERE commit_id = 0").fetchone()[0]
        self.assertTrue(state.startswith(pcr.BACKFILL_DONE))
        again = backfills.backfill_commit_relations(self.store.engine, str(self.repo), from_commit_id=0)
        self.assertEqual(again["state"], "done")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM m_commit_relation").fetchone()[0], rows)
        # Interrupted after commit 2: the next run starts there
        db.execute("UPDATE m_commit_relation SET note = 'next:2' WHERE commit_id = 0")
        resumed = backfills.backfill_commit_relations(self.store.engine, str(self.repo))
        self.assertEqual(resumed["commits"], db.execute("SELECT COUNT(*) FROM m_commit WHERE commit_id > 2").fetchone()[0])
        fixed = db.execute("SELECT target_hash FROM m_commit_relation WHERE kind = 1").fetchone()[0]
        self.assertEqual(bytes(fixed).hex(), self.T.bug)                   # Fixes: <12 digits> resolved

    def test_database_answers_like_the_relation_index(self) -> None:
        self.assertTrue(self.wcr.db_ready())
        self.wcr.relation_index.build()
        for sha in self.all_commits():
            db = self.wcr.db_relations(sha)
            sq = self.wcr.relation_index.relations(sha)
            self.assertEqual(db["source"], "db")
            for field in ("fixes", "fixed_by", "reverts", "reverted_by", "upstream", "backports"):
                self.assertEqual([(i["sha"], i["subject"], i.get("date")) for i in db[field]],
                                 [(i["sha"], i["subject"], i.get("date")) for i in sq[field]], (sha, field))
            self.assertEqual(db["stable_cc"], sq["stable_cc"], sha)
            self.assertEqual(db["links"], sq["links"], sha)
            self.assertEqual((db["date"], db["subject"]), (sq["date"], sq["subject"]))
        self.assertEqual(self.wcr.db_fixes_of(self.T.fix), [self.T.bug])
        self.assertEqual(self.wcr.db_backports_of(self.T.fix), [self.T.backport])
        self.assertEqual(self.wcr.db_commits_for_mids(["<FIX-1@x.org>"]), {"fix-1@x.org": [self.T.fix]})
        counts = self.wcr.db_counts([self.T.bug, self.T.fix])
        self.assertEqual(counts[self.T.bug]["fixed_by"], 1)
        self.assertEqual((counts[self.T.fix]["fixes"], counts[self.T.fix]["backports"], counts[self.T.fix]["stable_cc"]), (1, 1, True))
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in self.store.statements))

    def test_relations_need_no_sqlite_index(self) -> None:
        with mock.patch.object(self.wcr.relation_index, "build") as build, \
                mock.patch.object(self.wcr, "ensure_index") as ensure:
            r = self.wcr.relations(self.T.bug)
        build.assert_not_called()
        ensure.assert_not_called()
        self.assertEqual([f["sha"] for f in r["fixed_by"]], [self.T.fix])

    def test_not_ready_or_not_ingested_falls_back(self) -> None:
        db = self.store.db
        db.execute("UPDATE m_commit_relation SET note = 'next:1' WHERE commit_id = 0")
        try:
            self.assertFalse(self.wcr.db_ready())
            self.assertIsNone(self.wcr.db_relations(self.T.fix))
            self.assertIsNone(self.wcr.db_commits_for_mids(["fix-1@x.org"]))
        finally:
            db.execute("UPDATE m_commit_relation SET note = 'done:9' WHERE commit_id = 0")
        self.wcr._DB_READY.clear()
        self.assertIsNone(self.wcr.db_relations("f" * 40))                   # not in m_commit
        state = self.wcr.relations(self.T.fix)
        self.assertEqual(state["source"], "db")

    def test_backports_complete_only_when_releases_are_ingested(self) -> None:
        self.assertTrue(self.wcr.tags_ingested(["v1.0", "v1.0.1"]))
        key = bytes.fromhex(self.T.backport)
        row = self.store.db.execute("SELECT * FROM m_commit WHERE commit_hash = ?", (key,)).fetchone()
        self.store.db.execute("DELETE FROM m_commit WHERE commit_hash = ?", (key,))
        try:
            self.wcr._DB_READY.clear()
            self.assertFalse(self.wcr.tags_ingested(["v1.0", "v1.0.1"]))
        finally:
            self.store.db.execute("INSERT INTO m_commit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)

    def test_merged_trailers_from_the_database(self) -> None:
        from webapp.backend.services import merged_service
        with mock.patch.object(self.wcr, "ensure_index") as ensure, \
                mock.patch("webapp.backend.services.commit_facts.git_facts", return_value={}):
            out = merged_service._from_trailers("fix-1@x.org", 0)
        ensure.assert_not_called()
        self.assertEqual([c["hash"] for c in out], [self.T.fix])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestStableReport(unittest.TestCase):
    """The stable-missing report from SQL equals the SQLite answer (tests/test_webapp_stable.py repository)."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_webapp_stable import TestMissingReport as T
        T.setUpClass()
        cls.T = T
        cls.repo = T.repo
        cls.store = Store(cls.repo)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.T.tearDownClass()

    def test_database_report_equals_the_sqlite_report(self) -> None:
        from webapp.backend.services import commit_relations as wcr
        from webapp.backend.services import release_service as rs
        from webapp.backend.services import stable_service as ss
        from webapp.backend.services.jobs import JobManager
        patches = [
            mock.patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.T.cache.name}),
            mock.patch.object(rs, "get_repo_dir", lambda: self.repo),
            mock.patch.object(wcr, "get_repo_dir", lambda: self.repo),
            mock.patch("webapp.backend.config.get_repo_dir", lambda: self.repo),
            mock.patch.object(rs, "jobs", JobManager(workers=1)),
            mock.patch.object(wcr, "jobs", JobManager(workers=1)),
            *self.store.patches(),
        ]
        for p in patches:
            p.start()
        try:
            rs._TAGS_CACHE.clear()
            rs._DB_COVERAGE.clear()
            wcr._DB_READY.clear()
            scope = ss.series_scope("v1.0")
            self.assertTrue(ss.db_scope_covered(scope))
            with mock.patch.object(wcr.relation_index, "build") as build:
                db = ss.build_report("v1.0", use_db=True)
            build.assert_not_called()
            self.assertEqual(db["source"], "db")
            self.store.statements.clear()
            rs.release_index.build()
            wcr.relation_index.build()
            sq = ss._report_sqlite("v1.0", scope)
            self.assertEqual({**db, "source": ""}, {**sq, "source": ""})
            shas = {c["sha"] for c in db["candidates"]}
            self.assertEqual(shas, {self.T.cc, self.T.fix})
        finally:
            for p in patches:
                p.stop()
            wcr._DB_READY.clear()


class TestWriters(unittest.TestCase):
    def test_step1_inserts_relation_rows_with_the_commits(self) -> None:
        import main
        from tests.test_pipeline_steps import _commit, _reset_mock_db
        from db_engine.mock_db import MockDB
        from core.DBLayout import m_commit_link, m_commit_relation
        _reset_mock_db()
        try:
            c1, c2 = "1" * 40, "2" * 40
            fix = _commit(c2, "A", "a@x", [c1])
            fix.message = f"fix\n\nFixes: {c1} (\"x\")\nLink: https://lore.kernel.org/r/m@x"
            with MockDB() as db, mock.patch.object(main.MF, "repo_dir", None, create=True):
                ids = main._ingest_commit_range(db, [fix, _commit(c1, "A", "a@x", [])], network_id=1)
                rel = list(MockDB._global_store[m_commit_relation.table_name].values())
                links = list(MockDB._global_store[m_commit_link.table_name].values())
            flat = [r if isinstance(r, tuple) else tuple(r) for r in rel]
            self.assertTrue(any(ids[c2] in r and bytes.fromhex(c1) in r for r in flat), flat)
            self.assertTrue(any("m@x" in (r if isinstance(r, tuple) else tuple(r)) for r in links), links)
        finally:
            _reset_mock_db()

    def test_step65_stages_rows_of_commits_it_inserts(self) -> None:
        import main
        te = mock.MagicMock()
        with mock.patch.object(main.G, "TE", te):
            n_rel, n_link = main.stage_commit_relations([(7, "b" * 40, "x\n\nCc: stable@vger.kernel.org\nMessage-Id: <a@b>")], None)
        self.assertEqual((n_rel, n_link), (1, 1))
        tables = [c.args[0] for c in te.set.call_args_list]
        from core.DBLayout import m_commit_link, m_commit_relation
        self.assertEqual(tables, [m_commit_relation.table_id, m_commit_link.table_id])

    def test_automatic_backfill_runs_until_done(self) -> None:
        import main
        mock_db = mock.MagicMock(spec=[])
        mock_db.query_one = mock.MagicMock(side_effect=[("next:5",), None])
        mock_db.execute = mock.MagicMock()
        mock_db.fetchone = mock.MagicMock(side_effect=[("next:5",), None])
        db = mock.MagicMock()
        db.__enter__.return_value = mock_db
        args = mock.MagicMock(no_backfill=False)
        with mock.patch.object(main.G, "DB", return_value=db), \
                mock.patch.object(backfills, "backfill_commit_relations") as run:
            main.run_automatic_backfills(args)
        run.assert_called_once()
        mock_db.query_one.side_effect = [("done:5",), None]
        mock_db.fetchone.side_effect = [("done:5",), None]
        with mock.patch.object(main.G, "DB", return_value=db), \
                mock.patch.object(backfills, "backfill_commit_relations") as run:
            main.run_automatic_backfills(args)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
