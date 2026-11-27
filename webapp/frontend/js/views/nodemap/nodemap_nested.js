/**
 * nodemap_nested.js - Controller glue of nested NodeMaps (`webapp/API_FRONTEND.md` §5.3.9).
 *
 * Server-backed editing (debounced `PUT` with `expected_revision`, conflict / 403 handling), the
 * `nested_maps` cards of the open canvas, drill-down with a breadcrumb stack, and the insert / create /
 * group-selection flows. Functions take the `NodeMapController`; pure logic lives in `nested_nodes.js`.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { showChangePreview } from "../../components/change_preview.js";
import { escapeHtml, openModal } from "../../components/modal.js";
import * as M from "./canvas_model.js";
import { crumb, nestedLabel, nestedTargetId, pickerHtml, pickerRows, pushCrumb, truncateCrumbs, writeErrorKind } from "./nested_nodes.js";

export const SERVER_SAVE_DELAY_MS = 1200;

const clientOf = (ctrl) => ctrl.api || api;

/** Hash of the saved document (the pan / zoom viewport is not content). */
export function docHash(doc) {
  try {
    return JSON.stringify({ ...doc, viewport: null });
  } catch {
    return "";
  }
}

/** Reset the nested state of a controller (constructor). */
export function initNested(ctrl) {
  ctrl.serverCanvas = null; // {id, revision, hash, readOnly, conflict}
  ctrl.nestedMaps = {};
  ctrl.nav = { root: null, stack: [] };
  ctrl._serverSaveTimer = null;
  ctrl._serverSaving = null;
}

/** Replace the `nested_maps` cards; nested nodes re-render through their body key. */
export function setNestedMaps(ctrl, maps) {
  ctrl.nestedMaps = maps && typeof maps === "object" ? maps : {};
  if (ctrl.canvas?.nodes?.some((n) => n.type === "nodemap")) ctrl.commit?.({ save: false, inspector: false, record: false });
}

/** Card of the canvas a nested node references. */
export function nestedCard(ctrl, node) {
  const id = nestedTargetId(node);
  return id ? ctrl.nestedMaps?.[id] || null : null;
}

/** Body key of a nested node: changes when its target card changes. */
export function nestedBodyKey(ctrl, node) {
  const c = nestedCard(ctrl, node);
  return JSON.stringify(c ? [c.status, c.revision, c.updated_at, c.name] : null);
}

/** Bind the controller to a server envelope (`id`, `revision`, `nested_maps`) after its document loaded. */
export function bindServerCanvas(ctrl, env) {
  ctrl.serverCanvas = { id: String(env.id), revision: Number(env.revision) || 0, hash: docHash(ctrl.rootCanvas || ctrl.canvas), readOnly: false, conflict: false };
  ctrl.nestedMaps = env.nested_maps && typeof env.nested_maps === "object" ? env.nested_maps : {};
  if (ctrl.tab) {
    ctrl.tab.canvasId = ctrl.serverCanvas.id;
    state.updateTabState?.(ctrl.tab.id, { canvasId: ctrl.serverCanvas.id });
  }
  ctrl._loadedServerCanvasId = ctrl.serverCanvas.id;
}

// ---------------------------------------------------------------- server save

export function scheduleServerSave(ctrl) {
  const sc = ctrl.serverCanvas;
  if (!sc || sc.readOnly || sc.conflict || ctrl._storeLoading) return;
  clearTimeout(ctrl._serverSaveTimer);
  ctrl._serverSaveTimer = setTimeout(() => { flushServerSave(ctrl); }, SERVER_SAVE_DELAY_MS);
  ctrl._serverSaveTimer?.unref?.();
}

/** Save the document now when it changed since the last server state. Resolves `true` when in sync. */
export async function flushServerSave(ctrl) {
  clearTimeout(ctrl._serverSaveTimer);
  ctrl._serverSaveTimer = null;
  while (ctrl._serverSaving) await ctrl._serverSaving.catch(() => { });
  const sc = ctrl.serverCanvas;
  const doc = ctrl.rootCanvas || ctrl.canvas;
  if (!sc || !doc) return true;
  if (sc.readOnly) return false;
  if (sc.conflict) return false;
  const hash = docHash(doc);
  if (hash === sc.hash) return true;
  const run = (async () => {
    try {
      const res = await clientOf(ctrl).updateNodemapCanvas(sc.id, { canvas: doc, expected_revision: sc.revision });
      if (ctrl.serverCanvas !== sc) return true;
      sc.revision = Number(res?.revision ?? sc.revision + 1);
      sc.hash = hash;
      if (res?.nested_maps) setNestedMaps(ctrl, res.nested_maps);
      return true;
    } catch (err) {
      if (ctrl.serverCanvas === sc) handleWriteError(ctrl, err, sc);
      return false;
    }
  })();
  ctrl._serverSaving = run;
  try {
    return await run;
  } finally {
    if (ctrl._serverSaving === run) ctrl._serverSaving = null;
  }
}

function handleWriteError(ctrl, err, sc) {
  const kind = writeErrorKind(err);
  if (kind === "forbidden") {
    sc.readOnly = true;
    toast.warning?.("Saving NodeMaps to the server is disabled: changes stay in this browser only");
  } else if (kind === "conflict") {
    sc.conflict = true;
    openConflictDialog(ctrl, sc);
  } else if (kind === "invalid") {
    toast.error?.(`The server rejected the NodeMap: ${err?.message || err}`);
  } else {
    toast.error?.(`Could not save the NodeMap to the server: ${err?.message || err}`);
  }
}

function openConflictDialog(ctrl, sc) {
  const modal = openModal({
    title: "NodeMap changed on the server",
    subtitle: "Someone else saved a newer revision of this NodeMap",
    width: "520px",
    footerHtml: '<button type="button" class="code-btn" data-act="overwrite">Keep mine (overwrite)</button><button type="button" class="code-btn primary" data-act="reload">Reload from server</button>',
  });
  modal.body.innerHTML = `<p>Your edits to <b>${escapeHtml(ctrl.nav?.stack?.at(-1)?.name || ctrl.tab?.title || sc.id)}</b> were not saved. Reload discards them; overwrite replaces the server version with this one.</p>`;
  modal.footer.querySelector("[data-act='reload']").onclick = () => {
    modal.close();
    sc.conflict = false;
    ctrl.loadServerCanvas(sc.id, { nav: ctrl.nav });
  };
  modal.footer.querySelector("[data-act='overwrite']").onclick = async () => {
    modal.close();
    try {
      const latest = await clientOf(ctrl).getNodemapCanvas(sc.id);
      sc.revision = Number(latest?.revision) || sc.revision;
      sc.conflict = false;
      await flushServerSave(ctrl);
    } catch (err) {
      sc.conflict = false;
      toast.error?.(`Could not overwrite: ${err?.message || err}`);
    }
  };
}

/** Flush local and server saves (before navigation / structural server calls). `false` blocks the caller. */
export async function flushAll(ctrl) {
  ctrl.flushSave?.();
  if (!ctrl.serverCanvas) return true;
  const ok = await flushServerSave(ctrl);
  if (!ok && ctrl.serverCanvas?.readOnly) return true; // local-only fallback: navigation still works
  return ok;
}

// ---------------------------------------------------------------- navigation

/** Initialise `ctrl.nav` after a server canvas loaded. `nav` is a caller-provided `{root, stack}`. */
export function applyNav(ctrl, env, nav = null) {
  const id = String(env.id);
  const saved = nav || ctrl.tab?.nestedNav || null;
  const stack = Array.isArray(saved?.stack) ? saved.stack : [];
  if (saved?.root?.canvasId && (id === saved.root.canvasId || stack.at(-1)?.canvasId === id)) {
    ctrl.nav = { root: { ...saved.root }, stack: stack.map((c) => ({ ...c })) };
  } else {
    ctrl.nav = { root: crumb(id, env.name || id), stack: [] };
  }
  if (ctrl.tab) state.updateTabState?.(ctrl.tab.id, { nestedNav: ctrl.nav });
}

/** Drill down into a nested node. */
export async function openNested(ctrl, nodeId) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "nodemap") return false;
  const target = nestedTargetId(node);
  const card = nestedCard(ctrl, node);
  if (!target) return chooseNestedTarget(ctrl, nodeId).then(() => false);
  if (!card || card.status === "missing") {
    toast.warning?.("This NodeMap no longer exists: remove the reference");
    return false;
  }
  if (!(await ensurePublished(ctrl))) return false;
  if (!(await flushAll(ctrl))) return false;
  const root = ctrl.nav?.root || crumb(ctrl.serverCanvas.id, ctrl.tab?.title || ctrl.serverCanvas.id);
  const stack = pushCrumb(ctrl.nav?.stack || [], crumb(target, nestedLabel(node, card), node.id));
  return ctrl.loadServerCanvas(target, { nav: { root, stack } });
}

/** Breadcrumb click: keep `depth` stack entries (0 = the root canvas) and load that level. */
export async function goToCrumb(ctrl, depth) {
  const nav = ctrl.nav;
  if (!nav?.root) return false;
  const stack = truncateCrumbs(nav.stack, depth);
  const target = stack.length ? stack.at(-1).canvasId : nav.root.canvasId;
  if (!(await flushAll(ctrl))) return false;
  return ctrl.loadServerCanvas(target, { nav: { root: nav.root, stack } });
}

/** Re-point a nested node (unset / broken reference) through the insert picker. */
export async function chooseNestedTarget(ctrl, nodeId) {
  const node = ctrl.byId(nodeId);
  if (!node || node.type !== "nodemap") return false;
  if (!(await ensurePublished(ctrl))) return false;
  const id = await pickNodemap(ctrl);
  if (!id) return false;
  node.data = { canvasId: id };
  // the new target is validated by the server on the next save (422 on a cycle)
  ctrl.commit();
  await flushServerSave(ctrl);
  return true;
}

// ---------------------------------------------------------------- publish

/**
 * Make sure the open canvas is a saved server canvas. A local-only canvas is first saved as a new server
 * canvas after the user confirms. Resolves `true` when `ctrl.serverCanvas` is set.
 */
export async function ensurePublished(ctrl) {
  if (ctrl.serverCanvas) return true;
  if (ctrl.subMapPath?.length) {
    toast.info?.("Leave the inline Sub-Map first");
    return false;
  }
  const name = String(ctrl.tab?.title || "NodeMap").slice(0, 120);
  const ok = await showChangePreview({
    title: "Save this NodeMap to the server?",
    summary: "Nested NodeMaps reference saved server canvases",
    intro: "This canvas is stored only in this browser. It will be saved to the server as a new NodeMap, and this tab is then bound to it: later edits are saved to the server automatically.",
    sections: [{ prefix: "", label: "New server canvas", columns: ["Name", "Nodes", "Edges"], rows: [{ name, nodes: ctrl.canvas.nodes.length, edges: ctrl.canvas.edges.length }], fields: ["nodes", "edges"] }],
    confirmLabel: "Save to server",
    width: "560px",
  });
  if (!ok) return false;
  try {
    const doc = ctrl.rootCanvas || ctrl.canvas;
    const env = await clientOf(ctrl).createNodemapCanvas({ name, version: ctrl.currentVersion || undefined, canvas: doc });
    bindServerCanvas(ctrl, env);
    ctrl.nav = { root: crumb(env.id, env.name || name), stack: [] };
    if (ctrl.tab) state.updateTabState?.(ctrl.tab.id, { nestedNav: ctrl.nav });
    ctrl.renderBreadcrumbs?.();
    setNestedMaps(ctrl, env.nested_maps);
    return true;
  } catch (err) {
    toast[writeErrorKind(err) === "forbidden" ? "warning" : "error"]?.(writeErrorKind(err) === "forbidden"
      ? "Saving NodeMaps to the server is disabled: nested NodeMaps are unavailable"
      : `Could not save the NodeMap to the server: ${err?.message || err}`);
    return false;
  }
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

/** Picker of saved server canvases (`insertable_into` the open one); resolves the chosen id or `""`. */
export function pickNodemap(ctrl) {
  return new Promise((resolve) => {
    let chosen = "";
    const currentId = ctrl.serverCanvas?.id || "";
    const modal = openModal({
      title: "Insert existing NodeMap", subtitle: "A NodeMap that contains this one cannot be inserted (cycle)", width: "560px",
      onClose: () => resolve(chosen),
    });
    modal.body.innerHTML = '<input type="search" class="nm-nested-search" placeholder="Search saved NodeMaps…" aria-label="Search saved NodeMaps"/><div class="nm-nested-list"><div class="nm-status">Loading…</div></div>';
    const list = modal.body.querySelector(".nm-nested-list");
    const input = modal.body.querySelector("input");
    let seq = 0;
    const load = async () => {
      const mine = ++seq;
      try {
        const res = await clientOf(ctrl).getNodemapCanvases({ q: input.value.trim(), limit: 100, insertableInto: currentId });
        if (mine !== seq) return;
        list.innerHTML = pickerHtml(pickerRows(res?.items, { currentId }));
      } catch (err) {
        if (mine === seq) list.innerHTML = `<div class="nm-status nm-error">${escapeHtml(err?.message || "Could not list NodeMaps")}</div>`;
      }
    };
    let timer = null;
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
    list.addEventListener("click", (e) => {
      const btn = e.target.closest?.("[data-pick]");
      if (!btn || btn.disabled) return;
      chosen = btn.dataset.pick;
      modal.close();
    });
    load();
  });
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

/** Load a parent envelope (`{canvas, revision, nested_maps}`) into the open tab, keeping the viewport. */
export function reloadFromEnvelope(ctrl, env, selectId = "") {
  const vp = { ...ctrl.canvas.viewport };
  ctrl._storeLoading = true;
  try {
    ctrl.serverCanvas = null;
    ctrl.nestedMaps = env.nested_maps || {};
    if (!ctrl.loadCanvasDocument(env.canvas, { record: false, fit: false })) return false;
    bindServerCanvas(ctrl, env);
    ctrl.canvas.viewport.x = vp.x;
    ctrl.canvas.viewport.y = vp.y;
    ctrl.canvas.viewport.zoom = vp.zoom;
    ctrl.applyTransform();
    ctrl.history?.reset?.(M.structuralSnapshot(ctrl.canvas));
    if (selectId) {
      ctrl.selected = new Set([selectId]);
      ctrl.commit({ save: false });
    }
    ctrl.renderBreadcrumbs?.();
    return true;
  } finally {
    ctrl._storeLoading = false;
  }
}

function reportStructuralError(err) {
  const kind = writeErrorKind(err);
  if (kind === "conflict") toast.warning?.("The NodeMap changed on the server: reload it and try again");
  else if (kind === "forbidden") toast.warning?.("Saving NodeMaps to the server is disabled");
  else if (kind === "invalid") toast.error?.(`Not allowed: ${err?.message || "this would create a cycle or reference a missing NodeMap"}`);
  else toast.error?.(`NodeMap request failed: ${err?.message || err}`);
}

/** Add-menu / context-menu "Insert existing NodeMap…". */
export async function insertExistingNodemap(ctrl, at = null) {
  if (!(await ensurePublished(ctrl))) return false;
  const target = await pickNodemap(ctrl);
  if (!target) return false;
  if (!(await flushAll(ctrl))) return false;
  try {
    const res = await clientOf(ctrl).insertNodemapNested(ctrl.serverCanvas.id, { target_id: target, node: nodeSpecAt(ctrl, at), expected_revision: ctrl.serverCanvas.revision });
    return reloadFromEnvelope(ctrl, res.parent, res.node_id);
  } catch (err) {
    reportStructuralError(err);
    return false;
  }
}

/** Add-menu / context-menu "New nested NodeMap…". */
export async function createNestedNodemap(ctrl, at = null) {
  if (!(await ensurePublished(ctrl))) return false;
  const name = await promptName(ctrl, { title: "New nested NodeMap", value: "New NodeMap" });
  if (!name) return false;
  if (!(await flushAll(ctrl))) return false;
  try {
    const res = await clientOf(ctrl).insertNodemapNested(ctrl.serverCanvas.id, { name, node: nodeSpecAt(ctrl, at), expected_revision: ctrl.serverCanvas.revision });
    return reloadFromEnvelope(ctrl, res.parent, res.node_id);
  } catch (err) {
    reportStructuralError(err);
    return false;
  }
}

/** "Group selection into nested NodeMap…": a confirmation preview, then `POST /group`. */
export async function groupSelectionIntoNested(ctrl) {
  const ids = [...(ctrl.selected || [])].filter((id) => ctrl.byId(id));
  if (!ids.length) {
    toast.info?.("Select the nodes to group first");
    return false;
  }
  if (!(await ensurePublished(ctrl))) return false;
  const name = await promptName(ctrl, { title: "Group selection into nested NodeMap", label: "Name of the new NodeMap", value: "Grouped NodeMap" });
  if (!name) return false;
  const selected = new Set(ids);
  const internal = ctrl.canvas.edges.filter((e) => selected.has(e.from) && selected.has(e.to)).length;
  const boundary = ctrl.canvas.edges.filter((e) => selected.has(e.from) !== selected.has(e.to)).length;
  const ok = await showChangePreview({
    title: "Move the selection into a new NodeMap?",
    summary: `${ids.length} node${ids.length === 1 ? "" : "s"}`,
    intro: "The selected nodes (and the members of selected groups) move into a new saved NodeMap. A nested node takes their place.",
    sections: [{ prefix: "", label: "Moved into the new NodeMap", columns: ["Name", "Nodes", "Internal edges"], rows: [{ name, nodes: ids.length, edges: internal }], fields: ["nodes", "edges"] }],
    notes: [`${boundary} edge${boundary === 1 ? "" : "s"} crossing the selection ${boundary === 1 ? "is" : "are"} rewired to the nested node`],
    confirmLabel: "Group",
    width: "560px",
  });
  if (!ok) return false;
  if (!(await flushAll(ctrl))) return false;
  try {
    const res = await clientOf(ctrl).groupNodemapNodes(ctrl.serverCanvas.id, { node_ids: ids, name, expected_revision: ctrl.serverCanvas.revision });
    return reloadFromEnvelope(ctrl, res.parent, res.node_id);
  } catch (err) {
    reportStructuralError(err);
    return false;
  }
}
