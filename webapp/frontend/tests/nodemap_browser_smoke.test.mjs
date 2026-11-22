/**
 * nodemap_browser_smoke.test.mjs - Optional real-Chromium Playwright smoke test for NodeMap.
 *
 * Skips cleanly when Playwright or a Chromium binary is not installed in the test environment.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as M from "../js/views/nodemap/canvas_model.js";
import { canvasToInteractiveHtml } from "../js/views/nodemap/nodemap_export_formats.js";
import { renderVirtualSourceWindow, computeVirtualRange } from "../js/views/nodemap/virtual_source.js";

async function tryLaunchBrowser() {
  for (const pkg of ["playwright", "playwright-core"]) {
    try {
      const pw = await import(pkg);
      const chromium = pw.chromium || pw.default?.chromium;
      if (!chromium) continue;
      const browser = await chromium.launch({ headless: true });
      return browser;
    } catch {
      // Package missing or browser binary not installed
    }
  }
  return null;
}

test("browser smoke: real-DOM interactive HTML viewer, virtual scroll rows & translate3d positioning", async (t) => {
  const browser = await tryLaunchBrowser();
  if (!browser) {
    t.skip("Playwright / headless Chromium not installed");
    return;
  }
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();

    // 1. Standalone Interactive HTML Viewer + Code Tour navigation
    const a = M.createNode("struct", { x: 80, y: 120, w: 280, h: 180, data: { name: "task_struct", kind: "struct" } });
    const b = M.createNode("symbol", { x: 480, y: 140, w: 240, h: 110, data: { name: "schedule", kind: "func" } });
    const e1 = M.createEdge({ from: a.id, to: b.id, label: "calls", lockKind: "rcu" });
    const canvas = M.normalizeCanvas({
      ...M.emptyCanvas(),
      nodes: [a, b],
      edges: [e1],
      tours: [{
        id: "t1",
        title: "Smoke Tour",
        steps: [
          { id: "s1", title: "Step One", caption: "Start at task_struct", nodeIds: [a.id], edgeIds: [], viewport: { x: 0, y: 0, zoom: 1 } },
          { id: "s2", title: "Step Two", caption: "Jump to schedule", nodeIds: [b.id], edgeIds: [e1.id], viewport: { x: 0, y: 0, zoom: 1 } },
        ],
      }],
    });
    const html = canvasToInteractiveHtml(canvas, "", { title: "Browser Smoke Export" });
    const consoleErrors = [];
    page.on("pageerror", (err) => consoleErrors.push(err.message));
    await page.setContent(html, { waitUntil: "load" });

    assert.equal(await page.textContent("#nm-tour-counter"), "1 / 2");
    await page.click("#nm-tour-next");
    assert.equal(await page.textContent("#nm-tour-counter"), "2 / 2");
    assert.equal(await page.textContent("#nm-tour-title"), "Step Two");
    await page.click("#nm-zoom-in");
    const vb = await page.getAttribute("#nm-stage svg", "viewBox");
    assert.ok(vb && vb.split(/\s+/).length === 4);

    // 2. Virtual Source 6,000 lines + translate3d positioning in real Chromium layout
    const lines = Array.from({ length: 6000 }, (_, i) => `void fn_${i}(void) {}`);
    const range = computeVirtualRange(lines.length, 3000, 400);
    const vHtml = renderVirtualSourceWindow(lines, 1, "kernel/smoke.c", null, range);
    await page.setContent(`
      <div class="nm-node" style="position:absolute;transform:translate3d(120px, 80px, 0);width:320px;height:400px;">
        <div class="nm-src" style="height:400px;overflow:auto;">${vHtml}</div>
      </div>
    `);
    const metrics = await page.evaluate(() => {
      const node = document.querySelector(".nm-node");
      const rows = document.querySelectorAll(".nm-src-row");
      const rect = node.getBoundingClientRect();
      return { left: Math.round(rect.left), top: Math.round(rect.top), rowCount: rows.length };
    });
    assert.equal(metrics.left, 120);
    assert.equal(metrics.top, 80);
    assert.ok(metrics.rowCount > 0 && metrics.rowCount < 200);
    assert.deepEqual(consoleErrors, []);
    await context.close();
  } finally {
    await browser.close();
  }
});
