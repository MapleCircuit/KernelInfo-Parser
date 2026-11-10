import { test } from "node:test";
import assert from "node:assert/strict";
import { blameBeforeTarget, blameQuery, formatBlameOpts, movedFrom, parseBlameOpts } from "../js/views/code_view/blame_options.js";

test("blame option letters round trip", () => {
  assert.deepEqual(parseBlameOpts("mcw"), { move: true, copy: true, ws: true });
  assert.deepEqual(parseBlameOpts("w"), { move: false, copy: false, ws: true });
  assert.equal(formatBlameOpts({ ws: true, move: true }), "mw");
  assert.equal(formatBlameOpts(null), "");
  assert.equal(blameQuery("mc"), "move=1&copy=1");
  assert.equal(blameQuery({ ws: true }), "ws=1");
  assert.equal(blameQuery(""), "");
});

test("blame before this change", () => {
  const b = { line: 40, orig_line: 37, commit_hash: "a".repeat(40), previous_sha: "b".repeat(40), previous_path: "old.c", source_path: "new.c" };
  assert.deepEqual(blameBeforeTarget(b, "new.c"), { version: "b".repeat(40), path: "old.c", line: 37 });
  assert.equal(blameBeforeTarget({ line: 3 }, "x.c"), null);
  assert.deepEqual(blameBeforeTarget({ line: 3, previous_sha: "c".repeat(40) }, "x.c"), { version: "c".repeat(40), path: "x.c", line: 3 });
  assert.equal(movedFrom(b, "new.c"), "");
  assert.equal(movedFrom(b, "other.c"), "new.c");
});
