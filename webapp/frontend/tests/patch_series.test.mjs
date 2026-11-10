import { test } from "node:test";
import assert from "node:assert/strict";
import { addEdit, addPatch, assignFile, draftProblems, emptyDraft, gitBlobSha, movePatch, parseAddresses, removeEdit, removePatch, seriesPayload, updatePatch } from "../js/views/patch/series_model.js";

test("blob sha matches git hash-object", async () => {
  assert.equal(await gitBlobSha("hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a");
  assert.equal(await gitBlobSha(""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391");
});

test("staging, assignment, reorder and payload", () => {
  let { draft: d } = addEdit(emptyDraft(), { base: "v3.0", path: "lib/a.c", originalBlob: "aa", modified: "A" });
  d = addEdit(d, { base: "v3.0", path: "lib/b.c", originalBlob: "bb", modified: "B" }).draft;
  assert.equal(d.series.length, 1);
  assert.deepEqual(d.series[0].files, ["lib/a.c", "lib/b.c"]);
  assert.equal(d.series[0].subject, "lib: ");
  assert.match(addEdit(d, { base: "v3.1", path: "x.c", originalBlob: "", modified: "" }).error, /based on v3.0/);
  d = addPatch(d, "lib: second");
  const [p1, p2] = d.series.map((p) => p.id);
  d = assignFile(d, "lib/b.c", p2);
  assert.deepEqual(d.series.map((p) => p.files), [["lib/a.c"], ["lib/b.c"]]);
  d = addEdit(d, { base: "v3.0", path: "lib/b.c", originalBlob: "bb", modified: "B2" }).draft;
  assert.deepEqual(d.series.map((p) => p.files), [["lib/a.c"], ["lib/b.c"]], "a re-edited file keeps its patch");
  d = movePatch(d, p2, -1);
  assert.deepEqual(d.series.map((p) => p.id), [p2, p1]);
  d = updatePatch(d, p1, { subject: "lib: first" });
  assert.deepEqual(draftProblems(d), ["Author name and email are needed"]);
  d.author = { name: "Ada", email: "ada@example.org" };
  d.to = parseAddresses("A <a@x.org>, b@y.org\n");
  const body = seriesPayload(d);
  assert.equal(body.base_commit, "v3.0");
  assert.deepEqual(body.to, ["A <a@x.org>", "b@y.org"]);
  assert.deepEqual(body.patches.map((p) => [p.subject, p.files.map((f) => [f.path, f.original_blob, f.modified_content])]),
    [["lib: second", [["lib/b.c", "bb", "B2"]]], ["lib: first", [["lib/a.c", "aa", "A"]]]]);
  assert.ok(body.cover);
  d = removePatch(d, p2);
  assert.deepEqual(d.series.map((p) => p.files), [["lib/a.c", "lib/b.c"]]);
  assert.equal(removePatch(d, p1).series.length, 1, "the last patch stays");
  d = removeEdit(d, "lib/a.c");
  assert.deepEqual(Object.keys(d.edits), ["lib/b.c"]);
  assert.equal(seriesPayload(d).cover, null);
  // The draft is plain data: it survives the session round trip
  assert.deepEqual(JSON.parse(JSON.stringify(d)), d);
});
