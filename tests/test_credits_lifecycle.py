"""tests/test_credits_lifecycle.py - Multi-Version Differential Lifecycle Tests for CREDITS & MAINTAINERS."""
from __future__ import annotations
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.globalstuff import G, OP_REF, REF_POS, REF_ROOT
from core.GreatProcessor import GreatProcessor
from core.FileHandler import MasterFile
from core.TableHandling import Table, ChangeSet
from core.DBLayout import (
    init_db_layout,
    m_tag,
    m_map_ast,
    m_bridge_map,
    m_credits_entry,
    m_maintainer_section,
    m_maintainer_member,
    m_maintainer_pattern,
)
from table_engine import TECachedDB
from db_engine import MockDB
from parser.maintainer_ast.maintainer_ast import CreditsManager, MaintainerManager


CREDITS_V1 = """
N: Linus Torvalds
E: torvalds@linux-foundation.org
D: Original kernel author
S: Helsinki, Finland

N: Theodore Ts'o
E: tytso@mit.edu
D: Ext4 filesystem maintainer
S: Cambridge, Massachusetts
"""

CREDITS_V2 = """
N: Linus Torvalds
E: torvalds@linux-foundation.org
D: Original kernel author
S: Helsinki, Finland

N: Theodore Ts'o
E: tytso@mit.edu
D: Ext4 filesystem & random driver maintainer
S: Cambridge, Massachusetts

N: New Contributor
E: new@example.com
D: Added awesome driver
S: London, UK
"""

MAINTAINERS_V1 = """
EXT4 FILE SYSTEM
M:	Theodore Ts'o <tytso@mit.edu>
L:	linux-ext4@vger.kernel.org
S:	Maintained
F:	fs/ext4/

BTRFS FILE SYSTEM
M:	Chris Mason <clm@fb.com>
L:	linux-btrfs@vger.kernel.org
S:	Maintained
F:	fs/btrfs/
"""

MAINTAINERS_V2 = """
EXT4 FILE SYSTEM
M:	Theodore Ts'o <tytso@mit.edu>
L:	linux-ext4@vger.kernel.org
S:	Maintained
F:	fs/ext4/

BTRFS FILE SYSTEM
M:	Chris Mason <clm@fb.com>
M:	Josef Bacik <josef@toxicpanda.com>
L:	linux-btrfs@vger.kernel.org
S:	Maintained
F:	fs/btrfs/

NEW DRIVER
M:	New Author <author@driver.org>
S:	Maintained
F:	drivers/new/
"""


class DummyMasterFile:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], str] = {}

    def get_file(self, path: str, version: str) -> str:
        return self.files.get((path, version), "")


class TestCreditsAndMaintainerLifecycle(unittest.TestCase):
    """Test differential change detection across sequential versions."""

    def setUp(self) -> None:
        self.gp = GreatProcessor()
        init_db_layout(self.gp)
        G.DB = MockDB
        G.TE = TECachedDB()
        G.TE.start(self.gp.Table_Array, G.DB)

    def test_credits_differential_lifecycle(self) -> None:
        """Verify that unchanged credits entries are NOT re-inserted on version upgrade."""
        dmf = DummyMasterFile()
        dmf.files[("CREDITS", "v3.0")] = CREDITS_V1
        dmf.files[("CREDITS", "v3.1")] = CREDITS_V2

        # 1. Parse Version 1 (v3.0, VID=1, Old_VID=0)
        self.gp.Version_Name = "v3.0"
        self.gp.Old_Version_Name = ""
        self.gp.VID = 1
        self.gp.Old_VID = 0

        cs1 = ChangeSet("A\tCREDITS")
        cs1.gp = self.gp
        cs1.mf = dmf
        mgr1 = CreditsManager(cs1)

        credits_ops_v1 = [op for op in cs1.cs if op[0] == m_credits_entry.table_id]
        # In v1, 2 entries parsed -> 2 m_credits_entry insertions
        self.assertEqual(len(credits_ops_v1), 2)

        # 2. Parse Version 2 (v3.1, VID=2, Old_VID=1)
        self.gp.Version_Name = "v3.1"
        self.gp.Old_Version_Name = "v3.0"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        cs2 = ChangeSet("M\tCREDITS")
        cs2.gp = self.gp
        cs2.mf = dmf
        mgr2 = CreditsManager(cs2)

        credits_ops_v2 = [op for op in cs2.cs if op[0] == m_credits_entry.table_id]
        # In v2: Linus is unchanged (0 insert), Ted is modified (1 insert), New is added (1 insert)
        # Total inserts should be exactly 2 (NOT 3!)
        self.assertEqual(len(credits_ops_v2), 2)

    def test_maintainers_differential_lifecycle(self) -> None:
        """Verify that unchanged maintainer sections are NOT re-inserted on version upgrade."""
        dmf = DummyMasterFile()
        dmf.files[("MAINTAINERS", "v3.0")] = MAINTAINERS_V1
        dmf.files[("MAINTAINERS", "v3.1")] = MAINTAINERS_V2

        # 1. Parse Version 1 (v3.0, VID=1, Old_VID=0)
        self.gp.Version_Name = "v3.0"
        self.gp.Old_Version_Name = ""
        self.gp.VID = 1
        self.gp.Old_VID = 0

        cs1 = ChangeSet("A\tMAINTAINERS")
        cs1.gp = self.gp
        cs1.mf = dmf
        mgr1 = MaintainerManager(cs1)

        sec_ops_v1 = [op for op in cs1.cs if op[0] == m_maintainer_section.table_id]
        self.assertEqual(len(sec_ops_v1), 2)

        # 2. Parse Version 2 (v3.1, VID=2, Old_VID=1)
        self.gp.Version_Name = "v3.1"
        self.gp.Old_Version_Name = "v3.0"
        self.gp.VID = 2
        self.gp.Old_VID = 1

        cs2 = ChangeSet("M\tMAINTAINERS")
        cs2.gp = self.gp
        cs2.mf = dmf
        mgr2 = MaintainerManager(cs2)

        sec_ops_v2 = [op for op in cs2.cs if op[0] == m_maintainer_section.table_id]
        # In v2: EXT4 is unchanged (0 insert), BTRFS is modified (1 insert), NEW DRIVER is added (1 insert)
        # Total inserts should be 2 (NOT 3!)
        self.assertEqual(len(sec_ops_v2), 2)


if __name__ == "__main__":
    unittest.main()
