"""tests/test_cve_releases.py - Pre-evaluated release-to-CVE verdicts (parser/cve/cve_releases.py)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import DBLayout as D
from core import backfills
from db_engine import SQLiteDB
from parser.cve import cve_ingest as ci
from parser.cve import cve_releases as crl
from parser.git_ast import release_stats as rst
from tests.db_fixtures import patch_reader
from webapp.backend.services import commit_relations as crel
from webapp.backend.services import cve_service as cs
from webapp.backend.services import release_service as rs


ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@kernel.org",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@kernel.org",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        env=ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_file(repo: Path, rel: str, content: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _cna_git_cve(cve_id: str, intro: str | None, fix: str | None) -> dict:
    ver: dict = {"version": intro or "1da177e4c3f4", "status": "affected", "versionType": "git"}
    if fix:
        ver["lessThan"] = fix
    return {
        "cveMetadata": {
            "cveId": cve_id,
            "state": "PUBLISHED",
            "assignerShortName": "Linux",
            "datePublished": "2024-06-01T00:00:00",
        },
        "containers": {
            "cna": {
                "title": f"Title for {cve_id}",
                "descriptions": [{"lang": "en", "value": f"Bug described by {cve_id}"}],
                "metrics": [{"cvssV3_1": {"baseScore": 7.5, "baseSeverity": "HIGH"}}],
                "affected": [
                    {
                        "defaultStatus": "unknown",
                        "programFiles": ["net/core/dev.c"],
                        "versions": [ver],
                    }
                ],
            }
        },
    }


def _commit_cve(cve_id: str, fix_sha: str) -> dict:
    return {
        "cveMetadata": {
            "cveId": cve_id,
            "state": "PUBLISHED",
            "assignerShortName": "mitre",
            "datePublished": "2024-02-01",
        },
        "containers": {
            "cna": {
                "descriptions": [{"lang": "en", "value": f"Linux kernel bug {cve_id}"}],
                "affected": [{"vendor": "n/a", "product": "Linux", "versions": [{"version": "n/a", "status": "affected"}]}],
                "references": [{"url": f"https://git.kernel.org/linus/{fix_sha}"}],
            }
        },
    }


def _text_cve(cve_id: str, less_than: str) -> dict:
    return {
        "cveMetadata": {
            "cveId": cve_id,
            "state": "PUBLISHED",
            "assignerShortName": "mitre",
            "datePublished": "2020-01-01",
        },
        "containers": {
            "cna": {
                "descriptions": [{"lang": "en", "value": f"In the Linux kernel before {less_than}, bug."}],
                "affected": [{"vendor": "n/a", "product": "n/a", "versions": [{"version": "n/a", "status": "affected"}]}],
            }
        },
    }


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestCveReleases(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.krepo = base / "linux"
        self.crepo = base / "cvelistV5"
        self.krepo.mkdir()
        self.crepo.mkdir()

        # Initialize kernel repo with v3.0, v3.1-rc1, v3.1, and stable v3.0.1
        _git(self.krepo, "init", "-q", "-b", "master")
        self.c_init = _commit_file(self.krepo, "a.c", "v0\n", "initial")
        self.c_intro = _commit_file(self.krepo, "a.c", "bug\n", "introduce bug")
        _git(self.krepo, "tag", "v3.0")

        # Stable branch v3.0.1 backporting c_fix_commit
        _git(self.krepo, "checkout", "-q", "-b", "stable-3.0", "v3.0")

        # Back on master: v3.1-rc1 introduces c_intro2; v3.1 fixes c_intro and c_intro2
        _git(self.krepo, "checkout", "-q", "master")
        self.c_intro2 = _commit_file(self.krepo, "b.c", "bug2\n", "introduce bug2")
        _git(self.krepo, "tag", "v3.1-rc1")
        self.c_fix1 = _commit_file(self.krepo, "a.c", "fixed\n", f"fix bug1\n\nFixes: {self.c_intro[:12]} (\"introduce bug\")")
        self.c_fix2 = _commit_file(self.krepo, "b.c", "fixed2\n", f"fix bug2\n\nFixes: {self.c_intro2[:12]} (\"introduce bug2\")")
        _git(self.krepo, "tag", "v3.1")

        # Backport c_fix2 onto stable-3.0 as v3.0.1
        _git(self.krepo, "checkout", "-q", "stable-3.0")
        self.c_bp2 = _commit_file(
            self.krepo,
            "b.c",
            "bp2\n",
            f"fix bug2 stable\n\ncommit {self.c_fix2} upstream.\n",
        )
        _git(self.krepo, "tag", "v3.0.1")
        _git(self.krepo, "checkout", "-q", "master")

        # Initialize CVE repo
        _git(self.crepo, "init", "-q", "-b", "main")
        self.cve_dir = self.crepo / "cves" / "2024" / "0xxx"
        self.cve_dir.mkdir(parents=True)
        # 1. CNA git range (abbreviated prefix for fix1!)
        (self.cve_dir / "CVE-2024-0001.json").write_text(
            json.dumps(_cna_git_cve("CVE-2024-0001", self.c_intro, self.c_fix1[:12]))
        )
        # 2. commit CVE pointing to c_fix2 (has Fixes: c_intro2 and backport c_bp2 in v3.0.1)
        (self.cve_dir / "CVE-2024-0002.json").write_text(
            json.dumps(_commit_cve("CVE-2024-0002", self.c_fix2[:12]))
        )
        # 3. text CVE before 3.1
        (self.cve_dir / "CVE-2024-0003.json").write_text(
            json.dumps(_text_cve("CVE-2024-0003", "3.1"))
        )
        _git(self.crepo, "add", "-A")
        _git(self.crepo, "commit", "-q", "-m", "initial cves")
        self.head1 = _git(self.crepo, "rev-parse", "HEAD")

        # Set up in-memory SQLite DB with full schema
        self.db = SQLiteDB(":memory:", foreign_keys=True)
        self.db.create_table(list(D.TABLES))
        self.db.insert_rows("m_project", ["project_id", "name", "slug"], [(1, "Linux", "linux")])
        self.db.insert_rows(
            "m_git_network",
            ["network_id", "project_id", "name", "root_commit_hash"],
            [(1, 1, "linux", bytes.fromhex(self.c_init))],
        )
        self.db.insert_rows(
            "m_repository",
            ["repo_id", "network_id", "name", "git_url", "default_branch"],
            [(1, 1, "linux", "", "master")],
        )

        # Ingest release stats + CVE list + resolve prefixes
        rst.ingest(self.db, self.krepo, repo_id=1)
        ci.ingest(self.db, self.crepo)
        ci.resolve_commit_prefixes(self.db, self.krepo, 1)

        # Populate m_commit + m_commit_relation so both parser and webapp see identical commit relations
        _shas = [self.c_init, self.c_intro, self.c_intro2, self.c_fix1, self.c_fix2, self.c_bp2]
        self.db.insert_rows(
            "m_maintainer_person",
            ["person_id", "name", "email"],
            [(1, "Test", "test@kernel.org")],
            on_conflict="ignore",
        )
        self.db.insert_rows(
            "m_commit",
            ["commit_id", "network_id", "commit_hash", "author_id", "author_date", "committer_id", "committer_date", "subject", "message"],
            [
                (
                    i,
                    1,
                    bytes.fromhex(sha),
                    1,
                    1700000000 + i,
                    1,
                    1700000000 + i,
                    _git(self.krepo, "log", "-1", "--format=%s", sha),
                    _git(self.krepo, "log", "-1", "--format=%B", sha),
                )
                for i, sha in enumerate(_shas, start=1)
            ],
        )
        backfills.backfill_commit_relations(self.db, str(self.krepo))

        # Clear webapp caches and patch reader/repo
        self._reset_webapp_caches()
        self.addCleanup(self._reset_webapp_caches)
        for p in (
            patch_reader(self.db),
            patch("webapp.backend.database.helpers.get_active_repo_id", return_value=1),
            patch("webapp.backend.database.helpers.get_active_network_id", return_value=1),
            patch.object(rs, "get_repo_dir", return_value=self.krepo),
            patch.object(crel, "get_repo_dir", return_value=self.krepo),
            patch.object(rs, "_PASS_DISK", _NoDisk()),
            patch.object(cs, "_DISK", _NoDisk()),
        ):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _reset_webapp_caches() -> None:
        cs._SOURCE.update(checked=0.0, row=None)
        cs._MEM.update(built_at=None, head=None, records={})
        cs._CATALOG.update(built_at=None, head=None, rows=[])
        cs._PREFETCH_CACHE.clear()
        rs._TAGS_CACHE.clear()
        rs._DB_COVERAGE.clear()
        crel._DB_READY.clear()

    def test_full_sync_matches_runtime_report_exactly(self) -> None:
        res = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual(res["state"], "updated")
        self.assertEqual((res["incremental"], res["full"]), (0, 4))
        self.assertEqual(res["head"], self.head1)

        # Running sync again with no changes does nothing
        again = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual(again["state"], "current")
        self.assertEqual(again["segments"], 0)

        # Verify parity between db_release_report (cold and warm _MEM) and runtime release_report
        for tag in ("v3.0", "v3.1-rc1", "v3.1", "v3.0.1"):
            runtime = cs.release_report(tag)
            cs._MEM.update(built_at=None, head=None, records={})
            db_cold = cs.db_release_report(tag)
            self.assertIsNotNone(db_cold, tag)
            cs.records()
            db_warm = cs.db_release_report(tag)
            self.assertEqual(db_cold, db_warm, tag)
            self.assertEqual(db_cold["source"], "database")
            self.assertEqual(db_cold["cves"], runtime["cves"], tag)
            self.assertEqual(db_cold["counts"], runtime["counts"], tag)
            self.assertEqual(db_cold["kinds"], runtime["kinds"], tag)
            self.assertEqual(db_cold["head"], runtime["head"], tag)

        # Check specific expected verdicts:
        v30 = {c["id"]: c for c in cs.db_release_report("v3.0")["cves"]}
        self.assertEqual(v30["CVE-2024-0001"]["state"], "affected")
        self.assertEqual(v30["CVE-2024-0003"]["state"], "possibly")
        self.assertNotIn("CVE-2024-0002", v30)  # c_intro2 not in v3.0 -> unaffected

        v31 = {c["id"]: c for c in cs.db_release_report("v3.1")["cves"]}
        self.assertEqual((v31["CVE-2024-0001"]["state"], v31["CVE-2024-0001"]["fixed_here"]), ("fixed", True))
        self.assertEqual((v31["CVE-2024-0002"]["state"], v31["CVE-2024-0002"]["fixed_here"]), ("fixed", True))

        v301 = {c["id"]: c for c in cs.db_release_report("v3.0.1")["cves"]}
        self.assertEqual(v301["CVE-2024-0002"]["state"], "fixed")  # backported in v3.0.1
        self.assertTrue(v301["CVE-2024-0002"]["fixed_here"])

    def test_incremental_cve_update_only_reparses_after_last_hash(self) -> None:
        crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        # Prime cs.source() cache at head1 to verify db_release_report refreshes source when head moves
        self.assertEqual(cs.source(fresh=True)["head"], self.head1)

        # Advance CVE repo: add CVE-2024-0004, reject CVE-2024-0003, update CVE-2024-0001 to unfixed
        (self.cve_dir / "CVE-2024-0004.json").write_text(
            json.dumps(_cna_git_cve("CVE-2024-0004", self.c_intro2, None))
        )
        rejected = _text_cve("CVE-2024-0003", "3.1")
        rejected["cveMetadata"]["state"] = "REJECTED"
        (self.cve_dir / "CVE-2024-0003.json").write_text(json.dumps(rejected))
        (self.cve_dir / "CVE-2024-0001.json").write_text(
            json.dumps(_cna_git_cve("CVE-2024-0001", self.c_intro, None))
        )
        _git(self.crepo, "add", "-A")
        _git(self.crepo, "commit", "-q", "-m", "update cves")
        head2 = _git(self.crepo, "rev-parse", "HEAD")

        ing = ci.ingest(self.db, self.crepo)
        self.assertEqual(ing["state"], "incremental")
        self.assertEqual(ing["prev_head"], self.head1)
        self.assertEqual(sorted(ing["changed_ids"]), ["CVE-2024-0001", "CVE-2024-0003", "CVE-2024-0004"])

        # Before cve_releases.sync runs (once source cache refreshes), db_release_report detects stale head and returns None
        cs._SOURCE["checked"] = 0.0
        self.assertIsNone(cs.db_release_report("v3.1"))

        # Incremental sync updates all 4 segments without any full segment rebuild
        sync_res = crl.sync(
            self.db,
            1,
            kernel_repo=self.krepo,
            cve_repo=self.crepo,
            prev_head=ing["prev_head"],
            changed_ids=ing["changed_ids"],
        )
        self.assertEqual((sync_res["state"], sync_res["incremental"], sync_res["full"]), ("updated", 4, 0))
        self.assertEqual(sync_res["head"], head2)

        # Verify updated DB verdicts match runtime report across every tag
        for tag in ("v3.0", "v3.1-rc1", "v3.1", "v3.0.1"):
            rep = cs.report(tag)
            self.assertEqual(rep["state"], "done")
            self.assertEqual(rep["result"]["source"], "database")
            self.assertEqual(rep["result"]["head"], head2)
            runtime = cs.release_report(tag)
            self.assertEqual(rep["result"]["cves"], runtime["cves"], tag)
            self.assertEqual(rep["result"]["counts"], runtime["counts"], tag)

        v31 = {c["id"]: c for c in cs.db_release_report("v3.1")["cves"]}
        self.assertNotIn("CVE-2024-0003", v31)
        self.assertEqual(v31["CVE-2024-0001"]["state"], "affected")
        self.assertEqual(v31["CVE-2024-0004"]["state"], "affected")

    def test_new_release_tag_and_backfill_and_segment_replace(self) -> None:
        crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)

        # Add a new kernel tag v3.2 containing a new commit, summarize with release_stats
        _git(self.krepo, "checkout", "-q", "master")
        c_new = _commit_file(self.krepo, "c.c", "v3.2\n", "v3.2 commit")
        _git(self.krepo, "tag", "v3.2")
        rs._TAGS_CACHE.clear()
        rs._DB_COVERAGE.clear()
        rst.ingest(self.db, self.krepo, repo_id=1)

        # Before syncing v3.2, v3.2 falls back (returns None from db_release_report), while v3.1 stays cached in DB
        self.assertIsNone(cs.db_release_report("v3.2"))
        self.assertIsNotNone(cs.db_release_report("v3.1"))

        # Sync picks up only v3.2 as a full segment build
        res = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual((res["state"], res["full"]), ("updated", 1))
        v32 = cs.db_release_report("v3.2")
        self.assertIsNotNone(v32)
        self.assertEqual(v32["cves"], cs.release_report("v3.2")["cves"])

        # Add a CVE whose fix prefix was not yet in kernel repo at T1, then add commit + resolve prefix + sync
        (self.cve_dir / "CVE-2024-0005.json").write_text(
            json.dumps(_cna_git_cve("CVE-2024-0005", self.c_init, c_new[:12]))
        )
        _git(self.crepo, "add", "-A")
        _git(self.crepo, "commit", "-q", "-m", "add 0005")
        ci.ingest(self.db, self.crepo)
        before_unres = set(crl.unresolved_prefixes(self.db, 1))
        ci.resolve_commit_prefixes(self.db, self.krepo, 1)
        after_unres = set(crl.unresolved_prefixes(self.db, 1))
        extra = crl.cve_ids_for_prefixes(self.db, before_unres - after_unres)
        self.assertIn("CVE-2024-0005", extra)
        crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo, extra_cve_ids=extra)
        v32_after = {c["id"]: c for c in cs.db_release_report("v3.2")["cves"]}
        self.assertEqual((v32_after["CVE-2024-0005"]["state"], v32_after["CVE-2024-0005"]["fixed_here"]), ("fixed", True))

        # Backfill helper runs cleanly through guarded()
        with patch("parser.cve.cve_ingest.repo_dir", return_value=self.crepo):
            bf = backfills.backfill_cve_releases(self.db, str(self.krepo), 1, force=True)
        self.assertEqual(bf["state"], "updated")
        self.assertEqual(bf["full"], 5)

        # Moving a release tag and re-ingesting cleans up m_cve_release / m_cve_release_state for old_id
        seg_row = self.db.select_one("m_release_segment", ["segment_id"], {"repo_id": 1, "tag": "v3.2"}, as_dict=True)
        old_sid = int(seg_row["segment_id"])
        _git(self.krepo, "checkout", "-q", "master")
        _commit_file(self.krepo, "d.c", "v3.2 moved\n", "v3.2 moved commit")
        _git(self.krepo, "tag", "-f", "v3.2", "HEAD")
        rst.ingest(self.db, self.krepo, repo_id=1)
        self.assertEqual(self.db.count_rows("m_cve_release", {"segment_id": old_sid}), 0)
        self.assertEqual(self.db.count_rows("m_cve_release_state", {"segment_id": old_sid}), 0)

    def _db_reports(self) -> dict[str, Any]:
        self._reset_webapp_caches()
        return {tag: cs.db_release_report(tag)["cves"] for tag in ("v3.0", "v3.1-rc1", "v3.1", "v3.0.1")}

    def _sid(self, tag: str) -> int:
        return int(self.db.select_one("m_release_segment", ["segment_id"], {"repo_id": 1, "tag": tag})[0])

    def test_interrupted_first_build_resumes_without_reevaluating_built_segments(self) -> None:
        crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        expected = self._db_reports()
        # An interrupted first build leaves the last segments (holding the fixing commits) without verdicts
        for tag in ("v3.1-rc1", "v3.1"):
            self.db.delete_rows("m_cve_release", {"segment_id": self._sid(tag)})
            self.db.delete_rows("m_cve_release_state", {"segment_id": self._sid(tag)})
        with patch.object(crl, "_write_segment_incremental", side_effect=AssertionError("built segments re-evaluated")):
            res = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual((res["incremental"], res["full"]), (0, 2))
        self.assertEqual(self._db_reports(), expected)

    def test_segments_added_after_a_verdict_reevaluate_their_cves(self) -> None:
        crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        expected = self._db_reports()
        # Verdicts evaluated before v3.1-rc1 / v3.1 existed must pick up the CVEs whose commits they contain
        older = max(self._sid("v3.0"), self._sid("v3.0.1"))
        newer = [self._sid("v3.1-rc1"), self._sid("v3.1")]
        self.assertGreater(min(newer), older)
        for tag in ("v3.0", "v3.0.1"):
            self.db.update_rows("m_cve_release_state", {"max_segment_id": older}, {"segment_id": self._sid(tag)})
        res = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual((res["incremental"], res["full"]), (2, 0))
        self.assertEqual(self._db_reports(), expected)
        top = max(older, *newer)
        self.assertEqual(
            {int(r[0]) for r in self.db.select_rows("m_cve_release_state", ["max_segment_id"], {"repo_id": 1})}, {top}
        )
        again = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual(again["state"], "current")

    def test_git_commit_messages_large_batch_does_not_deadlock(self) -> None:
        # Request and response both exceed the OS pipe buffer (64 KiB); a write-all-then-read loop hangs here.
        shas = [self.c_fix1, self.c_fix2] * 4000
        msgs = crl._git_commit_messages(self.krepo, shas)
        self.assertEqual(set(msgs), {self.c_fix1, self.c_fix2})
        self.assertIn("Fixes: " + self.c_intro[:12], msgs[self.c_fix1])

    def test_processing_cve_list_pull_and_cli_flags(self) -> None:
        from contextlib import contextmanager
        from pipeline import cli, external_steps as es

        # Create a bare remote for self.crepo and clone it to a directory with the same basename ("cvelistV5")
        base = Path(self.tmp.name)
        upstream = base / "cve_upstream.git"
        work_clone = base / "pull_test" / "cvelistV5"
        work_clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--bare", "-q", str(self.crepo), str(upstream)], check=True, env=ENV)
        subprocess.run(["git", "clone", "-q", str(upstream), str(work_clone)], check=True, env=ENV)

        # Push a new CVE commit to upstream via self.crepo
        (self.cve_dir / "CVE-2024-0006.json").write_text(
            json.dumps(_cna_git_cve("CVE-2024-0006", self.c_init, self.c_fix1[:12]))
        )
        _git(self.crepo, "add", "-A")
        _git(self.crepo, "commit", "-q", "-m", "upstream cve 0006")
        _git(self.crepo, "push", "-q", str(upstream), "HEAD:main")
        head_up = _git(self.crepo, "rev-parse", "HEAD")

        # Point work_clone's origin at upstream and sync initial DB state to head1 first
        crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=work_clone)

        @contextmanager
        def fake_db():
            yield self.db

        with patch("parser.cve.cve_ingest.repo_dir", return_value=work_clone), \
                patch.object(es.G, "DB", fake_db), \
                patch.object(es, "feature_enabled", return_value=True):
            summary = es.processing_cve_list(kernel_repo=str(self.krepo), repo_id=1, pull=True)
            self.assertEqual(summary["state"], "incremental")
            self.assertEqual(summary["head"], head_up)
            self.assertEqual(summary["releases"]["state"], "updated")
            self.assertEqual(summary["releases"]["incremental"], 4)

            # processing_cve_releases runs cleanly and is now current
            rel_sum = es.processing_cve_releases(str(self.krepo), 1)
            self.assertEqual(rel_sum["state"], "current")

        # Verify CLI arguments parse properly without initializing G/DB
        parser = cli.build_arg_parser()
        parsed = parser.parse_args(["--cve", "--cve-pull", "--cve-interval", "60", "--backfill-cve-releases"])
        self.assertTrue(parsed.cve)
        self.assertTrue(parsed.cve_pull)
        self.assertEqual(parsed.cve_interval, 60)
        self.assertTrue(parsed.backfill_cve_releases)

    def test_next_tag_containment_and_backport_tags(self) -> None:
        sha_main = "1" * 40
        sha_prev_month = "2" * 40
        sha_same_day = "3" * 40
        sha_future_next = "4" * 40
        sha_other_stable = "5" * 40
        lookup = {
            "short_to_full": {},
            "sha_tags": {
                sha_main: {"mainline": "v3.1", "stable": None},
                sha_prev_month: {"mainline": None, "stable": "next-20260115"},
                sha_same_day: {"mainline": None, "stable": "next-20260201"},
                sha_future_next: {"mainline": None, "stable": "next-20260205"},
                sha_other_stable: {"mainline": None, "stable": "v3.1.1"},
            },
            "fixes_map": {},
            "backports_map": {},
        }
        ctx = crl.LookupReleaseContext("next-20260201", lookup)
        self.assertTrue(ctx.contains(sha_main))
        self.assertTrue(ctx.contains(sha_prev_month))
        self.assertTrue(ctx.contains(sha_same_day))
        self.assertFalse(ctx.contains(sha_future_next))
        self.assertFalse(ctx.contains(sha_other_stable))
        self.assertTrue(ctx.applies(sha_prev_month))
        self.assertFalse(ctx.applies(sha_other_stable))
        self.assertTrue(ctx.first_here(sha_same_day))
        self.assertFalse(ctx.first_here(sha_prev_month))

        stable_ctx = crl.LookupReleaseContext("v3.1.2", lookup)
        self.assertFalse(stable_ctx.applies(sha_same_day))
        self.assertTrue(stable_ctx.applies(sha_other_stable))

        with patch.object(
            rs,
            "tag_commits",
            return_value={
                "v3.0": "a" * 40,
                "v3.1": "b" * 40,
                "v3.1.1": "c" * 40,
                "next-20260115": "d" * 40,
                "next-20260201": "e" * 40,
                "next-20260205": "f" * 40,
            },
        ):
            web_ctx = cs.Context("next-20260201")
            self.assertEqual(
                sorted(web_ctx._backport_tags()),
                ["next-20260115", "next-20260201", "v3.0", "v3.1"],
            )

    def test_empty_new_segment_advances_max_segment_id(self) -> None:
        crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        _git(self.krepo, "checkout", "-q", "master")
        _commit_file(self.krepo, "plain.c", "plain\n", "unrelated commit")
        _git(self.krepo, "tag", "v3.2")
        rs._TAGS_CACHE.clear()
        rs._DB_COVERAGE.clear()
        rst.ingest(self.db, self.krepo, repo_id=1)

        res = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual((res["state"], res["full"]), ("updated", 1))
        top_sid = self._sid("v3.2")
        watermarks = {int(r[0]) for r in self.db.select_rows("m_cve_release_state", ["max_segment_id"], {"repo_id": 1})}
        self.assertEqual(watermarks, {top_sid})

        with patch.object(crl, "cve_ids_for_segments", side_effect=AssertionError("should not re-scan segments")):
            again = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual(again["state"], "current")

    def test_cve_ids_for_segments_includes_backport_and_fixes_relations(self) -> None:
        bp_cves = crl.cve_ids_for_segments(self.db, 1, [self._sid("v3.0.1")])
        self.assertIn("CVE-2024-0002", bp_cves)
        intro_cves = crl.cve_ids_for_segments(self.db, 1, [self._sid("v3.1-rc1")])
        self.assertIn("CVE-2024-0002", intro_cves)

    def test_worker_eval_and_write_batch_and_legacy_index_migration(self) -> None:
        from db_engine import DeadlockError
        from pipeline.db_setup import drop_outdated_derived_tables

        # 1. Verify legacy cve_release_cve_idx triggers drop_outdated_derived_tables and sync() migration
        self.db.create_index("cve_release_cve_idx", D.m_cve_release, (D.m_cve_release.cve_id,))
        self.assertTrue(self.db.index_exists("cve_release_cve_idx", D.m_cve_release))
        dropped = drop_outdated_derived_tables(self.db)
        self.assertIn("m_cve_release", dropped)
        self.assertIn("m_cve_release_state", dropped)
        self.db.create_table([D.m_cve_release_state, D.m_cve_release])

        self.db.create_index("cve_release_cve_idx", D.m_cve_release, (D.m_cve_release.cve_id,))
        res = crl.sync(self.db, 1, kernel_repo=self.krepo, cve_repo=self.crepo)
        self.assertEqual(res["state"], "updated")
        self.assertFalse(self.db.index_exists("cve_release_cve_idx", D.m_cve_release))

        # 2. Verify _worker_eval_and_write_batch writes verdicts and retries transient DeadlockError
        recs = crl._load_records(self.db)
        lookup = crl.build_lookup_data(self.db, 1, recs, kernel_repo=self.krepo)
        slim = [crl._slim_record(r, lookup) for r in recs]
        tags = {
            r[1]
            for r in self.db.select_rows("m_release_segment", ["segment_id", "tag"], {"repo_id": 1}, as_dict=False)
        }
        ptags = {t: crl.parse_tag(t) for t in tags}
        rkeys = {t: crl.release_key(t) for t in tags}
        src = crl._cve_source(self.db)
        segs = [
            {"segment_id": int(r[0]), "tag": str(r[1]), "tag_hash": bytes(r[2]).hex()}
            for r in self.db.select_rows(
                "m_release_segment", ["segment_id", "tag", "tag_hash"], {"repo_id": 1}, as_dict=False
            )
        ]
        max_sid = max(s["segment_id"] for s in segs)

        self.db.delete_rows("m_cve_release", None, all_rows=True)
        self.db.delete_rows("m_cve_release_state", None, all_rows=True)
        crl._init_worker(slim, lookup, ptags, rkeys, None, 1, src, 1700000100, max_sid)
        crl._POOL_DATA["db"] = self.db

        orig_write = crl._write_segments_full_batch
        attempts = {"n": 0}

        def flaky_write(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise DeadlockError("simulated deadlock")
            return orig_write(*args, **kwargs)

        try:
            with patch.object(crl, "_write_segments_full_batch", side_effect=flaky_write):
                stats = crl._worker_eval_and_write_batch(segs)
            self.assertEqual(attempts["n"], 2)
            self.assertEqual(len(stats), len(segs))
            self.assertEqual(self.db.count_rows("m_cve_release_state"), len(segs))
            self.assertGreater(self.db.count_rows("m_cve_release"), 0)
        finally:
            crl._POOL_DATA.clear()


class _NoDisk:
    def get(self, key):
        return None

    def put(self, key, obj) -> bool:
        return False


if __name__ == "__main__":
    unittest.main()
