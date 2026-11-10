import { test } from "node:test";
import assert from "node:assert/strict";
import { mergedListHtml, mergedSummary } from "../js/views/mail/merged_render.js";

test("merged badge", () => {
  assert.equal(mergedSummary({ commits: [], source: "db" }).label, "Not merged");
  const res = { source: "db", commits: [{ hash: "a".repeat(40), subject: "<x>", patch_idx: 1, patch_total: 5, released: { released: true, first_release: "v3.1" } }],
    series: { total: 5, merged: 4, first_release: "v3.1" } };
  assert.deepEqual(mergedSummary(res), { label: "Merged 4/5 · v3.1", cls: "partial", title: mergedSummary(res).title });
  const html = mergedListHtml(res);
  assert.ok(html.includes("&lt;x&gt;"));
  assert.ok(html.includes("[1/5]"));
  assert.ok(html.includes("v3.1"));
});
