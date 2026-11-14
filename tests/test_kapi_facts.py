"""tests/test_kapi_facts.py - Kernel API records per blob (parser/kapi_facts.py) and the catalogs assembled from them
(webapp/backend/services/kapi_service.py, device_index_service.py; webapp/API.md section 4.9)."""
from __future__ import annotations

from cache_engine import get_cache_engine

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import kapi_facts as kf
from webapp.backend.services import device_index_service as dis
from webapp.backend.services import kapi_service as k
from webapp.backend.services import source_index as si

DRIVER_C = """\
/*
 * This module exports functions to be used by CCS:
 *	EXPORT_SYMBOL(not_exported);
 */
#include "foo.h"
#define FOO_DEV 0x10D3
#define module_param(name, type, perm) module_param_named(name, name, type, perm)

static int debug;
module_param(debug, int, 0644);
MODULE_PARM_DESC(debug, "Debug level (0-2)");
//module_param(disabled, int, 0);
static int __init foo_setup(char *str) { return 1; }
__setup("foo=", foo_setup);

int foo_read(void) { return 0; }
EXPORT_SYMBOL_GPL(foo_read);
EXPORT_SYMBOL_NS(foo_ns, "FOO_NS");

static const struct pci_device_id foo_ids[] = {
	{ PCI_VDEVICE(INTEL, FOO_DEV) },
	{ PCI_DEVICE(FOO_VENDOR, 0x0001) },
	{ }
};
MODULE_DEVICE_TABLE(pci, foo_ids);
/* MODULE_DEVICE_TABLE(pci, gone_ids); */

module_init(foo_init);
MODULE_LICENSE("GPL");
/*
MODULE_AUTHOR("Someone Commented Out");
*/
MODULE_DESCRIPTION("Foo " "driver");
"""
FOO_H = "#define FOO_VENDOR 0x1234\n"
SYS_C = "SYSCALL_DEFINE3(read, unsigned int, fd,\n\t\tchar __user *, buf, size_t, count)\n{\n}\nCOMPAT_SYSCALL_DEFINE1(old, int, x)\n{\n}\n"
TRACE_H = """\
#undef TRACE_SYSTEM
#define TRACE_SYSTEM foo

DECLARE_EVENT_CLASS(foo_class,
	TP_PROTO(int a),
	TP_ARGS(a),
	TP_STRUCT__entry(__field(int, a)),
	TP_fast_assign(__entry->a = a;),
	TP_printk("a=%d", __entry->a)
);
DEFINE_EVENT(foo_class, foo_start, TP_PROTO(int a), TP_ARGS(a));
"""
MEMCPY_S = "ENTRY(memcpy)\n\tret\nENDPROC(memcpy)\nEXPORT_SYMBOL(memcpy)\n"
PCI_IDS = "#define PCI_VENDOR_ID_INTEL 0x8086\n"
FILES = {"drivers/foo/foo.c": DRIVER_C, "drivers/foo/foo.h": FOO_H, "kernel/sys.c": SYS_C,
         "include/trace/events/foo.h": TRACE_H, "arch/x86/lib/memcpy_64.S": MEMCPY_S, "include/linux/pci_ids.h": PCI_IDS,
         "tools/foo.c": "module_param(tool_only, int, 0);\n"}


def release_cache_scope(root) -> None:
    """End a test's temporary cache scope on every engine: RAM spaces outlive ``close()``, so the scope is dropped;
    SQLite handles are closed before the directory goes."""
    get_cache_engine("ram").discard_scope(root)
    get_cache_engine("sqlite").close()


class TestExtract(unittest.TestCase):
    def test_driver(self) -> None:
        r = kf.extract(DRIVER_C)
        self.assertEqual(r["x"], [["foo_read", "gpl", "", 17], ["foo_ns", "ns", "FOO_NS", 18]], "not the one in a comment")
        self.assertEqual([(m, line, args) for m, line, _body, args in r["p"]],
                         [("module_param", 10, ["debug", "int", "0644"]),
                          ("MODULE_PARM_DESC", 11, ["debug", '"Debug level (0-2)"']),
                          ("__setup", 14, ['"foo="', "foo_setup"])],
                         "neither the #define nor the commented-out parameter")
        self.assertEqual([(m, line) for m, line, _a in r["m"]],
                         [("MODULE_DEVICE_TABLE", 25), ("module_init", 28), ("MODULE_LICENSE", 29), ("MODULE_DESCRIPTION", 33)])
        self.assertEqual(r["v"], [["pci", "foo_ids", 20, ["{ PCI_VDEVICE(INTEL, FOO_DEV) }", "{ PCI_DEVICE(FOO_VENDOR, 0x0001) }"]]])
        self.assertEqual(r["vi"], ["foo.h"])
        self.assertEqual(r["vd"]["FOO_DEV"], "0x10D3")
        self.assertNotIn("e", r)
        self.assertNotIn("s", r)

    def test_syscalls_tracepoints_and_assembly(self) -> None:
        self.assertEqual(kf.extract(SYS_C)["s"], [["SYSCALL_DEFINE3", 1, ["read", "unsigned int", "fd", "char __user *", "buf", "size_t", "count"]],
                                                  ["COMPAT_SYSCALL_DEFINE1", 5, ["old", "int", "x"]]])
        tr = kf.extract(TRACE_H)
        self.assertEqual(tr["ts"], "foo")
        self.assertEqual([(line, tp["macro"], tp["name"], tp["class"]) for line, tp in tr["e"]],
                         [(4, "DECLARE_EVENT_CLASS", "foo_class", ""), (11, "DEFINE_EVENT", "foo_start", "foo_class")])
        self.assertEqual(tr["e"][0][1]["fields"], [{"kind": "field", "type": "int", "name": "a", "size": ""}])
        self.assertEqual(kf.extract(MEMCPY_S), {"x": [["memcpy", "plain", "", 4]]})

    def test_empty_binary_and_encoding(self) -> None:
        self.assertEqual(kf.extract("int x;\n"), {})
        self.assertEqual(kf.extract("EXPORT_SYMBOL(x);\0\n"), {}, "binary files are skipped, as git grep -I")
        self.assertEqual(kf.encode({}), b"")
        self.assertEqual(kf.decode(b""), {})
        self.assertIsNone(kf.decode(None))
        self.assertIsNone(kf.decode(b"junk"))
        r = kf.extract(DRIVER_C)
        self.assertEqual(kf.decode(kf.encode(r)), r)
        self.assertEqual(kf.extract_many([("a", SYS_C)])[0][1], kf.extract(SYS_C))


class GitReader:
    """git_reader stand-in reading the test repository."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def read_file(self, sha: str, path: str):
        out = subprocess.run(["git", "-C", str(self.repo), "show", f"{sha}:{path}"], capture_output=True)
        return (out.stdout if out.returncode == 0 else None), False, None


class KconfigStub:
    def resolve_arch(self, rev: str, arch: str) -> dict:
        return {"arch": arch, "srcarch": "x86", "rev": rev}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestCatalogsFromRecords(unittest.TestCase):
    CACHE_ENGINE = "ram"   # pinned: the developer's config.json must not pick the engine under test
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp) / "repo"
        self.cache = Path(self.tmp) / "cache"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env)
        for path, text in FILES.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text)
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "x"], check=True, env=self.env)
        self.sha = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, env=self.env,
                                  capture_output=True, text=True).stdout.strip()
        self.store = si.FactStore("kapi", kf.KAPI_FORMAT, kf.encode, kf.decode)
        self.extracted: list[str] = []
        real = kf.extract_many

        def counting(items):
            self.extracted.extend(sha for sha, _ in items)
            return real(items)

        self.stored: dict[str, dict] = {}
        self.patches = [
            patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": str(self.cache), "KERNELINFO_CACHE_ENGINE": self.CACHE_ENGINE}),
            patch.object(si, "get_repo_dir", lambda: self.repo),
            patch.object(si, "_settings", lambda: {"workers": 1, "max_mb": 64, "revisions": 0}),
            patch.object(si, "db_facts", lambda shas, family, fmt, decoder: {s: self.stored[s] for s in shas if s in self.stored}),
            patch.object(k, "kapi_store", self.store),
            patch.object(k, "git_reader", GitReader(self.repo)),
            patch.object(kf, "extract_many", counting),
            patch.dict(k._REVISIONS, clear=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        release_cache_scope(self.cache)
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_records_database_first_then_local_store(self) -> None:
        listing = dict(si.revision_files(self.sha, k.SUFFIXES))
        self.assertIn("arch/x86/lib/memcpy_64.S", listing, "assembly is listed for the Kernel API")
        self.stored[listing["kernel/sys.c"]] = kf.extract(SYS_C)
        records = dict(k.revision_records(self.sha))
        self.assertEqual(set(records), {"drivers/foo/foo.c", "kernel/sys.c", "include/trace/events/foo.h",
                                        "arch/x86/lib/memcpy_64.S", "tools/foo.c"}, "files without macros are left out")
        self.assertNotIn(listing["kernel/sys.c"], self.extracted, "the database's record is not extracted again")
        self.assertEqual(len(self.extracted), len(FILES) - 1)
        k._REVISIONS.clear()
        k.revision_records(self.sha)
        self.assertEqual(len(self.extracted), len(FILES) - 1, "the local store answers the second time")

    def test_catalogs(self) -> None:
        ex = k.build_exports(self.sha)
        self.assertEqual([(e["symbol"], e["path"]) for e in ex["exports"]],
                         [("memcpy", "arch/x86/lib/memcpy_64.S"), ("foo_read", "drivers/foo/foo.c"), ("foo_ns", "drivers/foo/foo.c")])
        self.assertEqual(ex["counts"], {"plain": 1, "gpl": 1, "ns": 1})
        tp = k.build_tracepoints(self.sha)["tracepoints"]
        self.assertEqual([(t["name"], t["kind"], t["system"], t["proto"]) for t in tp],
                         [("foo_class", "class", "foo", "int a"), ("foo_start", "event", "foo", "int a")])
        self.assertEqual(tp[1]["fields"], tp[0]["fields"], "an event takes its class's fields when it has none")
        defs = k._syscall_defs(k.revision_records(self.sha))
        self.assertEqual(defs["sys_read"]["args"][0], {"type": "unsigned int", "name": "fd"})
        self.assertEqual(defs["compat_sys_old"]["macro"], "COMPAT_SYSCALL_DEFINE1")

        idx = {"arch": "x86_64", "exprs": [{"sym": "FOO"}], "files": {"drivers/foo/foo.c": [0]},
               "objects": [{"target": "drivers/foo/foo.o", "modname": "foo", "kind": "obj", "expr": 0,
                            "members": [{"path": "drivers/foo/foo.c", "expr": 0}]}]}
        with patch("webapp.backend.services.kconfig_service.KconfigService", KconfigStub), \
                patch.object(k, "_kbuild_files", lambda version, arch: idx):
            params = k.build_params(self.sha, "x86_64", "vX")["params"]
            mods = k.build_modules(self.sha, "x86_64", "vX")["modules"]
        self.assertEqual([(p["kind"], p["full_name"], p.get("description")) for p in params],
                         [("module", "foo.debug", "Debug level (0-2)"), ("setup", "foo", None)], "tools/ is left out")
        self.assertEqual(len(mods), 1)
        self.assertEqual((mods[0]["license"], mods[0]["description"], mods[0]["init"], mods[0]["authors"], mods[0]["options"]),
                         ("GPL", "Foo driver", "foo_init", [], ["FOO"]))
        self.assertEqual(mods[0]["device_tables"], [{"bus": "pci", "table": "foo_ids", "path": "drivers/foo/foo.c", "line": 25}])

        dev = dis.build_index(self.sha)
        self.assertEqual(dev["tables"], 1)
        self.assertEqual([(e["vendor"], e["device"], e["line"]) for e in dev["entries"]], [(0x8086, 0x10D3, 20), (0x1234, 0x0001, 20)],
                         "constants from the driver, its header and pci_ids.h")


class TestCatalogsFromRecordsSQLite(TestCatalogsFromRecords):
    CACHE_ENGINE = "sqlite"


if __name__ == "__main__":
    unittest.main()
