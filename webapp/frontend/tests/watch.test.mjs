import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { checkGroups, dueWatches, nextState, unreadCount, validWatchTarget, watchLabel } from "../js/views/watch/watch_model.js";

const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};

const { api } = await import("../js/api.js");
const { localDB } = await import("../js/db.js");
const { checkWatches } = await import("../js/views/watch/watch_runtime.js");

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

test("watch_runtime.checkWatches handles missing/null res.items safely", async () => {
  const origUserList = localDB.userList;
  const origRequest = api.request;
  try {
    localDB.userList = async (kind) => (kind === "watch"
      ? [W("w1", { type: "file", path: "kernel/fork.c" }, "master", "aaa")]
      : []);
    api.request = async () => ({ to_sha: "bbb", items: null });
    assert.equal(await checkWatches(), 0);
    api.request = async () => null;
    assert.equal(await checkWatches(), 0);
  } finally {
    localDB.userList = origUserList;
    api.request = origRequest;
  }
});

test("watch_runtime.checkWatches chunks groups larger than 200 items", async () => {
  const origUserList = localDB.userList;
  const origUserPut = localDB.userPut;
  const origRequest = api.request;
  const batchSizes = [];
  try {
    const many = Array.from({ length: 205 }, (_, i) => W(`w${i}`, { type: "file", path: `kernel/f${i}.c` }, "master", "aaa"));
    localDB.userList = async (kind) => (kind === "watch" ? many : []);
    localDB.userPut = async () => {};
    api.request = async (_url, opts) => {
      const body = JSON.parse(opts.body);
      batchSizes.push(body.items.length);
      return { to_sha: "bbb", items: body.items.map((it) => ({ key: it.key, status: "same", changed: false })) };
    };
    assert.equal(await checkWatches(), 0);
    assert.deepEqual(batchSizes, [200, 5]);
  } finally {
    localDB.userList = origUserList;
    localDB.userPut = origUserPut;
    api.request = origRequest;
  }
});
