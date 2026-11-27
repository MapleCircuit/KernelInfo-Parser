/**
 * nodemap_nested.test.mjs - Nested NodeMap nodes: model normalization, static card rendering, breadcrumb
 * stack, insert-picker rows, SVG export of the node and the api.js client shapes (pure modules).
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
// Module singletons start heartbeats when api.js imports state.js: keep them unreffed
for (const k of ["setInterval", "setTimeout"]) {
  const real = globalThis[k];
  globalThis[k] = (...args) => {
    const t = real(...args);
    t?.unref?.();
    return t;
  };
}

import * as M from "../js/views/nodemap/canvas_model.js";
import * as N from "../js/views/nodemap/nested_nodes.js";
import { canvasToSvg } from "../js/views/nodemap/nodemap_export.js";

const card = (over = {}) => ({
  canvas_id: "child", status: "ok", name: "Child <b>map</b>", description: "d", version: "v3.0", revision: 2,
  counts: { nodes: 3, edges: 1, nested: 1 },
  preview: { w: 1000, h: 500, nodes: [{ x: 0, y: 0, w: 100, h: 50, type: "note", color: "" }, { x: 200, y: 100, w: 100, h: 50, type: "struct", color: "red" }], edges: [{ x1: 50, y1: 25, x2: 250, y2: 125, color: "", style: "dashed" }], truncated: false },
  ...over,
});

test("nodemap nodes normalize: canvasId validated, field ports cleared", () => {
  assert.ok(M.NODE_TYPES.includes("nodemap"));
  assert.equal(M.createNode("nodemap", { data: { canvasId: "abc-1_x" } }).data.canvasId, "abc-1_x");
  assert.equal(M.createNode("nodemap", { data: { canvasId: "bad id/../" } }).data.canvasId, "");
  assert.equal(M.createNode("nodemap", {}).title, "Nested NodeMap");
  const a = M.createNode("note", { data: { text: "x" } });
  const b = M.createNode("nodemap", { data: { canvasId: "child" } });
  const c = M.normalizeCanvas({
    ...M.emptyCanvas(), nodes: [a, b],
    edges: [{ id: "e1", from: a.id, to: b.id, fromField: "f", toField: "g" }],
  });
  assert.equal(c.edges.length, 1);
  assert.ok(!c.edges[0].toField, "no field port on the nested end");
  assert.equal(c.edges[0].fromField, "f", "the other end keeps its field");
});

test("nested body shows name, counts, escaped text and an SVG preview without child DOM", () => {
  const node = M.createNode("nodemap", { data: { canvasId: "child" } });
  const html = N.nestedBodyHtml(node, card());
  assert.match(html, /<svg class="nm-nested-svg" viewBox="0 0 1000 500"/);
  assert.match(html, /3 nodes · 1 edge · 1 sub-map/);
  assert.ok(!html.includes("<b>map</b>"));
  assert.match(html, /Child &lt;b&gt;map&lt;\/b&gt;/);
  assert.match(html, /data-act="nested-open"/);
  assert.match(html, /stroke-dasharray/);
  assert.ok(!/nm-node\b/.test(html));
});

test("missing and unset targets render broken / choose states", () => {
  const node = M.createNode("nodemap", { data: { canvasId: "gone" } });
  assert.equal(N.nestedState(node, { status: "missing" }), "missing");
  assert.equal(N.nestedState(node, null), "missing");
  const html = N.nestedBodyHtml(node, { canvas_id: "gone", status: "missing" });
  assert.match(html, /Broken reference/);
  assert.match(html, /data-act="nested-remove"/);
  assert.ok(!/data-act="nested-open"/.test(html));
  const unset = M.createNode("nodemap", {});
  assert.equal(N.nestedState(unset, null), "unset");
  assert.match(N.nestedBodyHtml(unset, null), /Choose NodeMap/);
  const evil = M.createNode("nodemap", { data: { canvasId: "ok" }, customTitle: "<img onerror=x>" });
  assert.ok(!N.nestedBodyHtml(evil, { status: "missing" }).includes("<img"));
});

test("breadcrumb stack pushes, de-duplicates and truncates", () => {
  let st = [];
  st = N.pushCrumb(st, N.crumb("a", "A", "n1"));
  st = N.pushCrumb(st, N.crumb("b", "B", "n2"));
  st = N.pushCrumb(st, N.crumb("c", "C", "n3"));
  assert.deepEqual(st.map((c) => c.canvasId), ["a", "b", "c"]);
  assert.deepEqual(N.truncateCrumbs(st, 1).map((c) => c.canvasId), ["a"]);
  assert.deepEqual(N.truncateCrumbs(st, 0), []);
  assert.equal(N.truncateCrumbs(st, 99).length, 3);
  assert.deepEqual(N.pushCrumb(st, N.crumb("b", "B", "n9")).map((c) => c.nodeId), ["n1", "n9"]);
  const html = N.crumbsHtml(N.crumb("root", "Root <x>"), st);
  assert.match(html, /data-nm-crumb="0"/);
  assert.match(html, /data-nm-crumb="2"/);
  assert.ok(!/data-nm-crumb="3"/.test(html), "the current level is not a button");
  assert.match(html, /aria-current="page"/);
  assert.ok(!html.includes("<x>"));
  assert.equal(N.crumbsHtml(N.crumb("root", "R"), []), "");
});

test("insert picker disables non-insertable canvases and drops the current one", () => {
  const rows = N.pickerRows([
    { id: "cur", name: "Cur", insertable: false },
    { id: "anc", name: "Parent", insertable: false, node_count: 2 },
    { id: "ok", name: "Fine <i>", insertable: true, node_count: 4, edge_count: 1 },
  ], { currentId: "cur" });
  assert.deepEqual(rows.map((r) => r.id), ["anc", "ok"]);
  assert.equal(rows[0].insertable, false);
  assert.equal(rows[0].hint, "would create a cycle");
  const html = N.pickerHtml(rows);
  assert.match(html, /data-pick="anc"[^>]*disabled/);
  assert.match(html, /would create a cycle/);
  assert.ok(!html.includes("<i>"));
});

test("write errors are classified by status", () => {
  assert.equal(N.writeErrorKind({ status: 409 }), "conflict");
  assert.equal(N.writeErrorKind({ status: 403 }), "forbidden");
  assert.equal(N.writeErrorKind({ status: 422 }), "invalid");
  assert.equal(N.writeErrorKind(new Error("boom")), "error");
});

test("SVG export renders a nested node as a titled box", () => {
  const n = M.createNode("nodemap", { x: 10, y: 10, data: { canvasId: "child" } });
  const c = M.normalizeCanvas({ ...M.emptyCanvas(), nodes: [n] });
  const svg = canvasToSvg(c, (v) => v, { nested: () => card() });
  assert.match(svg, /<svg/);
  assert.match(svg, /3 nodes/);
  assert.doesNotThrow(() => canvasToSvg(c, (v) => v));
});

test("api client methods use the documented URLs, methods and bodies", async () => {
  const { api } = await import("../js/api.js");
  const calls = [];
  const client = Object.create(api.constructor.prototype);
  client.request = async (url, opts) => { calls.push([url, opts]); return {}; };
  await client.getNodemapCanvases({ q: "x", insertableInto: "cur" });
  assert.match(calls.at(-1)[0], /insertable_into=cur/);
  await client.createNodemapCanvas({ name: "n", canvas: {} });
  assert.deepEqual([calls.at(-1)[0], calls.at(-1)[1].method], ["/webapi/nodemap/canvases", "POST"]);
  await client.updateNodemapCanvas("a b", { canvas: {}, expected_revision: 3 });
  assert.deepEqual([calls.at(-1)[0], calls.at(-1)[1].method], ["/webapi/nodemap/canvases/a%20b", "PUT"]);
  assert.equal(JSON.parse(calls.at(-1)[1].body).expected_revision, 3);
  await client.deleteNodemapCanvas("a");
  assert.equal(calls.at(-1)[1].method, "DELETE");
  await client.applyNodemapOps("a", [{ op: "remove_nodes", node_ids: ["n"] }], 4);
  assert.equal(calls.at(-1)[0], "/webapi/nodemap/canvases/a/ops");
  assert.equal(JSON.parse(calls.at(-1)[1].body).expected_revision, 4);
  await client.getNodemapHierarchy("a", 3);
  assert.equal(calls.at(-1)[0], "/webapi/nodemap/canvases/a/hierarchy?depth=3");
  await client.insertNodemapNested("a", { target_id: "t" });
  assert.deepEqual([calls.at(-1)[0], calls.at(-1)[1].method], ["/webapi/nodemap/canvases/a/nested", "POST"]);
  await client.groupNodemapNodes("a", { node_ids: ["n"] });
  assert.equal(calls.at(-1)[0], "/webapi/nodemap/canvases/a/group");
});
