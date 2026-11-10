import { test } from "node:test";
import assert from "node:assert/strict";
import { checkGroups, dueWatches, nextState, unreadCount, validWatchTarget, watchLabel } from "../js/views/watch/watch_model.js";

const W = (key, target, branch, base_sha = "aaa") => ({ key, data: { target, branch, base_sha } });

test("targets and labels", () => {
  assert.ok(validWatchTarget({ type: "file", path: "kernel/fork.c" }));
  assert.ok(!validWatchTarget({ type: "dir", path: "../x" }));
  assert.ok(validWatchTarget({ type: "symbol", name: "copy_process" }));
  assert.ok(!validWatchTarget({ type: "symbol", name: "a b" }));
  assert.equal(watchLabel({ type: "kconfig", name: "EXT4_FS" }), "CONFIG_EXT4_FS");
});

test("which updates concern which watches, and grouping", () => {
  const ws = [W("1", { type: "file", path: "a" }, "master"), W("2", { type: "symbol", name: "x" }, "master"), W("3", { type: "file", path: "b" }, "linux-6.6.y", "")];
  assert.deepEqual(dueWatches(ws, { changed: ["master"] }).map((w) => w.key), ["1", "2"]);
  assert.deepEqual(dueWatches(ws, { added: ["v9.9"] }), []);
  const states = new Map([["2", { last_sha: "bbb" }]]);
  assert.deepEqual(checkGroups(ws, states).map((g) => [g.from, g.to, g.watches.map((w) => w.key)]), [["aaa", "master", ["1"]], ["bbb", "master", ["2"]]]);
});

test("unread bookkeeping", () => {
  const s1 = nextState(null, { status: "same", changed: false, summary: "same" }, "s1", 1);
  assert.equal(s1.unread, false);
  const s2 = nextState(s1, { status: "modified", changed: true, summary: "modified" }, "s2", 2);
  assert.deepEqual([s2.unread, s2.changed_since, s2.last_sha], [true, "s1", "s2"]);
  const s3 = nextState(s2, { status: "same", changed: false }, "s3", 3);
  assert.equal(s3.unread, true, "stays unread until marked read");
  assert.equal(s3.result.status, "modified");
  assert.equal(unreadCount(new Map([["a", s3], ["b", s1]])), 1);
});
