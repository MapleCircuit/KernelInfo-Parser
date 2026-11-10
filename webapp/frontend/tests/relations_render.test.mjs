import { test } from "node:test";
import assert from "node:assert/strict";
import { relationChipsHtml, relationsCount, relationsHtml, releaseBadgeHtml, releaseLabel } from "../js/views/commits/relations_render.js";

test("release labels and badges", () => {
  assert.equal(releaseLabel({ released: true, first_tag: "v3.1-rc1", first_release: "v3.1" }), "v3.1-rc1 → v3.1");
  assert.equal(releaseLabel({ released: true, first_tag: "v3.1", first_release: "v3.1" }), "v3.1");
  assert.equal(releaseLabel({ released: true, first_stable: "v3.0.5" }), "v3.0.5 (stable)");
  assert.equal(releaseLabel({ released: false }), "not released yet");
  assert.match(releaseBadgeHtml({ released: true, first_tag: "v3.1-rc1", first_release: "v3.1" }), /data-tag="v3.1"/);
  assert.match(releaseBadgeHtml({ released: false, branches: ["origin/master"] }), /Not released yet/);
});

test("relations card escapes subjects and shows unresolved prefixes", () => {
  const html = relationsHtml({
    fixes: [{ sha: null, prefix: "deadbeef1234", subject: "<script>x</script>", resolved: false }],
    fixed_by: [{ sha: "a".repeat(40), subject: "fix <b>", author: "Ann", released: { released: true, first_tag: "v3.2-rc3", first_release: "v3.2" } }],
    reverts: [], reverted_by: [], upstream: [], backports: [],
    stable_cc: ["3.0+"], links: ["x@y"],
  });
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(html.includes("deadbeef1234"));
  assert.ok(html.includes("fix &lt;b&gt;"));
  assert.ok(html.includes("v3.2-rc3 → v3.2"));
  assert.ok(html.includes("3.0+"));
  assert.ok(html.includes('data-mid="x@y"'));
  assert.equal(relationsCount({ fixes: [1], fixed_by: [1, 2], stable_cc: ["x"] }), 4);
  assert.equal(relationsHtml({ fixes: [], fixed_by: [], stable_cc: [], links: [] }), "");
});

test("timeline chips", () => {
  assert.match(relationChipsHtml({ fixed_by: 2, reverted_by: 0, fixes: 0, backports: 0 }), /fixed later ×2/);
  assert.match(relationChipsHtml({ reverted_by: 1 }), /reverted/);
  assert.equal(relationChipsHtml({ fixed_by: 0 }), "");
  assert.equal(relationChipsHtml(null), "");
});
