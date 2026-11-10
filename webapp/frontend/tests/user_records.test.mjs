import { test } from "node:test";
import assert from "node:assert/strict";
import { exportDocument, filterRecords, importRecords, makeRecord, mergeImport, userKey } from "../js/utils/user_records.js";

test("keys, export filtering and import modes", () => {
  assert.equal(userKey("linux", "bookmark", "x1"), "linux::bookmark::x1");
  const a = makeRecord("bookmark", "linux", { path: "a.c" }, { id: "a", now: 1 });
  const b = makeRecord("note", "linux", { text: "hi" }, { id: "b", now: 2 });
  const c = makeRecord("bookmark", "other", { path: "c.c" }, { id: "c", now: 3 });
  assert.throws(() => makeRecord("nope", "linux", {}));
  assert.deepEqual(filterRecords([a, b, c], { kinds: ["bookmark"] }).map((r) => r.id), ["a", "c"]);
  assert.deepEqual(filterRecords([a, b, c], { repo: "linux" }).map((r) => r.id), ["a", "b"]);
  const doc = exportDocument([a, b], 0);
  assert.equal(doc.format, "kip-user");
  const back = importRecords(JSON.parse(JSON.stringify(doc)));
  assert.deepEqual(back.map((r) => r.key), [a.key, b.key]);
  assert.throws(() => importRecords({ format: "other" }));
  const existing = new Map([[a.key, a]]);
  const changedA = { ...a, data: { path: "z.c" } };
  assert.deepEqual(mergeImport(existing, [changedA, b], "skip-existing").put.map((r) => r.id), ["b"]);
  assert.equal(mergeImport(existing, [changedA, b], "skip-existing").skipped, 1);
  assert.deepEqual(mergeImport(existing, [changedA], "overwrite").put[0].data, { path: "z.c" });
  const copies = mergeImport(existing, [a], "copy").put;
  assert.notEqual(copies[0].id, "a");
  assert.equal(copies[0].kind, "bookmark");
});
