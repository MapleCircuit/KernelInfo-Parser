import { test } from "node:test";
import assert from "node:assert/strict";
import { compareHtml, deltaText, historySvg } from "../js/views/pahole/pahole_compare.js";

const RES = {
  a: { total_size: 24, cache_lines_used: 1 }, b: { total_size: 72, cache_lines_used: 2 },
  delta: { total_size: 48, holes_count: -1, cache_lines_used: 1 }, counts: { same: 2, added: 1 }, is_estimate: false,
  members: [{ name: "a<b", status: "same", a: { offset: 0, size: 4, type: "int" }, b: { offset: 0, size: 4, type: "int" } },
    { name: "x", status: "added", a: null, b: { offset: 4, size: 4, type: "int" } }],
};

test("compare rendering", () => {
  assert.equal(deltaText(RES), "+48 bytes, −1 hole, cachelines 1 → 2");
  const html = compareHtml(RES, "v3.0", "v4.0");
  assert.match(html, /pcmp-added/);
  assert.match(html, /a&lt;b/);
  assert.match(html, /1 added/);
});

test("history chart", () => {
  const svg = historySvg([{ version: "v1", available: true, total_size: 16, holes_count: 0, cache_lines_used: 1 },
    { version: "v2", available: false }, { version: "v3", available: true, total_size: 24, holes_count: 1, cache_lines_used: 1, is_estimate: true }]);
  assert.equal((svg.match(/<circle/g) || []).length, 2, "one point per available stop");
  assert.match(svg, /fill="var\(--accent-red\)"[^>]*>v2</);
  assert.equal(historySvg([]), "");
});
