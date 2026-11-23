/**
 * nodemap_canvas.test.mjs - NodeMap canvas document, geometry, groups, selection, layout, edge routing,
 * struct type toggles / #if guard rendering, note Markdown, source rows and SVG export (pure modules).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as M from "../js/views/nodemap/canvas_model.js";
import { anchorPoint, arrowHeads, autoSides, computeEdgeGeometry, dashArray, edgePath, resolveEnds } from "../js/views/nodemap/edge_router.js";
import { branchTruths, countDirectives, findItem, guardAutoCollapsed, guardTree, renderStructBody, structRows } from "../js/views/nodemap/struct_view.js";
import { DirectiveEvaluator } from "../js/views/kconfig/directive_evaluator.js";
import { headMetaOf, renderNoteMarkdown, renderSourceRows } from "../js/views/nodemap/node_content.js";
import { canvasToSvg, captureNodeForeignObject, pngSize, svgSize } from "../js/views/nodemap/nodemap_export.js";
import { contentKey, nodeBox } from "../js/views/nodemap/node_renderer.js";
import { htmlSegments, sourceLineSegments } from "../js/views/nodemap/node_content.js";
import * as D from "../js/views/nodemap/domain_nodes.js";
import { embedSessionChannel, embedSessionId } from "../js/utils/embed_mode.js";
import { TAB_DRAG_MIME, TAB_DRAG_TEXT_PREFIX, mayCarryTab, tabDragPayload } from "../js/utils/tab_drag.js";

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
  const canvas = { ...M.emptyCanvas(), nodes: [g, a, b], edges: [e], viewport: { x: 10, y: -20, zoom: 1.5 }, settings: M.normalizeSettings({ snap: true, grid: 16 }) };
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

// ------------------------------------------------------------------ NodeMap upgrade: schema, struct rows, export, clipboard

test("settings: Edit Mode off, Kconfig folding on, auto engine by default", () => {
  assert.deepEqual(M.emptyCanvas().settings, {
    snap: false, grid: M.GRID_DEFAULT, editMode: false, kconfigCollapse: true, kconfigEngine: "", bgColor: "",
    embedSessions: [{ id: "nms-1", name: "Canvas Session 1" }], followSession: "nms-1", layers: [], renderQuality: "auto",
  });
  const st = M.normalizeSettings({ editMode: true, kconfigCollapse: false, kconfigEngine: "tab-7", grid: 2 });
  assert.equal(st.editMode, true);
  assert.equal(st.kconfigCollapse, false);
  assert.equal(st.kconfigEngine, "tab-7");
  assert.equal(st.grid, 4);
  assert.equal(M.normalizeSettings({ editMode: "yes" }).editMode, false, "only a real boolean turns it on");
});

test("custom #rrggbb colors normalize, persist and resolve; presets keep their tokens", () => {
  const n = M.createNode("note", { color: "#A1B2C3" });
  assert.equal(n.color, "#a1b2c3");
  assert.equal(M.createNode("note", { color: "#abc" }).color, "", "short hex is not a stored color");
  assert.equal(M.createNode("note", { color: "url(x)" }).color, "");
  assert.equal(M.createNode("note", { color: "green" }).color, "green");
  const e = M.createEdge({ from: "a", to: "b", color: "#00ff7F" });
  assert.equal(e.color, "#00ff7f");
  assert.equal(M.accentVar("#a1b2c3"), "#a1b2c3");
  assert.equal(M.accentVar("cyan"), "var(--accent-cyan)");
  assert.equal(M.accentVar("", "var(--x)"), "var(--x)");
  const canvas = { ...M.emptyCanvas(), nodes: [n, M.createNode("note", { id: "b" })], edges: [] };
  assert.equal(M.normalizeCanvas(M.serializeCanvas(canvas)).nodes[0].color, "#a1b2c3", "custom colors are part of the canvas JSON");
  assert.equal(M.colorToHex("#abc"), "#aabbcc");
  assert.equal(M.colorToHex("rgb(255, 0, 16)"), "#ff0010");
  assert.equal(M.colorToHex("rgba(1,2,3,0.5)"), "#010203");
  assert.equal(M.colorToHex("#11223344"), "#112233");
  assert.equal(M.colorToHex("tomato"), null);
});

test("auto-fit: natural size, aspect-locked scaling, inspector sizes, off again", () => {
  const n = M.createNode("struct", { data: { name: "s" }, w: 300, h: 200 });
  assert.deepEqual([n.autoFit, n.scale, n.baseW, n.baseH], [false, 1, 300, 200]);
  assert.equal(M.createNode("tab", { autoFit: true }).autoFit, false, "live tab iframes cannot be fitted");
  assert.equal(M.createNode("group", { autoFit: true }).autoFit, false);
  M.setAutoFit(n, true);
  assert.equal(M.applyNaturalSize(n, 420, 900), true);
  assert.deepEqual([n.baseW, n.baseH, n.w, n.h], [420, 900, 420, 900], "the whole content fits, past the type's usual max height");
  assert.equal(M.applyNaturalSize(n, 420, 900), false, "same size, nothing changes");
  // Dragging the east handle by +210 doubles the width: the height follows, the content scales
  const r = M.scaleResize(M.rectOf(n), "e", 210, 0, n);
  assert.deepEqual(r, { x: n.x, y: n.y, w: 630, h: 1350, scale: 1.5 });
  // A corner scales by the axis that moved more; the north-west corner keeps the bottom right in place
  const nw = M.scaleResize({ x: 100, y: 100, w: 420, h: 900 }, "nw", 210, 0, n);
  assert.equal(nw.scale, 0.5);
  assert.deepEqual([nw.x + nw.w, nw.y + nw.h], [520, 1000]);
  assert.equal(M.scaleResize(M.rectOf(n), "s", 0, -100000, n).scale, M.SCALE_MIN);
  Object.assign(n, r);
  assert.equal(Math.round((n.w / n.h) * 1000), Math.round((n.baseW / n.baseH) * 1000), "aspect ratio is locked");
  M.applyNaturalSize(n, 200, 100);
  assert.deepEqual([n.w, n.h], [300, 150], "new content keeps the scale");
  M.setScaledSize(n, "h", 300);
  assert.deepEqual([n.scale, n.w, n.h], [3, 600, 300]);
  const back = M.normalizeNode(JSON.parse(JSON.stringify(n)));
  assert.deepEqual([back.autoFit, back.baseW, back.baseH, back.scale, back.w, back.h], [true, 200, 100, 3, 600, 300], "persisted");
  M.setAutoFit(n, false);
  assert.deepEqual([n.autoFit, n.scale, n.w, n.h], [false, 3, 600, 300], "off keeps the content scale and the on-screen box");
  assert.deepEqual(M.contentBox(n), { w: 200, h: 100, s: 3 }, "the content still lays out at its scaled-down box");
  assert.ok(n.w <= M.SIZE_LIMITS.struct.maxW && n.h <= M.SIZE_LIMITS.struct.maxH, "back within the type's limits");
});

test("cloneSubgraph: fresh ids, groups bring their members, internal edges only", () => {
  const g = M.createNode("group", { id: "g", x: 0, y: 0, w: 600, h: 400 });
  const a = M.createNode("note", { id: "a", x: 20, y: 40, groupId: "g", data: { text: "a" } });
  const b = M.createNode("struct", { id: "b", x: 300, y: 40, groupId: "g", data: { name: "b", hiddenItems: { "3:x": true } } });
  const c = M.createNode("note", { id: "c", x: 900, y: 40, data: { text: "c" } });
  const canvas = { ...M.emptyCanvas(), nodes: [g, a, b, c], edges: [M.createEdge({ id: "e1", from: "a", to: "b", label: "in" }), M.createEdge({ id: "e2", from: "b", to: "c" })] };
  const out = M.cloneSubgraph(canvas, ["g"], { x: 10, y: 20 });
  assert.equal(out.nodes.length, 3, "the group and both members");
  const ids = new Set(out.nodes.map((n) => n.id));
  for (const id of ["g", "a", "b", "c"]) assert.ok(!ids.has(id), "fresh ids");
  const ng = out.nodes.find((n) => n.type === "group");
  assert.ok(out.nodes.filter((n) => n.type !== "group").every((n) => n.groupId === ng.id), "members follow the copied group");
  assert.deepEqual([ng.x, ng.y], [10, 20]);
  assert.equal(out.edges.length, 1, "only the edge between copied nodes");
  assert.equal(out.edges[0].label, "in");
  assert.ok(ids.has(out.edges[0].from) && ids.has(out.edges[0].to));
  assert.notEqual(out.edges[0].id, "e1");
  const nb = out.nodes.find((n) => n.type === "struct");
  nb.data.hiddenItems["9:y"] = true;
  assert.deepEqual(b.data.hiddenItems, { "3:x": true }, "a deep copy");
  assert.equal(canvas.nodes.length, 4, "the canvas is not changed");
  const solo = M.cloneSubgraph(canvas, ["a"], { x: 24, y: 24 });
  assert.equal(solo.nodes[0].groupId, null, "a member copied without its group leaves it");
  assert.equal(solo.edges.length, 0);

  const clip = M.normalizeClipboard(JSON.parse(JSON.stringify(M.clipboardPayload(canvas, ["a", "b"], "tab-1"))));
  assert.equal(clip.nodes.length, 2);
  assert.equal(clip.edges.length, 1);
  assert.equal(clip.origin, "tab-1");
  assert.equal(M.normalizeClipboard({ schema: "nope", nodes: [] }), null);
  assert.equal(M.normalizeClipboard({ schema: M.CLIPBOARD_SCHEMA, nodes: [] }), null);
});

const NESTED = {
  version: "v3.0", name: "n", kind: "struct", file_path: "include/n.h", line_s: 1, line_e: 30, outer_conds: [],
  items: [
    { kind: "field", name: "a", type: "int", line: 2, conds: [] },
    { kind: "directive", directive: "ifdef", expr: "CONFIG_A", text: "#ifdef CONFIG_A", line: 3, depth: 0 },
    { kind: "field", name: "b", type: "long", line: 4, conds: ["defined(CONFIG_A)"] },
    { kind: "directive", directive: "if", expr: "defined(CONFIG_B)", text: "#if defined(CONFIG_B)", line: 5, depth: 1 },
    { kind: "field", name: "c", type: "char", line: 6, conds: [] },
    { kind: "directive", directive: "elif", expr: "IS_ENABLED(CONFIG_C)", text: "#elif IS_ENABLED(CONFIG_C)", line: 7, depth: 1 },
    { kind: "field", name: "d", type: "u8", line: 8, conds: [] },
    { kind: "directive", directive: "else", expr: "", text: "#else", line: 9, depth: 1 },
    { kind: "field", name: "e", type: "u16", line: 10, conds: [] },
    { kind: "directive", directive: "endif", expr: "", text: "#endif", line: 11, depth: 1 },
    { kind: "directive", directive: "else", expr: "", text: "#else", line: 12, depth: 0 },
    { kind: "field", name: "f", type: "u32", line: 13, conds: [] },
    { kind: "directive", directive: "endif", expr: "", text: "#endif", line: 14, depth: 0 },
    { kind: "directive", directive: "ifdef", expr: "FOO_UNKNOWN", text: "#ifdef FOO_UNKNOWN", line: 15, depth: 0 },
    { kind: "aggregate", tag: "union", name: "u", type_name: "", line: 16, conds: [], children: [
      { kind: "field", name: "p", type: "void *", line: 17, conds: [] },
      { kind: "field", name: "q", type: "u64", line: 18, conds: [], array: "[2]" },
    ] },
    { kind: "directive", directive: "endif", expr: "", text: "#endif", line: 20, depth: 0 },
  ],
};
const kcfg = () => new DirectiveEvaluator({ CONFIG_A: "y", A: "y", CONFIG_C: "y", C: "y" }, { partial: false });
const structData = (over = {}) => M.createNode("struct", { data: { name: "n", ...over } }).data;
const keysOf = (rows) => rows.map((r) => r.key);

test("#if blocks: nesting, #elif / #else branches and continuous scope bars", () => {
  const tree = guardTree(NESTED.items);
  assert.equal(tree.length, 3, "a field and two top-level blocks");
  assert.deepEqual(tree[1].branches.map((b) => b.key), ["3:ifdef", "12:else"]);
  assert.equal(tree[1].branches[0].items[1].branches.length, 3, "#if / #elif / #else nest inside");
  assert.equal(tree[1].end.line, 14);
  const { rows, tracks } = structRows(NESTED, structData());
  assert.equal(tracks, 2, "two nesting levels, two bar tracks");
  const at = (key) => rows.find((r) => r.key === key);
  assert.deepEqual(at("2:a").bars, [], "outside every block");
  assert.deepEqual(at("3:ifdef").bars, [[0, "start"]]);
  assert.deepEqual(at("4:b").bars, [[0, "full"]]);
  assert.deepEqual(at("5:if").bars, [[0, "full"], [1, "start"]]);
  assert.deepEqual(at("8:d").bars, [[0, "full"], [1, "full"]]);
  assert.deepEqual(at("9:else").bars, [[0, "full"], [1, "mid"]]);
  assert.deepEqual(at("11:endif").bars, [[0, "full"], [1, "end"]]);
  assert.deepEqual(at("12:else").bars, [[0, "mid"]]);
  assert.deepEqual(at("14:endif").bars, [[0, "end"]]);
  assert.deepEqual(at("17:p").bars, [[0, "full"]], "aggregate members inside a block keep its track");
  // Every row between an opening line and its #endif crosses track 0: the bar never breaks
  const from = rows.findIndex((r) => r.key === "3:ifdef");
  const to = rows.findIndex((r) => r.key === "14:endif");
  assert.ok(rows.slice(from, to + 1).every((r) => r.bars.some(([l]) => l === 0)));
  const html = renderStructBody(NESTED, structData());
  assert.match(html, /nm-pp-bar nm-pp-start nm-pp-l0/);
  assert.match(html, /nm-pp-bar nm-pp-end nm-pp-l1/);
  assert.match(html, /--nm-pp-tracks:2/);
  const off = structRows(NESTED, structData({ showGuards: false }));
  assert.equal(off.tracks, 0, "no directive lines, no bars");
  assert.ok(!off.rows.some((r) => r.kind === "pp"));
});

test("Kconfig folding: false branches fold, true and unknown stay open, manual toggles win, per-node off", () => {
  const ev = kcfg();
  const tree = guardTree(NESTED.items);
  assert.deepEqual(branchTruths(tree[1], ev), [1, 0], "#ifdef CONFIG_A built, its #else not");
  assert.deepEqual(branchTruths(tree[1].branches[0].items[1], ev), [0, 1, 0], "#if false, #elif true, #else not reached");
  assert.deepEqual(branchTruths(tree[2], ev), [-1], "a macro the engine does not know is unknown");
  const d = structData();
  let { rows } = structRows(NESTED, d, { evaluator: ev });
  const at = (key) => rows.find((r) => r.key === key);
  assert.equal(at("5:if").kind, "summary");
  assert.equal(at("5:if").count, 1);
  assert.equal(at("5:if").auto, true);
  assert.equal(at("6:c"), undefined, "members of a folded branch are not drawn");
  assert.equal(at("7:elif").kind, "pp", "the true branch stays open");
  assert.ok(at("8:d"));
  assert.equal(at("9:else").kind, "summary");
  assert.equal(at("12:else").kind, "summary");
  assert.equal(at("15:ifdef").kind, "pp", "unknown stays open");
  assert.ok(at("17:p"));
  let html = renderStructBody(NESTED, d, { evaluator: ev });
  assert.match(html, /1 member · compiled out/);
  assert.match(html, /data-act="guard-fold" data-key="5:if"/);
  // A manual unfold overrides the engine; a manual fold works on any branch
  assert.equal(guardAutoCollapsed(NESTED, "5:if", ev), true);
  assert.equal(guardAutoCollapsed(NESTED, "7:elif", ev), false);
  M.toggleGuardCollapse(d, "5:if", true);
  assert.deepEqual(d.collapsedGuards, { "5:if": false });
  M.toggleGuardCollapse(d, "7:elif", false);
  rows = structRows(NESTED, d, { evaluator: ev }).rows;
  assert.ok(rows.find((r) => r.key === "6:c"), "unfolded by hand");
  assert.ok(rows.find((r) => r.key === "6:c").dead, "still marked as compiled out");
  assert.equal(rows.find((r) => r.key === "7:elif").kind, "summary", "folded by hand");
  assert.equal(rows.find((r) => r.key === "7:elif").auto, false);
  M.toggleGuardCollapse(d, "5:if", true);
  assert.deepEqual(d.collapsedGuards, { "7:elif": true }, "back to the engine's choice drops the override");
  // No evaluator (no engine, or the node disables it): nothing folds on its own
  rows = structRows(NESTED, structData(), { evaluator: null }).rows;
  assert.ok(!rows.some((r) => r.kind === "summary"));
  html = renderStructBody(NESTED, structData({ showGuards: false }), { evaluator: ev });
  assert.match(html, /nm-sf-ppsum/, "a folded branch still shows its summary when guard lines are hidden");
  assert.doesNotMatch(html, /data-key="6:c"/);
});

test("hiding members, aggregates and #if blocks: dimmed in Edit Mode, left out in View Mode", () => {
  const d = structData();
  M.toggleHidden(d, "2:a");
  M.toggleHidden(d, "16:u");
  M.toggleHidden(d, "3:ifdef");
  const view = structRows(NESTED, d, { editMode: false });
  assert.deepEqual(keysOf(view.rows), ["15:ifdef", "20:endif"], "the hidden field, block and aggregate subtree are gone");
  assert.equal(view.hidden, 3);
  const edit = structRows(NESTED, d, { editMode: true });
  const at = (key) => edit.rows.find((r) => r.key === key);
  assert.equal(at("2:a").hidden, true);
  assert.equal(at("16:u").hidden, true);
  assert.equal(at("17:p").dim, true, "members of a hidden aggregate dim with it");
  assert.equal(at("17:p").hidden, false);
  assert.equal(at("4:b").dim, true, "members of a hidden block dim with it");
  const html = renderStructBody(NESTED, d, { editMode: true });
  assert.match(html, /data-act="hide-item" data-key="2:a" title="Restore/);
  assert.match(html, /data-act="hide-item" data-key="3:ifdef"/, "a block hides from its opening line");
  assert.match(html, /3 hidden/);
  const viewHtml = renderStructBody(NESTED, d, { editMode: false });
  assert.doesNotMatch(viewHtml, /data-key="2:a"|data-key="17:p"/);
  assert.equal(M.showAllHidden(d), 3);
  assert.equal(structRows(NESTED, d).rows.length, structRows(NESTED, structData()).rows.length);
  // A hidden block with guard lines off still offers its restore button in Edit Mode
  const d2 = structData({ showGuards: false });
  M.toggleHidden(d2, "3:ifdef");
  assert.match(renderStructBody(NESTED, d2, { editMode: true }), /data-act="hide-item" data-key="3:ifdef"/);
});

test("Edit Mode gates editing buttons; folds stay in View Mode", () => {
  const view = renderStructBody(NESTED, structData(), { editMode: false });
  assert.doesNotMatch(view, /data-act="toggle-type"|data-act="hide-item"|data-act="all-types"|data-act="guards"/);
  assert.match(view, /data-act="collapse" data-key="16:u"/, "aggregate folds work in View Mode");
  assert.match(view, /data-act="guard-fold"/, "branch folds work in View Mode");
  const edit = renderStructBody(NESTED, structData(), { editMode: true });
  assert.match(edit, /data-act="toggle-type" data-key="2:a"/);
  assert.match(edit, /data-act="hide-item" data-key="2:a"/);
  assert.match(edit, /data-act="all-types"/);
  assert.match(edit, /data-act="guards"/);
});

test("struct styles: list rows inline, table rows with Type and Symbol cells", () => {
  const list = renderStructBody(NESTED, structData());
  assert.match(list, /nm-style-list/);
  assert.doesNotMatch(list, /nm-sf-cell/);
  const d = structData({ style: "table" });
  assert.equal(d.style, "table");
  assert.equal(structData({ style: "grid" }).style, "list");
  const table = renderStructBody(NESTED, d);
  assert.match(table, /nm-style-table/);
  assert.match(table, /<span class="nm-sf-type nm-sf-cell">long<\/span><span class="nm-sf-name nm-sf-cell">b<\/span>/);
  assert.match(table, /<span class="nm-sf-name nm-sf-cell">q<span class="nm-sf-suffix">\[2\]<\/span>/);
  assert.match(table, /nm-sf-type nm-sf-cell"><span class="nm-sf-tag">union/, "aggregates fill both columns");
  assert.match(table, /--nm-type-col:\d+ch/);
  M.setAllTypes(d, false);
  assert.match(renderStructBody(NESTED, d), /<span class="nm-sf-type nm-sf-cell"><\/span><span class="nm-sf-name nm-sf-cell">b</, "a hidden type keeps its empty cell");
  assert.match(renderStructBody(NESTED, d), /nm-port-left[^>]*data-field="4:b"/, "field ports stay on both sides");
});

test("SVG export: whole canvas, struct rows and bars, folds, hidden items, scale, colors, field edges, tab cards", () => {
  const s = M.createNode("struct", { id: "s", x: 0, y: 0, w: 360, h: 500, color: "#ff8800", data: { name: "n", style: "table", hiddenItems: { "2:a": true } } });
  const t = M.createNode("tab", { id: "t", x: 3000, y: 2000, data: { tabType: "commits", hash: "v=v3.0&tab=commits" } });
  const src = M.createNode("source", { id: "src", x: 600, y: 0, data: { path: "kernel/fork.c" } });
  const note = M.createNode("note", { id: "no", x: -800, y: -300, w: 200, data: { text: "line one\nline two is long enough to wrap around the card" } });
  const fit = M.createNode("symbol", { id: "f", x: 600, y: 600, data: { name: "x", expanded: true, constituents: [{ name: "m1" }] } });
  M.setAutoFit(fit, true);
  M.applyNaturalSize(fit, 200, 100);
  Object.assign(fit, M.scaleResize(M.rectOf(fit), "e", 200, 0, fit));
  const canvas = {
    ...M.emptyCanvas(),
    nodes: [s, t, src, note, fit],
    edges: [M.createEdge({ from: "s", to: "src", fromField: "8:d", fromSide: "right", color: "#00aa11" }), M.createEdge({ from: "s", to: "t", fromField: "2:a", fromSide: "right" })],
  };
  const payloads = { s: NESTED, src: { path: "kernel/fork.c", start_line: 1, end_line: 2, total_lines: 2, lines: ["int x;", "\tint <y>;"] } };
  const resolve = (c) => (c.startsWith("var(") ? "#123456" : c);
  const svg = canvasToSvg(canvas, resolve, { content: (n) => payloads[n.id] || null, structOpts: () => ({ editMode: false, evaluator: kcfg() }) });
  const vb = /viewBox="(-?\d+) (-?\d+) (\d+) (\d+)"/.exec(svg).slice(1).map(Number);
  assert.ok(vb[0] <= -840 && vb[1] <= -340, "starts before the leftmost, topmost node");
  assert.ok(vb[0] + vb[2] >= 3000 + 720 + 40 && vb[1] + vb[3] >= 2000 + 480 + 40, "reaches past the farthest node: never the viewport");
  assert.doesNotMatch(svg, /var\(--|<foreignObject|<image/, "pure vector, literal colors");
  assert.match(svg, /<tspan fill="#123456">b<\/tspan>/, "table rows draw their members");
  assert.doesNotMatch(svg, />a<\/tspan>/, "a hidden member is left out in View Mode");
  assert.match(svg, />▸ <\/text><text[^>]*>#if defined\(CONFIG_B\) · 1 member/, "a Kconfig-folded branch is a summary row (chevron in the controls column)");
  assert.match(svg, /stroke-width="2"\/>/);
  assert.ok((svg.match(/stroke-opacity="0\.9"/g) || []).length > 3, "scope bars of the outer block");
  assert.match(svg, /stroke="#ff8800"/, "custom node color");
  assert.match(svg, /stroke="#00aa11"/, "custom edge color");
  assert.match(svg, /<tspan fill="#123456">&lt;<\/tspan><tspan fill="#123456">y<\/tspan>/, "source lines, escaped, as lexical tokens");
  assert.match(svg, /Live commits tab/, "a tab iframe is a placeholder card");
  assert.match(svg, /#v=v3\.0&amp;tab=commits/);
  assert.match(svg, /line one/);
  assert.match(svg, /translate\(600 600\) scale\(2\)/, "auto-fitted content is scaled");
  assert.match(svg, />m1</);
  // The field edge leaves at its row: below the head, toolbar and earlier rows, not the side's middle
  const paths = [...svg.matchAll(/<path d="M (-?[\d.]+) (-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])]);
  assert.equal(paths.length, 2);
  assert.equal(paths[0][0], 360, "on the right side of the struct");
  assert.ok(paths[0][1] > 26 + 22 && paths[0][1] < 250, `anchored at the row of d (y=${paths[0][1]})`);
  assert.equal(paths[1][1], 250, "a hidden row falls back to the side anchor");
  const edit = canvasToSvg(canvas, resolve, { editMode: true, content: (n) => payloads[n.id] || null, structOpts: () => ({ editMode: true }) });
  assert.match(edit, />a<\/tspan>/, "Edit Mode draws hidden members (dimmed)");
  assert.match(edit, /opacity="0\.45"/);
  assert.match(edit, />Types</, "and the struct toolbar chips");
});

test("SVG export stays valid XML: control characters dropped, label extents in the bounds; hidden counts follow the layout", () => {
  const src = M.createNode("source", { id: "src", x: 0, y: 0, data: { path: "a.c" } });
  const n = M.createNode("note", { id: "n", x: 900, y: 0, customTitle: "t\u0000itle", data: { text: "form\u000Cfeed" } });
  const canvas = { ...M.emptyCanvas(), nodes: [src, n], edges: [M.createEdge({ from: "src", to: "n", label: "x".repeat(400) })] };
  const svg = canvasToSvg(canvas, (c) => c, { content: () => ({ path: "a.c", start_line: 1, end_line: 1, total_lines: 1, lines: ["\u000C/* page */"] }) });
  // eslint-disable-next-line no-control-regex
  assert.doesNotMatch(svg, /[\u0000-\u0008\u000B\u000C\u000E-\u001F]/);
  assert.match(svg, /form feed/, "a form feed in a note is whitespace");
  assert.match(svg, />\/\* page \*\/</, "a form feed in a source line is dropped");
  const vb = /viewBox="(-?\d+) (-?\d+) (\d+) (\d+)"/.exec(svg).slice(1).map(Number);
  assert.ok(vb[0] < -200, "a long label widens the image");
  const d = structData();
  d.hiddenItems = { "2:a": true, "999:gone": true };
  assert.equal(structRows(NESTED, d, { editMode: true }).hidden, 1, "a key of a moved line is not counted");
});

test("hideHeader and backgrounds: normalized, persisted, themed defaults without literal colors", () => {
  const n = M.createNode("note", { hideHeader: true, bgColor: "#AABBCC" });
  assert.equal(n.hideHeader, true);
  assert.equal(n.bgColor, "#aabbcc");
  assert.equal(M.createNode("note", { hideHeader: "yes", bgColor: "nope" }).hideHeader, false, "only a real boolean hides");
  assert.equal(M.createNode("note", { bgColor: "nope" }).bgColor, "");
  const c = { ...M.emptyCanvas(), nodes: [n], settings: M.normalizeSettings({ bgColor: "green" }) };
  const back = M.normalizeCanvas(M.serializeCanvas(c));
  assert.equal(back.nodes[0].hideHeader, true);
  assert.equal(back.nodes[0].bgColor, "#aabbcc");
  assert.equal(back.settings.bgColor, "green");
  assert.equal(M.bgVar("", "var(--bg-canvas)"), "var(--bg-canvas)", "the theme default");
  assert.equal(M.bgVar("#102030"), "#102030");
  assert.match(M.bgVar("blue", "var(--bg-secondary)"), /^color-mix\(in srgb, var\(--accent-blue\) 22%, var\(--bg-secondary\)\)$/, "presets tint the theme background");
});

test("undo stack: bounded, no-op snapshots skipped, redo cleared by new changes, coalesced live edits", () => {
  const h = new M.UndoStack(3);
  assert.equal(h.record("a"), false, "the first snapshot is the baseline");
  assert.equal(h.record("a"), false, "no-op");
  assert.equal(h.record("b"), true);
  assert.equal(h.record("c"), true);
  assert.equal(h.undo("c"), "b");
  assert.equal(h.undo("b"), "a");
  assert.equal(h.undo("a"), null);
  assert.equal(h.redo("a"), "b");
  h.record("x");
  assert.equal(h.canRedo, false, "a new change drops the redo stack");
  for (const v of ["1", "2", "3", "4", "5"]) h.record(v);
  assert.equal(h.undoList.length, 3, "bounded");
  const live = new M.UndoStack();
  live.record("0");
  live.record("1", "drag");
  live.record("2", "drag");
  live.record("3", "drag");
  live.record("3"); // the final change event ends the run
  live.record("4", "drag");
  assert.deepEqual(live.undoList, ["0", "3"], "one step per live edit");
  assert.equal(live.undo("9"), "4", "an unrecorded change is recorded before undoing");
  const gap = new M.UndoStack();
  gap.record("0");
  gap.record("1");
  gap.record("1", "k"); // a no-op of a live edit opens no run
  gap.record("2", "k");
  assert.deepEqual(gap.undoList, ["0", "1"], "a real change after a no-op is its own step");
  gap.absorb("2b");
  gap.record("3", "k");
  assert.deepEqual(gap.undoList, ["0", "1", "2b"], "absorb ends a run");
  const snap = JSON.parse(M.historySnapshot({ ...M.emptyCanvas(), settings: M.normalizeSettings({ editMode: true, snap: true, bgColor: "red" }) }));
  assert.equal("editMode" in snap.settings || "snap" in snap.settings, false, "view state is not history");
  assert.equal(snap.settings.bgColor, "red");
});

test("nested #ifdef directive rows carry their nesting level and indent", () => {
  const d = M.createNode("struct", { data: { name: "n" } }).data;
  const { rows } = structRows(NESTED, d);
  const lvl = (text) => rows.find((r) => r.kind === "pp" && r.text === text).ppLevel;
  assert.equal(lvl("#ifdef CONFIG_A"), 0);
  assert.equal(lvl("#if defined(CONFIG_B)"), 1);
  assert.equal(lvl("#elif IS_ENABLED(CONFIG_C)"), 1);
  assert.equal(rows.filter((r) => r.kind === "pp" && r.directive === "endif").map((r) => r.ppLevel).sort().join(","), "0,0,1");
  const html = renderStructBody(NESTED, d);
  assert.match(html, /data-pp-key="5:if"[^>]*>\s*(<span class="nm-pp-bar[^>]*><\/span>)*<span class="nm-pp-indent" style="--nm-pp-lvl:1">/, "the nested #if steps in");
  assert.doesNotMatch(html, /data-pp-key="3:ifdef"[^>]*>\s*(<span class="nm-pp-bar[^>]*><\/span>)*<span class="nm-pp-indent"/, "an outermost directive does not");
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [M.createNode("struct", { id: "s", x: 0, y: 0, w: 600, h: 900, data: { name: "n" } })] }, (c) => c, { content: () => NESTED });
  const xOf = (t) => Number(new RegExp(`<text x="([\\d.]+)"[^>]*>▾ ${t.replace(/[()]/g, "\\$&")}`).exec(svg)?.[1]);
  assert.equal(xOf("#if defined(CONFIG_B)") - xOf("#ifdef CONFIG_A"), 10, "the export indents by the same step");
});

test("table style: a bordered #ifdef cell on every row when there are tracks", () => {
  const d = M.createNode("struct", { data: { name: "n", style: "table" } }).data;
  const html = renderStructBody(NESTED, d);
  assert.match(html, /nm-style-table nm-pp-cells/);
  const rows = html.match(/<div class="nm-sf /g).length;
  assert.equal((html.match(/class="nm-sf-ppcell"/g) || []).length, rows, "one cell per row, bars inside it");
  assert.match(html, /<span class="nm-sf-ppcell" aria-hidden="true"><span class="nm-pp-bar/);
  d.showGuards = false;
  assert.doesNotMatch(renderStructBody(NESTED, d), /nm-sf-ppcell/, "no tracks, no cell");
  d.showGuards = true;
  d.style = "list";
  assert.doesNotMatch(renderStructBody(NESTED, d), /nm-sf-ppcell/, "list style keeps the gutter bars");
  const node = M.createNode("struct", { id: "s", x: 0, y: 0, w: 600, h: 900, data: { name: "n", style: "table" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [node] }, (c) => c, { content: () => NESTED });
  // The divider closes the cell like nodemap.css: 2px row border + cell (gutter + 4, border-box) - half its 1px border
  const x = structRows(NESTED, node.data).tracks * 6 + 6 + 5.5;
  assert.match(svg, new RegExp(`<line x1="${x}" y1="[\\d.]+" x2="${x}" y2="[\\d.]+" stroke="var\\(--border-muted\\)"/>`));
});

test("outer #ifdef banner hides like a member; only the banner dims in Edit Mode", () => {
  const d = M.createNode("struct", { data: { name: "foo" } }).data;
  assert.match(renderStructBody(LAYOUT, d, { editMode: true }), /data-act="hide-item" data-key="__outer__"/);
  M.toggleHidden(d, M.OUTER_KEY);
  const view = renderStructBody(LAYOUT, d);
  assert.doesNotMatch(view, /nm-sf-outer/, "left out in View Mode");
  const edit = renderStructBody(LAYOUT, d, { editMode: true });
  assert.match(edit, /class="nm-sf-outer nm-sf-dim nm-sf-hidden"/);
  assert.doesNotMatch(edit, /class="nm-sf nm-sf-field nm-sf-dim/, "member rows are not dimmed");
  const model = structRows(LAYOUT, d, { editMode: true });
  assert.equal(model.outerHidden, true);
  assert.equal(model.hidden, 1, "counted for Show all hidden");
  assert.equal(M.showAllHidden(d), 1);
  assert.match(renderStructBody(LAYOUT, d), /nm-sf-outer/);
  M.toggleHidden(d, M.OUTER_KEY);
  const node = M.createNode("struct", { id: "s", x: 0, y: 0, data: d });
  const svgView = canvasToSvg({ ...M.emptyCanvas(), nodes: [node] }, (c) => c, { content: () => LAYOUT });
  assert.doesNotMatch(svgView, /defined\(CONFIG_FOO\)/);
  const svgEdit = canvasToSvg({ ...M.emptyCanvas(), nodes: [node] }, (c) => c, { editMode: true, content: () => LAYOUT, structOpts: () => ({ editMode: true }) });
  assert.match(svgEdit, /<g opacity="0\.45"><text[^>]*>defined\(CONFIG_FOO\)/, "the banner alone is dimmed");
});

test("struct toolbar only in Edit Mode; the source reference is in the header", () => {
  const d = M.createNode("struct", { data: { name: "foo" } }).data;
  assert.doesNotMatch(renderStructBody(LAYOUT, d), /nm-struct-bar/);
  assert.doesNotMatch(renderStructBody(LAYOUT, d), /nm-struct-src|include\/foo\.h/);
  assert.match(renderStructBody(LAYOUT, d, { editMode: true }), /<div class="nm-struct-bar">[\s\S]*Types/);
  assert.deepEqual(headMetaOf("struct", LAYOUT), { text: "include/foo.h:10", title: "include/foo.h:10-30 (span from the source text)" });
  assert.equal(headMetaOf("source", { path: "a.c", start_line: 3, end_line: 9, total_lines: 40, truncated: false, symbol: null }).text, "a.c · 3–9");
  assert.equal(headMetaOf("struct", null).text, "");
  const node = M.createNode("struct", { id: "s", x: 0, y: 0, w: 400, data: { name: "foo" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [node] }, (c) => c, { content: () => LAYOUT });
  assert.match(svg, /text-anchor="end"[^>]*>include\/foo\.h:10</, "the header shows the reference");
  assert.doesNotMatch(svg, />Types</, "no toolbar in View Mode");
  // Rows start right under the header (26) + the outer banner (16) + 2
  const first = /<text x="[\d.]+" y="([\d.]+)"[^>]*><tspan fill="var\(--syn-type\)">int /.exec(svg);
  assert.equal(Number(first[1]), 26 + 16 + 2 + 8 + 4, "first row baseline without the toolbar");
});

test("SVG export: headerless nodes, backgrounds, transparency, pixel scale and foreignObject snapshots", () => {
  const note = M.createNode("note", { id: "n", x: 0, y: 0, w: 200, h: 100, hideHeader: true, bgColor: "#204060", data: { text: "hello" } });
  const man = M.createNode("man", { id: "m", x: 400, y: 0, data: { name: "open", section: "2", path: "man2/open.2" } });
  const tab = M.createNode("tab", { id: "t", x: 0, y: 600, data: { tabType: "commits", hash: "tab=commits" } });
  const canvas = { ...M.emptyCanvas(), nodes: [note, man, tab], edges: [M.createEdge({ from: "n", to: "m", label: "see" })], settings: M.normalizeSettings({ bgColor: "#112233" }) };
  const resolve = (c) => ({ "var(--accent-blue)": "#0000ff", "var(--bg-secondary)": "#ffffff" }[c] || c);
  const svg = canvasToSvg(canvas, resolve, {});
  assert.doesNotMatch(svg, />NOTE</, "a hidden header is left out in View Mode");
  assert.match(svg, />hello</);
  assert.match(svg, /<text x="10" y="18"[^>]*>hello/, "the body starts at the node's top");
  assert.match(svg, /fill="#204060"/, "node background");
  assert.match(svg, /<rect x="-?\d+" y="-?\d+" width="\d+" height="\d+" fill="#112233"\/>/, "canvas background");
  assert.match(svg, /stroke="#112233" stroke-width="4" paint-order="stroke"/, "label halo in the canvas color");
  assert.match(svg, /Live commits tab/, "no live DOM: placeholder card");
  assert.match(canvasToSvg(canvas, resolve, { editMode: true }), /<g opacity="0\.45">[\s\S]*?>NOTE</, "Edit Mode draws it dimmed");
  const tinted = canvasToSvg({ ...canvas, nodes: [{ ...note, bgColor: "blue" }] }, resolve, {});
  assert.match(tinted, /fill="#c7c7ff"/, "a preset background is the accent tinted into the theme background");
  const clear = canvasToSvg(canvas, resolve, { transparent: true });
  assert.doesNotMatch(clear, /fill="#112233"/, "no background rect");
  assert.doesNotMatch(clear, /paint-order="stroke"/, "no halo on a transparent image");
  const { w, h } = svgSize(svg);
  const big = svgSize(canvasToSvg(canvas, resolve, { pixelScale: 3 }));
  assert.deepEqual([big.w, big.h], [w * 3, h * 3]);
  assert.match(canvasToSvg(canvas, resolve, { pixelScale: 3 }), new RegExp(`viewBox="-?\\d+ -?\\d+ ${w} ${h}"`), "same viewBox: vectors rasterized larger");
  const fo = canvasToSvg(canvas, resolve, { foreign: (n) => (n.type === "tab" ? { html: '<div xmlns="http://www.w3.org/1999/xhtml">live \u0001commits</div>' } : n.type === "man" ? { html: "<p>man body</p>" } : null) });
  assert.match(fo, /<foreignObject x="0" y="26" width="720" height="454"><div xmlns="http:\/\/www\.w3\.org\/1999\/xhtml" style="width:720px;height:454px;overflow:hidden"><div xmlns="http:\/\/www\.w3\.org\/1999\/xhtml">live commits<\/div>/);
  assert.match(fo, /man body/);
  assert.doesNotMatch(fo, /Live commits tab|\u0001/, "the snapshot replaces the card; XML-illegal characters go");
  assert.equal(captureNodeForeignObject(tab, null), null, "nothing mounted: no snapshot");
  assert.equal(captureNodeForeignObject(tab, { querySelector: () => null }), null);
});

test("PNG upscale: output size per factor, clamped to the canvas limits", () => {
  assert.deepEqual(pngSize(1000, 500, 3), { w: 3000, h: 1500, scale: 3, clamped: false });
  assert.deepEqual(pngSize(1000, 500, 1), { w: 1000, h: 500, scale: 1, clamped: false });
  const z = pngSize(10000, 2000, 4);
  assert.equal(z.w, 16384);
  assert.equal(z.clamped, true);
  assert.ok(z.h <= 16384 && Math.abs(z.w / z.h - 5) < 0.01, "aspect kept");
  const area = pngSize(15000, 15000, 4);
  assert.ok(area.w * area.h <= 268435456);
  assert.deepEqual(svgSize('<svg xmlns="x" width="120" height="80" viewBox="0 0 40 20">'), { w: 120, h: 80 });
});

// ------------------------------------------------------------------ export fixes, scale, embedded sessions, domain nodes

/** Resolver that keeps the token name readable: `var(--x)` -> `#<x>` stand-ins are not needed, names are. */
const named = (c) => (c.startsWith("var(--") ? `[${c.slice(6, -1)}]` : c);

test("SVG export: the node interior is clipped to its rounded box; group default accent is blue", () => {
  const n = M.createNode("note", { id: "a", x: 0, y: 0, w: 200, h: 120, color: "red", data: { text: "x" } });
  const g = M.createNode("group", { id: "g", x: -50, y: -50, w: 400, h: 300 });
  for (const transparent of [false, true]) {
    const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [g, n] }, named, { transparent });
    const clip = /<clipPath id="(nmclip\d+)"><rect x="0" y="0" width="200" height="120" rx="6"\/><\/clipPath>/.exec(svg);
    assert.ok(clip, "a rounded clip of the node's box");
    const body = svg.slice(svg.indexOf(`<g clip-path="url(#${clip[1]})">`));
    assert.ok(body.length < svg.length, "panel, header, accent strip and body inside it");
    const strip = body.indexOf('height="3" fill="[accent-red]"');
    const border = body.indexOf('rx="6" fill="none" stroke="[accent-red]"');
    assert.ok(strip > 0 && border > strip, "the 3-unit accent strip is clipped; the border is drawn on top");
    assert.doesNotMatch(svg, /<clipPath id="nmclip\d+"><rect x="0" y="0" width="200" height="26"\/>/, "no square header clip");
    assert.match(svg, /stroke="\[accent-blue\]" stroke-dasharray="6 4"/, "an uncolored group frame is blue");
  }
});

test("SVG export: auto-fitted structs end at their rows (folded #ifdefs leave no bottom band); live row heights win", () => {
  const s = M.createNode("struct", { id: "s", x: 0, y: 0, data: { name: "n", style: "table" } });
  M.setAutoFit(s, true);
  M.applyNaturalSize(s, 360, 600); // stale natural height (measured before branches folded)
  const canvas = { ...M.emptyCanvas(), nodes: [s] };
  const opts = { content: () => NESTED, structOpts: () => ({ editMode: false, evaluator: kcfg() }) };
  const rows = structRows(NESTED, s.data, { editMode: false, evaluator: kcfg() }).rows.length;
  const border = (svg) => Number(/<rect x="0" y="0" width="360" height="([\d.]+)" rx="6" fill="none"/.exec(svg)[1]);
  const h = border(canvasToSvg(canvas, named, opts));
  assert.equal(h, 26 + 2 + rows * 18 + 2, "header + rows (18-unit table rows) + padding, not the stale 600");
  const vb = /viewBox="-?\d+ -?\d+ \d+ (\d+)"/.exec(canvasToSvg(canvas, named, opts))[1];
  assert.equal(Number(vb), Math.ceil(h + 80), "the image ends with the node, not the stale box");
  const measured = canvasToSvg(canvas, named, { ...opts, rowHeights: () => Array(rows).fill(21) });
  assert.equal(border(measured), 26 + 2 + rows * 21 + 2, "measured DOM row heights lay the rows out");
  assert.equal(border(canvasToSvg(canvas, named, { ...opts, rowHeights: () => [21] })), h, "a row count that does not match is ignored");
  s.autoFit = false;
  s.h = 600;
  assert.equal(border(canvasToSvg(canvas, named, opts)), 600, "a fixed-size node keeps its box");
});

test("SVG export: source lines keep the code view's lexical colors", () => {
  const src = M.createNode("source", { id: "src", x: 0, y: 0, data: { path: "a.c" } });
  const payload = { path: "a.c", start_line: 1, end_line: 2, total_lines: 2, lines: ["static int x = 1; /* c */", '#define S "s"'] };
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [src] }, named, { content: () => payload });
  assert.match(svg, /<tspan fill="\[syn-keyword\]">static<\/tspan>/);
  assert.match(svg, /<tspan fill="\[syn-number\]">1<\/tspan>/);
  assert.match(svg, /<tspan fill="\[syn-comment\]" font-style="italic">\/\* c \*\/<\/tspan>/);
  assert.match(svg, /<tspan fill="\[syn-preproc\]">#define <\/tspan><tspan fill="\[syn-preproc\]">S<\/tspan> <tspan fill="\[syn-string\]">&quot;s&quot;<\/tspan>/);
  assert.deepEqual(htmlSegments('<span class="tok-keyword x">if</span> (<span class="ast-dimmed"><span class="tok-number">1</span></span>&amp;&#039;'),
    [{ text: "if", cls: "tok-keyword" }, { text: " (", cls: "" }, { text: "1", cls: "tok-number" }, { text: "&'", cls: "" }]);
  assert.equal(sourceLineSegments(["/* a", "b */ int"], "x.c")[1][0].cls, "tok-comment", "multi-line comments span lines");
});

test("content scale: every non-group node scales its content; outer box stays without auto-fit", () => {
  const n = M.createNode("source", { x: 0, y: 0, w: 600, h: 400, data: { path: "a.c" } });
  M.setContentScale(n, 2);
  assert.deepEqual([n.w, n.h, n.scale], [600, 400, 2], "the footprint stays");
  assert.deepEqual(M.contentBox(n), { w: 300, h: 200, s: 2 });
  assert.deepEqual(nodeBox(n), { width: 300, height: 200, transform: "translate(0px, 0px) scale(2)" });
  assert.equal(M.normalizeNode(JSON.parse(JSON.stringify(n))).scale, 2, "persisted for nodes without auto-fit");
  M.setContentScale(n, 99);
  assert.equal(n.scale, M.SCALE_MAX);
  const g = M.createNode("group", { scale: 3 });
  assert.equal(g.scale, 1, "groups never scale");
  M.setContentScale(g, 2);
  assert.equal(g.scale, 1);
  const f = M.createNode("note", { data: { text: "x" } });
  M.setAutoFit(f, true);
  M.applyNaturalSize(f, 200, 100);
  M.setContentScale(f, 1.5);
  assert.deepEqual([f.w, f.h], [300, 150], "an auto-fitted node grows with it");
  n.scale = 0.5;
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => ({ path: "a.c", start_line: 1, end_line: 1, total_lines: 1, lines: ["x"] }) });
  assert.match(svg, /translate\(0 0\) scale\(0\.5\)/);
  assert.match(svg, /width="1200" height="800" rx="6" fill="none"/, "laid out at w/scale × h/scale");
});

test("NodeMap embedded sessions: one by default, add / rename / delete keeps one, scoped embed addresses", () => {
  const c = M.emptyCanvas();
  assert.deepEqual(c.settings.embedSessions, [{ id: "nms-1", name: "Canvas Session 1" }]);
  assert.equal(c.settings.followSession, "nms-1", "the canvas follows its first session");
  const b = M.addEmbedSession(c.settings);
  assert.deepEqual(b, { id: "nms-2", name: "Canvas Session 2" });
  assert.ok(M.renameEmbedSession(c.settings, "nms-2", "  Kconfig lab "));
  assert.equal(c.settings.embedSessions[1].name, "Kconfig lab");
  assert.equal(M.renameEmbedSession(c.settings, "nms-2", "   "), false);
  const t = M.createNode("tab", { data: { tabType: "kconfig", hash: "tab=kconfig", embedSessionId: "nms-2" } });
  c.nodes.push(t);
  c.settings.followSession = "nms-2";
  assert.equal(M.embedSessionOf(c, t).id, "nms-2");
  assert.deepEqual(M.removeEmbedSession(c, "nms-2"), [t.id], "its tab nodes move to the first session");
  assert.equal(t.data.embedSessionId, "nms-1");
  assert.equal(c.settings.followSession, "nms-1");
  assert.equal(M.removeEmbedSession(c, "nms-1"), null, "the last session stays");
  const st = M.normalizeSettings({ embedSessions: [{ id: "x", name: "" }, { id: "bad id!" }, { id: "x" }], followSession: "gone" });
  assert.deepEqual(st.embedSessions, [{ id: "x", name: "x" }]);
  assert.equal(st.followSession, "x", "an unknown followed session falls back to the first");
  assert.equal(M.normalizeSettings({ followSession: "" }).followSession, "", "the host workspace can be followed");
  assert.equal(M.embedSrc("tab=kconfig", "/", "tab-1:nms-1"), "/?embed=1&embedSession=tab-1%3Anms-1#tab=kconfig");
  assert.equal(M.embedSrc("#tab=x"), "/?embed=1#tab=x");
  assert.equal(embedSessionId({ search: "?embed=1&embedSession=tab-1%3Anms-1" }), "tab-1:nms-1");
  assert.equal(embedSessionId({ search: "?embedSession=tab-1" }), "", "only an embed shell joins one");
  assert.equal(embedSessionId({ search: "?embed=1&embedSession=a%20b" }), "", "keys are validated");
  assert.equal(embedSessionChannel("tab-1:nms-1"), "kip_nodemap_session:tab-1:nms-1");
});

test("tab drag payloads: typed, text and announced forms; files never carry a tab", () => {
  const payload = { tabId: "t1", sourceWinId: "w", tabData: { id: "t1", type: "code", path: "a.c" } };
  const dt = (data, types = Object.keys(data)) => ({ types, getData: (k) => data[k] || "" });
  assert.deepEqual(tabDragPayload(dt({ [TAB_DRAG_MIME]: JSON.stringify(payload) })), payload);
  assert.deepEqual(tabDragPayload(dt({ "text/plain": `${TAB_DRAG_TEXT_PREFIX}${JSON.stringify(payload)}` })), payload);
  assert.deepEqual(tabDragPayload(dt({}, ["text/plain"]), payload), payload, "the announcement of another window");
  assert.equal(tabDragPayload(dt({}, ["Files"]), payload), null);
  assert.equal(mayCarryTab(dt({}, [TAB_DRAG_MIME])), true);
  assert.equal(mayCarryTab(dt({}, ["text/plain"])), false);
  assert.equal(mayCarryTab(dt({}, ["text/plain"]), payload), true);
  assert.equal(mayCarryTab(dt({}, ["Files"]), payload), false);
});

test("domain node payloads normalize; titles", () => {
  const d = M.createNode("diff", { data: { versionA: "v3.0", versionB: "v3.1", path: "kernel/fork.c" } });
  assert.deepEqual(d.data, { mode: "file", versionA: "v3.0", versionB: "v3.1", path: "kernel/fork.c", oldPath: "", commitHash: "" });
  assert.equal(d.title, "fork.c · v3.0..v3.1");
  const c = M.createNode("diff", { data: { commitHash: "ABCDEF0123456789" } });
  assert.equal(c.data.mode, "commit");
  assert.equal(c.data.commitHash, "abcdef0123456789");
  assert.equal(M.createNode("diff", { data: { mode: "commit", commitHash: "zz" } }).data.commitHash, "");
  const m = M.createNode("mail", { data: { tid: 7, mid: "<a@b>", openMids: { x: true, y: false }, single: true } });
  assert.deepEqual(m.data, { tid: 7, mid: "a@b", subject: "", openMids: { x: true }, fullMids: {}, pinnedBodies: {}, single: true, fullBody: "" });
  assert.equal(M.createNode("mail", { data: { tid: -1, single: true } }).data.single, false, "a single message needs its id");
  const k = M.createNode("kconfig", { data: { symbol: "CONFIG_SMP" } });
  assert.deepEqual(k.data, { symbol: "SMP", arch: "x86_64" });
  assert.equal(k.title, "CONFIG_SMP");
  const mt = M.createNode("maintainer", { data: { secId: 12, title: "SCHEDULER" } });
  assert.deepEqual(mt.data, { secId: 12, title: "SCHEDULER", query: "", path: "" });
  for (const t of ["diff", "mail", "kconfig", "maintainer"]) {
    assert.ok(M.AUTOFIT_TYPES.includes(t) && M.TAB_OPENABLE_TYPES.includes(t) && M.NODE_BADGES[t], t);
  }
});

const DIFF_TEXT = [
  "diff --git a/kernel/fork.c b/kernel/fork.c",
  "--- a/kernel/fork.c",
  "+++ b/kernel/fork.c",
  "@@ -10,3 +10,4 @@ static int x;",
  " int a;",
  "-int b;",
  "+int b2;",
  "+int <c>;",
  " int d;",
].join("\n");

test("diff nodes: unified diff text and commit files become hunks with old / new line numbers", () => {
  const files = D.parseUnifiedDiff(DIFF_TEXT);
  assert.equal(files.length, 1);
  assert.equal(files[0].path, "kernel/fork.c");
  assert.deepEqual([files[0].additions, files[0].deletions], [2, 1]);
  assert.deepEqual(files[0].hunks[0].lines, [[" ", 10, 10, "int a;"], ["-", 11, null, "int b;"], ["+", null, 11, "int b2;"], ["+", null, 12, "int <c>;"], [" ", 12, 13, "int d;"]]);
  const p = D.diffPayload({ mode: "file", versionA: "v3.0", versionB: "v3.1", path: "kernel/fork.c" }, { diff: DIFF_TEXT, additions: 2, deletions: 1 });
  const rows = D.diffRows(p);
  assert.deepEqual(rows.map((r) => r.kind), ["hunk", " ", "-", "+", "+", " "], "one file: no file header");
  const html = D.renderDiffBody(p);
  assert.match(html, /nm-diff-add[^]*int &lt;c&gt;;/, "escaped");
  assert.match(html, /<span class="nm-diff-no">11<\/span><span class="nm-diff-no"><\/span><span class="nm-diff-sign">-<\/span>/);
  const commit = { commit_hash: "abc", files: [
    { path: "a.c", old_path: "a.c", status: "M", additions: 1, deletions: 0, hunks: [{ old_start: 1, old_count: 1, new_start: 1, new_count: 2, heading: "f()", lines: [[" ", 1, 1, "x"], ["+", null, 2, "y"]] }] },
    { path: "b.c", old_path: "b.c", status: "A", additions: 1, deletions: 0, hunks: [] },
  ] };
  const all = D.diffPayload({ mode: "commit", commitHash: "abc", path: "" }, commit);
  assert.equal(all.files.length, 2);
  assert.deepEqual(D.diffRows(all).filter((r) => r.kind === "file").map((r) => r.text), ["M a.c  +1 −0", "A b.c  +1 −0"]);
  assert.equal(D.diffRows(all)[1].text, "@@ -1,1 +1,2 @@ f()");
  assert.equal(D.diffPayload({ mode: "commit", commitHash: "abc", path: "b.c" }, commit).files.length, 1, "a commit diff keeps the node's file");
  assert.equal(headMetaOf("diff", p).text, "kernel/fork.c · v3.0..v3.1");
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [M.createNode("diff", { id: "d", data: { path: "kernel/fork.c", versionA: "v3.0", versionB: "v3.1" } })] }, named, { content: () => p });
  assert.match(svg, /fill="\[diff-add-fg\]"[^>]*>\+ int &lt;c&gt;;/, "added lines in the diff color");
  assert.match(svg, /fill="\[diff-del-fg\]"[^>]*>- int b;/);
  assert.match(svg, /@@ -10,3 \+10,4 @@/);
  assert.match(svg, />DIFF</);
});

const THREAD = {
  tid: 5, total: 4, messages: [
    { message_id: "root@x", subject: "[PATCH v2 0/2] thing", author: "A", date_iso: "2011-01-01", timestamp: 1, is_patch: true, is_cover: true, patch_version: 2, references: "" },
    { message_id: "p1@x", subject: "[PATCH v2 1/2] one", author: "A", timestamp: 2, is_patch: true, patch_version: 2, patch_idx: 1, patch_total: 2, references: "<root@x>" },
    { message_id: "r1@x", subject: "Re: one", author: "B", timestamp: 3, references: ["root@x", "p1@x"] },
    { message_id: "p2@x", subject: "[PATCH v2 2/2] two", author: "A", timestamp: 4, references: "<root@x>" },
  ],
};

test("mail nodes: the reply tree with connector guides, badges and excerpts; expand-to-nodes layout", () => {
  const model = D.mailThreadModel(THREAD, { mid: "r1@x", openMids: { "r1@x": true } }, new Map([["r1@x", "> quoted\n\nI agree.\nThanks"]]));
  assert.deepEqual(model.rows.map((r) => [r.mid, r.parent, r.depth]), [["root@x", null, 0], ["p1@x", "root@x", 1], ["r1@x", "p1@x", 2], ["p2@x", "root@x", 1]]);
  assert.deepEqual(model.rows.map((r) => r.guides), [[], ["tee"], ["pipe", "end"], ["end"]], "an ancestor with later siblings keeps its line");
  assert.ok(model.rows[2].focus && model.rows[2].open);
  assert.deepEqual(model.rows[2].excerpt, ["I agree.", "Thanks"], "quoted text left out");
  assert.deepEqual(D.mailBadges(model.rows[0]), ["v2", "cover"]);
  assert.deepEqual(D.mailBadges(model.rows[1]), ["v2", "1/2", "PATCH"]);
  const html = D.renderMailBody(model);
  assert.match(html, /nm-g-pipe[^]*nm-g-end/);
  assert.match(html, /data-act="mail-spawn" data-key="p1@x"/);
  assert.match(html, /I agree\./);
  const lay = D.mailThreadLayout(model, { x: 100, y: 0 }, { w: 300, h: 100, gapX: 50, gapY: 20 });
  assert.deepEqual(lay.map((p) => [p.mid, p.x, p.y]), [["root@x", 100, 0], ["p1@x", 450, 120], ["r1@x", 800, 240], ["p2@x", 450, 360]]);
  const cyc = D.mailThreadModel({ messages: [{ message_id: "a", references: "<b>" }, { message_id: "b", references: "<a>" }] });
  assert.equal(cyc.rows.length, 2, "a reference cycle hides no message");
  const one = D.mailThreadModel({ message_id: "<m@x>", subject: "Hi", body: "Body text" }, { mid: "m@x", single: true });
  assert.equal(one.rows[0].body, "Body text", "a single message shows its whole body");
  assert.ok(one.rows[0].full && one.rows[0].open);
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [M.createNode("mail", { id: "m", data: { tid: 5 } })] }, named, { content: () => model });
  assert.match(svg, /\[PATCH v2 1\/2\] one/);
  assert.match(svg, />1\/2<\/text>/, "patch badges");
  assert.match(svg, /<line x1="(\d+)" y1="[\d.]+" x2="\1" y2="[\d.]+" stroke="\[border-color\]"\/>/, "connector lines");
  assert.equal(headMetaOf("mail", model).text, "4 messages · t/5");
});

test("kconfig nodes: relations from the symbol and the graph, live value, relation targets", () => {
  const detail = { name: "SMP", type_name: "bool", prompt: "Symmetric multi-processing", file_path: "arch/x86/Kconfig", line: 230, depends_on: ["X86_LOCAL_APIC"], selects: ["CONFIG_USE_GENERIC_SMP_HELPERS"], implies: [] };
  const graph = { edges: [{ from: "SMP", to: "X86_LOCAL_APIC", type: "depends_on" }, { from: "SMP", to: "IRQ_WORK", type: "implies" }, { from: "NUMA", to: "SMP", type: "depends_on" }, { from: "X86_64_SMP", to: "SMP", type: "selects" }, { from: "A", to: "B", type: "selects" }] };
  const model = D.kconfigModel(detail, graph, "SMP");
  assert.deepEqual(model.groups.map((g) => [g.rel, g.names]), [
    ["depends_on", ["X86_LOCAL_APIC"]], ["selects", ["USE_GENERIC_SMP_HELPERS"]], ["implies", ["IRQ_WORK"]],
    ["depended_by", ["NUMA"]], ["selected_by", ["X86_64_SMP"]],
  ]);
  assert.equal(D.kconfigValue({ CONFIG_SMP: "y" }, "SMP"), "y");
  assert.equal(D.kconfigValue({ SMP: "m" }, "SMP"), "m");
  assert.equal(D.kconfigValue({}, "SMP"), "");
  assert.equal(D.kconfigValue({ HZ: 250 }, "HZ"), "250");
  const html = D.renderKconfigBody(model, "y", new Set(["NUMA"]));
  assert.match(html, /nm-kc-val nm-kc-y[^>]*>y</);
  assert.match(html, /class="nm-kc-chip on" data-act="kc-rel" data-key="depended_by:NUMA"/, "a related symbol on the canvas");
  assert.match(D.renderKconfigBody(model, null), /no engine/);
  assert.deepEqual(D.relationTargets(model).map((t) => [t.name, t.edge, t.dir]), [
    ["X86_LOCAL_APIC", "depends on", "out"], ["USE_GENERIC_SMP_HELPERS", "selects", "out"], ["IRQ_WORK", "implies", "out"],
    ["NUMA", "depends on", "in"], ["X86_64_SMP", "selects", "in"],
  ]);
  assert.deepEqual(D.relationTargets(model, "selected_by:X86_64_SMP").map((t) => t.name), ["X86_64_SMP"]);
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [M.createNode("kconfig", { id: "k", data: { symbol: "SMP" } })] }, named, { content: () => model, kconfigValues: () => ({ SMP: "y" }) });
  assert.match(svg, /CONFIG_SMP/);
  assert.match(svg, /fill="\[accent-green\]" font-family="monospace" font-size="10" text-anchor="middle" xml:space="preserve">y</, "the value badge");
  assert.match(svg, />Selected by</);
  assert.match(canvasToSvg({ ...M.emptyCanvas(), nodes: [M.createNode("kconfig", { id: "k", data: { symbol: "SMP" } })] }, named, { content: () => model }), />no engine</);
  assert.equal(headMetaOf("kconfig", model).text, "arch/x86/Kconfig:230");
});

test("maintainer nodes: M / R / L / T / F lines and the status", () => {
  const res = { version: "v3.0", section: {
    sec_id: 3, name: "SCHEDULER", status: "Maintained", mailing_list: "linux-kernel@vger.kernel.org", scm_tree: "git git://a/sched.git",
    members: [{ name: "Ingo", email: "i@x", role_type: 1 }, { name: "Peter", email: "p@x", role: "R", role_type: 2 }],
    patterns: [{ pattern_type: "F", pattern: "kernel/sched/" }, { pattern_type: "X", pattern: "kernel/sched/x.c" }, { pattern_type: "K", pattern: "sched" }],
  } };
  const m = D.maintainerModel(res);
  assert.deepEqual([m.title, m.status, m.maintainers, m.reviewers, m.lists, m.trees, m.files, m.excludes], [
    "SCHEDULER", "Maintained", ["Ingo <i@x>"], ["Peter <p@x>"], ["linux-kernel@vger.kernel.org"], ["git git://a/sched.git"], ["kernel/sched/"], ["kernel/sched/x.c"],
  ]);
  assert.deepEqual(D.maintainerLines(m).map(([t]) => t), ["M", "R", "L", "T", "F", "X"]);
  assert.equal(D.statusClass("Odd Fixes"), "odd");
  assert.match(D.renderMaintainerBody(m), /nm-mt-status nm-mt-maintained/);
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [M.createNode("maintainer", { id: "x", data: { secId: 3 } })] }, named, { content: () => m });
  assert.match(svg, />M:<\/text>[^]*Ingo &lt;i@x&gt;/);
  assert.match(svg, />Maintained</);
  assert.equal(headMetaOf("maintainer", m).text, "Maintained");
});
