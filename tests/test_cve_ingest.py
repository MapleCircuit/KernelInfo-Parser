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

from core import DBLayout as D
from db_engine import SQLiteDB
from parser.cve import cve_ingest as ci
from parser.cve import cve_records as cr
from tests.test_webapp_cve import FIX, INTRO, STABLE_FIX, cna_record, other_record


CVE_TABLES = (D.m_cve_source, D.m_cve, D.m_cve_commit, D.m_cve_commit_hash, D.m_cve_file)


def cve_db() -> SQLiteDB:
    """An in-memory SQLite database with the CVE tables."""
    db = SQLiteDB(":memory:", foreign_keys=False)
    db.create_table(list(CVE_TABLES))
    return db


def ids(db: SQLiteDB) -> list[str]:
    """The cve_id of every stored m_cve row, sorted."""
    return sorted(str(r[0]) for r in db.select_rows("m_cve", ["cve_id"]))


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

            db = cve_db()
            db.insert_rows("m_cve", ["cve_id", "kind", "published", "updated", "score", "kev", "record_path", "title", "data"],
                           [("CVE-1999-0001", 1, 0, 0, 0, 0, "old.json", "stale", b"")])
            db.insert_rows("m_cve_commit", ["commit_hash", "role", "cve_id"], [("a" * 40, 1, "CVE-1999-0001")])
            res = ci.ingest(db, repo)
            self.assertEqual((res["state"], res["layout"], res["records"]), ("full", "cvelist", 2))
            self.assertEqual(ids(db), ["CVE-2024-0001", "CVE-2024-0002"], "a full ingestion replaces everything")
            links = {(str(r[0]), int(r[1]), str(r[2])) for r in db.select_rows("m_cve_commit", ["commit_hash", "role", "cve_id"])}
            self.assertIn((FIX, cr.ROLE_FIX, "CVE-2024-0002"), links)
            self.assertNotIn(("a" * 40, 1, "CVE-1999-0001"), links)
            source = ci.source_state(db)
            self.assertEqual((source["name"], source["layout"], source["head"], source["format"], source["records"]),
                             (repo.name, "cvelist", head1, cr.RECORD_FORMAT, 2))

            # Same HEAD: nothing to do
            self.assertEqual(ci.ingest(db, repo)["state"], "current")

            # A new HEAD: one record removed, one added, one rejected
            (d / "CVE-2024-0001.json").unlink()
            (d / "CVE-2024-0004.json").write_text(json.dumps(cna_record("CVE-2024-0004")))
            rejected = cna_record("CVE-2024-0002")
            rejected["cveMetadata"]["state"] = "REJECTED"
            (d / "CVE-2024-0002.json").write_text(json.dumps(rejected))
            self.commit(tmp)
            res2 = ci.ingest(db, repo)
            self.assertEqual((res2["state"], res2["changed"], res2["paths"]), ("incremental", 1, 3))
            self.assertEqual(ids(db), ["CVE-2024-0004"])
            self.assertEqual(ci.source_state(db)["records"], 1, "the source row follows the stored record count")

            # Another record format: everything again
            db.update_rows("m_cve_source", {"format": cr.RECORD_FORMAT - 1}, {"name": repo.name})
            self.assertEqual(ci.ingest(db, repo)["state"], "full")

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
            self.assertEqual(ci.ingest(cve_db(), Path(tmp)), {"state": "missing"})
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
            db = cve_db()
            db.insert_rows("m_cve_commit", ["commit_hash", "role", "cve_id"],
                           [(sha[:12], 1, "CVE-2024-0001"), ("deadbeef0000", 1, "CVE-2024-0001"),
                            (sha, 1, "CVE-2024-0002"), ("abcdef012345", 1, "CVE-2024-0003")])
            db.insert_rows("m_cve_commit_hash", ["repo_id", "prefix", "commit_hash"], [(7, "abcdef012345", b"\x01" * 20)])
            out = ci.resolve_commit_prefixes(db, tmp, 7)
            self.assertEqual(out, {"pending": 2, "resolved": 1}, "a full hash and a prefix with a row are not pending")
            stored = {(int(r[0]), str(r[1])): bytes(r[2]) for r in db.select_rows("m_cve_commit_hash", ["repo_id", "prefix", "commit_hash"])}
            self.assertEqual(stored[(7, sha[:12])], bytes.fromhex(sha))               # the unknown one gets no row
            self.assertNotIn((7, "deadbeef0000"), stored)
            self.assertEqual(ci.resolve_commit_prefixes(db, tmp, 7), {"pending": 1, "resolved": 0}, "an unresolved prefix is tried again")
        db = cve_db()
        db.insert_rows("m_cve_commit_hash", ["repo_id", "prefix", "commit_hash"], [(7, "abcdef012345", b"\x01" * 20)])
        db.insert_rows("m_cve_file", ["cve_id", "file_path"], [("CVE-1", "a.c")])
        ci.write(db, {"state": "full", "records": {}, "name": "x", "layout": "cvelist", "head": "ab" * 20})
        self.assertEqual(db.count_rows("m_cve_commit_hash"), 0)
        self.assertEqual(db.count_rows("m_cve_file"), 0)

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
        db = cve_db()
        ci._insert(db, {"path.json": ("path.json", rec)})
        files = sorted((str(r[0]), str(r[1])) for r in db.select_rows("m_cve_file", ["cve_id", "file_path"]))
        self.assertEqual(files, [("CVE-2024-99999", "fs/ext4/inode.c"), ("CVE-2024-99999", "fs/ext4/super.c")])

        ci._delete_ids(db, ["CVE-2024-99999"])
        self.assertEqual(db.count_rows("m_cve_file"), 0)
        self.assertEqual(ids(db), [])

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
