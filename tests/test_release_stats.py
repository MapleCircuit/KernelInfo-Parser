"""tests/test_release_stats.py - The parser's release statistics step (parser/git_ast/release_stats.py,
release ranges of parser/git_ast/release_order.py; core/API.md section 10)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import DBLayout as D
from db_engine import SQLiteDB
from db_engine.spec import Join
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
        plan = {(d.table, d.condition) for d in I.rollback_plan(TABLES, marks, 10, [])}
        self.assertIn(("m_commit_release", "segment_id >"), plan)

    def test_shortstat(self) -> None:
        self.assertEqual(rst.parse_shortstat(" 7818 files changed, 496971 insertions(+), 383598 deletions(-)\n"), (7818, 496971, 383598))
        self.assertEqual(rst.parse_shortstat(" 1 file changed, 2 deletions(-)\n"), (1, 0, 2))
        self.assertEqual(rst.parse_shortstat(""), (0, 0, 0))


def _release_db() -> SQLiteDB:
    """In-memory SQLite database holding the tables of the release statistics, with a known person (7)."""
    db = SQLiteDB(":memory:", foreign_keys=False)
    db.create_table([D.m_maintainer_person, D.m_release_segment, D.m_release_person, D.m_person_first_release,
                     D.m_commit_release, D.m_release_pull, D.m_release_section_health])
    db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], [(7, "Ann B", "ann@x.org")], on_conflict="ignore")
    return db


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
        self.db = _release_db()

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

    def rows(self, source, columns, where=None, joins=()) -> list:
        return [tuple(r) for r in self.db.select_rows(source, columns, where, joins=joins)]

    def tag_join(self, table: str) -> tuple[Join, ...]:
        """Join of a per-segment table to its segment row (alias s)."""
        return (Join(("m_release_segment", "s"), {"s.segment_id": f"{table}.segment_id"}),)

    def test_process_pool_matches_the_serial_path(self) -> None:
        todo = rst.plan(rst.release_tags(self.dir), {})
        serial = [rst.compute(self.dir, r) for r in todo]
        pooled = list(rst.compute_all(self.dir, todo, workers=2, processes=2))
        threaded = list(rst.compute_all(self.dir, todo, workers=2, processes=0))
        self.assertEqual(pooled, serial)
        self.assertEqual(threaded, serial)

    def test_distributed_person_ids_come_from_leased_blocks(self) -> None:
        class FakeDB:
            def __init__(self) -> None:
                self.blocks: list[int] = []
                self.rows: list[tuple] = []

            def select_rows(self, *a, **k):
                return [(5, "Old", "old@x.org")]

            def reserve_id_block(self, table, size, commit_id=0):
                self.blocks.append(size)
                start = 1000 + 256 * len(self.blocks)
                return start, start + size - 1

            def insert_rows(self, table, cols, rows, **k):
                self.rows.extend(rows)

        db = FakeDB()
        persons = rst._Persons(db, distributed=True)
        ids = persons.resolve(db, [("A", "a@x"), ("B", "b@x")])
        self.assertEqual(sorted(v for k, v in ids.items() if k in {("A", "a@x"), ("B", "b@x")}), [1256, 1257])
        self.assertEqual((db.blocks, persons.added), ([256], 2))
        alone = FakeDB()
        local = rst._Persons(alone)
        self.assertEqual(local.resolve(alone, [("A", "a@x")])[("A", "a@x")], 6)
        self.assertEqual(alone.blocks, [])

    def test_ingest_and_incremental(self) -> None:
        summary = rst.ingest(self.db, self.dir, repo_id=1, workers=2)
        self.assertEqual((summary["state"], summary["tags"], summary["segments"]), ("updated", 4, 4))
        seg = {t: (c, m, b, d, f) for t, c, m, b, d, f in self.rows(
            "m_release_segment", ["tag", "commits", "merges", "base_tag", "diff_base_tag", "files_changed"])}
        self.assertEqual(seg["v3.0"], (1, 0, "", "", 0))
        self.assertEqual(seg["v3.1-rc1"], (2, 0, "v3.0", "v3.0", 2))
        self.assertEqual(seg["v3.1"], (2, 1, "v3.1-rc1", "v3.0", 3))   # segment rc1..v3.1, diff over the cycle
        self.assertEqual(seg["v3.0.1"], (1, 0, "v3.0", "v3.0", 1))
        pid = {name: p for p, name in self.rows("m_maintainer_person", ["person_id", "name"])}
        self.assertEqual(pid["Ann B"], 7)  # the existing person row is reused
        people = {(t, n, r): c for t, n, r, c in self.rows(
            ("m_release_person", "r"), ["s.tag", "p.name", "r.role_type", "r.commits"],
            joins=[*self.tag_join("r"), Join(("m_maintainer_person", "p"), {"p.person_id": "r.person_id"})])}
        self.assertEqual(people[("v3.1-rc1", "Ann B", AUTHOR)], 1)
        self.assertEqual(people[("v3.1-rc1", "Ann B", SOB)], 1)          # Neo's commit, not her own
        self.assertNotIn(("v3.1-rc1", "Neo N", SOB), people)
        self.assertEqual(people[("v3.1-rc1", "Cy D", REVIEW)], 1)
        self.assertEqual(people[("v3.1", "q@q.org", REPORT)], 1)
        first = {n: t for n, t in self.rows(
            ("m_person_first_release", "f"), ["p.name", "s.tag"],
            joins=[*self.tag_join("f"), Join(("m_maintainer_person", "p"), {"p.person_id": "f.person_id"})])}
        self.assertEqual(first, {"Ann B": "v3.0", "Neo N": "v3.1-rc1", "Cy D": "v3.1"})  # stable chain does not count

        # Every commit of each chain is held by the first segment containing it
        tag_of = {self.git("rev-parse", f"{t}^{{commit}}").strip(): t for t in ("v3.0", "v3.1-rc1", "v3.1", "v3.0.1")}
        held = {(h.hex(), c): t for h, c, t in self.rows(
            ("m_commit_release", "r"), ["r.commit_hash", "r.chain", "s.tag"], joins=self.tag_join("r"))}
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
        self.assertEqual(self.rows("m_release_segment", ["base_tag", "commits"], {"tag": "v3.0.2"}), [("v3.0.1", 1)])

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
            "m_release_segment", ["tag", "commits", "base_tag", "diff_base_tag"], {"repo_id": 2})}
        self.assertEqual(seg["man-pages-6.00"], (1, "", ""))
        self.assertEqual(seg["man-pages-6.1-rc1"], (2, "man-pages-6.00", "man-pages-6.00"))
        self.assertEqual(seg["man-pages-6.01"], (2, "man-pages-6.1-rc1", "man-pages-6.00"))
        self.assertEqual(seg["man-pages-6.00.1"], (1, "man-pages-6.00", "man-pages-6.00"))
        first = {n for (n,) in self.rows(
            ("m_person_first_release", "f"), ["p.name"], {"f.repo_id": 2},
            joins=[Join(("m_maintainer_person", "p"), {"p.person_id": "f.person_id"})])}
        self.assertEqual(first, {"Ann B", "Neo N", "Cy D"})

    def test_moved_mainline_tag_rebuilds_first_releases(self) -> None:
        rst.ingest(self.db, self.dir, repo_id=1)
        self.git("tag", "-f", "v3.0", "v3.1-rc1")  # v3.0 now holds Neo's commit too
        self.assertEqual(rst.ingest(self.db, self.dir, repo_id=1)["state"], "updated")
        first = {n: t for n, t in self.rows(
            ("m_person_first_release", "f"), ["p.name", "s.tag"],
            joins=[*self.tag_join("f"), Join(("m_maintainer_person", "p"), {"p.person_id": "f.person_id"})])}
        self.assertEqual(first["Neo N"], "v3.0")
        self.assertEqual(self.db.count_rows("m_release_segment", {"tag": "v3.0"}), 1)
        # The replaced segments rewrote their commits: Neo's commit now belongs to v3.0, none is orphaned
        neo = self.git("log", "--format=%H", "--author=Neo", "-1", "v3.1").strip()
        self.assertEqual(self.rows(("m_commit_release", "r"), ["s.tag"],
                                   {"r.commit_hash": bytes.fromhex(neo), "r.chain": rst.CHAIN_MAINLINE},
                                   joins=self.tag_join("r")), [("v3.0",)])
        held = {h for (h,) in self.rows("m_commit_release", ["commit_hash"], {"chain": rst.CHAIN_MAINLINE})}
        self.assertEqual({bytes.fromhex(x) for x in self.git("rev-list", "v3.1").split()}, held)

    def test_ingest_insights_populated_and_incremental(self) -> None:
        summary = rst.ingest(self.db, self.dir, repo_id=1, workers=2)
        self.assertEqual(summary["state"], "updated")

        # Verify m_release_pull was populated
        pull_rows = [tuple(r) for r in self.db.select_rows(
            ("m_release_pull", "p"), ["s.tag", "p.pull_idx", "p.commits", "p.subject", "p.format"],
            joins=self.tag_join("p"),
        )]
        pull_by_tag = {t: (idx, c, subj, fmt) for t, idx, c, subj, fmt in pull_rows}

        # v3.0 has 0 merges -> sentinel row
        self.assertEqual(pull_by_tag["v3.0"], (rst.EMPTY_PULL_IDX, 0, "", rst.MW_FORMAT))

        # v3.1 has 1 merge -> pull_idx 0, commits 1, subject "Merge branch 'side'"
        self.assertEqual(pull_by_tag["v3.1"], (0, 1, "Merge branch 'side'", rst.MW_FORMAT))

        # Verify m_release_section_health has sentinel rows for v3.0 and v3.1
        health_rows = [tuple(r) for r in self.db.select_rows(
            ("m_release_section_health", "h"), ["s.tag", "h.sec_name", "h.format"],
            joins=self.tag_join("h"),
        )]
        health_tags = {t for t, sec_name, fmt in health_rows if sec_name == "" and fmt == rst.HEALTH_FORMAT}
        self.assertIn("v3.0", health_tags)
        self.assertIn("v3.1", health_tags)

        # Incremental check: re-running ingest_insights returns current
        again = rst.ingest_insights(self.db, self.dir, repo_id=1)
        self.assertEqual(again["state"], "current")
        self.assertEqual(again["segments"], 0)

    def test_ingest_insights_with_maintainers(self) -> None:
        self.git("checkout", "-q", "master")
        maint_content = "NETWORKING\nM: Ann B <ann@x.org>\nS: Maintained\nF: a.c\n"
        self.commit("Ann B", "ann@x.org", "Add MAINTAINERS", "MAINTAINERS", maint_content)
        self.commit("Neo N", "neo@n.org", "net fix\n\nReviewed-by: Cy D <cy@y.org>", "a.c", "1\n2\n3\n4\n")
        self.git("tag", "v3.2")

        summary = rst.ingest(self.db, self.dir, repo_id=1, workers=2)
        self.assertEqual(summary["state"], "updated")

        seg_row = self.db.select_one("m_release_segment", ["segment_id"], {"repo_id": 1, "tag": "v3.2"})
        self.assertIsNotNone(seg_row)
        sid = int(seg_row[0])

        rows = [tuple(r) for r in self.db.select_rows(
            "m_release_section_health",
            ["sec_name", "status", "commits", "reviewed_commits", "format"],
            {"segment_id": sid},
        )]
        rows_by_name = {name: (st, c, rc, fmt) for name, st, c, rc, fmt in rows}
        self.assertIn("", rows_by_name)  # Sentinel row
        self.assertIn("NETWORKING", rows_by_name)
        self.assertEqual(rows_by_name["NETWORKING"], ("Maintained", 1, 1, rst.HEALTH_FORMAT))

        # Check incremental after maintainers ingestion
        self.assertEqual(rst.ingest_insights(self.db, self.dir, repo_id=1)["state"], "current")


class TestInsightsHelpers(unittest.TestCase):
    def test_hash_helpers(self) -> None:
        from parser.maintainer_ast.maintainer_types import MaintainerSection, PatternRule, PatternType

        sec = MaintainerSection(name="NETWORKING", status="Maintained")
        sec.patterns = [
            PatternRule(pat_type=PatternType.FILE, pattern="drivers/net/"),
            PatternRule(pat_type=PatternType.FILE, pattern="net/"),
        ]
        h1 = rst.section_pattern_hash(sec)
        self.assertEqual(len(h1), 20)

        sec2 = MaintainerSection(name="NETWORKING", status="Maintained")
        sec2.patterns = [
            PatternRule(pat_type=PatternType.FILE, pattern="/net/"),
            PatternRule(pat_type=PatternType.FILE, pattern="drivers/net/"),
        ]
        self.assertEqual(rst.section_pattern_hash(sec2), h1)

        m_hash = rst.sections_manifest_hash([sec, sec2], catchall_section="THE REST")
        self.assertEqual(len(m_hash), 20)

    def test_write_release_pulls_and_health(self) -> None:
        db = _release_db()
        n = rst.write_release_pulls(db, segment_id=42, pulls=[])
        self.assertEqual(n, 1)
        rows = [tuple(r) for r in db.select_rows("m_release_pull", ["segment_id", "pull_idx", "format"], {"segment_id": 42})]
        self.assertEqual(rows, [(42, rst.EMPTY_PULL_IDX, rst.MW_FORMAT)])

        n_h = rst.write_release_section_health(db, segment_id=42, sections=[], sec_stats={})
        self.assertEqual(n_h, 1)
        h_rows = [tuple(r) for r in db.select_rows("m_release_section_health", ["segment_id", "sec_name", "format"], {"segment_id": 42})]
        self.assertEqual(h_rows, [(42, "", rst.HEALTH_FORMAT)])

    def test_process_workers_saturates_cpus(self) -> None:
        self.assertEqual(rst.process_workers(), max(1, os.cpu_count() or 4))


class TestInsightsDeltas(unittest.TestCase):
    """Tests for delta tables, pure extractor, idempotent upsert, ReadIntents ranges, and backfills."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="insights-deltas-"))
        self.db = SQLiteDB(":memory:", foreign_keys=False)
        self.db.create_table(D.TABLES)
        self.git("init", "-q", "-b", "master")

        maint = (
            "NETWORKING\n"
            "M:\tAnn B <ann@x.org>\n"
            "S:\tMaintained\n"
            "F:\tdrivers/net/\n\n"
            "FILESYSTEMS\n"
            "M:\tBob Z <bob@z.org>\n"
            "S:\tSupported\n"
            "F:\tfs/\n\n"
            "THE REST\n"
            "M:\tLinus <torvalds@l.org>\n"
            "S:\tBuried\n"
            "F:\t*\n"
        )
        self.commit("Ann B", "ann@x.org", "init MAINTAINERS", {"MAINTAINERS": maint, "Makefile": "VERSION = 3\n"}, ts=1_700_000_000)
        self.sha_v30_net = self.commit("Ann B", "ann@x.org", "net: init eth", {"drivers/net/eth.c": "int x = 1;\n"}, ts=1_700_000_100)
        self.git("tag", "v3.0")

        # Empty/unmatched stable branch off v3.0 to test sentinel row with 0 matched commits
        self.git("checkout", "-q", "-b", "stable-3.0", "v3.0")
        self.commit("Ann B", "ann@x.org", "kbuild: bump Makefile", {"Makefile": "VERSION = 3\nEXTRA = .1\n"}, ts=1_700_000_200)
        self.git("tag", "v3.0.1")

        # Back to master for v3.1-rc1 and v3.1
        self.git("checkout", "-q", "master")
        self.sha_rc1_a = self.commit(
            "Ann B",
            "ann@x.org",
            "net: fix eth rx\n\nReviewed-by: Cy D <cy@y.org>\nTested-by: Tester <t@t.org>\n",
            {"drivers/net/eth.c": "int x = 1;\nint y = 2;\n"},
            ts=1_700_086_400,
        )
        self.sha_rc1_b = self.commit(
            "Ann B",
            "ann@work.com",
            f"net/fs: cross-subsys fix\n\nFixes: {self.sha_v30_net[:12]} (\"net: init eth\")\nCc: stable@vger.kernel.org\n",
            {"drivers/net/eth.c": "int x = 1;\nint y = 3;\n", "fs/ext4.c": "int f = 1;\n"},
            ts=1_700_172_800,
        )
        self.git("tag", "-a", "-m", "v3.1-rc1", "v3.1-rc1")

        # Side branch merged for v3.1 to test merge_commit_count
        self.git("checkout", "-q", "-b", "side", "v3.1-rc1")
        self.commit("Bob Z", "bob@z.org", "net: add wifi", {"drivers/net/wifi.c": "int w = 1;\n"}, ts=1_700_259_200)
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side", env_ts=1_700_259_300)
        self.sha_v31_main = self.commit(
            "Bob Z",
            "bob@z.org",
            "net: tweak wifi\n",
            {"drivers/net/wifi.c": "int w = 2;\n"},
            ts=1_700_345_600,
        )
        self.git("tag", "v3.1")

        # Stable series v3.1.1 and v3.1.2 off v3.1
        self.git("checkout", "-q", "-b", "stable-3.1", "v3.1")
        # Target timestamps: sha_rc1_a = 1_700_086_400 (+10 days = 1_700_950_400), sha_v30_net = 1_700_000_100 (+11 days diff check)
        self.sha_upstream_cve = "d" * 40
        self.sha_v311 = self.commit(
            "Ann B",
            "ann@x.org",
            (
                "net: backport fix\n\n"
                f"[ Upstream commit {self.sha_upstream_cve} ]\n"
                f"Fixes: {self.sha_rc1_a[:12]} (\"net: fix eth rx\")\n"
                f"Fixes: {self.sha_v30_net[:12]} (\"net: init eth\")\n"
                "Cc: stable@vger.kernel.org\n"
            ),
            {"drivers/net/eth.c": "int x = 10;\nint y = 3;\n"},
            ts=1_700_950_400,  # 10.0 days after sha_rc1_a, 11.0 days - 100s = 10.9988 days after sha_v30_net
        )
        self.git("tag", "v3.1.1")
        self.commit(
            "Bob Z",
            "bob@z.org",
            f"net: second backport\n\nFixes: {self.sha_rc1_a[:12]} (\"net: fix eth rx\")\n",
            {"drivers/net/wifi.c": "int w = 3;\n"},
            ts=1_701_814_400,  # 20.0 days after sha_rc1_a
        )
        self.git("tag", "v3.1.2")
        self.git("checkout", "-q", "master")

        # Seed CVE fix record matching upstream commit of v3.1.1
        self.db.insert_rows("m_cve", ["cve_id", "kind", "published", "updated", "score", "kev", "record_path", "title", "data"],
                            [("CVE-2026-0001", "commit", 0, 0, 0, 0, "p", "t", b"{}")])
        self.db.insert_rows("m_cve_commit", ["commit_hash", "role", "cve_id"],
                            [(self.sha_upstream_cve, 1, "CVE-2026-0001")])

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def git(self, *args: str, env_ts: int | None = None) -> str:
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_COMMITTER_NAME": "Linus",
            "GIT_COMMITTER_EMAIL": "torvalds@l.org",
            "GIT_AUTHOR_NAME": "Linus",
            "GIT_AUTHOR_EMAIL": "torvalds@l.org",
        }
        if env_ts is not None:
            env["GIT_AUTHOR_DATE"] = f"{env_ts} +0000"
            env["GIT_COMMITTER_DATE"] = f"{env_ts} +0000"
        return subprocess.run(["git", "-C", str(self.dir), *args], check=True, capture_output=True, text=True, env=env).stdout

    def commit(self, name: str, email: str, message: str, files: dict[str, str], ts: int) -> str:
        for rel, content in files.items():
            p = self.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            self.git("add", rel, env_ts=ts)
        self.git(
            "-c", f"user.name={name}",
            "-c", f"user.email={email}",
            "commit", "-q", "--author", f"{name} <{email}>", "-m", message,
            env_ts=ts,
        )
        return self.git("rev-parse", "HEAD").strip()

    def test_track_ordinal_and_author_id_helpers(self) -> None:
        from parser.git_ast.identity import author_identity_id
        from parser.git_ast.release_order import series_ord_for, series_track_for

        self.assertEqual(series_track_for("v3.0"), "mainline")
        self.assertEqual(series_track_for("v3.1-rc1"), "mainline")
        self.assertEqual(series_track_for("v3.0.1"), "stable:v3.0")
        self.assertEqual(series_track_for("v2.6.39.4"), "stable:v2.6.39")
        self.assertEqual(series_track_for("next-20260810"), "next")
        self.assertEqual(series_track_for("man-pages-6.9.1"), "stable:man-pages-6.9")
        self.assertEqual(series_track_for("not-a-tag"), "")
        self.assertEqual(series_ord_for("not-a-tag"), 0)

        self.assertLess(series_ord_for("v2.6.39"), series_ord_for("v3.0-rc1"))
        self.assertLess(series_ord_for("v3.0-rc1"), series_ord_for("v3.0-rc2"))
        self.assertLess(series_ord_for("v3.0-rc2"), series_ord_for("v3.0"))
        self.assertLess(series_ord_for("v3.0"), series_ord_for("v3.1-rc1"))
        self.assertEqual(series_ord_for("v3.0.1"), 1)
        self.assertEqual(series_ord_for("v3.0.2"), 2)
        self.assertEqual(series_ord_for("next-20260810"), 20260810)

        id1 = author_identity_id("n:ann b")
        id2 = author_identity_id("n:bob z")
        self.assertEqual(id1, author_identity_id("n:ann b"))
        self.assertGreaterEqual(id1, 1)
        self.assertLess(id1, 1 << 63)
        self.assertNotEqual(id1, id2)

    def test_ingest_deltas_idempotence_and_read_intents(self) -> None:
        from parser.git_ast import insights_deltas as idl
        from parser.git_ast.release_order import series_ord_for

        summary = idl.ingest_insights_deltas(self.db, self.dir, repo_id=1, workers=2)
        self.assertEqual(summary["state"], "updated")
        self.assertEqual(summary["tags"], 6)
        self.assertEqual(summary["written"], 6)

        # Incremental skip on second run
        again = idl.ingest_insights_deltas(self.db, self.dir, repo_id=1, workers=2)
        self.assertEqual(again["state"], "current")
        self.assertEqual(again["written"], 0)

        with self.db.reader() as reader:
            # Sentinel row emitted for v3.0.1 even with 0 matched commits
            v301 = reader.subsystem_version_deltas("stable:v3.0", tags=["v3.0.1"])
            self.assertEqual(len(v301), 1)
            self.assertEqual((v301[0]["subsystem_name"], v301[0]["commit_count"]), ("", 0))
            v301_stable = reader.stable_version_deltas("stable:v3.0", tags=["v3.0.1"])
            self.assertEqual(len(v301_stable), 1)
            self.assertEqual((v301_stable[0]["subsystem_name"], v301_stable[0]["total_commits"]), ("", 0))

            # Half-open (from_ord, to_ord] range query: (v3.0, v3.1] excludes v3.0 and includes v3.1-rc1, v3.1
            deltas = reader.subsystem_version_deltas(
                "mainline",
                from_ord=series_ord_for("v3.0"),
                to_ord=series_ord_for("v3.1"),
            )
            by_tag_sub = {(r["version_tag"], r["subsystem_name"]): r for r in deltas}
            self.assertNotIn(("v3.0", ""), by_tag_sub)
            self.assertIn(("v3.1-rc1", ""), by_tag_sub)
            self.assertIn(("v3.1", ""), by_tag_sub)
            self.assertNotIn(("v3.1-rc1", "THE REST"), by_tag_sub)

            rc1_net = by_tag_sub[("v3.1-rc1", "NETWORKING")]
            self.assertEqual(rc1_net["maintainer_status"], "Maintained")
            self.assertEqual(rc1_net["commit_count"], 2)
            self.assertEqual(rc1_net["distinct_author_count"], 1)  # both Ann B aliases fold to one identity_key
            self.assertEqual(rc1_net["reviewed_commit_count"], 1)
            self.assertEqual(rc1_net["tested_commit_count"], 1)
            self.assertEqual(rc1_net["fixes_commit_count"], 1)
            self.assertEqual(rc1_net["stable_cc_count"], 1)
            self.assertEqual(rc1_net["files_touched_count"], 1)
            self.assertGreater(rc1_net["lines_added"], 0)

            rc1_fs = by_tag_sub[("v3.1-rc1", "FILESYSTEMS")]
            self.assertEqual(rc1_fs["commit_count"], 1)
            self.assertEqual(rc1_fs["fixes_commit_count"], 1)

            v31_net = by_tag_sub[("v3.1", "NETWORKING")]
            self.assertEqual(v31_net["merge_commit_count"], 1)
            self.assertEqual(v31_net["commit_count"], 2)  # side commit + v31 commit

            # Exact COUNT(DISTINCT author_identity_id) over (v3.0, v3.1]
            dist_window = reader.subsystem_distinct_authors(
                "mainline",
                from_ord=series_ord_for("v3.0"),
                to_ord=series_ord_for("v3.1"),
            )
            self.assertEqual(dist_window, {"FILESYSTEMS": 1, "NETWORKING": 2})

            dist_rc1 = reader.subsystem_distinct_authors(
                "mainline",
                from_ord=series_ord_for("v3.0"),
                to_ord=series_ord_for("v3.1-rc1"),
            )
            self.assertEqual(dist_rc1, {"FILESYSTEMS": 1, "NETWORKING": 1})

            auth_rows = reader.subsystem_author_deltas(
                "mainline",
                from_ord=series_ord_for("v3.0"),
                to_ord=series_ord_for("v3.1"),
                subsystem_name="NETWORKING",
            )
            self.assertEqual(sum(r["commit_count"] for r in auth_rows), 4)

            # Stable version deltas + SQL SUM / avg_fix_latency_days aggregation
            st_rows = reader.stable_version_deltas(series_track="stable:v3.1")
            st_map = {(r["version_tag"], r["subsystem_name"]): r for r in st_rows}
            v311_net = st_map[("v3.1.1", "NETWORKING")]
            self.assertEqual(v311_net["base_mainline_tag"], "v3.1")
            self.assertEqual(v311_net["total_commits"], 1)
            self.assertEqual(v311_net["backport_commits"], 1)
            self.assertEqual(v311_net["fixes_tag_commits"], 1)
            self.assertEqual(v311_net["cc_stable_commits"], 1)
            self.assertEqual(v311_net["cve_linked_commits"], 1)
            self.assertEqual(v311_net["same_cycle_fixes_count"], 1)
            self.assertEqual(v311_net["cross_release_fixes_count"], 1)
            self.assertEqual(v311_net["resolved_fixes_count"], 2)

            agg_all = reader.stable_aggregated_metrics(base_mainline_tag="v3.1")
            self.assertEqual(len(agg_all), 1)
            expected_sum_days = (10.0 + (1_700_950_400 - 1_700_000_100) / 86400.0 + 20.0)
            self.assertEqual(agg_all[0]["total_commits"], 2)
            self.assertEqual(agg_all[0]["resolved_fixes_count"], 3)
            self.assertEqual(agg_all[0]["same_cycle_fixes_count"], 2)
            self.assertEqual(agg_all[0]["cross_release_fixes_count"], 1)
            self.assertEqual(agg_all[0]["avg_fix_latency_days"], round(expected_sum_days / 3, 2))

            agg_by_sub = reader.stable_aggregated_metrics(base_mainline_tag="v3.1", by_subsystem=True)
            self.assertEqual([r["subsystem_name"] for r in agg_by_sub], ["NETWORKING"])

            # Empty query cases
            self.assertEqual(reader.subsystem_version_deltas("mainline", from_ord=999999999999999), [])
            self.assertEqual(reader.subsystem_distinct_authors("mainline", tags=[]), {})
            self.assertEqual(reader.stable_aggregated_metrics(series_track="stable:v9.9"), [])
            # Zero resolved fixes -> avg_fix_latency_days is None
            agg_zero = reader.stable_aggregated_metrics(series_track="stable:v3.0")
            self.assertEqual(len(agg_zero), 1)
            self.assertIsNone(agg_zero[0]["avg_fix_latency_days"])

        # Idempotent upsert verification
        v31_sha = self.git("rev-parse", "v3.1^{commit}").strip()
        v31_rc1_sha = self.git("rev-parse", "v3.1-rc1^{commit}").strip()
        ext = idl.extract_release_deltas_from_git(self.dir, "v3.1", v31_sha, "v3.1-rc1", v31_rc1_sha, compute_pulls=False)
        for row in ext["subsystem_deltas"]:
            if row["subsystem_name"] == "NETWORKING":
                row["commit_count"] = 77
        idl.write_insights_deltas(self.db, ext)
        with self.db.reader() as reader:
            rows = reader.subsystem_version_deltas("mainline", tags=["v3.1"])
            net = [r for r in rows if r["subsystem_name"] == "NETWORKING"]
            self.assertEqual(len(net), 1)
            self.assertEqual(net[0]["commit_count"], 77)

    def test_backfill_insights_deltas_and_ingest_insights(self) -> None:
        from core import backfills

        self.assertIs(backfills.BACKFILL_HANDLERS["insights_deltas"], backfills.backfill_insights_deltas)
        res = backfills.backfill_insights_deltas(self.db, str(self.dir), repo_id=1)
        self.assertEqual(res["state"], "updated")
        self.assertGreater(res["written"], 0)
        res2 = backfills.backfill_insights_deltas(self.db, str(self.dir), repo_id=1)
        self.assertEqual(res2["state"], "current")

        # Also verify release_stats.ingest() populates delta tables alongside pulls/health
        for tbl in ("insights_subsystem_version_delta", "insights_subsystem_author_delta", "insights_stable_version_delta"):
            self.db.delete_rows(tbl, None, all_rows=True)
        ingest_summary = rst.ingest(self.db, self.dir, repo_id=1, workers=2)
        self.assertEqual(ingest_summary["state"], "updated")
        with self.db.reader() as reader:
            ml = reader.subsystem_version_deltas("mainline")
            self.assertGreater(len(ml), 0)

    def test_mockdb_read_intents_empty_and_populated(self) -> None:
        from db_engine import MockDB
        from parser.git_ast import insights_deltas as idl
        from parser.git_ast.release_order import series_ord_for

        mdb = MockDB(use_global=False)
        try:
            with mdb.reader() as reader:
                self.assertEqual(reader.subsystem_version_deltas("mainline"), [])
                self.assertEqual(reader.subsystem_distinct_authors("mainline"), {})
                self.assertEqual(reader.stable_aggregated_metrics(base_mainline_tag="v3.1"), [])

            res = idl.ingest_insights_deltas(mdb, self.dir, repo_id=1, workers=2)
            self.assertEqual(res["state"], "updated")
            with mdb.reader() as reader:
                dist = reader.subsystem_distinct_authors(
                    "mainline",
                    from_ord=series_ord_for("v3.0"),
                    to_ord=series_ord_for("v3.1"),
                )
                self.assertEqual(dist, {"FILESYSTEMS": 1, "NETWORKING": 2})
                agg = reader.stable_aggregated_metrics(base_mainline_tag="v3.1")
                self.assertEqual(len(agg), 1)
                self.assertEqual(agg[0]["total_commits"], 2)
        finally:
            mdb.close()


if __name__ == "__main__":
    unittest.main()
