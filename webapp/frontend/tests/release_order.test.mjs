import { test } from "node:test";
import assert from "node:assert/strict";
import {
  compareReleases, familyOf, isFinalRelease, parseTag, prefixedFinals, prefixedReleaseOf, releaseKey, releaseTagNames, seriesOf,
} from "../js/views/sections/release_order.js";

test("kernel tags order as before", () => {
  const tags = ["v3.1", "v3.0.2", "v3.1-rc2", "v3.0", "v3.1-rc1", "v3.0.1", "v2.6.39.4", "v2.6.39", "v3.1.1"];
  assert.deepEqual([...tags].sort(compareReleases), ["v2.6.39", "v2.6.39.4", "v3.0", "v3.0.1", "v3.0.2", "v3.1-rc1", "v3.1-rc2", "v3.1", "v3.1.1"]);
  assert.equal(seriesOf("v3.0.5"), "v3.0");
  assert.equal(seriesOf("v2.6.39.4"), "v2.6.39");
  assert.equal(familyOf("v3.1-rc2"), "");
  assert.equal(parseTag("v2.6"), null);
  assert.equal(parseTag("v3.0.1-rc1"), null);
  assert.equal(parseTag("next-20240101"), null);
  assert.equal(parseTag("2.4.0"), null);
});

test("prefixed releases: one family per prefix, numeric order", () => {
  const p = parseTag("man-pages-6.9.1");
  assert.deepEqual(p.series, [1000, "man-pages-", 6, 9]);
  assert.equal(p.stable, 1);
  assert.equal(p.family, "man-pages-");
  assert.equal(familyOf("foo-1.2-rc1"), "foo-");
  assert.equal(parseTag("foo-1.2.3-rc1"), null);
  const tags = ["man-pages-6.10", "man-pages-6.9.1", "man-pages-6.9", "man-pages-6.10-rc1", "man-pages-5.13", "v3.0"];
  assert.deepEqual([...tags].sort(compareReleases), ["v3.0", "man-pages-5.13", "man-pages-6.9", "man-pages-6.9.1", "man-pages-6.10-rc1", "man-pages-6.10"]);
  assert.ok(releaseKey("a-1.0") < releaseKey("b-0.1"));
  assert.equal(seriesOf("man-pages-6.04.1"), "man-pages-6.4");
  assert.ok(isFinalRelease("man-pages-6.9") && !isFinalRelease("man-pages-6.9.1"));
});

test("release tag names: the kernel scheme's when any, else every prefixed one", () => {
  assert.deepEqual(releaseTagNames(["v3.0", "for-linus-6.1", "v3.1-rc1", "master"]), ["v3.0", "v3.1-rc1"]);
  assert.deepEqual(releaseTagNames(["man-pages-6.9", "foo", "man-pages-6.9.1"]), ["man-pages-6.9", "man-pages-6.9.1"]);
});

test("prefixed releases group under their series' final tag", () => {
  const finals = prefixedFinals(["man-pages-6.00", "man-pages-6.9", "man-pages-6.9.1", "man-pages-6.0-rc1"]);
  assert.equal(finals.get("man-pages-6.0"), "man-pages-6.00");
  assert.deepEqual(prefixedReleaseOf("man-pages-6.9.1", finals), { base: "man-pages-6.9", suffix: ".1", isSub: true });
  assert.deepEqual(prefixedReleaseOf("man-pages-6.0-rc1", finals), { base: "man-pages-6.00", suffix: "-rc1", isSub: true });
  assert.deepEqual(prefixedReleaseOf("man-pages-6.9", finals), { base: "man-pages-6.9", suffix: "", isSub: false });
  // Without the final tag, the candidate names its series as spelled
  assert.deepEqual(prefixedReleaseOf("foo-2.1-rc3", new Map()), { base: "foo-2.1", suffix: "-rc3", isSub: true });
  assert.equal(prefixedReleaseOf("v3.0.1", finals), null);
  assert.equal(prefixedReleaseOf("master", finals), null);
});
