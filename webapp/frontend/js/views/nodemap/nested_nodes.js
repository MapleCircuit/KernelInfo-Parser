/**
 * nested_nodes.js - Pure helpers of nested NodeMap nodes (`webapp/API_FRONTEND.md` §5.3.9).
 *
 * A `nodemap` node references a browser-local child canvas by `data.canvasId`. The reference is never
 * expanded: the node body is a static card (name, counts, SVG preview) built from the node's cached
 * `data.summary` / `data.preview` (`nested_store.js: cardOf`); with `data.highQualityPreview` the preview is
 * the child's rendered picture (`nested_snapshot.js`) as an `<img>` once it is ready. DOM-free; every
 * interpolated string is escaped.
 */
import { accentVar } from "./canvas_model.js";
import { isSnapshotUrl } from "./nested_snapshot.js";

const ID_RE = /^[\w-]{1,64}$/;

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

const num = (v, d = 0) => (Number.isFinite(Number(v)) ? Number(v) : d);

/** The referenced canvas id of a nested node (`""` when unset or malformed). */
export function nestedTargetId(node) {
  const id = String(node?.data?.canvasId || "");
  return ID_RE.test(id) ? id : "";
}

/**
 * Display state of a nested node: `unset` (no target chosen), `missing` (deleted target or no card for
 * it), `ok`. `card` is `nested_store.js: cardOf`.
 */
export function nestedState(node, card) {
  if (!nestedTargetId(node)) return "unset";
  if (!card || card.status === "missing") return "missing";
  return "ok";
}

/** `"3 nodes · 2 edges · 1 sub-map"` of a card's counts. */
export function nestedCountsText(counts) {
  const c = counts || {};
  const part = (n, one, many) => `${num(n)} ${num(n) === 1 ? one : many}`;
  return [part(c.nodes, "node", "nodes"), part(c.edges, "edge", "edges"), part(c.nested, "sub-map", "sub-maps")].join(" · ");
}

/** Static read-only SVG of a card preview (rectangles and lines in a `0 0 w h` box); `""` without one. */
export function nestedPreviewSvg(preview) {
  if (!preview || typeof preview !== "object") return "";
  const w = Math.max(1, Math.round(num(preview.w, 1000)));
  const h = Math.max(1, Math.round(num(preview.h, 1000)));
  const nodes = Array.isArray(preview.nodes) ? preview.nodes.slice(0, 400) : [];
  const edges = Array.isArray(preview.edges) ? preview.edges.slice(0, 800) : [];
  if (!nodes.length && !edges.length) return "";
  const lines = edges.map((e) => {
    const dash = e?.style === "dashed" ? ' stroke-dasharray="12 8"' : e?.style === "dotted" ? ' stroke-dasharray="3 7"' : "";
    return `<line x1="${Math.round(num(e?.x1))}" y1="${Math.round(num(e?.y1))}" x2="${Math.round(num(e?.x2))}" y2="${Math.round(num(e?.y2))}" style="stroke:${esc(accentVar(e?.color, "var(--text-secondary)"))}"${dash}/>`;
  });
  const rects = nodes.map((n) => {
    const fill = esc(accentVar(n?.color, n?.type === "note" ? "var(--accent-yellow)" : "var(--accent-blue)"));
    return `<rect x="${Math.round(num(n?.x))}" y="${Math.round(num(n?.y))}" width="${Math.max(2, Math.round(num(n?.w, 2)))}" height="${Math.max(2, Math.round(num(n?.h, 2)))}" rx="6" style="fill:${fill}"/>`;
  });
  return `<svg class="nm-nested-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false"><g class="nm-nested-edges" stroke-width="4" fill="none">${lines.join("")}</g><g class="nm-nested-rects" fill-opacity="0.55">${rects.join("")}</g></svg>`;
}

/** Whether a nested node asks for the high-quality picture of its child (opt-in, off by default). */
export function wantsHighQuality(node) {
  return node?.data?.highQualityPreview === true;
}

/** Label of a nested node: the target name when known, else the node title. */
export function nestedLabel(node, card) {
  if (card && card.status !== "missing" && card.name) return String(card.name);
  return String(node?.customTitle || node?.title || nestedTargetId(node) || "Nested NodeMap");
}

/**
 * Body HTML of a nested node (no child DOM, no editors): static preview, counts, "Open"/"Remove" actions.
 * `data-act` buttons are routed by `nodemap_gestures.js` onClick (`nested-open`, `nested-remove`,
 * `nested-choose`). `snapshot` is the node's cached high-quality picture (`nested_snapshot.js` entry) or
 * `null`: a high-quality node shows the schematic preview (marked pending) until it is ready.
 */
export function nestedBodyHtml(node, card, snapshot = null) {
  const state = nestedState(node, card);
  const target = nestedTargetId(node);
  if (state === "unset") {
    return `<div class="nm-nested nm-nested-unset"><div class="nm-nested-msg">No NodeMap chosen</div><div class="nm-nested-actions"><button type="button" class="nodemap-btn" data-act="nested-choose">Choose NodeMap…</button><button type="button" class="nodemap-btn nm-danger" data-act="nested-remove">Remove</button></div></div>`;
  }
  if (state === "missing") {
    return `<div class="nm-nested nm-nested-broken" role="alert"><div class="nm-nested-msg"><span class="nm-nested-warn" aria-hidden="true">⚠</span> Broken reference: NodeMap <code>${esc(target)}</code> is not stored in this browser</div><div class="nm-nested-actions"><button type="button" class="nodemap-btn" data-act="nested-choose">Choose another…</button><button type="button" class="nodemap-btn nm-danger" data-act="nested-remove">Remove</button></div></div>`;
  }
  const hq = wantsHighQuality(node);
  const pic = hq && snapshot && !snapshot.empty && isSnapshotUrl(snapshot.url) ? snapshot.url : "";
  const svg = pic ? `<img class="nm-nested-img" src="${esc(pic)}" alt="" draggable="false" decoding="async"/>` : nestedPreviewSvg(card.preview);
  const trunc = !pic && card.preview?.truncated ? '<span class="nm-nested-trunc" title="The preview shows a part of the map">…</span>' : "";
  const pcls = pic ? " nm-nested-hq" : hq && !snapshot ? " nm-nested-pending" : "";
  const desc = card.description ? `<div class="nm-nested-desc">${esc(card.description)}</div>` : "";
  const ver = card.version ? `<span class="nm-nested-ver">${esc(card.version)}</span>` : "";
  return `<div class="nm-nested"><div class="nm-nested-name"><span class="nm-nested-title">${esc(nestedLabel(node, card))}</span>${ver}</div>${desc}<div class="nm-nested-preview${pcls}">${svg || '<div class="nm-nested-empty">Empty NodeMap</div>'}${trunc}</div><div class="nm-nested-foot"><span class="nm-nested-counts">${esc(nestedCountsText(card.counts))}</span><button type="button" class="nodemap-btn" data-act="nested-open" title="Open this NodeMap for editing">Open</button></div></div>`;
}

/** Plain-text label lines of a nested node for exports (`[title, counts]`). */
export function nestedExportLines(node, card) {
  const state = nestedState(node, card);
  const label = nestedLabel(node, card);
  if (state === "unset") return [label, "(no NodeMap chosen)"];
  if (state === "missing") return [label, "(missing NodeMap)"];
  return [label, nestedCountsText(card.counts)];
}

// ---------------------------------------------------------------- navigation stack (breadcrumbs)

/** Stack entry of one drill-down step: the canvas entered and the nested node it was entered from. */
export function crumb(canvasId, name, nodeId = "") {
  return { canvasId: String(canvasId || ""), name: String(name || canvasId || ""), nodeId: String(nodeId || "") };
}

/**
 * The stack after entering `entry`. Entering a canvas already on the stack (a stale stack can) truncates back to it instead of growing.
 */
export function pushCrumb(stack, entry) {
  const list = Array.isArray(stack) ? stack : [];
  const at = list.findIndex((c) => c.canvasId === entry.canvasId);
  if (at >= 0) return list.slice(0, at).concat([{ ...entry }]);
  return list.concat([{ ...entry }]).slice(-64);
}

/** The stack truncated so that `depth` entries remain (`0` = none: the root is the current canvas). */
export function truncateCrumbs(stack, depth) {
  const d = Math.max(0, Math.min((stack || []).length, Math.floor(Number(depth) || 0)));
  return (stack || []).slice(0, d);
}

/**
 * Breadcrumb bar HTML: the root, every stack entry, the last one being the current canvas (not a button).
 * `data-nm-crumb` is the depth to truncate the stack to (root = 0 means "the root canvas").
 */
export function crumbsHtml(root, stack) {
  const list = Array.isArray(stack) ? stack : [];
  if (!root || !list.length) return "";
  const parts = [`<button type="button" class="nm-bc-btn" data-nm-crumb="0" title="${esc(root.canvasId)}">${esc(root.name || root.canvasId || "Root")}</button>`];
  list.forEach((c, i) => {
    const last = i === list.length - 1;
    parts.push('<span class="nm-bc-sep">/</span>');
    parts.push(last
      ? `<span class="nm-bc-current" aria-current="page">${esc(c.name || c.canvasId)}</span>`
      : `<button type="button" class="nm-bc-btn" data-nm-crumb="${i + 1}" title="${esc(c.canvasId)}">${esc(c.name || c.canvasId)}</button>`);
  });
  return parts.join("");
}

// ---------------------------------------------------------------- insert picker

/**
 * Picker rows of the local canvases: `items` are named-library summaries `{id, name, nodeCount, edgeCount,
 * nested}`; ids in `blocked` (the open canvas, its ancestors and every canvas containing them) are
 * disabled with a cycle hint.
 */
export function pickerRows(items, { currentId = "", blocked = new Set() } = {}) {
  return (Array.isArray(items) ? items : [])
    .filter((c) => c && c.id && c.id !== currentId)
    .map((c) => {
      const insertable = !blocked.has(String(c.id));
      return {
        id: String(c.id),
        name: String(c.name || c.id),
        description: c.nested ? "Nested NodeMap" : "",
        counts: `${num(c.nodeCount ?? c.node_count)} nodes · ${num(c.edgeCount ?? c.edge_count)} edges`,
        insertable,
        hint: insertable ? "" : "would create a cycle",
      };
    });
}

/** HTML of the picker list (rows are `<button data-pick>`; disabled rows carry the cycle hint). */
export function pickerHtml(rows) {
  if (!rows.length) return '<div class="nm-status">No NodeMaps found</div>';
  return rows.map((r) => `<button type="button" class="nm-nested-pick${r.insertable ? "" : " nm-disabled"}" data-pick="${esc(r.id)}"${r.insertable ? "" : ' disabled aria-disabled="true"'}><span class="nm-pick-name">${esc(r.name)}</span><span class="nm-pick-meta">${esc(r.counts)}</span>${r.description ? `<span class="nm-pick-desc">${esc(r.description)}</span>` : ""}${r.hint ? `<span class="nm-pick-hint">${esc(r.hint)}</span>` : ""}</button>`).join("");
}
