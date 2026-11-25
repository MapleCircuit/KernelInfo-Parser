"""tests/test_webapp_source_index.py - Source index: per-blob facts, revision assembly and its saved form (webapp/API.md section 2.19)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from cache_engine import get_cache_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import source_facts as sf
from webapp.backend.services import source_index as si

MAIN_C = """\
/* start_kernel() is called from head.S: a comment, not a call */
#include <linux/init.h>
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#define LIMIT 42
#define current_task get_current()

static noinline int helper(int x)
{
	if (x > LIMIT)
		return sizeof(x);
	return do_work(x, "printk(not a call)");
}

int __init setup(void) __acquires(lock)
{
	helper(1);
	ops->read(file, buf);
	dev . write (dev);
	return shared(helper(2));
}

#ifdef CONFIG_FOO
static int twice(int a)
{
#else
static int twice(int a, int b)
{
#endif
	return shared(a);
}

SYSCALL_DEFINE3(open, const char __user *, filename, int, flags, umode_t, mode)
{
	return do_sys_open(AT_FDCWD, filename, flags, mode);
}
"""

TABLES_C = """\
/* struct file_operations fake = { .read = nope }; */
static const struct file_operations foo_fops = {
	.owner		= THIS_MODULE,
	.read		= foo_read,	/* the reader, { tricky } */
	.write		= &foo_write,
	.ioctl		= foo_ioctl(1),
	.name		= "a, b {",
	.nested		= { .read = inner_read },
};

struct file_operations arr_fops[2] __read_mostly __aligned(sizeof(long)) = {
	{ .read = arr_read0, .llseek = no_llseek },
	[1] = { .read = arr_read1 },
};

static struct file_operations positional_fops = { pos_open, pos_read };

static ssize_t foo_read(struct file *f) { return 0; }
ssize_t foo_write(struct file *f) { return 0; }
ssize_t arr_read0(struct file *f) { return 0; }
ssize_t arr_read1(struct file *f) { return 0; }
ssize_t inner_read(struct file *f) { return 0; }
"""


def defs_of(facts: dict) -> dict:
    return {d[0]: d for d in facts["d"]}


def calls_of(facts: dict, name: str) -> list:
    idx = [i for i, d in enumerate(facts["d"]) if d[0] == name]
    return [(c[1], c[2]) for c in facts["c"] if c[0] in idx]


def release_cache_scope(root) -> None:
    """End a test's temporary cache scope on every engine: RAM spaces outlive ``close()``, so the scope is dropped;
    SQLite handles are closed before the directory goes."""
    get_cache_engine("ram").discard_scope(root)
    get_cache_engine("sqlite").close()


class TestFacts(unittest.TestCase):
    def setUp(self) -> None:
        self.f = sf.extract(MAIN_C)

    def test_functions_and_scope(self) -> None:
        d = defs_of(self.f)
        self.assertEqual(d["helper"][1:], [7, 12, sf.F_STATIC, "int"])
        self.assertEqual(d["setup"][1:], [14, 20, 0, "int"], "annotations after the parameters are skipped")
        self.assertEqual(d["sys_open"][3:], [0, "long"], "SYSCALL_DEFINEn(open, ...) defines sys_open")
        self.assertEqual(d["twice"][3], sf.F_STATIC, "a head written twice under #ifdef/#else")
        self.assertNotIn("LIMIT", d, "a constant macro calls nothing")
        self.assertEqual(d["pr_fmt"][3], sf.F_MACRO_FN)
        self.assertEqual(d["current_task"][3], sf.F_MACRO_OBJ, "an object-like macro that calls something")
        self.assertEqual(d["pr_fmt"][4], "", "macros have no return type")

    def test_calls_members_and_noise(self) -> None:
        self.assertEqual(calls_of(self.f, "helper"), [("do_work", 11)], "keywords, sizeof and strings are not calls")
        self.assertEqual(calls_of(self.f, "setup"), [("helper", 16), ("shared", 19), ("helper", 19)])
        self.assertEqual(calls_of(self.f, "twice"), [("shared", 29)], "the #else head does not unbalance the braces")
        self.assertEqual(calls_of(self.f, "sys_open"), [("do_sys_open", 34)])
        self.assertEqual(calls_of(self.f, "current_task"), [("get_current", 5)])
        members = [(m[1], m[2]) for m in self.f["m"]]
        self.assertEqual(members, [("read", 17), ("write", 18)])
        self.assertNotIn("start_kernel", [c[1] for c in self.f["c"]], "comments are masked")

    def test_tables(self) -> None:
        t = {row[1]: row for row in sf.extract(TABLES_C)["t"]}
        self.assertEqual(sorted(t), ["arr_fops", "foo_fops", "positional_fops"], "the commented-out table is not one")
        foo = t["foo_fops"]
        self.assertEqual(foo[0], "file_operations")
        self.assertEqual(foo[2], 2)
        self.assertEqual([(e[0], e[1], e[2]) for e in foo[4]],
                         [("owner", "THIS_MODULE", 3), ("read", "foo_read", 4), ("write", "foo_write", 5), ("nested.read", "inner_read", 8)])
        self.assertEqual([(e[0], e[1]) for e in t["arr_fops"][4]], [("read", "arr_read0"), ("llseek", "no_llseek"), ("read", "arr_read1")])
        self.assertEqual(t["positional_fops"][3], 2)

    def test_special_function_macros_and_assignments(self) -> None:
        src = """\
ARCH_SYSCALL_DEFINE1(mmap2, unsigned long, addr)
{
    ops->open = __devinit_p(foo_open);
    return do_mmap2(addr);
}

TRACE_EVENT(sched_switch,
    TP_PROTO(struct task_struct *prev),
    TP_ARGS(prev)
)
{
    return 0;
}

static struct file_operations const extra_ops = {
    .read = __devinit_p(extra_read),
    .write = (void *)extra_write,
};
"""
        facts = sf.extract(src)
        d = defs_of(facts)
        self.assertIn("arch_sys_mmap2", d)
        self.assertIn("trace_sched_switch", d)

        # Dynamic assignment
        self.assertEqual(len(facts.get("a", [])), 1)
        assign = facts["a"][0]
        self.assertEqual(assign[:3], ["ops", "open", "foo_open"])

        # Tables with qualifier and unwrapped macros
        t = {row[1]: row for row in facts["t"]}
        self.assertIn("extra_ops", t)
        self.assertEqual(t["extra_ops"][0], "file_operations")
        entries = {e[0]: e[1] for e in t["extra_ops"][4]}
        self.assertEqual(entries.get("read"), "extra_read")
        self.assertEqual(entries.get("write"), "extra_write")


RETURN_TYPES_C = """\
struct pci_dev *pci_get_bus_and_slot(unsigned int bus, unsigned int devfn)
{
	return NULL;
}

static int
foo(void)
{
	return 0;
}

static inline unsigned long __must_check bar(const void __user *p, unsigned long n)
{
	return n;
}

SYSCALL_DEFINE5(pciconfig_read, unsigned long, bus, unsigned long, dfn,
		unsigned long, off, unsigned long, len, void __user *, buf)
{
	return 0;
}

module_init(foo)
asmlinkage NORET_TYPE void __attribute__((noreturn)) do_exit(long code)
{
}

BPF_CALL_2(bpf_lookup, struct bpf_map *, map, void *, key)
{
	return 0;
}
"""


class TestReturnTypes(unittest.TestCase):
    """`d` rows carry the declared return type (format 2), from the logic `c_scope.parse_signature` shares."""

    def test_return_types(self) -> None:
        d = defs_of(sf.extract(RETURN_TYPES_C))
        self.assertEqual(sf.FACTS_FORMAT, 2)
        self.assertEqual({name: row[4] for name, row in d.items()}, {
            "pci_get_bus_and_slot": "struct pci_dev *", "foo": "int", "bar": "unsigned long",
            "sys_pciconfig_read": "long", "do_exit": "void", "bpf_lookup": "u64"})
        self.assertEqual(d["foo"][1], 7, "the line of the name in a multi-line head")

    def test_shared_with_parse_signature(self) -> None:
        from parser.c_ast.c_scope import parse_signature
        for head, want in (("struct pci_dev *pci_get_bus_and_slot(unsigned int bus, unsigned int devfn)", "struct pci_dev *"),
                           ("static int\nfoo(void)", "int"),
                           ("static inline unsigned long __must_check foo(int a)", "unsigned long"),
                           (("SYSCALL_DEFINE5(pciconfig_read, unsigned long, bus, unsigned long, dfn, unsigned long, off, "
                             "unsigned long, len, void __user *, buf)"), "long")):
            found = sf.function_name(sf.mask(head))
            self.assertEqual(sf.return_type(sf.mask(head), found[1]), want)
            self.assertEqual(parse_signature(head)["return_type"], want)

    def test_type_text(self) -> None:
        self.assertEqual(sf.type_text(["struct", "pci_dev", "*"]), "struct pci_dev *")
        self.assertEqual(sf.type_text(["const", "char", "*", "const"]), "const char *const")
        self.assertEqual(sf.type_text(["char", "*", "*"]), "char **")
        self.assertEqual(sf.type_text(["void", "(", "*", ")", "(", "int", ",", "long", ")"]), "void (*)(int, long)")
        self.assertEqual(sf.type_text(["int", "[", "4", "]"]), "int [4]")


class TestFactStore(unittest.TestCase):
    CACHE_ENGINE = "ram"   # pinned: the developer's config.json must not pick the engine under test

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": self.tmp, "KERNELINFO_CACHE_ENGINE": self.CACHE_ENGINE})
        self.env.start()
        self.store = si.FactStore()

    def tearDown(self) -> None:
        release_cache_scope(self.tmp)
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_missing_and_prune(self) -> None:
        a, b = "a" * 40, "b" * 40
        self.store.put_many([(a, {"d": [["f", 1, 2, 0]], "c": [], "m": [], "t": []})])
        self.assertEqual(self.store.missing([a, b]), [b])
        self.assertEqual(self.store.get_many([a, b]), {a: {"d": [["f", 1, 2, 0]], "c": [], "m": [], "t": []}})
        self.store.put_many([(b, {"d": [], "c": [["x", 1, 2]] * 50, "m": [], "t": []})])
        self.assertEqual(self.store.count(), 2)
        self.assertGreater(self.store.prune(1), 0)
        self.assertLess(self.store.count(), 2)


    def test_older_formats_are_discarded(self) -> None:
        older = si.FactStore(fmt=sf.FACTS_FORMAT - 1)
        older.put_many([("a" * 40, {"d": [["f", 1, 2, 0]], "c": [], "m": [], "t": []})])
        self.assertEqual(self.store.retire_older(), 1)
        self.assertEqual(self.store.retire_older(), 0, "nothing left to discard")
        self.assertEqual(older.count(), 0)


class TestFactStoreSQLite(TestFactStore):
    CACHE_ENGINE = "sqlite"


def load_from(files: dict[str, str]):
    """(files [(path, blob)], load) for in-memory sources: the blob id is the path's hash."""
    import hashlib
    blobs = {hashlib.sha1(text.encode()).hexdigest(): text for text in files.values()}
    order = [(path, hashlib.sha1(text.encode()).hexdigest()) for path, text in files.items()]
    facts = {sha: sf.extract(text) for sha, text in blobs.items()}
    return order, lambda shas: {s: facts[s] for s in shas if s in facts}


SOURCES = {
    "kernel/a.c": "static void helper(void) { shared(); }\nvoid entry(void) { helper(); asm_only(); }\n",
    "drivers/b.c": "static void helper(void) { b_only(); }\nvoid b_entry(void) { helper(); shared(); }\n",
    "lib/shared.c": "void shared(void) { leaf(); }\nvoid leaf(void) { }\n",
    "arch/arm/kernel/c.c": "void leaf2(void) { shared(); }\n",
    "fs/ops.c": TABLES_C + "ssize_t vfs_read(struct file *f) { return f->f_op->read(f); }\n",
}


class TestRevisionIndex(unittest.TestCase):
    def setUp(self) -> None:
        files, load = load_from(SOURCES)
        self.idx = si.RevisionIndex("0" * 40, files, load)

    def names(self, edges):
        return sorted((self.idx.name_of(n), self.idx.paths[p], via) for n, p, _line, via in edges)

    def test_static_functions_bind_per_file(self) -> None:
        idx = self.idx
        helpers = idx.nodes_named("helper")
        self.assertEqual(sorted(idx.file_of(n) for n in helpers), ["drivers/b.c", "kernel/a.c"])
        entry = idx.nodes_named("entry")[0]
        self.assertEqual(self.names(idx.callees(entry)), [("asm_only", "kernel/a.c", ""), ("helper", "kernel/a.c", "")])
        a_helper = next(n for n in helpers if idx.file_of(n) == "kernel/a.c")
        self.assertEqual(self.names(idx.callers(a_helper)), [("entry", "kernel/a.c", "")], "b.c's call binds to b.c's helper")
        self.assertEqual(idx.definitions(idx.nodes_named("asm_only")[0]), [], "a callee without a C definition")

    def test_arch_filter(self) -> None:
        idx = self.idx
        shared = idx.nodes_named("shared")[0]
        everyone = self.names(idx.callers(shared))
        self.assertIn(("leaf2", "arch/arm/kernel/c.c", ""), everyone)
        allowed = idx.allowed("x86_64", lambda p: not p.startswith("arch/arm/"))
        self.assertNotIn(("leaf2", "arch/arm/kernel/c.c", ""), self.names(idx.callers(shared, allowed)))

    def test_ops_tables_and_indirect_edges(self) -> None:
        idx = self.idx
        r = idx.struct_report("file_operations")
        self.assertEqual((r["tables"], r["files"], r["positional_skipped"]), (3, 1, 2))
        self.assertEqual([(i["function"], i["table"]["name"]) for i in r["fields"]["read"]],
                         [("foo_read", "foo_fops"), ("arr_read0", "arr_fops"), ("arr_read1", "arr_fops")])
        self.assertNotIn("owner", r["fields"], "THIS_MODULE is no function")
        self.assertEqual([i["function"] for i in r["fields"]["nested.read"]], ["inner_read"])
        self.assertEqual([(u["table"], u["field"]) for u in idx.uses_of("foo_write")], [("foo_fops", "write")])
        vfs_read = idx.nodes_named("vfs_read")[0]
        self.assertEqual(self.names(idx.callees(vfs_read)), [])
        indirect = self.names(idx.callees(vfs_read, indirect=True))
        self.assertIn(("foo_read", "fs/ops.c", "file_operations.read"), indirect)
        self.assertIn(("inner_read", "fs/ops.c", "file_operations.nested.read"), indirect)
        self.assertEqual(self.names(idx.callers(idx.nodes_named("arr_read1")[0], indirect=True)), [("vfs_read", "fs/ops.c", "file_operations.read")])

    def test_return_types(self) -> None:
        idx = self.idx
        self.assertEqual(idx.return_type("vfs_read"), ["ssize_t"])
        self.assertEqual(idx.return_type("helper"), ["void"], "both static helpers return void")
        self.assertEqual(idx.return_type("asm_only"), [], "no definition")
        self.assertEqual([d["return_type"] for d in idx.definitions(idx.nodes_named("shared")[0])], ["void"])

    def test_format_1_facts_have_unknown_return_types(self) -> None:
        """Stored rows of format 1 (four-element `d` rows) still assemble; their return types are unknown."""
        files, load = load_from(SOURCES)

        def load_v1(shas):
            out = {}
            for sha, facts in load(shas).items():
                old = dict(facts)
                old["d"] = [row[:4] for row in facts["d"]]
                if sha == files[0][1]:  # kernel/a.c stays format 2
                    old = facts
                out[sha] = old
            return out

        idx = si.RevisionIndex("0" * 40, files, load_v1)
        self.assertEqual(idx.summary()["calls"], self.idx.summary()["calls"])
        self.assertEqual(idx.return_type("shared"), [])
        self.assertEqual(idx.definitions(idx.nodes_named("shared")[0])[0]["return_type"], None)
        self.assertEqual(idx.return_type("entry"), ["void"], "a format 2 file in the same revision")
        back = si.RevisionIndex.from_bytes(idx.to_bytes())
        self.assertEqual(back.return_type("entry"), ["void"])
        self.assertEqual(back.return_type("shared"), [])

    def test_saved_form_round_trip(self) -> None:
        idx = self.idx
        back = si.RevisionIndex.from_bytes(idx.to_bytes())
        self.assertIsNotNone(back)
        self.assertEqual(back.summary(), idx.summary())
        for node in range(len(idx.node_name)):
            self.assertEqual(self.names(back.callees(node, indirect=True)), self.names(idx.callees(node, indirect=True)))
            self.assertEqual(self.names(back.callers(node, indirect=True)), self.names(idx.callers(node, indirect=True)))
            self.assertEqual(back.definitions(node), idx.definitions(node))
        self.assertEqual(back.struct_report("file_operations"), idx.struct_report("file_operations"))
        self.assertEqual(back.uses_of("foo_write"), idx.uses_of("foo_write"))
        self.assertEqual(back.nodes_named("helper"), idx.nodes_named("helper"))
        self.assertEqual(back.return_type("vfs_read"), ["ssize_t"])
        self.assertIsNone(si.RevisionIndex.from_bytes(b"not an index"))


class TestDatabaseFacts(unittest.TestCase):
    """The parser's `m_blob_facts` rows of family 1 are read in format 1 and 2 (no whole-tree re-extraction)."""

    def setUp(self) -> None:
        from core.DBLayout import m_blob_facts
        from tests.db_fixtures import make_db, patch_reader
        self.v1, self.v2, self.other = "1" * 40, "2" * 40, "3" * 40
        old = sf.extract("void a(void) { b(); }\n")
        old["d"] = [row[:4] for row in old["d"]]
        rows = [{"blob_hash": bytes.fromhex(self.v1), "family": sf.FAMILY, "format": 1, "data": sf.encode(old)},
                {"blob_hash": bytes.fromhex(self.v2), "family": sf.FAMILY, "format": 2,
                 "data": sf.encode(sf.extract("int c(void) { return d(); }\n"))},
                {"blob_hash": bytes.fromhex(self.other), "family": sf.FAMILY, "format": 0, "data": sf.encode(None)}]
        self.db = make_db("sqlite", tables=[m_blob_facts], rows={"m_blob_facts": rows})
        patcher = patch_reader(self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_readable_formats(self) -> None:
        shas = [self.v1, self.v2, self.other]
        self.assertEqual(si.db_has(shas), {self.v1, self.v2}, "format 1 rows count, unknown formats do not")
        facts = si.db_facts(shas)
        self.assertEqual(facts[self.v1]["d"][0][:4], ["a", 1, 1, 0])
        self.assertEqual(facts[self.v2]["d"][0][4], "int")
        self.assertEqual(si.db_has(shas, sf.FAMILY, 2), {self.v2}, "an explicit single format")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestBuildFromGit(unittest.TestCase):
    env: ClassVar[dict[str, str]] = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                                     "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def commit(self, repo: str) -> str:
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "x"], check=True, env=self.env)
        return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True, env=self.env, capture_output=True, text=True).stdout.strip()

    def test_only_new_blobs_are_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
            repo = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True, env=self.env)
            for path, text in SOURCES.items():
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text(text)
            (repo / "README").write_text("not C")
            first = self.commit(tmp)
            (repo / "lib" / "shared.c").write_text("void shared(void) { leaf(); extra(); }\nvoid leaf(void) { }\n")
            second = self.commit(tmp)
            extracted = []
            real = si.extract_many

            def counting(items):
                extracted.extend(sha for sha, _ in items)
                return real(items)

            store = si.FactStore()
            with patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": cache, "KERNELINFO_CACHE_ENGINE": "ram"}), \
                    patch.object(si, "get_repo_dir", lambda: repo), \
                    patch.object(si, "fact_store", store), patch.object(si, "extract_many", counting), \
                    patch.object(si, "db_has", lambda shas: set()), patch.object(si, "db_facts", lambda shas: {}):
                one = si.build_revision(first, workers=1)
                self.assertEqual(len(extracted), len(SOURCES))
                two = si.build_revision(second, workers=1)
                self.assertEqual(len(extracted), len(SOURCES) + 1, "only the changed file is read again")
                again = si.build_revision(second, workers=1)
                self.assertEqual(len(extracted), len(SOURCES) + 1, "a saved revision is read back, not assembled")
                self.assertEqual(again.summary(), two.summary())
                release_cache_scope(cache)
            self.assertEqual(len(one.paths), len(SOURCES))
            shared = two.nodes_named("shared")[0]
            self.assertIn("extra", [two.name_of(n) for n, _p, _l, _v in two.callees(shared)])

    def test_parser_facts_are_used_first(self) -> None:
        """Blobs the parser stored (m_blob_facts) are neither read nor extracted by the webapp."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
            repo = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True, env=self.env)
            for path, text in SOURCES.items():
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text(text)
            head = self.commit(tmp)
            with patch.object(si, "get_repo_dir", lambda: repo):
                listing = si.revision_files(head)
            stored = {sha: sf.extract(SOURCES[path]) for path, sha in listing if path != "fs/ops.c"}
            extracted = []
            real = si.extract_many

            def counting(items):
                extracted.extend(sha for sha, _ in items)
                return real(items)

            store = si.FactStore()
            with patch.dict(os.environ, {"KERNELINFO_CACHE_DIR": cache, "KERNELINFO_CACHE_ENGINE": "ram"}), \
                    patch.object(si, "get_repo_dir", lambda: repo), \
                    patch.object(si, "fact_store", store), patch.object(si, "extract_many", counting), \
                    patch.object(si, "db_has", lambda shas: {s for s in shas if s in stored}), \
                    patch.object(si, "db_facts", lambda shas: {s: stored[s] for s in shas if s in stored}):
                idx = si.build_revision(head, workers=1)
                release_cache_scope(cache)
            self.assertEqual(len(extracted), 1, "only the blob the database lacks is extracted")
            self.assertEqual(idx.struct_report("file_operations")["tables"], 3)
            self.assertEqual(len(idx.nodes_named("helper")), 2)

    def test_read_blobs_drains_non_blob_entries(self) -> None:
        import io

        t_sha = "1" * 40
        b_sha = "2" * 40
        stream = io.BytesIO(
            f"{t_sha} tree 4\nTREE\n{b_sha} blob 7\nint x;\n\n".encode("ascii")
        )

        class FakeProc:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = stream

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                pass

        with patch.object(si.subprocess, "Popen", return_value=FakeProc()):
            got = list(si.read_blobs([t_sha, b_sha]))
        self.assertEqual(got, [(t_sha, None), (b_sha, "int x;\n")])


if __name__ == "__main__":
    unittest.main()
