"""tests/test_webapp_cve.py - Linux kernel CVE records (H4, webapp/API.md section 4.11)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db_engine import MockDB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import cve_service as cs

INTRO, FIX, STABLE_FIX = "a" * 40, "b" * 40, "c" * 40


def cna_record(cve_id: str) -> dict:
    """The kernel CNA's shape: git ranges in one entry, versions per stable series in two entries."""
    return {
        "cveMetadata": {"cveId": cve_id, "state": "PUBLISHED", "assignerShortName": "Linux", "datePublished": "2024-05-01T00:00:00"},
        "containers": {"cna": {
            "title": "net: fix a leak", "descriptions": [{"lang": "en", "value": "In the Linux kernel, ..."}],
            "metrics": [{"cvssV3_1": {"baseScore": 7.8, "baseSeverity": "HIGH", "vectorString": "CVSS:3.1/AV:L"}}],
            "affected": [
                {"defaultStatus": "unaffected", "programFiles": ["net/core/x.c"], "versions": [
                    {"version": INTRO, "lessThan": FIX, "status": "affected", "versionType": "git"},
                    {"version": INTRO, "lessThan": STABLE_FIX, "status": "affected", "versionType": "git"},
                    {"version": "5.4.262", "lessThan": "5.4.269", "status": "affected", "versionType": "semver"},
                ]},
                {"defaultStatus": "affected", "versions": [
                    {"version": "6.5", "status": "affected"},
                    {"version": "0", "lessThan": "6.5", "status": "unaffected", "versionType": "semver"},
                    {"version": "5.4.269", "lessThanOrEqual": "5.4.*", "status": "unaffected", "versionType": "semver"},
                    {"version": "6.6.17", "lessThanOrEqual": "6.6.*", "status": "unaffected", "versionType": "semver"},
                    {"version": "6.8", "lessThanOrEqual": "*", "status": "unaffected", "versionType": "original_commit_for_fix"},
                    {"version": "4.7-rc1 through 4.13", "status": "affected"},
                ]},
            ],
            "references": [{"url": "https://git.kernel.org/stable/c/" + FIX}, {"url": "javascript:alert(1)"}],
        }},
    }


def other_record(cve_id: str, desc: str, urls: list[str], product: str = "n/a") -> dict:
    return {
        "cveMetadata": {"cveId": cve_id, "state": "PUBLISHED", "assignerShortName": "mitre", "datePublished": "2016-10-10"},
        "containers": {
            "cna": {"descriptions": [{"lang": "en", "value": desc}],
                    "affected": [{"vendor": "n/a", "product": product, "versions": [{"version": "n/a", "status": "affected"}]}],
                    "references": [{"url": u} for u in urls[:1]]},
            "adp": [{"title": "CVE Program Container", "references": [{"url": u} for u in urls[1:]]},
                    {"title": "CISA ADP Vulnrichment", "metrics": [{"other": {"type": "kev", "content": {"dateAdded": "2022-03-03"}}},
                                                                    {"cvssV3_1": {"baseScore": 7.0, "baseSeverity": "HIGH"}}]}],
        },
    }


class FakeContext(cs.Context):
    """Containment from a table: {sha: bool}, with optional backports and Fixes: targets."""

    def __init__(self, release: str, contained: dict, backports: dict | None = None, fixes: dict | None = None, released: set | None = None) -> None:
        # No release index here: every lookup below comes from the tables
        self.release = release
        self.p = cs.rs.parse_tag(release)
        self.version = release[1:]
        self.key = cs.rs.release_key(release)
        self.base_key = cs.rs.release_key(cs.rs.series_name(self.p["series"]))
        self._contained, self._bp, self._fx = contained, backports or {}, fixes or {}
        self._released = released if released is not None else set(contained)
        self._rel = None

    def tags(self, sha):
        return {"mainline": "v1.0", "stable": None} if sha in self._released else None

    def contains(self, sha):
        return bool(self._contained.get(sha))

    def applies(self, fix):
        return fix != STABLE_FIX

    def backports(self, sha):
        return self._bp.get(sha, [])

    def fixes_tags(self, sha):
        return self._fx.get(sha, [])


class TestCveParsing(unittest.TestCase):
    def test_kernel_cna_record(self) -> None:
        r = cs.parse_record(cna_record("CVE-2024-12345"))
        self.assertEqual(r["kind"], "cna")
        self.assertEqual(r["ranges"][0], {"introduced": INTRO, "fixed": FIX, "files": ["net/core/x.c"]})
        self.assertEqual(r["references"], ["https://git.kernel.org/stable/c/" + FIX])
        self.assertEqual(r["cvss"]["score"], 7.8)
        self.assertEqual([e["default"] for e in r["entries"]], ["unaffected", "affected"])
        # "4.7-rc1 through 4.13" is not a version: dropped
        self.assertFalse(any("through" in v["version"] for e in r["entries"] for v in e["versions"]))
        self.assertIsNone(cs.parse_record({"cveMetadata": {"cveId": "nope"}}))
        rejected = cna_record("CVE-2024-12346")
        rejected["cveMetadata"]["state"] = "REJECTED"
        self.assertIsNone(cs.parse_record(rejected))

    def test_commit_text_and_foreign_records(self) -> None:
        sha = "19be0eaffa3ac7d8eb6784ad9bdbc7d67ed8e619"
        r = cs.parse_record(other_record("CVE-2016-5195", "Race condition in mm/gup.c in the Linux kernel 2.x through 4.x before 4.8.3 allows ...",
                                         ["https://example.org/a", f"https://github.com/torvalds/linux/commit/{sha}",
                                          f"http://git.kernel.org/cgit/linux/kernel/git/torvalds/linux.git/commit/?id={sha}"]))
        self.assertEqual(r["kind"], "commit")
        self.assertEqual(r["ranges"], [{"introduced": None, "fixed": sha, "files": []}])
        self.assertEqual(r["kev"], "2022-03-03")
        self.assertEqual(r["entries"][0]["versions"], [{"version": "0", "lessThan": "4.8.3", "status": "affected"}])
        t = cs.parse_record(other_record("CVE-2014-3180", "In kernel/compat.c in the Linux kernel before 3.17, as used in Google Chrome OS ...", []))
        self.assertEqual(t["kind"], "text")
        self.assertEqual(t["entries"][0]["source"], "description")
        # Another git.kernel.org project, no kernel wording: not a kernel record
        self.assertIsNone(cs.parse_record(other_record(
            "CVE-2015-0247", "Heap-based buffer overflow in openfs.c in the libext2fs library in e2fsprogs before 1.42.12 ...",
            ["http://git.kernel.org/cgit/fs/ext2/e2fsprogs.git/commit/?id=" + "d" * 40])))

    def test_kernel_commit_urls(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        urls = [f"https://git.kernel.org/linus/{sha}", f"https://git.kernel.org/stable/c/{sha[:12]}",
                f"https://git.kernel.org/pub/scm/linux/kernel/git/davem/net.git/commit/?h=master&id={sha}",
                f"http://git.kernel.org/?p=linux/kernel/git/torvalds/linux-2.6.git;a=commit;h={sha}",
                f"https://github.com/torvalds/linux/commit/{sha}.patch",
                f"https://git.kernel.org/pub/scm/fs/ext2/e2fsprogs.git/commit/?id={'e' * 40}",
                f"https://github.com/someone/linux/commit/{'f' * 40}", "https://git.kernel.org/linus/abc123"]
        self.assertEqual(cs.kernel_commits(urls), [sha, sha[:12]])

    def test_description_bound(self) -> None:
        self.assertEqual(cs.description_bound("The Linux kernel through 6.0.6 has ..."),
                         {"version": "0", "lessThanOrEqual": "6.0.6", "status": "affected"})
        self.assertEqual(cs.description_bound("Linux kernel before 3.2.3, and 3.3.x before 3.3.1, allows ..."),
                         {"version": "0", "lessThan": "3.3.1", "status": "affected"})
        self.assertIsNone(cs.description_bound("Junos OS before 21.4 ..."))


class TestCveVersions(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = cs.parse_record(cna_record("CVE-2024-00001"))["entries"]

    def test_entries_answer_on_their_own(self) -> None:
        st = lambda v: cs.version_status(self.entries, v)  # noqa: E731
        self.assertEqual(st("3.0"), "unaffected")        # before the bug
        self.assertEqual(st("5.4.100"), "unaffected")    # the stable series before the backported bug
        self.assertEqual(st("5.4.265"), "affected")      # backported bug, not fixed yet
        self.assertEqual(st("5.4.270"), "fixed")         # same series, fixed
        self.assertEqual(st("6.5"), "affected")          # introduced
        self.assertEqual(st("6.6"), "affected")          # default of the second entry
        self.assertEqual(st("6.6.20"), "fixed")
        self.assertEqual(st("6.8"), "fixed")
        self.assertEqual(st("6.2.5"), "unaffected")      # a series that never had it


class TestCveAffects(unittest.TestCase):
    def test_cna_versions_then_git_ranges(self) -> None:
        rec = cs.parse_record(cna_record("CVE-2024-00001"))
        self.assertEqual(cs.affects(rec, "v6.6", FakeContext("v6.6", {})), ("affected", "record"))
        self.assertEqual(cs.affects(rec, "v5.4.270", FakeContext("v5.4.270", {})), ("fixed", "record"))
        # -rc tags: commit containment, the stable branch's fix ignored
        self.assertEqual(cs.affects(rec, "v6.8-rc3", FakeContext("v6.8-rc3", {INTRO: True, FIX: False})), ("affected", "record"))
        self.assertEqual(cs.affects(rec, "v6.8-rc5", FakeContext("v6.8-rc5", {INTRO: True, FIX: True})), ("fixed", "record"))
        self.assertEqual(cs.affects(rec, "v6.4-rc1", FakeContext("v6.4-rc1", {INTRO: False, FIX: False})), ("unaffected", "record"))
        # A fix in no release yet: affected unless a released range says fixed
        self.assertEqual(cs.affects(rec, "v6.8-rc3", FakeContext("v6.8-rc3", {INTRO: True}, released={INTRO})), ("affected", "record"))

    def test_commit_records(self) -> None:
        sha = "1" * 40
        rec = {"kind": "commit", "ranges": [{"introduced": None, "fixed": sha, "files": []}], "entries": []}
        self.assertEqual(cs.affects(rec, "v4.8", FakeContext("v4.8", {sha: False})), ("possibly", "reference"))
        self.assertEqual(cs.affects(rec, "v4.9", FakeContext("v4.9", {sha: True})), ("fixed", "reference"))
        # Its stable backport is in the release
        self.assertEqual(cs.affects(rec, "v4.8.3", FakeContext("v4.8.3", {sha: False, "2" * 40: True}, backports={sha: ["2" * 40]})), ("fixed", "reference"))
        # The Fixes: tag names the commit that introduced the bug
        self.assertEqual(cs.affects(rec, "v4.8", FakeContext("v4.8", {sha: False, "3" * 40: True}, fixes={sha: ["3" * 40]})), ("affected", "fixes-tag"))
        self.assertEqual(cs.affects(rec, "v3.0", FakeContext("v3.0", {sha: False, "3" * 40: False}, fixes={sha: ["3" * 40]})), ("unaffected", "fixes-tag"))

    def test_text_records(self) -> None:
        rec = {"kind": "text", "ranges": [], "entries": [{"default": None, "source": "description",
                                                          "versions": [{"version": "0", "lessThan": "3.17", "status": "affected"}]}]}
        self.assertEqual(cs.affects(rec, "v3.0", FakeContext("v3.0", {})), ("possibly", "description"))
        self.assertEqual(cs.affects(rec, "v4.0", FakeContext("v4.0", {})), ("unknown", ""))

    def test_ids_validated(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            cs.record("../etc")


class TestCveFromDatabase(unittest.TestCase):
    """The webapp reads what the parser ingested (m_cve_source, m_cve, m_cve_commit)."""

    def setUp(self) -> None:
        import zlib
        rec = cs.parse_record(cna_record("CVE-2024-0001"))
        other = cs.parse_record(other_record("CVE-2016-5195", "Race in the Linux kernel before 4.8.3",
                                             ["https://github.com/torvalds/linux/commit/" + FIX[:12]]))
        self.db = MockDB(use_global=False)
        self.db.insert_rows("m_cve_source", ["name", "layout", "head", "format", "records", "built_at"],
                            [("cvelistV5", "cvelist", bytes.fromhex("e" * 40), cs.RECORD_FORMAT, 2, 1700000000)])
        self.db.insert_rows("m_cve", ["cve_id", "kind", "published", "updated", "score", "kev", "record_path", "title", "data"],
                            [(r["id"], 0, 0, 0, 0, 0, "", "", zlib.compress(json.dumps(r).encode())) for r in (rec, other)])
        self.db.insert_rows("m_cve_commit", ["commit_hash", "role", "cve_id"], [
            (FIX, cs.ROLE_FIX, "CVE-2024-0001"), (INTRO, cs.ROLE_INTRODUCED, "CVE-2024-0001"),
            (FIX[:12], cs.ROLE_FIX, "CVE-2016-5195"), (STABLE_FIX, cs.ROLE_FIX, "CVE-2024-0001")])
        self.reads = 0

        def reader():
            self.reads += 1
            return self.db.reader()

        self.patch = patch.object(cs, "get_reader", reader)
        self.patch.start()
        cs._SOURCE.update(checked=0.0, row=None)
        cs._MEM.update(built_at=None, records={})

    def tearDown(self) -> None:
        self.patch.stop()
        cs._SOURCE.update(checked=0.0, row=None)
        cs._MEM.update(built_at=None, records={})

    def test_status_and_records(self) -> None:
        st = cs.status()
        self.assertEqual((st["ingested"], st["layout"], st["records"], st["head"]), (True, "cvelist", 2, "e" * 40))
        recs = cs.records()
        self.assertEqual(sorted(recs), ["CVE-2016-5195", "CVE-2024-0001"])
        self.assertEqual(recs["CVE-2016-5195"]["kind"], "commit")
        self.assertEqual(cs.kinds(recs), {"cna": 1, "commit": 1, "text": 0})
        n = self.reads
        cs.records()
        self.assertEqual(self.reads, n, "loaded once per ingestion")

    def test_for_commit(self) -> None:
        self.assertEqual(cs.for_commit(FIX), {"fixes": ["CVE-2016-5195", "CVE-2024-0001"], "introduced": [], "fixes_upstream": []})
        self.assertEqual(cs.for_commit(INTRO)["introduced"], ["CVE-2024-0001"])
        self.assertEqual(cs.for_commit("d" * 40, upstream=[STABLE_FIX])["fixes_upstream"], ["CVE-2024-0001"])
        self.assertEqual(cs.for_commit("not a sha"), {"fixes": [], "introduced": [], "fixes_upstream": []})

    def test_cves_for_file(self) -> None:
        self.db.insert_rows("m_cve_file", ["cve_id", "file_path"], [("CVE-2024-0001", "fs/ext4/super.c")])
        res = cs.cves_for_file("v6.1.55", "fs/ext4/super.c")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["file_path"], "fs/ext4/super.c")
        self.assertEqual(res["cves"][0]["cve_id"], "CVE-2024-0001")

    def test_affects_evaluates_stable_backport_fix(self) -> None:
        from unittest import mock
        # Range 1 is mainline fix (not in release), Range 2 is stable backport fix (contained in release)
        rec = {
            "kind": "cna",
            "ranges": [
                {"introduced": INTRO, "fixed": FIX},
                {"introduced": INTRO, "fixed": STABLE_FIX},
            ],
            "entries": [],
        }
        ctx = mock.MagicMock()
        ctx.p = {"pre": False, "stable": 55, "series": (6, 1)}
        ctx.version = "v6.1.55"
        ctx.tags.side_effect = lambda f: {"mainline": "v6.5"} if f == FIX else {"stable": "v6.1.50"}
        ctx.applies.return_value = True
        ctx.contains.side_effect = lambda f: f in (INTRO, STABLE_FIX)  # STABLE_FIX is in v6.1.55, but FIX is not
        state, basis = cs.affects(rec, "v6.1.55", ctx)
        self.assertEqual(state, "fixed")

    def test_not_ingested(self) -> None:
        self.db.delete_rows("m_cve_source", None, all_rows=True)
        with patch("parser.cve.cve_ingest.repo_dir", lambda raw=None: None):
            self.assertEqual(cs.status(), {"configured": False, "ingested": False})
        with patch("parser.cve.cve_ingest.repo_dir", lambda raw=None: Path("/srv/cves")):
            self.assertEqual(cs.status(), {"configured": True, "ingested": False})
        self.assertEqual(cs.for_commit(FIX)["fixes"], [])


if __name__ == "__main__":
    unittest.main()


class TestStoredPrefixes(unittest.TestCase):
    def test_no_rev_parse_for_a_stored_prefix(self) -> None:
        from unittest import mock
        full = "ab" * 20
        with mock.patch.object(cs, "stored_prefixes", return_value={full[:12]: full}), \
                mock.patch.object(cs.rs, "release_lookup", return_value={}), \
                mock.patch.object(cs.rs, "resolve_sha") as rev_parse:
            ctx = cs.Context("v3.0")
            ctx.prefetch([full[:12], "cd" * 20])
            self.assertEqual(ctx.full(full[:12]), full)
        rev_parse.assert_not_called()
