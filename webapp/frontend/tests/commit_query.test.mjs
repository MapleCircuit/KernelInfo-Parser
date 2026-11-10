import { test } from "node:test";
import assert from "node:assert/strict";
import { completeKey, isStructured, parseCommitQuery, queryParams, removeToken, tokenize } from "../js/views/commits/commit_query.js";

test("tokens: quotes, keys, plain words", () => {
  assert.deepEqual(tokenize('path:fs/ext4 reviewer:"Jan Kara" journal').map((t) => [t.key, t.value]),
    [["path", "fs/ext4"], ["reviewer", "Jan Kara"], ["", "journal"]]);
  assert.deepEqual(tokenize('"two words" x').map((t) => t.value), ["two words", "x"]);
});

test("parse: chips, repeated keys, unknown keys stay text", () => {
  const q = parseCommitQuery('path:fs/ext4 path:*.h reviewer:"Jan Kara" since:2011-01-01 in:v3.0 merges:none foo:bar journal');
  assert.deepEqual(q.paths, ["fs/ext4", "*.h"]);
  assert.deepEqual(q.roles, ["reviewer:Jan Kara"]);
  assert.equal(q.since, "2011-01-01");
  assert.equal(q.release, "v3.0");
  assert.equal(q.merges, "none");
  assert.equal(q.text, "foo:bar journal");
  assert.equal(q.chips.length, 6);
  assert.equal(isStructured("just text"), false);
  assert.equal(isStructured("http://x journal"), false);
  assert.equal(isStructured("author:ann"), true);
});

test("request parameters and chip removal", () => {
  const q = parseCommitQuery("path:fs/ext4 in:v3.0 merges:none journal");
  assert.equal(queryParams(q, 2, 50).toString(), "path=fs%2Fext4&release=v3.0&merges=none&text=journal&page=2&limit=50");
  const text = 'path:fs/ext4 reviewer:"Jan Kara" journal';
  const chip = parseCommitQuery(text).chips.find((c) => c.key === "reviewer");
  assert.equal(removeToken(text, chip.index), "path:fs/ext4 journal");
  assert.deepEqual(completeKey("rev"), ["reviewer:"]);
  assert.deepEqual(completeKey("path:x"), []);
});
