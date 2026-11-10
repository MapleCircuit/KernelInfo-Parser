"""tests/test_webapp_ops.py - Ops implementations over the source index (N1, webapp/API.md section 4.7)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from tests.test_webapp_source_index import SOURCES, load_from
from webapp.backend.services import ops_service as o
from webapp.backend.services.source_index import RevisionIndex


class TestOps(unittest.TestCase):
    def setUp(self) -> None:
        files, load = load_from(SOURCES)
        idx = RevisionIndex("0" * 40, files, load)
        self.patches = [patch("webapp.backend.services.kapi_service.resolve", lambda v: "0" * 40),
                        patch("webapp.backend.services.source_index.revision_state", lambda sha, wait=0.0: {"state": "done", "index": idx})]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_struct_report_shape(self) -> None:
        r = o.index_state("v3.0", "file_operations")["result"]
        self.assertEqual(sorted(r), ["fields", "files", "positional_skipped", "struct", "tables"])
        impl = r["fields"]["read"][0]
        self.assertEqual(impl, {"function": "foo_read", "path": "fs/ops.c", "line": 4,
                                "table": {"name": "foo_fops", "path": "fs/ops.c", "line": 2}})
        self.assertEqual(o.index_state("v3.0", "no_such_ops")["result"]["tables"], 0)

    def test_uses(self) -> None:
        r = o.uses_state("v3.0", "arr_read1")["result"]
        self.assertEqual([(u["struct"], u["field"], u["table"], u["path"]) for u in r["uses"]], [("file_operations", "read", "arr_fops", "fs/ops.c")])
        self.assertEqual(o.uses_state("v3.0", "inner_read")["result"]["uses"][0]["field"], "nested.read", "a nested initializer keeps its path")

    def test_building_and_validation(self) -> None:
        with patch("webapp.backend.services.source_index.revision_state", lambda sha, wait=0.0: {"state": "building", "progress": {"done": 1}}):
            self.assertEqual(o.index_state("v3.0", "file_operations")["state"], "building")
        with self.assertRaises(HTTPException):
            o.index_state("v3.0", "file operations; drop")
        # Scoped Rust identifiers are accepted
        res = o.index_state("v3.0", "pci::Driver")
        self.assertEqual(res["state"], "done")


if __name__ == "__main__":
    unittest.main()
