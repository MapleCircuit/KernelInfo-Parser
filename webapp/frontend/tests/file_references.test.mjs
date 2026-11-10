import { test } from "node:test";
import assert from "node:assert/strict";
import { referenceCount, referenceGroups, referenceMenuItems } from "../js/components/file_references.js";

const RES = {
  path: "include/linux/sched.h", indexed: true,
  included_by: [{ path: "kernel/fork.c", line: 12 }, { path: "kernel/<x>.c", line: 3 }],
  sourced_by: [],
  documented_in: [{ path: "Documentation/a.txt", line: 4, details: "see <sched.h>" }],
  built_by: [{ path: "kernel/Makefile", line: 2, rule: "obj-$(CONFIG_X) += x.o", symbols: ["X", "y"] }],
  totals: { included_by: 5000, sourced_by: 0, documented_in: 1, built_by: 1 }, truncated: true,
};

test("groups in display order, totals and labels", () => {
  const groups = referenceGroups(RES);
  assert.deepEqual(groups.map((g) => g.key), ["included_by", "built_by", "documented_in"]);
  assert.equal(groups[0].total, 5000);
  assert.equal(groups[0].items[1].label, "kernel/<x>.c:3");          // raw: the context menu escapes labels
  assert.equal(groups[1].items[0].title, "obj-$(CONFIG_X) += x.o (CONFIG_X, CONFIG_y)");
  assert.equal(groups[2].items[0].title, "see <sched.h>");
  assert.equal(referenceCount(RES), 5002);
});

test("menu items open in place or in a new tab", () => {
  const opened = [];
  const items = referenceMenuItems(RES, (p, l, n) => opened.push([p, l, n]));
  assert.equal(items[0].label, "Included by (5000) · showing 2");
  assert.ok(items[0].disabled);
  items[1].onClick();
  items[1].onMiddleClick();
  assert.deepEqual(opened, [["kernel/fork.c", 12, false], ["kernel/fork.c", 12, true]]);
  assert.ok(items.some((it) => it.separator));
});

test("empty and unindexed", () => {
  assert.equal(referenceMenuItems({ indexed: true, totals: {} }, () => {})[0].label, "No file refers to this one");
  assert.equal(referenceMenuItems({ indexed: false }, () => {})[0].label, "This version of the file is not indexed");
  assert.equal(referenceCount({}), 0);
});
