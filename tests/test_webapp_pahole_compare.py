"""tests/test_webapp_pahole_compare.py - Struct layout comparison and history stops (N3, webapp/API.md section 4.7)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from webapp.backend.services.pahole_compare import (
    compare_layouts,
    history_stops,
    layout_history,
)
from webapp.backend.services.pahole_service import PaholeService, _parse_decl_type_info


def layout(members, size, holes=0, est=False):
    ms = [{"name": n, "type": t, "size": s, "offset": o} for n, t, s, o in members]
    return {"members": ms, "total_size": size, "holes_count": holes, "padding_bytes": holes * 4, "cache_lines_used": (size - 1) // 64 + 1, "is_estimate": est}


A = layout([("a", "int", 4, 0), ("b", "long", 8, 8), ("c", "char", 1, 16), ("old", "u32", 4, 20)], 24, 1)
B = layout([("a", "int", 4, 0), ("x", "int", 4, 4), ("b", "long", 8, 8), ("c", "short", 2, 16), ("new", "u32", 4, 20), ("d", "u64", 8, 64)], 72, 0, est=True)


class TestPaholeCompare(unittest.TestCase):
    def test_member_matching_and_delta(self) -> None:
        r = compare_layouts(A, B)
        status = {m["name"]: m["status"] for m in r["members"]}
        self.assertEqual(status, {"a": "same", "x": "added", "b": "same", "c": "type_changed", "old": "removed", "new": "added", "d": "added"})
        old = next(m for m in r["members"] if m["name"] == "old")
        self.assertEqual(old.get("renamed_to"), "new")
        self.assertEqual(r["delta"], {"total_size": 48, "holes_count": -1, "padding_bytes": -4, "cache_lines_used": 1})
        self.assertTrue(r["is_estimate"], "an estimate on either side propagates")
        self.assertEqual(compare_layouts(A, A)["counts"], {"same": 4})
        moved = compare_layouts(A, layout([("a", "int", 4, 0), ("b", "long", 8, 16), ("c", "char", 1, 8), ("old", "u32", 4, 20)], 24))
        self.assertEqual({m["name"]: m["status"] for m in moved["members"]}["b"], "moved")

    def test_history(self) -> None:
        timeline = [{"vname": "v1", "status": "absent"}, {"vname": "v2", "status": "introduced"}, {"vname": "v3", "status": "unchanged"},
                    {"vname": "v4", "status": "cosmetic"}, {"vname": "v5", "status": "modified"}]
        self.assertEqual(history_stops(timeline), ["v2", "v5"])

        def layout_of(v):
            if v == "v5":
                raise HTTPException(status_code=404, detail="not indexed")
            return A
        stops = layout_history(["v2", "v5"], layout_of)
        self.assertEqual([(s["version"], s["available"]) for s in stops], [("v2", True), ("v5", False)])
        self.assertEqual(stops[0]["total_size"], 24)


class TestPaholeServiceLayout(unittest.TestCase):
    def test_ext3_inode_info_layout_and_unsigned_long_alignment(self) -> None:
        from unittest.mock import MagicMock, patch

        svc = PaholeService()
        reader = MagicMock()
        ext3_def = {"ast_id": 100, "blob_id": 1, "line_s": 70, "line_e": 149, "linkage": 0, "fname": "include/linux/ext3_fs_i.h"}
        inode_def = {"ast_id": 200, "blob_id": 2, "line_s": 738, "line_e": 811, "linkage": 0, "fname": "include/linux/fs.h"}

        ext3_fields = [
            {"priority": 0, "ref_ast_id": 101, "field_name": "i_data", "type_name": "C_SCtypedef", "ref_type_name": "C_SCtypedef", "attr_size": "88", "attr_offset": None, "attr_bitfield": None, "attr_type_str": "__le32[22]"},
            {"priority": 1, "ref_ast_id": 102, "field_name": "i_block_group", "type_name": "C_SCtypedef", "ref_type_name": "C_SCtypedef", "attr_size": "4", "attr_offset": None, "attr_bitfield": None, "attr_type_str": "__u32"},
            {"priority": 2, "ref_ast_id": 103, "field_name": "i_state_flags", "type_name": "C_unsigned", "ref_type_name": "C_unsigned", "attr_size": "8", "attr_offset": None, "attr_bitfield": None, "attr_type_str": "unsigned long"},
            {"priority": 3, "ref_ast_id": 104, "field_name": "inode", "type_name": "C_struct", "ref_type_name": "C_struct", "attr_size": None, "attr_offset": None, "attr_bitfield": None, "attr_type_str": None},
            {"priority": 4, "ref_ast_id": 105, "field_name": "vfs_inode", "type_name": "C_Compound", "ref_type_name": "C_Compound", "attr_size": None, "attr_offset": None, "attr_bitfield": None, "attr_type_str": "struct inode"},
        ]
        inode_fields = [
            {"priority": 0, "ref_ast_id": 201, "field_name": "i_body", "type_name": "C_long", "ref_type_name": "C_long", "attr_size": "600", "attr_offset": "0", "attr_bitfield": None, "attr_type_str": "unsigned long[75]"},
        ]

        def fake_defn(_r, _v, name):
            if name == "ext3_inode_info":
                return "v3.0", ext3_def
            if name == "inode":
                return "v3.0", inode_def
            return "v3.0", None

        def fake_fetch(_r, ast_id):
            return ext3_fields if ast_id == 100 else (inode_fields if ast_id == 200 else [])

        with patch.object(svc, "_definition", side_effect=fake_defn), \
             patch.object(svc, "_fetch_container_fields", side_effect=fake_fetch), \
             patch.object(svc, "_extract_source_field_info", return_value=({"i_state_flags": "unsigned long", "vfs_inode": "struct inode"}, {"i_data", "i_block_group", "i_state_flags", "vfs_inode"})):
            res = svc._compute_layout(reader, "v3.0", "ext3_inode_info", ext3_def, _seen=frozenset({"ext3_inode_info"}))

        self.assertFalse(res["is_estimate"])
        self.assertEqual(res["estimated_members"], 0)
        names = [m["name"] for m in res["members"]]
        self.assertNotIn("inode", names)
        self.assertEqual(names, ["i_data", "i_block_group", "i_state_flags", "vfs_inode"])
        st = next(m for m in res["members"] if m["name"] == "i_state_flags")
        self.assertEqual((st["size"], st["alignment"], st["offset"], st["padding_before"]), (8, 8, 96, 4))
        vfs = next(m for m in res["members"] if m["name"] == "vfs_inode")
        self.assertEqual((vfs["size"], vfs["alignment"], vfs["estimated"]), (600, 8, False))
        self.assertEqual(_parse_decl_type_info("unsigned long")[2], (8, 8))
        self.assertEqual(_parse_decl_type_info("struct inode")[3], ("struct", "inode"))


if __name__ == "__main__":
    unittest.main()
