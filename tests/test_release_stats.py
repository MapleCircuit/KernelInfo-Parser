"""tests/test_release_stats.py - The parser's release statistics step (parser/git_ast/release_stats.py,
release ranges of parser/git_ast/release_order.py; core/API.md section 10)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser.git_ast import release_stats as rst
from parser.git_ast.git_types import CommitRole
from parser.git_ast.release_order import previous_in, release_ranges

AUTHOR, SOB, REVIEW, REPORT = int(CommitRole.AUTHOR), int(CommitRole.SIGNED_OFF_BY), int(CommitRole.REVIEWED_BY), int(CommitRole.REPORTED_BY)


class TestReleaseRanges(unittest.TestCase):
    TAGS = ["v2.6.38", "v2.6.39-rc1", "v2.6.39", "v2.6.39.1", "v3.0-rc1", "v3.0-rc2", "v3.0", "v3.0.1", "v3.0.2",
            "v3.1-rc1", "v3.1", "v3.1.1", "v3.0-tree", "next-20110101"]

    def test_segments_and_report_ranges(self) -> None:
        got = {r["tag"]: (r["kind"], r["segment_base"], r["range_base"]) for r in release_ranges(self.TAGS)}
        self.assertNotIn("v3.0-tree", got)
        self.assertEqual(got["v2.6.38"], ("mainline", None, None))
        self.assertEqual(got["v3.0-rc1"], ("mainline", "v2.6.39", "v2.6.39"))
        self.assertEqual(got["v3.0-rc2"], ("mainline", "v3.0-rc1", "v3.0-rc1"))
        self.assertEqual(got["v3.0"], ("mainline", "v3.0-rc2", "v2.6.39"))      # a release covers its cycle
        self.assertEqual(got["v3.1-rc1"], ("mainline", "v3.0", "v3.0"))          # stable tags are not on the chain
        self.assertEqual(got["v3.0.1"], ("stable", "v3.0", "v3.0"))
        self.assertEqual(got["v3.0.2"], ("stable", "v3.0.1", "v3.0.1"))
        self.assertEqual(got["v2.6.39.1"], ("stable", "v2.6.39", "v2.6.39"))

    def test_range_base_is_previous_in(self) -> None:
        for r in release_ranges(self.TAGS):
            self.assertEqual(r["range_base"], previous_in(r["tag"], self.TAGS), r["tag"])


class TestManPagesTags(unittest.TestCase):
    """man-pages release tags (`man-pages-X.Y[.Z][-rcN]`) are one prefixed family: numeric order (zero-padded
    minors included), their own chains, kernel tags untouched."""
    TAGS = ["man-pages-1.70", "man-pages-2.00", "man-pages-5.13", "man-pages-5.19-rc1", "man-pages-6.0-rc1",
            "man-pages-6.00", "man-pages-6.04", "man-pages-6.04.01", "man-pages-6.05", "man-pages-6.05-a1",
            "man-pages-6.06", "man-pages-6.7", "man-pages-6.9", "man-pages-6.9.1", "man-pages-6.10"]

    def test_parse(self) -> None:
        from parser.git_ast.release_order import PREFIXED_SERIES, parse_tag
        self.assertEqual(parse_tag("man-pages-6.04")["series"], (PREFIXED_SERIES, "man-pages-", 6, 4))
        self.assertEqual(parse_tag("man-pages-6.04")["series"], parse_tag("man-pages-6.4")["series"])
        self.assertEqual(parse_tag("man-pages-6.9.1")["kind"], "stable")
        self.assertEqual(parse_tag("man-pages-6.0-rc1")["pre"], 1)
        self.assertTrue(parse_tag("man-pages-6.10")["final"])
        self.assertEqual(parse_tag("man-pages-6.10")["family"], "man-pages-")
        for bad in ("man-pages-6.05-a1", "man-pages-6", "man-pages-x.y", "man-pages-6.9.1-rc1"):
            self.assertIsNone(parse_tag(bad), bad)
        # Kernel tags are unaffected
        self.assertEqual(parse_tag("v6.9")["series"], (6, 9, 0))
        self.assertEqual(parse_tag("v6.9")["family"], "")

    def test_order_and_names(self) -> None:
        from parser.git_ast.release_order import final_of, parse_tag, release_key, series_name
        ordered = sorted((t for t in self.TAGS if parse_tag(t)), key=release_key)
        self.assertEqual(ordered, [
            "man-pages-1.70", "man-pages-2.00", "man-pages-5.13", "man-pages-5.19-rc1", "man-pages-6.0-rc1",
            "man-pages-6.00", "man-pages-6.04", "man-pages-6.04.01", "man-pages-6.05", "man-pages-6.06",
            "man-pages-6.7", "man-pages-6.9", "man-pages-6.9.1", "man-pages-6.10",
        ])
        # A series name parses back to its series (minor without zero padding)
        self.assertEqual(series_name(parse_tag("man-pages-6.05")["series"]), "man-pages-6.5")
        self.assertEqual(parse_tag("man-pages-6.5")["series"], parse_tag("man-pages-6.05")["series"])
        self.assertEqual(series_name(parse_tag("man-pages-6.10")["series"]), "man-pages-6.10")
        self.assertEqual(final_of("man-pages-6.0-rc1", set(self.TAGS)), "man-pages-6.00")
        self.assertIsNone(final_of("man-pages-5.19-rc1", set(self.TAGS)))

    def test_ranges(self) -> None:
        got = {r["tag"]: (r["kind"], r["segment_base"], r["range_base"]) for r in release_ranges(self.TAGS)}
        self.assertNotIn("man-pages-6.05-a1", got)
        self.assertEqual(got["man-pages-1.70"], ("mainline", None, None))
        self.assertEqual(got["man-pages-6.7"], ("mainline", "man-pages-6.06", "man-pages-6.06"))
        self.assertEqual(got["man-pages-6.9.1"], ("stable", "man-pages-6.9", "man-pages-6.9"))
        self.assertEqual(got["man-pages-6.10"], ("mainline", "man-pages-6.9", "man-pages-6.9"))
        self.assertEqual(got["man-pages-6.00"], ("mainline", "man-pages-6.0-rc1", "man-pages-5.13"))
        for r in release_ranges(self.TAGS):
            self.assertEqual(r["range_base"], previous_in(r["tag"], self.TAGS), r["tag"])


class TestGenericTagSchemes(unittest.TestCase):
    """Any `<prefix>X.Y[.Z][-rcN]` scheme orders naturally per prefix; each prefix has its own chains."""

    def test_semver_prefix(self) -> None:
        from parser.git_ast.release_order import family_of, final_of, parse_tag, release_key, series_name
        tags = ["foo-1.2", "foo-1.9", "foo-1.10-rc1", "foo-1.10-rc2", "foo-1.10", "foo-1.10.1",
                "foo-2.0-pre1", "foo-2.0", "release_3.1", "gtk2-1.2"]
        self.assertEqual(sorted(tags[:8], key=release_key), [
            "foo-1.2", "foo-1.9", "foo-1.10-rc1", "foo-1.10-rc2", "foo-1.10", "foo-1.10.1",
            "foo-2.0-pre1", "foo-2.0"])
        self.assertEqual((family_of("release_3.1"), family_of("gtk2-1.2"), family_of("foo-1.2")), ("release_", "gtk2-", "foo-"))
        self.assertEqual(parse_tag("foo-1.10.1")["kind"], "stable")
        self.assertEqual(parse_tag("foo-2.0-pre1")["pre_kind"], "pre")
        self.assertEqual(final_of("foo-1.10-rc2", set(tags)), "foo-1.10")
        self.assertEqual(series_name(parse_tag("foo-1.10.1")["series"]), "foo-1.10")
        for bad in ("foo", "foo-1", "foo.1.2", "foo-1.2-beta1", "foo-1.2.3-rc1", "for-linus", "v1.2-beta", "1.2.3.4.5"):
            self.assertIsNone(parse_tag(bad), bad)

    def test_families_have_their_own_chains(self) -> None:
        tags = ["foo-1.0", "foo-1.1-rc1", "foo-1.1", "foo-1.1.1", "bar-0.9", "bar-1.0"]
        got = {r["tag"]: (r["kind"], r["segment_base"], r["range_base"]) for r in release_ranges(tags)}
        self.assertEqual(got["bar-0.9"], ("mainline", None, None))
        self.assertEqual(got["bar-1.0"], ("mainline", "bar-0.9", "bar-0.9"))
        self.assertEqual(got["foo-1.0"], ("mainline", None, None))   # not after bar-1.0
        self.assertEqual(got["foo-1.1-rc1"], ("mainline", "foo-1.0", "foo-1.0"))
        self.assertEqual(got["foo-1.1"], ("mainline", "foo-1.1-rc1", "foo-1.0"))
        self.assertEqual(got["foo-1.1.1"], ("stable", "foo-1.1", "foo-1.1"))
        for r in release_ranges(tags):
            self.assertEqual(r["range_base"], previous_in(r["tag"], tags), r["tag"])

    def test_release_tag_names(self) -> None:
        from parser.git_ast.release_order import release_tag_names
        # A kernel tree: prefixed names (pull-request tags) are not releases
        self.assertEqual(release_tag_names(["v6.0", "for-linus-6.1", "v6.1-rc1", "next-20221010", "kvm-6.1-1"]),
                         ["v6.0", "v6.1-rc1", "next-20221010"])
        # Without kernel-scheme tags every prefixed release counts
        self.assertEqual(release_tag_names(["man-pages-6.9", "nofwd-fix", "man-pages-6.05-a1", "foo-1.0"]),
                         ["man-pages-6.9", "foo-1.0"])
        self.assertEqual(release_tag_names([]), [])

    def test_kernel_semantics_unchanged(self) -> None:
        from parser.git_ast.release_order import parse_tag, release_key, series_name
        self.assertEqual(parse_tag("v2.6.39.4"), {"series": (2, 6, 39), "stable": 4, "pre": 0, "pre_kind": "",
                                                  "kind": "stable", "final": False, "family": ""})
        self.assertEqual(parse_tag("2.4.0")["series"], (2, 4, 0))
        self.assertEqual(parse_tag("next-20260810")["kind"], "next")
        self.assertIsNone(parse_tag("v2.6"))
        self.assertIsNone(parse_tag("v3.0.1.2"))
        self.assertEqual(series_name((3, 1, 0)), "v3.1")
        self.assertEqual(series_name((2, 6, 39)), "v2.6.39")
        # Kernel tags sort before prefixed ones; next tags keep following the last kernel release
        mixed = ["foo-1.0", "v3.1", "next-20110801", "v3.0"]
        self.assertEqual(sorted(mixed, key=release_key), ["v3.0", "v3.1", "next-20110801", "foo-1.0"])
        got = {r["tag"]: r["segment_base"] for r in release_ranges(mixed)}
        self.assertEqual(got, {"v3.0": None, "v3.1": "v3.0", "next-20110801": "v3.1", "foo-1.0": None})


class TestSummarizeLog(unittest.TestCase):
    def test_roles_aliases_and_own_signoff(self) -> None:
        log = (
            "\x1eAnn B\x1fann@x.org\x1fnet: fix\n\nReported-by: syzbot+1@syzkaller.appspotmail.com\n"
            "Reviewed-by: Cy D <cy@y.org>\nReviewed-by: cy d <cy@other.org>\n"   # one identity, two aliases
            "Signed-off-by: Ann B <ann@x.org>\nSigned-off-by: Ann B <ann@work.com>\n"  # the author's own
            "Signed-off-by: Max M <max@m.org>\nSigned-off-by: Max M <max@m.org>\n"
            "\x1eCy D\x1fcy@y.org\x1fmm: tidy\n\nSigned-off-by: Cy D <cy@y.org>\nSigned-off-by: Max M <max@m.org>\n"
        )
        commits, people = rst.summarize_log(log)
        self.assertEqual(commits, 2)
        key = rst.stored_person_key
        self.assertEqual(people[(key("Ann B", "ann@x.org"), AUTHOR)], 1)
        self.assertEqual(people[(key("Cy D", "cy@y.org"), AUTHOR)], 1)
        self.assertEqual(people[(key("Cy D", "cy@y.org"), REVIEW)], 1)
        self.assertNotIn((key("cy d", "cy@other.org"), REVIEW), people)
        self.assertNotIn((key("Ann B", "ann@x.org"), SOB), people)
        self.assertNotIn((key("Ann B", "ann@work.com"), SOB), people)
        self.assertNotIn((key("Cy D", "cy@y.org"), SOB), people)
        self.assertEqual(people[(key("Max M", "max@m.org"), SOB)], 2)  # distinct commits, not trailer lines
        bot = "syzbot+1@syzkaller.appspotmail.com"
        self.assertEqual(people[(key(bot, bot), REPORT)], 1)

    def test_merge_records_are_counted_not_summarized(self) -> None:
        a, b, m = "a" * 40, "b" * 40, "c" * 40
        log = (f"\x1e{m}\x1f{a} {b}\x1fLinus\x1ftorvalds@l.org\x1fMerge branch 'net'\n\nSigned-off-by: Linus <torvalds@l.org>\n"
               f"\x1e{b}\x1f{a}\x1fAnn B\x1fann@x.org\x1fnet: fix\n\nReviewed-by: Cy D <cy@y.org>\n"
               f"\x1e{a}\x1f\x1fAnn B\x1fann@x.org\x1finitial\n")
        out = rst.summarize_records(log)
        self.assertEqual((out["commits"], out["merges"]), (2, 1))
        self.assertEqual(out["shas"], [m, b, a])
        key = rst.stored_person_key
        self.assertEqual(out["people"][(key("Ann B", "ann@x.org"), AUTHOR)], 2)
        self.assertNotIn((key("Linus", "torvalds@l.org"), AUTHOR), out["people"])
        # The same records without merges in the older format give the same figures
        old = "".join(f"\x1e{n}\x1f{e}\x1f{body}" for n, e, body in (("Ann B", "ann@x.org", "net: fix\n\nReviewed-by: Cy D <cy@y.org>\n"),
                                                                   ("Ann B", "ann@x.org", "initial\n")))
        self.assertEqual(rst.summarize_log(old), (out["commits"], out["people"]))

    def test_commit_release_rows_go_with_their_segment_on_rollback(self) -> None:
        from core import ingest_state as I
        from core.DBLayout import TABLES
        marks = {t.table_name: 10 for t, _ in I.marked_tables(TABLES)}
        plan = {(d.table, d.where) for d in I.rollback_plan(TABLES, marks, 10, [])}
        self.assertIn(("m_commit_release", "segment_id > %s"), plan)

    def test_shortstat(self) -> None:
        self.assertEqual(rst.parse_shortstat(" 7818 files changed, 496971 insertions(+), 383598 deletions(-)\n"), (7818, 496971, 383598))
        self.assertEqual(rst.parse_shortstat(" 1 file changed, 2 deletions(-)\n"), (1, 0, 2))
        self.assertEqual(rst.parse_shortstat(""), (0, 0, 0))


class _Cursor:
    """A DB-API cursor over sqlite3 taking the MariaDB `%s` placeholders."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._cur = conn.cursor()

    def execute(self, sql: str, params=()) -> None:
        self._cur.execute(sql.replace("%s", "?").replace("INSERT IGNORE", "INSERT OR IGNORE"), tuple(params))

    def executemany(self, sql: str, rows) -> None:
        self._cur.executemany(sql.replace("%s", "?"), list(rows))

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    @property
    def lastrowid(self) -> int:
        return self._cur.lastrowid

    def close(self) -> None:
        self._cur.close()


class _Connection:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE m_maintainer_person (person_id INTEGER PRIMARY KEY, name TEXT, email TEXT);
            INSERT INTO m_maintainer_person VALUES (0, '', ''), (7, 'Ann B', 'ann@x.org');
            CREATE TABLE m_release_segment (segment_id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id INT, tag TEXT,
                tag_hash BLOB, chain INT, base_tag TEXT, base_hash BLOB, commits INT, merges INT, diff_base_tag TEXT,
                files_changed INT, lines_added INT, lines_removed INT, format INT, UNIQUE (repo_id, tag));
            CREATE TABLE m_release_person (segment_id INT, person_id INT, role_type INT, commits INT,
                PRIMARY KEY (segment_id, person_id, role_type));
            CREATE TABLE m_person_first_release (repo_id INT, person_id INT, segment_id INT, PRIMARY KEY (repo_id, person_id));
            CREATE TABLE m_commit_release (repo_id INT, commit_hash BLOB, chain INT, segment_id INT,
                PRIMARY KEY (repo_id, commit_hash, chain));
        """)

    def query(self, sql: str, params=(), dictionary: bool = True):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql: str, params=(), dictionary: bool = True):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur.fetchone()

    def execute(self, sql: str, params=()) -> int:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur._cur.rowcount

    def executemany(self, sql: str, params_seq) -> int:
        cur = self.cursor()
        cur.executemany(sql, params_seq)
        return cur._cur.rowcount

    def cursor(self) -> _Cursor:
        return _Cursor(self.db)

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()


class TestIngest(unittest.TestCase):
    """End to end on a small repository: v3.0 -> v3.1-rc1 -> v3.1 on master, v3.0.1 on a stable branch."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="relstats-"))
        self.git("init", "-q", "-b", "master")
        self.commit("Ann B", "ann@x.org", "base", "a.c", "1\n")
        self.git("tag", "v3.0")
        self.commit("Ann B", "ann@x.org", "fix\n\nSigned-off-by: Ann B <ann@x.org>\nReviewed-by: Cy D <cy@y.org>", "a.c", "1\n2\n")
        self.commit("Neo N", "neo@n.org", "new\n\nSigned-off-by: Neo N <neo@n.org>\nSigned-off-by: Ann B <ann@x.org>", "b.c", "x\n")
        self.git("tag", "-a", "-m", "Linux 3.1-rc1", "v3.1-rc1")   # annotated: peeled to its commit
        self.git("checkout", "-q", "-b", "side", "v3.1-rc1")
        self.commit("Ann B", "ann@x.org", "side work", "c.c", "y\n")
        self.git("checkout", "-q", "master")
        self.commit("Cy D", "cy@y.org", "late\n\nReported-by: q@q.org", "a.c", "1\n2\n3\n")
        self.git("merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
        self.git("tag", "v3.1")
        self.git("checkout", "-q", "-b", "stable", "v3.0")
        self.commit("Ann B", "ann@x.org", "fix (backport)", "a.c", "1\nB\n")
        self.git("tag", "v3.0.1")
        self.db = _Connection()
        self.cnx = self.db

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
               "GIT_COMMITTER_NAME": "Linus", "GIT_COMMITTER_EMAIL": "torvalds@l.org",
               "GIT_AUTHOR_NAME": "Linus", "GIT_AUTHOR_EMAIL": "torvalds@l.org"}
        return subprocess.run(["git", "-C", str(self.dir), *args], check=True, capture_output=True, text=True, env=env).stdout

    def commit(self, name: str, email: str, message: str, path: str, text: str) -> None:
        (self.dir / path).write_text(text)
        self.git("add", path)
        self.git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "--author", f"{name} <{email}>", "-m", message)

    def rows(self, sql: str, params=()) -> list:
        return self.db.db.execute(sql, params).fetchall()

    def test_ingest_and_incremental(self) -> None:
        summary = rst.ingest(self.db, self.dir, repo_id=1, workers=2)
        self.assertEqual((summary["state"], summary["tags"], summary["segments"]), ("updated", 4, 4))
        seg = {t: (c, m, b, d, f) for t, c, m, b, d, f in self.rows(
            "SELECT tag, commits, merges, base_tag, diff_base_tag, files_changed FROM m_release_segment")}
        self.assertEqual(seg["v3.0"], (1, 0, "", "", 0))
        self.assertEqual(seg["v3.1-rc1"], (2, 0, "v3.0", "v3.0", 2))
        self.assertEqual(seg["v3.1"], (2, 1, "v3.1-rc1", "v3.0", 3))   # segment rc1..v3.1, diff over the cycle
        self.assertEqual(seg["v3.0.1"], (1, 0, "v3.0", "v3.0", 1))
        pid = {name: p for p, name in self.rows("SELECT person_id, name FROM m_maintainer_person")}
        self.assertEqual(pid["Ann B"], 7)  # the existing person row is reused
        people = {(t, n, r): c for t, n, r, c in self.rows(
            "SELECT s.tag, p.name, r.role_type, r.commits FROM m_release_person r JOIN m_release_segment s USING (segment_id) "
            "JOIN m_maintainer_person p USING (person_id)")}
        self.assertEqual(people[("v3.1-rc1", "Ann B", AUTHOR)], 1)
        self.assertEqual(people[("v3.1-rc1", "Ann B", SOB)], 1)          # Neo's commit, not her own
        self.assertNotIn(("v3.1-rc1", "Neo N", SOB), people)
        self.assertEqual(people[("v3.1-rc1", "Cy D", REVIEW)], 1)
        self.assertEqual(people[("v3.1", "q@q.org", REPORT)], 1)
        first = {n: t for n, t in self.rows(
            "SELECT p.name, s.tag FROM m_person_first_release f JOIN m_release_segment s USING (segment_id) "
            "JOIN m_maintainer_person p USING (person_id)")}
        self.assertEqual(first, {"Ann B": "v3.0", "Neo N": "v3.1-rc1", "Cy D": "v3.1"})  # stable chain does not count

        # Every commit of each chain is held by the first segment containing it
        tag_of = {self.git("rev-parse", f"{t}^{{commit}}").strip(): t for t in ("v3.0", "v3.1-rc1", "v3.1", "v3.0.1")}
        held = {(h.hex(), c): t for h, c, t in self.rows(
            "SELECT r.commit_hash, r.chain, s.tag FROM m_commit_release r JOIN m_release_segment s USING (segment_id)")}
        all_mainline = self.git("rev-list", "v3.1").split()
        self.assertEqual({sha for sha, chain in held if chain == rst.CHAIN_MAINLINE}, set(all_mainline))
        for sha, tag in tag_of.items():
            self.assertEqual(held[(sha, rst.CHAIN_STABLE if tag == "v3.0.1" else rst.CHAIN_MAINLINE)], tag)
        merge = self.git("rev-parse", "v3.1").strip()
        self.assertEqual(held[(merge, rst.CHAIN_MAINLINE)], "v3.1")   # merges are members too
        stable_only = [s for s, c in held if c == rst.CHAIN_STABLE]
        self.assertEqual(len(stable_only), 1)                         # v3.0.1's backport

        self.assertEqual(rst.ingest(self.db, self.dir, repo_id=1)["state"], "current")
        self.git("checkout", "-q", "stable")
        self.commit("Neo N", "neo@n.org", "second backport", "a.c", "1\nC\n")
        self.git("tag", "v3.0.2")
        again = rst.ingest(self.db, self.dir, repo_id=1)
        self.assertEqual((again["state"], again["segments"]), ("updated", 1))
        self.assertEqual(self.rows("SELECT base_tag, commits FROM m_release_segment WHERE tag = 'v3.0.2'"), [("v3.0.1", 1)])

    def test_prefixed_tags_and_kernel_tree_filter(self) -> None:
        """A repository tagged `man-pages-X.Y` is summarized like a kernel one; a kernel tree ignores prefixed tags."""
        self.assertEqual(set(rst.release_tags(self.dir)), {"v3.0", "v3.1-rc1", "v3.1", "v3.0.1"})
        self.git("tag", "for-linus-3.1", "v3.1")   # a pull-request tag in a kernel tree is no release
        self.assertNotIn("for-linus-3.1", rst.release_tags(self.dir))
        for old_tag, new_tag in (("v3.0", "man-pages-6.00"), ("v3.1-rc1", "man-pages-6.1-rc1"),
                                 ("v3.1", "man-pages-6.01"), ("v3.0.1", "man-pages-6.00.1")):
            self.git("tag", new_tag, f"{old_tag}^{{commit}}")
        for t in ("v3.0", "v3.1-rc1", "v3.1", "v3.0.1", "for-linus-3.1"):
            self.git("tag", "-d", t)
        summary = rst.ingest(self.db, self.dir, repo_id=2, workers=2)
        self.assertEqual((summary["state"], summary["tags"], summary["segments"]), ("updated", 4, 4))
        seg = {t: (c, b, d) for t, c, b, d in self.rows(
            "SELECT tag, commits, base_tag, diff_base_tag FROM m_release_segment WHERE repo_id = 2")}
        self.assertEqual(seg["man-pages-6.00"], (1, "", ""))
        self.assertEqual(seg["man-pages-6.1-rc1"], (2, "man-pages-6.00", "man-pages-6.00"))
        self.assertEqual(seg["man-pages-6.01"], (2, "man-pages-6.1-rc1", "man-pages-6.00"))
        self.assertEqual(seg["man-pages-6.00.1"], (1, "man-pages-6.00", "man-pages-6.00"))
        first = {n for (n,) in self.rows(
            "SELECT p.name FROM m_person_first_release f JOIN m_maintainer_person p USING (person_id) WHERE f.repo_id = 2")}
        self.assertEqual(first, {"Ann B", "Neo N", "Cy D"})

    def test_moved_mainline_tag_rebuilds_first_releases(self) -> None:
        rst.ingest(self.db, self.dir, repo_id=1)
        self.git("tag", "-f", "v3.0", "v3.1-rc1")  # v3.0 now holds Neo's commit too
        self.assertEqual(rst.ingest(self.db, self.dir, repo_id=1)["state"], "updated")
        first = {n: t for n, t in self.rows(
            "SELECT p.name, s.tag FROM m_person_first_release f JOIN m_release_segment s USING (segment_id) "
            "JOIN m_maintainer_person p USING (person_id)")}
        self.assertEqual(first["Neo N"], "v3.0")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM m_release_segment WHERE tag = 'v3.0'"), [(1,)])
        # The replaced segments rewrote their commits: Neo's commit now belongs to v3.0, none is orphaned
        neo = self.git("log", "--format=%H", "--author=Neo", "-1", "v3.1").strip()
        self.assertEqual(self.rows("SELECT s.tag FROM m_commit_release r JOIN m_release_segment s USING (segment_id) "
                                   "WHERE r.commit_hash = ? AND r.chain = ?", (bytes.fromhex(neo), rst.CHAIN_MAINLINE)), [("v3.0",)])
        held = {h for (h,) in self.rows("SELECT commit_hash FROM m_commit_release WHERE chain = ?", (rst.CHAIN_MAINLINE,))}
        self.assertEqual({bytes.fromhex(x) for x in self.git("rev-list", "v3.1").split()}, held)


if __name__ == "__main__":
    unittest.main()
