/**
 * nodemap_export_parity.test.mjs - NodeMap SVG export parity with the live renderer (`nodemap.css`,
 * `codeview.css`, `variables.css`): CSS color evaluation (`utils/css_color.js`), every theme's tokens,
 * lock / mail / commit / history / Kconfig / CVE / KAPI / struct / note / group colors and parts, scroll
 * offsets, fonts, measured text and portable SVG 1.1 paints.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as M from "../js/views/nodemap/canvas_model.js";
import * as D from "../js/views/nodemap/domain_nodes.js";
import { EXPORT_METRICS, LOCK_COLORS, TOKEN_STYLES, canvasToSvg, captureNodeForeignObject } from "../js/views/nodemap/nodemap_export.js";
import { evalCssColor, formatCssColor, hexOfCssColor, mixCssColors, mixRgba, parseCssColor, substituteVars, svgPaint } from "../js/utils/css_color.js";
import { resolveColor, resolveFont } from "../js/utils/theme.js";

const CSS = (f) => readFileSync(new URL(`../css/${f}`, import.meta.url), "utf8");

/** Token values of each theme block of variables.css (dark `:root`, then each `data-theme` override). */
function themes() {
  const blocks = {};
  for (const m of CSS("variables.css").matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sel = m[1].trim().split("\n").pop().trim();
    blocks[sel] = Object.fromEntries([...m[2].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((x) => [x[1], x[2].trim()]));
  }
  const base = blocks[":root"];
  const out = { dark: base };
  for (const t of ["light", "hc-dark", "hc-light"]) out[t] = { ...base, ...blocks[`:root[data-theme="${t}"]`] };
  return out;
}
const THEMES = themes();
const readerOf = (theme) => (name) => THEMES[theme][name] || "";
const themeResolve = (theme) => (c) => resolveColor(c, readerOf(theme));
const named = (c) => String(c).replace(/^var\(--([\w-]+)\)$/, "[$1]");

/** Every `fill` / `stroke` paint of an SVG document. */
const paints = (svg) => [...svg.matchAll(/\s(fill|stroke)="([^"]*)"/g)].map((m) => m[2]);
const sha = (i) => String(i).repeat(40).slice(0, 40);

// ------------------------------------------------------------------ CSS color evaluation

test("css colors: hex, rgb() / rgba() in comma and space syntax, percentages, transparent", () => {
  assert.deepEqual(parseCssColor("#abc"), { r: 170, g: 187, b: 204, a: 1 });
  assert.deepEqual(parseCssColor("#abcd"), { r: 170, g: 187, b: 204, a: 221 / 255 });
  assert.deepEqual(parseCssColor("#102030"), { r: 16, g: 32, b: 48, a: 1 });
  assert.equal(Math.round(parseCssColor("#10203080").a * 100) / 100, 0.5);
  assert.deepEqual(parseCssColor("rgba(240, 246, 252, 0.1)"), { r: 240, g: 246, b: 252, a: 0.1 });
  assert.deepEqual(parseCssColor("rgb(10 20 30 / 25%)"), { r: 10, g: 20, b: 30, a: 0.25 });
  assert.deepEqual(parseCssColor("rgb(100% 0% 50%)"), { r: 255, g: 0, b: 127.5, a: 1 });
  assert.deepEqual(parseCssColor("transparent"), { r: 0, g: 0, b: 0, a: 0 });
  assert.equal(formatCssColor(parseCssColor("hsl(120, 100%, 25%)")), "#008000");
  assert.equal(formatCssColor(parseCssColor("hsl(0 100% 50% / 0.5)")), "rgba(255, 0, 0, 0.5)");
  assert.equal(parseCssColor("var(--x)"), null);
  assert.equal(parseCssColor("rgb(1, 2)"), null);
  assert.equal(formatCssColor({ r: 1, g: 2, b: 3, a: 1 }), "#010203");
  assert.equal(formatCssColor({ r: 1, g: 2, b: 3, a: 0.5 }), "rgba(1, 2, 3, 0.5)", "alpha is preserved");
});

test("css colors: nested var() with fallbacks and color-mix(in srgb) with the CSS Color 5 rules", () => {
  const read = (n) => ({ "--a": "#ff0000", "--b": "var(--a)", "--c": "color-mix(in srgb, var(--a) 50%, #0000ff)", "--glass": "rgba(0, 0, 0, 0.5)" }[n] || "");
  assert.equal(substituteVars("var(--b)", read), "#ff0000", "a token naming a token");
  assert.equal(substituteVars("var(--missing, var(--a))", read), "#ff0000", "nested fallback");
  assert.equal(substituteVars("var(--missing)", read), null);
  assert.equal(formatCssColor(evalCssColor("var(--c)", read)), "#800080");
  assert.equal(mixCssColors("#ffffff", "#000000", 25), "#404040");
  assert.equal(formatCssColor(evalCssColor("color-mix(in srgb, #ff0000, #0000ff)")), "#800080", "no percentage: 50 / 50");
  assert.equal(formatCssColor(evalCssColor("color-mix(in srgb, #ff0000 30%, #0000ff 30%)")), "rgba(128, 0, 128, 0.6)", "a sum under 100% scales the alpha");
  assert.equal(formatCssColor(evalCssColor("color-mix(in srgb, #ff0000 5%, transparent)")), "rgba(255, 0, 0, 0.05)", "mixing into transparent keeps the hue (premultiplied)");
  assert.equal(formatCssColor(evalCssColor("color-mix(in srgb, var(--glass) 50%, #ffffff)", read)), "rgba(170, 170, 170, 0.75)");
  assert.deepEqual(mixRgba({ r: 0, g: 0, b: 0, a: 0 }, { r: 0, g: 0, b: 0, a: 0 }, 50), { r: 0, g: 0, b: 0, a: 0 });
  assert.deepEqual(svgPaint("rgba(10, 20, 30, 0.25)"), { color: "#0a141e", opacity: 0.25 });
  assert.deepEqual(svgPaint("[x]"), { color: "[x]", opacity: 1 }, "an unreadable color passes through");
  assert.equal(hexOfCssColor("rgb(1 2 3 / 0.5)"), "#010203");
  assert.equal(M.colorToHex("rgba(255, 128, 0, 0.3)"), "#ff8000", "the native color input reads space and comma syntax alike");
  assert.equal(M.colorToHex("color-mix(in srgb, #ffffff 50%, #000000)"), "#808080");
});

test("theme.js: resolveColor evaluates nested tokens and mixes; resolveFont makes an attribute-safe stack", () => {
  const read = (n) => ({ "--accent-blue": "#58a6ff", "--x": "var(--accent-blue)", "--border-muted": "rgba(240, 246, 252, 0.1)", "--font-mono": '"JetBrains Mono", monospace' }[n] || "");
  assert.equal(resolveColor("var(--x)", read), "#58a6ff");
  assert.equal(resolveColor("var(--border-muted)", read), "rgba(240, 246, 252, 0.1)", "alpha kept");
  assert.equal(resolveColor("color-mix(in srgb, var(--accent-blue) 22%, #000000)", read), "#132538");
  assert.equal(resolveColor("var(--nope)", read), "");
  assert.equal(resolveFont("--font-mono", "monospace", read), "'JetBrains Mono', monospace");
  assert.equal(resolveFont("--font-sans", "sans-serif", read), "sans-serif");
});

// ------------------------------------------------------------------ themes

/** One node of every type, with content, for whole-canvas checks. */
function everyType() {
  const mk = (type, over) => M.createNode(type, over);
  const nodes = [
    mk("group", { id: "g", x: -40, y: -60, w: 2000, h: 1200, color: "purple", bgColor: "cyan" }),
    mk("struct", { id: "s", x: 0, y: 0, w: 300, h: 220, color: "green", locks: [{ kind: "rcu" }], data: { name: "n", style: "list" } }),
    mk("source", { id: "src", x: 320, y: 0, data: { path: "a.c" } }),
    mk("note", { id: "n", x: 640, y: 0, data: { text: "# T\n**b** `c`" } }),
    mk("symbol", { id: "sy", x: 960, y: 0, data: { name: "f", kind: "func", expanded: true, constituents: [{ name: "g" }] } }),
    mk("diff", { id: "d", x: 0, y: 300, data: { path: "a.c" } }),
    mk("mail", { id: "m", x: 320, y: 300, data: { tid: 5 } }),
    mk("kconfig", { id: "k", x: 640, y: 300, data: { symbol: "SMP" } }),
    mk("maintainer", { id: "mt", x: 960, y: 300, data: { title: "S" } }),
    mk("cve", { id: "c", x: 0, y: 600, data: { cveId: "CVE-1" } }),
    mk("kapi", { id: "ka", x: 320, y: 600, data: { catalog: "syscalls" } }),
    mk("doc", { id: "doc", x: 640, y: 600, data: { path: "x.rst" } }),
    mk("history", { id: "h", x: 960, y: 600, data: { mode: "blame", path: "a.c" } }),
    mk("commit", { id: "cm", x: 1280, y: 600, data: { commitHash: "abcdef1" } }),
    mk("tab", { id: "t", x: 1280, y: 0, data: { tabType: "commits", hash: "tab=commits" } }),
    mk("man", { id: "man", x: 1280, y: 300, data: { name: "open", section: "2" } }),
  ];
  const models = {
    s: { name: "n", kind: "struct", outer_conds: [], items: [{ kind: "field", name: "a", type: "int", line: 2, conds: [] }] },
    src: { path: "a.c", start_line: 1, end_line: 1, total_lines: 1, lines: ["static int x; /* c */"] },
    d: D.diffPayload({ mode: "file", path: "a.c" }, { diff: "--- a/a.c\n+++ b/a.c\n@@ -1 +1 @@\n-a\n+b\n" }),
    m: D.mailThreadModel({ tid: 5, messages: [{ message_id: "r@x", subject: "[PATCH] x", author: "A", is_patch: true }] }, { openMids: { "r@x": true } }),
    k: D.kconfigModel({ name: "SMP", type: "bool", depends_on: ["X86"] }, null, "SMP"),
    mt: D.maintainerModel({ name: "S", status: "Maintained", members: [{ name: "I", email: "i@x", role: "M" }] }),
    c: D.cveModel({ id: "CVE-1", title: "t", cvss: { score: 9 }, kev: "y", release_state: { state: "affected" }, files: ["a.c"] }),
    ka: { catalog: "syscalls", arch: "", total: 1, items: [{ kind: "syscall", name: "open", subtitle: "o" }] },
    doc: D.docModel({ path: "x.rst", content: "T\n=\n\nx\n" }),
    h: { mode: "blame", path: "a.c", totalLines: 1, commits: [{ commitHash: sha(3), author: "L", subject: "s", lineStart: 1, lineEnd: 1, count: 1 }] },
    cm: D.commitModel({ commit_hash: "abcdef1", subject: "s", message: "s\n\nbody\n\nSigned-off-by: A <a@x>", files: [{ change_type: "M", file: "a.c" }] }),
  };
  const edges = [M.createEdge({ from: "s", to: "src", label: "x", lockKind: "rcu", color: "pink" })];
  return { canvas: { ...M.emptyCanvas(), nodes, edges, settings: M.normalizeSettings({ bgColor: "orange" }) }, content: (n) => models[n.id] || null };
}

test("themes: every token the export uses resolves to a color in dark, light, hc-dark and hc-light", () => {
  const src = readFileSync(new URL("../js/views/nodemap/nodemap_export.js", import.meta.url), "utf8");
  const used = [...new Set([...src.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]))].filter((t) => t in THEMES.dark && !t.startsWith("--font") && t !== "--shadow-sm");
  for (const t of ["--bg-canvas", "--border-muted", "--accent-yellow", "--syn-preproc"]) assert.ok(used.includes(t), t);
  for (const theme of Object.keys(THEMES)) {
    for (const t of [...used, "--accent-pink"]) assert.ok(parseCssColor(resolveColor(`var(${t})`, readerOf(theme))), `${theme}: ${t}`);
  }
  const own = Object.fromEntries([...CSS("variables.css").matchAll(/:root\[data-theme="hc-dark"\]\s*\{([^}]*)\}/g)].flatMap((m) => [...m[1].matchAll(/(--[\w-]+)\s*:/g)].map((x) => [x[1], true])));
  for (const t of ["--bg-canvas", "--border-muted", "--accent-pink", "--accent-rose", "--bg-glass", "--text-inverse", "--text-on-accent", "--kdoc-inline-code-bg"]) {
    assert.ok(own[t], `hc-dark defines ${t} itself (not the dark :root value)`);
  }
});

test("themes: the whole canvas exports with literal SVG 1.1 paints in every theme (no var(), color-mix() or rgba())", () => {
  const { canvas, content } = everyType();
  for (const theme of Object.keys(THEMES)) {
    for (const transparent of [false, true]) {
      const svg = canvasToSvg(canvas, themeResolve(theme), { content, transparent, kconfigValues: () => ({ SMP: "y" }) });
      const bad = paints(svg).filter((p) => !/^(#[0-9a-f]{6}|none|url\(#[\w-]+\))$/.test(p));
      assert.deepEqual(bad, [], `${theme}${transparent ? " transparent" : ""}`);
      assert.doesNotMatch(svg, /var\(--|color-mix\(|rgba\(/);
      assert.doesNotMatch(svg, /<foreignObject/, "vector nodes stay pure vector");
    }
  }
  const dark = canvasToSvg(canvas, themeResolve("dark"), { content });
  const bg = mixCssColors(THEMES.dark["--accent-orange"], THEMES.dark["--bg-canvas"], 22);
  assert.match(dark, new RegExp(`<rect x="-?\\d+" y="-?\\d+" width="\\d+" height="\\d+" fill="${bg}"/>`), "a preset canvas background is the accent tinted 22% into --bg-canvas");
  assert.match(dark, /fill-opacity="0\.1"/, "translucent tokens become fill-opacity");
  assert.match(dark, /<filter id="nmshadow"[^>]*><feDropShadow dx="0" dy="1" stdDeviation="1\.5" flood-color="#000000" flood-opacity="0\.4"\/><\/filter>/, "--shadow-sm under every node");
  assert.doesNotMatch(canvasToSvg(canvas, themeResolve("dark"), { content, transparent: true }), /feDropShadow|paint-order/, "a transparent image has no shadows or label halos");
  const one = canvasToSvg({ ...M.emptyCanvas(), nodes: [M.createNode("note", { id: "q", x: 0, y: 0, w: 100, h: 60, bgColor: "#204060" })] }, themeResolve("dark"));
  assert.equal((one.match(/fill="#204060"/g) || []).length, 1, "the panel is painted once (with its shadow)");
  assert.doesNotMatch(canvasToSvg(canvas, () => "", { content }), /(fill|stroke)=""/, "a missing token paints nothing, never an invalid value");
});

// ------------------------------------------------------------------ frames: locks, groups, accents, headers, edges

test("lock badges and strips take the per-kind colors of .nm-lock-<kind> on a --bg-primary strip", () => {
  const kinds = M.LOCK_KINDS;
  const n = M.createNode("source", { id: "s", x: 0, y: 0, w: 1400, h: 200, data: { path: "a.c" }, locks: kinds.map((kind) => ({ kind })) });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named);
  for (const kind of kinds) {
    const tok = named(LOCK_COLORS[kind]);
    assert.match(svg, new RegExp(`fill="\\[bg-tertiary\\]" stroke="\\${tok.slice(0, -1)}\\]"[^>]*/><text[^>]*fill="\\${tok.slice(0, -1)}\\]"[^>]*>🔒 ${kind}<`), kind);
  }
  assert.match(svg, /<rect x="0" y="33" width="1400" height="25\.5" fill="\[bg-primary\]"\/>/, "strip: --bg-primary, 4 + 16.5 + 4 + 1 units under the header");
  assert.deepEqual(Object.keys(LOCK_COLORS).sort(), [...kinds].sort());
  const wrapped = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...n, w: 200 }] }, named);
  assert.match(wrapped, /height="(\d+(\.\d+)?)" fill="\[bg-primary\]"/);
  assert.ok(Number(/height="([\d.]+)" fill="\[bg-primary\]"/.exec(wrapped)[1]) > 25.5, "badges wrap like the flex row instead of being cut");
  const field = { name: "n", kind: "struct", outer_conds: [], items: [{ kind: "field", name: "lock", type: "spinlock_t", line: 2, conds: [] }] };
  const st = M.createNode("struct", { id: "st", x: 0, y: 0, w: 400, h: 200, data: { name: "n", fieldLocks: { "2:lock": { kind: "mutex", lockName: "m" } } } });
  const s2 = canvasToSvg({ ...M.emptyCanvas(), nodes: [st] }, named, { content: () => field });
  if (/🔒 mutex: m/.test(s2)) assert.match(s2, /stroke="\[accent-blue\]"[^>]*\/><text[^>]*fill="\[accent-blue\]"[^>]*>🔒 mutex: m</, "struct field badges use the kind color too");
});

test("groups: bgColor, 5% accent fill, border mixed 60% into --border-color, GROUP badge; accented borders 55%, notes 8% / 10%", () => {
  const T = THEMES.dark;
  const r = themeResolve("dark");
  const g = M.createNode("group", { id: "g", x: 0, y: 0, w: 400, h: 300, color: "red" });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [g] }, r);
  assert.match(svg, new RegExp(`fill="${T["--accent-red"]}" fill-opacity="0\\.05" stroke="${mixCssColors(T["--accent-red"], T["--border-color"], 60)}" stroke-width="1\\.5" stroke-dasharray="6 4"`));
  assert.match(svg, />GROUP</, "the group head has its badge");
  const bg = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...g, bgColor: "#204060" }] }, r);
  assert.match(bg, /fill="#204060" stroke=/, "a group background wins over the 5% tint");
  const tinted = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...g, bgColor: "green" }] }, r);
  assert.match(tinted, new RegExp(`fill="${mixCssColors(T["--accent-green"], T["--bg-secondary"], 22)}" stroke=`));
  const note = M.createNode("note", { id: "n", x: 0, y: 0, w: 200, h: 100, data: { text: "x" } });
  const plain = canvasToSvg({ ...M.emptyCanvas(), nodes: [note] }, r);
  assert.match(plain, new RegExp(`fill="${mixCssColors(T["--accent-yellow"], T["--bg-secondary"], 8)}"`), "a note is 8% yellow into --bg-secondary");
  assert.match(plain, new RegExp(`fill="${T["--accent-yellow"]}" fill-opacity="0\\.16"`), "its badge is yellow");
  const acc = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...note, color: "cyan" }] }, r);
  assert.match(acc, new RegExp(`fill="${mixCssColors(T["--accent-cyan"], T["--bg-secondary"], 10)}"`), "an accented note is 10%");
  assert.match(acc, new RegExp(`rx="6" fill="none" stroke="${mixCssColors(T["--accent-cyan"], T["--border-color"], 55)}"`), "accented border: 55% into --border-color");
});

test("headers: live geometry, title clipped like .nm-title, hidden header dashed in Edit Mode", () => {
  const n = M.createNode("source", { id: "s", x: 0, y: 0, w: 300, h: 120, customTitle: "A very long title that does not fit in the header at all", data: { path: "a.c" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named);
  assert.equal(EXPORT_METRICS.HEAD_H, 33, "1 border + 3 top border + 5 + 18 + 5 + 1 bottom border");
  assert.match(svg, /<rect x="0" y="1" width="300" height="3" fill="\[border-muted\]"\/>/, "uncolored top border");
  assert.match(svg, /<g clip-path="url\(#nmclip\d+\)"><text[^>]*font-weight="600" xml:space="preserve">A very long title that does not fit in the header at all<\/text><\/g>/, "the whole title, clipped (no ellipsis)");
  const hidden = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...n, hideHeader: true }] }, named, { editMode: true });
  assert.match(hidden, /<g opacity="0\.45"><rect[^>]*\/><line[^>]*stroke-width="3" stroke-dasharray="6 4"\/><line[^>]*stroke-dasharray="4 3"\/>/, "Edit Mode: dimmed and dashed");
});

test("edges: 0.9 line opacity, lock tag in parentheses, label font and halo in the canvas color", () => {
  const a = M.createNode("note", { id: "a", x: 0, y: 0, w: 100, h: 60 });
  const b = M.createNode("note", { id: "b", x: 300, y: 0, w: 100, h: 60 });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [a, b], edges: [M.createEdge({ from: "a", to: "b", lockKind: "rcu" })] }, named, { fonts: { sans: 'Inter, "Noto Sans", sans-serif' } });
  assert.match(svg, /stroke="\[accent-blue\]" stroke-opacity="0\.9" stroke-width="2"/);
  assert.match(svg, /font-family="Inter, 'Noto Sans', sans-serif" font-size="11">\(🔒 rcu\)</, "the live label text; quotes made attribute-safe");
  const labeled = canvasToSvg({ ...M.emptyCanvas(), nodes: [a, b], edges: [M.createEdge({ from: "a", to: "b", label: "rcu read", lockKind: "rcu" })] }, named);
  assert.match(labeled, />rcu read</, "a label naming the lock kind is kept as it is");
  const css = CSS("nodemap.css");
  assert.match(css, /\.nm-edge-label \{[^}]*stroke: var\(--nm-canvas-bg, var\(--bg-canvas\)\)/, "the live halo is the canvas background too");
});

test("snap grid: the canvas dots are a pattern on opaque images only", () => {
  const n = M.createNode("note", { id: "a", x: 0, y: 0, w: 100, h: 60 });
  const canvas = { ...M.emptyCanvas(), nodes: [n], settings: M.normalizeSettings({ snap: true, grid: 20 }) };
  assert.match(canvasToSvg(canvas, named), /<pattern id="nmgrid" x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="10" cy="10" r="1" fill="\[border-color\]"\/><\/pattern>/);
  assert.doesNotMatch(canvasToSvg(canvas, named, { transparent: true }), /nmgrid/);
  assert.doesNotMatch(canvasToSvg({ ...canvas, settings: M.normalizeSettings({ snap: false }) }, named), /nmgrid/);
});

// ------------------------------------------------------------------ domain nodes

test("mail: every badge blue, quote depths green / cyan / purple, excerpt and body boxes on --bg-primary", () => {
  const model = D.mailThreadModel({ tid: 1, messages: [
    { message_id: "c@x", subject: "[PATCH v2 0/2] cover", author: "A", is_cover: true, patch_version: 2, patch_idx: 0, patch_total: 2 },
    { message_id: "p@x", subject: "[PATCH v2 1/2] one", author: "A", is_patch: true, references: "<c@x>" },
    { message_id: "r@x", subject: "Re: one", author: "B", references: "<c@x> <p@x>" },
  ] }, { openMids: { "r@x": true }, fullMids: { "p@x": true } }, new Map([["p@x", "> one\n>> two\n>>> three\ndiff --git a/f b/f\n+add"], ["r@x", "excerpt line"]]));
  const n = M.createNode("mail", { id: "m", x: 0, y: 0, w: 500, h: 600, data: { tid: 1 } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model });
  for (const b of ["cover", "PATCH", "v2", "0/2"]) assert.match(svg, new RegExp(`fill="\\[accent-blue\\]" fill-opacity="0\\.16"/><text[^>]*fill="\\[accent-blue\\]"[^>]*>${b}<`), b);
  assert.doesNotMatch(svg, /fill="\[accent-orange\]"|fill="\[accent-green\]"[^>]*>PATCH/, "no invented badge colors");
  assert.match(svg, /fill="\[accent-green\]"[^>]*>&gt; one</);
  assert.match(svg, /fill="\[accent-cyan\]"[^>]*>&gt;&gt; two</);
  assert.match(svg, /fill="\[accent-purple\]"[^>]*>&gt;&gt;&gt; three</);
  assert.match(svg, /font-weight="700"[^>]*>diff --git a\/f b\/f</, ".nm-ml-file is bold");
  assert.match(svg, /fill="\[bg-primary\]"\/><rect[^>]*width="2"[^>]*fill="\[accent-blue\]"\/>/, "a full body: --bg-primary box with a blue left border");
  assert.match(svg, /fill="\[bg-primary\]"\/><rect[^>]*width="2"[^>]*fill="\[border-color\]"\/>/, "an excerpt: --border-color left border");
  assert.match(svg, /fill="\[text-secondary\]"[^>]*>excerpt line</);
  assert.match(svg, />⤢ Show full message</, "the message actions are drawn");
  const anchors = (field) => {
    const other = M.createNode("note", { id: "o", x: 900, y: 0 });
    const s = canvasToSvg({ ...M.emptyCanvas(), nodes: [n, other], edges: [M.createEdge({ from: "m", to: "o", fromField: field, fromSide: "right", toSide: "left" })] }, named, { content: () => model });
    return Number(/<path d="M\s*500[ ,]([\d.]+)/.exec(s)[1]);
  };
  assert.equal(anchors("mail:p@x") - anchors("mail:c@x"), EXPORT_METRICS.MAIL_ROW_H + EXPORT_METRICS.MAIL_META_H, "a closed message: its line and its author line");
});

test("commit: green trailer keys, secondary values, M orange / A green / D red / R C blue, message box, no caps", () => {
  const body = Array.from({ length: 60 }, (_, i) => `line ${i}`).join("\n");
  const model = D.commitModel({
    commit_hash: "abcdef1", subject: "s", message: `s\n\n${body}\n\nFixes: 0123456789ab ("x")\nSigned-off-by: A <a@x>`,
    files: ["M", "A", "D", "R", "C"].map((st, i) => ({ change_type: st, file: `f${i}.c` })),
  });
  const n = M.createNode("commit", { id: "c", x: 0, y: 0, w: 400, h: 3000, data: { commitHash: "abcdef1" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model });
  assert.match(svg, /fill="\[accent-green\]"[^>]*>Fixes:<\/text><text[^>]*fill="\[text-secondary\]"[^>]*>0123456789ab/);
  for (const [st, col] of [["M", "accent-orange"], ["A", "accent-green"], ["D", "accent-red"], ["R", "accent-blue"], ["C", "accent-blue"]]) {
    assert.match(svg, new RegExp(`fill="\\[${col}\\]"[^>]*font-weight="700"[^>]*>${st}</text>`), st);
  }
  assert.match(svg, /fill="\[text-secondary\]"[^>]*>f0\.c</, "paths in --text-secondary");
  assert.match(svg, /rx="4" fill="\[bg-primary\]"\/>/, ".nm-commit-msg box");
  assert.match(svg, />line 59</, "the whole body, no 40-line cap");
});

test("history: blame author cyan, line counts muted; release rows color only the status pill", () => {
  const n = M.createNode("history", { id: "h", x: 0, y: 0, w: 420, h: 300, data: { mode: "blame", path: "a.c" } });
  const blame = { mode: "blame", path: "a.c", totalLines: 4, commits: [{ commitHash: sha(1), author: "Linus", subject: "init", lineStart: 1, lineEnd: 4, count: 4 }] };
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => blame });
  assert.match(svg, /fill="\[accent-cyan\]"[^>]*>Linus</);
  assert.match(svg, /fill="\[text-muted\]"[^>]*>L1–4 \(4\)</);
  assert.match(svg, /fill="\[text-secondary\]"[^>]*>init</);
  assert.match(svg, /font-weight="600"[^>]*>11111111</);
  const rel = { mode: "release", name: "x", changeCount: 1, totalVersions: 2, rows: [{ vname: "v3.0", status: "modified", added: 2, removed: 1 }, { vname: "v3.1", status: "removed", added: 0, removed: 0 }] };
  const r = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...n, data: { name: "x" } }] }, named, { content: () => rel });
  assert.match(r, /fill="\[text-primary\]"[^>]*font-weight="600"[^>]*>v3\.0</, "the version is not status-colored");
  assert.match(r, /stroke="\[accent-orange\]"\/><text[^>]*fill="\[accent-orange\]"[^>]*>modified</);
  assert.match(r, /stroke="\[accent-red\]"\/><text[^>]*fill="\[accent-red\]"[^>]*>removed</);
  assert.match(r, /fill="\[text-muted\]"[^>]*>\+2 −1</);
});

test("kconfig: chips of symbols already on the canvas are .nm-kc-chip.on; undefined symbols say so", () => {
  const model = D.kconfigModel({ name: "SMP", type: "bool", depends_on: ["X86", "PCI"], defined_for_arch: false }, null, "SMP");
  const n = M.createNode("kconfig", { id: "k", x: 0, y: 0, w: 400, h: 300, data: { symbol: "SMP" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model, onCanvas: () => new Set(["X86"]) });
  assert.match(svg, /fill="\[accent-blue\]" fill-opacity="0\.14" stroke="\[accent-blue\]"[^>]*\/><text[^>]*>X86</);
  assert.match(svg, /fill="none" stroke="\[border-color\]"[^>]*\/><text[^>]*>PCI</);
  assert.match(svg, />Not defined for this ARCH</);
});

test("cve: severity / state / KEV pills, version summary, tagged fix chain, every file, description", () => {
  const model = D.cveModel({
    id: "CVE-2024-1", title: "uaf", description: "d".repeat(700), cvss: { score: 7.8, severity: "HIGH" }, kev: "yes",
    release_state: { state: "affected", release: "v3.0" }, version_summary: { text: "Affected v2.6 – v6.1" },
    files: Array.from({ length: 12 }, (_, i) => `f${i}.c`), ranges: [{ inferred_introduced: [{ sha: sha(1), subject: "intro subj" }], fixed: sha(2), fixed_subject: "fix subj" }],
  });
  const n = M.createNode("cve", { id: "c", x: 0, y: 0, w: 420, h: 2000, data: { cveId: "CVE-2024-1" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model });
  assert.match(svg, /stroke="\[accent-red\]"\/><text[^>]*fill="\[accent-red\]"[^>]*>7\.8 HIGH</, "high severity is red");
  assert.match(svg, />affected \(v3\.0\)</);
  assert.match(svg, /fill="\[accent-red\]"[^>]*>KEV</);
  assert.match(svg, />Affected v2\.6 – v6\.1</);
  assert.match(svg, /stroke="\[accent-orange\]"\/><text[^>]*>intro<\/text><text[^>]*fill="\[text-primary\]"[^>]*>1111111111</, "a tag, then the neutral sha");
  assert.match(svg, /fill="\[text-secondary\]"[^>]*>intro subj</, "the inferred subject in --text-secondary");
  assert.match(svg, /stroke="\[accent-green\]"\/><text[^>]*>fix</);
  assert.match(svg, />f11\.c</, "every file (no 8-file cap)");
  assert.match(svg, />Fix Chain<[\s\S]*>Affected Files</);
  assert.match(svg, /d…<\/text>/, "the description, capped at 600 characters like the live node");
  const med = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => ({ ...model, score: 5, kev: "" }) });
  assert.match(med, /fill="\[accent-orange\]"[^>]*>5 HIGH</);
  assert.doesNotMatch(med, />KEV</);
});

test("kapi: blue kind tag, bold name, secondary subtitle, catalog chips", () => {
  const model = { catalog: "syscalls", arch: "x86_64", total: 2, items: [{ kind: "syscall", name: "open", subtitle: "open a file", symbol: "do_sys_open" }, { kind: "catalog", cat: "ioctls", name: "ioctls", subtitle: "ioctl numbers" }] };
  const n = M.createNode("kapi", { id: "k", x: 0, y: 0, w: 420, h: 200, data: { catalog: "syscalls" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model });
  assert.match(svg, /fill="\[accent-blue\]"[^>]*font-size="9"[^>]*>syscall</);
  assert.match(svg, /fill="\[text-primary\]"[^>]*font-weight="600"[^>]*>open</);
  assert.match(svg, /fill="\[text-secondary\]"[^>]*>open a file</);
  assert.match(svg, />x86_64</);
  assert.match(svg, /fill="\[accent-blue\]"[^>]*>ioctls</, "a catalog row is a blue chip");
  assert.match(svg, />⊕ fn</);
});

// ------------------------------------------------------------------ struct

const STRUCT = {
  name: "n", kind: "struct", outer_conds: [], file_path: "n.h", line_s: 1,
  items: [
    { kind: "field", name: "a", type: "struct foo", line: 2, conds: [] },
    { kind: "field", name: "b", type: "long", line: 3, conds: ["defined(CONFIG_B)"] },
    { kind: "directive", directive: "ifdef", expr: "CONFIG_X", text: "#ifdef CONFIG_X", line: 4, depth: 0 },
    { kind: "field", name: "x", type: "int", line: 5, conds: [] },
    { kind: "directive", directive: "endif", expr: "", text: "#endif", line: 6, depth: 0 },
  ],
};

test("struct: version-diff banner and row tints, guarded border, ⇶ Types chip, hidden rows struck through", () => {
  const vd = { hasChanges: true, summaryText: "+1 · ~1 type", addedFields: ["a"], removedFields: ["gone"], changedTypes: [{ name: "b", from: "int", to: "long" }], changedGuards: [], fieldStatus: new Map([["a", "added"], ["b", "type"]]) };
  const n = M.createNode("struct", { id: "s", x: 0, y: 0, w: 420, h: 400, data: { name: "n", style: "list", showGuards: false } });
  const so = { editMode: true, versionDiff: vd, versionDiffRange: ["v3.0", "v3.1"] };
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { editMode: true, content: () => STRUCT, structOpts: () => so });
  assert.match(svg, /font-weight="600"[^>]*>v3\.0 → v3\.1: \+1 · ~1 type</);
  assert.match(svg, />\+a<[\s\S]*>-gone<[\s\S]*>~b</, "diff chips");
  assert.match(svg, /fill="color-mix\(in srgb, \[accent-purple\] 10%, \[bg-tertiary\]\)"/, "banner background");
  assert.match(svg, /fill="\[diff-add-fg\]" fill-opacity="0\.12"/, "an added field (fieldStatus \"added\")");
  assert.match(svg, /fill="\[accent-orange\]" fill-opacity="0\.1"/, "a changed type");
  assert.match(svg, /width="2" height="[\d.]+" fill="\[syn-preproc\]" fill-opacity="0\.55"/, ".nm-guarded left border");
  assert.match(svg, />⇶ Types 1</, "the expand-types chip");
  assert.match(svg, /stroke="\[accent-blue\]"|stroke="\[border-color\]"/);
  const unchanged = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => STRUCT, structOpts: () => ({ versionDiff: { hasChanges: false, fieldStatus: new Map() }, versionDiffRange: ["v3.0", "v3.1"] }) });
  assert.match(unchanged, /fill="\[accent-green\]"[^>]*>v3\.0 → v3\.1: unchanged</);
  const css = CSS("nodemap.css");
  assert.match(css, /\.nm-sf-diff-added \{|\.nm-sf-diff-added\s*\{|\.nm-sf-diff-add,\s*\n\.nm-sf-diff-added/, "the live row class of an added field is styled");
});

test("struct: compiled-out branches strike through; folded summaries carry the tint and member pill", () => {
  const n = M.createNode("struct", { id: "s", x: 0, y: 0, w: 420, h: 400, data: { name: "n", style: "list" } });
  const hiddenRows = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...n, data: { ...n.data, hiddenItems: { "2:a": true } } }] }, named, { editMode: true, content: () => STRUCT, structOpts: () => ({ editMode: true }) });
  assert.match(hiddenRows, /<g opacity="0\.45">[\s\S]*?text-decoration="line-through"[^>]*>(<tspan[^>]*>[^<]*<\/tspan>)*<tspan fill="\[text-primary\]">a<\/tspan>/, "a hidden member in Edit Mode: dimmed and struck through");
});

test("struct layout (pahole): est. badge, ⇄CL tag and crossing tint, content-sized columns, measured rows", () => {
  const pahole = { size: 72, alignment: 8, cachelines: 2, padding: 4, holes: 1, is_estimate: true, members: [
    { name: "a", type: "long", offset: 0, size: 8 }, { name: "big", type: "char", offset: 60, size: 8, crosses_cacheline: true },
  ] };
  const n = M.createNode("struct", { id: "s", x: 0, y: 0, w: 420, h: 300, data: { name: "n", style: "layout" } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => STRUCT, structOpts: () => ({ pahole }) });
  assert.match(svg, /font-style="italic"[^>]*>est\.</);
  assert.match(svg, /fill="\[accent-red\]"[^>]*>⇄CL</);
  assert.match(svg, /fill="\[accent-red\]" fill-opacity="0\.07"/);
  assert.match(svg, /fill="\[accent-blue\]" stroke-opacity="0\.45"|stroke="\[accent-blue\]" stroke-opacity="0\.45"/, "dashed 45% cache-line rule");
  assert.match(svg, /cache lines</);
  const unavailable = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => STRUCT, structOpts: () => ({ pahole: { unavailable: true } }) });
  assert.match(unavailable, /Memory layout \(pahole\) unavailable/);
});

// ------------------------------------------------------------------ note, source, scroll, fonts, measure

test("notes: Markdown headings, emphasis, code, quotes, tasks and fences; plain text when Markdown is off", () => {
  const text = "# Big\n## Mid\nA **bold** *it* ~~del~~ `code` word\n> quoted\n- [x] done\n- [ ] open\n1. one\n```\nint x;\n```";
  const n = M.createNode("note", { id: "n", x: 0, y: 0, w: 400, h: 400, data: { text } });
  const svg = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named);
  assert.match(svg, /font-size="14" font-weight="700"[^>]*>Big</);
  assert.match(svg, /font-size="13" font-weight="700"[^>]*>Mid</);
  assert.match(svg, /<tspan font-weight="700">bold<\/tspan>/);
  assert.match(svg, /<tspan font-style="italic">it<\/tspan>/);
  assert.match(svg, /<tspan text-decoration="line-through">del<\/tspan>/);
  assert.match(svg, /rx="3" fill="\[bg-tertiary\]"\/>[\s\S]*<tspan x="[\d.]+" font-family="monospace">code<\/tspan>/, "inline code: mono on --bg-tertiary");
  assert.match(svg, /width="2" height="16" fill="\[border-color\]"\/><text[^>]*fill="\[text-secondary\]"[^>]*>quoted</, "blockquote border and color");
  assert.match(svg, /fill="\[text-muted\]"[^>]*text-decoration="line-through"[^>]*>☑ done</, "a done task: muted and struck through");
  assert.match(svg, />1\.<\/text>/);
  assert.match(svg, /font-family="monospace"[^>]*>int x;</, "fenced code");
  const plain = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...n, data: { text: "**raw** `x`", markdown: false } }] }, named);
  assert.match(plain, />\*\*raw\*\* `x`</, "markdown: false keeps the text as typed");
  const empty = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...n, data: { text: "" } }] }, named, { editMode: true });
  assert.match(empty, /font-style="italic"[^>]*>Double-click to write a note</);
});

test("token styles mirror codeview.css (weights 500, italics) for every class the highlighter emits", () => {
  const css = CSS("codeview.css");
  for (const [cls, [color, weight, italic]] of Object.entries(TOKEN_STYLES)) {
    const rule = new RegExp(`\\.${cls} \\{([^}]*)\\}`).exec(css)?.[1] || "";
    assert.ok(rule.includes(color), `${cls} color`);
    assert.equal(/font-weight: 500/.test(rule), weight === 500, `${cls} weight`);
    assert.equal(/font-style: italic/.test(rule), Boolean(italic), `${cls} italic`);
  }
  assert.equal([...css.matchAll(/^\.tok-[\w-]+ \{/gm)].length, Object.keys(TOKEN_STYLES).length, "every tok-* class is mapped");
});

test("scroll offsets: bodies show the live scroll position (clamped) and anchors move with it", () => {
  const lines = Array.from({ length: 100 }, (_, i) => `int v${i};`);
  const src = M.createNode("source", { id: "s", x: 0, y: 0, w: 300, h: 200, data: { path: "a.c" } });
  const payload = { path: "a.c", start_line: 1, end_line: 100, total_lines: 100, lines };
  const at = canvasToSvg({ ...M.emptyCanvas(), nodes: [src] }, named, { content: () => payload, scroll: () => ({ top: 330 }) });
  assert.match(at, /<g transform="translate\(0 -330\)">/, "the body is shifted by its scrollTop");
  assert.match(at, />v20<\/tspan>/, "the rows in view are drawn");
  assert.doesNotMatch(at, />v0<\/tspan>/, "rows above the view are skipped");
  const end = canvasToSvg({ ...M.emptyCanvas(), nodes: [src] }, named, { content: () => payload, scroll: () => ({ top: 99999 }) });
  const max = 100 * EXPORT_METRICS.SRC_ROW_H + 2 - (200 - EXPORT_METRICS.HEAD_H);
  assert.match(end, new RegExp(`translate\\(0 -${Math.round(max * 10) / 10}\\)`), "clamped to the content");
  const fit = canvasToSvg({ ...M.emptyCanvas(), nodes: [{ ...src, autoFit: true, baseW: 300, baseH: 200 }] }, named, { content: () => payload, scroll: () => ({ top: 330 }) });
  assert.doesNotMatch(fit, /translate\(0 -330\)/, "an auto-fitted node never scrolls");
  const hist = M.createNode("history", { id: "h", x: 0, y: 0, w: 300, h: 120, data: { mode: "blame", path: "a.c" } });
  const blame = { mode: "blame", path: "a.c", totalLines: 9, commits: Array.from({ length: 9 }, (_, i) => ({ commitHash: sha(i + 1), author: "x", subject: "s", lineStart: i, lineEnd: i, count: 1 })) };
  const other = M.createNode("note", { id: "o", x: 600, y: 0 });
  const edgeY = (top) => Number(/<path d="M\s*300[ ,]([\d.]+)/.exec(canvasToSvg({ ...M.emptyCanvas(), nodes: [hist, other], edges: [M.createEdge({ from: "h", to: "o", fromField: `commit:${sha(3)}`, fromSide: "right", toSide: "left" })] }, named, { content: () => blame, scroll: () => ({ top }) }))[1]);
  assert.equal(edgeY(0) - edgeY(20), 20, "a row anchor follows the scrolled row");
});

test("fonts and measured text: the live font stacks and widths drive families, wraps and pill sizes", () => {
  const n = M.createNode("cve", { id: "c", x: 0, y: 0, w: 300, h: 400, data: { cveId: "CVE-1" } });
  const model = D.cveModel({ id: "CVE-1", title: "one two three four five six seven eight nine ten" });
  const est = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model, fonts: { sans: "Inter", mono: "Fira Code" } });
  assert.match(est, /font-family="Inter"/);
  assert.match(est, /font-family="Fira Code"/);
  const wide = canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model, measure: (t) => t.length * 40 });
  const titleLines = (svg) => [...svg.matchAll(/font-weight="600"[^>]*>([a-z ]+)<\/text>/g)].length;
  assert.ok(titleLines(wide) > titleLines(est), "a wider measured font wraps into more lines");
  assert.doesNotMatch(canvasToSvg({ ...M.emptyCanvas(), nodes: [n] }, named, { content: () => model, measure: () => { throw new Error("x"); } }), /NaN/, "a failing measurer falls back to the estimate");
});

test("captureNodeForeignObject: nothing laid out (culled / LOD-hidden) keeps the vector fallback", () => {
  const body = { offsetWidth: 0, offsetHeight: 0, querySelector: () => root };
  const root = { offsetWidth: 0, offsetHeight: 0, nodeType: 1, localName: "div", childNodes: [], ownerDocument: { body: {} }, cloneNode: () => ({ attributes: [], setAttribute() {}, getAttribute: () => "", appendChild() {} }) };
  globalThis.XMLSerializer ??= class { serializeToString() { return "<div/>"; } };
  const el = { querySelector: () => body, ownerDocument: { defaultView: { getComputedStyle: () => ({ getPropertyValue: () => "" }) } } };
  assert.equal(captureNodeForeignObject({ type: "man" }, el), null);
});
