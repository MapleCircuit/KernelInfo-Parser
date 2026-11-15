import { test } from "node:test";
import assert from "node:assert/strict";
import { exportDocument, filterRecords, importRecords, importUserDataText, makeRecord, mergeImport, parseUserDataText, userKey } from "../js/utils/user_records.js";

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
  assert.throws(() => importRecords({ format: "other" }), /Not a KernelInfo user data export/);
  assert.throws(() => importRecords({ format: "kip-session", user: { records: [a] } }), /session export/i);
  assert.throws(() => importRecords({ format: "kiptour", tour: {} }), /code tour export/i);
  assert.deepEqual(importRecords([{ kind: "bookmark", id: "bad", data: null }, a]).map((r) => r.id), ["a"]);
  const existing = new Map([[a.key, a]]);
  const changedA = { ...a, data: { path: "z.c" } };
  assert.deepEqual(mergeImport(existing, [changedA, b], "skip-existing").put.map((r) => r.id), ["b"]);
  assert.equal(mergeImport(existing, [changedA, b], "skip-existing").skipped, 1);
  assert.equal(mergeImport(new Map(), [a, a], "skip-existing").put.length, 1);
  assert.equal(mergeImport(new Map(), [a, a], "skip-existing").skipped, 1);
  assert.deepEqual(mergeImport(existing, [changedA], "overwrite").put[0].data, { path: "z.c" });
  const copies = mergeImport(existing, [a], "copy").put;
  assert.notEqual(copies[0].id, "a");
  assert.equal(copies[0].kind, "bookmark");
});

test("parseUserDataText and offline importUserDataText", async () => {
  const a = makeRecord("bookmark", "linux", { path: "a.c" }, { id: "a", now: 1 });
  const b = makeRecord("note", "linux", { text: "hi" }, { id: "b", now: 2 });
  const json = `\uFEFF${JSON.stringify(exportDocument([a, b], 0))}`;
  assert.equal(parseUserDataText(json).length, 2);
  assert.deepEqual(parseUserDataText(JSON.stringify(exportDocument([], 0))), []);
  assert.throws(() => parseUserDataText("{"), /Not a valid JSON file/);
  assert.throws(() => parseUserDataText(JSON.stringify(exportDocument([{ kind: "bad" }], 0))), /No valid user records/);

  const store = new Map([[a.key, a]]);
  const mockDb = {
    db: {},
    ready: Promise.resolve(),
    userList: async () => [...store.values()],
    userPut: async (rec) => { store.set(rec.key, rec); return true; },
  };
  const res = await importUserDataText(json, mockDb);
  assert.deepEqual(res, { imported: 1, skipped: 1 });
  assert.ok(store.has(b.key));

  await assert.rejects(
    importUserDataText(json, { db: null, ready: Promise.resolve(), userList: async () => [], userPut: async () => false }),
    /IndexedDB/
  );
  await assert.rejects(
    importUserDataText(json, { ready: Promise.resolve(), userList: async () => [], userPut: async () => false }),
    /Could not save user data/
  );
});
