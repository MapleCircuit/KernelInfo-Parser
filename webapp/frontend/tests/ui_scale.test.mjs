/**
 * ui_scale.test.mjs - Unit tests for UI Scale helpers, applyTypographySettings(), VirtualList live
 * scaling on `ui:scale-changed`, and scaled SVG charts (webapp/API_FRONTEND.md §5.5, §5.29).
 */
import { FakeElement, FakeEventTarget } from "./helpers/dom_shim.mjs";
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

// Support CSS custom property read/write on document.documentElement in the Node test shim.
const rootVars = new Map();
document.documentElement.style = {
  setProperty(k, v) { rootVars.set(k, String(v)); },
  getPropertyValue(k) { return rootVars.get(k) || ""; },
  removeProperty(k) { rootVars.delete(k); },
};

const { state } = await import("../js/state.js");
const { applyTypographySettings } = await import("../js/app.js");
const { VirtualList } = await import("../js/components/virtual_list.js");
const { computeUiScale, getUiFontScale, getUiLayoutScale, scaleCssLength, scaleUiPx } = await import("../js/utils/ui_scale.js");
const { barChartSvg } = await import("../js/utils/svg_charts.js");
const { historySvg } = await import("../js/views/pahole/pahole_compare.js");

function makeVirtualListHost(clientHeight = 300) {
  const viewport = new FakeElement("div");
  viewport.clientHeight = clientHeight;
  viewport.scrollTop = 0;
  viewport.setAttribute = (k, v) => { viewport[k] = v; };
  viewport.removeAttribute = (k) => { delete viewport[k]; };
  const spacer = new FakeElement("div");
  const rowsEl = new FakeElement("div");
  const host = new FakeElement("div");
  host.querySelector = (sel) => ({ ".vl-viewport": viewport, ".vl-spacer": spacer, ".vl-rows": rowsEl }[sel] || null);
  return { host, viewport, spacer, rowsEl };
}

test("ui_scale pure helpers clamp, compute damped layoutScale, and scale px lengths", () => {
  assert.deepEqual(computeUiScale(100), { uiFontScale: 100, fontScale: 1, layoutScale: 1 });
  assert.deepEqual(computeUiScale(80), { uiFontScale: 80, fontScale: 0.8, layoutScale: 0.9 });
  assert.deepEqual(computeUiScale(160), { uiFontScale: 160, fontScale: 1.6, layoutScale: 1.3 });
  assert.deepEqual(computeUiScale(40), { uiFontScale: 80, fontScale: 0.8, layoutScale: 0.9 });
  assert.deepEqual(computeUiScale(300), { uiFontScale: 160, fontScale: 1.6, layoutScale: 1.3 });
  assert.equal(scaleUiPx(30, 1.3), 39);
  assert.equal(scaleUiPx(30, 0.9), 27);
  assert.equal(scaleCssLength("760px"), "calc(760px * var(--ui-layout-scale, 1))");
  assert.equal(scaleCssLength("90vw"), "90vw");
});

test("applyTypographySettings updates CSS vars, localStorage, and emits ui:scale-changed", () => {
  const scaleEvents = [];
  const codeEvents = [];
  const offScale = state.subscribe("ui:scale-changed", (e) => scaleEvents.push(e));
  const offCode = state.subscribe("codeview:font-changed", (e) => codeEvents.push(e));

  const res = applyTypographySettings(15, 160);
  assert.deepEqual(res, { codeFontSize: 15, rowHeight: 25, uiFontScale: 160, fontScale: 1.6, layoutScale: 1.3 });
  assert.equal(localStorage.getItem("kip-code-font-size"), "15");
  assert.equal(localStorage.getItem("kip-ui-font-scale"), "160");
  assert.equal(rootVars.get("--code-font-size"), "15px");
  assert.equal(rootVars.get("--code-row-height"), "25px");
  assert.equal(rootVars.get("--ui-font-scale"), "1.6");
  assert.equal(rootVars.get("--ui-layout-scale"), "1.3");
  assert.equal(getUiFontScale(), 1.6);
  assert.equal(getUiLayoutScale(), 1.3);
  assert.deepEqual(scaleEvents.at(-1), { uiFontScale: 160, fontScale: 1.6, layoutScale: 1.3 });
  assert.deepEqual(codeEvents.at(-1), { fontSize: 15, rowHeight: 25 });

  // Reset back to 100% for subsequent tests
  applyTypographySettings(12, 100);
  offScale();
  offCode();
});

test("VirtualList scales rowHeight on init and updates live on ui:scale-changed", () => {
  applyTypographySettings(12, 100);
  const { host, viewport, spacer, rowsEl } = makeVirtualListHost(300);
  const list = new VirtualList(host, {
    rowHeight: 30,
    renderRow: (r) => `<span>${r.id}</span>`,
    rowKey: (r) => r.id,
  });
  const rows = Array.from({ length: 100 }, (_, i) => ({ id: `row-${i}` }));
  list.setRows(rows);

  assert.equal(list.rowHeight, 30);
  assert.equal(spacer.style.height, "3000px");
  assert.ok(rowsEl.innerHTML.includes("height:30px"));

  // Scroll to row 20 (600px at 30px/row), then scale UI to 160% (layoutScale 1.3 => rowHeight 39)
  viewport.scrollTop = 600;
  applyTypographySettings(null, 160);

  assert.equal(list.rowHeight, 39);
  assert.equal(spacer.style.height, "3900px");
  assert.equal(viewport.scrollTop, 780);
  assert.ok(rowsEl.innerHTML.includes("height:39px"));

  // Scaling down to 80% (layoutScale 0.9 => rowHeight 27)
  applyTypographySettings(null, 80);
  assert.equal(list.rowHeight, 27);
  assert.equal(spacer.style.height, "2700px");
  assert.equal(viewport.scrollTop, 540);
  assert.ok(rowsEl.innerHTML.includes("height:27px"));

  // Dispose unsubscribes from ui:scale-changed
  list.dispose();
  applyTypographySettings(12, 100);
  assert.equal(list.rowHeight, 27);
});

test("SVG chart generators adjust text size and margins at non-default UI scale", () => {
  applyTypographySettings(12, 100);
  const baseChart = barChartSvg([{ x: 0, value: 10 }], { markers: [{ x: 0, label: "m" }] });
  assert.ok(baseChart.includes('font-size="10"'));

  applyTypographySettings(12, 160);
  const scaledChart = barChartSvg([{ x: 0, value: 10 }], { markers: [{ x: 0, label: "m" }] });
  assert.ok(!scaledChart.includes('font-size="10"'));

  const scaledPahole = historySvg([{ version: "v3.0", available: true, total_size: 24, holes_count: 1, cache_lines_used: 1 }]);
  assert.ok(!scaledPahole.includes('font-size="9"'));

  applyTypographySettings(12, 100);
});
