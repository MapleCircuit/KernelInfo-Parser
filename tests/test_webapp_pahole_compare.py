"""tests/test_webapp_pahole_compare.py - Struct layout comparison and history stops (N3, webapp/API.md section 4.7)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from webapp.backend.services.pahole_compare import compare_layouts, history_stops, layout_history


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


if __name__ == "__main__":
    unittest.main()
