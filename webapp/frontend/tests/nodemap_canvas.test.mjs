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
  assert.deepEqual(M.emptyCanvas().settings, { snap: false, grid: M.GRID_DEFAULT, editMode: false, kconfigCollapse: true, kconfigEngine: "" });
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
  assert.deepEqual([n.autoFit, n.scale], [false, 1]);
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
  assert.match(svg, /▸ #if defined\(CONFIG_B\) · 1 member/, "a Kconfig-folded branch is a summary row");
  assert.match(svg, /stroke-width="2"\/>/);
  assert.ok((svg.match(/stroke-opacity="0\.9"/g) || []).length > 3, "scope bars of the outer block");
  assert.match(svg, /stroke="#ff8800"/, "custom node color");
  assert.match(svg, /stroke="#00aa11"/, "custom edge color");
  assert.match(svg, /&lt;y&gt;/, "source lines, escaped");
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
