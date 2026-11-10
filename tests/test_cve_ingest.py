"""tests/test_cve_ingest.py - The parser's CVE list ingestion (parser/cve/cve_ingest.py, core/API.md section 9)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser.cve import cve_ingest as ci
from parser.cve import cve_records as cr
from tests.test_webapp_cve import FIX, INTRO, STABLE_FIX, cna_record, other_record


class RecordingCursor:
    """Executes nothing: records the statements and answers COUNT(*) / the m_cve_source SELECT."""

    def __init__(self, conn: "RecordingConnection") -> None:
        self.conn = conn
        self._row = None

    def execute(self, sql: str, params=()) -> None:
        self.conn.statements.append((sql, tuple(params)))
        if sql.startswith("SELECT COUNT(*) FROM m_cve"):
            self._row = (self.conn.count,)
        elif "FROM m_cve_source" in sql:
            self._row = self.conn.source
        else:
            self._row = None

    def executemany(self, sql: str, rows) -> None:
        rows = list(rows)
        self.conn.statements.append((sql, rows))
        if sql.startswith("INSERT INTO m_cve "):
            self.conn.count += len(rows)

    def fetchone(self):
        return self._row

    def close(self) -> None:
        pass


class RecordingConnection:
    def __init__(self, source=None, count: int = 0) -> None:
        self.statements: list = []
        self.source = source
        self.count = count
        self.commits = self.rollbacks = 0

    def query(self, sql: str, params=(), dictionary: bool = True):
        cur = self.cursor()
        cur.execute(sql, params)
        res = cur.fetchall()
        return res if res is not None else []

    def query_one(self, sql: str, params=(), dictionary: bool = True):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.fetchone()

    def execute(self, sql: str, params=()) -> int:
        cur = self.cursor()
        cur.execute(sql, params)
        return 1

    def executemany(self, sql: str, params_seq) -> int:
        cur = self.cursor()
        cur.executemany(sql, params_seq)
        return len(params_seq)

    def cursor(self):
        return RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def sql(self, prefix: str) -> list:
        return [params for sql, params in self.statements if sql.startswith(prefix)]


class TestRows(unittest.TestCase):
    def test_record_and_commit_rows(self) -> None:
        rec = cr.parse_record(cna_record("CVE-2024-0001"))
        row = ci.record_row("cves/2024/0xxx/CVE-2024-0001.json", rec)
        self.assertEqual(row[:7], ("CVE-2024-0001", cr.KINDS["cna"], 1714521600, 0, 78, 0, "cves/2024/0xxx/CVE-2024-0001.json"))
        self.assertEqual(json.loads(zlib.decompress(row[8])), json.loads(json.dumps(rec)))
        self.assertEqual(sorted(ci.commit_rows(rec)), sorted([(FIX, cr.ROLE_FIX, "CVE-2024-0001"), (INTRO, cr.ROLE_INTRODUCED, "CVE-2024-0001"),
                                                              (STABLE_FIX, cr.ROLE_FIX, "CVE-2024-0001")]))
        other = cr.parse_record(other_record("CVE-2016-5195", "Linux kernel before 4.8.3",
                                             ["https://git.kernel.org/linus/" + FIX[:12]]))
        self.assertEqual(ci.commit_rows(other), [(FIX[:12], cr.ROLE_FIX, "CVE-2016-5195")])
        self.assertEqual(ci.record_row("x", other)[5], 1, "listed in CISA's KEV")

    def test_cve_short_sha_and_cwe(self) -> None:
        data = {
            "cveMetadata": {
                "cveId": "CVE-2024-9999",
                "state": "PUBLISHED",
                "assignerShortName": "Linux",
            },
            "containers": {
                "cna": {
                    "problemTypes": [
                        {
                            "descriptions": [
                                {
                                    "cweId": "CWE-416",
                                    "description": "Use After Free",
                                }
                            ]
                        }
                    ],
                    "affected": [
                        {
                            "product": "Linux",
                            "versions": [
                                {
                                    "versionType": "git",
                                    "version": "123456789abc",
                                    "lessThan": "abcdef123456",
                                    "status": "affected",
                                }
                            ]
                        }
                    ]
                }
            }
        }
        rec = cr.parse_record(data)
        self.assertIsNotNone(rec)
        self.assertIn("CWE-416", rec.get("cwes", []))
        self.assertEqual(len(rec.get("ranges", [])), 1)
        self.assertEqual(rec["ranges"][0]["introduced"], "123456789abc")
        self.assertEqual(rec["ranges"][0]["fixed"], "abcdef123456")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestIngest(unittest.TestCase):
    env = {**os.environ, "GIT_AUTHOR_NAME": "c", "GIT_AUTHOR_EMAIL": "c@x", "GIT_COMMITTER_NAME": "c",
           "GIT_COMMITTER_EMAIL": "c@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def commit(self, tmp: str) -> str:
        subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", "x"], check=True, env=self.env)
        return subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True, env=self.env, capture_output=True, text=True).stdout.strip()

    def test_cvelist_full_then_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True, env=self.env)
            d = repo / "cves" / "2024" / "0xxx"
            d.mkdir(parents=True)
            for i in (1, 2):
                (d / f"CVE-2024-{i:04d}.json").write_text(json.dumps(cna_record(f"CVE-2024-{i:04d}"), indent=4))
            (d / "CVE-2024-0003.json").write_text(json.dumps(other_record("CVE-2024-0003", "A bug in a web server", [])))
            (repo / "cves" / "delta.json").write_text("{}")
            head1 = self.commit(tmp)

            conn = RecordingConnection()
            res = ci.ingest(conn, repo)
            self.assertEqual((res["state"], res["layout"], res["records"]), ("full", "cvelist", 2))
            self.assertEqual([p for s, p in conn.statements if s == "DELETE FROM m_cve_commit"], [()], "a full ingestion replaces everything")
            inserted = [r[0] for rows in conn.sql("INSERT INTO m_cve ") for r in rows]
            self.assertEqual(sorted(inserted), ["CVE-2024-0001", "CVE-2024-0002"])
            links = {(r[0], r[1], r[2]) for rows in conn.sql("INSERT IGNORE INTO m_cve_commit") for r in rows}
            self.assertIn((FIX, cr.ROLE_FIX, "CVE-2024-0002"), links)
            source = conn.sql("INSERT INTO m_cve_source")[0]
            self.assertEqual(source[:5], (repo.name, "cvelist", bytes.fromhex(head1), cr.RECORD_FORMAT, 2))
            self.assertEqual((conn.commits, conn.rollbacks), (1, 0))

            # Same HEAD: nothing to do
            state = (repo.name, "cvelist", bytes.fromhex(head1), cr.RECORD_FORMAT, 2, 1)
            self.assertEqual(ci.ingest(RecordingConnection(source=state), repo)["state"], "current")

            # A new HEAD: one record removed, one added, one rejected
            (d / "CVE-2024-0001.json").unlink()
            (d / "CVE-2024-0004.json").write_text(json.dumps(cna_record("CVE-2024-0004")))
            rejected = cna_record("CVE-2024-0002")
            rejected["cveMetadata"]["state"] = "REJECTED"
            (d / "CVE-2024-0002.json").write_text(json.dumps(rejected))
            self.commit(tmp)
            conn2 = RecordingConnection(source=state, count=2)
            res2 = ci.ingest(conn2, repo)
            self.assertEqual((res2["state"], res2["changed"], res2["paths"]), ("incremental", 1, 3))
            deleted = sorted(p for params in conn2.sql("DELETE FROM m_cve WHERE") for p in params)
            self.assertEqual(deleted, ["CVE-2024-0001", "CVE-2024-0002", "CVE-2024-0004"])
            self.assertEqual([r[0] for rows in conn2.sql("INSERT INTO m_cve ") for r in rows], ["CVE-2024-0004"])
            self.assertEqual([sql for sql, _p in conn2.statements if sql in ("DELETE FROM m_cve_commit", "DELETE FROM m_cve_source")], [])

            # Another record format: everything again
            old_format = (*state[:3], cr.RECORD_FORMAT - 1, *state[4:])
            self.assertEqual(ci.ingest(RecordingConnection(source=old_format), repo)["state"], "full")

    def test_vulns_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True, env=self.env)
            (repo / "cve" / "published" / "2024").mkdir(parents=True)
            (repo / "cve" / "published" / "2024" / "CVE-2024-00001.json").write_text(json.dumps(cna_record("CVE-2024-00001")))
            (repo / "cve" / "published" / "2024" / "CVE-2024-00001.sha1").write_text(FIX)
            self.commit(tmp)
            plan = ci.prepare(repo, None)
            self.assertEqual((plan["state"], plan["layout"]), ("full", "vulns"))
            self.assertEqual(list(plan["records"]), ["CVE-2024-00001"])

    def test_not_a_cve_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True, env=self.env)
            (Path(tmp) / "README").write_text("x")
            self.commit(tmp)
            self.assertEqual(ci.ingest(RecordingConnection(), Path(tmp)), {"state": "missing"})
        self.assertIsNone(ci.repo_dir(""), "an empty cve.repo_dir turns the CVE list off")


if __name__ == "__main__":
    unittest.main()


class TestCommitPrefixes(unittest.TestCase):
    """Abbreviated CVE commit hashes resolved in the kernel repository (m_cve_commit_hash, D09)."""

    env = TestIngest.env

    def test_prefixes_resolved_ambiguous_left_and_full_clears(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True, env=self.env)
            (Path(tmp) / "a").write_text("1")
            sha = TestIngest.commit(self, tmp)
            db = mock.MagicMock()
            db.query.return_value = [(sha[:12],), ("deadbeef0000",)]
            out = ci.resolve_commit_prefixes(db, tmp, 7)
            self.assertEqual(out, {"pending": 2, "resolved": 1})
            select = db.query.call_args_list[0].args
            self.assertIn("NOT EXISTS (SELECT 1 FROM m_cve_commit_hash h", select[0])   # only prefixes without a row
            self.assertEqual(select[1], (7,))
            rows = db.executemany.call_args.args[1]
            self.assertEqual(rows, [(7, sha[:12], bytes.fromhex(sha))])               # the unknown one gets no row
        conn = RecordingConnection()
        ci.write(conn, {"state": "full", "records": {}, "name": "x", "layout": "cvelist", "head": "ab" * 20})
        self.assertIn("DELETE FROM m_cve_commit_hash", [sql for sql, _ in conn.statements])
        self.assertIn("DELETE FROM m_cve_file", [sql for sql, _ in conn.statements])

    def test_m_cve_file_insertion_and_deletion(self) -> None:
        """m_cve_file table is populated on insert and cleaned up on delete."""
        from parser.cve import cve_records as cr
        rec = {
            "id": "CVE-2024-99999",
            "kind": "cna",
            "published": "2024-01-01",
            "updated": "2024-01-02",
            "cvss": {"score": 7.5},
            "kev": None,
            "title": "Bug in fs/ext4",
            "files": ["fs/ext4/inode.c", "fs/ext4/super.c"],
            "ranges": [],
            "entries": [],
        }
        conn = RecordingConnection()
        cur = conn.cursor()
        ci._insert(cur, {"path.json": ("path.json", rec)})
        file_inserts = [
            (sql, rows)
            for sql, rows in conn.statements
            if "INSERT IGNORE INTO m_cve_file" in sql
        ]
        self.assertEqual(len(file_inserts), 1)
        self.assertEqual(
            file_inserts[0][1],
            [("CVE-2024-99999", "fs/ext4/inode.c"), ("CVE-2024-99999", "fs/ext4/super.c")],
        )

        ci._delete_ids(cur, ["CVE-2024-99999"])
        delete_stmts = [sql for sql, _ in conn.statements if "DELETE FROM m_cve_file" in sql]
        self.assertEqual(len(delete_stmts), 1)

    def test_root_commit_12_char_filtered(self) -> None:
        """Abbreviated 12-char root commit 1da177e4c3f4 is filtered out as introducing commit."""
        from parser.cve import cve_records as cr
        raw_rec = {
            "cveMetadata": {"cveId": "CVE-2024-11111", "state": "PUBLISHED", "assignerShortName": "Linux"},
            "containers": {
                "cna": {
                    "descriptions": [{"lang": "en", "value": "A kernel issue since the beginning"}],
                    "affected": [
                        {
                            "product": "Linux",
                            "programFiles": ["kernel/fork.c"],
                            "versions": [
                                {
                                    "version": "1da177e4c3f4",
                                    "lessThan": "b" * 40,
                                    "status": "affected",
                                    "versionType": "git",
                                }
                            ],
                        }
                    ],
                }
            },
        }
        parsed = cr.parse_record(raw_rec)
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed["ranges"][0]["introduced"])
        self.assertEqual(parsed["ranges"][0]["fixed"], "b" * 40)

    def test_version_rollback_does_not_touch_it(self) -> None:
        from core import ingest_state as I
        from core.DBLayout import TABLES
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        self.assertNotIn("m_cve_commit_hash", {d.table for d in I.rollback_plan(TABLES, marks, 10, [3])})
