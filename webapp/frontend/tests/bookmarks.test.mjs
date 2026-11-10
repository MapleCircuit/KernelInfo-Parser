import { test } from "node:test";
import assert from "node:assert/strict";
import { decodeTour, encodeTour, relocate, sameTarget, sortBookmarks, targetLabel, validTarget, validateTour } from "../js/views/bookmarks/bookmark_model.js";

test("targets: validation and labels", () => {
  assert.ok(validTarget({ type: "line", path: "init/main.c", line: 474, rev: "v3.0", sha: "02f8c6aee8df", snippet: "asmlinkage void __init start_kernel(void)" }));
  for (const bad of [{ type: "line", path: "../etc/passwd", line: 1 }, { type: "line", path: "/abs", line: 1 }, { type: "line", path: "a.c", line: 0 },
    { type: "symbol", name: "a b" }, { type: "commit", hash: "xyz" }, { type: "line", path: "a.c", line: 1, rev: "../x" }, { type: "nope" }, null]) {
    assert.equal(validTarget(bad), false, JSON.stringify(bad));
  }
  assert.equal(targetLabel({ type: "kconfig", name: "EXT4_FS" }), "CONFIG_EXT4_FS");
  assert.ok(sameTarget({ type: "line", path: "a.c", line: 3, rev: "v3.0" }, { type: "line", path: "a.c", line: 3, rev: "v3.0" }));
  assert.ok(!sameTarget({ type: "line", path: "a.c", line: 3, rev: "v3.0" }, { type: "line", path: "a.c", line: 4, rev: "v3.0" }));
});

test("snippet relocation", () => {
  const lines = ["a", "b", "int x;", "c"];
  assert.deepEqual(relocate(lines, 3, "int x;"), { line: 3, moved: false });
  assert.deepEqual(relocate(["new", ...lines], 3, "int x;"), { line: 4, moved: true });
  assert.equal(relocate(lines, 3, "gone"), null);
  assert.equal(relocate(Array(500).fill("z").concat(["int x;"]), 3, "int x;"), null, "outside ±200 lines");
});

test("ordering", () => {
  const r = (id, group, order, created) => ({ id, created_at: created, data: { group, order } });
  assert.deepEqual(sortBookmarks([r("c", "b", 0, 1), r("a", "", 1, 2), r("b", "", 0, 3)]).map((x) => x.id), ["b", "a", "c"]);
});

test("tours: validation and link round trip", async () => {
  const tour = { title: "Syscall entry", description: "How a syscall enters", steps: [
    { target: { type: "line", path: "arch/x86/kernel/entry_64.S", line: 470, rev: "v3.0" }, title: "entry", markdown: "**system_call**" },
    { target: { type: "symbol", name: "sys_read", rev: "v3.0" }, title: "read", markdown: "x".repeat(2000) },
  ] };
  const t = await encodeTour(tour);
  assert.match(t, /^[A-Za-z0-9_-]+$/);
  assert.ok(t.length < 2000, "compressed");
  assert.deepEqual(await decodeTour(t), validateTour(tour));
  assert.throws(() => validateTour({ ...tour, steps: [{ target: { type: "line", path: "../x", line: 1 } }] }), /invalid target/);
  assert.throws(() => validateTour({ title: "", steps: [] }), /title/);
  assert.throws(() => validateTour({ title: "big", steps: [{ target: { type: "symbol", name: "a" }, markdown: "x".repeat(1100000) }] }), /1 MB/);
  await assert.rejects(decodeTour("not-a-tour"));
});
