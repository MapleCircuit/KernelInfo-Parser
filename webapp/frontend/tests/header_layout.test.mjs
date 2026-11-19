/**
 * header_layout.test.mjs - Measurement-driven header collapse tiers and the reduced version selector
 * popover (webapp/frontend/js/components/header_layout.js, webapp/API_FRONTEND.md §5.13).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeEventTarget } from "./helpers/dom_shim.mjs";

const {
  HeaderLayout, MAX_HEADER_TIER, REDUCED_VERSION_TIER, clampPopoverPosition, formatVersionSummary,
  selectHeaderTier, tierClasses,
} = await import("../js/components/header_layout.js");

// ---------------------------------------------------------------- fake DOM

class El extends FakeEventTarget {
  constructor(cls = "", { id = "", rect = null } = {}) {
    super();
    this.id = id;
    this.parent = null;
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.attrs = {};
    this.textContent = "";
    this.title = "";
    this.focused = false;
    this._rect = rect || { top: 10, left: 100, bottom: 34, right: 300, width: 200, height: 24 };
    const set = new Set(cls.split(/\s+/).filter(Boolean));
    this.classOps = 0;
    this.classList = {
      add: (c) => { set.add(c); this.classOps += 1; },
      remove: (c) => { set.delete(c); this.classOps += 1; },
      contains: (c) => set.has(c),
      toggle: (c, on) => ((on ?? !set.has(c)) ? set.add(c) : set.delete(c)),
      values: () => [...set],
    };
  }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } return this; }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  closest(sel) {
    const cls = sel.replace(/^\./, "");
    for (let n = this; n; n = n.parent) if (n.classList.contains(cls)) return n;
    return null;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  getBoundingClientRect() { return this._rect; }
  focus() { this.focused = true; }
}

function fakeDoc(root) {
  const doc = new FakeEventTarget();
  doc.popups = [];
  doc.querySelector = (sel) => (sel === ".ref-picker" ? doc.popups[0] || null : null);
  doc.getElementById = (id) => {
    const walk = (n) => { if (n.id === id) return n; for (const c of n.children) { const f = walk(c); if (f) return f; } return null; };
    return walk(root);
  };
  return doc;
}

function tierOf(header) {
  let t = 0;
  while (t < MAX_HEADER_TIER && header.classList.contains(`hdr-collapse-${t + 1}`)) t += 1;
  return t;
}

/**
 * Header content widths (CSS px at 100% UI Scale) for each tier: fonts scale by `font`, chrome by
 * `layout` (§5.5: layout = 1 + (font - 1) / 2). `wide` adds long project/branch/RC values.
 */
function headerModel({ font = 1, wide = false } = {}) {
  const layout = 1 + (font - 1) * 0.5;
  const text = (px) => px * font;
  const chrome = (px) => px * layout;
  return (header) => {
    const t = tierOf(header);
    const gap = chrome(t >= 2 ? 8 : 12);
    const brand = chrome(24) + (t >= 4 ? 0 : chrome(8) + text(110));
    const labels = t >= 1 ? 0 : text(wide ? 300 : 250);
    const controls = text(wide ? 1040 : 640) + chrome(80);
    const version = t >= 3 ? chrome(16) + text(200) : labels + controls;
    const session = t >= 2 ? chrome(40) : chrome(40) + text(wide ? 160 : 110);
    const search = t >= 2 ? chrome(28) : chrome(40) + text(60) + (t >= 1 ? 0 : text(40));
    const badge = t >= 2 ? chrome(28) : chrome(30) + text(45);
    const left = brand + version + session + 2 * gap;
    const right = search + chrome(28) + badge + 2 * chrome(8);
    return 2 * chrome(t >= 2 ? 8 : 12) + left + gap + right;
  };
}

function setup({ width = 1920, font = 1, wide = false, selection = null } = {}) {
  const header = new El("", { id: "app-header" });
  const left = new El("header-left");
  const right = new El("header-right");
  const container = new El("version-selector-container");
  const trigger = new El("version-summary-btn", { id: "version-summary-btn" });
  const summaryLabel = new El("version-summary-label", { id: "version-summary-label" });
  const controls = new El("version-selector-controls", { id: "version-selector-controls", rect: { top: 0, left: 0, bottom: 0, right: 320, width: 320, height: 220 } });
  const branchBtn = new El("branch-picker-btn", { id: "branch-picker-btn" });
  const commitBtn = new El("commit-picker-btn", { id: "commit-picker-btn" });
  const projectSelect = new El("", { id: "project-select" });
  const applyBtn = new El("version-apply-btn", { id: "version-apply-btn" });
  const newTabBtn = new El("version-newtab-btn", { id: "version-newtab-btn" });
  const actions = new El("version-selector-actions").append(applyBtn, newTabBtn);
  trigger.append(summaryLabel);
  controls.append(projectSelect, branchBtn, commitBtn, actions);
  container.append(trigger, controls);
  left.append(container);
  header.append(left, right);
  const body = new El("body").append(header);
  const doc = fakeDoc(body);
  const win = new FakeEventTarget();
  win.innerWidth = width;
  win.innerHeight = 1080;
  const frames = [];
  const env = { width, font, wide, measure: headerModel({ font, wide }) };
  const layout = new HeaderLayout().attach(
    { header, left, right, container, controls, trigger, summaryLabel, applyBtn, newTabBtn },
    {
      doc, win, observe: false,
      raf: (fn) => { frames.push(fn); return frames.length; },
      available: () => env.width,
      measure: (h) => env.measure(h),
      readSelection: () => selection || { project: "Linux Kernel", branch: "— any branch —", tag: "v6.1", commit: "830b3c6", detached: false },
    },
  );
  const resize = (w, f = env.font, wd = env.wide) => {
    Object.assign(env, { width: w, font: f, wide: wd, measure: headerModel({ font: f, wide: wd }) });
    win.innerWidth = w;
    return layout.update({ force: true });
  };
  const flush = () => { while (frames.length) frames.shift()(); };
  return { layout, header, container, controls, trigger, summaryLabel, branchBtn, commitBtn, applyBtn, newTabBtn, doc, win, frames, resize, flush, body };
}

// ---------------------------------------------------------------- tier selection

test("selectHeaderTier picks the first fitting tier, the last when nothing fits", () => {
  const widths = [1500, 1300, 1100, 800, 700];
  assert.equal(selectHeaderTier({ available: 1600, measure: (t) => widths[t] }), 0);
  assert.equal(selectHeaderTier({ available: 1200, measure: (t) => widths[t] }), 2);
  assert.equal(selectHeaderTier({ available: 1104, measure: (t) => widths[t], margin: 8 }), 3);
  assert.equal(selectHeaderTier({ available: 500, measure: (t) => widths[t] }), MAX_HEADER_TIER);
  assert.equal(selectHeaderTier({ available: 0, measure: () => 0 }), MAX_HEADER_TIER);
  assert.deepEqual(tierClasses(0), []);
  assert.deepEqual(tierClasses(3), ["hdr-collapse-1", "hdr-collapse-2", "hdr-collapse-3"]);
});

test("expanding needs extra slack (hysteresis), collapsing does not", () => {
  const widths = [1500, 1300, 1100, 800, 700];
  const measure = (t) => widths[t];
  // 1310px fits tier 1 exactly enough when arriving fresh or collapsing from tier 0...
  assert.equal(selectHeaderTier({ available: 1310, measure, current: 0, expandMargin: 16 }), 1);
  // ...but on that boundary an expansion from tier 2 keeps tier 2 until 16px more are free
  assert.equal(selectHeaderTier({ available: 1310, measure, current: 2, expandMargin: 16 }), 2);
  assert.equal(selectHeaderTier({ available: 1316, measure, current: 2, expandMargin: 16 }), 1);
});

test("1920px at 125% stays put although transitions would report lagging widths", () => {
  // Fake CSS transitions: unless transitions are off (hdr-measuring), a width read reports the tier that
  // was last painted, as an animating padding/font-size does in the browser.
  for (const width of [1700, 1750, 1800, 1850, 1900, 1920]) {
    const h = setup({ width, font: 1.25, wide: true });
    const model = headerModel({ font: 1.25, wide: true });
    let painted = h.layout.tier;
    const reads = [];
    h.layout.env.measure = (hdr) => {
      const measuring = hdr.classList.contains("hdr-measuring");
      reads.push(measuring);
      if (measuring) return model(hdr);
      const probe = new El();
      for (const c of tierClasses(painted)) probe.classList.add(c);
      return model(probe);
    };
    const first = h.layout.update({ force: true });
    for (let i = 0; i < 8; i++) {
      painted = h.layout.tier;
      assert.equal(h.layout.update({ force: true }), first, `width ${width}: tier flipped on update ${i}`);
    }
    assert.ok(reads.length > 0 && reads.every(Boolean), "every width read happens with transitions off");
    assert.ok(!h.header.classList.contains("hdr-measuring"), "transitions are restored after measuring");
  }
});

test("1920x1080 keeps the full header, wide selectors collapse only as far as needed", () => {
  const h = setup({ width: 1920 });
  assert.equal(h.layout.tier, 0);
  assert.equal(h.header.dataset.collapseTier, "0");
  const tier = h.resize(1920, 1, true);
  assert.ok(tier >= 1 && tier < REDUCED_VERSION_TIER, `wide selectors at 1920px: tier ${tier}`);
  // the chosen tier fits and the one before it does not
  assert.ok(headerModel({ wide: true })(h.header) <= 1920);
  assert.equal(tierOf(h.header), tier);
});

test("UI Scale 120%..160% steps down monotonically and reaches the reduced selector", () => {
  const h = setup({ width: 1920 });
  assert.equal(h.resize(1920, 1.2), 0, "120% with short selectors still fits 1920px");
  let prev = h.resize(1920, 1, true);
  for (const font of [1.2, 1.3, 1.4, 1.5, 1.6]) {
    const tier = h.resize(1920, font, true);
    assert.ok(tier >= prev, `font ${font}: tier ${tier} < ${prev}`);
    prev = tier;
  }
  assert.ok(prev >= REDUCED_VERSION_TIER, `160% at 1920px should reduce the version selector (tier ${prev})`);
  assert.ok(h.layout.isReduced());
});

test("narrow viewports reach every tier and expand back cleanly", () => {
  const h = setup({ width: 1920 });
  const seen = new Set();
  for (let w = 1920; w >= 500; w -= 20) seen.add(h.resize(w));
  for (let t = 0; t <= MAX_HEADER_TIER; t++) assert.ok(seen.has(t), `tier ${t} never chosen`);
  assert.equal(h.layout.tier, MAX_HEADER_TIER);
  assert.equal(h.resize(1920), 0);
  assert.deepEqual(h.header.classList.values(), []);
  assert.equal(h.header.dataset.collapseTier, "0");
});

test("repeated evaluations at the same size never oscillate", () => {
  for (const [w, font] of [[1920, 1], [1400, 1], [1180, 1.2], [1920, 1.6], [900, 1]]) {
    const h = setup({ width: w, font });
    const first = h.layout.tier;
    const classes = h.header.classList.values().join(" ");
    for (let i = 0; i < 5; i++) {
      assert.equal(h.layout.update({ force: true }), first);
      assert.equal(h.header.classList.values().join(" "), classes);
    }
  }
});

test("schedule coalesces into one animation frame", () => {
  const h = setup({ width: 1920 });
  h.frames.length = 0;
  for (let i = 0; i < 5; i++) h.layout.schedule();
  h.layout.schedule({ force: true });
  assert.equal(h.frames.length, 1);
  h.flush();
  assert.equal(h.frames.length, 0);
});

// ---------------------------------------------------------------- summary

test("formatVersionSummary covers project/tag/release/commit combinations", () => {
  assert.equal(formatVersionSummary({ project: "Linux Kernel", tag: "v6.1", commit: "830b3c6" }).text, "Linux Kernel / v6.1");
  assert.equal(formatVersionSummary({ project: "Linux Kernel", tag: "v6.1", release: "v6.1.5" }).text, "Linux Kernel / v6.1.5");
  assert.equal(formatVersionSummary({ project: "Linux Kernel", tag: "v6.1", release: "v6.1" }).text, "Linux Kernel / v6.1");
  assert.equal(formatVersionSummary({ project: "Linux Kernel", tag: "— none —", commit: "deadbee", detached: true }).text, "Linux Kernel / deadbee");
  assert.equal(formatVersionSummary({ tag: "v3.0" }).text, "v3.0");
  assert.equal(formatVersionSummary({ commit: "abc1234", detached: true }).text, "abc1234");
  assert.equal(formatVersionSummary({}).text, "Select revision");
  const { title } = formatVersionSummary({ project: "Linux Kernel", branch: "master", tag: "v6.1", release: "v6.1-rc3", commit: "830b3c6" });
  assert.match(title, /^Project: Linux Kernel$/m);
  assert.match(title, /^Branch: master$/m);
  assert.match(title, /^Tag: v6\.1$/m);
  assert.match(title, /^Release: v6\.1-rc3$/m);
  assert.match(title, /^Commit: 830b3c6$/m);
  assert.match(formatVersionSummary({ commit: "deadbee", detached: true }).title, /^Commit: deadbee \(detached\)$/m);
});

test("the trigger label and tooltip follow the selection", () => {
  const selection = { project: "Linux Kernel", branch: "master", tag: "v6.1", commit: "830b3c6", detached: false };
  const h = setup({ width: 900, selection });
  assert.equal(h.summaryLabel.textContent, "Linux Kernel / v6.1");
  assert.match(h.trigger.title, /Branch: master/);
  Object.assign(selection, { tag: "— none —", commit: "deadbee", detached: true });
  h.layout.schedule();
  h.flush();
  assert.equal(h.summaryLabel.textContent, "Linux Kernel / deadbee");
});

// ---------------------------------------------------------------- popover

function reduced() {
  const h = setup({ width: 900 });
  assert.ok(h.layout.isReduced(), `900px should reduce the selector (tier ${h.layout.tier})`);
  return h;
}

test("trigger toggles the anchored popover", () => {
  const h = reduced();
  h.trigger.dispatchEvent({ type: "click", target: h.trigger, stopPropagation() {} });
  assert.ok(h.layout.open);
  assert.ok(h.container.classList.contains("popover-open"));
  assert.equal(h.trigger.attrs["aria-expanded"], "true");
  assert.equal(h.controls.style.top, "40px");
  assert.equal(h.controls.style.left, "100px");
  h.trigger.dispatchEvent({ type: "click", target: h.trigger, stopPropagation() {} });
  assert.ok(!h.layout.open);
  assert.ok(!h.container.classList.contains("popover-open"));
  assert.equal(h.controls.style.top, "");
});

test("popover does not open in full mode", () => {
  const h = setup({ width: 1920 });
  assert.equal(h.layout.openPopover(), false);
  h.container.dispatchEvent({ type: "click", target: h.branchBtn });
  assert.ok(!h.layout.open);
});

test("Escape closes the popover unless a child picker is open", () => {
  const h = reduced();
  h.layout.openPopover();
  h.doc.popups.push(new El("ref-picker branch-picker"));
  h.doc.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.ok(h.layout.open, "Escape belongs to the open branch picker first");
  h.doc.popups.length = 0;
  h.doc.dispatchEvent({ type: "keydown", key: "Enter" });
  assert.ok(h.layout.open);
  h.doc.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.ok(!h.layout.open);
  assert.ok(h.trigger.focused);
});

test("outside clicks close, clicks in the popover or its child pickers do not", () => {
  const h = reduced();
  h.layout.openPopover();
  h.doc.dispatchEvent({ type: "mousedown", target: h.branchBtn });
  assert.ok(h.layout.open, "inside the popover");
  const picker = new El("ref-picker");
  const item = new El("ref-picker-item");
  picker.append(item);
  h.body.append(picker);
  h.doc.dispatchEvent({ type: "mousedown", target: item });
  assert.ok(h.layout.open, "inside a refPicker/branchPicker opened from the popover");
  const elsewhere = new El("workspace");
  h.body.append(elsewhere);
  h.doc.dispatchEvent({ type: "mousedown", target: elsewhere });
  assert.ok(!h.layout.open);
});

test("Apply and New Tab close the popover", () => {
  for (const key of ["applyBtn", "newTabBtn"]) {
    const h = reduced();
    h.layout.openPopover();
    h[key].dispatchEvent({ type: "click", target: h[key] });
    assert.ok(!h.layout.open, key);
  }
});

test("a programmatic click on a hidden picker button opens the popover first", () => {
  for (const key of ["branchBtn", "commitBtn"]) {
    const h = reduced();
    h.container.dispatchEvent({ type: "click", target: h[key] });
    assert.ok(h.layout.open, key);
    // a second click while open leaves it open (the picker toggles itself)
    h.container.dispatchEvent({ type: "click", target: h[key] });
    assert.ok(h.layout.open, key);
  }
});

test("expanding out of the reduced tier closes the popover", () => {
  const h = reduced();
  h.layout.openPopover();
  // content changes while open do not re-measure (a focused native <select> would lose its dropdown)
  h.layout.update();
  assert.ok(h.layout.open);
  h.resize(1920);
  assert.equal(h.layout.tier, 0);
  assert.ok(!h.layout.open);
  assert.ok(!h.container.classList.contains("popover-open"));
});

test("clampPopoverPosition keeps the popover inside the viewport", () => {
  const vp = { width: 800, height: 600 };
  assert.deepEqual(clampPopoverPosition({ left: 100, top: 10, bottom: 34 }, { width: 300, height: 200 }, vp),
    { left: 100, top: 40, maxWidth: 784, maxHeight: 552 });
  assert.equal(clampPopoverPosition({ left: 700, top: 10, bottom: 34 }, { width: 300, height: 200 }, vp).left, 492);
  assert.equal(clampPopoverPosition({ left: -50, top: 10, bottom: 34 }, { width: 300, height: 200 }, vp).left, 8);
  const wide = clampPopoverPosition({ left: 100, top: 10, bottom: 34 }, { width: 2000, height: 200 }, vp);
  assert.equal(wide.left, 8);
  assert.equal(wide.maxWidth, 784);
  // no room below: opens above the anchor
  assert.equal(clampPopoverPosition({ left: 10, top: 500, bottom: 524 }, { width: 100, height: 200 }, vp).top, 294);
});
