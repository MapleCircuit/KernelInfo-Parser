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
import { canvasToSvg, pngSize, svgSize } from "../js/views/nodemap/nodemap_export.js";

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

test("browser smoke: real-DOM interactive HTML viewer, virtual scroll rows & 2D translate positioning", async (t) => {
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

    // 2. Virtual Source 6,000 lines + 2D translate positioning in real Chromium layout
    const lines = Array.from({ length: 6000 }, (_, i) => `void fn_${i}(void) {}`);
    const range = computeVirtualRange(lines.length, 3000, 400);
    const vHtml = renderVirtualSourceWindow(lines, 1, "kernel/smoke.c", null, range);
    await page.setContent(`
      <div class="nm-node" style="position:absolute;transform:translate(120px, 80px);width:320px;height:400px;">
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

test("browser smoke: exported SVG renders and rasterizes to a full-resolution PNG in real Chromium", async (t) => {
  const browser = await tryLaunchBrowser();
  if (!browser) {
    t.skip("Playwright / headless Chromium not installed");
    return;
  }
  try {
    const page = await (await browser.newContext({ viewport: { width: 800, height: 600 } })).newPage();
    const a = M.createNode("note", { id: "a", x: 0, y: 0, w: 220, h: 120, color: "green", data: { text: "# Export\n**bold** `code`" }, locks: [{ kind: "rcu" }] });
    const b = M.createNode("source", { id: "b", x: 320, y: 0, w: 260, h: 120, data: { path: "a.c" } });
    const canvas = { ...M.emptyCanvas(), nodes: [a, b], edges: [M.createEdge({ from: "a", to: "b", label: "reads", lockKind: "rcu" })] };
    const tokens = { "--bg-canvas": "#0a0d12", "--bg-secondary": "#161b22", "--bg-tertiary": "#21262d", "--bg-primary": "#0d1117", "--text-primary": "#e6edf3", "--border-color": "#30363d", "--border-muted": "rgba(240, 246, 252, 0.1)", "--accent-green": "#3fb950", "--accent-blue": "#58a6ff", "--accent-cyan": "#39c5cf", "--accent-yellow": "#d29922" };
    const resolve = (c) => { const m = /^var\((--[\w-]+)\)$/.exec(c); return m ? tokens[m[1]] || "#808080" : c; };
    const svg = canvasToSvg(canvas, resolve, { content: (n) => (n.id === "b" ? { path: "a.c", start_line: 1, end_line: 2, total_lines: 2, lines: ["int x;", "int y;"] } : null) });
    const { w, h } = svgSize(svg);
    const size = pngSize(w, h, 2);
    const errors = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.setContent("<!doctype html><body></body>");
    const out = await page.evaluate(async ({ svg, size, scale }) => {
      const scaled = svg.replace(/<svg ([^>]*?)width="(\d+)" height="(\d+)"/, (m, pre, sw, sh) => `<svg ${pre}width="${Math.floor(sw * scale)}" height="${Math.floor(sh * scale)}"`);
      const img = new Image();
      img.src = URL.createObjectURL(new Blob([scaled], { type: "image/svg+xml" }));
      await img.decode();
      const c = document.createElement("canvas");
      c.width = size.w;
      c.height = size.h;
      const ctx = c.getContext("2d");
      ctx.drawImage(img, 0, 0, size.w, size.h);
      const px = ctx.getImageData(0, 0, size.w, size.h).data;
      let lit = 0;
      for (let i = 0; i < px.length; i += 4) if (px[i] + px[i + 1] + px[i + 2] > 300) lit += 1;
      return { natural: [img.naturalWidth, img.naturalHeight], lit, total: px.length / 4 };
    }, { svg, size, scale: size.scale });
    assert.deepEqual(out.natural, [size.w, size.h], "the SVG decodes at the output size (vectors, no upscale)");
    assert.ok(out.lit > 50 && out.lit < out.total, "text and borders rasterized");
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
