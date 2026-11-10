import { test } from "node:test";
import assert from "node:assert/strict";
import {
  containingRanges, currentDefinition, filterOutline, flattenOutline, groupOutline, kconfigOutline, matchSymbols,
  stickyLines, stickyRanges,
} from "../js/views/code_view/outline_model.js";

const item = (name, kind, s, e, children = []) => ({ name, kind, type_name: "x", line_s: s, line_e: e, children });
const ITEMS = [
  item("FOO", "macro", 3, 3),
  item("task", "type", 10, 30, [item("A", "constant", 12, 12), item("inner", "type", 15, 25)]),
  item("copy_process", "function", 40, 400),
  item("copy_fs", "function", 410, 420),
  item("do_fork", "prototype", 5, 5),
];

test("containing ranges and the current definition", () => {
  const ranges = stickyRanges(ITEMS);
  assert.deepEqual(ranges.map((r) => r.name), ["task", "inner", "copy_process", "copy_fs"]);
  assert.deepEqual(containingRanges(ranges, 20).map((r) => r.name), ["task", "inner"]);
  assert.deepEqual(containingRanges(ranges, 35), []);
  assert.equal(currentDefinition(ranges, 100).name, "copy_process");
  assert.equal(currentDefinition(ranges, 1), null);
});

test("sticky levels: definitions starting above the top line, two at most", () => {
  const ranges = stickyRanges(ITEMS);
  assert.deepEqual(stickyLines(ranges, 100).map((r) => r.name), ["copy_process"]);
  assert.deepEqual(stickyLines(ranges, 40), []);
  assert.deepEqual(stickyLines(ranges, 18).map((r) => r.name), ["task", "inner"]);
  // One pinned row hides line 14: line 15 (inner's own first line) is visible, so only "task" is pinned
  assert.deepEqual(stickyLines(ranges, 14).map((r) => r.name), ["task"]);
  assert.deepEqual(stickyLines(ranges, 18, 1).map((r) => r.name), ["inner"]);
  assert.deepEqual(stickyLines([], 5), []);
});

test("filter keeps ancestors; grouping; symbol matching", () => {
  const f = filterOutline(ITEMS, "inn");
  assert.deepEqual(flattenOutline(f).map((x) => x.name), ["task", "inner"]);
  assert.deepEqual(groupOutline(ITEMS).map((g) => g.title), ["Functions", "Types", "Macros", "Declarations"]);
  assert.deepEqual(matchSymbols(ITEMS, "copy_").map((x) => x.name), ["copy_fs", "copy_process"]);
  assert.deepEqual(matchSymbols(ITEMS, "copy_process").map((x) => x.name), ["copy_process"]);
  assert.deepEqual(matchSymbols(ITEMS, "do_fork").map((x) => x.name), []);
});

test("Kconfig files are outlined from their text", () => {
  const lines = [
    'menu "General setup"',
    "config EXPERIMENTAL",
    '\tbool "Prompt for development code"',
    "\thelp",
    "\t  Some text.",
    "",
    "choice",
    '\tprompt "Kernel compression mode"',
    "config KERNEL_GZIP",
    '\tbool "Gzip"',
    "endchoice",
    "if BLOCK",
    "menuconfig IOSCHED",
    "endif",
    "endmenu",
  ];
  const out = kconfigOutline(lines);
  assert.equal(out.length, 1);
  assert.equal(out[0].name, "General setup");
  assert.deepEqual([out[0].line_s, out[0].line_e], [1, 15]);
  assert.deepEqual(out[0].children.map((c) => c.name), ["EXPERIMENTAL", "choice: Kernel compression mode", "if BLOCK"]);
  assert.deepEqual([out[0].children[0].line_s, out[0].children[0].line_e], [2, 5]);
  assert.deepEqual(out[0].children[1].children.map((c) => c.name), ["KERNEL_GZIP"]);
  assert.deepEqual(out[0].children[2].children.map((c) => c.name), ["IOSCHED"]);
});
