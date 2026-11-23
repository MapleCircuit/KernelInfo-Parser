/**
 * nodemap_export_pipeline.test.mjs - NodeMap image export pipeline: culled / low-LOD nodes and
 * `content-visibility` rows materialized for the export and restored, live scroll offsets, full-resolution
 * PNG rasterization (target-size SVG, canvas-limit step-down, snapshot fallback) and the worker's decode.
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};

const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const M = await import("../js/views/nodemap/canvas_model.js");
const { exportPNG } = await import("../js/views/nodemap/nodemap_persistence.js");
const { pngSize, svgSize } = await import("../js/views/nodemap/nodemap_export.js");
const { rasterizePngJob } = await import("../js/views/nodemap/nodemap_worker.js");

/** An element with a real class list, a scroll position and queryable children. */
function el(classes = [], { scrollTop = 0, children = {}, all = {} } = {}) {
  const set = new Set(classes);
  return {
    classList: { add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c), toggle: (c, on) => (on ?? !set.has(c) ? set.add(c) : set.delete(c)) },
    classes: set,
    scrollTop,
    offsetHeight: 0,
    querySelector: (sel) => children[sel] || null,
    querySelectorAll: (sel) => all[sel] || [],
  };
}

function controller(nodes) {
  const c = new NodeMapController();
  c.tab = { id: "t", type: "nodemap", version: "v3.0", repo: "" };
  c.canvas = { ...M.emptyCanvas(), nodes };
  return c;
}

test("withExportLayout: culled nodes, the low-LOD layer and content-visibility rows are materialized, then restored", () => {
  const a = M.createNode("note", { id: "a", x: 0, y: 0 });
  const b = M.createNode("note", { id: "b", x: 900, y: 0 });
  const layer = el(["nm-lod-low"]);
  const els = { a: el(["nm-node", "nm-culled"]), b: el(["nm-node"]) };
  const c = controller([a, b]);
  c.renderer = { layer, element: (id) => els[id] || null };
  let inside = null;
  const out = c.withExportLayout(() => {
    inside = { lod: layer.classList.contains("nm-lod-low"), exporting: layer.classList.contains("nm-exporting"), culled: els.a.classList.contains("nm-culled") };
    return 42;
  });
  assert.equal(out, 42);
  assert.deepEqual(inside, { lod: false, exporting: true, culled: false }, "full detail while the export measures and snapshots");
  assert.ok(layer.classList.contains("nm-lod-low") && !layer.classList.contains("nm-exporting"), "the layer is restored");
  assert.ok(els.a.classList.contains("nm-culled") && !els.b.classList.contains("nm-culled"), "only the culled node is culled again");
  assert.throws(() => c.withExportLayout(() => {
    throw new Error("boom");
  }), /boom/);
  assert.ok(layer.classList.contains("nm-lod-low") && els.a.classList.contains("nm-culled") && !layer.classList.contains("nm-exporting"), "restored in finally");
  c.renderer = null;
  assert.equal(c.withExportLayout(() => "no renderer"), "no renderer");
});

test("canvasSvg runs inside withExportLayout; scrollOf reads the live body and mail boxes", () => {
  const src = M.createNode("source", { id: "s", x: 0, y: 0, w: 300, h: 200, data: { path: "a.c" } });
  const mail = M.createNode("mail", { id: "m", x: 400, y: 0, data: { tid: 1 } });
  const bodyS = el([], { children: { ".nm-scroll": el([], { scrollTop: 66 }) } });
  const bodyM = el([], { children: { ".nm-scroll": el([], { scrollTop: 10 }) }, all: { ".nm-mail-body:not(.nm-mail-body-single)": [el([], { scrollTop: 30 }), el([], { scrollTop: 0 })] } });
  const els = { s: el(["nm-node"], { children: { ".nm-node-body": bodyS } }), m: el(["nm-node"], { children: { ".nm-node-body": bodyM } }) };
  const layer = el([]);
  const c = controller([src, mail]);
  let wrapped = 0;
  c.renderer = { layer, element: (id) => els[id] || null };
  const orig = c.withExportLayout.bind(c);
  c.withExportLayout = (fn) => {
    wrapped += 1;
    return orig(fn);
  };
  assert.deepEqual(c.scrollOf(src), { top: 66, boxes: [] });
  assert.deepEqual(c.scrollOf(mail), { top: 10, boxes: [30, 0] });
  assert.equal(c.scrollOf(M.createNode("note", { id: "x" })), null, "nothing mounted");
  assert.match(c.canvasSvg(), /^<svg /);
  assert.equal(wrapped, 1);
  assert.equal(c.textMeasurer({ sans: "sans-serif", mono: "monospace" }), null, "no canvas under Node: estimated widths");
  assert.equal(typeof c.svgOptions().scroll, "function");
  assert.equal(c.svgOptions().onCanvas(M.createNode("kconfig", { id: "k", data: { symbol: "A" } })) instanceof Set, true);
});

/** Browser fakes for exportPNG: Image, canvas, Blob URLs and a download sink. */
function pngHarness({ canvasOk = (w, h) => true, imageFails = () => false } = {}) {
  const made = { canvases: [], images: [], urls: [] };
  const prev = { Image: globalThis.Image, create: globalThis.document.createElement, URL: globalThis.URL };
  globalThis.Image = class {
    set src(v) {
      made.images.push({ src: v, w: this.width, h: this.height });
      queueMicrotask(() => (imageFails(made.images.length) ? this.onerror?.() : this.onload?.()));
    }
  };
  globalThis.document.createElement = (tag) => {
    if (tag !== "canvas") return prev.create.call(globalThis.document, tag);
    const cv = {
      width: 0, height: 0,
      getContext: () => (canvasOk(cv.width, cv.height) ? { drawImage: (...args) => (cv.drawn = args) } : null),
      toBlob: (cb) => queueMicrotask(() => cb(canvasOk(cv.width, cv.height) ? { size: cv.width * cv.height } : null)),
    };
    made.canvases.push(cv);
    return cv;
  };
  globalThis.URL = Object.assign(Object.create(prev.URL), { createObjectURL: (b) => (made.urls.push(b), `blob:${made.urls.length}`), revokeObjectURL() {} });
  const restore = () => Object.assign(globalThis, { Image: prev.Image, URL: prev.URL }) && (globalThis.document.createElement = prev.create);
  return { made, restore };
}

const settle = () => new Promise((r) => setTimeout(r, 20));

test("exportPNG: the SVG is rendered at the target pixels (never a 1× bitmap upscaled) and downloaded", async () => {
  const { made, restore } = pngHarness();
  try {
    const calls = [];
    const downloads = [];
    const ctrl = {
      canvasSvg: (o) => (calls.push(o), `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.floor(400 * (o.pixelScale || 1))}" height="${Math.floor(300 * (o.pixelScale || 1))}" viewBox="0 0 400 300"></svg>`),
      download: (name, blob) => downloads.push([name, blob]),
    };
    const size = exportPNG(ctrl, { scale: 3 });
    await settle();
    assert.deepEqual(size, pngSize(400, 300, 3));
    assert.equal(calls[1].pixelScale, 3, "rasterized from an SVG sized 3× (same viewBox)");
    assert.equal(calls[1].foreign, true, "the live snapshots are tried first");
    const cv = made.canvases.at(-1);
    assert.deepEqual([cv.width, cv.height], [1200, 900]);
    assert.deepEqual(cv.drawn.slice(1), [0, 0, 1200, 900], "drawn once at the output size");
    assert.equal(svgSize(calls[1].pixelScale ? ctrl.canvasSvg({ pixelScale: 3 }) : "").w, 1200);
    assert.match(made.images[0].src, /^blob:/, "a Blob URL, not a size-limited data: URL");
    assert.deepEqual(downloads.map((d) => d[0]), ["nodemap_canvas.png"]);
  } finally {
    restore();
  }
});

test("exportPNG: a canvas the browser cannot allocate steps the factor down instead of failing", async () => {
  const { made, restore } = pngHarness({ canvasOk: (w, h) => w * h <= 1200 * 900 / 4 });
  try {
    const calls = [];
    const downloads = [];
    const ctrl = {
      canvasSvg: (o) => (calls.push(o), `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.floor(400 * (o.pixelScale || 1))}" height="${Math.floor(300 * (o.pixelScale || 1))}" viewBox="0 0 400 300"></svg>`),
      download: (name) => downloads.push(name),
    };
    exportPNG(ctrl, { scale: 4 });
    await settle();
    assert.deepEqual(calls.filter((o) => o.pixelScale).map((o) => o.pixelScale), [4, 2, 1], "halved until the canvas fits");
    assert.deepEqual(downloads, ["nodemap_canvas.png"]);
    assert.deepEqual([made.canvases.at(-1).width, made.canvases.at(-1).height], [400, 300]);
  } finally {
    restore();
  }
});

test("exportPNG: an image that refuses the DOM snapshots is rendered again with placeholders", async () => {
  const { restore } = pngHarness({ imageFails: (n) => n === 1 });
  try {
    const calls = [];
    const downloads = [];
    const ctrl = {
      canvasSvg: (o) => (calls.push(o), `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300">${o.foreign ? "<foreignObject/>" : ""}</svg>`),
      download: (name) => downloads.push(name),
    };
    exportPNG(ctrl, { scale: 1 });
    await settle();
    assert.deepEqual(calls.slice(1).map((o) => o.foreign), [true, false]);
    assert.deepEqual(downloads, ["nodemap_canvas.png"]);
  } finally {
    restore();
  }
});

test("exportPNG: a vector-only SVG goes to the worker's OffscreenCanvas at the output size", async () => {
  const prev = globalThis.OffscreenCanvas;
  globalThis.OffscreenCanvas = class {};
  try {
    const jobs = [];
    const downloads = [];
    const ctrl = {
      canvasSvg: (o) => `<svg xmlns="http://www.w3.org/2000/svg" width="${200 * (o.pixelScale || 1)}" height="${100 * (o.pixelScale || 1)}" viewBox="0 0 200 100"></svg>`,
      workerPool: { workerSupported: true, rasterizePng: async (j) => (jobs.push(j), { blob: { size: 1 } }) },
      download: (name) => downloads.push(name),
    };
    exportPNG(ctrl, { scale: 2 });
    await settle();
    assert.deepEqual(jobs.map((j) => [j.width, j.height]), [[400, 200]]);
    assert.match(jobs[0].svg, /width="400" height="200"/);
    assert.deepEqual(downloads, ["nodemap_canvas.png"]);
  } finally {
    globalThis.OffscreenCanvas = prev;
  }
});

test("rasterizePngJob decodes the SVG straight at the output size", async () => {
  const prev = { O: globalThis.OffscreenCanvas, C: globalThis.createImageBitmap };
  const seen = {};
  globalThis.createImageBitmap = async (blob, opts) => (Object.assign(seen, opts), { close() {} });
  globalThis.OffscreenCanvas = class {
    constructor(w, h) {
      seen.canvas = [w, h];
    }
    getContext() {
      return { drawImage: (...a) => (seen.draw = a.slice(1)) };
    }
    async convertToBlob() {
      return { type: "image/png" };
    }
  };
  try {
    const res = await rasterizePngJob({ svg: "<svg/>", width: 1200, height: 900 });
    assert.equal(res.blob.type, "image/png");
    assert.deepEqual([seen.resizeWidth, seen.resizeHeight, seen.resizeQuality], [1200, 900, "high"]);
    assert.deepEqual(seen.canvas, [1200, 900]);
    assert.deepEqual(seen.draw, [0, 0, 1200, 900]);
  } finally {
    globalThis.OffscreenCanvas = prev.O;
    globalThis.createImageBitmap = prev.C;
  }
});
