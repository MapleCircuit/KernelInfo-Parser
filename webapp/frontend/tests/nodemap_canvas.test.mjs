/**
 * nodemap_canvas.test.mjs - NodeMap canvas document, geometry, groups, selection, layout, edge routing,
 * struct type toggles / #if guard rendering, note Markdown, source rows and SVG export (pure modules).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as M from "../js/views/nodemap/canvas_model.js";
import { anchorPoint, arrowHeads, autoSides, computeEdgeGeometry, dashArray, edgePath, resolveEnds } from "../js/views/nodemap/edge_router.js";
import { countDirectives, findItem, renderStructBody } from "../js/views/nodemap/struct_view.js";
import { renderNoteMarkdown, renderSourceRows } from "../js/views/nodemap/node_content.js";
import { canvasToSvg } from "../js/views/nodemap/nodemap_export.js";
import { contentKey } from "../js/views/nodemap/node_renderer.js";

const memStorage = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), _m: m };
};

test("nodes normalize per type: sizes clamped, payload repaired, default titles", () => {
  const s = M.createNode("struct", { data: { name: "task_struct" }, w: 5, h: 99999 });
  assert.equal(s.w, M.SIZE_LIMITS.struct.minW);
  assert.equal(s.h, M.SIZE_LIMITS.struct.maxH);
  assert.equal(s.title, "struct task_struct");
  assert.equal(s.data.showTypes, true);
  assert.equal(s.data.showGuards, true);
  assert.equal(M.normalizeNode({ type: "bogus" }), null);
  const src = M.createNode("source", { data: { path: "kernel/fork.c", mode: "range", start: 10, end: 3 } });
  assert.equal(src.data.end, 10, "an end before the start collapses onto it");
  assert.equal(src.title, "fork.c:10");
  assert.equal(M.createNode("source", { data: { path: "a/b.c", mode: "symbol", symbol: "f" } }).title, "b.c · f");
  assert.equal(M.createNode("man", { data: { name: "open", section: "2" } }).title, "open(2)");
  const n = M.createNode("note", { color: "not-a-color" });
  assert.equal(n.color, "");
  assert.equal(M.accentVar("green"), "var(--accent-green)");
  assert.equal(M.accentVar(""), "var(--border-color)");
});

test("inline titles: custom title wins, empty resets to the default", () => {
  const n = M.createNode("struct", { data: { name: "page" } });
  assert.equal(M.displayTitle(n), "struct page");
  n.customTitle = "Page frame";
  assert.equal(M.displayTitle(n), "Page frame");
  n.customTitle = "";
  assert.equal(M.displayTitle(n), "struct page");
});

test("canvas JSON round-trips; invalid payloads reset and dangling references are dropped", () => {
  const g = M.createNode("group", { id: "g1", x: 0, y: 0, w: 600, h: 400 });
  const a = M.createNode("struct", { id: "a", x: 40, y: 60, groupId: "g1", data: { name: "x", typeOverrides: { "3:f": true }, collapsed: { "5:u": true } } });
  const b = M.createNode("note", { id: "b", x: 800, y: 60, data: { text: "hi" } });
  const e = M.createEdge({ id: "e1", from: "a", to: "b", fromField: "3:f", fromSide: "right", label: "uses", arrow: "both", style: "dashed", curve: "orthogonal", color: "red" });
  const canvas = { ...M.emptyCanvas(), nodes: [g, a, b], edges: [e], viewport: { x: 10, y: -20, zoom: 1.5 }, settings: { snap: true, grid: 16 } };
  const json = JSON.parse(JSON.stringify(M.serializeCanvas(canvas)));
  const back = M.normalizeCanvas(json);
  assert.deepEqual(back, M.serializeCanvas(canvas));

  const store = memStorage();
  assert.ok(M.saveCanvas(store, "k", canvas));
  assert.deepEqual(M.loadCanvas(store, "k"), back);
  store.setItem("bad", "{not json");
  assert.equal(M.loadCanvas(store, "bad"), null);
  assert.equal(store.getItem("bad"), null, "an unusable payload is removed");
  store.setItem("old", JSON.stringify({ schema: M.CANVAS_SCHEMA, version: 0, nodes: [] }));
  assert.equal(M.loadCanvas(store, "old"), null);

  const repaired = M.normalizeCanvas({
    ...json,
    nodes: [...json.nodes, { type: "note", id: "c", groupId: "missing" }, { type: "group", id: "g2", groupId: "g3" }, { type: "group", id: "g3", groupId: "g2" }],
    edges: [...json.edges, { from: "a", to: "nope" }, { from: "a", to: "a" }],
    viewport: { zoom: 99 },
  });
  assert.equal(repaired.nodes.find((n) => n.id === "c").groupId, null);
  assert.ok(repaired.nodes.filter((n) => n.id === "g2" || n.id === "g3").some((n) => n.groupId === null), "cycles are broken");
  assert.equal(repaired.edges.length, 1);
  assert.equal(repaired.viewport.zoom, M.ZOOM_MAX);
});

test("resizeRect keeps the opposite edge, clamps to limits and snaps", () => {
  const lim = { minW: 100, minH: 50, maxW: 500, maxH: 400 };
  const r = { x: 100, y: 100, w: 200, h: 100 };
  assert.deepEqual(M.resizeRect(r, "se", 30, 20, lim), { x: 100, y: 100, w: 230, h: 120 });
  assert.deepEqual(M.resizeRect(r, "nw", 50, 30, lim), { x: 150, y: 130, w: 150, h: 70 });
  assert.deepEqual(M.resizeRect(r, "w", 500, 0, lim), { x: 200, y: 100, w: 100, h: 100 }, "min width holds, right edge stays");
  assert.deepEqual(M.resizeRect(r, "e", 9999, 0, lim), { x: 100, y: 100, w: 500, h: 100 });
  assert.deepEqual(M.resizeRect(r, "s", 7, 0, lim, 20), { x: 100, y: 100, w: 200, h: 100 });
  assert.deepEqual(M.resizeRect(r, "se", 13, 13, lim, 20), { x: 100, y: 100, w: 220, h: 120 });
  assert.equal(M.snapValue(37, 20), 40);
});

test("marquee selects touched nodes and only enclosed groups", () => {
  const nodes = [
    M.createNode("note", { id: "n1", x: 0, y: 0, w: 120, h: 60 }),
    M.createNode("note", { id: "n2", x: 500, y: 500, w: 120, h: 60 }),
    M.createNode("group", { id: "g", x: 0, y: 0, w: 300, h: 300 }),
  ];
  assert.deepEqual(M.nodesInMarquee(nodes, M.rectFromPoints(100, 50, 10, 10)), ["n1"]);
  assert.deepEqual(M.nodesInMarquee(nodes, { x: -10, y: -10, w: 320, h: 320 }).sort(), ["g", "n1"]);
});

test("align and distribute", () => {
  const ns = [M.createNode("note", { x: 10, y: 0, w: 120, h: 60 }), M.createNode("note", { x: 200, y: 50, w: 160, h: 80 }), M.createNode("note", { x: 600, y: 30, w: 120, h: 60 })];
  M.alignNodes(ns, "left");
  assert.deepEqual(ns.map((n) => n.x), [10, 10, 10]);
  M.alignNodes(ns, "bottom");
  assert.deepEqual(ns.map((n) => n.y + n.h), [130, 130, 130]);
  M.alignNodes(ns, "center");
  assert.equal(new Set(ns.map((n) => n.x + n.w / 2)).size, 1);
  const row = [M.createNode("note", { x: 0, y: 0, w: 120, h: 60 }), M.createNode("note", { x: 130, y: 0, w: 120, h: 60 }), M.createNode("note", { x: 600, y: 0, w: 120, h: 60 })];
  M.distributeNodes(row, "horizontal");
  const gap1 = row[1].x - (row[0].x + row[0].w);
  const gap2 = row[2].x - (row[1].x + row[1].w);
  assert.ok(Math.abs(gap1 - gap2) <= 1);
  assert.equal(row[0].x, 0);
  assert.equal(row[2].x, 600);
});

test("groups: drop in/out changes membership, a group drags its members, removal keeps them", () => {
  const outer = M.createNode("group", { id: "outer", x: 0, y: 0, w: 1000, h: 800 });
  const inner = M.createNode("group", { id: "inner", x: 100, y: 100, w: 400, h: 300 });
  const a = M.createNode("note", { id: "a", x: 150, y: 150, w: 120, h: 60 });
  const b = M.createNode("note", { id: "b", x: 2000, y: 0, w: 120, h: 60 });
  const nodes = [outer, inner, a, b];
  M.reassignGroups(nodes, [inner, a, b]);
  assert.equal(inner.groupId, "outer");
  assert.equal(a.groupId, "inner", "the smallest frame holding the center");
  assert.equal(b.groupId, null);
  assert.deepEqual(M.groupDescendants(nodes, "outer").map((n) => n.id).sort(), ["a", "inner"]);
  assert.deepEqual(M.dragSet(nodes, ["outer"]).map((n) => n.id).sort(), ["a", "inner", "outer"]);
  a.x = 1500;
  assert.deepEqual(M.reassignGroups(nodes, [a]), ["a"]);
  assert.equal(a.groupId, null);
  b.x = 600;
  b.y = 600;
  M.reassignGroups(nodes, [b]);
  assert.equal(b.groupId, "outer");
  assert.equal(M.containingGroup(nodes, outer), null, "a group never contains itself");
  assert.deepEqual(M.paintOrder(nodes).map((n) => n.id).slice(0, 2), ["outer", "inner"]);
  const canvas = { ...M.emptyCanvas(), nodes, edges: [M.createEdge({ from: "inner", to: "b" })] };
  M.removeNodes(canvas, ["outer"]);
  assert.equal(canvas.nodes.find((n) => n.id === "inner").groupId, null);
  assert.equal(canvas.edges.length, 1);
  M.removeNodes(canvas, ["inner"]);
  assert.equal(canvas.edges.length, 0);
});

test("fitViewport centers the bounds", () => {
  const vp = M.fitViewport({ x: 0, y: 0, w: 400, h: 200 }, 880, 480, 40);
  assert.equal(vp.zoom, 1.5, "fit zoom is capped at 150%");
  assert.equal(Math.round(vp.x + 200 * vp.zoom), 440);
});

test("edge routing: anchors, auto sides, curves, arrows, dashes", () => {
  const a = { x: 0, y: 0, w: 100, h: 50 };
  const b = { x: 300, y: 0, w: 100, h: 50 };
  assert.deepEqual(anchorPoint(a, "top"), { x: 50, y: 0 });
  assert.deepEqual(anchorPoint(a, "left"), { x: 0, y: 25 });
  assert.deepEqual(autoSides(a, b), ["right", "left"]);
  assert.deepEqual(autoSides(a, { x: 0, y: 300, w: 100, h: 50 }), ["bottom", "top"]);
  assert.deepEqual(autoSides(b, a), ["left", "right"]);
  const ends = resolveEnds({ rect: a, side: "auto" }, { rect: b, side: "top" });
  assert.equal(ends.toSide, "top");
  assert.deepEqual(ends.p2, { x: 350, y: 0 });
  const pinned = resolveEnds({ rect: a, side: "right", point: { x: 100, y: 40 } }, { rect: b, side: "auto" });
  assert.deepEqual(pinned.p1, { x: 100, y: 40 });
  const straight = edgePath({ x: 0, y: 0 }, { x: 100, y: 0 }, "right", "left", "straight");
  assert.equal(straight.d, "M 0 0 L 100 0");
  assert.deepEqual(straight.mid, { x: 50, y: 0 });
  const ortho = edgePath({ x: 0, y: 0 }, { x: 200, y: 100 }, "right", "left", "orthogonal");
  assert.match(ortho.d, /^M 0 0 L 24 0 L 100 0 L 100 100 L 176 100 L 200 100$/);
  const bez = edgePath({ x: 0, y: 0 }, { x: 200, y: 0 }, "right", "left", "bezier");
  assert.match(bez.d, /^M 0 0 C /);
  assert.equal(Math.round(bez.mid.x), 100);
  assert.equal(arrowHeads(bez, { x: 0, y: 0 }, { x: 200, y: 0 }, "none").length, 0);
  assert.equal(arrowHeads(bez, { x: 0, y: 0 }, { x: 200, y: 0 }, "forward").length, 1);
  assert.equal(arrowHeads(bez, { x: 0, y: 0 }, { x: 200, y: 0 }, "both").length, 2);
  const back = arrowHeads(straight, { x: 0, y: 0 }, { x: 100, y: 0 }, "backward")[0];
  assert.ok(back.startsWith("0,0 "), "the backward head points at the start");
  assert.equal(dashArray("solid"), "");
  assert.equal(dashArray("dashed"), "8 5");
  assert.equal(dashArray("dotted"), "2 4");
  const g = computeEdgeGeometry({ rect: a, side: "auto" }, { rect: b, side: "auto" }, { curve: "straight", arrow: "both" });
  assert.equal(g.heads.length, 2);
  assert.equal(g.fromSide, "right");
});

const LAYOUT = {
  version: "v3.0", name: "foo", kind: "struct", file_path: "include/foo.h", line_s: 10, line_e: 30,
  span_source: "git", guard_source: "source", has_guards: true, outer_conds: ["defined(CONFIG_FOO)"],
  items: [
    { kind: "field", name: "a", type: "int", decl: "int a", line: 11, bits: null, array: null, conds: [] },
    { kind: "directive", directive: "ifdef", expr: "CONFIG_SMP", text: "#ifdef CONFIG_SMP", line: 12, depth: 0 },
    { kind: "field", name: "cpu", type: "unsigned int", decl: "unsigned int cpu", line: 13, bits: 4, array: null, conds: ["defined(CONFIG_SMP)"] },
    { kind: "directive", directive: "else", expr: "", text: "#else", line: 14, depth: 0 },
    { kind: "directive", directive: "endif", expr: "", text: "#endif", line: 16, depth: 0 },
    { kind: "aggregate", tag: "union", name: "u", type_name: "", line: 17, line_e: 22, conds: [], children: [
      { kind: "field", name: "p", type: "void *", decl: "void *p", line: 18, bits: null, array: null, conds: [] },
      { kind: "directive", directive: "if", expr: "BITS_PER_LONG == 64", text: "#if BITS_PER_LONG == 64", line: 19, depth: 0 },
      { kind: "field", name: "q", type: "u64", decl: "u64 q", line: 20, bits: null, array: "[2]", conds: ["(BITS_PER_LONG == 64)"] },
      { kind: "directive", directive: "endif", expr: "", text: "#endif", line: 21, depth: 0 },
    ] },
  ],
};

test("struct body: struct-wide and per-member type toggles, guard rows on/off", () => {
  const node = M.createNode("struct", { data: { name: "foo" } });
  const d = node.data;
  assert.equal(countDirectives(LAYOUT.items), 5);
  let html = renderStructBody(LAYOUT, d);
  assert.match(html, /#ifdef CONFIG_SMP/);
  assert.match(html, /#if BITS_PER_LONG == 64/);
  assert.match(html, /nm-sf-type">unsigned int/);
  assert.match(html, /Built when: defined\(CONFIG_SMP\)/);
  assert.match(html, /defined\(CONFIG_FOO\)/, "outer conditions are shown");
  assert.match(html, / : 4/);
  assert.match(html, /\[2\]/);

  d.showGuards = false;
  html = renderStructBody(LAYOUT, d);
  assert.doesNotMatch(html, /#ifdef CONFIG_SMP/);
  assert.match(html, /nm-sf-guard/, "guarded members keep a marker");

  M.setAllTypes(d, false);
  html = renderStructBody(LAYOUT, d);
  assert.doesNotMatch(html, /nm-sf-type">int/);
  const cpu = findItem(LAYOUT.items, "13:cpu");
  assert.equal(M.toggleItemType(d, cpu), true);
  html = renderStructBody(LAYOUT, d);
  assert.match(html, /nm-sf-type">unsigned int/);
  assert.doesNotMatch(html, /nm-sf-type">void \*/);
  // An aggregate flips with every member inside it
  const u = findItem(LAYOUT.items, "17:u");
  M.toggleItemType(d, u);
  assert.equal(M.typeVisible(d, "18:p"), true);
  assert.equal(M.typeVisible(d, "20:q"), true);
  assert.equal(M.typeVisible(d, "11:a"), false);
  M.toggleItemType(d, u);
  assert.deepEqual(Object.keys(d.typeOverrides), ["13:cpu"], "back to the struct-wide value drops the override");
  M.setAllTypes(d, true);
  assert.deepEqual(d.typeOverrides, {});

  d.collapsed["17:u"] = true;
  html = renderStructBody(LAYOUT, d);
  assert.doesNotMatch(html, /data-key="18:p"/);
  assert.match(html, /aria-expanded="false"/);
});

test("struct body escapes source text", () => {
  const evil = { ...LAYOUT, file_path: "<x>", outer_conds: [], items: [{ kind: "field", name: "<img src=x onerror=1>", type: "int", decl: "", line: 1, bits: null, array: null, conds: [] }] };
  const html = renderStructBody(evil, M.createNode("struct", { data: { name: "foo" } }).data);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
});

test("note markdown: structure, emphasis, escaping, no links or images", () => {
  const html = renderNoteMarkdown("# Title\n- one **bold**\n- [x] done\n1. first\n> quote\n`a<b`\n```\n<code>\n```\n[l](javascript:alert(1)) ![i](http://x/y.png)");
  assert.match(html, /nm-note-h1">Title/);
  assert.match(html, /<ul><li>one <strong>bold<\/strong><\/li><li class="nm-note-task done">/);
  assert.match(html, /<ol><li>first<\/li><\/ol>/);
  assert.match(html, /<blockquote>quote<\/blockquote>/);
  assert.match(html, /<code>a&lt;b<\/code>/);
  assert.match(html, /<pre class="nm-note-code">&lt;code&gt;<\/pre>/);
  assert.doesNotMatch(html, /<a |<img|javascript:alert\(1\)"/);
  assert.match(renderNoteMarkdown(""), /Double-click/);
});

test("source rows carry file line numbers and highlight the symbol span", () => {
  const html = renderSourceRows(["static int x;", "int f(void)", "{", "}"], 40, "kernel/a.c", new Set([41, 42]));
  assert.match(html, /nm-src-no">40</);
  assert.match(html, /nm-src-no">43</);
  assert.equal((html.match(/nm-src-hl/g) || []).length, 2);
  assert.match(html, /tok-/, "the code view highlighter colors the tokens");
});

test("content key ignores geometry and titles, follows payload and revision", () => {
  const n = M.createNode("tab", { data: { tabType: "commits", hash: "v=v3.0&tab=commits" }, version: "v3.0" });
  const k = contentKey(n);
  n.x += 100;
  n.w += 50;
  n.customTitle = "x";
  n.color = "red";
  assert.equal(contentKey(n), k, "moving or restyling keeps the live iframe");
  n.version = "v3.1";
  assert.notEqual(contentKey(n), k);
});

test("embed addresses and tab types", () => {
  assert.equal(M.embedSrc("#v=v3.0&tab=commits"), "/?embed=1#v=v3.0&tab=commits");
  assert.equal(M.tabTypeOfHash("v=v3.0&tab=kconfig&arch=x86"), "kconfig");
  assert.equal(M.tabTypeOfHash(""), "");
});

test("SVG export draws groups, nodes, styled edges and labels with resolved colors", () => {
  const a = M.createNode("struct", { id: "a", x: 0, y: 0, data: { name: "a" }, color: "green" });
  const b = M.createNode("note", { id: "b", x: 600, y: 0, data: { text: "hello <b>" } });
  const g = M.createNode("group", { id: "g", x: -40, y: -60, w: 1200, h: 600, customTitle: "Core" });
  const canvas = { ...M.emptyCanvas(), nodes: [a, b, g], edges: [M.createEdge({ from: "a", to: "b", label: "ptr", arrow: "both", style: "dotted" })] };
  const svg = canvasToSvg(canvas, (c) => (c.startsWith("var(") ? "#123456" : c));
  assert.match(svg, /^<svg /);
  assert.match(svg, /stroke-dasharray="2 4"/);
  assert.equal((svg.match(/<polygon/g) || []).length, 2);
  assert.match(svg, />ptr</);
  assert.match(svg, />Core</);
  assert.match(svg, /hello &lt;b&gt;/);
  assert.doesNotMatch(svg, /var\(--/);
  assert.ok(svg.indexOf(">Core<") < svg.indexOf(">struct a<"), "groups paint below nodes");
});
