/**
 * scope_model.test.mjs - Local-scope resolution in the code view (§5.6, §5.21): token positions, the scope
 * hover card, usage ranges, XRef access badges, and hover eligibility of one-character C names.
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  formatCounts, formatScopeCard, isScopeFile, occurrenceRanges, scopeActions, scopeDeclTarget, scopeOf,
  scopeTypeTarget, scopeXrefTitle, textOffsetOf, tokenColumn, tokenPosition, xrefBadge,
} from "../js/views/code_view/scope_model.js";
import { hoverable, hoverCacheKey } from "../js/views/code_view/hover_model.js";
import { buildByteToCharMap, byteColToCharIdx, charIdxToByteCol } from "../js/utils/text_coordinates.js";

// drivers/pci/syscall.c (v3.0), SYSCALL_DEFINE5(pciconfig_write, ...)
const DEV = {
  found: true, kind: "local", name: "dev", file: "drivers/pci/syscall.c",
  type: "struct pci_dev *", type_ref: { kind: "struct", name: "pci_dev", pointer: 1, array: false },
  type_def: { file: "include/linux/pci.h", line: 227, kind: "struct", name: "pci_dev" },
  decl: { line: 89, col: 18 }, init: null, function: { name: "sys_pciconfig_write", line_s: 86, line_e: 136 },
  access: "r", uses: [{ line: 89, col: 18, access: "decl" }, { line: 98, col: 2, access: "w" }, { line: 99, col: 7, access: "r" }],
  counts: { decl: 1, r: 5, w: 1, rw: 0, addr: 0 },
  inferred: [{ callee: "pci_get_bus_and_slot", line: 98, return_type: "struct pci_dev *", agrees: true, file: "drivers/pci/search.c", def_line: 154 }],
  effective_type: "struct pci_dev *", last_assign: { line: 98, col: 2 }, member: null,
};

test("isScopeFile: only C sources and headers resolve locally", () => {
  assert.ok(isScopeFile("drivers/pci/syscall.c"));
  assert.ok(isScopeFile("include/linux/pci.h"));
  assert.ok(!isScopeFile("arch/x86/kernel/entry_64.S"));
  assert.ok(!isScopeFile("drivers/pci/Kconfig"));
  assert.ok(!isScopeFile("rust/kernel/lib.rs"));
  assert.ok(!isScopeFile(""));
});

test("charIdxToByteCol inverts byteColToCharIdx on multi-byte lines", () => {
  const line = "\t/* é → 😀 */ x = y;";
  const map = buildByteToCharMap(line);
  for (const ch of ["x", "y", "→", "😀"]) {
    const idx = line.indexOf(ch);
    const col = charIdxToByteCol(line, idx);
    assert.equal(byteColToCharIdx(map, col), idx, ch);
  }
  assert.equal(charIdxToByteCol("abc", 0), 1);
  assert.equal(charIdxToByteCol("abc", 99), 4, "clamped past the end");
  assert.equal(charIdxToByteCol("é", 1), 3);
});

test("tokenColumn: the name at the token start, inside a wider token, after multi-byte text", () => {
  const line = "\tdev = pci_get_bus_and_slot(bus, dfn);";
  assert.equal(tokenColumn(line, 1, "dev"), 2);
  assert.equal(tokenColumn(line, 7, "bus"), line.indexOf("bus,") + 1, "first whole word from the span start");
  assert.equal(tokenColumn("/* ü */ i++;", 8, "i"), 10, "byte column counts ü as 2 bytes");
  assert.equal(tokenColumn("x", 0, ""), 1);
});

/** Minimal DOM nodes: text nodes and elements with childNodes, closest(), dataset. */
function el(tag, cls, children = [], dataset = {}) {
  const node = { nodeType: 1, tag, cls, childNodes: children, dataset, parent: null };
  node.closest = (sel) => {
    for (let n = node; n; n = n.parent) if (n.cls && sel === `.${n.cls}`) return n;
    return null;
  };
  Object.defineProperty(node, "textContent", { get: () => children.map((c) => c.textContent).join("") });
  for (const c of children) c.parent = node;
  return node;
}
const txt = (v) => ({ nodeType: 3, nodeValue: v, textContent: v });

test("textOffsetOf / tokenPosition: line and byte column of a rendered token", () => {
  const span = el("span", "ast-token", [txt("byte")], { name: "byte" });
  const mark = el("mark", "find-match", [txt("err")]);
  const cell = el("div", "code-text", [txt("\t\t"), mark, txt(" = put_user(é, "), span, txt(");")]);
  const row = el("div", "code-row", [el("div", "gutter-line-no", [txt("110")]), cell], { lineNo: "110" });
  assert.equal(textOffsetOf(cell, span), "\t\terr = put_user(é, ".length);
  assert.equal(textOffsetOf(cell, txt("other")), -1);
  assert.deepEqual(tokenPosition(span), { line: 110, col: "\t\terr = put_user(é, ".length + 2 });
  assert.equal(row.childNodes.length, 2);
  const loose = el("span", "ast-token", [txt("x")], { name: "x" });
  assert.equal(tokenPosition(loose), null, "a token outside a code row has no position");
});

test("occurrenceRanges: byte columns to characters, several uses on a line, nearest-name fallback", () => {
  const lines = {
    89: "\tstruct pci_dev *dev;",
    98: "\tdev = pci_get_bus_and_slot(bus, dfn);",
    104: "\t\terr = pci_user_write_config_byte(dev, off, byte); /* dev */ dev->x",
    120: "\t/* é */ dev++;",
  };
  const uses = [
    { line: 89, col: 18, access: "decl" },
    { line: 98, col: 2, access: "w" },
    { line: 104, col: 37, access: "r" },
    { line: 104, col: 63, access: "r" },
    { line: 120, col: 11, access: "rw" },
    { line: 120, col: 11, access: "r" },
    { line: 7, col: 1, access: "r" },
    { line: 89, col: 9, access: "bogus" },
    { line: 98, col: 3, access: "r" },
  ];
  const r = occurrenceRanges(uses, (l) => lines[l], "dev");
  assert.deepEqual(r.get(89), [{ start: 8, end: 15, access: "r" }, { start: 17, end: 20, access: "decl" }], "unknown access reads as r");
  assert.deepEqual(r.get(98), [{ start: 1, end: 4, access: "w" }], "a column inside a word snaps to the nearest name, overlaps dropped");
  const l104 = r.get(104);
  assert.equal(l104.length, 2);
  assert.equal(lines[104].slice(l104[0].start, l104[0].end), "dev");
  assert.equal(lines[104].slice(l104[1].start, l104[1].end), "dev");
  assert.deepEqual(r.get(120), [{ start: 9, end: 12, access: "rw" }], "é is two bytes; duplicate start dropped");
  assert.ok(!r.has(7), "a line without text is skipped");
  // A column inside a word falls back to the nearest whole-word `name`
  const fb = occurrenceRanges([{ line: 1, col: 4, access: "addr" }], () => "x = &dev;", "dev");
  assert.deepEqual(fb.get(1), [{ start: 5, end: 8, access: "addr" }]);
});

test("scopeOf: ScopeInfo itself, a payload's scope, or null", () => {
  assert.equal(scopeOf(DEV), DEV);
  assert.equal(scopeOf({ found: true, kind: "function", scope: DEV }), DEV);
  assert.equal(scopeOf({ found: false }), null);
  assert.equal(scopeOf({ found: true, kind: "C_vardecl", signature: "int x" }).kind, "C_vardecl", "a bare found payload with a kind passes through");
  assert.equal(scopeOf(null), null);
  assert.deepEqual(scopeOf({ found: true, kind: "param", name: "bus" }).uses, []);
});

test("scope card: local variable with type link, lines, inferred callee (agrees) and counts", () => {
  const html = formatScopeCard(DEV, { name: "dev", signature: "struct pci_dev *dev" });
  assert.match(html, /hc-kind-scope[^>]*>local variable</);
  assert.match(html, /<pre class="hc-sig">struct pci_dev \*dev<\/pre>/);
  assert.match(html, /data-goto-file="include\/linux\/pci.h" data-goto-line="227"[^>]*><code>struct pci_dev \*<\/code>/);
  assert.match(html, /declared at <a[^>]*data-goto-file="drivers\/pci\/syscall.c" data-goto-line="89"/);
  assert.match(html, /last assigned at <a[^>]*data-goto-line="98"[^>]*>L98</);
  assert.match(html, /data-goto-file="drivers\/pci\/search.c" data-goto-line="154"[^>]*><code>pci_get_bus_and_slot\(\)<\/code><\/a> → <code>struct pci_dev \*<\/code> <span class="hc-agree"/);
  assert.match(html, /5 reads · 1 write/);
  assert.match(html, /in <code>sys_pciconfig_write\(\)<\/code>/);
  assert.deepEqual(scopeDeclTarget(DEV), { file: "drivers/pci/syscall.c", line: 89 });
  assert.deepEqual(scopeTypeTarget(DEV), { file: "include/linux/pci.h", line: 227, name: "pci_dev" });
  const acts = scopeActions(DEV);
  for (const a of ["decl", "typedef", "hl", "refs"]) assert.match(acts, new RegExp(`data-hc="${a}"`));
});

test("scope card: parameter with a builtin type and an initializer on its declaration line", () => {
  const p = { found: true, kind: "param", name: "len", file: "drivers/pci/syscall.c", type: "unsigned long",
    type_ref: { kind: "builtin", name: "unsigned long" }, type_def: null, decl: { line: 87, col: 30 }, init: { line: 87, col: 30 },
    uses: [{ line: 87, col: 30, access: "decl" }], counts: { decl: 1, r: 1 } };
  const html = formatScopeCard(p, { name: "len" });
  assert.match(html, />parameter</);
  assert.match(html, /<pre class="hc-sig">unsigned long len<\/pre>/, "signature built from the type");
  assert.match(html, /type <code>unsigned long<\/code>/, "no link without a type definition");
  assert.match(html, /initialized there/);
  assert.equal(scopeTypeTarget(p), null, "builtins have no type definition");
  assert.doesNotMatch(scopeActions(p), /typedef/);
});

test("scope card: struct field read through a pointer", () => {
  const f = { found: true, kind: "field", name: "bus", file: "drivers/pci/syscall.c", type: "struct pci_bus *",
    type_ref: { kind: "struct", name: "pci_bus", pointer: 1 }, type_def: null, decl: null, access: "r",
    uses: [{ line: 99, col: 12, access: "r" }], counts: { r: 2, w: 0 },
    member: { base: "dev", base_kind: "local", base_type: "struct pci_dev *", base_decl: { line: 89, col: 18 }, path: ["bus"], op: "->",
      struct: { kind: "struct", name: "pci_dev", file: "include/linux/pci.h", line: 227 },
      field_def: { file: "include/linux/pci.h", line: 233, col: 18, type: "struct pci_bus *" } } };
  const html = formatScopeCard(f, { name: "bus", signature: "struct pci_bus *bus" });
  assert.match(html, />struct field</);
  assert.match(html, /field <code>bus<\/code> of <a[^>]*data-goto-line="227"[^>]*><code>struct pci_dev<\/code><\/a>/);
  assert.match(html, /via <code>dev-&gt;bus<\/code>/);
  assert.match(html, /hc-acc-r">read here</);
  assert.match(html, /defined at <a[^>]*data-goto-file="include\/linux\/pci.h" data-goto-line="233"/);
  assert.match(html, /2 reads in this function/);
  assert.doesNotMatch(html, /declared at/);
  assert.deepEqual(scopeDeclTarget(f), { file: "include/linux/pci.h", line: 233 });
  assert.deepEqual(scopeTypeTarget(f), { name: "pci_bus" }, "no type_def: looked up by tag name");
  assert.match(scopeActions(f), /Go to field definition/);
});

test("scope card: unknown type with an inferred return type that disagrees", () => {
  const u = { found: true, kind: "local", name: "p", file: "a.c", type: null, type_ref: null, type_def: null,
    decl: { line: 5, col: 2 }, uses: [], counts: {}, effective_type: "void *",
    inferred: [{ callee: "kmalloc", line: 6, return_type: "void *", agrees: false }] };
  const html = formatScopeCard(u, { name: "p" });
  assert.match(html, /type unknown — inferred <code>void \*<\/code>/);
  assert.match(html, /<code>kmalloc\(\)<\/code> → <code>void \*<\/code> <span class="hc-disagree"/);
  assert.doesNotMatch(html, /<pre class="hc-sig">/, "no signature without a type");
  assert.doesNotMatch(scopeActions(u), /data-hc="hl"/, "nothing to highlight");
  assert.equal(formatCounts({}), "");
  assert.equal(formatCounts({ r: 1, rw: 2, addr: 1 }), "1 read · 2 read-writes · 1 address-of");
});

test("scope card escapes every interpolated string", () => {
  const evil = { ...DEV, name: "<img src=x>", type: "struct <b>x</b> *", file: "a\"onclick=\"x.c",
    type_def: { file: "<i>.h", line: "1\" onload=\"x", kind: "struct", name: "y" },
    function: { name: "<script>" }, inferred: [{ callee: "<svg>", return_type: "<u>", agrees: true, file: "\"x", def_line: 3 }] };
  const html = formatScopeCard(evil, { signature: "<sig>" });
  assert.doesNotMatch(html, /<img|<b>|<script>|<svg>|<u>|<sig>|<i>/);
  assert.doesNotMatch(html, /"onclick=|" onload=/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.match(html, /data-goto-line="1"/, "line numbers are numeric");
  assert.match(scopeXrefTitle(evil, "x"), /Uses of local variable <img src=x> in <script>\(\)/, "title is plain text (escaped by the modal)");
});

test("xrefBadge: access badges for local uses, the reference type otherwise", () => {
  assert.deepEqual(xrefBadge({ type: "Write", access: "w" }), { label: "Write", cls: "xref-acc xref-acc-w" });
  assert.deepEqual(xrefBadge({ access: "addr" }), { label: "AddressOf", cls: "xref-acc xref-acc-addr" });
  assert.deepEqual(xrefBadge({ type: "MemberRef" }), { label: "MemberRef", cls: "" });
  assert.deepEqual(xrefBadge({}), { label: "Call", cls: "" });
  assert.equal(scopeXrefTitle(DEV, "dev"), "Uses of local variable dev in sys_pciconfig_write()");
});

test("hoverable: one-character names only in C files; hover cache key carries the position", () => {
  const tok = (name, cls = []) => ({ dataset: { name }, textContent: name, classList: { contains: (c) => cls.includes(c) } });
  assert.equal(hoverable(tok("i")), false);
  assert.equal(hoverable(tok("i"), { shortNames: true }), true);
  assert.equal(hoverable(tok("dev")), true);
  assert.equal(hoverable(tok("i", ["tok-comment"]), { shortNames: true }), false);
  assert.equal(hoverable(tok("1"), { shortNames: true }), false);
  assert.equal(hoverCacheKey("v3.0", "x86_64", "a.c", "dev"), "v3.0|x86_64|a.c|dev");
  assert.equal(hoverCacheKey("v3.0", "x86_64", "a.c", "dev", {}), "v3.0|x86_64|a.c|dev");
  assert.equal(hoverCacheKey("v3.0", "x86_64", "a.c", "dev", { line: 98, col: 2 }), "v3.0|x86_64|a.c|dev|98:2");
});

test("scope card: agrees null shows no marker; a field without field_def has no location", () => {
  const u = { found: true, kind: "local", name: "q", file: "a.c", type: null, decl: { line: 3, col: 2 }, uses: [],
    effective_type: "struct foo *", inferred: [{ callee: "get_foo", line: 4, return_type: "struct foo *", agrees: null }] };
  const html = formatScopeCard(u);
  assert.match(html, /type unknown — inferred <code>struct foo \*<\/code>/);
  assert.doesNotMatch(html, /hc-agree|hc-disagree/);
  const f = { found: true, kind: "field", name: "x", file: "a.c", type: null, uses: [], access: "w",
    member: { base: "s", op: ".", path: ["x"], struct: null, field_def: null } };
  const fh = formatScopeCard(f);
  assert.match(fh, /hc-acc-w">written here</);
  assert.match(fh, /via <code>s\.x<\/code>/);
  assert.doesNotMatch(fh, /defined at|data-goto/);
  assert.equal(scopeDeclTarget(f), null);
  assert.doesNotMatch(scopeActions(f), /data-hc="decl"/);
});
