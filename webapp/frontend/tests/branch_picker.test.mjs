/**
 * branch_picker.test.mjs - Grouping branches by source, sorting within sources, drill-down
 * navigation, cross-source search, and keyboard controls for the header branch picker.
 */
import { FakeElement } from "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

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
globalThis.__KIP_SKIP_AUTOSTART__ = true;

class RichElement extends FakeElement {
  constructor(tag = "div") {
    super(tag);
    this._innerHTML = "";
    this.className = "";
    this.value = "";
    this.placeholder = "";
    this.title = "";
    this.selectionStart = 0;
    this._parts = new Map();
  }
  set innerHTML(html) {
    this._innerHTML = String(html);
    this._parts?.clear();
  }
  get innerHTML() {
    return this._innerHTML;
  }
  contains(target) {
    return target === this || this._childrenNodes?.includes(target) || false;
  }
  querySelector(sel) {
    if (sel === ".ref-picker-input") return this._ensurePart("input", () => new RichElement("input"));
    if (sel === ".branch-picker-breadcrumb") return this._ensurePart("crumb", () => new RichElement("div"));
    if (sel === ".ref-picker-list") return this._ensurePart("list", () => new RichElement("div"));
    if (sel === ".ref-picker-footer") return this._ensurePart("footer", () => new RichElement("div"));
    if (sel === ".branch-picker-back") {
      if (this._innerHTML.includes("branch-picker-back")) {
        return this._ensurePart("back", () => new RichElement("button"));
      }
      const crumb = this._parts.get("crumb");
      return crumb ? crumb.querySelector(sel) : null;
    }
    if (sel === ".ref-picker-item.highlight") return null;
    return null;
  }
  querySelectorAll(sel) {
    if (sel === ".ref-picker-item") {
      const list = this._parts.get("list");
      if (list) return list.querySelectorAll(sel);
      const html = this._innerHTML || "";
      if (this._items && this._items._html === html) return this._items;
      const items = [];
      items._html = html;
      const re = /data-idx="(\d+)"/g;
      let m;
      while ((m = re.exec(html)) !== null) {
        const el = new RichElement("div");
        el.dataset.idx = m[1];
        items.push(el);
      }
      this._items = items;
      return items;
    }
    return [];
  }
  _ensurePart(key, factory) {
    if (!this._parts.has(key)) this._parts.set(key, factory());
    return this._parts.get(key);
  }
  focus() {}
}

document.createElement = (tag) => new RichElement(tag);

const {
  BranchPicker,
  branchSourceOf,
  formatBranchScopeLabel,
  groupBranchesBySource,
  isSourceScope,
  sourceNameOfScope,
  sourceScopeValue,
} = await import("../js/components/branch_picker.js");

const SAMPLE_BRANCHES = [
  { name: "master", is_default: true, commit_id: 1, commit_hash: "02f8c6aee8df311261e6da8ba96bd41704aa9623" },
  { name: "history/0.10-fix-RCS", is_default: false, commit_id: 0, commit_hash: "1111111111111111111111111111111111111111" },
  { name: "history/2.4-stable", is_default: false, commit_id: 0, commit_hash: "2222222222222222222222222222222222222222" },
  { name: "history/master", is_default: false, commit_id: 0, commit_hash: "3333333333333333333333333333333333333333" },
  { name: "linux-next/fs-next", is_default: false, commit_id: 0, commit_hash: "4444444444444444444444444444444444444444" },
  { name: "linux-next/akpm", is_default: false, commit_id: 0, commit_hash: "5555555555555555555555555555555555555555" },
  { name: "linux-next/master", is_default: false, commit_id: 0, commit_hash: "6666666666666666666666666666666666666666" },
  { name: "origin/rcu-free-v3", is_default: false, commit_id: 0, commit_hash: "7777777777777777777777777777777777777777" },
  { name: "stable/linux-2.6.11.y", is_default: false, commit_id: 0, commit_hash: "8888888888888888888888888888888888888888" },
  { name: "stable/linux-6.6.y", is_default: false, commit_id: 2, commit_hash: "5dba9ddd98cbc7ad319d687887981a0ea0062c75" },
  { name: "stable/linux-6.12.y", is_default: false, commit_id: 0, commit_hash: "9999999999999999999999999999999999999999" },
];

test("branchSourceOf and groupBranchesBySource organize branches cleanly", () => {
  assert.deepEqual(branchSourceOf("master"), { source: "local", shortName: "master" });
  assert.deepEqual(branchSourceOf("stable/linux-6.6.y"), { source: "stable", shortName: "linux-6.6.y" });
  assert.deepEqual(branchSourceOf("origin/user/topic"), { source: "origin", shortName: "user/topic" });

  const grouped = groupBranchesBySource(SAMPLE_BRANCHES);
  assert.deepEqual(grouped.local.map((b) => b.name), ["master"]);
  assert.deepEqual(grouped.sources.map((s) => s.source), ["history", "linux-next", "origin", "stable"]);

  const stable = grouped.sources.find((s) => s.source === "stable").branches.map((b) => b.name);
  assert.deepEqual(stable, ["stable/linux-6.12.y", "stable/linux-6.6.y", "stable/linux-2.6.11.y"]);

  const history = grouped.sources.find((s) => s.source === "history").branches.map((b) => b.name);
  assert.deepEqual(history, ["history/master", "history/2.4-stable", "history/0.10-fix-RCS"]);

  const next = grouped.sources.find((s) => s.source === "linux-next").branches.map((b) => b.name);
  assert.deepEqual(next, ["linux-next/master", "linux-next/akpm", "linux-next/fs-next"]);
});

test("BranchPicker drills into sources, navigates back, searches across sources, and selects", () => {
  const picker = new BranchPicker();
  const anchor = new RichElement("button");
  let selected = null;

  picker.open(anchor, {
    current: "stable/linux-6.6.y",
    branches: SAMPLE_BRANCHES,
    tagViews: [{ name: "— any branch —", regex_rules: [] }],
    onSelect: (val) => { selected = val; },
  });

  // Root shows tag view, master, and the 4 sources, with stable highlighted as active source
  assert.equal(picker.activeSource, null);
  assert.deepEqual(
    picker.items.map((it) => `${it.kind}:${it.source || it.value}`),
    ["view:", "branch:master", "source:history", "source:linux-next", "source:origin", "source:stable"],
  );
  assert.equal(picker.items[picker.highlight].source, "stable");
  assert.equal(picker.items[picker.highlight].isCurrent, true);

  // Pressing Enter on highlighted source "stable" opens source-scope + its branches and highlights stable/linux-6.6.y
  picker.handleKey({ key: "Enter", preventDefault() {} }, picker.popover.querySelector(".ref-picker-input"));
  assert.equal(picker.activeSource, "stable");
  assert.deepEqual(
    picker.items.map((it) => it.value),
    ["source:stable", "stable/linux-6.12.y", "stable/linux-6.6.y", "stable/linux-2.6.11.y"],
  );
  assert.equal(picker.items[picker.highlight].value, "stable/linux-6.6.y");

  // ArrowUp moves to stable/linux-6.12.y; Backspace on empty filter goes back to sources
  const input = picker.popover.querySelector(".ref-picker-input");
  picker.handleKey({ key: "ArrowUp", preventDefault() {} }, input);
  assert.equal(picker.items[picker.highlight].value, "stable/linux-6.12.y");

  picker.handleKey({ key: "Backspace", preventDefault() {} }, input);
  assert.equal(picker.activeSource, null);
  assert.equal(picker.items[picker.highlight].source, "stable");

  // Search from root finds matching branches across all sources directly
  input.value = "2.4";
  input.dispatchEvent({ type: "input" });
  assert.deepEqual(
    picker.items.filter((it) => it.kind === "branch").map((it) => it.value),
    ["history/2.4-stable"],
  );

  // Enter selects the matching branch and closes the popover
  picker.handleKey({ key: "Enter", preventDefault() {} }, input);
  assert.equal(selected, "history/2.4-stable");
  assert.equal(picker.popover, null);
});

test("BranchPicker groups many local branches under a local source", () => {
  const picker = new BranchPicker();
  const anchor = new RichElement("button");
  const manyLocal = [
    { name: "master", is_default: true },
    { name: "feat-a" },
    { name: "feat-b" },
    { name: "feat-c" },
    { name: "feat-d" },
    { name: "stable/linux-6.6.y" },
  ];
  picker.open(anchor, {
    current: "feat-c",
    branches: manyLocal,
    tagViews: [{ name: "— any branch —", regex_rules: [] }],
    onSelect: () => {},
  });

  assert.deepEqual(
    picker.items.map((it) => `${it.kind}:${it.source || it.value}`),
    ["view:", "source:local", "source:stable"],
  );
  picker.enterSource("local");
  assert.deepEqual(picker.items.map((it) => it.value), ["source:local", "master", "feat-a", "feat-b", "feat-c", "feat-d"]);
  picker.close();
});

test("BranchPicker allows selecting an entire branch source from drill-in or root search", () => {
  assert.equal(isSourceScope("source:history"), true);
  assert.equal(isSourceScope("history/2.4-stable"), false);
  assert.equal(sourceScopeValue("history"), "source:history");
  assert.equal(sourceNameOfScope("source:history"), "history");
  assert.equal(formatBranchScopeLabel("source:history"), "history/*");
  assert.equal(formatBranchScopeLabel("master"), "master");

  const picker = new BranchPicker();
  const anchor = new RichElement("button");
  let selected = null;

  picker.open(anchor, {
    current: "source:history",
    branches: SAMPLE_BRANCHES,
    tagViews: [{ name: "— any branch —", regex_rules: [] }],
    onSelect: (val) => { selected = val; },
  });

  // Root highlights the source owning the active source-scope
  const histRootIdx = picker.items.findIndex((it) => it.kind === "source" && it.source === "history");
  assert.equal(picker.highlight, histRootIdx);
  assert.equal(picker.items[histRootIdx].isCurrent, true);
  assert.equal(picker.items[histRootIdx].meta, "all tags · 3 branches");

  // Drilling into history places source:history first and highlights it; Enter selects it
  picker.handleKey({ key: "Enter", preventDefault() {} }, picker.popover.querySelector(".ref-picker-input"));
  assert.equal(picker.activeSource, "history");
  assert.equal(picker.items[0].kind, "source-scope");
  assert.equal(picker.items[0].value, "source:history");
  assert.equal(picker.highlight, 0);

  picker.handleKey({ key: "Enter", preventDefault() {} }, picker.popover.querySelector(".ref-picker-input"));
  assert.equal(selected, "source:history");
  assert.equal(picker.popover, null);

  // Root search for "history" also surfaces source:history for direct selection
  picker.open(anchor, {
    current: "",
    branches: SAMPLE_BRANCHES,
    tagViews: [{ name: "— any branch —", regex_rules: [] }],
    onSelect: (val) => { selected = val; },
  });
  const input = picker.popover.querySelector(".ref-picker-input");
  input.value = "history/*";
  input.dispatchEvent({ type: "input" });
  const directIdx = picker.items.findIndex((it) => it.kind === "source-scope" && it.value === "source:history");
  assert.ok(directIdx >= 0);
  picker.activate(directIdx);
  assert.equal(selected, "source:history");
  assert.equal(picker.popover, null);
});

test("BranchPicker mousedown on source or back button keeps popover open when bubbling to document", async () => {
  const picker = new BranchPicker();
  const anchor = new RichElement("button");
  let selected = null;

  picker.open(anchor, {
    current: "",
    branches: SAMPLE_BRANCHES,
    tagViews: [{ name: "— any branch —", regex_rules: [] }],
    onSelect: (val) => { selected = val; },
  });

  // Let setTimeout(..., 0) attach document's outside mousedown listener
  await new Promise((r) => realSetTimeout(r, 10));

  // Simulate mousedown on the "stable" source item (idx 5) bubbling through popover to document
  const stableIdx = picker.items.findIndex((it) => it.kind === "source" && it.source === "stable");
  const stableEl = picker.popover.querySelectorAll(".ref-picker-item")[stableIdx];
  let stopped = false;
  const evSource = {
    type: "mousedown",
    target: stableEl,
    preventDefault() {},
    stopPropagation() { stopped = true; },
  };
  stableEl.dispatchEvent(evSource);
  if (!stopped) {
    picker.popover?.dispatchEvent(evSource);
    if (!stopped) document.dispatchEvent(evSource);
  }

  assert.equal(stopped, true);
  assert.notEqual(picker.popover, null);
  assert.equal(picker.activeSource, "stable");

  // Simulate mousedown on "← All sources" back button
  const backBtn = picker.popover.querySelector(".branch-picker-back");
  let backStopped = false;
  const evBack = {
    type: "mousedown",
    target: backBtn,
    preventDefault() {},
    stopPropagation() { backStopped = true; },
  };
  backBtn.onmousedown(evBack);
  if (!backStopped) document.dispatchEvent(evBack);

  assert.equal(backStopped, true);
  assert.notEqual(picker.popover, null);
  assert.equal(picker.activeSource, null);

  // Clicking into "history" and then picking "history/2.4-stable" works
  const histIdx = picker.items.findIndex((it) => it.kind === "source" && it.source === "history");
  const histEl = picker.popover.querySelectorAll(".ref-picker-item")[histIdx];
  histEl.dispatchEvent({ type: "mousedown", target: histEl, preventDefault() {}, stopPropagation() {} });
  assert.equal(picker.activeSource, "history");

  const branchIdx = picker.items.findIndex((it) => it.value === "history/2.4-stable");
  const branchEl = picker.popover.querySelectorAll(".ref-picker-item")[branchIdx];
  branchEl.dispatchEvent({ type: "mousedown", target: branchEl, preventDefault() {}, stopPropagation() {} });
  assert.equal(selected, "history/2.4-stable");
  assert.equal(picker.popover, null);

  // Outside mousedown closes an open popover
  picker.open(anchor, {
    current: "",
    branches: SAMPLE_BRANCHES,
    tagViews: [{ name: "— any branch —", regex_rules: [] }],
    onSelect: () => {},
  });
  await new Promise((r) => realSetTimeout(r, 10));
  document.dispatchEvent({ type: "mousedown", target: new RichElement("div") });
  assert.equal(picker.popover, null);
});
