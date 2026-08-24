"""tests/test_maintainer_ast.py - Unit & Integration Test Suite for Maintainer & Credits Subsystem.

Validates MAINTAINERS & CREDITS syntax parsing, RFC name/email extraction, pattern matching engine,
unified person deduplication, ChangeSet relational mapping, and MockDB pipeline execution.
"""
from __future__ import annotations
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import G, ASTT, REF_ROOT, REF_C_AST
from core.GreatProcessor import GreatProcessor
from core.TableHandling import ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_file_name,
    m_file,
    m_bridge_file,
    m_ast,
    m_ast_container,
    m_tag,
    m_bridge_tag,
    m_map_ast,
    m_bridge_map,
    m_maintainer_person,
    m_maintainer_section,
    m_maintainer_member,
    m_maintainer_pattern,
    m_maintainer_file,
    m_credits_entry,
)
from db_engine import MockDB
from table_engine import TECachedDB
from parser.maintainer_ast.maintainer_types import (
    MaintainerRole,
    PatternType,
    MaintainerPerson,
    PatternRule,
    MaintainerSection,
    CreditsEntry,
)
from parser.maintainer_ast.maintainer_parser import (
    MaintainerParser,
    parse_person_string,
)
from parser.maintainer_ast.credits_parser import CreditsParser
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.maintainer_ast.maintainer_ast import (
    maintainer_ast_parse,
    credits_ast_parse,
    query_maintainers_for_file,
    query_credits,
)

SAMPLE_MAINTAINERS = """
        List of maintainers and how to submit kernel changes

Descriptions of section entries:
        M: Mail patches to: FullName <address@domain>
        R: Reviewer: FullName <address@domain>
        L: Mailing list that is relevant to this area
        W: Web-page with status/info
        S: Status
        F: Files and directories with wildcard patterns.
        X: Files and directories that are NOT maintained

Maintainers List (try to look for most precise areas first)

-----------------------------------

3C505 NETWORK DRIVER
M:      Philip Blundell <philb@gnu.org>
L:      netdev@vger.kernel.org
S:      Maintained
F:      drivers/net/3c505*

EXT4 FILE SYSTEM
M:      "Theodore Ts'o" <tytso@mit.edu>
M:      Andreas Dilger <adilger.kernel@dilger.ca>
R:      Jan Kara <jack@suse.cz>
L:      linux-ext4@vger.kernel.org
W:      http://ext4.wiki.kernel.org
T:      git git://git.kernel.org/pub/scm/linux/kernel/git/tytso/ext4.git
S:      Maintained
F:      fs/ext4/
F:      Documentation/filesystems/ext4.txt

NETWORKING [GENERAL]
M:      "David S. Miller" <davem@davemloft.net>
R:      Eric Dumazet <edumazet@google.com>
L:      netdev@vger.kernel.org
S:      Maintained
F:      net/
X:      net/ipv6/
X:      net/bluetooth/

IPV6 NETWORKING
M:      Hideaki YOSHIFUJI <yoshfuji@linux-ipv6.org>
L:      netdev@vger.kernel.org
S:      Maintained
F:      net/ipv6/
"""

SAMPLE_CREDITS = """
        This is at least a partial credits-file of people that have
        contributed to the Linux project.

----------

N: Linus Torvalds
E: torvalds@linux-foundation.org
D: Original author of the Linux kernel
S: Santa Clara, California
S: USA

N: Alan Cox
E: alan@lxorguk.ukuu.org.uk
W: http://www.linux.org.uk/
P: 1024/8A6A4E45
D: Linux Networking, TTY, Z80, Sound, IDE, and general fixes
S: Swansea, Wales
S: UK

N: Theodore Ts'o
E: tytso@mit.edu
W: http://thunk.org/tytso/
D: Random number generator, Ext4, serial driver, POSIX job control
S: Cambridge, Massachusetts
S: USA
"""


class TestMaintainerParser(unittest.TestCase):
    """Test MAINTAINERS file text parsing and section extraction."""

    def test_parse_person_string(self) -> None:
        p1 = parse_person_string("Linus Torvalds <torvalds@linux-foundation.org>", MaintainerRole.MAINTAINER)
        self.assertEqual(len(p1), 1)
        self.assertEqual(p1[0].name, "Linus Torvalds")
        self.assertEqual(p1[0].email, "torvalds@linux-foundation.org")
        self.assertEqual(p1[0].role, MaintainerRole.MAINTAINER)

        p2 = parse_person_string('"Theodore Ts\'o" <tytso@mit.edu>', MaintainerRole.MAINTAINER)
        self.assertEqual(len(p2), 1)
        self.assertEqual(p2[0].name, "Theodore Ts'o")
        self.assertEqual(p2[0].email, "tytso@mit.edu")

        p3 = parse_person_string("<someone@kernel.org>", MaintainerRole.REVIEWER)
        self.assertEqual(len(p3), 1)
        self.assertEqual(p3[0].name, "")
        self.assertEqual(p3[0].email, "someone@kernel.org")
        self.assertEqual(p3[0].role, MaintainerRole.REVIEWER)

        p4 = parse_person_string("Alice <alice@test.org>, Bob <bob@test.org>", MaintainerRole.MAINTAINER)
        self.assertEqual(len(p4), 2)
        self.assertEqual(p4[0].name, "Alice")
        self.assertEqual(p4[0].email, "alice@test.org")
        self.assertEqual(p4[1].name, "Bob")
        self.assertEqual(p4[1].email, "bob@test.org")

    def test_parse_sample_sections(self) -> None:
        parser = MaintainerParser(SAMPLE_MAINTAINERS)
        sections = parser.parse()
        self.assertEqual(len(sections), 4)

        sec0 = sections[0]
        self.assertEqual(sec0.name, "3C505 NETWORK DRIVER")
        self.assertEqual(sec0.status, "Maintained")
        self.assertEqual(sec0.mailing_list, "netdev@vger.kernel.org")
        self.assertEqual(len(sec0.members), 1)
        self.assertEqual(sec0.members[0].name, "Philip Blundell")
        self.assertEqual(sec0.members[0].email, "philb@gnu.org")
        self.assertEqual(len(sec0.patterns), 1)
        self.assertEqual(sec0.patterns[0].pat_type, PatternType.FILE)
        self.assertEqual(sec0.patterns[0].pattern, "drivers/net/3c505*")

        sec1 = sections[1]
        self.assertEqual(sec1.name, "EXT4 FILE SYSTEM")
        self.assertEqual(sec1.web_page, "http://ext4.wiki.kernel.org")
        self.assertEqual(sec1.scm_tree, "git git://git.kernel.org/pub/scm/linux/kernel/git/tytso/ext4.git")
        self.assertEqual(len(sec1.get_maintainers()), 2)
        self.assertEqual(len(sec1.get_reviewers()), 1)
        self.assertEqual(sec1.get_reviewers()[0].name, "Jan Kara")
        self.assertEqual(len(sec1.get_file_patterns()), 2)

        sec2 = sections[2]
        self.assertEqual(sec2.name, "NETWORKING [GENERAL]")
        self.assertEqual(len(sec2.get_file_patterns()), 1)
        self.assertEqual(len(sec2.get_exclude_patterns()), 2)
        self.assertIn("net/ipv6/", sec2.get_exclude_patterns())


class TestCreditsParser(unittest.TestCase):
    """Test CREDITS file text parsing and contributor profile extraction."""

    def test_parse_sample_credits(self) -> None:
        parser = CreditsParser(SAMPLE_CREDITS)
        entries = parser.parse()
        self.assertEqual(len(entries), 3)

        e0 = entries[0]
        self.assertEqual(e0.name, "Linus Torvalds")
        self.assertEqual(e0.email, "torvalds@linux-foundation.org")
        self.assertIn("Original author", e0.description)
        self.assertIn("Santa Clara, California\nUSA", e0.snail_mail)

        e1 = entries[1]
        self.assertEqual(e1.name, "Alan Cox")
        self.assertEqual(e1.email, "alan@lxorguk.ukuu.org.uk")
        self.assertEqual(e1.web_page, "http://www.linux.org.uk/")
        self.assertEqual(e1.pgp_key, "1024/8A6A4E45")
        self.assertIn("Networking, TTY", e1.description)
        self.assertIn("Swansea, Wales", e1.snail_mail)

        e2 = entries[2]
        self.assertEqual(e2.name, "Theodore Ts'o")
        self.assertEqual(e2.email, "tytso@mit.edu")
        self.assertEqual(e2.web_page, "http://thunk.org/tytso/")
        self.assertIn("Ext4", e2.description)

    def test_query_credits_helper(self) -> None:
        res = query_credits(raw_credits_content=SAMPLE_CREDITS)
        self.assertEqual(len(res), 3)
        names = [r["name"] for r in res]
        self.assertIn("Linus Torvalds", names)
        self.assertIn("Alan Cox", names)
        self.assertIn("Theodore Ts'o", names)


class TestMaintainerMatcher(unittest.TestCase):
    """Test pattern matching engine for file-to-maintainer resolution."""

    def setUp(self) -> None:
        parser = MaintainerParser(SAMPLE_MAINTAINERS)
        self.sections = parser.parse()
        self.matcher = MaintainerMatcher(self.sections)

    def test_wildcard_matching(self) -> None:
        matches = self.matcher.match_file("drivers/net/3c505.c")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].name, "3C505 NETWORK DRIVER")

        matches_h = self.matcher.match_file("drivers/net/3c505.h")
        self.assertEqual(len(matches_h), 1)
        self.assertEqual(matches_h[0].name, "3C505 NETWORK DRIVER")

        matches_other = self.matcher.match_file("drivers/net/e1000.c")
        self.assertEqual(len(matches_other), 0)

    def test_directory_recursive_matching(self) -> None:
        matches = self.matcher.match_file("fs/ext4/super.c")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].name, "EXT4 FILE SYSTEM")

        matches_nested = self.matcher.match_file("fs/ext4/crypto/keyinfo.c")
        self.assertEqual(len(matches_nested), 1)
        self.assertEqual(matches_nested[0].name, "EXT4 FILE SYSTEM")

        matches_doc = self.matcher.match_file("Documentation/filesystems/ext4.txt")
        self.assertEqual(len(matches_doc), 1)
        self.assertEqual(matches_doc[0].name, "EXT4 FILE SYSTEM")

    def test_exclusion_overrides(self) -> None:
        matches_net = self.matcher.match_file("net/core/dev.c")
        self.assertEqual(len(matches_net), 1)
        self.assertEqual(matches_net[0].name, "NETWORKING [GENERAL]")

        matches_ipv6 = self.matcher.match_file("net/ipv6/ip6_output.c")
        self.assertEqual(len(matches_ipv6), 1)
        self.assertEqual(matches_ipv6[0].name, "IPV6 NETWORKING")

    def test_query_maintainers_helper(self) -> None:
        res = query_maintainers_for_file("fs/ext4/inode.c", sections=self.sections)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["section"], "EXT4 FILE SYSTEM")
        maintainer_emails = [m["email"] for m in res[0]["maintainers"]]
        self.assertIn("tytso@mit.edu", maintainer_emails)
        self.assertIn("adilger.kernel@dilger.ca", maintainer_emails)
        reviewer_emails = [r["email"] for r in res[0]["reviewers"]]
        self.assertIn("jack@suse.cz", reviewer_emails)


class TestMaintainerAndCreditsIntegration(unittest.TestCase):
    """Test AST generation, ChangeSet emission, and database ingestion with MockDB."""

    def setUp(self) -> None:
        G.DB = MockDB
        G.TE = TECachedDB()
        self.gp = GreatProcessor()
        self.gp.VID = 1
        self.gp.Old_VID = 0
        self.gp.Version_Name = "v3.0"
        init_db_layout(self.gp)
        G.TE.start(self.gp.Table_Array, G.DB)

    def test_maintainers_and_credits_deduplication(self) -> None:
        # 1. Process MAINTAINERS
        cs_maint = ChangeSet("MAINTAINERS")
        cs_maint.file_operation = "A"
        cs_maint.current_vid = 1
        cs_maint.gp = self.gp
        cs_maint.raw_content = SAMPLE_MAINTAINERS
        cs_maint.store(m_file_name.get_set(None, "MAINTAINERS"))
        cs_maint.store(m_file.set(None, 1, 0, 5, "A", 0))
        cs_maint.store(m_bridge_file.set(1, cs_maint.ref(m_file_name.fnid), cs_maint.ref(m_file.fid)))
        maintainer_ast_parse(cs_maint)

        # 2. Process CREDITS
        cs_cred = ChangeSet("CREDITS")
        cs_cred.file_operation = "A"
        cs_cred.current_vid = 1
        cs_cred.gp = self.gp
        cs_cred.raw_content = SAMPLE_CREDITS
        cs_cred.store(m_file_name.get_set(None, "CREDITS"))
        cs_cred.store(m_file.set(None, 1, 0, 6, "A", 0))
        cs_cred.store(m_bridge_file.set(1, cs_cred.ref(m_file_name.fnid), cs_cred.ref(m_file.fid)))
        credits_ast_parse(cs_cred)

        # Execute both ChangeSets
        self.assertTrue(cs_maint.execute())
        self.assertTrue(cs_cred.execute())
        G.TE.commit_all()

        # Validate Maintainer Section rows in TE cache
        sec_rows = G.TE._cached_rows.get(m_maintainer_section.table_id, [])
        self.assertEqual(len(sec_rows), 4)

        # Validate Credits Entry rows in TE cache
        cred_rows = G.TE._cached_rows.get(m_credits_entry.table_id, [])
        self.assertEqual(len(cred_rows), 3)

        # Validate Person Deduplication
        # Theodore Ts'o is in both MAINTAINERS and CREDITS with email tytso@mit.edu
        person_rows = G.TE._cached_rows.get(m_maintainer_person.table_id, [])
        tytso_entries = [r for r in person_rows if r[2] == "tytso@mit.edu"]
        self.assertEqual(len(tytso_entries), 1, "Theodore Ts'o must be deduplicated to a single person row")


if __name__ == "__main__":
    unittest.main()
