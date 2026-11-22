/**
 * nodemap_rows_types.test.mjs - NodeMap row-level edge anchors (mail messages `mail:<mid>`, history rows
 * `commit:<sha>` / `ver:<release>`), whole mail messages, beautified doc nodes, struct `table` style
 * `#ifdef` geometry (live markup / CSS and SVG export), and non-standard C type extraction with the
 * bulk "Expand Types" action.
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};

const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const M = await import("../js/views/nodemap/canvas_model.js");
const D = await import("../js/views/nodemap/domain_nodes.js");
const S = await import("../js/views/nodemap/struct_view.js");
const { canvasToSvg } = await import("../js/views/nodemap/nodemap_export.js");

const CSS = readFileSync(new URL("../css/nodemap.css", import.meta.url), "utf8");

function controller() {
  const c = new NodeMapController();
  c.tab = { id: "tab-r", type: "nodemap", version: "v3.0", repo: "" };
  c.currentVersion = "v3.0";
  c.repo = "";
  c.storageKey = `${M.STORAGE_PREFIX}tab-r`;
  c.canvas = M.emptyCanvas();
  c.renderer = { sync() {}, invalidate() {}, element: () => null, clear() {} };
  c.renderInspector = () => {};
  c.renderWires = () => {};
  c.wrapEl = { classList: { add() {}, remove() {}, toggle() {} }, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }), style: { setProperty() {} } };
  c.layerEl = { style: {} };
  c.marqueeEl = { style: {}, hidden: true };
  c.applyTransform = () => {};
  c.scheduleWires = () => {};
  return c;
}

function clickAct(c, id, act, key = "") {
  const btn = { dataset: { act, key } };
  const nodeEl = { dataset: { nodeId: id } };
  c.onClick({ target: { closest: (s) => (s === "[data-act]" ? btn : s === ".nm-node" ? nodeEl : null) }, stopPropagation() {} });
}

const THREAD = {
  tid: 5, total: 3, messages: [
    { message_id: "root@x", subject: "[PATCH 0/1] t", timestamp: 1, references: "" },
    { message_id: "p1@x", subject: "[PATCH 1/1] one", timestamp: 2, references: "<root@x>" },
    { message_id: "r1@x", subject: "Re: one", timestamp: 3, references: "<root@x> <p1@x>" },
  ],
};

const LONG_BODY = [
  "Hi all,", "", "> quoted line", ">> older quote",
  ...Array.from({ length: 30 }, (_, i) => `line ${i + 1}`),
  "diff --git a/f.c b/f.c", "--- a/f.c", "+++ b/f.c", "@@ -1,2 +1,2 @@", "-old", "+new", " same",
  "-- ", "Signed",
].join("\r\n");

// ------------------------------------------------------------------ non-standard C types

test("type extraction: struct / union / enum tags and kernel typedefs; scalars and qualifiers left out", () => {
  const one = (t) => S.extractNonStandardType(t);
  assert.deepEqual(one("struct task_struct *"), { kind: "struct", name: "task_struct", isPointer: true, edgeLabel: "points to" });
  assert.deepEqual(one("const union foo"), { kind: "union", name: "foo", isPointer: false, edgeLabel: "embeds" });
  assert.deepEqual(one("struct list_head"), { kind: "struct", name: "list_head", isPointer: false, edgeLabel: "embeds" });
  assert.deepEqual(one("enum pid_type"), { kind: "enum", name: "pid_type", isPointer: false, edgeLabel: "type" });
  for (const td of ["spinlock_t", "atomic_t", "rwlock_t", "mm_segment_t", "gfp_t", "volatile atomic_t"]) {
    assert.equal(one(td)?.kind, "typedef", td);
    assert.equal(one(td)?.edgeLabel, "type", td);
  }
  assert.equal(one("spinlock_t *").edgeLabel, "points to");
  assert.deepEqual(one("struct foo __rcu *"), { kind: "struct", name: "foo", isPointer: true, edgeLabel: "points to" });
  assert.equal(one("__attribute__((aligned(8))) atomic64_t")?.name, "atomic64_t", "attributes are not types");
  for (const scalar of ["int", "unsigned long", "const char *", "void *", "u8", "u64", "s32", "__u32 __user *", "__le16", "__be64",
    "uint32_t", "int64_t", "size_t", "ssize_t", "bool", "_Bool", "phys_addr_t", "dma_addr_t", "uintptr_t", "loff_t", "typeof(x)",
    "DECLARE_BITMAP(bits, 4)", "NR_CPUS", ""]) {
    assert.equal(one(scalar), null, scalar);
  }
  assert.ok(S.STANDARD_C_TYPES.has("u16") && S.STANDARD_C_TYPES.has("const") && !S.STANDARD_C_TYPES.has("spinlock_t"));
});

test("type extraction: function pointers yield return and parameter types, never parameter names", () => {
  const refs = S.extractTypeRefs("int (*)(struct inode *inode, struct file *filp, size_t len)");
  assert.deepEqual(refs.map((r) => `${r.kind} ${r.name}`), ["struct inode", "struct file"]);
  assert.ok(refs.every((r) => r.edgeLabel === "type"));
  assert.deepEqual(S.extractTypeRefs("struct page *(*)(gfp_t gfp, unsigned int order)").map((r) => r.name), ["page", "gfp_t"]);
  assert.deepEqual(S.extractTypeRefs("void (*)(struct rcu_head *head)").map((r) => r.name), ["rcu_head"]);
  assert.deepEqual(S.extractTypeRefs("void (*)(void)"), []);
  assert.deepEqual(S.extractTypeRefs("int (*)(struct file *, struct file *)").length, 1, "deduplicated");
});

const TYPED = {
  version: "v3.0", name: "task", kind: "struct", file_path: "include/t.h", line_s: 1, line_e: 20, outer_conds: [],
  items: [
    { kind: "field", name: "lock", type: "spinlock_t", line: 2, conds: [] },
    { kind: "field", name: "usage", type: "atomic_t", line: 3, conds: [] },
    { kind: "field", name: "parent", type: "struct task *", line: 4, conds: [] },
    { kind: "field", name: "mm", type: "struct mm_struct *", line: 5, conds: [] },
    { kind: "field", name: "active_mm", type: "struct mm_struct *", line: 6, conds: [] },
    { kind: "field", name: "state", type: "enum task_state", line: 7, conds: [] },
    { kind: "field", name: "flags", type: "unsigned long", line: 8, conds: [] },
    { kind: "field", name: "pid", type: "u32", line: 9, conds: [] },
    { kind: "field", name: "secret", type: "struct cred *", line: 10, conds: [] },
    { kind: "field", name: "open", type: "int (*)(struct inode *, struct file *)", line: 11, conds: [] },
    { kind: "aggregate", tag: "union", name: "u", type_name: null, line: 12, conds: [], children: [
      { kind: "field", name: "rcu", type: "struct rcu_head", line: 13, conds: [] },
    ] },
  ],
};

test("collectExpandableTypes: unique visible types, the first member keyed, self and hidden members left out", () => {
  const d = M.createNode("struct", { data: { name: "task" } }).data;
  d.hiddenItems = { "10:secret": true };
  const list = S.collectExpandableTypes(TYPED, d);
  const names = list.map((t) => `${t.ref.kind} ${t.ref.name}`);
  assert.deepEqual(names, ["typedef spinlock_t", "typedef atomic_t", "struct mm_struct", "enum task_state", "struct inode", "struct file", "struct rcu_head"]);
  assert.equal(list.find((t) => t.ref.name === "mm_struct").key, "5:mm", "the first referencing member anchors the edge");
  assert.ok(!names.includes("struct task"), "the struct itself is not expanded");
  assert.ok(!names.includes("struct cred"), "hidden members are not visible");
  d.collapsed = { "12:u": true };
  assert.ok(!S.collectExpandableTypes(TYPED, d).some((t) => t.ref.name === "rcu_head"), "a folded aggregate hides its members");
  // every typedef / enum member gets a follow button
  const html = S.renderStructBody(TYPED, M.createNode("struct", { data: { name: "task" } }).data);
  assert.match(html, /data-act="follow-type" data-key="2:lock" title="Follow typedef spinlock_t \(type\)"/);
  assert.match(html, /data-act="follow-type" data-key="7:state"/);
  assert.doesNotMatch(html, /data-act="follow-type" data-key="8:flags"/);
  const edit = S.renderStructBody(TYPED, M.createNode("struct", { data: { name: "task" } }).data, { editMode: true });
  assert.match(edit, /data-act="expand-types"[^>]*>⇶ Types 8</, "the Edit Mode toolbar offers Expand Types");
});

test("Expand Types: one node per type (struct / symbol), field-anchored labeled edges, reuse, one undo step", () => {
  const c = controller();
  const st = c.addNode({ type: "struct", data: { name: "task" } });
  c.content.set(c.fetchKeyOf(st), { status: "ok", value: TYPED });
  assert.equal(c.expandableTypesOf(st.id).length, 8);
  const existing = c.addNode({ type: "struct", data: { name: "inode", kind: "struct" } });
  const before = c.canvas.nodes.length;
  const created = c.expandStructTypes(st.id);
  assert.equal(created.length, 7, "the inode node on the canvas is reused");
  assert.equal(c.canvas.nodes.length, before + 7);
  const kinds = Object.fromEntries(created.map((n) => [n.data.name, `${n.type}:${n.data.kind}`]));
  assert.equal(kinds.mm_struct, "struct:struct");
  assert.equal(kinds.spinlock_t, "symbol:typedef");
  assert.equal(kinds.task_state, "symbol:enum");
  assert.ok(created.every((n) => n.x > st.x + st.w), "laid out to the right");
  const ys = created.map((n) => n.y);
  assert.deepEqual([...ys].sort((a, b) => a - b), ys, "stacked top to bottom");
  const from = c.canvas.edges.filter((e) => e.from === st.id);
  assert.equal(from.length, 8);
  const toMm = from.find((e) => e.to === created.find((n) => n.data.name === "mm_struct").id);
  assert.deepEqual([toMm.fromField, toMm.fromSide, toMm.toSide, toMm.label], ["5:mm", "right", "left", "points to"]);
  assert.equal(from.find((e) => e.to === existing.id).fromField, "11:open");
  assert.equal(from.find((e) => e.to === created.find((n) => n.data.name === "rcu_head").id).label, "embeds");
  assert.deepEqual(c.expandStructTypes(st.id), [], "a second expansion adds nothing");
  assert.equal(c.canvas.edges.filter((e) => e.from === st.id).length, 8, "nor connects twice");
  assert.equal(c.undo(), true);
  assert.equal(c.canvas.nodes.length, before, "the whole expansion is one undo step");
  assert.equal(c.canvas.edges.length, 0);
  // per-row follow of a typedef spawns its symbol node, anchored to the member
  const t = c.followStructType(st.id, "3:usage");
  assert.equal(t.type, "symbol");
  assert.equal(c.canvas.edges.find((e) => e.to === t.id).fromField, "3:usage");
  assert.equal(c.followableTypeFor(st.id, "3:usage"), "typedef atomic_t");
  assert.equal(c.followableTypeFor(st.id, "8:flags"), null);
});

// ------------------------------------------------------------------ table-style #ifdef geometry

const GUARDED = {
  version: "v3.0", name: "g", kind: "struct", file_path: "g.h", line_s: 1, line_e: 20, outer_conds: [],
  items: [
    { kind: "field", name: "a", type: "int", line: 2, conds: [] },
    { kind: "directive", directive: "ifdef", text: "#ifdef CONFIG_A", expr: "CONFIG_A", line: 3 },
    { kind: "field", name: "b", type: "long", line: 4, conds: ["CONFIG_A"] },
    { kind: "directive", directive: "ifdef", text: "#ifdef CONFIG_B", expr: "CONFIG_B", line: 5 },
    { kind: "field", name: "c", type: "char", line: 6, conds: ["CONFIG_A", "CONFIG_B"] },
    { kind: "directive", directive: "else", text: "#else", line: 7 },
    { kind: "field", name: "d", type: "u8", line: 8, conds: ["CONFIG_A"] },
    { kind: "directive", directive: "endif", text: "#endif", line: 9 },
    { kind: "directive", directive: "endif", text: "#endif", line: 10 },
  ],
};

test("table #ifdef geometry: row ports are out of the flex flow, every row starts with its #if cell", () => {
  const d = M.createNode("struct", { data: { name: "g", style: "table" } }).data;
  const html = S.renderStructBody(GUARDED, d);
  const rows = html.split(/(?=<div class="nm-sf )/).slice(1);
  assert.equal(rows.length, 9);
  for (const r of rows) {
    // field rows carry their ports, directive rows none: what follows the ports must be the #if cell either way
    const body = r.replace(/^<div[^>]*>\s*/, "").replace(/<span class="nm-port nm-port-field[^>]*><\/span>/g, "");
    assert.match(body, /^<span class="nm-sf-ppcell"/, `the #if cell leads the row: ${r.slice(0, 80)}`);
  }
  const portRule = /\.nm-port\.nm-port-field \{([^}]*)\}/.exec(CSS)?.[1] || "";
  assert.match(portRule, /position: absolute/, "field ports take no room in the row: the cell starts at x = 0 on every row");
  assert.match(CSS, /\.nm-port\.nm-port-field\.nm-port-left \{ left: 0; \}/);
  assert.match(CSS, /\.nm-pp-indent \{[^}]*margin-right: -4px/, "a nested directive steps in by its indent only, not an extra flex gap");
  assert.match(CSS, /\.nm-style-table \.nm-sf-type\.nm-sf-cell \{[^}]*width: calc\(var\(--nm-type-col, 12ch\) \+ 7px\);[^}]*box-sizing: border-box/,
    "the type column keeps one outer width at every member depth");
});

test("table #ifdef geometry in the SVG export: one x per track on every row, divider at the cell edge", () => {
  const node = M.createNode("struct", { id: "g", x: 0, y: 0, w: 500, h: 400, data: { name: "g", style: "table" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [node] }, (x) => x, { content: () => GUARDED });
  const bars = [...svg.matchAll(/<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)" stroke="var\(--syn-preproc\)"/g)]
    .filter((m) => m[1] === m[3]).map((m) => Number(m[1]));
  assert.ok(bars.length > 8);
  assert.deepEqual([...new Set(bars)].sort((a, b) => a - b), [9, 15], "track l sits at 2px border + 7 + 6·l on field and directive rows alike");
  const tracks = S.structRows(GUARDED, node.data).tracks;
  assert.equal(tracks, 2);
  assert.match(svg, new RegExp(`<line x1="${tracks * 6 + 6 + 5.5}" y1="[\\d.]+" x2="${tracks * 6 + 6 + 5.5}"`), "the cell divider");
  // nested directive text steps in by PP_INDENT, its chevron in the controls column
  assert.match(svg, /<text x="38"[^>]*>▾ <\/text><text x="58"[^>]*>#ifdef CONFIG_B<\/text>/);
  assert.match(svg, /<text x="28"[^>]*>▾ <\/text><text x="48"[^>]*>#ifdef CONFIG_A<\/text>/);
});

// ------------------------------------------------------------------ whole mail messages

test("mail: single-message nodes show and save the whole message; threads expand, pin and cap messages", () => {
  const full = D.fullMailBody(LONG_BODY);
  assert.ok(!full.includes("\r"));
  assert.ok(full.includes("> quoted line") && full.includes("line 30") && full.includes("Signed"), "nothing cut at 10 lines");
  const one = D.mailThreadModel({ message_id: "<m@x>", subject: "Hi", body: LONG_BODY }, { mid: "m@x", single: true });
  assert.equal(one.rows[0].body, full);
  const html = D.renderMailBody(one);
  assert.match(html, /<pre class="nm-mail-body nm-mail-body-single"/);
  assert.match(html, /<span class="nm-ml-q nm-ml-q1">&gt; quoted line<\/span>/);
  assert.match(html, /<span class="nm-ml-q nm-ml-q2">&gt;&gt; older quote<\/span>/);
  assert.match(html, /<span class="nm-ml-add">\+new<\/span>/);
  assert.match(html, /<span class="nm-ml-del">-old<\/span>/);
  assert.match(html, /<span class="nm-ml-hunk">@@ -1,2 \+1,2 @@<\/span>/);
  assert.match(html, /<span class="nm-ml-sig">Signed<\/span>/);
  assert.doesNotMatch(html, /data-act="mail-full"/, "a single message is always whole");
  // saved body: shows before (or without) a refetch
  const saved = M.createNode("mail", { data: { mid: "m@x", single: true, fullBody: full } }).data;
  assert.equal(saved.fullBody, full);
  assert.equal(D.mailThreadModel({ message_id: "m@x" }, saved).rows[0].body, full);
  assert.equal(M.createNode("mail", { data: { mid: "m@x", single: true, fullBody: "x".repeat(M.MAIL_BODY_MAX + 50) } }).data.fullBody.length, M.MAIL_BODY_MAX);
  assert.equal(M.createNode("mail", { data: { tid: 5, fullBody: "x" } }).data.fullBody, "", "a thread node keeps no single body");

  const bodies = new Map([["p1@x", LONG_BODY]]);
  const open = { tid: 5, openMids: { "p1@x": true } };
  const ex = D.mailThreadModel(THREAD, open, bodies).rows.find((r) => r.mid === "p1@x");
  assert.equal(ex.full, false);
  assert.ok(ex.excerpt.length <= 10, "an open thread message starts as its excerpt");
  assert.match(D.renderMailBody(D.mailThreadModel(THREAD, open, bodies)), /data-act="mail-full" data-key="p1@x"[^>]*>⤢ Show full message/);
  const whole = D.mailThreadModel(THREAD, { ...open, fullMids: { "p1@x": true } }, bodies).rows.find((r) => r.mid === "p1@x");
  assert.ok(whole.full && whole.body.includes("line 30"));
  const pinnedData = M.createNode("mail", { data: { tid: 5, openMids: { "p1@x": true }, fullMids: { "p1@x": true }, pinnedBodies: { "p1@x": full } } }).data;
  const pinned = D.mailThreadModel(THREAD, pinnedData, new Map()).rows.find((r) => r.mid === "p1@x");
  assert.ok(pinned.open && pinned.full && pinned.pinned && pinned.body === full, "a pinned message survives a reload without its fetch");
  const pinnedExcerpt = D.mailThreadModel(THREAD, { ...pinnedData, fullMids: {} }, new Map([["p1@x", "(network error)"]])).rows.find((r) => r.mid === "p1@x");
  assert.ok(!pinnedExcerpt.full && pinnedExcerpt.pinned && pinnedExcerpt.excerpt.includes("Hi all,"), "back to its excerpt, the pin (preferred over a failed fetch) stays");
  const cut = D.fullMailBody("z".repeat(M.MAIL_BODY_MAX + 10));
  assert.ok(cut.length <= M.MAIL_BODY_MAX && cut.endsWith("characters)"), "the cut note fits inside the cap");
  const many = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`m${i}@x`, "y".repeat(100_000)]));
  const capped = M.createNode("mail", { data: { tid: 5, pinnedBodies: many } }).data.pinnedBodies;
  const total = Object.values(capped).reduce((a, b) => a + b.length, 0);
  assert.ok(Object.keys(capped).length <= M.MAIL_PIN_MAX && total <= M.MAIL_BODY_MAX * 2, "pins are capped per node");
});

test("mail: Show full message and Pin toggle per message; the pin is one undoable canvas change", async () => {
  const c = controller();
  const th = c.addNode({ type: "mail", data: { tid: 5 } });
  c.content.set(c.fetchKeyOf(th), { status: "ok", value: THREAD });
  c.mailBodies.set("p1@x", LONG_BODY);
  await c.toggleMailMessage(th.id, "p1@x", "full");
  assert.deepEqual([th.data.openMids, th.data.fullMids], [{ "p1@x": true }, { "p1@x": true }]);
  await c.toggleMailMessage(th.id, "p1@x", "pin");
  assert.equal(th.data.pinnedBodies["p1@x"], D.fullMailBody(LONG_BODY));
  await c.toggleMailMessage(th.id, "p1@x", "full");
  assert.deepEqual(th.data.fullMids, {});
  assert.ok(th.data.pinnedBodies["p1@x"], "Excerpt keeps the pin");
  c.undo();
  c.undo();
  assert.deepEqual(th.data.pinnedBodies, {}, "undo drops the pin");
  c.api = { getMailMessage: async () => { throw new Error("offline"); } };
  await c.toggleMailMessage(th.id, "r1@x", "pin");
  assert.equal(th.data.pinnedBodies["r1@x"], undefined, "a failed fetch is never pinned");
  await c.toggleMailMessage(th.id, "p1@x", "open");
  assert.deepEqual([th.data.openMids, th.data.fullMids], [{}, {}], "closing a message closes its full view");
});

// ------------------------------------------------------------------ row-anchored edges

test("row anchors: mail spawns wire from the message row, history spawns from the commit / release row", () => {
  const c = controller();
  const th = c.addNode({ type: "mail", data: { tid: 5 } });
  c.content.set(c.fetchKeyOf(th), { status: "ok", value: THREAD });
  const html = D.renderMailBody(c.modelOf(th));
  assert.match(html, /<div class="nm-mail-row[^"]*" data-mid="p1@x" data-key="mail:p1@x">/);
  assert.match(html, /<span class="nm-port nm-port-field nm-port-left" data-field="mail:p1@x" data-side="left">/);
  clickAct(c, th.id, "mail-spawn", "p1@x");
  const e1 = c.canvas.edges.at(-1);
  assert.deepEqual([e1.from, e1.fromField, e1.fromSide, e1.toSide, e1.label], [th.id, "mail:p1@x", "right", "left", "reply"]);
  clickAct(c, th.id, "mail-spawn", "r1@x");
  const e2 = c.canvas.edges.at(-1);
  assert.notEqual(e2.from, th.id, "a reply whose parent has its own node hangs off that node");
  assert.equal(e2.fromField, null);

  const blame = c.addNode({ type: "history", data: { mode: "blame", path: "kernel/fork.c" } });
  const sha = "abcdef1234567890";
  c.content.set(c.fetchKeyOf(blame), { status: "ok", value: { path: "kernel/fork.c", lines: [{ commit_hash: sha, line: 3, author_name: "A", subject: "s" }] } });
  assert.match(D.renderHistoryBody(c.modelOf(blame)), new RegExp(`<div class="nm-hist-row" data-key="commit:${sha}"><span class="nm-port nm-port-field nm-port-left" data-field="commit:${sha}"`));
  clickAct(c, blame.id, "hist-spawn-commit", sha);
  assert.equal(c.canvas.edges.at(-1).fromField, `commit:${sha}`);
  const hist = c.addNode({ type: "history", data: { path: "kernel/fork.c", line: 10 } });
  c.content.set(c.fetchKeyOf(hist), { status: "ok", value: { timeline: [{ vname: "v3.1", prev_vname: "v3.0", status: "modified", path: "kernel/fork.c" }] } });
  assert.match(D.renderHistoryBody(c.modelOf(hist)), /data-key="ver:v3\.1"/);
  clickAct(c, hist.id, "hist-spawn-diff", "v3.0..v3.1:kernel/fork.c");
  assert.equal(c.canvas.edges.at(-1).fromField, "ver:v3.1");
});

test("row anchors: fieldPoint / refreshPortOffsets find mail and history rows, at the header line, clamped to the scroller", () => {
  const c = controller();
  const th = c.addNode({ type: "mail", data: { tid: 5 } });
  Object.assign(th, { x: 100, y: 50, w: 300, h: 200 });
  const rect = (top, height) => ({ top, height, bottom: top + height, left: 0, right: 300 });
  const line = (top) => ({ getBoundingClientRect: () => rect(top, 18) });
  const rows = {
    "mail:root@x": { dataset: { key: "mail:root@x" }, getBoundingClientRect: () => rect(40, 300), querySelector: (s) => (s === ".nm-anchor-line" ? line(40) : null) },
    "mail:p1@x": { dataset: { key: "mail:p1@x" }, getBoundingClientRect: () => rect(400, 40), querySelector: (s) => (s === ".nm-anchor-line" ? line(400) : null) },
  };
  const selectors = [];
  const scroller = { getBoundingClientRect: () => rect(30, 160) };
  c.renderer.element = () => ({
    getBoundingClientRect: () => rect(0, 200),
    querySelector: (sel) => {
      selectors.push(sel);
      if (sel === ".nm-scroll") return scroller;
      const k = /\.nm-mail-row\[data-key="([^"]+)"\]/.exec(sel)?.[1];
      return k && rows[k] ? { ...rows[k], closest: () => scroller } : null;
    },
    querySelectorAll: (sel) => (sel.includes(".nm-mail-row[data-key]") && sel.includes(".nm-hist-row[data-key]") ? Object.values(rows) : []),
  });
  const p = c.fieldPoint(th, "mail:root@x", "right");
  assert.deepEqual(p, { x: 400, y: 50 + 49 }, "the message's header line, not the middle of its open body");
  assert.ok(selectors.some((s) => s.includes('.nm-mail-row[data-key="mail:root@x"]')));
  c.refreshPortOffsets(th.id);
  assert.equal(c.portOffsets.get(th.id).get("mail:p1@x"), 30 + 160 - 4, "a row scrolled out of view pins to the scroller's bottom");
  assert.equal(c.portOffsets.get(th.id).get("mail:root@x"), 49);
});

test("row anchors in the SVG export: mail and history edges leave from their row", () => {
  const c = controller();
  const th = c.addNode({ type: "mail", data: { tid: 5 } });
  Object.assign(th, { x: 0, y: 0, w: 300, h: 400 });
  const diff = c.addNode({ type: "diff", data: { mode: "commit", commitHash: "abcdef12" } });
  Object.assign(diff, { x: 600, y: 0, w: 300, h: 200 });
  c.content.set(c.fetchKeyOf(th), { status: "ok", value: THREAD });
  const edgeY = (field) => {
    const canvas = { ...M.emptyCanvas(), nodes: [th, diff], edges: [M.createEdge({ from: th.id, to: diff.id, fromSide: "right", toSide: "left", fromField: field })] };
    const svg = canvasToSvg(canvas, (x) => x, { content: (n) => c.modelOf(n) });
    return Number(/<path d="M\s*300[ ,]([\d.]+)/.exec(svg)?.[1]);
  };
  const root = edgeY("mail:root@x");
  const p1 = edgeY("mail:p1@x");
  const r1 = edgeY("mail:r1@x");
  assert.ok(root < p1 && p1 < r1, `rows top to bottom: ${root} ${p1} ${r1}`);
  assert.equal(p1 - root, 18 + 14, "one header line and one meta line per closed message");
  const blame = c.addNode({ type: "history", data: { mode: "blame", path: "f.c" } });
  Object.assign(blame, { x: 0, y: 0, w: 300, h: 300 });
  const lines = [{ commit_hash: "aaaa1111", line: 1 }, { commit_hash: "bbbb2222", line: 2 }];
  c.content.set(c.fetchKeyOf(blame), { status: "ok", value: { path: "f.c", lines } });
  const hy = (field) => {
    const canvas = { ...M.emptyCanvas(), nodes: [blame, diff], edges: [M.createEdge({ from: blame.id, to: diff.id, fromSide: "right", toSide: "left", fromField: field })] };
    return Number(/<path d="M\s*300[ ,]([\d.]+)/.exec(canvasToSvg(canvas, (x) => x, { content: (n) => c.modelOf(n) }))?.[1]);
  };
  assert.equal(hy("commit:bbbb2222") - hy("commit:aaaa1111"), 16);
});

// ------------------------------------------------------------------ beautified doc nodes

test("doc nodes render through the Kernel Docs renderers inside .kdoc-body; scripts and handlers never pass", () => {
  const rst = D.docModel({ path: "Documentation/x.rst", content: "Title\n=====\n\nSome *text*, ``code`` and :doc:`other`.\n\n.. note::\n   careful\n\n.. code-block:: c\n\n   int x;\n\n=====  =====\nA      B\n=====  =====\n1      2\n=====  =====\n" });
  const html = D.renderDocBody(rst);
  assert.match(html, /<div class="nm-doc nm-doc-rendered nm-scroll"><div class="kdoc-body nm-doc-page">/);
  assert.match(html, /<h1 id="title">Title/);
  assert.match(html, /<em>text<\/em>/);
  assert.match(html, /class="admonition note"/);
  assert.match(html, /<span class="hl-k">int<\/span>/, "code blocks are syntax highlighted");
  assert.match(html, /<table class="docutils/);
  assert.ok(rst.sections.length >= 1, "the plain outline stays for the vector export");
  const md = D.renderDocBody(D.docModel({ path: "README.md", content: "# Hello\n\n<script>alert(1)</script>\n\n[x](http://a) `y`" }));
  assert.match(md, /<h1 id="hello">/);
  assert.doesNotMatch(md, /<script/);
  assert.match(md, /target="_blank" rel="noopener noreferrer"/);
  const page = D.renderDocBody(D.docModel({ path: "Documentation/a.html", content: '<html><body><h2 onclick="x()">Hi</h2><script>bad()</script><a href="javascript:alert(1)">l</a><iframe src="x"></iframe></body></html>' }));
  assert.match(page, /Hi<\/h2>|Hi</);
  assert.doesNotMatch(page, /<script|onclick|javascript:|<iframe/i);
  assert.equal(D.scrubDocHtml('<img src="data:image/png;base64,AA" onerror="x()"><a href="data:text/html,1">d</a><style>body{}</style>'), '<img src="data:image/png;base64,AA" ><a href="#">d</a>');
  for (const evil of ['<img src=x o onerror="a"nerror="alert(1)">', "<svg/onload=alert(1)>", "<img/onerror=alert(1) src=x>", '<a/href="javascript:alert(1)">x</a>',
    '<a href="&#106;avascript:alert(1)">x</a>', '<a href="java&Tab;script:alert(1)">x</a>', '<a href=" JaVaScRiPt:alert(1)">x</a>',
    '<div style="position:fixed;inset:0">x</div>', '<button data-act="refresh">x</button>', '<p data-act="refresh">x</p>', "<scr<script>ipt>alert(1)</script>"]) {
    const out = D.scrubDocHtml(evil);
    assert.doesNotMatch(out, /on(error|load)\s*=|javascript|&#106;|&Tab;|style=|data-act|<script|<svg|<button/i, `${evil} -> ${out}`);
  }
  const kd = D.renderDocBody(D.docModel({ path: "kernel/fork.c", entries: [{ kind: "function", name: "copy_process", brief: "create a @clone_flags task", signature: "struct task_struct *copy_process(unsigned long clone_flags)", line: 42, params: [{ name: "clone_flags", desc: "flags of &struct kernel_clone_args" }], sections: [{ title: "Return", body: "the new task" }] }] }));
  assert.match(kd, /<dl class="c function">/);
  assert.match(kd, /<code class="sig-name"><span class="hl-k">struct<\/span>/);
  assert.match(kd, /<p class="rubric">Parameters<\/p>/);
  assert.match(kd, /data-src="kernel\/fork\.c" data-line="42"/);
});

test("doc nodes export as a foreignObject of the live page when mounted, as a vector outline otherwise", () => {
  const node = M.createNode("doc", { id: "d", x: 0, y: 0, w: 400, h: 300, data: { path: "Documentation/x.rst" } });
  const model = D.docModel({ path: "Documentation/x.rst", content: "Intro\n=====\n\nBody text.\n" });
  const canvas = { ...M.emptyCanvas(), nodes: [node] };
  const vector = canvasToSvg(canvas, (x) => x, { content: () => model });
  assert.doesNotMatch(vector, /<foreignObject/);
  assert.match(vector, />Intro</);
  const live = canvasToSvg(canvas, (x) => x, { content: () => model, foreign: (n) => (n.type === "doc" ? { html: '<div class="nm-doc"><h1>Intro</h1></div>', w: 400, h: 274 } : null) });
  assert.match(live, /<foreignObject x="0" y="26" width="400" height="274"><div xmlns="http:\/\/www\.w3\.org\/1999\/xhtml"[^>]*><div class="nm-doc"><h1>Intro<\/h1>/);
});

test("doc links in a doc node spawn connected nodes instead of leaving the page", () => {
  const c = controller();
  const doc = c.addNode({ type: "doc", data: { path: "Documentation/x.rst" } });
  const click = (a) => {
    let prevented = false;
    const nodeEl = { dataset: { nodeId: doc.id }, querySelector: () => null };
    c.onClick({
      target: { closest: (s) => (s === "[data-act]" ? null : s === ".nm-node" ? nodeEl : s === ".nm-doc-page" ? {} : s === "a" ? a : null) },
      preventDefault: () => { prevented = true; }, stopPropagation() {},
    });
    return prevented;
  };
  const link = (dataset, href = "#") => ({ dataset, classList: { contains: (k) => k === "external" && /^http/.test(href) }, getAttribute: () => href });
  assert.equal(click(link({ doc: "Documentation/other.rst#sec" })), true);
  const n1 = c.canvas.nodes.at(-1);
  assert.deepEqual([n1.type, n1.data.path], ["doc", "Documentation/other.rst"]);
  assert.equal(c.canvas.edges.at(-1).label, "links to");
  click(link({ src: "kernel/fork.c", line: "42" }));
  assert.deepEqual([c.canvas.nodes.at(-1).type, c.canvas.nodes.at(-1).data.start], ["source", 42]);
  const count = c.canvas.nodes.length;
  assert.equal(click(link({}, "https://kernel.org")), false, "external links open in a new tab, untouched");
  assert.equal(c.canvas.nodes.length, count);
});
