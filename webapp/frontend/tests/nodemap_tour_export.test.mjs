/**
 * nodemap_tour_export.test.mjs - Unit tests for NodeMap Tour Frame Export (PNG Sequence):
 *   1. Pure timeline planner (target stops, interior transitions, custom ranges, FPS, hold, degenerate cases)
 *   2. Filename formatter & sanitizer (5+ digit widening, illegal char stripping, dot-stem guards, preview)
 *   3. Resolution bounds & uniform aspect-ratio-safe framing math (independent of devicePixelRatio)
 *   4. Visual options, hidden toggle default, HUD compositing exclusion, directory-picker/fallback writers,
 *      single working canvas reuse, mid-run cancellation, and guaranteed live-state restoration.
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
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};

const M = await import("../js/views/nodemap/canvas_model.js");
const X = await import("../js/views/nodemap/nodemap_export.js");
const { NodeMapController } = await import("../js/views/nodemap/nodemap_controller.js");
const { NodeMapTourPlayer, createTour } = await import("../js/views/nodemap/tour_mode.js");
const { onKeyDown, onPointerDown, onWheel } = await import("../js/views/nodemap/nodemap_gestures.js");
const P = await import("../js/views/nodemap/tour_export_plan.js");
const E = await import("../js/views/nodemap/tour_export.js");

function makeSampleTour(count = 3) {
  const tour = createTour("Kernel Walkthrough");
  for (let i = 0; i < count; i++) {
    tour.steps.push({
      id: `step_${i + 1}`,
      title: `Target ${i + 1}`,
      caption: `Caption ${i + 1}`,
      nodeIds: i === 0 ? ["n1"] : i === 1 ? ["n2"] : [],
      edgeIds: i === 0 ? ["e1"] : [],
      viewport: { x: -i * 200, y: -i * 100, zoom: i === 0 ? 1 : i === 1 ? 1.5 : 0.75 },
    });
  }
  return tour;
}

// ------------------------------------------------------------------ 1. Pure Frame Timeline Planner

test("planTourFrames: target-only mode emits 1 frame per stop by default", () => {
  const tour = makeSampleTour(3);
  const plan = P.planTourFrames(tour, { includeTransitions: false, holdSeconds: "0" }, { width: 1000, height: 700 });
  assert.equal(plan.valid, true);
  assert.equal(plan.totalFrames, 3);
  assert.equal(plan.stopCount, 3);
  assert.equal(plan.transitionCount, 0);
  assert.deepEqual(plan.frames.map((f) => [f.kind, f.stepIndex, f.frameNumber]), [
    ["stop", 0, 1],
    ["stop", 1, 2],
    ["stop", 2, 3],
  ]);
});

test("planTourFrames: full-tour transitions emit strictly interior frames with zero duplicate endpoints", () => {
  const tour = makeSampleTour(3);
  const plan = P.planTourFrames(tour, {
    includeTransitions: true,
    fpsPreset: "30",
    durationOverride: "0.5",
    holdSeconds: "0",
  }, { width: 1000, height: 700 });

  // intervals = round(0.5 * 30) = 15 => 14 interior frames per transition
  // 3 stops + 2 * 14 transitions = 31 frames
  assert.equal(plan.valid, true);
  assert.equal(plan.totalFrames, 31);
  assert.equal(plan.stopCount, 3);
  assert.equal(plan.transitionCount, 28);
  assert.ok(Math.abs(plan.approxDurationSec - 31 / 30) < 1e-9);

  // Verify boundary frames: Stop 0 -> 14 interior (rawT in (0, 1)) -> Stop 1
  assert.equal(plan.frames[0].kind, "stop");
  assert.equal(plan.frames[0].stepIndex, 0);
  for (let k = 1; k < 15; k++) {
    const f = plan.frames[k];
    assert.equal(f.kind, "transition");
    assert.ok(f.rawT > 0 && f.rawT < 1, "strictly interior t");
    assert.equal(f.rawT, k / 15);
    assert.equal(f.easedT, P.tourEaseOut(k / 15));
    assert.equal(f.discreteStepIndex, k / 15 < 0.5 ? 0 : 1);
  }
  assert.equal(plan.frames[15].kind, "stop");
  assert.equal(plan.frames[15].stepIndex, 1);

  // Verify log-scale zoom interpolation on transition 0 -> 1 (zoom 1 -> 1.5)
  const midFrame = plan.frames[7]; // rawT = 7/15
  const expectedZoom = Math.exp(Math.log(1) + (Math.log(1.5) - Math.log(1)) * midFrame.easedT);
  assert.ok(Math.abs(midFrame.camera.zoom - expectedZoom) < 1e-9);
});

test("planTourFrames: custom From -> To sub-ranges, custom FPS, duration override, and hold frames", () => {
  const tour = makeSampleTour(4);

  // Multi-target sub-range Target 2 -> Target 4 with hold = 0.5s @ 20 fps (10 frames/stop)
  const multi = P.planTourFrames(tour, {
    rangeMode: "custom",
    fromTarget: 2,
    toTarget: 4,
    includeTransitions: true,
    fpsPreset: "custom",
    customFps: "20",
    durationOverride: "0.25", // intervals = 5 => 4 interior frames per transition
    holdSeconds: "0.5",
  }, { width: 1200, height: 800 });
  assert.equal(multi.valid, true);
  assert.equal(multi.stopCount, 3);
  assert.equal(multi.transitionCount, 8);
  assert.equal(multi.totalFrames, 3 * 10 + 8);
  assert.equal(multi.frames[0].stepIndex, 1);
  assert.equal(multi.frames.at(-1).stepIndex, 3);

  // Single consecutive pair Target 2 -> Target 3
  const pair = P.planTourFrames(tour, {
    rangeMode: "custom",
    fromTarget: 2,
    toTarget: 3,
    includeTransitions: true,
    fpsPreset: "24",
    durationOverride: "1",
    holdSeconds: "0",
  });
  assert.equal(pair.valid, true);
  assert.equal(pair.totalFrames, 2 + 23);

  // Invalid From > To
  const badRange = P.planTourFrames(tour, { rangeMode: "custom", fromTarget: 3, toTarget: 2 });
  assert.equal(badRange.valid, false);
  assert.ok(badRange.errors.some((e) => /From Target/.test(e)));

  // Custom range with 1 target disables transitions
  const singleRange = P.planTourFrames(tour, {
    rangeMode: "custom",
    fromTarget: 2,
    toTarget: 2,
    includeTransitions: true,
  });
  assert.equal(singleRange.valid, true);
  assert.equal(singleRange.canTransition, false);
  assert.equal(singleRange.transitionCount, 0);
  assert.equal(singleRange.totalFrames, 1);

  // Out-of-bounds FPS rejected
  const badFps = P.planTourFrames(tour, { fpsPreset: "custom", customFps: "500" });
  assert.equal(badFps.valid, false);
  const badDur = P.planTourFrames(tour, { includeTransitions: true, durationOverride: "-1" });
  assert.equal(badDur.valid, false);
});

test("planTourFrames: degenerate cases (0 targets, 1 target, identical consecutive coords, tiny duration)", () => {
  const empty = createTour("Empty");
  const pEmpty = P.planTourFrames(empty);
  assert.equal(pEmpty.valid, false);
  assert.equal(pEmpty.totalFrames, 0);

  const single = makeSampleTour(1);
  const pSingle = P.planTourFrames(single, { includeTransitions: true });
  assert.equal(pSingle.valid, true);
  assert.equal(pSingle.canTransition, false);
  assert.equal(pSingle.totalFrames, 1);

  // Identical consecutive targets -> no NaN or divide-by-zero
  const dupTour = createTour("Same Coords");
  dupTour.steps.push(
    { id: "s1", title: "A", caption: "", nodeIds: [], edgeIds: [], viewport: { x: 100, y: 50, zoom: 1.25 } },
    { id: "s2", title: "B", caption: "", nodeIds: [], edgeIds: [], viewport: { x: 100, y: 50, zoom: 1.25 } },
  );
  const pDup = P.planTourFrames(dupTour, { includeTransitions: true, fpsPreset: "30", durationOverride: "0.2" });
  assert.equal(pDup.valid, true);
  for (const f of pDup.frames) {
    assert.ok(Number.isFinite(f.camera.cx) && Number.isFinite(f.camera.cy) && Number.isFinite(f.camera.zoom));
    assert.equal(f.camera.zoom, 1.25);
  }

  // Very short duration where Math.round(duration * fps) <= 1 -> 0 interior frames, no errors
  const pTiny = P.planTourFrames(dupTour, { includeTransitions: true, fpsPreset: "24", durationOverride: "0.01" });
  assert.equal(pTiny.valid, true);
  assert.equal(pTiny.transitionCount, 0);
  assert.equal(pTiny.totalFrames, 2);
});

// ------------------------------------------------------------------ 2. Filename Formatter & Sanitizer

test("filename formatter & sanitizer: default padding, auto-widening, illegal char stripping & preview", () => {
  assert.equal(P.formatFrameFilename(1, 120), "frame_00001.png");
  assert.equal(P.formatFrameFilename(120, 120), "frame_00120.png");
  assert.equal(P.formatFrameFilename(42, 120000), "frame_000042.png", "auto-widens beyond 5 digits");
  assert.equal(P.formatFrameFilename(7, 10, { prefix: "tour_", suffix: "_rgba" }), "tour_00007_rgba.png");

  // Strips /, \, null bytes, control chars, OS-illegal <>:"|?*
  assert.equal(
    P.formatFrameFilename(3, 10, { prefix: '../bad\\<a:b>"c|d?*\x00\x1f_', suffix: "/end\\*" }),
    "..badabcd_00003end.png".replace(/^\.+/, ""),
  );
  assert.equal(P.sanitizeFilenameAffix(".."), "");
  assert.equal(P.sanitizeFilenameAffix("."), "");
  assert.equal(P.sanitizeFilenameAffix("..."), "");
  assert.equal(P.validateFilenameConfig("..", "").valid, false);
  assert.equal(P.validateFilenameConfig("", ".").valid, false);

  const prev = P.filenamePreview(120, { prefix: "frame_", suffix: "" });
  assert.deepEqual(prev, { first: "frame_00001.png", last: "frame_00120.png", single: false });
});

// ------------------------------------------------------------------ 3. Resolution & Uniform Framing Math

test("resolution bounds validation & uniform aspect-ratio-preserving viewBox independent of devicePixelRatio", () => {
  const prevDpr = globalThis.window.devicePixelRatio;
  globalThis.window.devicePixelRatio = 3;
  try {
    const vpRes = P.resolveExportDimensions({ resolutionPreset: "viewport" }, { width: 1000, height: 700 });
    assert.deepEqual([vpRes.width, vpRes.height, vpRes.valid], [1000, 700, true]);

    const hdRes = P.resolveExportDimensions({ resolutionPreset: "1080p" }, { width: 1000, height: 700 });
    assert.deepEqual([hdRes.width, hdRes.height, hdRes.valid], [1920, 1080, true]);

    const uhdRes = P.resolveExportDimensions({ resolutionPreset: "4k" }, { width: 1000, height: 700 });
    assert.deepEqual([uhdRes.width, uhdRes.height, uhdRes.valid], [3840, 2160, true]);

    const tooSmall = P.resolveExportDimensions({ resolutionPreset: "custom", customWidth: "8", customHeight: "100" });
    assert.equal(tooSmall.valid, false);

    const tooLarge = P.resolveExportDimensions({ resolutionPreset: "custom", customWidth: "9000", customHeight: "1080" });
    assert.equal(tooLarge.valid, false);

    const areaGuard = P.resolveExportDimensions({ resolutionPreset: "custom", customWidth: "8192", customHeight: "8192" });
    assert.equal(areaGuard.valid, false);
    assert.ok(areaGuard.errors.some((e) => /area/i.test(e)));

    const warnLarge = P.resolveExportDimensions({ resolutionPreset: "custom", customWidth: "5120", customHeight: "2880" });
    assert.equal(warnLarge.valid, true);
    assert.ok(warnLarge.warnings.length >= 1);

    // Uniform aspect ratio: exporting 1920x1080 and portrait 1080x1920 from 1000x700 viewport
    const cam = P.viewportToCamera({ x: 100, y: 50, zoom: 1.5 }, 1000, 700);
    const vbLand = P.computeExportViewBox(cam, 1920, 1080, 1000, 700);
    assert.ok(Math.abs(vbLand.w / vbLand.h - 1920 / 1080) < 1e-9);
    assert.ok(Math.abs((vbLand.x + vbLand.w / 2) - cam.cx) < 1e-9);
    assert.ok(Math.abs((vbLand.y + vbLand.h / 2) - cam.cy) < 1e-9);

    const vbPort = P.computeExportViewBox(cam, 1080, 1920, 1000, 700);
    assert.ok(Math.abs(vbPort.w / vbPort.h - 1080 / 1920) < 1e-9);
    assert.ok(Math.abs((vbPort.x + vbPort.w / 2) - cam.cx) < 1e-9);
    assert.ok(Math.abs((vbPort.y + vbPort.h / 2) - cam.cy) < 1e-9);
  } finally {
    globalThis.window.devicePixelRatio = prevDpr;
  }
});

// ------------------------------------------------------------------ 4. Visual Options, Hidden Toggle & Writer Lifecycle

test("Video / Frame Export controls are hidden behind toggle by default and render when toggled ON", () => {
  const canvas = M.emptyCanvas();
  const tour = makeSampleTour(2);
  canvas.tours = [tour];

  const closedHtml = NodeMapTourPlayer.inspectorHtml(
    canvas,
    tour.id,
    E.tourExportPanelHtml(tour, E.createTourExportState(), { width: 1000, height: 700 }),
  );
  assert.match(closedHtml, /data-insp="tour-export-toggle"/);
  assert.match(closedHtml, /aria-pressed="false"/);
  assert.doesNotMatch(closedHtml, /nm-tour-export-panel/);
  assert.doesNotMatch(closedHtml, /Export PNG Frames/);

  const openState = { ...E.createTourExportState(), open: true };
  const openHtml = NodeMapTourPlayer.inspectorHtml(
    canvas,
    tour.id,
    E.tourExportPanelHtml(tour, openState, { width: 1000, height: 700 }, { supported: false, reason: "insecure-context", notice: "Non-HTTPS notice" }),
  );
  assert.match(openHtml, /nm-tour-export-panel/);
  assert.match(openHtml, /Export PNG Frames/);
  assert.match(openHtml, /Non-HTTPS notice/);
  assert.match(openHtml, /frame_00001\.png/);
});

test("canvasToSvg: respects viewportBox framing, transparent vs opaque theme bg, and tour highlights without stale carryover", () => {
  const canvas = M.emptyCanvas();
  const n1 = M.createNode("note", { id: "n1", x: 100, y: 100, w: 200, h: 100, data: { text: "One" } });
  const n2 = M.createNode("note", { id: "n2", x: 500, y: 100, w: 200, h: 100, data: { text: "Two" } });
  const e1 = M.createEdge({ id: "e1", from: "n1", to: "n2", label: "next" });
  canvas.nodes.push(n1, n2);
  canvas.edges.push(e1);
  canvas.settings.snap = true;

  const vb = { x: -50, y: -50, w: 960, h: 540, width: 1920, height: 1080 };
  const svgTrans = X.canvasToSvg(canvas, (c) => (c.startsWith("var(") ? "#112233" : c), {
    transparent: true,
    viewportBox: vb,
    tourHighlights: { active: true, nodeIds: new Set(["n1"]), edgeIds: new Set(["e1"]) },
  });
  assert.match(svgTrans, /width="1920" height="1080" viewBox="-50 -50 960 540" preserveAspectRatio="xMidYMid meet"/);
  assert.doesNotMatch(svgTrans, /id="nmgrid"/, "transparent export suppresses grid");
  assert.doesNotMatch(svgTrans, /<rect x="-50" y="-50" width="960" height="540" fill="#112233"/, "transparent export suppresses opaque background");
  assert.match(svgTrans, /opacity="0.2"/, "unfocused node dimmed");
  assert.match(svgTrans, /stroke-width="3.5"/, "focused edge emphasized");

  const svgOpaque = X.canvasToSvg(canvas, (c) => (c.startsWith("var(") ? "#112233" : c), {
    transparent: false,
    viewportBox: vb,
    tourHighlights: { active: false, nodeIds: new Set(), edgeIds: new Set() },
  });
  assert.match(svgOpaque, /id="nmgrid"/, "opaque export keeps grid when snap is on");
  assert.match(svgOpaque, /<rect x="-50" y="-50" width="960" height="540" fill="#112233"/);
  assert.doesNotMatch(svgOpaque, /opacity="0.2"/, "inactive highlights leave no stale dimming");
});

function makeRunHarness({
  transparent = true,
  hideHud = true,
  pickerSupported = true,
  isSecureContext = true,
  pickerAbort = false,
  writeFailAt = Infinity,
  cancelAtFrame = Infinity,
} = {}) {
  const c = new NodeMapController();
  c.tab = { id: "tour-exp-1", type: "nodemap", version: "v3.0", repo: "linux" };
  c.canvas = M.emptyCanvas();
  const n1 = M.createNode("note", { id: "n1", x: 50, y: 50, w: 220, h: 120, data: { text: "A" } });
  const n2 = M.createNode("note", { id: "n2", x: 400, y: 120, w: 220, h: 120, data: { text: "B" } });
  c.canvas.nodes.push(n1, n2);
  c.canvas.viewport = { x: 42, y: 24, zoom: 1.2 };
  c.selected = new Set(["n1"]);
  c.selectedEdge = null;
  c.active = null;
  const tour = makeSampleTour(3);
  c.canvas.tours = [tour];
  c.activeTourId = tour.id;

  const rootCls = new Set();
  const layerCls = new Set();
  const wrapCls = new Set();
  const hudBar = {
    tagName: "DIV",
    hidden: false,
    classList: { contains: (k) => k === "nodemap-toolbar", add() {}, remove() {} },
    getBoundingClientRect: () => ({ left: 10, top: 10, right: 210, bottom: 40, width: 200, height: 30 }),
    children: [],
    dataset: {},
  };
  const exportUi = {
    tagName: "DIV",
    hidden: false,
    classList: { contains: (k) => k === "nm-tour-export-ui" },
    getBoundingClientRect: () => ({ left: 20, top: 20, right: 220, bottom: 120, width: 200, height: 100 }),
    children: [],
    dataset: { tourExportUi: "1" },
  };
  const tourBar = {
    tagName: "DIV",
    hidden: true,
    classList: { contains: (k) => k === "nm-tour-bar" },
    getBoundingClientRect: () => ({ left: 300, top: 620, right: 700, bottom: 680, width: 400, height: 60 }),
    children: [],
    dataset: {},
  };
  c.wrapEl = {
    tagName: "DIV",
    children: [tourBar, exportUi],
    classList: { add: (k) => wrapCls.add(k), remove: (k) => wrapCls.delete(k), toggle() {}, contains: (k) => wrapCls.has(k) },
    style: { setProperty() {} },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 700 }),
  };
  c.rootEl = {
    tagName: "DIV",
    children: [hudBar, c.wrapEl],
    classList: { add: (...ks) => ks.forEach((k) => rootCls.add(k)), remove: (...ks) => ks.forEach((k) => rootCls.delete(k)), toggle: (k, on) => (on ? rootCls.add(k) : rootCls.delete(k)), contains: (k) => rootCls.has(k) },
  };
  c.layerEl = {
    style: {},
    classList: { add: (k) => layerCls.add(k), remove: (k) => layerCls.delete(k), contains: (k) => layerCls.has(k) },
  };
  c.wireSvg = { style: {}, setAttribute() {}, innerHTML: "" };
  c.tourPlayer = new NodeMapTourPlayer(c);
  c.tourPlayer.el = tourBar;
  c.tourPlayer.counterEl = { textContent: "" };
  c.tourPlayer.titleEl = { textContent: "" };
  c.tourPlayer.captionEl = { textContent: "", hidden: true };
  c.tourPlayer.prevBtn = { disabled: false };
  c.tourPlayer.nextBtn = { disabled: false };

  c.tourExport = {
    ...E.createTourExportState(),
    open: true,
    transparent,
    hideHud,
    resolutionPreset: "1080p",
    includeTransitions: true,
    fpsPreset: "30",
    durationOverride: "0.2", // intervals = 6 => 5 interior frames * 2 + 3 stops = 13 frames
  };

  const writtenFiles = [];
  const abortedFiles = [];
  const downloads = [];
  const createdCanvases = [];
  const clearRectCalls = [];
  const hudCaptured = [];
  const revokedUrls = [];
  let pickerSyncCalled = false;

  const dirHandle = {
    async getFileHandle(name) {
      return {
        async createWritable() {
          let buf = null;
          return {
            async write(blob) {
              if (writtenFiles.length + 1 >= writeFailAt) throw new Error("disk full");
              buf = blob;
            },
            async close() {
              writtenFiles.push({ name, blob: buf });
            },
            async abort() {
              abortedFiles.push(name);
            },
          };
        },
      };
    },
  };

  const win = {
    isSecureContext,
    devicePixelRatio: 2,
    ...(pickerSupported ? {
      showDirectoryPicker() {
        pickerSyncCalled = true;
        if (pickerAbort) return Promise.reject(new DOMException("User aborted", "AbortError"));
        return Promise.resolve(dirHandle);
      },
    } : {}),
  };

  const deps = {
    win,
    createCanvas(w, h) {
      const ctx = {
        clearRect: (...a) => clearRectCalls.push(a),
        drawImage() {},
        save() {},
        restore() {},
        translate() {},
        scale() {},
      };
      const cv = { width: w, height: h, getContext: () => ctx };
      createdCanvases.push(cv);
      return cv;
    },
    async loadSvgImage(svg) {
      if (writtenFiles.length + 1 >= cancelAtFrame) E.cancelTourFrameExport(c);
      return { close() { revokedUrls.push("bitmap-closed"); }, svg };
    },
    async canvasToBlob(cv) {
      return { size: cv.width * cv.height, type: "image/png" };
    },
    async captureHudElement({ el }) {
      hudCaptured.push(el.classList?.contains?.("nm-tour-bar") ? "tour-bar" : "toolbar");
    },
    downloadFile(name, blob) {
      downloads.push({ name, blob });
      if (downloads.length >= cancelAtFrame) E.cancelTourFrameExport(c);
    },
    confirmFallback: async () => true,
    yieldFrame: async () => {},
    fontsReady: async () => {},
    staggerMs: 0,
  };

  return {
    c,
    deps,
    writtenFiles,
    abortedFiles,
    downloads,
    createdCanvases,
    clearRectCalls,
    hudCaptured,
    rootCls,
    isPickerSyncCalled: () => pickerSyncCalled,
  };
}

test("startTourFrameExport: invokes showDirectoryPicker synchronously, reuses 1 canvas, writes PNGs, and cleans up", async () => {
  const h = makeRunHarness({ transparent: true, hideHud: true, pickerSupported: true });
  const res = await E.startTourFrameExport(h.c, h.deps);
  assert.equal(h.isPickerSyncCalled(), true, "showDirectoryPicker called synchronously on click");
  assert.equal(res.cancelled, false);
  assert.equal(res.written, 13);
  assert.equal(h.createdCanvases.length, 1, "single working canvas allocated and reused");
  assert.deepEqual([h.createdCanvases[0].width, h.createdCanvases[0].height], [1920, 1080], "exact pixel dims independent of dpr=2");
  assert.equal(h.clearRectCalls.length, 13, "clears working canvas every frame");
  assert.equal(h.hudCaptured.length, 0, "hideHud=true excludes HUD overlays");
  assert.equal(h.writtenFiles[0].name, "frame_00001.png");
  assert.equal(h.writtenFiles.at(-1).name, "frame_00013.png");
  assert.deepEqual(h.c.canvas.viewport, { x: 42, y: 24, zoom: 1.2 }, "live viewport restored");
  assert.deepEqual([...h.c.selected], ["n1"], "live selection restored");
  assert.equal(h.c.tourExporting, false);
  assert.equal(h.rootCls.has("nm-tour-export-locked"), false);
  assert.equal(h.rootCls.has("nm-tour-export-noanim"), false);
});

test("startTourFrameExport: AbortError from directory picker is a clean no-op with no error banner", async () => {
  const h = makeRunHarness({ pickerAbort: true });
  const res = await E.startTourFrameExport(h.c, h.deps);
  assert.equal(res.cancelled, true);
  assert.equal(res.reason, "picker-aborted");
  assert.equal(h.c.tourExport.errorBanner, "");
  assert.equal(h.writtenFiles.length, 0);
});

test("runTourFrameExport: hideHud=false composites visible HUD & tour bar while always excluding export UI", async () => {
  const h = makeRunHarness({ transparent: false, hideHud: false });
  const res = await E.startTourFrameExport(h.c, h.deps);
  assert.equal(res.written, 13);
  assert.ok(h.hudCaptured.includes("toolbar"));
  assert.ok(h.hudCaptured.includes("tour-bar"));
  assert.equal(h.c.tourPlayer.el.hidden, true, "tour bar visibility restored after export");
});

test("runTourFrameExport: aborts open writable (.crswap cleanup) and restores state on error or mid-run cancel", async () => {
  // 1. Mid-write error aborts open writable and restores state
  const hErr = makeRunHarness({ writeFailAt: 4 });
  const resErr = await E.startTourFrameExport(hErr.c, hErr.deps);
  assert.ok(resErr.error);
  assert.equal(hErr.writtenFiles.length, 3);
  assert.deepEqual(hErr.abortedFiles, ["frame_00004.png"]);
  assert.deepEqual(hErr.c.canvas.viewport, { x: 42, y: 24, zoom: 1.2 });
  assert.equal(hErr.c.tourExporting, false);

  // 2. Mid-run cancel stops cleanly and restores state
  const hCancel = makeRunHarness({ cancelAtFrame: 5 });
  const resCancel = await E.startTourFrameExport(hCancel.c, hCancel.deps);
  assert.equal(resCancel.cancelled, true);
  assert.ok(hCancel.writtenFiles.length < 13);
  assert.deepEqual(hCancel.c.canvas.viewport, { x: 42, y: 24, zoom: 1.2 });
  assert.equal(hCancel.c.tourExporting, false);
});

test("runTourFrameExport: fallback writer notices non-HTTPS, confirms >20 frames, and stops on cancel", async () => {
  const supportInsecure = E.detectDirectoryPickerSupport({ isSecureContext: false });
  assert.equal(supportInsecure.supported, false);
  assert.equal(supportInsecure.reason, "insecure-context");
  assert.match(supportInsecure.notice, /isSecureContext|HTTPS/i);

  const supportOld = E.detectDirectoryPickerSupport({ isSecureContext: true });
  assert.equal(supportOld.supported, false);
  assert.equal(supportOld.reason, "unsupported-browser");

  // Confirmation declined above 20 frames
  const hDecline = makeRunHarness({ pickerSupported: false });
  hDecline.c.tourExport.durationOverride = "0.5"; // > 20 frames
  let confirmedCount = 0;
  const resDecline = await E.startTourFrameExport(hDecline.c, {
    ...hDecline.deps,
    confirmFallback: async (n) => {
      confirmedCount = n;
      return false;
    },
  });
  assert.ok(confirmedCount > 20);
  assert.equal(resDecline.cancelled, true);
  assert.equal(resDecline.reason, "fallback-declined");
  assert.equal(hDecline.downloads.length, 0);

  // Fallback downloads stop cleanly on cancel
  const hFb = makeRunHarness({ pickerSupported: false, cancelAtFrame: 4 });
  const resFb = await E.startTourFrameExport(hFb.c, hFb.deps);
  assert.equal(resFb.cancelled, true);
  assert.equal(hFb.downloads.length, 4);
  assert.equal(hFb.downloads[0].name, "frame_00001.png");
  assert.equal(hFb.c.tourExporting, false);
});

test("interaction lock blocks canvas gestures during active export and allows Escape to cancel", () => {
  const h = makeRunHarness();
  let aborted = false;
  h.c._tourExportRun = { id: 1, abortController: { abort: () => { aborted = true; } } };
  assert.equal(h.c.tourExporting, true);

  // Gestures blocked
  onPointerDown(h.c, { target: { closest: () => null }, button: 0, clientX: 10, clientY: 10 });
  assert.equal(h.c.gesture, null);
  let prevented = false;
  onWheel(h.c, { target: { closest: () => null }, preventDefault: () => { prevented = true; }, deltaY: 10 });
  assert.equal(prevented, true);
  assert.deepEqual(h.c.canvas.viewport, { x: 42, y: 24, zoom: 1.2 });

  // Escape cancels export
  onKeyDown(h.c, { key: "Escape", target: { tagName: "DIV", closest: () => null }, preventDefault() {} });
  assert.equal(aborted, true);
});

test("runTourFrameExport: a high-quality nested picture is rendered once and reused by every frame", async () => {
  const S = await import("../js/views/nodemap/nested_store.js");
  const Nested = await import("../js/views/nodemap/nodemap_nested.js");
  const store = { userDb: null, storage: globalThis.localStorage };
  const child = M.normalizeCanvas({ ...M.emptyCanvas(), nodes: [M.createNode("note", { id: "k1", x: 0, y: 0, data: { text: "nested picture text" } })] });
  assert.equal(await S.saveChild(store, "tourkid", child, { name: "Kid" }), true);
  const h = makeRunHarness({ transparent: true, hideHud: true, pickerSupported: true });
  h.c.canvas.nodes.push(M.createNode("nodemap", { id: "nm1", x: 700, y: 50, w: 420, h: 320, data: { canvasId: "tourkid", highQualityPreview: true } }));
  h.c.rootCanvas = h.c.canvas;
  let renders = 0;
  h.c.snapshotRenderFn = (ctrl, d) => (renders++, Nested.renderChildSnapshot(ctrl, d));
  h.c.snapshotCacheOpts = { urls: { create: () => "blob:tour-snap", revoke() {} }, defer: async () => {} };
  const svgs = [];
  const load = h.deps.loadSvgImage;
  h.deps.loadSvgImage = async (svg, ...rest) => (svgs.push(svg), load(svg, ...rest));
  const res = await E.startTourFrameExport(h.c, h.deps);
  assert.equal(res.written, 13);
  assert.equal(renders, 1, "the child canvas is rendered once for the whole sequence");
  assert.equal(svgs.length, 13);
  assert.ok(svgs.every((s) => s.includes("nested picture text") && /id="nq0-/.test(s)), "every frame embeds the cached picture with scoped ids");
  assert.ok(svgs.every((s) => !/blob:|nmsnapq7/.test(s)), "frames stay self-contained");
  Nested.disposeSnapshots(h.c);
});
