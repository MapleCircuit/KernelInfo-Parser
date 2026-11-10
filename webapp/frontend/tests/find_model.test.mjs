import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildMatcher, countLabel, findAll, findAllChunked, findInRows, firstMatchFrom, groupByRow, matchLine,
  rangesToNodePieces, searchRows, stepMatch,
} from "../js/views/code_view/find_model.js";

test("literal, case and whole word", () => {
  const lines = ["spin_lock(&rq->lock);", "RQ->LOCK", "rq->lock_owner = rq->lock;"];
  let r = findAll(lines, "rq->lock");
  assert.equal(r.matches.length, 4);
  r = findAll(lines, "rq->lock", { caseSensitive: true });
  assert.equal(r.matches.length, 3);
  r = findAll(lines, "rq->lock", { wholeWord: true });
  assert.deepEqual(r.matches.map((m) => [m.row, m.start]), [[0, 11], [1, 0], [2, 17]]);
  assert.deepEqual(findAll(lines, "(").matches.length, 1); // escaped, not a regex
});

test("regex, anchors and errors", () => {
  const lines = ["static inline int a;", "  static inline", "static int b;"];
  const r = findAll(lines, "^static inline", { regex: true });
  assert.deepEqual(r.matches.map((m) => m.row), [0]);
  const bad = findAll(lines, "a(", { regex: true });
  assert.equal(bad.matches.length, 0);
  assert.ok(bad.error);
  assert.equal(buildMatcher("").re, null);
});

test("zero-width regexes advance and Unicode/tabs keep offsets", () => {
  assert.deepEqual(matchLine("abc", buildMatcher("x*", { regex: true }).re), []);
  assert.deepEqual(matchLine("a\tb\tb", buildMatcher("b").re), [[2, 3], [4, 5]]);
  assert.deepEqual(matchLine("é😀x", buildMatcher("x").re), [[3, 4]]);
  assert.deepEqual(matchLine("😀", buildMatcher("(?:)", { regex: true }).re), []);
});

test("cap and chunked search", async () => {
  const lines = Array.from({ length: 50 }, () => "aaaa");
  const r = findAll(lines, "a", {}, 10);
  assert.equal(r.matches.length, 10);
  assert.equal(r.capped, true);
  const rows = searchRows(lines);
  let progress = 0;
  const res = await findAllChunked(rows, buildMatcher("a").re, { chunk: 7, idle: async () => {}, onProgress: () => progress++ });
  assert.equal(res.matches.length, 200);
  assert.ok(progress >= 7);
  const capped = await findAllChunked(rows, buildMatcher("a").re, { chunk: 7, cap: 15, idle: async () => {} });
  assert.equal(capped.matches.length, 15);
  assert.equal(capped.capped, true);
  let n = 0;
  const cancelled = await findAllChunked(rows, buildMatcher("a").re, { chunk: 7, idle: async () => {}, isCancelled: () => n++ > 1 });
  assert.equal(cancelled.cancelled, true);
});

test("diff rows: unified rows and split halves", () => {
  const fd = { layout: "split", pairs: [{ l: { t: "foo" }, r: { t: "foo bar" } }, { l: null, r: { t: "bar" } }], rows: [] };
  const rows = searchRows([], fd);
  assert.deepEqual(rows.map((r) => `${r.row}${r.side}`), ["0l", "0r", "1r"]);
  const { matches } = findInRows(rows, buildMatcher("bar").re);
  assert.deepEqual(matches.map((m) => `${m.row}${m.side}:${m.start}`), ["0r:4", "1r:0"]);
  const unified = searchRows([], { layout: "unified", rows: [{ t: "x" }, { t: "y" }] });
  assert.deepEqual(unified.map((r) => r.side), [null, null]);
});

test("stepping wraps and starts below the viewport top", () => {
  const ms = [{ row: 1, side: null, start: 0 }, { row: 5, side: null, start: 2 }, { row: 9, side: null, start: 0 }];
  assert.equal(stepMatch(ms, 0, 1), 1);
  assert.equal(stepMatch(ms, 2, 1), 0);
  assert.equal(stepMatch(ms, 0, -1), 2);
  assert.equal(stepMatch([], 0, 1), -1);
  assert.equal(firstMatchFrom(ms, 4), 1);
  assert.equal(firstMatchFrom(ms, 10), 0);
  assert.equal(stepMatch(ms, -1, 1, { row: 5, side: null, start: 1 }), 1);
  assert.equal(stepMatch(ms, -1, -1, { row: 5, side: null, start: 2 }), 0);
});

test("highlight pieces split across text nodes, from the end of each node", () => {
  // "static" "␠" "int" -> match "ic in" spans three nodes
  const pieces = rangesToNodePieces([6, 1, 3], [[4, 9, 0]]);
  assert.deepEqual(pieces, [
    { node: 0, start: 4, end: 6, index: 0 },
    { node: 1, start: 0, end: 1, index: 0 },
    { node: 2, start: 0, end: 2, index: 0 },
  ]);
  const two = rangesToNodePieces([10], [[1, 2, 0], [5, 7, 1]]);
  assert.deepEqual(two.map((p) => p.start), [5, 1]);
  const grouped = groupByRow([{ row: 3, side: null, start: 1, end: 2 }, { row: 3, side: null, start: 4, end: 5 }, { row: 3, side: "r", start: 0, end: 1 }]);
  assert.deepEqual([...grouped.keys()], ["3|", "3|r"]);
  assert.deepEqual(grouped.get("3|"), [[1, 2, 0], [4, 5, 1]]);
});

test("count labels", () => {
  assert.equal(countLabel(0, 12, false), "1/12");
  assert.equal(countLabel(9999, 10000, true), "10000/10000+");
  assert.equal(countLabel(-1, 0, false), "No results");
});
