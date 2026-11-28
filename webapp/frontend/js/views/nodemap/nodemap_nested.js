/**
 * nodemap_nested.js - Controller glue of nested NodeMaps (`webapp/API_FRONTEND.md` §5.3.9).
 *
 * Browser-local only: child canvases live in browser storage (`nested_store.js`), the parent card is a
 * static preview built from the node's cached `summary` / `preview`, drill-down keeps a breadcrumb stack
 * (`ctrl.nav`) and the insert / create / group flows edit local documents. Server canvases are read-only
 * (`GET`) and always become a local copy. No request in this module writes to the server.
 * Functions take the `NodeMapController`; pure logic lives in `nested_nodes.js` / `nested_store.js`.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { showChangePreview } from "../../components/change_preview.js";
import { escapeHtml, openModal } from "../../components/modal.js";
import * as M from "./canvas_model.js";
import { listNamedCanvases } from "./nodemap_library.js";
import { resolveColor, resolveFont } from "../../utils/theme.js";
import { cardOf, cycleBlocked, findNodeDeep, groupIntoNested, importDocument, inlineDocument, loadChild, nestedNodesOf, nestedOptions, referenceGraph, refreshNodeCache, saveChild, storeOf, summarizeCanvas, buildStaticPreview, cachedDepth, unpackDocument } from "./nested_store.js";
import { crumb, nestedLabel, nestedTargetId, pickerHtml, pickerRows, pushCrumb, truncateCrumbs, wantsHighQuality } from "./nested_nodes.js";
import { NestedSnapshotCache, SNAPSHOT_ID_TOKEN, themeKey } from "./nested_snapshot.js";
import { canvasToSvg } from "./nodemap_export.js";

const clientOf = (ctrl) => ctrl.api || api;

/** Reset the nested state of a controller (constructor). */
export function initNested(ctrl) {
  ctrl.nestedStatus = {}; // local child id -> "ok" | "missing" (probed asynchronously)
  ctrl.nav = { root: null, stack: [] };
  ctrl._navDocs = []; // documents of the ancestor levels (index = level, 0 = the tab canvas)
  ctrl._navSubPaths = []; // inline Sub-Map path open at each ancestor level
}

/** Card of the nested node (`cardOf` of its cached data and probed status). */
export function nestedCard(ctrl, node) {
  const id = nestedTargetId(node);
  return id ? cardOf(node, ctrl.nestedStatus?.[id]) : null;
}

/** Body key of a nested node: changes when its cached card, its picture option or its picture changes. */
export function nestedBodyKey(ctrl, node) {
  const id = nestedTargetId(node);
  const hq = wantsHighQuality(node);
  const snap = hq ? nestedSnapshot(ctrl, node) : null;
  return JSON.stringify([id, ctrl.nestedStatus?.[id] || "", node.data?.summary || null, node.data?.preview || null, hq, hq ? nestedThemeKey(ctrl) : "", snap?.key || "", snap?.url || ""]);
}

// ---------------------------------------------------------------- high-quality pictures (nested_snapshot.js)

/** Theme part of the snapshot keys, cached until the theme changes (`onNestedThemeChanged`). */
export function nestedThemeKey(ctrl) {
  if (!ctrl._nestedThemeKey) {
    ctrl._nestedThemeKey = typeof ctrl.themeKeyFn === "function"
      ? String(ctrl.themeKeyFn())
      : themeKey((c) => resolveColor(c), globalThis.document?.documentElement?.dataset?.theme || "", `${resolveFont("--font-sans", "sans-serif")}|${resolveFont("--font-mono", "monospace")}`);
  }
  return ctrl._nestedThemeKey;
}

/**
 * Standalone SVG of a serialized child canvas for its snapshot: the export renderer in the live theme with
 * ids prefixed for embedding. No `nestedSnapshot` option: nested nodes inside the child stay schematic
 * cards, so a picture never renders further pictures.
 */
export function renderChildSnapshot(ctrl, doc) {
  const fonts = { sans: resolveFont("--font-sans", "sans-serif"), mono: resolveFont("--font-mono", "monospace") };
  return canvasToSvg(doc, (c) => resolveColor(c), {
    nested: (n) => cardOf(n, "ok"),
    content: (n) => ctrl.modelOf?.(n) ?? null,
    fonts,
    measure: ctrl.textMeasurer?.(fonts) || null,
    idPrefix: SNAPSHOT_ID_TOKEN,
  });
}

/**
 * Which data payloads of a child's nodes the controller has loaded (`modelOf`): part of the snapshot's
 * content hash, so a picture drawn before a payload arrived is never reused once it is there.
 */
export function snapshotContentSignature(ctrl, canvas) {
  if (typeof ctrl.modelOf !== "function") return "";
  const ready = [];
  for (const n of canvas?.nodes || []) {
    try {
      if (ctrl.modelOf(n) != null) ready.push(n.id);
    } catch {
      // not loadable: drawn as its card
    }
  }
  return ready.join(",");
}

/** A picture the cache budget evicted was on a card: re-render once (the card shows its schematic). */
function onSnapshotEvicted(ctrl) {
  if (ctrl._snapshotEvictQueued) return;
  ctrl._snapshotEvictQueued = true;
  Promise.resolve().then(() => {
    ctrl._snapshotEvictQueued = false;
    if (ctrl.nestedSnapshots) ctrl.commit?.({ save: false, inspector: false, record: false });
  });
}

/**
 * The controller's snapshot cache (created on first use; `ctrl.snapshotRenderFn` replaces the renderer);
 * `null` once the controller is disposed.
 */
export function snapshotCacheOf(ctrl) {
  if (ctrl._snapshotsDisposed) return null;
  if (!ctrl.nestedSnapshots) {
    ctrl.nestedSnapshots = new NestedSnapshotCache({
      render: (doc, info) => (ctrl.snapshotRenderFn || renderChildSnapshot)(ctrl, doc, info),
      onEvict: () => onSnapshotEvicted(ctrl),
      ...(ctrl.snapshotCacheOpts || {}),
    });
  }
  return ctrl.nestedSnapshots;
}

/** The picture a high-quality nested node shows now (cached snapshot of its child in this theme), or `null`. */
export function nestedSnapshot(ctrl, node) {
  if (!wantsHighQuality(node)) return null;
  const id = nestedTargetId(node);
  if (!id || ctrl.nestedStatus?.[id] === "missing") return null;
  return ctrl.nestedSnapshots?.latest(id, nestedThemeKey(ctrl)) || null;
}

/**
 * Render or reuse the snapshots of the high-quality nested nodes in `nodes` (default: every nested node of
 * the open document). Each distinct child is read once (from `children`, a `Map` id -> canvas already
 * loaded, else from local storage) and requested from the cache, which coalesces concurrent renders and
 * replaces the picture of a child whose content changed. The canvas re-renders when a card's picture
 * changes. Resolves the number of children whose picture changed.
 */
export async function refreshSnapshots(ctrl, nodes = null, { children = null, rerender = true } = {}) {
  const list = (nodes || nestedNodesOf(ctrl.rootCanvas || ctrl.canvas)).filter((n) => wantsHighQuality(n) && nestedTargetId(n));
  const cache = list.length ? snapshotCacheOf(ctrl) : null;
  if (!cache) return 0;
  const theme = nestedThemeKey(ctrl);
  const gen = ctrl._disposeGen;
  const store = storeOf(ctrl);
  let changed = 0;
  await Promise.all([...new Set(list.map(nestedTargetId))].map(async (id) => {
    const before = cache.latest(id, theme);
    let canvas = children?.has?.(id) ? children.get(id) : undefined;
    if (canvas === undefined) {
      try {
        canvas = (await loadChild(store, id))?.canvas || null;
      } catch {
        canvas = null;
      }
    }
    if (!canvas || gen !== ctrl._disposeGen) return;
    await cache.request({ canvasId: id, canvas, theme, extra: snapshotContentSignature(ctrl, canvas) });
    if (cache.latest(id, theme) !== before) changed += 1;
  }));
  if (changed && rerender && gen === ctrl._disposeGen) ctrl.commit?.({ save: false, inspector: false, record: false });
  return changed;
}

/**
 * Ask for the picture of one high-quality node in the background (its body rendered without one: the option
 * was just switched on, the target changed, the theme changed). Returns whether a request was queued.
 */
export function scheduleSnapshot(ctrl, node) {
  if (!wantsHighQuality(node)) return false;
  const id = nestedTargetId(node);
  if (!id || ctrl.nestedStatus?.[id] === "missing") return false;
  const theme = nestedThemeKey(ctrl);
  const cache = snapshotCacheOf(ctrl);
  // a failed render or a picture evicted by the budget is not re-requested by every re-render
  if (!cache || cache.latest(id, theme) || cache.pending(id, theme) || cache.settled(id, theme)) return false;
  const queued = (ctrl._snapshotQueued ||= new Set());
  const slot = JSON.stringify([id, theme]);
  if (queued.has(slot)) return false;
  queued.add(slot);
  Promise.resolve()
    .then(() => refreshSnapshots(ctrl, [node]))
    .catch(() => 0)
    .finally(() => queued.delete(slot));
  return true;
}

/** The theme changed: new snapshot keys, so the high-quality cards re-render (and re-request) in the new theme. */
export function onNestedThemeChanged(ctrl) {
  ctrl._nestedThemeKey = "";
  if (nestedNodesOf(ctrl.canvas).some((n) => wantsHighQuality(n) && nestedTargetId(n))) {
    ctrl.commit?.({ save: false, inspector: false, record: false });
  }
}

// the nested nodes an image export draws: the top level of the shown canvas, hidden layers excluded
const hqNodesOf = (ctrl) => (ctrl.canvas ? M.visibleCanvasSlice(ctrl.canvas).nodes : []).filter((n) => n.type === "nodemap").filter((n) => wantsHighQuality(n) && nestedTargetId(n) && ctrl.nestedStatus?.[nestedTargetId(n)] !== "missing");

/** Whether the shown canvas has a high-quality nested node with a target (an image export prepares pictures). */
export function hasHighQualityNested(ctrl) {
  return hqNodesOf(ctrl).length > 0;
}

/** Before an image export: every high-quality picture of the shown canvas is current (rendered once if missing). */
export async function prepareSnapshots(ctrl) {
  const nodes = hqNodesOf(ctrl);
  return nodes.length ? refreshSnapshots(ctrl, nodes) : 0;
}

/** Release every snapshot (object URLs revoked); the controller is going away. */
export function disposeSnapshots(ctrl) {
  ctrl.nestedSnapshots?.clear({ dispose: true });
  ctrl.nestedSnapshots = null;
  ctrl._snapshotsDisposed = true;
}

/** Canvas id of the open level (`""` = the tab canvas). */
export function currentLevelId(ctrl) {
  return ctrl.nav?.stack?.at(-1)?.canvasId || "";
}

function rootCrumb(ctrl) {
  return crumb(ctrl.tab?.id || "root", ctrl.tab?.title || "NodeMap");
}

/** Back to a plain tab canvas (no drill-down). */
export function resetNav(ctrl) {
  ctrl.nav = { root: rootCrumb(ctrl), stack: [] };
  ctrl._navDocs = [];
  ctrl._navSubPaths = [];
  ctrl.renderBreadcrumbs?.();
}

// ---------------------------------------------------------------- persistence of the open level

/** Persist the open level into its child record (drill-down level). `false` when at the tab canvas. */
export async function saveChildLevel(ctrl) {
  const id = currentLevelId(ctrl);
  if (!id) return false;
  const doc = ctrl.rootCanvas || ctrl.canvas;
  if (!doc) return false;
  const name = ctrl.nav.stack.at(-1)?.name || "Nested NodeMap";
  const ok = await saveChild(storeOf(ctrl), id, doc, { name, repo: ctrl.repo || "" });
  if (!ok && !ctrl.warnedQuota) {
    ctrl.warnedQuota = true;
    toast.warning?.("NodeMap could not save this nested canvas in the browser storage (full?): export it as JSON to keep it");
  }
  return ok;
}

/** Save the open level now (tab canvas through `flushSave`, a child through its record). */
export async function flushLevel(ctrl) {
  if (currentLevelId(ctrl)) {
    clearTimeout(ctrl.saveTimer);
    ctrl.saveTimer = null;
    return saveChildLevel(ctrl);
  }
  ctrl.flushSave?.();
  return true;
}

// ---------------------------------------------------------------- status probe

/**
 * Check the children referenced by the open canvas: a missing local record marks the card as broken, a
 * stored one refreshes the node's cached `summary` / `preview` (and a high-quality node's picture) from its
 * current content.
 */
export async function probeStatuses(ctrl) {
  const canvas = ctrl.rootCanvas || ctrl.canvas;
  const nodes = nestedNodesOf(canvas).filter((n) => nestedTargetId(n));
  if (!nodes.length) return false;
  const gen = ctrl._disposeGen;
  const store = storeOf(ctrl);
  let changed = false;
  const children = new Map();
  for (const n of nodes) {
    const id = nestedTargetId(n);
    const child = await loadChild(store, id);
    if (gen !== ctrl._disposeGen || (ctrl.rootCanvas || ctrl.canvas) !== canvas) return false;
    const status = child ? "ok" : "missing";
    if (ctrl.nestedStatus[id] !== status) {
      ctrl.nestedStatus[id] = status;
      changed = true;
    }
    if (child) children.set(id, child.canvas);
    if (child && refreshNodeCache(n, child.canvas, n.data.summary?.title || child.name)) changed = true;
  }
  if (changed) ctrl.commit?.({ save: true, inspector: false, record: false });
  // a high-quality picture follows the child's current content (an edited child gets a new one)
  const hq = nodes.filter((n) => wantsHighQuality(n) && children.has(nestedTargetId(n)));
  if (hq.length) await refreshSnapshots(ctrl, hq, { children });
  return changed;
}

// ---------------------------------------------------------------- navigation

function loadLevel(ctrl, doc, subPath = []) {
  const ok = ctrl.loadCanvasDocument(doc, { record: false, fit: true });
  if (!ok) return false;
  ctrl.history?.reset?.(M.structuralSnapshot(ctrl.canvas));
  if (subPath.length && ctrl.navigateToSubMapPath) {
    ctrl.navigateToSubMapPath(subPath);
  }
  ctrl.renderBreadcrumbs?.();
  return true;
}

/** Drill down into a nested node: the child is loaded from local storage and a crumb is pushed. */
export async function openNested(ctrl, nodeId) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "nodemap") return false;
  const target = nestedTargetId(node);
  if (!target) return chooseNestedTarget(ctrl, nodeId).then(() => false);
  const stack = ctrl.nav?.stack || [];
  if (stack.some((c) => c.canvasId === target)) {
    toast.warning?.("This NodeMap is already open above: opening it again would loop");
    return false;
  }
  await flushLevel(ctrl);
  const child = await loadChild(storeOf(ctrl), target);
  if (!child) {
    ctrl.nestedStatus[target] = "missing";
    ctrl.commit?.({ save: false, inspector: false, record: false });
    toast.warning?.("This NodeMap is not stored in this browser: choose another one or remove the reference");
    return false;
  }
  ctrl.nestedStatus[target] = "ok";
  const level = stack.length;
  const root = level && ctrl.nav?.root ? ctrl.nav.root : rootCrumb(ctrl);
  ctrl._navDocs[level] = ctrl.rootCanvas || ctrl.canvas;
  ctrl._navSubPaths[level] = [...(ctrl.subMapPath || [])];
  const label = nestedLabel(node, cardOf(node)) || child.name;
  ctrl.nav = { root, stack: pushCrumb(stack, crumb(target, label, node.id)) };
  return loadLevel(ctrl, child.canvas);
}

/** Breadcrumb click: keep `depth` stack entries (0 = the tab canvas); parents refresh their cached cards. */
export async function goToCrumb(ctrl, depth) {
  const nav = ctrl.nav;
  if (!nav?.root) return false;
  const level = nav.stack.length;
  const keep = truncateCrumbs(nav.stack, depth).length;
  if (keep >= level) return false;
  await flushLevel(ctrl);
  const store = storeOf(ctrl);
  let childDoc = ctrl.rootCanvas || ctrl.canvas;
  for (let l = level; l > keep; l--) {
    const entry = nav.stack[l - 1];
    const parentDoc = ctrl._navDocs[l - 1];
    const node = parentDoc ? findNodeDeep(parentDoc, entry.nodeId) : null;
    if (node && node.data?.canvasId === entry.canvasId) refreshNodeCache(node, childDoc, node.data.summary?.title || entry.name);
    if (l - 1 > keep && parentDoc) {
      await saveChild(store, nav.stack[l - 2].canvasId, parentDoc, { name: nav.stack[l - 2].name, repo: ctrl.repo || "" });
    }
    childDoc = parentDoc;
  }
  const target = ctrl._navDocs[keep];
  const subPath = ctrl._navSubPaths[keep] || [];
  ctrl.nav = { root: nav.root, stack: nav.stack.slice(0, keep) };
  ctrl._navDocs.length = keep;
  ctrl._navSubPaths.length = keep;
  return loadLevel(ctrl, target, subPath);
}

// ---------------------------------------------------------------- dialogs

/** Name prompt dialog; resolves the trimmed name or `null`. */
export function promptName(ctrl, { title, label = "Name", value = "", confirmLabel = "Create" }) {
  return new Promise((resolve) => {
    let result = null;
    const open = ctrl.openModalFn || openModal;
    const modal = open({
      title, width: "460px", onClose: () => resolve(result),
      footerHtml: `<button type="button" class="code-btn primary" data-act="ok">${escapeHtml(confirmLabel)}</button>`,
    });
    modal.body.innerHTML = `<form class="nm-form"><label class="nm-form-row"><span>${escapeHtml(label)}</span><input name="name" value="${escapeHtml(value)}" maxlength="120" required spellcheck="false"/></label></form>`;
    const form = modal.body.querySelector("form");
    const submit = () => {
      const v = String(form.elements.name.value || "").trim();
      if (!v) return;
      result = v;
      modal.close();
    };
    form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
    modal.footer.querySelector("[data-act='ok']").onclick = submit;
    form.elements.name.focus?.();
  });
}

/**
 * Rows of the insert picker: the local canvases (library maps and nested children, cycle-blocked rows
 * disabled) filtered by `q`, and the read-only server canvases (`GET` list; copied locally on pick).
 */
export async function pickerData(ctrl, { q = "" } = {}) {
  const store = storeOf(ctrl);
  const currentId = currentLevelId(ctrl);
  const ancestors = (ctrl.nav?.stack || []).map((c) => c.canvasId);
  const needle = String(q || "").trim().toLowerCase();
  const items = (await listNamedCanvases(store.userDb, { nested: "all" }, store.storage)).filter((r) => !needle || r.name.toLowerCase().includes(needle));
  const blocked = cycleBlocked(await referenceGraph(store), currentId, ancestors);
  const local = pickerRows(items, { currentId, blocked });
  let server = [];
  let serverError = "";
  try {
    const res = await clientOf(ctrl).getNodemapCanvases({ q: needle, limit: 100 });
    server = pickerRows(res?.items || [], {}).map((r) => ({ ...r, description: "Server (read-only: inserted as a local copy)" }));
  } catch (err) {
    serverError = String(err?.message || "Could not list server NodeMaps");
  }
  return { local, server, serverError };
}

/** Picker of local and server canvases; resolves `{kind: "local"|"server", id}` or `null`. */
export function pickNodemap(ctrl) {
  return new Promise((resolve) => {
    let chosen = null;
    const modal = (ctrl.openModalFn || openModal)({
      title: "Insert existing NodeMap", subtitle: "A NodeMap that contains this one cannot be inserted (cycle)", width: "560px",
      onClose: () => resolve(chosen),
    });
    modal.body.innerHTML = '<input type="search" class="nm-nested-search" placeholder="Search NodeMaps…" aria-label="Search NodeMaps"/><div class="nm-nested-list"><div class="nm-status">Loading…</div></div>';
    const list = modal.body.querySelector(".nm-nested-list");
    const input = modal.body.querySelector("input");
    let seq = 0;
    const load = async () => {
      const mine = ++seq;
      try {
        const data = await pickerData(ctrl, { q: input.value });
        if (mine !== seq) return;
        const serverHtml = data.serverError
          ? `<div class="nm-status nm-error">${escapeHtml(data.serverError)}</div>`
          : pickerHtml(data.server.map((r) => ({ ...r, id: `server:${r.id}` })));
        list.innerHTML = `<div class="nm-hint">This browser</div>${pickerHtml(data.local.map((r) => ({ ...r, id: `local:${r.id}` })))}<div class="nm-hint">Server (read-only copy)</div>${serverHtml}`;
      } catch (err) {
        if (mine === seq) list.innerHTML = `<div class="nm-status nm-error">${escapeHtml(err?.message || "Could not list NodeMaps")}</div>`;
      }
    };
    let timer = null;
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
    list.addEventListener("click", (e) => {
      const btn = e.target.closest?.("[data-pick]");
      if (!btn || btn.disabled) return;
      const [kind, ...rest] = String(btn.dataset.pick).split(":");
      chosen = { kind, id: rest.join(":") };
      modal.close();
    });
    load();
  });
}

/**
 * Local child behind a picked row: a local id is used as is, a server canvas is fetched (`GET`, inline),
 * unpacked into local storage and stored as a new child. `{id, name, canvas}` or `null`.
 */
export async function resolvePick(ctrl, pick) {
  if (!pick?.id) return null;
  const store = storeOf(ctrl);
  if (pick.kind !== "server") return loadChild(store, pick.id);
  try {
    const res = await clientOf(ctrl).getNodemapCanvas(pick.id);
    const canvas = await importDocument(store, res?.canvas, { repo: ctrl.repo || "" });
    if (!canvas) throw new Error("not a NodeMap canvas");
    const id = M.newId("nm");
    const name = String(res?.name || pick.id);
    if (!(await saveChild(store, id, canvas, { name, repo: ctrl.repo || "" }))) throw new Error("browser storage is full");
    if (Array.isArray(res?.warnings) && res.warnings.length) toast.warning?.(`${res.warnings.length} reference warning${res.warnings.length === 1 ? "" : "s"} in the server NodeMap: unresolved nested nodes were left unset`);
    return { id, name, canvas };
  } catch (err) {
    toast.error?.(`Could not copy the server NodeMap "${pick.id}": ${err?.message || err}`);
    return null;
  }
}

function nodeSpecAt(ctrl, at) {
  const { w, h } = M.SIZE_LIMITS.nodemap;
  const c = at || ctrl.viewCenter();
  let x = Math.round(c.x - (at ? 0 : w / 2));
  let y = Math.round(c.y - (at ? 0 : h / 2));
  if (ctrl.canvas.settings.snap) {
    x = M.snapValue(x, ctrl.canvas.settings.grid);
    y = M.snapValue(y, ctrl.canvas.settings.grid);
  }
  return { x, y, w, h };
}

function dataOfChild(id, name, canvas) {
  return { canvasId: id, summary: summarizeCanvas(canvas, name, cachedDepth(canvas)), preview: buildStaticPreview(canvas) };
}

function addNestedNode(ctrl, id, name, canvas, at) {
  const node = M.createNode("nodemap", { ...nodeSpecAt(ctrl, at), title: name, version: ctrl.currentVersion, repo: ctrl.repo, data: dataOfChild(id, name, canvas) });
  ctrl.canvas.nodes.push(node);
  M.reassignGroups(ctrl.canvas.nodes, [node]);
  ctrl.nestedStatus[id] = "ok";
  ctrl.selected = new Set([node.id]);
  ctrl.selectedEdge = null;
  ctrl.commit();
  return node;
}

/** Re-point a nested node (unset / broken reference) through the insert picker. */
export async function chooseNestedTarget(ctrl, nodeId) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "nodemap") return false;
  const child = await resolvePick(ctrl, await (ctrl.nestedPickerFn || pickNodemap)(ctrl));
  if (!child) return false;
  node.data = { ...dataOfChild(child.id, child.name, child.canvas), ...nestedOptions(node.data) };
  ctrl.nestedStatus[child.id] = "ok";
  ctrl.commit();
  return true;
}

/** Add-menu / context-menu "Insert existing NodeMap…". */
export async function insertExistingNodemap(ctrl, at = null) {
  const child = await resolvePick(ctrl, await (ctrl.nestedPickerFn || pickNodemap)(ctrl));
  if (!child) return false;
  addNestedNode(ctrl, child.id, child.name, child.canvas, at);
  return true;
}

/** Add-menu / context-menu "New nested NodeMap…": an empty child stored locally. */
export async function createNestedNodemap(ctrl, at = null) {
  const name = await (ctrl.promptNameFn || promptName)(ctrl, { title: "New nested NodeMap", value: "New NodeMap" });
  if (!name) return false;
  const canvas = M.emptyCanvas();
  const id = M.newId("nm");
  if (!(await saveChild(storeOf(ctrl), id, canvas, { name, repo: ctrl.repo || "" }))) {
    toast.error?.("Could not store the new NodeMap in this browser");
    return false;
  }
  addNestedNode(ctrl, id, name, canvas, at);
  return true;
}

/** "Group selection into nested NodeMap…": a confirmation preview, then the selection moves into a local child. */
export async function groupSelectionIntoNested(ctrl) {
  const ids = [...(ctrl.selected || [])].filter((id) => ctrl.byId(id));
  if (!ids.length) {
    toast.info?.("Select the nodes to group first");
    return false;
  }
  const name = await (ctrl.promptNameFn || promptName)(ctrl, { title: "Group selection into nested NodeMap", label: "Name of the new NodeMap", value: "Grouped NodeMap" });
  if (!name) return false;
  const selected = new Set(ids);
  for (const id of ids) if (ctrl.byId(id)?.type === "group") for (const d of M.groupDescendants(ctrl.canvas.nodes, id)) selected.add(d.id);
  const internal = ctrl.canvas.edges.filter((e) => selected.has(e.from) && selected.has(e.to)).length;
  const boundary = ctrl.canvas.edges.filter((e) => selected.has(e.from) !== selected.has(e.to)).length;
  const ok = await (ctrl.confirmFn || showChangePreview)({
    title: "Move the selection into a new NodeMap?",
    summary: `${selected.size} node${selected.size === 1 ? "" : "s"}`,
    intro: "The selected nodes (and the members of selected groups) move into a new NodeMap stored in this browser. A nested node takes their place.",
    sections: [{ prefix: "", label: "Moved into the new NodeMap", columns: ["Name", "Nodes", "Internal edges"], rows: [{ name, nodes: selected.size, edges: internal }], fields: ["nodes", "edges"] }],
    notes: [`${boundary} edge${boundary === 1 ? "" : "s"} crossing the selection ${boundary === 1 ? "is" : "are"} rewired to the nested node`],
    confirmLabel: "Group",
    width: "560px",
  });
  if (!ok) return false;
  const res = groupIntoNested(ctrl.canvas, ids, { title: name, version: ctrl.currentVersion || "" });
  if (!res) return false;
  if (!(await saveChild(storeOf(ctrl), res.childId, res.child, { name, repo: ctrl.repo || "" }))) {
    toast.error?.("Could not store the new NodeMap in this browser");
  }
  ctrl.nestedStatus[res.childId] = "ok";
  for (const id of res.movedIds) ctrl.portOffsets?.delete?.(id);
  ctrl.selected = new Set([res.node.id]);
  ctrl.selectedEdge = null;
  ctrl.commit();
  return true;
}

// ---------------------------------------------------------------- import / server canvases

/** Unpack inline children of the open document in place (a seeded `initialCanvas`). */
export async function unpackInPlace(ctrl) {
  const root = ctrl.rootCanvas;
  if (!root || !nestedNodesOf(root).some((n) => n.data?.canvas)) return false;
  await unpackDocument(storeOf(ctrl), root, { repo: ctrl.repo || "" });
  ctrl.commit?.({ save: true, record: false });
  probeStatuses(ctrl);
  return true;
}

/**
 * Load a read-only server canvas (`GET`, inline document) into the tab as a LOCAL copy: every inline
 * nested canvas is unpacked into local storage; later edits never reach the server.
 */
export async function loadServerCanvas(ctrl, canvasId, { tourId = "" } = {}) {
  const cid = String(canvasId || "").trim();
  if (!cid) return false;
  ctrl._loadedServerCanvasId = cid;
  ctrl._storeLoading = true;
  try {
    const res = await clientOf(ctrl).getNodemapCanvas(cid);
    if (ctrl._loadedServerCanvasId !== cid) return false;
    const doc = await importDocument(storeOf(ctrl), res?.canvas, { repo: ctrl.repo || "" });
    if (!doc) {
      toast.error?.(`Canvas "${cid}" is empty or invalid`);
      return false;
    }
    if (Array.isArray(res?.warnings) && res.warnings.length) toast.warning?.(`${res.warnings.length} nested reference warning${res.warnings.length === 1 ? "" : "s"} in "${cid}": unresolved nested nodes were left unset`);
    if (ctrl.tab && res?.name) {
      ctrl.tab.title = String(res.name);
      state.updateTabState?.(ctrl.tab.id, { title: ctrl.tab.title });
    }
    resetNav(ctrl);
    const ok = ctrl.loadCanvasDocument(doc, { record: false, fit: true });
    if (ok) {
      ctrl.renderBreadcrumbs?.();
      // the tab now owns a local copy: the deep link must not reload (and overwrite) it
      if (ctrl.tab?.canvasId) {
        ctrl.tab.canvasId = "";
        state.updateTabState?.(ctrl.tab.id, { canvasId: "" });
      }
    }
    const targetTour = String(tourId || ctrl.tab?.tourId || "").trim();
    if (ok && targetTour && Array.isArray(ctrl.canvas?.tours) && ctrl.canvas.tours.some((t) => t.id === targetTour)) {
      ctrl.tourPlayer?.start?.(targetTour);
    }
    return ok;
  } catch (err) {
    if (ctrl._loadedServerCanvasId === cid) ctrl._loadedServerCanvasId = "";
    toast.error?.(`Failed to load canvas "${cid}": ${err?.message || err}`);
    return false;
  } finally {
    ctrl._storeLoading = false;
  }
}

/** Self-contained JSON document of the open level (children re-inlined) and the warnings of the export. */
export async function exportDocument(ctrl) {
  const chain = (ctrl.nav?.stack || []).map((c) => c.canvasId);
  return inlineDocument(storeOf(ctrl), ctrl.canvas, { chain });
}
