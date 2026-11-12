import { test } from "node:test";
import assert from "node:assert/strict";
import { scrollToShow, stepIndex, visibleRange } from "../js/components/virtual_list.js";
import { chipCounts, filterRows, findRowBySel, formatChips, parseChips, sectionParamsFromState, sectionStateFromParams, toTsv } from "../js/views/sections/section_model.js";
import { compareReleases, isFinalRelease, seriesOf } from "../js/views/sections/release_order.js";
import { tabRoute } from "../js/tab_routes.js";
import "../js/routes.js";

test("windowing math", () => {
  assert.deepEqual(visibleRange(0, 300, 30, 100000, 8), [0, 19]);
  assert.deepEqual(visibleRange(3000, 300, 30, 100000, 8), [92, 119]);
  assert.deepEqual(visibleRange(0, 300, 30, 5), [0, 5]);
  assert.deepEqual(visibleRange(0, 300, 30, 0), [0, 0]);
  assert.equal(stepIndex("ArrowDown", -1, 10), 0);
  assert.equal(stepIndex("ArrowDown", 9, 10), 9);
  assert.equal(stepIndex("End", 0, 10), 9);
  assert.equal(stepIndex("PageDown", 2, 10, 5), 7);
  assert.equal(stepIndex("x", 2, 10), null);
  assert.equal(scrollToShow(50, 0, 300, 30), 50 * 30 + 30 - 300);
  assert.equal(scrollToShow(2, 300, 300, 30), 60);
  assert.equal(scrollToShow(12, 300, 300, 30), 300);
});

test("filtering and chips", () => {
  const sec = { rowText: (r) => r.name, chips: [{ id: "gpl", label: "GPL", test: (r) => r.gpl }, { id: "srv", label: "server-side" }] };
  const rows = [{ name: "kmalloc", gpl: false }, { name: "kfree_rcu", gpl: true }, { name: "vmalloc", gpl: true }];
  assert.deepEqual(filterRows(sec, rows, "k", new Set()).map((r) => r.name), ["kmalloc", "kfree_rcu"]);
  assert.deepEqual(filterRows(sec, rows, "", parseChips("gpl")).map((r) => r.name), ["kfree_rcu", "vmalloc"]);
  assert.deepEqual(filterRows(sec, rows, "MALLOC", parseChips("gpl,srv")).map((r) => r.name), ["vmalloc"]);
  assert.deepEqual(chipCounts(sec, rows), { gpl: 2 });
  assert.equal(formatChips(new Set(["b", "a"])), "a,b");
  assert.equal(toTsv([{ a: "x\ty", b: 2 }], [["A", (r) => r.a], ["B", (r) => r.b]]), "A\tB\nx y\t2\n");
});

test("section deep links round trip", () => {
  const route = tabRoute("insights");
  const tab = { view: "stable", release: "v3.0", query: "iommu", filter: "cc-stable", sel: "abc", listCollapsed: true };
  const params = new URLSearchParams();
  route.toHash(tab, params);
  assert.equal(params.toString(), "view=stable&r=v3.0&q=iommu&f=cc-stable&sel=abc&lc=1");
  const back = route.fromHash(params);
  for (const k of ["view", "release", "query", "filter", "sel", "listCollapsed"]) assert.equal(back[k], tab[k]);
  assert.deepEqual(sectionStateFromParams(new URLSearchParams("arch=arm")).arch, "arm");
  const p2 = new URLSearchParams();
  sectionParamsFromState({ view: "syscalls", arch: "x86_64" }, p2);
  assert.equal(p2.toString(), "view=syscalls&arch=x86_64");
  assert.notEqual(route.navKey(tab), route.navKey({ ...tab, sel: "other" }));
  assert.equal(route.navKey(tab), route.navKey({ ...tab, query: "x" }));
  assert.ok(tabRoute("api"));
});

test("release ordering on the client", () => {
  const tags = ["v3.1", "v3.0.1", "v3.1-rc1", "v3.0", "v2.6.39.4", "v3.1-rc10", "v3.1-rc2"];
  assert.deepEqual([...tags].sort(compareReleases), ["v2.6.39.4", "v3.0", "v3.0.1", "v3.1-rc1", "v3.1-rc2", "v3.1-rc10", "v3.1"]);
  assert.equal(isFinalRelease("v3.0"), true);
  assert.equal(isFinalRelease("v3.0.1"), false);
  assert.equal(isFinalRelease("v3.1-rc1"), false);
  assert.equal(seriesOf("v2.6.39.4"), "v2.6.39");
  assert.equal(seriesOf("v3.0.5"), "v3.0");
});

test("sel selects by exact key, else by the key's last segment", () => {
  const rows = [{ key: "64:0:read" }, { key: "common:1:write" }, { key: ":2:open" }, { key: "u:mmap_pgoff" }, { key: "plain" }];
  const key = (r) => r.key;
  assert.equal(findRowBySel(rows, key, "common:1:write"), rows[1]);
  assert.equal(findRowBySel(rows, key, "read"), rows[0]);
  assert.equal(findRowBySel(rows, key, "write"), rows[1]);
  assert.equal(findRowBySel(rows, key, "open"), rows[2]);
  assert.equal(findRowBySel(rows, key, "mmap_pgoff"), rows[3]);
  assert.equal(findRowBySel(rows, key, "plain"), rows[4]);
  assert.equal(findRowBySel(rows, key, "pla"), null);
  assert.equal(findRowBySel(rows, key, "x:9:read"), null); // a full key never falls back
  assert.equal(findRowBySel(rows, key, ""), null);
  // An exact key wins over a segment match
  assert.equal(findRowBySel([{ key: "a:read" }, { key: "read" }], key, "read").key, "read");
});
