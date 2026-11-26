/**
 * nodemap_persistence.js - NodeMap async IndexedDB persistence, embedded-session Kconfig channel
 * synchronization, Copy to Session, tab drag-and-drop, and orphaned tab canvas pruning.
 */
import { localDB } from "../../db.js";
import { state } from "../../state.js";
import { sessionManager } from "../../session_manager.js";
import { urlSync } from "../../url_sync.js";
import { toast } from "../../components/toast.js";
import { embedSessionChannel } from "../../utils/embed_mode.js";
import { resolveColor } from "../../utils/theme.js";
import { mayCarryTab, tabDragPayload } from "../../utils/tab_drag.js";
import * as M from "./canvas_model.js";
import { nodeSpecsForTab } from "./nodemap_bridge.js";
import { PNG_DEFAULT_SCALE, pngSize, svgSize } from "./nodemap_export.js";
import { LIVE_NODE_TYPES, browserPngEnv, liveNodeRasters } from "./nodemap_png.js";
import { canvasToDot, canvasToInteractiveHtml, canvasToMermaid } from "./nodemap_export_formats.js";
import { collectReferencedTabIds, pruneOrphanedTabCanvases } from "./nodemap_library.js";

const SAVE_DELAY_MS = 300;
const PASTE_STEP = 24;
const PUSH_ACK_GRACE_MS = 400;
const MAX_IMPORT_BYTES = 50 * 1024 * 1024;
let memoryClipboard = null;

export function hasIndexedDbBackend(ctrl) {
  return Boolean(ctrl.userDb && (ctrl.userDb !== localDB || typeof indexedDB !== "undefined") && typeof ctrl.userDb.userGet === "function");
}

export async function loadFromIndexedDB(ctrl) {
  const gen = ctrl._disposeGen;
  try {
    const res = await M.loadCanvasAsync(ctrl.userDb, ctrl.storageKey, null);
    if (gen !== ctrl._disposeGen || !ctrl.containerEl?.isConnected || ctrl._seededAtMount || ctrl._userEdited) return;
    if (res?.backend === "indexeddb" && res.canvas) {
      ctrl.canvasMeta.created_at = res.created_at || ctrl.canvasMeta.created_at;
      ctrl.rootCanvas = res.canvas;
      const incoming = ctrl._incomingAtMount || [];
      ctrl._incomingAtMount = [];
      for (const n of incoming) {
        if (!ctrl.rootCanvas.nodes.some((x) => x.id === n.id)) {
          ctrl.rootCanvas.nodes.push(n);
          if (n.type !== "group") M.reassignGroups(ctrl.rootCanvas.nodes, [n]);
        }
      }
      const resolved = M.resolveSubMapAtPath(ctrl.rootCanvas, ctrl.subMapPath);
      ctrl.canvas = resolved.canvas;
      if (incoming.length) ctrl.selected = new Set(incoming.map((n) => n.id));
      ctrl.history.reset(M.structuralSnapshot(ctrl.canvas));
      syncFollowChannel(ctrl);
      ctrl.applyTransform();
      ctrl.renderBreadcrumbs();
      ctrl.commit({ save: incoming.length > 0, absorb: true });
    }
  } catch {
    // Keep localStorage/empty canvas on IDB errors
  }
}

export function scheduleSave(ctrl) {
  clearTimeout(ctrl.saveTimer);
  ctrl.saveTimer = setTimeout(() => flushSave(ctrl), SAVE_DELAY_MS);
}

export function flushSave(ctrl) {
  if (!ctrl.saveTimer || !ctrl.canvas) return;
  clearTimeout(ctrl.saveTimer);
  ctrl.saveTimer = null;
  const doc = ctrl.rootCanvas || ctrl.canvas;
  if (hasIndexedDbBackend(ctrl)) {
    M.saveCanvasAsync(
      ctrl.userDb,
      ctrl.storageKey,
      doc,
      { repo: ctrl.repo, id: ctrl.tab?.id || ctrl.storageKey, created_at: ctrl.canvasMeta.created_at },
      globalThis.localStorage,
    ).then((res) => {
      if (res?.created_at && !ctrl.canvasMeta.created_at) ctrl.canvasMeta.created_at = res.created_at;
      if (!res?.ok && !ctrl.warnedQuota) {
        ctrl.warnedQuota = true;
        toast.warning("NodeMap could not save this canvas in the browser storage (full?): export it as JSON to keep it");
      }
    }).catch(() => { });
    return;
  }
  if (!M.saveCanvas(globalThis.localStorage, ctrl.storageKey, doc) && !ctrl.warnedQuota) {
    ctrl.warnedQuota = true;
    toast.warning("NodeMap could not save this canvas in the browser storage (full?): export it as JSON to keep it");
  }
}

export function embedKey(ctrl, sessionId) {
  return `${ctrl.tab?.id || "nodemap"}:${sessionId}`;
}

export function syncFollowChannel(ctrl) {
  const id = ctrl.canvas?.settings.followSession || "";
  const key = id ? embedKey(ctrl, id) : "";
  if (key === ctrl.followKey && (ctrl.followChannel || !key)) return;
  closeFollowChannel(ctrl);
  ctrl.followKey = key;
  if (!key) return;
  let ch = null;
  try {
    ch = ctrl.channelFactory(embedSessionChannel(key));
  } catch {
    ch = null;
  }
  if (!ch) return;
  ch.unref?.();
  ctrl.followChannel = ch;
  ch.onmessage = (ev) => onEmbedMessage(ctrl, ev?.data);
  try {
    ch.postMessage({ type: "kconfig:request_engines", sessionId: `nodemap:${key}` });
  } catch {
    // closed meanwhile
  }
}

export function closeFollowChannel(ctrl) {
  try {
    ctrl.followChannel?.close?.();
  } catch {
    // already closed
  }
  const had = ctrl.embedEngines.size > 0;
  ctrl.followChannel = null;
  ctrl.followKey = "";
  ctrl.embedEngines.clear();
  if (had && ctrl.canvas) ctrl.onKconfigChanged();
}

export function onEmbedMessage(ctrl, msg) {
  if (msg?.type === "window:unloading") {
    let dropped = false;
    for (const [id, e] of ctrl.embedEngines) {
      if (msg.winId && e.winId === msg.winId) {
        ctrl.embedEngines.delete(id);
        dropped = true;
      }
    }
    if (dropped) ctrl.onKconfigChanged();
    return;
  }
  if (!msg?.type || !msg.tabId) return;
  const resolvedOf = (a) => Math.floor(Object.keys(a || {}).length / 2) >= 1;
  if (msg.type === "kconfig:engine:announced") {
    ctrl.embedEngines.set(msg.tabId, {
      tabId: msg.tabId, winId: msg.winId || "", title: msg.title || `KConfig (${msg.arch || "?"})`, version: msg.version || "",
      arch: msg.arch || "", configName: msg.configName || "", assignments: msg.assignments || {}, resolved: resolvedOf(msg.assignments), isEmbedded: true,
    });
  } else if (msg.type === "kconfig:assignments:broadcast") {
    const eng = ctrl.embedEngines.get(msg.tabId);
    if (!eng) {
      try {
        ctrl.followChannel?.postMessage({ type: "kconfig:request_engines", sessionId: `nodemap:${ctrl.followKey}` });
      } catch {
        // closed
      }
      return;
    }
    eng.assignments = msg.assignments || {};
    eng.resolved = resolvedOf(eng.assignments);
    for (const k of ["title", "version", "arch"]) if (msg[k]) eng[k] = msg[k];
    if (typeof msg.configName === "string") eng.configName = msg.configName;
  } else if (msg.type === "kconfig:engine:revoked") {
    const eng = ctrl.embedEngines.get(msg.tabId);
    if (!eng || (msg.winId && eng.winId && eng.winId !== msg.winId)) return;
    ctrl.embedEngines.delete(msg.tabId);
  } else return;
  ctrl.onKconfigChanged();
}

export function copyTargets(ctrl) {
  let list = [];
  try {
    list = (ctrl.sessions || sessionManager).getSessionsIndex() || [];
  } catch {
    list = [];
  }
  const current = state.currentSessionId || sessionManager.activeSessionId || "";
  return (Array.isArray(list) ? list : []).filter((x) => x && x.id && x.id !== current);
}

export function copyNodeToSession(ctrl, id, sessionId) {
  const node = ctrl.byId(id);
  const target = copyTargets(ctrl).find((x) => x.id === sessionId);
  if (!node || !target) return false;
  const tab = ctrl.tabOfNode(node);
  if (!tab) {
    toast.warning("This node has no tab to copy");
    return false;
  }
  const sm = ctrl.sessions || sessionManager;
  const tabData = { ...tab, id: `tab-${Math.random().toString(36).slice(2, 9)}` };
  const payload = {
    tabId: tabData.id,
    sourcePaneId: null,
    sourceWinId: sm.getWindowId?.() || "",
    sourceSessionId: state.currentSessionId || "",
    sourceSessionName: state.currentSessionName || "",
    tabData,
  };
  const name = target.name || target.id;
  let taken = false;
  const off = sm.subscribe?.("tab:transfer:accepted", (msg) => {
    if (msg?.tabId === tabData.id) taken = true;
  });
  sm.broadcastTabPush?.(payload, target.id);
  setTimeout(() => {
    if (typeof off === "function") off();
    if (taken) return toast.success(`Copied '${M.displayTitle(node)}' to session '${name}'`);
    const stored = sm.pushTabToStoredSession?.(target.id, payload);
    if (stored) toast.success(`Copied '${M.displayTitle(node)}' to session '${name}' (opens there with it)`);
    else toast.error(`'${M.displayTitle(node)}' could not be copied to session '${name}'`);
  }, PUSH_ACK_GRACE_MS);
  return true;
}

export function acceptsTabDrag(e) {
  return mayCarryTab(e?.dataTransfer, sessionManager.getIncomingTabDrag?.() || null);
}

export function dropTab(ctrl, payload, clientX, clientY, { live = false } = {}) {
  const tab = payload?.tabData;
  if (!tab) return [];
  const specs = nodeSpecsForTab(tab, (t) => urlSync.paramsForTab(t).toString(), { live });
  if (!specs.length) {
    toast.info(tab.type === "nodemap" ? "A NodeMap canvas cannot hold another canvas" : "This tab cannot be shown on the canvas");
    return [];
  }
  const p = ctrl.toCanvas(clientX, clientY);
  const added = [];
  specs.forEach((spec, i) => {
    const node = ctrl.nodeFromSpec(spec);
    if (!node) return;
    node.x = Math.round(p.x - node.w / 2 + i * PASTE_STEP);
    node.y = Math.round(p.y - node.h / 2 + i * PASTE_STEP);
    if (ctrl.canvas.settings.snap) {
      node.x = M.snapValue(node.x, ctrl.canvas.settings.grid);
      node.y = M.snapValue(node.y, ctrl.canvas.settings.grid);
    }
    ctrl.canvas.nodes.push(node);
    added.push(node);
  });
  if (!added.length) return [];
  M.reassignGroups(ctrl.canvas.nodes, added.filter((n) => n.type !== "group"));
  ctrl.selected = new Set(added.map((n) => n.id));
  ctrl.selectedEdge = null;
  ctrl.commit();
  if (payload.sourceWinId) sessionManager.broadcastTabDropCopied?.({ tabId: payload.tabId || tab.id, sourceWinId: payload.sourceWinId });
  toast.success(added[0].type === "tab" ? `Embedded '${tab.title || tab.type}' as a live tab` : `Copied '${tab.title || tab.type}' onto the canvas`);
  return added;
}

export function attachTabDrop(ctrl) {
  const wrap = ctrl.wrapEl;
  const off = () => wrap.classList.remove("nm-drop-target");
  wrap.addEventListener("dragover", (e) => {
    if (!acceptsTabDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    wrap.classList.add("nm-drop-target");
  });
  wrap.addEventListener("dragleave", (e) => {
    if (!wrap.contains(e.relatedTarget)) off();
  });
  wrap.addEventListener("drop", (e) => {
    off();
    const payload = tabDragPayload(e.dataTransfer, sessionManager.getIncomingTabDrag?.() || null);
    if (!payload) return;
    e.preventDefault();
    e.stopPropagation();
    dropTab(ctrl, payload, e.clientX, e.clientY, { live: e.shiftKey });
  });
}

export async function pruneOrphanedCanvases(ctrl, { maxAgeMs, now } = {}) {
  const sm = ctrl.sessions || sessionManager;
  const sessionsIndex = sm.getSessionsIndex?.() || [];
  const activeTabIds = collectReferencedTabIds({
    openPanes: state.panes || [],
    sessionsIndex,
    readSession: (sid) => sm.readSession?.(sid),
    storage: globalThis.localStorage,
  });
  if (ctrl.tab?.id) activeTabIds.add(ctrl.tab.id);
  return pruneOrphanedTabCanvases(ctrl.userDb, {
    activeTabIds,
    maxAgeMs,
    now,
    fallbackStorage: globalThis.localStorage,
  });
}

// ------------------------------------------------------------------ clipboard

export function copySelection(ctrl) {
  const ids = [...ctrl.selected];
  if (!ids.length) return null;
  const payload = M.clipboardPayload(ctrl.canvas, ids, ctrl.tab?.id || "");
  memoryClipboard = payload;
  try {
    globalThis.localStorage.setItem(M.CLIPBOARD_KEY, JSON.stringify(payload));
  } catch {
    try {
      globalThis.localStorage?.removeItem(M.CLIPBOARD_KEY);
    } catch {
      // nothing stored
    }
  }
  ctrl.pasteStamp = payload.stamp;
  ctrl.pasteCount = 0;
  toast.info(`Copied ${payload.nodes.length} node${payload.nodes.length === 1 ? "" : "s"} (Ctrl+V pastes them in any NodeMap tab)`);
  return payload;
}

export function readClipboard() {
  let raw = null;
  try {
    const stored = globalThis.localStorage?.getItem(M.CLIPBOARD_KEY);
    raw = stored ? JSON.parse(stored) : null;
  } catch {
    raw = null;
  }
  return M.normalizeClipboard(raw) || M.normalizeClipboard(memoryClipboard);
}

export function pasteClipboard(ctrl) {
  const clip = ctrl.readClipboard();
  if (!clip) return false;
  if (clip.stamp !== ctrl.pasteStamp) {
    ctrl.pasteStamp = clip.stamp;
    ctrl.pasteCount = 0;
  }
  ctrl.pasteCount += 1;
  const step = PASTE_STEP * ctrl.pasteCount;
  let offset = { x: step, y: step };
  if (clip.origin !== (ctrl.tab?.id || "")) {
    const b = M.boundsOf(clip.nodes);
    const c = ctrl.viewCenter();
    offset = { x: Math.round(c.x - (b.x + b.w / 2)) + step - PASTE_STEP, y: Math.round(c.y - (b.y + b.h / 2)) + step - PASTE_STEP };
  }
  const clones = M.cloneSubgraph({ nodes: clip.nodes, edges: clip.edges }, clip.nodes.map((n) => n.id), offset);
  ctrl.insertClones(clones);
  return true;
}

// ------------------------------------------------------------------ file export & import

export function download(name, blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

export function exportJSON(ctrl) {
  ctrl.download("nodemap_canvas.json", new Blob([JSON.stringify(M.serializeCanvas(ctrl.canvas), null, 2)], { type: "application/json" }));
  toast.success("Exported the canvas as JSON");
}

export function exportSVG(ctrl, { transparent = false } = {}) {
  const svg = ctrl.canvasSvg({ transparent });
  ctrl.download("nodemap_canvas.svg", new Blob([svg], { type: "image/svg+xml" }));
  toast.success("Exported SVG image");
  return svg;
}

export function exportInteractiveHTML(ctrl, { title = "KernelInfo NodeMap", transparent = false } = {}) {
  const svg = ctrl.canvasSvg({ transparent, foreign: true });
  const html = canvasToInteractiveHtml(ctrl.canvas, svg, {
    title,
    transparent,
    resolve: (c) => resolveColor(c),
  });
  ctrl.download("nodemap_canvas.html", new Blob([html], { type: "text/html;charset=utf-8" }));
  toast.success("Exported interactive HTML canvas");
  return html;
}

export function exportMermaid(ctrl, { direction = "LR" } = {}) {
  const text = canvasToMermaid(ctrl.canvas, { direction });
  ctrl.download("nodemap_canvas.mmd", new Blob([text], { type: "text/plain;charset=utf-8" }));
  toast.success("Exported Mermaid topology (.mmd)");
  return text;
}

export function exportDot(ctrl, { direction = "LR" } = {}) {
  const text = canvasToDot(ctrl.canvas, { direction });
  ctrl.download("nodemap_canvas.dot", new Blob([text], { type: "text/vnd.graphviz;charset=utf-8" }));
  toast.success("Exported Graphviz topology (.dot)");
  return text;
}

/**
 * PNG of the whole canvas (async; resolves `{ w, h, scale, clamped, report }` once downloaded, null on
 * failure). The SVG is rendered with its `width`/`height` at the output size (`pixelScale`), so vectors are
 * rasterized directly at the target pixels, never a 1× bitmap upscaled. Live DOM nodes (`tab`, `man`,
 * `doc`) are prepared and rasterized one by one first (`nodemap_png.js`: tab iframes loaded and read at 1×,
 * a taint-free DOM snapshot per node, else a 2D paint of its DOM, else a placeholder card) and embedded at
 * the same density, so one node the browser refuses never degrades the others. A browser canvas smaller
 * than `pngSize()`'s limits steps the factor down (halving, with a note naming the size); a canvas image
 * the browser still refuses is rendered again with placeholder cards and a warning.
 */
export async function exportPNG(ctrl, { transparent = false, scale = PNG_DEFAULT_SCALE, env = null } = {}) {
  const { w, h } = svgSize(ctrl.canvasSvg({ transparent, foreign: false }));
  const first = pngSize(w, h, scale);
  let rasters = null;
  let report = null;
  const live = (ctrl.canvas?.nodes || []).filter((n) => LIVE_NODE_TYPES.includes(n.type));
  if (live.length && ctrl.renderer) {
    const tabs = live.filter((n) => n.type === "tab").length;
    if (tabs) toast.info(`Preparing the PNG: loading ${tabs} live tab${tabs === 1 ? "" : "s"}…`);
    // while frames are read at 1×, zoom changes and frame loads leave their supersampling alone
    ctrl._exportingFrames = true;
    let prep = null;
    try {
      prep = typeof ctrl.prepareExportFrames === "function" ? await ctrl.prepareExportFrames() : null;
      ({ rasters, report } = await liveNodeRasters(ctrl, first.scale, env || browserPngEnv()));
    } finally {
      prep?.restore?.();
      ctrl._exportingFrames = false;
    }
    report.timedOut = prep?.timedOut || [];
  }
  const notes = [];
  if (report?.painted.length) notes.push(`${report.painted.length} live node${report.painted.length === 1 ? " was" : "s were"} drawn from ${report.painted.length === 1 ? "its" : "their"} page text (this browser refused the snapshot)`);
  if (report?.placeholder.length) notes.push(`${report.placeholder.length} live node${report.placeholder.length === 1 ? "" : "s"} could not be read${report.timedOut.length ? " (still loading)" : ""}: placeholder${report.placeholder.length === 1 ? "" : "s"} instead`);
  if (notes.length) toast.warning(`PNG export: ${notes.join("; ")}`);
  const rastersOf = rasters && [...rasters.values()].some(Boolean) ? (n) => rasters.get(n.id) || null : null;

  const done = (blob, size) => {
    ctrl.download("nodemap_canvas.png", blob);
    const note = size.scale < Number(scale) - 1e-9 ? ` at ${Math.round(size.scale * 100) / 100}× (reduced to the browser's canvas limit)` : "";
    toast.success(`Exported PNG image (${size.w} × ${size.h} px${note})`);
    return { ...size, report };
  };
  // `withLive`: the per-node rasters embedded; a refusal of the whole image retries without them
  const rasterize = (withLive, size) => new Promise((resolve) => {
    const svg = ctrl.canvasSvg({ transparent, foreign: false, pixelScale: size.scale, rasters: withLive ? rastersOf : null });
    const smaller = () => {
      const next = pngSize(w, h, size.scale / 2);
      if (size.scale <= 1 || next.w * next.h >= size.w * size.h) return false;
      resolve(rasterize(withLive, next));
      return true;
    };
    const drawMainThread = () => {
      const img = new Image();
      const url = typeof URL?.createObjectURL === "function" ? URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" })) : "";
      const release = () => url && URL.revokeObjectURL(url);
      const fallback = (err) => {
        release();
        if (withLive && svg.includes("<image ")) {
          toast.warning("This browser cannot rasterize the live man page / doc / tab contents: the PNG shows placeholders for them");
          resolve(rasterize(false, size));
        } else {
          toast.error(`PNG export failed${err?.message ? `: ${err.message}` : ""}`);
          resolve(null);
        }
      };
      img.onload = () => {
        release();
        try {
          const c = document.createElement("canvas");
          c.width = size.w;
          c.height = size.h;
          const ctx = c.getContext("2d");
          if (!ctx) {
            if (!smaller()) {
              toast.error("PNG export failed: no canvas of that size");
              resolve(null);
            }
            return;
          }
          ctx.imageSmoothingEnabled = true;
          ctx.imageSmoothingQuality = "high";
          ctx.drawImage(img, 0, 0, size.w, size.h);
          c.toBlob((blob) => {
            if (blob) resolve(done(blob, size));
            else if (!smaller()) {
              toast.error("PNG export failed");
              resolve(null);
            }
          }, "image/png");
        } catch (err) {
          fallback(err);
        }
      };
      img.onerror = () => fallback(null);
      img.decoding = "sync";
      img.width = size.w;
      img.height = size.h;
      img.src = url || `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    };
    // a vector-only canvas (no embedded rasters) decodes in the worker's OffscreenCanvas
    if (!svg.includes("<image ") && !svg.includes("<foreignObject") && ctrl.workerPool?.workerSupported && typeof OffscreenCanvas !== "undefined") {
      ctrl.workerPool.rasterizePng({ svg, width: size.w, height: size.h }).then((res) => {
        if (res?.blob) resolve(done(res.blob, size));
        else drawMainThread();
      }).catch(drawMainThread);
      return;
    }
    drawMainThread();
  });
  return rasterize(Boolean(rastersOf), first);
}

export function importJSON(ctrl, file) {
  if (!file) return;
  if (file.size > MAX_IMPORT_BYTES) {
    toast.error("Import failed: file exceeds the 50 MB size limit");
    return;
  }
  const gen = ctrl._disposeGen;
  const reader = new FileReader();
  reader.onerror = () => {
    if (ctrl._disposeGen !== gen) return;
    toast.error(`Import failed: ${reader.error?.message || "could not read file"}`);
  };
  reader.onload = (e) => {
    if (ctrl._disposeGen !== gen) return;
    let parsed = null;
    try {
      parsed = JSON.parse(e.target.result);
    } catch (err) {
      toast.error(`Import failed: ${err.message}`);
      return;
    }
    const canvas = M.normalizeCanvas(parsed);
    if (!canvas) {
      toast.error(`Import failed: not a NodeMap canvas (schema "${M.CANVAS_SCHEMA}" version ${M.CANVAS_VERSION})`);
      return;
    }
    ctrl.loadCanvasDocument(canvas, { record: true, fit: false });
    toast.success(`Imported ${canvas.nodes.length} nodes and ${canvas.edges.length} connections`);
  };
  reader.readAsText(file);
}
