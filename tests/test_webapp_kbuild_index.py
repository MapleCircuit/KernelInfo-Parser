"""tests/test_webapp_kbuild_index.py - Kbuild object index helpers (S04, webapp/API.md section 4.4)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.backend.services import kbuild_index_service as k

IDX = {
    "sha": "x", "arch": "x86_64", "srcarch": "x86",
    "exprs": [{"and": [{"sym": "E1000E"}, {"sym": "NETDEVICES"}]}, "y", {"and": [{"def": "PCI"}, {"sym": "SND_HDA"}]}],
    "objects": [
        {"target": "drivers/net/e1000e/e1000e.o", "modname": "e1000e", "kind": "obj", "expr": 0, "composite": True,
         "members": [{"path": "drivers/net/e1000e/netdev.c", "expr": 1}, {"path": "drivers/net/e1000e/lib.c", "expr": 1}], "rules": []},
        {"target": "sound/pci/hda/snd-hda-intel.o", "modname": "snd_hda_intel", "kind": "obj", "expr": 2, "composite": True,
         "members": [{"path": "sound/pci/hda/hda_intel.c", "expr": 1}], "rules": []},
    ],
    "files": {"drivers/net/e1000e/netdev.c": [0], "drivers/net/e1000e/lib.c": [0], "sound/pci/hda/hda_intel.c": [1]},
}


class TestKbuildIndex(unittest.TestCase):
    def test_required_symbols_and_module_capability(self) -> None:
        self.assertEqual(k.required_symbols(IDX["exprs"][0]), ["E1000E", "NETDEVICES"])
        self.assertEqual(k.required_symbols({"or": [{"sym": "A"}, {"sym": "B"}]}), [])
        self.assertTrue(k.module_capable({"sym": "A"}))
        self.assertFalse(k.module_capable({"def": "A"}))
        self.assertFalse(k.module_capable("y"))

    def test_module_and_file_lookups(self) -> None:
        out = k.objects_for_module(IDX, ["e1000e", "snd-hda-intel", "missing"])
        self.assertEqual(out["unmatched"], ["missing"])
        self.assertEqual(out["modules"]["e1000e"]["required_symbols"], ["E1000E", "NETDEVICES"])
        self.assertEqual(out["modules"]["snd-hda-intel"]["objects"][0]["target"], "sound/pci/hda/snd-hda-intel.o")
        self.assertEqual(out["modules"]["snd-hda-intel"]["required_symbols"], ["PCI", "SND_HDA"])
        objs = k.objects_for_file(IDX, "/drivers/net/e1000e/lib.c")
        self.assertEqual([o["modname"] for o in objs], ["e1000e"])
        self.assertEqual(k.objects_for_file(IDX, "nope.c"), [])
        c = k.compact(IDX)
        self.assertEqual(c["objects"][0], {"t": "drivers/net/e1000e/e1000e.o", "n": "e1000e", "k": "obj", "e": 0,
                                           "m": [["drivers/net/e1000e/netdev.c", 1], ["drivers/net/e1000e/lib.c", 1]]})


if __name__ == "__main__":
    unittest.main()


    def test_module_gates(self) -> None:
        # Every symbol of a module-capable condition (K1 trim candidates); `def`-only conditions are not module-capable
        self.assertEqual(k.module_gates(IDX), ["E1000E", "NETDEVICES", "PCI", "SND_HDA"])


class TestKconfigFragments(unittest.TestCase):
    """Fragment listing and the fragment paths `GET /defconfig` accepts (K2)."""

    def setUp(self) -> None:
        from unittest import mock
        from webapp.backend.services.kconfig_service import KconfigService
        self.svc = KconfigService()
        info = {"rev": "abc", "arch": "x86_64", "srcarch": "x86"}
        listing = {"kernel/configs": ["kvm_guest.config", "tiny.config", "README"], "arch/x86/configs": ["x86_64_defconfig", "xen.config"]}
        self.read = []
        patches = [
            mock.patch.object(self.svc, "resolve_arch", return_value=info),
            mock.patch.object(self.svc, "_tree_listing", side_effect=lambda rev, path, recursive=False: listing.get(path, [])),
            mock.patch.object(self.svc, "_read", side_effect=lambda rev, path: self.read.append(path) or "CONFIG_KVM=y\n"),
            mock.patch.object(self.svc, "get_model", side_effect=__import__("fastapi").HTTPException(status_code=404)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_listing(self) -> None:
        got = self.svc.list_fragments("v6.6", "x86_64")["fragments"]
        self.assertEqual([f["path"] for f in got], ["kernel/configs/kvm_guest.config", "kernel/configs/tiny.config", "arch/x86/configs/xen.config"])
        self.assertEqual([f["scope"] for f in got], ["common", "common", "x86"])

    def test_paths(self) -> None:
        from fastapi import HTTPException
        res = self.svc.get_defconfig_content("v6.6", "kernel/configs/kvm_guest.config", "x86_64")
        self.assertEqual((res["file_path"], res["values"]), ("kernel/configs/kvm_guest.config", {"KVM": "y"}))
        for bad in ("kernel/configs/../../Makefile", "arch/../Makefile", "kernel/configs/x.txt", "kernel/configs/sub/a.config"):
            with self.assertRaises(HTTPException) as cm:
                self.svc.get_defconfig_content("v6.6", bad, "x86_64")
            self.assertEqual(cm.exception.status_code, 400, bad)
        self.assertEqual(self.read, ["kernel/configs/kvm_guest.config"])


class TestBuildTreemap(unittest.TestCase):
    def test_classification_and_validation(self) -> None:
        from fastapi import HTTPException
        full = {f"S{i}": "n" for i in range(60)}
        cls = k.classify_files(IDX, {**full, "E1000E": "m", "NETDEVICES": "y"})
        self.assertEqual(cls["drivers/net/e1000e/netdev.c"], "m")
        self.assertEqual(cls["sound/pci/hda/hda_intel.c"], "n")  # SND_HDA unset in a full configuration
        partial = k.classify_files(IDX, {"E1000E": "y", "NETDEVICES": "y"})
        self.assertEqual(partial["drivers/net/e1000e/netdev.c"], "y")
        self.assertEqual(partial["sound/pci/hda/hda_intel.c"], "?")  # unknown in a partial one
        self.assertEqual(k.validate_assignments({"CONFIG_X": "y", "Y": 3}), {"X": "y", "Y": "3"})
        for bad in ({"a b": "y"}, {"X": ["y"]}, "nope"):
            with self.assertRaises(HTTPException):
                k.validate_assignments(bad)
