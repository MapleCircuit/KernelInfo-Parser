import { test } from "node:test";
import assert from "node:assert/strict";
import {
  IMPORT_FILTERS, basename, chainSegments, countByFilter, emptyStateMessage, filterIdOf, filterImports,
  formatChain, groupImports, isImportsPath, kindIcon, kindLabel, matchesQuery,
} from "../js/views/code_view/imports_model.js";

const it = (name, kind, header_path, chain, extra = {}) => ({
  name, kind, header_path, chain, line: 1, forced: false, ...extra,
});
const ITEMS = [
  it("kmalloc", "function", "include/linux/slab.h", ["kernel/fork.c", "include/linux/mm.h", "include/linux/slab.h"]),
  it("do_fork", "prototype", "include/linux/sched.h", ["kernel/fork.c", "include/linux/sched.h"]),
  it("task_struct", "struct", "include/linux/sched.h", ["kernel/fork.c", "include/linux/sched.h"]),
  it("pid_t", "type", "include/linux/types.h", ["kernel/fork.c", "include/linux/types.h"]),
  it("IS_ENABLED", "macro", "include/linux/kconfig.h", ["include/linux/kconfig.h"], { forced: true }),
  it("jiffies", "variable", "include/linux/jiffies.h", ["kernel/fork.c", "include/linux/jiffies.h"]),
];

test("path eligibility", () => {
  for (const p of ["a.c", "kernel/fork.c", "x/y.h", "entry.S", "entry.s"]) assert.equal(isImportsPath(p), true, p);
  for (const p of [null, undefined, "", ".c", "dir/.h", "lib.rs", "Makefile", "foo.C", "foo.H"]) {
    assert.equal(isImportsPath(p), false, String(p));
  }
});

test("filter ids, icons and labels", () => {
  assert.deepEqual(IMPORT_FILTERS.map((f) => f.id), ["all", "function", "struct", "type", "macro", "variable"]);
  assert.equal(filterIdOf("prototype"), "function");
  assert.equal(filterIdOf("struct"), "struct");
  assert.equal(filterIdOf("weird"), "variable");
  assert.equal(kindIcon("struct"), "{}");
  assert.equal(kindIcon("prototype"), "ƒ");
  assert.equal(kindIcon("nope"), "•");
  assert.equal(kindLabel("macro"), "Macro");
  assert.equal(kindLabel("nope"), "nope");
});

test("kind pill filtering folds prototypes into functions", () => {
  assert.deepEqual(filterImports(ITEMS, { kind: "function" }).map((i) => i.name), ["kmalloc", "do_fork"]);
  assert.equal(filterImports(ITEMS).length, ITEMS.length);
  assert.deepEqual(filterImports(null), []);
  assert.deepEqual(filterImports(undefined, { kind: "macro" }), []);
});

test("search matches names, headers and intermediate chain headers", () => {
  assert.deepEqual(filterImports(ITEMS, { query: "  KMAL " }).map((i) => i.name), ["kmalloc"]);
  // linux/mm.h is only in the middle of kmalloc's chain
  assert.deepEqual(filterImports(ITEMS, { query: "linux/mm.h" }).map((i) => i.name), ["kmalloc"]);
  assert.deepEqual(filterImports(ITEMS, { query: "sched.h" }).map((i) => i.name), ["do_fork", "task_struct"]);
  assert.equal(matchesQuery(ITEMS[0], "   "), true);
  assert.equal(matchesQuery(ITEMS[0], "zzz"), false);
  assert.deepEqual(filterImports(ITEMS, { query: "sched.h", kind: "struct" }).map((i) => i.name), ["task_struct"]);
});

test("countByFilter with and without query", () => {
  assert.deepEqual(countByFilter(ITEMS), { all: 6, function: 2, struct: 1, type: 1, macro: 1, variable: 1 });
  assert.deepEqual(countByFilter(ITEMS, "sched.h"), { all: 2, function: 1, struct: 1, type: 0, macro: 0, variable: 0 });
  assert.deepEqual(countByFilter(null), { all: 0, function: 0, struct: 0, type: 0, macro: 0, variable: 0 });
});

test("groupImports by kind is ordered and deterministic", () => {
  const input = [...ITEMS].reverse();
  const snapshot = JSON.stringify(input);
  const groups = groupImports(input, "kind");
  assert.equal(JSON.stringify(input), snapshot);
  assert.deepEqual(groups.map((g) => [g.key, g.label]), [
    ["function", "Functions"], ["struct", "Structs"], ["type", "Types"], ["macro", "Macros"], ["variable", "Variables"],
  ]);
  assert.deepEqual(groups[0].items.map((i) => i.name), ["do_fork", "kmalloc"]);
  assert.deepEqual(groupImports([ITEMS[3]]).map((g) => g.key), ["type"]);
  assert.deepEqual(groupImports(null), []);
});

test("groupImports by header orders by chain length then path; items tie-break on header and line", () => {
  const groups = groupImports(ITEMS, "header");
  assert.deepEqual(groups.map((g) => g.key), [
    "include/linux/kconfig.h", "include/linux/jiffies.h", "include/linux/sched.h", "include/linux/types.h",
    "include/linux/slab.h",
  ]);
  assert.equal(groups[0].label, "include/linux/kconfig.h");
  assert.deepEqual(groups[2].items.map((i) => i.name), ["do_fork", "task_struct"]);
  const dup = [
    it("a", "macro", "b.h", ["x.c", "b.h"], { line: 9 }),
    it("a", "macro", "a.h", ["x.c", "a.h"], { line: 5 }),
    it("a", "macro", "a.h", ["x.c", "a.h"], { line: 2 }),
  ];
  assert.deepEqual(groupImports(dup, "kind")[0].items.map((i) => [i.header_path, i.line]), [["a.h", 2], ["a.h", 5], ["b.h", 9]]);
});

test("chainSegments", () => {
  const segs = chainSegments(ITEMS[0]);
  assert.deepEqual(segs.map((s) => s.label), ["fork.c", "mm.h", "slab.h"]);
  assert.deepEqual(segs.map((s) => [s.index, s.isRoot, s.isTarget]), [[0, true, false], [1, false, false], [2, false, true]]);
  assert.equal(segs[1].path, "include/linux/mm.h");
  assert.deepEqual(chainSegments({}), []);
  assert.equal(basename("a/b/c.h"), "c.h");
  assert.equal(basename("c.h"), "c.h");
});

test("formatChain normal vs forced include", () => {
  assert.equal(formatChain(ITEMS[0]), "fork.c → mm.h → slab.h");
  assert.equal(formatChain(ITEMS[0], { arrow: " > ", full: true }), "kernel/fork.c > include/linux/mm.h > include/linux/slab.h");
  const forced = it("foo", "macro", "include/linux/foo.h", ["include/linux/kconfig.h", "include/linux/foo.h"], { forced: true });
  assert.equal(formatChain(forced), "-include kconfig.h → foo.h");
  assert.equal(formatChain({}), "");
});

test("emptyStateMessage", () => {
  const ok = { indexed: true, layout: true, arch: "x86_64", items: ITEMS };
  assert.equal(emptyStateMessage(ok, { path: "a.c" }), null);
  assert.equal(emptyStateMessage(null, { path: "a.c" }), null);
  assert.equal(emptyStateMessage(ok, { path: "a.rs" }), "Imports are available for C, header and assembly files.");
  assert.equal(emptyStateMessage(null, { path: "Makefile" }), "Imports are available for C, header and assembly files.");
  assert.equal(emptyStateMessage({ ...ok, indexed: false }, { path: "a.c" }),
    "This revision's file is not indexed; imported symbols are unavailable.");
  assert.equal(emptyStateMessage({ ...ok, layout: false }, { path: "a.c" }), "No build layout for ARCH=x86_64 at this revision.");
  assert.equal(emptyStateMessage({ ...ok, items: [] }, { path: "a.c" }), "No imported symbols referenced in this file.");
});
