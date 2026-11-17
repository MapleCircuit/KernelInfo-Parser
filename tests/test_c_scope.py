"""tests/test_c_scope.py - Lexical scopes, variables, uses and struct fields of C files (`parser/c_ast/c_scope.py`,
`parser/c_ast/CAST_API.md` section 14)."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser.c_ast import c_scope as cs

FIXTURES = Path(__file__).parent / "fixtures" / "c_scope"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("latin-1")


def var(model: dict, name: str, line: int | None = None, fn: str | None = None) -> int:
    """Index of the variable `name` (declared on `line` / in function `fn`); exactly one must match."""
    found = []
    for i, v in enumerate(model["vars"]):
        if v["name"] != name or (line is not None and v["line"] != line):
            continue
        if fn is not None and (v["fn"] is None or model["functions"][v["fn"]]["name"] != fn):
            continue
        found.append(i)
    assert len(found) == 1, f"{name}: {found}"
    return found[0]


def uses(model: dict, v: int) -> list[tuple[int, str]]:
    return [(u[1], u[3]) for u in model["uses"] if u[0] == v]


def members(model: dict, v: int) -> list[tuple[int, list[str], str, str]]:
    return [(m[1], m[3], m[4], m[5]) for m in model["members"] if m[0] == v]


def col_of(text: str, line: int, token: str, nth: int = 0) -> int:
    """1-based column of the `nth` occurrence of `token` on `line`."""
    row = text.split("\n")[line - 1]
    at = -1
    for _ in range(nth + 1):
        at = row.index(token, at + 1)
    return at + 1


class TestPciSyscall(unittest.TestCase):
    """Linux v3.0 `drivers/pci/syscall.c`: two `SYSCALL_DEFINE5` functions with the same local names."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = read_fixture("pci_syscall_v3.0.c")
        cls.m = cs.extract_scopes(cls.text)

    def fn(self, name: str) -> dict:
        return next(f for f in self.m["functions"] if f["name"] == name)

    def test_functions(self) -> None:
        self.assertEqual(self.m["format"], cs.SCOPE_FORMAT)
        w = self.fn("sys_pciconfig_write")
        self.assertEqual((w["line_s"], w["line_e"], w["return_type"], w["macro"]), (86, 136, "long", "SYSCALL_DEFINE5"))
        self.assertEqual(w["body"], [88, 1, 136, 1])
        r = self.fn("sys_pciconfig_read")
        self.assertEqual((r["line_s"], r["line_e"]), (16, 84))
        self.assertEqual([p["name"] for p in self.m["protos"]], ["sys_pciconfig_read", "sys_pciconfig_write"])

    def test_params(self) -> None:
        w = self.fn("sys_pciconfig_write")
        params = [self.m["vars"][v] for v in w["params"]]
        self.assertEqual([(p["name"], p["type"], p["kind"]) for p in params],
                         [("bus", "unsigned long", "param"), ("dfn", "unsigned long", "param"),
                          ("off", "unsigned long", "param"), ("len", "unsigned long", "param"),
                          ("buf", "void __user *", "param")])
        self.assertEqual(params[0]["base"], {"kind": "builtin", "name": "unsigned long"})
        self.assertEqual((params[4]["pointer"], params[4]["base"]), (1, {"kind": "builtin", "name": "void"}))

    def test_locals(self) -> None:
        m = self.m
        dev = m["vars"][var(m, "dev", fn="sys_pciconfig_write")]
        self.assertEqual((dev["type"], dev["base"], dev["pointer"], dev["line"], dev["kind"]),
                         ("struct pci_dev *", {"kind": "struct", "name": "pci_dev"}, 1, 89, "local"))
        for name, typ in (("byte", "u8"), ("word", "u16"), ("dword", "u32")):
            v = m["vars"][var(m, name, fn="sys_pciconfig_write")]
            self.assertEqual((v["type"], v["base"]), (typ, {"kind": "typedef", "name": typ}))
        err = m["vars"][var(m, "err", fn="sys_pciconfig_write")]
        self.assertEqual((err["type"], err["init"]), ("int", [93, col_of(self.text, 93, "0")]))
        self.assertEqual(m["vars"][var(m, "err", fn="sys_pciconfig_read")]["type"], "long")

    def test_uses(self) -> None:
        m = self.m
        dev = var(m, "dev", fn="sys_pciconfig_write")
        self.assertEqual(uses(m, dev), [(89, "decl"), (98, "w"), (99, "r"), (107, "r"), (116, "r"), (125, "r"), (134, "r")])
        self.assertEqual(uses(m, var(m, "byte", fn="sys_pciconfig_write")), [(90, "decl"), (104, "w"), (107, "r")],
                         "get_user() writes its first argument")
        self.assertEqual(uses(m, var(m, "word", fn="sys_pciconfig_write")), [(91, "decl"), (113, "w"), (116, "r")])
        err = uses(m, var(m, "err", fn="sys_pciconfig_write"))
        self.assertEqual([ln for ln, a in err if a == "w"], [104, 107, 109, 113, 116, 118, 122, 125, 127, 131])
        self.assertEqual([ln for ln, a in err if a == "r"], [105, 108, 114, 117, 123, 126, 135])
        self.assertIn([dev, 98, 2, "pci_get_bus_and_slot"], m["assigns"])

    def test_functions_do_not_share_locals(self) -> None:
        m = self.m
        w_dev, r_dev = var(m, "dev", fn="sys_pciconfig_write"), var(m, "dev", fn="sys_pciconfig_read")
        self.assertFalse([u for u in m["uses"] if u[0] == w_dev and 16 <= u[1] <= 84])
        self.assertFalse([u for u in m["uses"] if u[0] == r_dev and 86 <= u[1] <= 136])
        self.assertEqual(uses(m, var(m, "byte", fn="sys_pciconfig_read")), [(20, "decl"), (36, "addr"), (55, "r")])

    def test_lookup(self) -> None:
        m = self.m
        found = cs.lookup(m, 98, 2)
        dev = var(m, "dev", fn="sys_pciconfig_write")
        self.assertEqual(found, {"kind": "var", "var": dev, "access": "w",
                                 "uses": [[89, 18, "decl"], [98, 2, "w"], [99, 7, "r"], [107, 36, "r"], [116, 36, "r"],
                                          [125, 37, "r"], [134, 14, "r"]]})
        self.assertEqual(cs.lookup(m, 98, 4), found, "any column inside the token")
        self.assertEqual(cs.lookup(m, 98, 1, "dev"), found, "by name on the line")
        self.assertIsNone(cs.lookup(m, 67, 1), "the label `error`")
        self.assertIsNone(cs.lookup(m, 32, col_of(self.text, 32, "error")), "goto error")
        self.assertIsNone(cs.lookup(m, 98, col_of(self.text, 98, "pci_get_bus_and_slot")))
        self.assertIsNone(cs.lookup(m, 108, col_of(self.text, 108, "PCIBIOS_SUCCESSFUL")))
        self.assertIsNone(cs.lookup(m, 108, 1, "PCIBIOS_SUCCESSFUL"))

    def test_json_serialisable(self) -> None:
        self.assertEqual(json.loads(json.dumps(self.m)), self.m)


SCOPES_C = """\
static int counter;
int total = 3;
static struct foo bar = { .a = 1, .b = &total };
static DEFINE_SPINLOCK(lock);
DEFINE_PER_CPU(struct rq *, runqueues);
extern int ext_fn(int a, char *b);
extern int ext_var;
typedef unsigned long ulong_t;
struct point { int x, y; } origin;
static int helper(int x, void (*fn)(int), const char *const name, ...);

static int helper(int x, void (*fn)(int), const char *const name, ...)
{
	int a, *b = NULL, c[4];
	static int calls;
	int (*cb)(int);
	struct item arr[4];
	struct pt s;
	/* x = 5; a comment naming a */
	printk("a = %d", x);
	if (x == 1) {
		int x = 2;
		x++;
	}
	for (int i = 0; i < 4; i++)
		c[i] = i;
	a = b = 0;
	a += x;
	--a;
	a--;
	*b = 3;
	a = sizeof(x) + sizeof(struct foo);
	b = (int *)kmalloc(sizeof(*b), GFP_KERNEL);
	cb = fn;
	cb(x);
	counter = total == 3 || a <= 1 || a >= 2 || a != 4;
	calls++;
	ext_var = 1;
	n = ({ int t = a; t + 1; });
	return a + origin.x;
}

int user(void)
{
	return counter + helper;
}
"""


class TestScopes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = cs.extract_scopes(SCOPES_C)

    def test_file_scope(self) -> None:
        m = self.m
        files = {v["name"]: v for v in m["vars"] if v["kind"] == "file"}
        self.assertEqual(sorted(files), ["bar", "counter", "ext_var", "lock", "origin", "runqueues", "total"],
                         "functions, typedefs, prototypes and extern functions are no variables")
        self.assertEqual((files["lock"]["type"], files["lock"]["macro"]), ("spinlock_t", "DEFINE_SPINLOCK"))
        self.assertEqual((files["runqueues"]["type"], files["runqueues"]["macro"]), ("struct rq *", "DEFINE_PER_CPU"))
        self.assertEqual(files["origin"]["type"], "struct point")
        self.assertEqual(files["total"]["init"], [2, 13])
        self.assertEqual(uses(m, var(m, "total")), [(2, "decl"), (3, "addr"), (36, "r")])
        self.assertEqual(uses(m, var(m, "counter")), [(1, "decl"), (36, "w"), (45, "r")])
        self.assertEqual(uses(m, var(m, "ext_var")), [(7, "decl"), (38, "w")])
        self.assertEqual({n: v["extern"] for n, v in files.items()}["ext_var"], True)
        self.assertEqual(sorted(n for n, v in files.items() if v["extern"]), ["ext_var"],
                         "only `extern` declarations are flagged: they declare a variable defined elsewhere")
        blk = cs.extract_scopes("int f(void)\n{\n\textern int blk;\n\tint own;\n\treturn blk + own;\n}\n")
        self.assertEqual({v["name"]: (v["kind"], v["extern"]) for v in blk["vars"]},
                         {"blk": ("local", True), "own": ("local", False)})
        self.assertEqual(m["scopes"][0]["kind"], "file")

    def test_multi_declarators_and_kinds(self) -> None:
        m = self.m
        types = {m["vars"][var(m, n, fn="helper")]["name"]: m["vars"][var(m, n, fn="helper")]
                 for n in ("a", "b", "c", "calls", "cb", "arr", "s")}
        self.assertEqual(types["a"]["type"], "int")
        self.assertEqual((types["b"]["type"], types["b"]["pointer"], types["b"]["init"]), ("int *", 1, [14, 14]))
        self.assertEqual((types["c"]["type"], types["c"]["array"]), ("int [4]", True))
        self.assertEqual(types["calls"]["kind"], "static_local")
        self.assertEqual((types["cb"]["type"], types["cb"]["base"], types["cb"]["pointer"]), ("int (*)(int)", None, 1))
        self.assertEqual(types["arr"]["base"], {"kind": "struct", "name": "item"})
        fn = m["vars"][var(m, "fn")]
        self.assertEqual((fn["kind"], fn["type"]), ("param", "void (*)(int)"))
        self.assertEqual(m["vars"][var(m, "name")]["type"], "const char *const")
        helper = next(f for f in m["functions"] if f["name"] == "helper")
        self.assertEqual([m["vars"][v]["name"] for v in helper["params"]], ["x", "fn", "name"], "`...` is no variable")
        proto = next(p for p in m["protos"] if p["name"] == "helper")
        self.assertEqual(proto["params"][-1], {"name": "...", "type": "..."})
        self.assertEqual(next(p for p in m["protos"] if p["name"] == "ext_fn")["params"],
                         [{"name": "a", "type": "int"}, {"name": "b", "type": "char *"}])

    def test_shadowing_and_for_scopes(self) -> None:
        m = self.m
        outer, inner = var(m, "x", line=12), var(m, "x", line=22)
        self.assertEqual(uses(m, inner), [(22, "decl"), (23, "rw")])
        self.assertIn((21, "r"), uses(m, outer))
        self.assertNotIn(23, [ln for ln, _a in uses(m, outer)])
        i = m["vars"][var(m, "i")]
        scope = m["scopes"][i["scope"]]
        self.assertEqual((scope["kind"], scope["span"][0], scope["span"][2]), ("for", 25, 26))
        self.assertEqual(uses(m, var(m, "i")), [(25, "decl"), (25, "r"), (25, "rw"), (26, "r"), (26, "r")])
        self.assertEqual(uses(m, var(m, "c")), [(14, "decl"), (26, "w")], "an element of a local array is written")
        t = m["vars"][var(m, "t")]
        self.assertEqual(m["scopes"][t["scope"]]["kind"], "block", "a statement expression is a block")

    def test_access(self) -> None:
        m = self.m
        a = uses(m, var(m, "a", fn="helper"))
        self.assertEqual(a, [(14, "decl"), (27, "w"), (28, "rw"), (29, "rw"), (30, "rw"), (32, "w"), (36, "r"), (36, "r"),
                             (36, "r"), (39, "r"), (40, "r")], "comments and strings are no uses; == <= >= != read")
        self.assertEqual(uses(m, var(m, "b", fn="helper")), [(14, "decl"), (27, "w"), (31, "r"), (33, "w"), (33, "r")],
                         "a = b = 0 writes both; *b = 3 reads b")
        self.assertEqual(uses(m, var(m, "cb")), [(16, "decl"), (34, "w"), (35, "r")])
        self.assertIn([var(m, "b", fn="helper"), 33, 2, "kmalloc"], m["assigns"], "an assignment through a cast")
        self.assertEqual(uses(m, var(m, "calls")), [(15, "decl"), (37, "rw")])
        self.assertEqual(uses(m, var(m, "origin")), [(9, "decl"), (40, "r")])
        self.assertEqual(members(m, var(m, "origin")), [(40, ["x"], ".", "r")])
        self.assertIn((32, "r"), uses(m, var(m, "x", line=12)), "sizeof(x) reads x; sizeof(struct foo) uses no bar")

    def test_function_names_are_no_uses(self) -> None:
        m = self.m
        self.assertFalse([u for u in m["uses"] if u[1] == 45 and m["vars"][u[0]]["name"] == "helper"])


ACCESS_C = """\
int f(struct dev *dev, int len, int *p)
{
	struct pt s;
	struct item arr[4];
	u32 lo, hi;
	u64 n;
	unsigned long flags;
	int v, i, w;
	struct list_head *pos, *tmp;

	dev->bus->number = 0;
	v = dev->devfn;
	dev->refcnt++;
	foo(&dev->dev);
	s.field = 1;
	arr[i].x += 2;
	s.a.b++;
	foo(&s.field);
	rdmsr(MSR_X, lo, hi);
	rdmsrl(MSR_Y, n);
	__get_user(w, p);
	get_user(v, p);
	do_div(n, 10);
	swap(lo, hi);
	spin_lock_irqsave(&dev->lock, flags);
	raw_spin_lock_irqsave_nested(&dev->lock, flags, 1);
	local_irq_save(flags);
	local_save_flags(flags);
	list_for_each_entry(pos, &dev->list, node) { v++; }
	list_for_each_entry_safe(pos, tmp, &dev->list, node)
		v--;
	return v;
}
"""


class TestAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = cs.extract_scopes(ACCESS_C)

    def test_member_chains(self) -> None:
        m = self.m
        dev = var(m, "dev")
        self.assertEqual(members(m, dev)[:5], [
            (11, ["bus"], "->", "r"), (11, ["bus", "number"], "->", "w"), (12, ["devfn"], "->", "r"),
            (13, ["refcnt"], "->", "rw"), (14, ["dev"], "->", "addr")])
        self.assertEqual(uses(m, dev)[1:5], [(11, "r"), (12, "r"), (13, "r"), (14, "r")], "the base of a -> chain is read")
        self.assertEqual(m["members"][1][1:3], [11, ACCESS_C.split("\n")[10].index("number") + 1])
        s, arr = var(m, "s"), var(m, "arr")
        self.assertEqual(members(m, s), [(15, ["field"], ".", "w"), (17, ["a"], ".", "r"), (17, ["a", "b"], ".", "rw"),
                                         (18, ["field"], ".", "addr")])
        self.assertEqual(uses(m, s), [(3, "decl"), (15, "w"), (17, "w"), (18, "r")], "a written . chain writes the object")
        self.assertEqual(members(m, arr), [(16, ["[]", "x"], ".", "rw")])
        self.assertEqual(uses(m, arr), [(4, "decl"), (16, "w")])
        self.assertEqual(uses(m, var(m, "i")), [(8, "decl"), (16, "r")], "a subscript is read")

    def test_out_parameters(self) -> None:
        m = self.m
        self.assertEqual(uses(m, var(m, "lo")), [(5, "decl"), (19, "w"), (24, "rw")])
        self.assertEqual(uses(m, var(m, "hi")), [(5, "decl"), (19, "w"), (24, "rw")])
        self.assertEqual(uses(m, var(m, "n")), [(6, "decl"), (20, "w"), (23, "rw")])
        self.assertEqual(uses(m, var(m, "w")), [(8, "decl"), (21, "w")])
        self.assertEqual(uses(m, var(m, "p")), [(1, "decl"), (21, "r"), (22, "r")])
        self.assertEqual(uses(m, var(m, "flags")), [(7, "decl"), (25, "w"), (26, "w"), (27, "w"), (28, "w")])
        self.assertEqual([(ln, path, a) for ln, path, _op, a in members(m, var(m, "dev")) if ln in (25, 26)],
                         [(25, ["lock"], "addr"), (26, ["lock"], "addr")])
        self.assertEqual(uses(m, var(m, "pos")), [(9, "decl"), (29, "w"), (30, "w")], "for_each iterators write")
        self.assertEqual(uses(m, var(m, "tmp")), [(9, "decl"), (30, "w")], "a _safe iterator also writes the second")
        self.assertEqual(uses(m, var(m, "v"))[-4:], [(22, "w"), (29, "rw"), (31, "rw"), (32, "r")])
        self.assertIn("get_user", cs.OUT_PARAMS)


DECLS_C = """\
void g(spinlock_t *x, int q)
{
	u8 byte;
	spinlock_t *lock = &x;
	x * y;
	foo(bar);
	q = byte;
	q.b = byte;
	LIST_HEAD(list);
	DECLARE_BITMAP(mask, 64);
	DEFINE_MUTEX(mtx);
	DECLARE_COMPLETION_ONSTACK(done);
	DECLARE_WAIT_QUEUE_HEAD(wq);
	DECLARE_WAITQUEUE(wait, current);
	DEFINE_WAIT(w2);
	DEFINE_FOO(thing, q);
	DEFINE_PER_CPU(int, pc);
	struct foo init = { .len = q, .next = { .p = byte } };
	struct foo *fp = foo_get(q), *fq = (struct foo *)bar_get(q);
	char buf[sizeof(struct foo) + 1];
	unsigned long *vals[4];
	struct { int a; } anon;
	enum state st = STATE_A;
	typeof(q) copy = q;
	register int r asm("r1");
	int __maybe_unused un;
	const volatile int cv;
	switch (q) {
	case 1:
		goto out;
	default:
		break;
	}
#define LOCAL_MACRO(x) (x + q)
out:
	return;
}
"""


class TestDeclarations(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m = cs.extract_scopes(DECLS_C)
        cls.by = {v["name"]: v for v in cls.m["vars"] if v["kind"] != "param"}

    def test_typedef_guesses(self) -> None:
        by = self.by
        self.assertEqual(by["byte"]["type"], "u8")
        self.assertEqual((by["lock"]["type"], by["lock"]["init"]), ("spinlock_t *", [4, 21]))
        self.assertEqual(by["y"]["type"], "x *", "`x * y;` declares y")
        self.assertNotIn("bar", by, "`foo(bar);` is a call")
        self.assertEqual(uses(self.m, var(self.m, "q")), [(1, "decl"), (7, "w"), (8, "w"), (16, "r"), (18, "r"), (19, "r"),
                                                          (19, "r"), (24, "r"), (28, "r")], "`q.b = byte;` writes q")

    def test_macro_declarations(self) -> None:
        by = self.by
        want = {"list": ("struct list_head", False, "LIST_HEAD"), "mask": ("unsigned long", True, "DECLARE_BITMAP"),
                "mtx": ("struct mutex", False, "DEFINE_MUTEX"), "done": ("struct completion", False, "DECLARE_COMPLETION_ONSTACK"),
                "wq": ("wait_queue_head_t", False, "DECLARE_WAIT_QUEUE_HEAD"), "wait": (None, False, "DECLARE_WAITQUEUE"),
                "w2": (None, False, "DEFINE_WAIT"), "thing": (None, False, "DEFINE_FOO"), "pc": ("int", False, "DEFINE_PER_CPU")}
        self.assertEqual({k: (by[k]["type"], by[k]["array"], by[k]["macro"]) for k in want}, want)

    def test_declarators(self) -> None:
        by, m = self.by, self.m
        self.assertEqual(by["init"]["type"], "struct foo")
        self.assertEqual(uses(m, var(m, "byte")), [(3, "decl"), (7, "r"), (8, "r"), (18, "r")],
                         "designated initializer names are no uses, their values are")
        self.assertEqual(by["fp"]["type"], "struct foo *")
        self.assertEqual(by["fq"]["type"], "struct foo *")
        self.assertIn([var(m, "fp"), 19, col_of(DECLS_C, 19, "fp"), "foo_get"], m["assigns"])
        self.assertIn([var(m, "fq"), 19, col_of(DECLS_C, 19, "fq"), "bar_get"], m["assigns"], "through a cast")
        self.assertEqual((by["buf"]["type"], by["buf"]["array"]), ("char [sizeof(struct foo) + 1]", True))
        self.assertEqual((by["vals"]["type"], by["vals"]["pointer"], by["vals"]["array"]), ("unsigned long *[4]", 1, True))
        self.assertEqual((by["anon"]["type"], by["anon"]["base"]), ("struct {...}", None))
        self.assertNotIn("a", by, "the field of a local struct type is no variable")
        self.assertEqual(by["st"]["base"], {"kind": "enum", "name": "state"})
        self.assertEqual((by["copy"]["type"], by["copy"]["base"]), ("typeof(q)", None))
        self.assertEqual(by["r"]["type"], "int")
        self.assertEqual(by["un"]["type"], "int")
        self.assertEqual(by["cv"]["type"], "const volatile int")
        self.assertNotIn("out", by)
        self.assertNotIn("LOCAL_MACRO", by)
        self.assertIsNone(cs.lookup(m, 30, col_of(DECLS_C, 30, "out")), "goto out")


IFDEF_C = """\
int h(int q)
{
#ifdef CONFIG_A
	int v = 1;
#else
	long v = 2;
#endif
	return v + q;
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
"""


class TestPreprocessor(unittest.TestCase):
    def test_duplicate_declarations(self) -> None:
        m = cs.extract_scopes(IFDEF_C)
        v1, v2 = var(m, "v", line=4), var(m, "v", line=6)
        self.assertEqual((m["vars"][v1]["type"], m["vars"][v2]["type"]), ("int", "long"), "both branches are kept")
        self.assertEqual(uses(m, v2), [(6, "decl"), (8, "r")], "the use binds to the nearest preceding one")
        self.assertEqual(uses(m, v1), [(4, "decl")])

    def test_head_written_twice(self) -> None:
        m = cs.extract_scopes(IFDEF_C)
        twice = [f for f in m["functions"] if f["name"] == "twice"]
        self.assertEqual(len(twice), 2)
        self.assertEqual([len(f["params"]) for f in twice], [1, 2])
        a = var(m, "a", line=15)
        self.assertEqual(uses(m, a), [(15, "decl"), (18, "r")])


class TestLookup(unittest.TestCase):
    SRC = """\
static struct dev gdev;
void one(void)
{
	gdev.count = 1;
	gdev.count++;
}
void two(struct dev *d)
{
	gdev.count = d->count;
	d->count = d->count + 1;
}
"""

    def test_members(self) -> None:
        m = cs.extract_scopes(self.SRC)
        found = cs.lookup(m, 4, col_of(self.SRC, 4, "count"))
        self.assertEqual((found["kind"], found["path"], found["op"], found["access"]), ("member", ["count"], ".", "w"))
        self.assertEqual(found["member_uses"], [[4, 7, ["count"], "w"], [5, 7, ["count"], "rw"]],
                         "a file variable's member uses stay in the same function")
        d = cs.lookup(m, 10, col_of(self.SRC, 10, "count"))
        self.assertEqual(d["member_uses"], [[9, 18, ["count"], "r"], [10, 5, ["count"], "w"], [10, 16, ["count"], "r"]])
        self.assertEqual(cs.lookup(m, 9, 1, "gdev")["kind"], "var")
        self.assertIsNone(cs.lookup(m, 10, 1, "count"), "two `count` members on the line: ambiguous")
        self.assertIsNone(cs.lookup(m, 3, 1))


class TestSignatures(unittest.TestCase):
    def test_parse_signature(self) -> None:
        ps = cs.parse_signature
        self.assertEqual(ps("struct pci_dev *pci_get_bus_and_slot(unsigned int bus, unsigned int devfn)"),
                         {"name": "pci_get_bus_and_slot", "return_type": "struct pci_dev *", "macro": None,
                          "params": [{"name": "bus", "type": "unsigned int"}, {"name": "devfn", "type": "unsigned int"}]})
        self.assertEqual(ps("static int\nfoo(void)"), {"name": "foo", "return_type": "int", "params": [], "macro": None})
        sig = ps("static inline __init noinline __always_inline asmlinkage __cold __weak notrace __visible __must_check "
                 "__attribute__((section(\".x\"))) __printf(1, 2) unsigned long bar(const char *fmt, ...) __acquires(l);")
        self.assertEqual((sig["return_type"], sig["params"]),
                         ("unsigned long", [{"name": "fmt", "type": "const char *"}, {"name": "...", "type": "..."}]))
        sys_ = ps("SYSCALL_DEFINE5(pciconfig_write, unsigned long, bus, unsigned long, dfn,\n\t\tunsigned long, off, "
                  "unsigned long, len, void __user *, buf)\n{")
        self.assertEqual((sys_["name"], sys_["return_type"], sys_["macro"]), ("sys_pciconfig_write", "long", "SYSCALL_DEFINE5"))
        self.assertEqual(sys_["params"][-1], {"name": "buf", "type": "void __user *"})
        self.assertEqual(ps("COMPAT_SYSCALL_DEFINE1(foo, int, x)")["return_type"], "long")
        bpf = ps("BPF_CALL_2(bpf_map_lookup, struct bpf_map *, map, void *, key)")
        self.assertEqual((bpf["name"], bpf["return_type"], bpf["params"][0]), ("bpf_map_lookup", "u64",
                                                                             {"name": "map", "type": "struct bpf_map *"}))
        fp = ps("int register_hook(void (*fn)(int), int)")
        self.assertEqual(fp["params"], [{"name": "fn", "type": "void (*)(int)"}, {"name": None, "type": "int"}])
        self.assertEqual(ps("extern int foo(struct bar *);")["params"], [{"name": None, "type": "struct bar *"}])
        self.assertIsNone(ps("#define foo(x) (x)"))
        self.assertIsNone(ps("struct foo { … }"))
        self.assertIsNone(ps(""))

    def test_type_base(self) -> None:
        tb = cs.type_base
        self.assertEqual(tb("struct pci_dev *"), {"kind": "struct", "name": "pci_dev", "pointer": 1, "array": False})
        self.assertEqual(tb("u8"), {"kind": "typedef", "name": "u8", "pointer": 0, "array": False})
        self.assertEqual(tb("void __user *"), {"kind": "builtin", "name": "void", "pointer": 1, "array": False})
        self.assertEqual(tb("const unsigned long"), {"kind": "builtin", "name": "unsigned long", "pointer": 0, "array": False})
        self.assertEqual(tb("const char *const *"), {"kind": "builtin", "name": "char", "pointer": 2, "array": False})
        self.assertEqual(tb("union u [4]"), {"kind": "union", "name": "u", "pointer": 0, "array": True})
        self.assertEqual(tb("char [sizeof(*p)]"), {"kind": "builtin", "name": "char", "pointer": 0, "array": True})
        self.assertEqual(tb("void (*)(int *)"), {"kind": None, "name": None, "pointer": 1, "array": False})
        self.assertEqual(tb("typeof(x) *")["kind"], None)
        self.assertEqual(tb("")["kind"], None)


STRUCT_C = """\
struct device_like {
	int a, *b;	/* int fake; */
	union {
		struct { u8 lo; u8 hi; };
		u16 word;
	};
	unsigned int flag:1, other:3;
#ifdef CONFIG_X
	long xonly;
#endif
	void (*release)(struct device *dev);
	char name[16];
	struct { int inner; } named;
	DECLARE_BITMAP(bits, 64);
};
"""


class TestFindMember(unittest.TestCase):
    def test_synthetic(self) -> None:
        fm = cs.find_member
        self.assertEqual(fm(STRUCT_C, 1, 15, "b"), {"line": 2, "col": 10, "type": "int *",
                                                    "base": {"kind": "builtin", "name": "int"}, "pointer": 1, "array": False})
        self.assertEqual(fm(STRUCT_C, 1, 15, "hi")["line"], 4, "anonymous struct in an anonymous union")
        self.assertEqual(fm(STRUCT_C, 1, 15, "word")["type"], "u16")
        self.assertEqual((fm(STRUCT_C, 1, 15, "other")["line"], fm(STRUCT_C, 1, 15, "other")["col"]), (7, 23))
        self.assertEqual(fm(STRUCT_C, 1, 15, "xonly")["type"], "long")
        release = fm(STRUCT_C, 1, 15, "release")
        self.assertEqual((release["type"], release["base"], release["pointer"], release["col"]),
                         ("void (*)(struct device *dev)", None, 1, 9))
        self.assertEqual(fm(STRUCT_C, 1, 15, "name")["array"], True)
        self.assertEqual(fm(STRUCT_C, 1, 15, "named")["line"], 13)
        self.assertEqual(fm(STRUCT_C, 1, 15, "bits")["type"], "unsigned long")
        for absent in ("inner", "dev", "fake", "nothing"):
            self.assertIsNone(fm(STRUCT_C, 1, 15, absent), absent)
        self.assertIsNone(fm(STRUCT_C, 40, 50, "a"))

    def test_pci_structs(self) -> None:
        text = read_fixture("pci_h_v3.0_excerpt.h")
        lines = text.split("\n")
        dev_s = lines.index("struct pci_dev {") + 1
        dev_e = next(i for i in range(dev_s, len(lines)) if lines[i - 1] == "};")
        bus_s = lines.index("struct pci_bus {") + 1
        bus_e = next(i for i in range(bus_s, len(lines)) if lines[i - 1] == "};")
        bus = cs.find_member(text, dev_s, dev_e, "bus")
        self.assertEqual((bus["type"], bus["base"], bus["line"], bus["col"]),
                         ("struct pci_bus *", {"kind": "struct", "name": "pci_bus"}, dev_s + 2, 18))
        self.assertEqual(cs.find_member(text, dev_s, dev_e, "devfn")["type"], "unsigned int")
        self.assertEqual(cs.find_member(text, dev_s, dev_e, "dev")["type"], "struct device")
        self.assertEqual(cs.find_member(text, dev_s, dev_e, "resource")["type"], "struct resource [DEVICE_COUNT_RESOURCE]")
        self.assertEqual(cs.find_member(text, dev_s, dev_e, "pme_support")["type"], "unsigned int", "a bit-field")
        self.assertEqual(cs.find_member(text, dev_s, dev_e, "link_state")["base"], {"kind": "struct", "name": "pcie_link_state"},
                         "a field under #ifdef")
        self.assertEqual(cs.find_member(text, bus_s, bus_e, "number")["type"], "unsigned char")
        self.assertEqual(cs.find_member(text, bus_s, bus_e, "self")["base"], {"kind": "struct", "name": "pci_dev"})
        self.assertIsNone(cs.find_member(text, bus_s, bus_e, "devfn"), "a field of the other struct")


class TestRobustness(unittest.TestCase):
    ODD = ("", "\n", "}}}", "{{{", "int", "#if 0\n{\n#else\n", "static int f(int a\n{ return a; }", "a = b;",
           "struct { int x; ", "void f(void) { int x = (1 + ; }", "SYSCALL_DEFINE2(x, int)\n{ }", "int x[] = {",
           "void f() { for (int i = 0; ; ) { int j; } i = 1; }", "'\\0' \"abc", "\x00\xff\x0c", "do { } while (0)",
           "void f(void) { ({ int a; ", "#define X {\nvoid f(void) { }\n", "int (*(*f)(int))(long);",
           "typedef struct foo { int a; } foo_t;\nfoo_t v;", "void k(a, b) int a; char *b; { return; }")

    def test_never_raises(self) -> None:
        for src in self.ODD:
            model = cs.extract_scopes(src)
            self.assertEqual(model["format"], cs.SCOPE_FORMAT)
            json.dumps(model)
            for scope in model["scopes"]:
                self.assertIsNotNone(scope["span"][2], src)
            cs.lookup(model, 1, 1, "a")
        self.assertIsNone(cs.find_member("", 1, 1, "x"))

    def test_masked_text_is_reused(self) -> None:
        from parser.source_facts import mask
        text = read_fixture("pci_syscall_v3.0.c")
        self.assertEqual(cs.extract_scopes(text, mask(text)), cs.extract_scopes(text))

    def test_speed(self) -> None:
        text = read_fixture("pci_syscall_v3.0.c")
        big = "\n".join([text] * 8)  # ~1,100 lines
        started = time.perf_counter()
        model = cs.extract_scopes(big)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(model["functions"]), 16)
        self.assertLess(elapsed, 0.5, "a thousand lines take milliseconds (generous bound for loaded machines)")


if __name__ == "__main__":
    unittest.main()
