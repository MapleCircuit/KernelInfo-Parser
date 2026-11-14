"""tests/test_webapp_man.py - Linux man-pages endpoints (webapp/API.md section 2.20, webapp/API_ENDPOINTS.md
section 4.13) on temporary repositories: a man-pages repository with the old (`man2/`) and the new
(`man/man2/` + `man2 -> man/man2` symlink) layouts, `.so` aliases and dated `man-pages-X.Y` tags, and a kernel
repository with dated `vX.Y` tags. The database is mocked away (no stored facts)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import webapp.backend.config as cfg
from webapp.backend.routers.man import router as man_router
from webapp.backend.security.middleware import RepoContextMiddleware
from webapp.backend.services import man_service as ms
from webapp.backend.services import source_index

READ = """.TH READ 2 2024-05-02 "Linux man-pages 6.10"
.SH NAME
read \\- read from a file descriptor
.SH SYNOPSIS
.nf
.B #include <unistd.h>
.P
.BI "ssize_t read(int " fd ", void " buf [. count "], size_t " count );
.fi
.SH DESCRIPTION
.BR read ()
attempts to read.
.SH SEE ALSO
.BR pread (2)
"""
PREAD = """.TH PREAD 2 2024-05-02 "Linux man-pages 6.10"
.SH NAME
pread, pwrite \\- read from or write to a file descriptor at a given offset
.SH SYNOPSIS
.nf
.BI "ssize_t pread(int " fd ", void " buf ", size_t " count ", off_t " offset );
.BI "ssize_t pwrite(int " fd ", const void " buf ", size_t " count ", off_t " offset );
.fi
.SH DESCRIPTION
Text.
"""
PRINTF = """.TH PRINTF 3 2024-05-02 "Linux man-pages 6.10"
.SH NAME
printf \\- formatted output conversion
.SH SYNOPSIS
.nf
.BI "int printf(const char *restrict " format ", ...);"
.fi
"""
OFF_T = """.TH off_t 2type 2024-05-02 "Linux man-pages 6.10"
.SH NAME
off_t \\- file sizes
"""
NEWCALL = """.TH NEWCALL 2 (date) "Linux man-pages (unreleased)"
.SH NAME
newcall \\- a call added later
"""

MAN_OLD_DATE = "2021-01-01T12:00:00+00:00"
MAN_MID_DATE = "2024-04-01T12:00:00+00:00"
MAN_NEW_DATE = "2024-06-01T12:00:00+00:00"
# man-pages-6.9 is tagged a month after its commit and man-pages-6.10 years later (a retroactive tag): both
# date from their commits
MAN_MID_TAG_DATE = "2024-05-01T12:00:00+00:00"
MAN_NEW_TAG_DATE = "2026-01-01T12:00:00+00:00"


def _git(repo: str, *args: str, date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True, env=env).stdout


def _write(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _commit_all(repo: str, msg: str, date: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, date=date)


class ManFixture(unittest.TestCase):
    tmp: str
    man: str
    linux: str
    repos: list[dict]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="ki_man_")
        cls.man = os.path.join(cls.tmp, "man-pages")
        cls.linux = os.path.join(cls.tmp, "linux")
        os.makedirs(cls.man)
        os.makedirs(cls.linux)
        m = cls.man
        _git(m, "init", "-q", "-b", "master")
        # Old layout: pages at the root, aliases relative to the root
        _write(m, "man2/read.2", READ)
        _write(m, "man2/pread.2", PREAD)
        _write(m, "man2/pwrite.2", ".so man2/pread.2\n")
        _write(m, "man2/loop1.2", ".so man2/loop2.2\n")
        _write(m, "man2/loop2.2", ".so man2/loop1.2\n")
        _write(m, "man3/printf.3", PRINTF)
        _write(m, "README", "not a page\n")
        _commit_all(m, "5.10", MAN_OLD_DATE)
        _git(m, "tag", "man-pages-5.10", date=MAN_OLD_DATE)  # lightweight: the commit's date
        _write(m, "man3/printf.3", PRINTF + ".SH DESCRIPTION\nMore.\n")
        _commit_all(m, "6.9", MAN_MID_DATE)
        _git(m, "tag", "-a", "-m", "6.9", "man-pages-6.9", date=MAN_MID_TAG_DATE)
        # New layout: pages under man/, root manS symlinks, `.so` targets still relative to the man root
        os.makedirs(os.path.join(m, "man"))
        shutil.move(os.path.join(m, "man2"), os.path.join(m, "man", "man2"))
        shutil.move(os.path.join(m, "man3"), os.path.join(m, "man", "man3"))
        os.symlink("man/man2", os.path.join(m, "man2"))
        os.symlink("man/man3", os.path.join(m, "man3"))
        _write(m, "man/man2type/off_t.2type", OFF_T)
        _write(m, "man/man2/newcall.2", NEWCALL)
        _commit_all(m, "6.10", MAN_NEW_DATE)
        _git(m, "tag", "-a", "-m", "6.10", "man-pages-6.10", date=MAN_NEW_TAG_DATE)

        k = cls.linux
        _git(k, "init", "-q", "-b", "master")
        for tag, date in (("v5.10", "2020-12-13T12:00:00+00:00"), ("v6.8", "2024-03-10T12:00:00+00:00"),
                          ("v6.9", "2024-05-12T12:00:00+00:00"), ("v6.10-rc1", "2024-05-26T12:00:00+00:00"),
                          ("v6.10", "2024-07-14T12:00:00+00:00")):
            _write(k, "Makefile", f"# {tag}\n")
            _commit_all(k, tag, date)
            # v6.9 is tagged after man-pages-6.10's commit: the kernel release still dates from its commit
            tag_date = "2024-06-15T12:00:00+00:00" if tag == "v6.9" else date
            _git(k, "tag", "-a", "-m", tag, tag, date=tag_date)

        cls.repos = [
            {"name": "linux", "path": cls.linux, "project": "linux"},
            {"name": "man-pages", "path": cls.man, "project": "man-pages"},
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        repos = self.repos

        def get_repository(name=None):
            if not name:
                return repos[0] if repos else None
            return next((r for r in repos if r["name"] == name), None)

        self.db_rows: dict[str, dict] = {}
        patches = [
            patch.object(cfg, "get_repository", get_repository),
            patch.object(cfg, "list_repositories", lambda: list(repos)),
            patch.object(ms, "list_repositories", lambda: list(repos)),
            patch.object(source_index, "db_facts", lambda shas, *a, **k: {s: self.db_rows[s] for s in shas if s in self.db_rows}),
            patch.object(source_index, "db_has", lambda shas, *a, **k: {s for s in shas if s in self.db_rows}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for cache in (ms._TAG_DATES, ms._PAGE_MAPS, ms._INDEXES, ms._DIFFS, ms._FACTS, ms._DOCS, ms._RESOLVED):
            dict.clear(cache)

    def active(self, name: str):
        token = cfg.set_active_repository(name)
        self.assertIsNotNone(token)
        self.addCleanup(cfg.reset_active_repository, token)


class TestManIndex(ManFixture):
    def test_new_layout_lists_each_page_once(self) -> None:
        self.active("man-pages")
        idx = ms.index("man-pages-6.10")
        self.assertEqual(idx["source"], "git")
        paths = [p["path"] for p in idx["pages"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(all(p.startswith("man/") for p in paths), paths)
        self.assertEqual([p["section"] for p in idx["pages"]], ["2"] * 6 + ["2type", "3"])
        by = {p["name"]: p for p in idx["pages"]}
        self.assertEqual(by["read"]["summary"], "read from a file descriptor")
        self.assertIsNone(by["read"]["so"])
        self.assertEqual(by["pwrite"]["so"], "man/man2/pread.2")
        self.assertEqual(by["pwrite"]["summary"], by["pread"]["summary"])  # an alias shows its target's summary
        self.assertIn(by["loop1"]["so"], ("man/man2/loop1.2", "man/man2/loop2.2"))  # a loop stops

    def test_old_layout_and_db_source(self) -> None:
        self.active("man-pages")
        idx = ms.index("man-pages-5.10")
        self.assertEqual({p["path"] for p in idx["pages"]},
                         {"man2/read.2", "man2/pread.2", "man2/pwrite.2", "man2/loop1.2", "man2/loop2.2", "man3/printf.3"})
        self.assertEqual(next(p for p in idx["pages"] if p["name"] == "pwrite")["so"], "man2/pread.2")
        # Every page blob stored by the parser: the index comes from the database
        tree = ms.git_tree_resolver.list_tree("man-pages-6.9")
        self.db_rows = {b: {"n": ["x"], "s": "stored summary", "so": ""} for p, b in tree.items() if ms.page_of_path(p)}
        idx = ms.index("man-pages-6.9")
        self.assertEqual(idx["source"], "db")
        self.assertEqual({p["summary"] for p in idx["pages"]}, {"stored summary"})

    def test_from_another_project_and_errors(self) -> None:
        self.active("linux")  # the configured man-pages repository answers
        self.assertEqual(ms.index("man-pages-6.10")["repo"], "man-pages")
        with self.assertRaises(HTTPException) as cm:
            ms.index("--output=x")
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            ms.index("man-pages-9.99")
        self.assertEqual(cm.exception.status_code, 404)


class TestManPage(ManFixture):
    def test_by_name_path_and_alias(self) -> None:
        self.active("man-pages")
        pg = ms.page("man-pages-6.10", name="read")
        self.assertEqual((pg["name"], pg["section"], pg["path"], pg["resolved_path"]), ("read", "2", "man/man2/read.2", "man/man2/read.2"))
        self.assertEqual(pg["doc"]["th"]["title"], "READ")
        self.assertEqual(pg["aliases"], ["read"])
        self.assertEqual(pg["linux"]["syscall"], "read")
        self.assertIn("read", pg["linux"]["symbols"])
        self.assertIn("size_t", pg["linux"]["symbols"])
        # An old-layout path at a new-layout tag (the root man2 is a symlink)
        self.assertEqual(ms.page("man-pages-6.10", path="man2/read.2")["path"], "man/man2/read.2")
        alias = ms.page("man-pages-6.10", name="pwrite", section="2")
        self.assertEqual((alias["path"], alias["resolved_path"]), ("man/man2/pwrite.2", "man/man2/pread.2"))
        self.assertEqual(alias["aliases"], ["pread", "pwrite"])
        self.assertEqual(alias["linux"]["syscall"], "pwrite")
        old = ms.page("man-pages-5.10", name="pwrite")
        self.assertEqual(old["resolved_path"], "man2/pread.2")
        loop = ms.page("man-pages-6.10", name="loop1")  # terminates
        self.assertIn(loop["resolved_path"], ("man/man2/loop1.2", "man/man2/loop2.2"))

    def test_unreleased_header(self) -> None:
        self.active("man-pages")
        pg = ms.page("man-pages-6.10", name="newcall")
        self.assertEqual((pg["doc"]["th"]["date"], pg["doc"]["th"]["source"]), ("2024-06-01", "Linux man-pages 6.10"))
        # The cached parse keeps the placeholders; a branch shows them as they are
        self.assertEqual(ms.page("master", name="newcall")["doc"]["th"]["date"], "(date)")
        self.assertEqual(ms.page("man-pages-6.10", name="read")["doc"]["th"]["date"], "2024-05-02")

    def test_sections(self) -> None:
        self.active("man-pages")
        self.assertEqual(ms.page("man-pages-6.10", name="off_t", section="2")["section"], "2type")  # sibling section
        self.assertEqual(ms.page("man-pages-6.10", name="off_t")["path"], "man/man2type/off_t.2type")
        pg = ms.page("man-pages-6.10", name="printf", section="3")
        self.assertIsNone(pg["linux"]["syscall"])
        self.assertEqual(pg["linux"]["symbols"], ["printf"])
        self.assertEqual(ms.page("man-pages-6.10", name="READ")["name"], "read")  # case-insensitive fallback

    def test_linux_version(self) -> None:
        self.active("man-pages")
        # man-pages-6.10 (2024-06-01): v6.10-rc1 is a release candidate, v6.10 is later
        self.assertEqual(ms.page("man-pages-6.10", name="read")["linux"], {
            "repo": "linux", "version": "v6.9", "syscall": "read", "symbols": ["read", "ssize_t", "size_t"]})
        self.assertEqual(ms.page("man-pages-6.9", name="read")["linux"]["version"], "v6.8")
        # Older than every kernel release: the oldest one
        self.assertEqual(ms.page("man-pages-5.10", name="read")["linux"]["version"], "v5.10")
        self.assertEqual(ms.page("man-pages-6.10", name="read", kernel="v6.8", kernel_repo="linux")["linux"]["version"], "v6.8")
        self.assertEqual(ms.page("man-pages-6.10", name="read", kernel="v9.9")["linux"]["version"], "v6.9")
        self.assertEqual(ms.page("man-pages-6.10", name="read", kernel="-o")["linux"]["version"], "v6.9")

    def test_errors(self) -> None:
        self.active("man-pages")
        for kwargs, status in ((dict(name="nope"), 404), (dict(name="read", section="5"), 404),
                               (dict(path="man/man2/nope.2"), 404), (dict(path="../etc/passwd"), 400),
                               (dict(path="README"), 400), (dict(name="a b"), 400), (dict(name="read", section="x"), 400),
                               (dict(), 400)):
            with self.assertRaises(HTTPException, msg=kwargs) as cm:
                ms.page("man-pages-6.10", **kwargs)
            self.assertEqual(cm.exception.status_code, status, kwargs)
        with self.assertRaises(HTTPException) as cm:
            ms.page("man-pages-5.10", name="newcall")  # added later
        self.assertEqual(cm.exception.status_code, 404)


class TestManResolveAndTags(ManFixture):
    def test_tags_newest_first(self) -> None:
        self.active("linux")
        tags = ms.tags()["tags"]
        self.assertEqual([t["name"] for t in tags], ["man-pages-6.10", "man-pages-6.9", "man-pages-5.10"])
        self.assertGreater(tags[0]["date"], tags[1]["date"])

    def test_dates_are_commit_dates(self) -> None:
        """A tag dates from the committer date of its commit, not from when it was tagged (annotated or not)."""
        from datetime import datetime

        def stamp(iso: str) -> int:
            return int(datetime.fromisoformat(iso).timestamp())

        self.active("man-pages")
        dates = {t["name"]: t["date"] for t in ms.tags()["tags"]}
        self.assertEqual(dates, {"man-pages-6.10": stamp(MAN_NEW_DATE), "man-pages-6.9": stamp(MAN_MID_DATE),
                                 "man-pages-5.10": stamp(MAN_OLD_DATE)})
        self.assertEqual(ms.page("man-pages-6.9", name="read")["date"], stamp(MAN_MID_DATE))
        self.active("linux")
        self.assertEqual(ms.tag_dates()["v6.9"][0], stamp("2024-05-12T12:00:00+00:00"))
        res = ms.resolve("read", "2", "v6.9")
        self.assertEqual((res["kernel_date"], res["tag_date"]), (stamp("2024-05-12T12:00:00+00:00"), stamp(MAN_MID_DATE)))

    def test_tag_by_kernel_date(self) -> None:
        self.active("linux")
        res = ms.resolve("read", "2", "v6.10")
        self.assertTrue(res["available"])
        self.assertEqual((res["repo"], res["version"], res["path"], res["name"], res["section"]),
                         ("man-pages", "man-pages-6.10", "man/man2/read.2", "read", "2"))
        self.assertLessEqual(res["tag_date"], res["kernel_date"])
        self.assertEqual(ms.resolve("read", "2", "v6.9")["version"], "man-pages-6.9")
        self.assertEqual(ms.resolve("read", "2", "v5.10")["version"], "man-pages-5.10")  # before every tag: the oldest
        self.assertEqual(ms.resolve("read", "", "v6.10-rc1")["version"], "man-pages-6.9")
        # Missing at the chosen tag: newer tags are tried after older ones
        self.assertEqual(ms.resolve("newcall", "2", "v5.10")["version"], "man-pages-6.10")
        self.assertEqual(ms.resolve("off_t", "2", "v6.10")["section"], "2type")
        self.assertEqual(ms.resolve("read")["version"], "man-pages-6.10")  # default: the newest kernel release

    def test_unavailable(self) -> None:
        self.active("linux")
        res = ms.resolve("nope", "2", "v6.10")
        self.assertEqual(res, {"available": False, "url": "https://man7.org/linux/man-pages/man2/nope.2.html",
                               "name": "nope", "section": "2", "kernel": "v6.10", "kernel_date": res["kernel_date"]})
        self.assertFalse(ms.resolve("read", "2", "v1.0")["available"])  # unknown kernel revision
        with self.assertRaises(HTTPException):
            ms.resolve("read", "2", "--all")
        with self.assertRaises(HTTPException):
            ms.resolve("a/b", "2", "v6.10")
        with patch.object(ms, "list_repositories", lambda: [self.repos[0]]):
            res = ms.resolve("read", "2", "v6.10")
            self.assertEqual(res["available"], False)
            self.assertEqual(res["url"], "https://man7.org/linux/man-pages/man2/read.2.html")


class TestManRouter(ManFixture):
    def setUp(self) -> None:
        super().setUp()
        app = FastAPI()
        app.include_router(man_router)
        app.add_middleware(RepoContextMiddleware)
        self.client = TestClient(app)

    def test_endpoints_and_repo_selection(self) -> None:
        c = self.client
        r = c.get("/api/man/man-pages-6.10/index", params={"repo": "man-pages"}, headers={"X-KI-Repo": "man-pages"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["repo"], "man-pages")
        self.assertEqual(r.json()["version"], "man-pages-6.10")
        r = c.get("/api/man/man-pages-6.10/page", params={"name": "read", "repo": "man-pages"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["linux"]["version"], "v6.9")
        self.assertEqual(c.get("/api/man/man-pages-6.10/page", params={"name": "nope"}).status_code, 404)
        self.assertEqual(c.get("/api/man/man-pages-6.10/page", params={"path": "../../x"}).status_code, 400)
        self.assertEqual([t["name"] for t in c.get("/api/man/tags", headers={"X-KI-Repo": "man-pages"}).json()["tags"]][0],
                         "man-pages-6.10")
        # resolve runs on the kernel repository (the default one here, or named by the header)
        r = c.get("/api/man/resolve", params={"name": "read", "section": "2", "kernel": "v6.9"}, headers={"X-KI-Repo": "linux"})
        self.assertEqual(r.json()["version"], "man-pages-6.9")
        self.assertEqual(c.get("/api/man/resolve", params={"name": "read", "kernel": "-x"}).status_code, 400)
        r = c.get("/api/man/diff", params={"a": "man-pages-6.9", "b": "man-pages-6.10", "repo": "man-pages"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["summary"], {"added": 2, "modified": 0, "deleted": 0, "unchanged": 6, "total_a": 6, "total_b": 8})
        self.assertEqual(c.get("/api/man/diff", params={"a": "--x", "b": "man-pages-6.10"}).status_code, 400)
        self.assertEqual(c.get("/api/man/diff", params={"a": "man-pages-9.99", "b": "man-pages-6.10"}).status_code, 404)


class TestManDiff(ManFixture):
    def test_layout_migration_and_added_modified_deleted(self) -> None:
        self.active("man-pages")
        # 5.10 -> 6.9: only printf(3) was modified
        d56 = ms.diff("man-pages-5.10", "man-pages-6.9")
        self.assertEqual(d56["source"], "git")
        self.assertEqual(d56["summary"], {"added": 0, "modified": 1, "deleted": 0, "unchanged": 5, "total_a": 6, "total_b": 6})
        self.assertEqual([(e["name"], e["section"], e["status"]) for e in d56["entries"]], [("printf", "3", "modified")])

        # 6.9 -> 6.10: man2/ moved to man/man2/ without spurious delete/add; only off_t(2type) and newcall(2) are added
        d610 = ms.diff("man-pages-6.9", "man-pages-6.10")
        self.assertEqual(d610["summary"], {"added": 2, "modified": 0, "deleted": 0, "unchanged": 6, "total_a": 6, "total_b": 8})
        self.assertEqual([(e["name"], e["section"], e["status"]) for e in d610["entries"]],
                         [("newcall", "2", "added"), ("off_t", "2type", "added")])

        # Reverse: 6.10 -> 5.10 reports deleted + modified
        drev = ms.diff("man-pages-6.10", "man-pages-5.10")
        self.assertEqual(drev["summary"], {"added": 0, "modified": 1, "deleted": 2, "unchanged": 5, "total_a": 8, "total_b": 6})
        self.assertEqual([(e["name"], e["section"], e["status"]) for e in drev["entries"]],
                         [("newcall", "2", "deleted"), ("off_t", "2type", "deleted"), ("printf", "3", "modified")])

        # Same revision: empty entries, all unchanged
        same = ms.diff("man-pages-6.10", "man-pages-6.10")
        self.assertEqual(same["entries"], [])
        self.assertEqual(same["summary"]["unchanged"], 8)

    def test_modified_semantic_deltas_and_db_source(self) -> None:
        self.active("linux")  # works from another active repository via manpages_repository()
        # Verify modified semantic deltas (summary, fn, aliases, alias target) against a branch commit
        _git(self.man, "checkout", "-q", "-b", "test-delta", "man-pages-6.10")
        try:
            _write(self.man, "man/man2/read.2", READ.replace(
                "read \\- read from a file descriptor",
                "read, read2 \\- read bytes from an fd",
            ).replace(
                ".BI \"ssize_t read(int \" fd \", void \" buf [. count \"], size_t \" count );",
                ".BI \"ssize_t read(int \" fd \", void \" buf [. count \"], size_t \" count );\n.BI \"ssize_t read2(int \" fd );",
            ))
            _write(self.man, "man/man2/pwrite.2", PREAD)
            _commit_all(self.man, "delta", "2024-07-01T12:00:00+00:00")

            d = ms.diff("man-pages-6.10", "test-delta")
            self.assertEqual(d["repo"], "man-pages")
            self.assertEqual(d["summary"], {"added": 0, "modified": 2, "deleted": 0, "unchanged": 6, "total_a": 8, "total_b": 8})
            by = {e["name"]: e for e in d["entries"]}
            self.assertTrue(by["read"]["summary_changed"])
            self.assertEqual(by["read"]["fn_added"], ["read2"])
            self.assertEqual(by["read"]["aliases_added"], ["read2"])
            self.assertTrue(by["pwrite"]["so_changed"])
            self.assertEqual(by["pwrite"]["so_a"], "man/man2/pread.2")
            self.assertIsNone(by["pwrite"]["so_b"])

            # Stored family-6 DB rows for changed blobs mark source="db"
            ms._DIFFS.clear()
            with ms.using_repo("man-pages"):
                tree_a = ms.git_tree_resolver.list_tree("man-pages-6.10")
                tree_b = ms.git_tree_resolver.list_tree("test-delta")
            self.db_rows = {
                b: {"n": ["x"], "s": "stored", "so": ""}
                for t in (tree_a, tree_b)
                for p, b in t.items()
                if ms.page_of_path(p)
            }
            d_db = ms.diff("man-pages-6.10", "test-delta")
            self.assertEqual(d_db["source"], "db")
        finally:
            _git(self.man, "checkout", "-q", "master")
            _git(self.man, "branch", "-D", "test-delta")


class TestVersionsOrder(unittest.TestCase):
    def test_release_tag_key(self) -> None:
        from webapp.backend.routers.versions import release_tag_key
        names = ["man-pages-6.06", "man-pages-6.0-rc1", "man-pages-6.00", "man-pages-6.05-a1", "man-pages-6.05",
                 "man-pages-6.7", "man-pages-6.04.01", "man-pages-6.04", "man-pages-6.10", "man-pages-6.9"]
        self.assertEqual(sorted(names, key=release_tag_key), [
            "man-pages-6.0-rc1", "man-pages-6.00", "man-pages-6.04", "man-pages-6.04.01", "man-pages-6.05-a1",
            "man-pages-6.05", "man-pages-6.06", "man-pages-6.7", "man-pages-6.9", "man-pages-6.10"])

    def test_projects_catalog_fields(self) -> None:
        from webapp.backend.routers import versions
        repos = [{"name": "linux", "path": "/nonexistent/linux", "project": "linux"},
                 {"name": "man-pages", "path": "/nonexistent/mp"}]
        with patch.object(versions, "list_repositories", lambda: repos), \
                patch.object(versions, "get_reader", side_effect=RuntimeError("no db")), \
                patch.object(versions, "default_branch_of", lambda r: "master"):
            cat = {p["repo"]: p for p in versions.projects_catalog()}
        self.assertEqual(cat["man-pages"]["project"], "man-pages")
        self.assertEqual(cat["linux"]["project"], "linux")
        self.assertEqual(cat["man-pages"]["tag_views"][0]["regex_rules"], [r"^(man-pages-\d+)(\..*)$"])
        self.assertFalse(cat["man-pages"]["available"])


if __name__ == "__main__":
    unittest.main()
