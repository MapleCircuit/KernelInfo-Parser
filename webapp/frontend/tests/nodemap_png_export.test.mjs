/**
 * nodemap_png_export.test.mjs - PNG export of canvases with live DOM nodes (`nodemap_png.js`): tab
 * iframes loaded eagerly with a bounded wait and read at 1×, taint-free CSS sanitization of snapshots,
 * per-node rasters embedded in the vector canvas (one bad node never degrades another), the 2D paint
 * fallback when a snapshot is refused, placeholders only when nothing is readable, and `exportPNG()`
 * rendering again without the rasters when the browser refuses the whole image.
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

const M = await import("../js/views/nodemap/canvas_model.js");
const X = await import("../js/views/nodemap/nodemap_export.js");
const P = await import("../js/views/nodemap/nodemap_png.js");
const { exportPNG } = await import("../js/views/nodemap/nodemap_persistence.js");

const PNG = "data:image/png;base64,iVBORw0KGgo=";

// ------------------------------------------------------------------ sanitization

test("sanitizeCssValue: url() layers go, data: URLs and gradients stay, SVG refs follow renamed ids, vars resolve", () => {
  const ids = new Map([["grad", "fo1-grad"]]);
  assert.equal(X.sanitizeCssValue("background-image", 'linear-gradient(red, blue), url("https://cdn.example/x.png")'), "linear-gradient(red, blue)");
  assert.equal(X.sanitizeCssValue("background-image", 'url("/img/a.png")'), null, "only a url layer: left out");
  assert.equal(X.sanitizeCssValue("background-image", 'url("data:image/png;base64,AAAA")'), 'url("data:image/png;base64,AAAA")');
  assert.equal(X.sanitizeCssValue("mask-image", "url(/m.svg), none"), "none");
  assert.equal(X.sanitizeCssValue("list-style-image", 'url("/bullet.png")'), null);
  assert.equal(X.sanitizeCssValue("clip-path", 'url("#clip")'), null, "a clip outside the snapshot is left out");
  assert.equal(X.sanitizeCssValue("clip-path", "inset(0px 2px)"), "inset(0px 2px)");
  assert.equal(X.sanitizeCssValue("fill", 'url("#grad")', { ids }), 'url("#fo1-grad")', "a gradient of the snapshot");
  assert.equal(X.sanitizeCssValue("fill", 'url("#other") rgb(1, 2, 3)', { ids }), "rgb(1, 2, 3)", "its fallback color");
  assert.equal(X.sanitizeCssValue("filter", 'url("https://x/f.svg#f")'), null);
  assert.equal(X.sanitizeCssValue("color", "var(--accent, #ff0000)"), "#ff0000", "variables evaluated (utils/css_color.js)");
  assert.equal(X.sanitizeCssValue("stop-color", "color-mix(in srgb, #000000 50%, #ffffff)"), "#808080");
  assert.equal(X.sanitizeCssValue("width", "120px"), "120px");
});

test("canvasToSvg: per-node rasters replace live bodies as data: images (placeholders without one)", () => {
  const canvas = M.emptyCanvas();
  const a = M.createNode("tab", { id: "ta", x: 0, y: 0, w: 300, h: 200, data: { tabType: "code", hash: "tab=code" } });
  const b = M.createNode("tab", { id: "tb", x: 400, y: 0, w: 300, h: 200, data: { tabType: "commits", hash: "tab=commits" } });
  canvas.nodes.push(a, b);
  const rasters = (n) => (n.id === "ta" ? { href: PNG, w: 300, h: 170 } : n.id === "tb" ? { href: "javascript:alert(1)", w: 1, h: 1 } : null);
  const svg = X.canvasToSvg(canvas, (c) => c, { rasters, foreign: () => ({ html: "<div/>", w: 1, h: 1 }) });
  assert.equal((svg.match(/<image /g) || []).length, 1);
  assert.match(svg, new RegExp(`<image x="0" y="\\d+" width="300" height="170" preserveAspectRatio="none" href="${PNG.replace(/[+/=]/g, "\\$&")}"/>`));
  assert.doesNotMatch(svg, /javascript:/, "only a data: PNG / JPEG / WebP is embedded");
  assert.match(svg, /<foreignObject/, "the other node falls back to its snapshot in the SVG export path");
  const plain = X.canvasToSvg(canvas, (c) => c, {});
  assert.doesNotMatch(plain, /<image |<foreignObject/);
  assert.match(plain, /Live code tab/, "no raster, no snapshot: the placeholder card");
});

// ------------------------------------------------------------------ frames

function fakeFrame({ ready = false, lazy = true, ss = 1 } = {}) {
  const listeners = new Set();
  const doc = { readyState: ready ? "complete" : "loading", body: { childElementCount: ready ? 1 : 0 }, querySelector: () => null };
  const win = { location: { href: ready ? "http://localhost/?embed=1" : "about:blank" }, requestAnimationFrame: (fn) => setTimeout(fn, 0) };
  const attrs = lazy ? { loading: "lazy" } : {};
  const frame = {
    contentDocument: doc,
    contentWindow: win,
    dataset: ss > 1 ? { ss: String(ss) } : {},
    get loading() { return attrs.loading || "auto"; },
    set loading(v) { attrs.loading = v; },
    getAttribute: (k) => attrs[k] ?? null,
    setAttribute: (k, v) => { attrs[k] = v; },
    addEventListener: (t, fn) => listeners.add(fn),
    removeEventListener: (t, fn) => listeners.delete(fn),
    load() {
      doc.readyState = "complete";
      doc.body.childElementCount = 2;
      win.location.href = "http://localhost/?embed=1";
      for (const fn of [...listeners]) fn();
    },
    listeners,
  };
  return frame;
}

test("prepareTabFrames: lazy frames load eagerly and are awaited; an unresponsive one times out; supersampling comes back", async () => {
  const lazy = fakeFrame();
  const stuck = fakeFrame();
  const sharp = fakeFrame({ ready: true, lazy: false, ss: 2 });
  const applied = [];
  const setSupersample = (f, k) => {
    applied.push([f === sharp ? "sharp" : "?", k]);
    f.dataset.ss = String(k);
  };
  setTimeout(() => lazy.load(), 20);
  const t0 = Date.now();
  const prep = await P.prepareTabFrames([{ id: "lazy", frame: lazy }, { id: "stuck", frame: stuck }, { id: "sharp", frame: sharp }], { timeoutMs: 120, setSupersample, pollMs: 5 });
  const took = Date.now() - t0;
  assert.equal(lazy.getAttribute("loading"), "eager", "a lazy frame is told to load now");
  assert.equal(P.frameReady(lazy), true);
  assert.deepEqual(prep.timedOut, ["stuck"]);
  assert.ok(took < 1000, `bounded wait (${took} ms)`);
  assert.equal(stuck.listeners.size, 0, "no listener left behind");
  assert.deepEqual(applied, [["sharp", 1]], "read at 1× while capturing");
  prep.restore();
  assert.deepEqual(applied, [["sharp", 1], ["sharp", 2]]);
});

// ------------------------------------------------------------------ fake env and DOM

/** Raster env: `fail(svg)` makes an image load fail, `taint(svg)` a canvas tainted by it. */
function fakeEnv({ fail = () => false, taint = () => false, canvasMax = Infinity } = {}) {
  const made = { canvases: [], loads: [] };
  return {
    made,
    createCanvas(w, h) {
      const ops = [];
      const ctx = new Proxy({}, { get: (t, k) => (k in t ? t[k] : (...args) => ops.push([k, ...args])), set: (t, k, v) => ((t[k] = v), true) });
      const c = { width: w, height: h, ops, tainted: false, getContext: () => (w * h > canvasMax ? null : ctx) };
      ctx.drawImage = (img, ...args) => {
        ops.push(["drawImage", img.svg ? "svg" : "el", ...args]);
        if (img.svg && taint(img.svg)) c.tainted = true;
      };
      made.canvases.push(c);
      return c;
    },
    async loadImage(svg) {
      made.loads.push(svg);
      if (fail(svg)) throw new Error("refused");
      return { svg };
    },
    toDataURL(c) {
      if (c.tainted) throw new Error("SecurityError: tainted");
      return PNG;
    },
  };
}

test("rasterizeSnapshot: a snapshot becomes a PNG at the node density; refusals and taint give null", async () => {
  const fo = { html: '<div xmlns="http://www.w3.org/1999/xhtml">hi</div>', w: 200, h: 100, bg: "rgb(0, 0, 0)" };
  const env = fakeEnv();
  assert.equal(await P.rasterizeSnapshot(fo, 3, env), PNG);
  assert.deepEqual([env.made.canvases[0].width, env.made.canvases[0].height], [600, 300]);
  assert.match(env.made.loads[0], /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" width="600" height="300" viewBox="0 0 200 100"><foreignObject/);
  assert.equal(await P.rasterizeSnapshot(fo, 3, fakeEnv({ fail: () => true })), null, "image refused");
  assert.equal(await P.rasterizeSnapshot(fo, 3, fakeEnv({ taint: () => true })), null, "tainted canvas");
  const small = fakeEnv({ canvasMax: 300 * 150 });
  assert.equal(await P.rasterizeSnapshot(fo, 3, small), PNG, "a canvas too large halves the density");
  assert.deepEqual(small.made.canvases.map((c) => c.width), [600, 300]);
  const blank = fakeEnv();
  let n = 0;
  blank.toDataURL = () => (n++ === 0 ? "data:," : PNG); // a canvas the browser could not make answers "data:,"
  assert.equal(await P.rasterizeSnapshot(fo, 3, blank), PNG, "a blank answer steps down instead of passing for a raster");
  assert.deepEqual(blank.made.canvases.map((c) => c.width), [600, 300]);
});

/** Minimal DOM for snapshots and the paint fallback: elements with computed styles, rects and text. */
function fakeDom({ throwStyle = false, marker = "" } = {}) {
  const styles = new Map();
  const win = {
    getComputedStyle: (el) => {
      if (throwStyle) throw new Error("detached");
      const st = styles.get(el) || {};
      return { getPropertyValue: (p) => st[p] ?? "" };
    },
  };
  const doc = {
    readyState: "complete",
    body: null,
    defaultView: win,
    createElement: (tag) => el(tag),
    createRange: () => {
      let node = null;
      return {
        selectNodeContents: (n) => (node = n),
        setStart() {}, setEnd() {},
        getClientRects: () => (node ? [node.rect] : []),
        getBoundingClientRect: () => node?.rect,
      };
    },
    querySelector: () => null,
  };
  function el(tag, { style = {}, rect = { left: 0, top: 0, width: 0, height: 0 }, children = [] } = {}) {
    const attrs = {};
    const e = {
      nodeType: 1, localName: tag, tagName: tag.toUpperCase(), namespaceURI: "http://www.w3.org/1999/xhtml", ownerDocument: doc,
      childNodes: [], scrollTop: 0, scrollLeft: 0, rect,
      get attributes() { return Object.entries(attrs).map(([name, value]) => ({ name, value })); },
      setAttribute: (k, v) => { attrs[k] = String(v); },
      getAttribute: (k) => attrs[k] ?? null,
      removeAttribute: (k) => { delete attrs[k]; },
      appendChild: (c) => (e.childNodes.push(c), c),
      cloneNode: () => el(tag),
      matches: () => false,
      getBoundingClientRect: () => rect,
      querySelectorAll: () => [],
      attrs,
    };
    styles.set(e, style);
    for (const c of children) e.appendChild(c);
    return e;
  }
  const text = (s, rect) => ({ nodeType: 3, nodeValue: s, rect, cloneNode: () => ({ nodeType: 3, nodeValue: s }) });
  const root = el("div", {
    style: { display: "block", "background-color": "rgb(13, 17, 23)", color: "rgb(201, 209, 217)", "font-size": "12px", "font-family": "monospace", "overflow-x": "hidden", "overflow-y": "auto" },
    rect: { left: 0, top: 0, width: 300, height: 170 },
    children: [text(`hello ${marker}`, { left: 4, top: 4, width: 60, height: 14 })],
  });
  root.id = "workspace";
  doc.querySelector = (sel) => (sel === "#workspace" ? root : null);
  return { root, doc, win };
}

/** A node element whose body holds a tab iframe (or a man page) over `dom`. */
function nodeEl(type, dom) {
  const frame = { contentDocument: dom.doc, contentWindow: dom.win, getAttribute: () => null, dataset: {} };
  const body = {
    offsetWidth: 300, offsetHeight: 170,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 300, height: 170 }),
    querySelector: (sel) => (sel === "iframe" ? (type === "tab" ? frame : null) : sel === ".nm-man" ? dom.root : null),
  };
  return { ownerDocument: { defaultView: dom.win }, querySelector: (sel) => (sel === ".nm-node-body" ? body : null) };
}

/** XMLSerializer stand-in for the fake DOM (enough for the snapshot path). */
function withSerializer(fn) {
  const prev = globalThis.XMLSerializer;
  const ser = (n) => (n.nodeType === 3 ? String(n.nodeValue).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    : `<${n.localName}${n.attributes.map((a) => ` ${a.name}="${String(a.value).replace(/"/g, "&quot;")}"`).join("")}>${n.childNodes.map(ser).join("")}</${n.localName}>`);
  globalThis.XMLSerializer = class { serializeToString(n) { return ser(n); } };
  return Promise.resolve().then(fn).finally(() => { globalThis.XMLSerializer = prev; });
}

test("liveNodeRasters: each node on its own — snapshot, paint fallback when refused, placeholder when unreadable", () => withSerializer(async () => {
  const canvas = M.emptyCanvas();
  const good = M.createNode("tab", { id: "good", x: 0, y: 0, w: 300, h: 200, data: { tabType: "code", hash: "tab=code" } });
  const refused = M.createNode("tab", { id: "refused", x: 400, y: 0, w: 300, h: 200, data: { tabType: "commits", hash: "tab=commits" } });
  const broken = M.createNode("man", { id: "broken", x: 800, y: 0, w: 300, h: 200, data: { name: "open", section: "2", path: "man2/open.2" } });
  const scaled = M.createNode("tab", { id: "scaled", x: 0, y: 300, w: 300, h: 200, scale: 2, data: { tabType: "code", hash: "tab=code" } });
  canvas.nodes.push(good, refused, broken, scaled);
  const els = {
    good: nodeEl("tab", fakeDom({ marker: "GOOD" })),
    refused: nodeEl("tab", fakeDom({ marker: "REFUSED" })),
    broken: nodeEl("man", fakeDom({ throwStyle: true })),
    scaled: nodeEl("tab", fakeDom({ marker: "SCALED" })),
  };
  let layouts = 0;
  const ctrl = { canvas, renderer: { element: (id) => els[id] }, withExportLayout: (fn) => (layouts++, fn()) };
  const env = fakeEnv({ fail: (svg) => svg.includes("REFUSED") });
  const { rasters, report } = await P.liveNodeRasters(ctrl, 2, env);
  assert.deepEqual(report, { dom: ["good", "scaled"], painted: ["refused"], placeholder: ["broken"] });
  assert.equal(rasters.get("good").href, PNG);
  assert.equal(rasters.get("good").via, "dom");
  assert.deepEqual([rasters.get("good").w, rasters.get("good").h], [300, 170], "content units of the body");
  assert.equal(rasters.get("refused").via, "paint", "drawn from its DOM instead of a placeholder");
  assert.equal(rasters.get("broken"), null);
  assert.ok(layouts >= 4, "captured inside withExportLayout()");
  const sizes = env.made.canvases.map((c) => [c.width, c.height]);
  assert.deepEqual(sizes[0], [600, 340], "pixelScale 2");
  assert.ok(sizes.some(([w, h]) => w === 1200 && h === 680), "pixelScale × content scale for a scaled node");
  // the paint fallback drew the background, then the text
  const paintCanvas = env.made.canvases.find((c) => c.ops.some((op) => op[0] === "fillText"));
  assert.ok(paintCanvas.ops.some((op) => op[0] === "fillText" && op[1] === "hello REFUSED"));
}));

test("collectPaintOps: boxes, borders and text inside their clips; null without a size", () => {
  const dom = fakeDom({ marker: "X" });
  const body = { offsetWidth: 300, offsetHeight: 170, getBoundingClientRect: () => ({ left: 0, top: 0, width: 300, height: 170 }) };
  const paint = P.collectPaintOps({ body, root: dom.root, win: dom.win, frame: {} });
  assert.equal(paint.bg, "rgb(13, 17, 23)");
  assert.deepEqual(paint.ops.map((o) => o.k), ["rect", "text"]);
  assert.equal(paint.ops[1].clip, 1, "the scroll box clips its text");
  assert.equal(paint.ops[1].font, "normal 400 12px monospace");
  assert.equal(P.collectPaintOps({ body: { offsetWidth: 0, offsetHeight: 0 }, root: dom.root, win: dom.win }), null);
});

// ------------------------------------------------------------------ exportPNG

function pngHarness({ imageFails = () => false } = {}) {
  const made = { images: [], canvases: [] };
  const prev = { Image: globalThis.Image, create: globalThis.document.createElement, URL: globalThis.URL };
  globalThis.Image = class {
    set src(v) {
      made.images.push(v);
      queueMicrotask(() => (imageFails(made.images.length) ? this.onerror?.() : this.onload?.()));
    }
  };
  globalThis.document.createElement = (tag) => {
    if (tag !== "canvas") return prev.create.call(globalThis.document, tag);
    const cv = { width: 0, height: 0, getContext: () => ({ drawImage() {} }), toBlob: (cb) => queueMicrotask(() => cb({ size: 1 })) };
    made.canvases.push(cv);
    return cv;
  };
  globalThis.URL = Object.assign(Object.create(prev.URL), { createObjectURL: () => "blob:1", revokeObjectURL() {} });
  return { made, restore: () => { Object.assign(globalThis, { Image: prev.Image, URL: prev.URL }); globalThis.document.createElement = prev.create; } };
}

function exportCtrl(calls, downloads) {
  const canvas = M.emptyCanvas();
  canvas.nodes.push(M.createNode("tab", { id: "good", x: 0, y: 0, w: 300, h: 200, data: { tabType: "code", hash: "tab=code" } }));
  const prepared = [];
  return {
    canvas,
    prepared,
    renderer: { element: () => nodeEl("tab", fakeDom({ marker: "GOOD" })) },
    withExportLayout: (fn) => fn(),
    prepareExportFrames: async () => (prepared.push("prepare"), { timedOut: [], restore: () => prepared.push("restore") }),
    canvasSvg: (o) => {
      calls.push(o);
      const img = o.rasters?.(canvas.nodes[0]);
      return `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.floor(400 * (o.pixelScale || 1))}" height="${Math.floor(300 * (o.pixelScale || 1))}" viewBox="0 0 400 300">${img ? `<image href="${img.href}"/>` : ""}</svg>`;
    },
    download: (name) => downloads.push(name),
  };
}

test("exportPNG: frames prepared and restored, node rasters embedded at the output density, one render", () => withSerializer(async () => {
  const { restore } = pngHarness();
  try {
    const calls = [];
    const downloads = [];
    const ctrl = exportCtrl(calls, downloads);
    const env = fakeEnv();
    const res = await exportPNG(ctrl, { scale: 2, env });
    assert.deepEqual(ctrl.prepared, ["prepare", "restore"]);
    assert.deepEqual(res.report.dom, ["good"]);
    assert.equal(calls.length, 2, "size probe + one render");
    assert.equal(calls[1].pixelScale, 2);
    assert.equal(calls[1].foreign, false, "no all-or-nothing foreignObject image");
    assert.equal(calls[1].rasters(ctrl.canvas.nodes[0]).href, PNG);
    assert.deepEqual([env.made.canvases[0].width, env.made.canvases[0].height], [600, 340], "the node raster at 2×");
    assert.deepEqual(downloads, ["nodemap_canvas.png"]);
  } finally {
    restore();
  }
}));

test("exportPNG: a whole image the browser refuses with rasters is rendered again with placeholders", () => withSerializer(async () => {
  const { restore } = pngHarness({ imageFails: (n) => n === 1 });
  try {
    const calls = [];
    const downloads = [];
    const ctrl = exportCtrl(calls, downloads);
    const res = await exportPNG(ctrl, { scale: 1, env: fakeEnv() });
    assert.deepEqual(calls.slice(1).map((o) => Boolean(o.rasters)), [true, false]);
    assert.deepEqual(downloads, ["nodemap_canvas.png"]);
    assert.deepEqual([res.w, res.h], [400, 300]);
  } finally {
    restore();
  }
}));
