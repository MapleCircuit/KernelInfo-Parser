/**
 * nodemap_inspector.js - NodeMap Inspector rendering and input/click delegation
 * (canvas, single-node, multi-node, edge, visibility layers, embedded sessions, tours,
 * and locking/concurrency annotations).
 */
import { escapeHtml } from "../../components/modal.js";
import { resolveColor } from "../../utils/theme.js";
import * as M from "./canvas_model.js";
import { kconfigValue } from "./domain_nodes.js";
import { attachAutocomplete, fetchSuggestions, openFileExplorer, openVersionPicker, structKindOf, withPickHtml } from "./nodemap_pickers.js";
import { paholeRows, structRows } from "./struct_view.js";
import { NodeMapTourPlayer, captureTourStep, createTour, removeTourStep, reorderTourStep } from "./tour_mode.js";
import { revisionOf } from "./version_diff.js";

/** A bulk "Expand … to Nodes" that adds more nodes than this asks first (`nodemap_gestures.js`). */
export const BULK_CONFIRM_ABOVE = 20;

/** `#rrggbb` a color input starts at: the custom color, else what the accent resolves to. */
export function pickerValue(color, fallback = "var(--accent-blue)") {
  if (M.isCustomColor(color)) return color.toLowerCase();
  let hex = null;
  try {
    hex = M.colorToHex(resolveColor(M.accentVar(color, fallback)));
    if (!hex && typeof document !== "undefined") {
      const ctx = document.createElement("canvas").getContext?.("2d");
      if (ctx) {
        ctx.fillStyle = resolveColor(M.accentVar(color, fallback));
        hex = M.colorToHex(ctx.fillStyle);
      }
    }
  } catch {
    hex = null;
  }
  return hex || `#${"0".repeat(6)}`;
}

export function renderInspector(ctrl) {
  if (!ctrl.inspectorEl) return;
  const swatches = (current, fallback = "var(--accent-blue)", { act = "color", bg = null } = {}) => {
    const custom = M.isCustomColor(current);
    const label = bg ? "Background" : "Accent";
    return `<div class="nm-swatches" role="group" aria-label="${label}">${M.ACCENTS.map((a) =>
      `<button type="button" class="nm-swatch${a.id === current ? " on" : ""}" data-insp="${act}" data-value="${a.id}" title="${a.id || !bg ? a.label : "Theme default"}" style="--nm-sw:${bg ? M.bgVar(a.id, bg) : M.accentVar(a.id, "var(--bg-tertiary)")}"></button>`).join("")}` +
      `<label class="nm-swatch nm-swatch-custom${custom ? " on" : ""}" title="Custom color…" style="--nm-sw:${custom ? escapeHtml(current) : "var(--bg-tertiary)"}"><input type="color" data-insp="${act}-custom" value="${escapeHtml(pickerValue(current, fallback))}" aria-label="Custom ${label.toLowerCase()} color"/></label></div>`;
  };
  const bgRow = (label, current, base, act) => `<div class="inspector-field"><span class="inspector-label">${escapeHtml(label)}</span><div class="nm-insp-inline">${swatches(current, base, { act, bg: base })}${current ? `<button type="button" class="nodemap-btn" data-insp="${act}-reset" title="Back to the theme background">Reset</button>` : ""}</div></div>`;
  const sel = (name, value, options, labels = null, extra = "") => `<select data-insp="${name}" ${extra}>${options.map((o, i) => `<option value="${escapeHtml(o)}"${o === value ? " selected" : ""}>${escapeHtml(labels ? labels[i] : (o || "any"))}</option>`).join("")}</select>`;
  const row = (label, inner) => `<label class="inspector-field"><span class="inspector-label">${escapeHtml(label)}</span>${inner}</label>`;
  const input = (name, value, extra = "") => `<input data-insp="${name}" value="${escapeHtml(value ?? "")}" ${extra}/>`;
  // Revision / file inputs with their picker button (`nodemap_pickers.js`); `data-ac` inputs autocomplete
  const verInput = (name, value, extra = "") => withPickHtml(input(name, value, extra), "version", name, { insp: true });
  const fileInput = (name, value, extra = "") => withPickHtml(input(name, value, extra), "file", name, { insp: true });
  const check = (name, on, label, title = "") => `<label class="inspector-field nm-insp-check"${title ? ` title="${escapeHtml(title)}"` : ""}><input type="checkbox" data-insp="${name}" ${on ? "checked" : ""}/> <span>${escapeHtml(label)}</span></label>`;
  // Checkbox over several nodes: checked when all are on, indeterminate (set after render) when mixed
  const triCheck = (name, list, on, label, title = "") => {
    const n = list.filter(on).length;
    const state = n === 0 ? "off" : n === list.length ? "on" : "mixed";
    return `<label class="inspector-field nm-insp-check"${title ? ` title="${escapeHtml(title)}"` : ""}><input type="checkbox" data-insp="${name}" data-tri="${state}" ${state === "on" ? "checked" : ""} aria-checked="${state === "mixed" ? "mixed" : state === "on"}"/> <span>${escapeHtml(`${label} (${list.length})`)}</span></label>`;
  };
  const layoutRow = (what) => `<div class="inspector-field"><span class="inspector-label">Auto-Layout${what ? ` · ${escapeHtml(what)}` : ""}</span><div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="layout-dag">DAG (Sugiyama)</button><button type="button" class="nodemap-btn" data-insp="layout-force">Force-Directed</button></div></div>`;
  const timeTravel = (scoped) => {
    const t = ctrl.versionDiffTarget;
    const label = scoped ? `Version Time-Travel (${nodes.length} selected)` : "Version Time-Travel";
    const note = t && ctrl.versionDiffScope ? `<span class="inspector-val nm-hint">${escapeHtml(`comparing ${ctrl.versionDiffScope.size} node${ctrl.versionDiffScope.size === 1 ? "" : "s"} with ${t}`)}</span>` : "";
    return `<div class="inspector-field"><span class="inspector-label">${escapeHtml(label)}</span><div class="nm-insp-inline">${verInput("ver-target", t, 'placeholder="v6.6" spellcheck="false" aria-label="Compare revision"')}<button type="button" class="nodemap-btn" data-insp="ver-compare">Compare</button>${t ? '<button type="button" class="nodemap-btn" data-insp="ver-clear">Clear</button>' : ""}</div>${note}
      <div class="nm-insp-actions">${t ? `<button type="button" class="nodemap-btn" data-insp="ver-apply" title="${scoped ? "Move the selected nodes with a revision to it (one undo step)" : "Move every node with a revision to it"}">Apply Revision</button>` : ""}${ctrl.versionRevertMap ? '<button type="button" class="nodemap-btn" data-insp="ver-revert" title="Back to the revisions the last Apply changed">↶ Revert Versions</button>' : ""}</div></div>`;
  };
  const st = ctrl.canvas.settings;
  const em = Boolean(st.editMode);
  const scaleRow = (list) => {
    const sc = new Set(list.map((x) => x.scale || 1));
    const cur = sc.size === 1 ? [...sc][0] : 1;
    const pct = Math.round(cur * 100);
    const one = list.length === 1 ? list[0] : null;
    const note = one?.autoFit ? ` of ${one.baseW}×${one.baseH}` : sc.size > 1 ? " (mixed)" : "";
    return `<div class="inspector-field"><span class="inspector-label">Content scale${list.length > 1 ? ` (${list.length})` : ""}</span><div class="nm-insp-inline nm-insp-scale">` +
      `<input type="range" data-insp="content-scale" min="${M.SCALE_MIN * 100}" max="${M.SCALE_MAX * 100}" step="5" value="${pct}" aria-label="Content scale"/>` +
      `<input type="number" data-insp="content-scale" min="${M.SCALE_MIN * 100}" max="${M.SCALE_MAX * 100}" step="5" value="${pct}" aria-label="Content scale in percent"/><span class="inspector-val">%${escapeHtml(note)}</span>` +
      `${sc.size > 1 || cur !== 1 ? '<button type="button" class="nodemap-btn" data-insp="scale-reset" title="Back to 100%">100%</button>' : ""}</div></div>`;
  };
  let html = `<div class="nm-insp-mode"><button type="button" class="nodemap-btn nm-mode-toggle" data-insp="edit-mode" aria-pressed="${em}" title="${em ? "Hide the node editing controls" : "Show the node editing controls: struct type, #ifdef and hide toggles, inline title and note editing"}">✎ Edit Mode: ${em ? "On" : "Off"}</button></div>`;
  const edge = ctrl.selectedEdge ? ctrl.canvas.edges.find((e) => e.id === ctrl.selectedEdge) : null;
  const nodes = ctrl.selectedNodes();
  if (edge) {
    const a = ctrl.byId(edge.from);
    const b = ctrl.byId(edge.to);
    html += `<div class="inspector-field"><span class="inspector-label">Connection</span><span class="inspector-val">${escapeHtml(a ? M.displayTitle(a) : "?")} → ${escapeHtml(b ? M.displayTitle(b) : "?")}</span></div>`;
    html += row("Semantic preset", sel("edge-preset", "", M.EDGE_PRESETS.map((p) => p.id), M.EDGE_PRESETS.map((p) => p.label)));
    html += row("Lock / concurrency", sel("edge-lockKind", edge.lockKind || "", ["", ...M.LOCK_KINDS], ["(none)", ...M.LOCK_KINDS]));
    html += row("Label", input("edge-label", edge.label, 'placeholder="(none)"'));
    html += `<div class="nm-insp-pair">${row("From side", sel("edge-fromSide", edge.fromSide, M.SIDES))}${row("To side", sel("edge-toSide", edge.toSide, M.SIDES))}</div>`;
    html += `<div class="nm-insp-pair">${row("Arrows", sel("edge-arrow", edge.arrow, M.ARROWS))}${row("Line", sel("edge-style", edge.style, M.LINE_STYLES))}</div>`;
    html += row("Route", sel("edge-curve", edge.curve, M.CURVES));
    if (st.layers?.length) {
      html += row("Layer", sel("edge-layer", edge.layerId || "", ["", ...st.layers.map((l) => l.id)], ["(Always visible)", ...st.layers.map((l) => l.name)]));
    }
    if (edge.waypoints?.length) {
      html += `<div class="inspector-field"><span class="inspector-label">Waypoints (${edge.waypoints.length})</span><button type="button" class="nodemap-btn" data-insp="edge-clear-wp">× Clear Waypoints</button></div>`;
    }
    if (edge.callSite?.path) {
      html += `<div class="inspector-field"><span class="inspector-label">Call site</span><div class="nm-insp-inline"><span class="inspector-val">${escapeHtml(`${edge.callSite.path}:${edge.callSite.line || 1}`)}</span><button type="button" class="nodemap-btn" data-insp="edge-callsite">↗ Open</button></div></div>`;
    }
    html += `<div class="inspector-field"><span class="inspector-label">Color</span>${swatches(edge.color)}</div>`;
    html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="edge-reverse">⇄ Reverse</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
  } else if (nodes.length > 1) {
    html += `<div class="inspector-field"><span class="inspector-label">Selection</span><span class="inspector-val">${nodes.length} nodes</span></div>`;
    html += `<div class="inspector-field"><span class="inspector-label">Align</span><div class="nm-insp-grid">${M.ALIGN_MODES.map((m) => `<button type="button" class="nodemap-btn" data-insp="align" data-value="${m}">${m}</button>`).join("")}</div></div>`;
    html += `<div class="inspector-field"><span class="inspector-label">Distribute</span><div class="nm-insp-grid"><button type="button" class="nodemap-btn" data-insp="distribute" data-value="horizontal" ${nodes.length < 3 ? "disabled" : ""}>horizontally</button><button type="button" class="nodemap-btn" data-insp="distribute" data-value="vertical" ${nodes.length < 3 ? "disabled" : ""}>vertically</button></div></div>`;
    const scalables = nodes.filter((n) => M.scalable(n.type));
    if (scalables.length) html += scaleRow(scalables);
    const fits = nodes.filter((n) => M.AUTOFIT_TYPES.includes(n.type));
    if (fits.length) html += triCheck("multi-autofit", fits, (n) => n.autoFit, "Auto-fit & Scale", "Size each node to all of its content; resizing then keeps the aspect ratio and scales the content (one undo step)");
    html += triCheck("multi-hide-header", nodes, (n) => n.hideHeader, "Hide header in View Mode", "View Mode and exports leave these nodes' headers out; Edit Mode shows them dimmed (one undo step)");
    const revs = nodes.map(revisionOf).filter((r) => r !== null);
    if (revs.length) {
      const one = new Set(revs).size === 1 ? revs[0] : "";
      html += `<div class="inspector-field"><span class="inspector-label">${escapeHtml(`Revision (${revs.length} of ${nodes.length})`)}</span><div class="nm-insp-inline">${verInput("multi-version", one, `spellcheck="false" placeholder="${one ? "" : "mixed"}" title="Moves every selected node with a revision (one undo step)"`)}<button type="button" class="nodemap-btn" data-insp="multi-version-apply" title="Move every selected node with a revision to this one (one undo step)">Apply</button></div></div>`;
    }
    const structs = nodes.filter((n) => n.type === "struct");
    if (structs.length) {
      const styles = new Set(structs.map((n) => n.data.style));
      const cur = styles.size === 1 ? [...styles][0] : "";
      html += row(`Struct style (${structs.length})`, sel("multi-style", cur, cur ? M.STRUCT_STYLES : ["", ...M.STRUCT_STYLES], cur ? null : ["mixed", ...M.STRUCT_STYLES]));
    }
    if (st.layers?.length) {
      html += row("Layer", sel("multi-layer", "", ["", ...st.layers.map((l) => l.id)], ["— assign layer —", ...st.layers.map((l) => l.name)]));
    }
    html += `<div class="inspector-field"><span class="inspector-label">Color</span>${swatches("")}</div>`;
    const bgs = new Set(nodes.map((x) => x.bgColor));
    html += bgRow("Background", bgs.size === 1 ? [...bgs][0] : "", "var(--bg-secondary)", "bg");
    const arranged = nodes.filter((x) => x.type !== "group").length;
    if (arranged) html += layoutRow(`${arranged} selected`);
    if (revs.length) html += timeTravel(true);
    html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="duplicate" title="Duplicate the selection (Ctrl+D)">⧉ Duplicate</button><button type="button" class="nodemap-btn" data-insp="group">▭ Group</button><button type="button" class="nodemap-btn" data-insp="fit">Fit</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
  } else if (nodes.length === 1) {
    const n = nodes[0];
    const d = n.data;
    html += row("Title", `<div class="nm-insp-inline">${input("title", M.displayTitle(n))}${n.customTitle ? '<button type="button" class="nodemap-btn" data-insp="reset-title" title="Back to the default title">Reset</button>' : ""}</div>`);
    html += `<div class="nm-insp-pair">${row("Width", input("w", n.w, 'type="number"'))}${row("Height", input("h", n.h, 'type="number"'))}</div>`;
    if (M.AUTOFIT_TYPES.includes(n.type)) {
      html += check("autofit", n.autoFit, "Auto-fit & Scale", "Size the node to all of its content; resizing then keeps the aspect ratio and scales the content");
    }
    if (M.scalable(n.type)) html += scaleRow([n]);
    html += `<div class="inspector-field"><span class="inspector-label">Color</span>${swatches(n.color, n.type === "note" ? "var(--accent-yellow)" : "var(--accent-blue)")}</div>`;
    html += bgRow("Background", n.bgColor, "var(--bg-secondary)", "bg");
    if (st.layers?.length) {
      html += row("Layer", sel("layerId", n.layerId || "", ["", ...st.layers.map((l) => l.id)], ["(Always visible)", ...st.layers.map((l) => l.name)]));
    }
    html += check("hide-header", n.hideHeader, "Hide header in View Mode", "View Mode and exports leave this node's header out; Edit Mode shows it dimmed (drag, rename, restore)");
    if (["struct", "source", "symbol", "kconfig", "maintainer", "kapi", "doc", "history"].includes(n.type) || (n.type === "diff" && n.data.mode === "commit")) html += row("Revision", verInput("version", n.version, 'spellcheck="false"'));
    if (n.type === "struct") {
      html += row("Name", input("d-name", d.name, 'spellcheck="false" data-ac="struct"'));
      html += `<div class="nm-insp-pair">${row("Kind", sel("d-kind", d.kind, ["struct", "union"]))}${row("Style", sel("d-style", d.style, M.STRUCT_STYLES))}</div>`;
      html += row("Defining file", fileInput("d-path", d.path, `spellcheck="false" placeholder="${escapeHtml(d.hint || "from the index")}"`));
      html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="struct-types" aria-pressed="${d.showTypes}">Types: ${d.showTypes ? "shown" : "hidden"}</button><button type="button" class="nodemap-btn" data-insp="struct-guards" aria-pressed="${d.showGuards}">#ifdef: ${d.showGuards ? "shown" : "hidden"}</button></div>`;
      html += check("d-disableKconfigEngine", d.disableKconfigEngine, "Disable Kconfig engine", "This node ignores the Kconfig engine: no #ifdef branch folds on its own");
      const eng = ctrl.kconfigEngineFor(n);
      const why = d.disableKconfigEngine ? "off for this node" : !st.kconfigCollapse ? "off for the canvas" : eng ? `${eng.title || eng.tabId} · ${eng.version || "?"}${eng.arch ? ` · ${eng.arch}` : ""}: compiled-out branches fold` : `no resolved engine for ${n.version || "this revision"}`;
      html += `<div class="inspector-field"><span class="inspector-label">Kconfig folding</span><span class="inspector-val">${escapeHtml(why)}</span></div>`;
      if (d.style === "layout") {
        const pm = paholeRows(ctrl.paholeFor(n), d);
        if (pm?.totalSize) html += `<div class="inspector-field"><span class="inspector-label">Memory layout</span><span class="inspector-val">${pm.totalSize} bytes · ${pm.cacheLines} cache line${pm.cacheLines === 1 ? "" : "s"} · ${pm.holesCount} hole${pm.holesCount === 1 ? "" : "s"}</span></div>`;
      }
      const fieldLocks = Object.entries(d.fieldLocks || {});
      if (fieldLocks.length) {
        html += `<div class="inspector-field"><span class="inspector-label">Field locks (${fieldLocks.length})</span><div class="nm-es-list">${fieldLocks.map(([fk, fl]) =>
          `<div class="nm-es-row"><span class="nm-lock-badge nm-lock-${escapeHtml(fl.kind)}">${escapeHtml(M.formatLockBadge(fl))}</span><span class="inspector-val">${escapeHtml(fk)}</span><button type="button" class="nodemap-btn nm-danger" data-insp="field-lock-del" data-key="${escapeHtml(fk)}" title="Clear field lock">×</button></div>`).join("")}</div></div>`;
      }
      const loaded = ctrl.content.get(ctrl.fetchKeyOf(n));
      const hidden = loaded?.status === "ok" ? structRows(loaded.value, d, { editMode: true }).hidden : Object.keys(d.hiddenItems || {}).length;
      const types = ctrl.expandableTypesOf ? ctrl.expandableTypesOf(n.id) : [];
      if (types.length) html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="struct-expand-types" title="${escapeHtml(`Add or connect a node for every non-standard type the visible members use: ${types.slice(0, 12).map((t) => `${t.ref.kind} ${t.ref.name}`).join(", ")}${types.length > 12 ? ", …" : ""}`)}">⇶ Expand Types (${types.length})</button></div>`;
      if (hidden) html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="show-hidden" title="Restore every hidden member, aggregate, #ifdef block and the outer conditions">Show all hidden (${hidden})</button></div>`;
      const layout = ctrl.content.get(ctrl.fetchKeyOf(n))?.value;
      if (layout) html += `<div class="inspector-field"><span class="inspector-label">Definition</span><span class="inspector-val">${escapeHtml(`${layout.file_path}:${layout.line_s}-${layout.line_e}`)} · span from ${layout.span_source === "db" ? "the index" : "the source text"}; guards read from the source</span></div>`;
    } else if (n.type === "symbol") {
      html += row("Symbol", input("d-name", d.name, 'spellcheck="false" data-ac="symbol"'));
      if (d.kind === "func") {
        html += `<div class="nm-insp-actions">
          <button type="button" class="nodemap-btn" data-insp="cg-callers">⤴ + Callers</button>
          <button type="button" class="nodemap-btn" data-insp="cg-callees">⤵ + Callees</button>
          <button type="button" class="nodemap-btn" data-insp="cg-path">🛣 Call Path…</button>
        </div>`;
      }
    } else if (n.type === "source") {
      html += row("File", fileInput("d-path", d.path, 'spellcheck="false"'));
      html += row("Show", sel("d-mode", d.mode, M.SOURCE_MODES));
      if (d.mode === "range") html += `<div class="nm-insp-pair">${row("From", input("d-start", d.start || "", 'type="number" min="1"'))}${row("To", input("d-end", d.end || "", 'type="number" min="1"'))}</div>`;
      if (d.mode === "symbol") html += `<div class="nm-insp-pair">${row("Symbol", input("d-symbol", d.symbol, 'spellcheck="false" data-ac="symbol"'))}${row("Kind", sel("d-symbolKind", d.symbolKind, M.SYMBOL_KINDS))}</div>${row("Context lines", input("d-context", d.context, 'type="number" min="0" max="200"'))}`;
    } else if (n.type === "man") {
      html += `<div class="inspector-field"><span class="inspector-label">Page</span><span class="inspector-val">${escapeHtml(d.path || `${d.name}(${d.section})`)} · ${escapeHtml(d.version || "")}</span></div>`;
    } else if (n.type === "tab") {
      html += `<div class="inspector-field"><span class="inspector-label">Embedded ${escapeHtml(d.tabType)} tab</span><span class="inspector-val">#${escapeHtml(d.hash)}</span></div>`;
      const sess = M.embedSessionOf(ctrl.canvas, n);
      html += row("Embedded session", sel("d-embedSessionId", sess.id, st.embedSessions.map((x) => x.id), st.embedSessions.map((x) => x.name)));
      html += `<div class="inspector-field"><span class="inspector-label">Runs isolated in this NodeMap's session "${escapeHtml(sess.name)}": tabs of one session share their Kconfig engines; nothing is saved and no workspace session sees it. Click it to interact; Esc or a click on the canvas gives the pointer back.</span></div>`;
    } else if (n.type === "diff") {
      html += row("Show", sel("d-mode", d.mode, M.DIFF_MODES, ["one file between two revisions", "a commit"]));
      if (d.mode === "commit") html += row("Commit", input("d-commitHash", d.commitHash, 'spellcheck="false"'));
      else html += `<div class="nm-insp-pair">${row("From", verInput("d-versionA", d.versionA, 'spellcheck="false"'))}${row("To", verInput("d-versionB", d.versionB, 'spellcheck="false"'))}</div>`;
      html += row(d.mode === "commit" ? "File (all when empty)" : "File", fileInput("d-path", d.path, 'spellcheck="false"'));
      if (d.mode === "file") html += row("Old path (rename)", fileInput("d-oldPath", d.oldPath, 'spellcheck="false"'));
    } else if (n.type === "mail") {
      html += `<div class="nm-insp-pair">${row("Thread id", input("d-tid", d.tid || "", 'type="number" min="1"'))}${row("Message-ID", input("d-mid", d.mid, 'spellcheck="false"'))}</div>`;
      if (!d.single) html += '<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="mail-expand" title="One node per message, connected by reply edges as the thread replies">⇶ Expand Thread to Nodes</button></div>';
      if (d.single) {
        html += `<div class="inspector-field"><span class="inspector-label">Message</span><span class="inspector-val">${d.fullBody ? `whole message saved with the NodeMap (${d.fullBody.length.toLocaleString()} characters)` : "shown whole once it loads"}</span></div>`;
      } else {
        const open = Object.keys(d.openMids || {});
        const pins = Object.keys(d.pinnedBodies || {});
        const allFull = open.length > 0 && open.every((m) => d.fullMids?.[m]);
        html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="mail-full-open" aria-pressed="${allFull}" ${open.length ? "" : "disabled"} title="Show every open message whole instead of its 10-line excerpt">${allFull ? "⤡ Excerpts" : "⤢ Full messages"}${open.length ? ` (${open.length} open)` : ""}</button>${pins.length ? `<button type="button" class="nodemap-btn" data-insp="mail-unpin-all" title="Stop saving the pinned messages with the NodeMap">📌 Unpin all (${pins.length})</button>` : ""}</div>`;
      }
    } else if (n.type === "kconfig") {
      html += `<div class="nm-insp-pair">${row("Symbol", input("d-symbol", d.symbol, 'spellcheck="false" data-ac="kconfig"'))}${row("ARCH", input("d-arch", d.arch, 'spellcheck="false"'))}</div>`;
      const values = ctrl.kconfigValuesFor(n);
      const eng = ctrl.pickEngine(n);
      const why = values ? `${kconfigValue(values, d.symbol) || "unset"} in ${eng?.title || eng?.tabId || "the engine"}` : "no Kconfig engine followed for this revision";
      html += `<div class="inspector-field"><span class="inspector-label">Value</span><span class="inspector-val">${escapeHtml(why)}</span></div>`;
      html += '<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="kc-expand" title="Add or connect every related symbol with labeled edges">⇶ Expand Relations</button></div>';
    } else if (n.type === "maintainer") {
      html += row("Section name", input("d-query", d.query || d.title, 'spellcheck="false"'));
      html += `<div class="nm-insp-pair">${row("Or a file", fileInput("d-path", d.path, 'spellcheck="false"'))}${row("Section id", input("d-secId", d.secId || "", 'type="number" min="1"'))}</div>`;
    } else if (n.type === "cve") {
      html += row("CVE ID", input("d-cveId", d.cveId, 'placeholder="CVE-2024-..." spellcheck="false"'));
      html += `<div class="nm-insp-pair">${row("Release", verInput("d-release", d.release, 'spellcheck="false"'))}${row("File CVEs", fileInput("d-filePath", d.filePath, 'spellcheck="false"'))}</div>`;
    } else if (n.type === "kapi") {
      html += `<div class="nm-insp-pair">${row("Catalog", sel("d-catalog", d.catalog, M.KAPI_CATALOGS))}${row("ARCH", input("d-arch", d.arch, 'spellcheck="false"'))}</div>`;
      html += `<div class="nm-insp-pair">${row("Symbol", input("d-symbol", d.symbol, 'spellcheck="false"'))}${row("Query", input("d-query", d.query, 'spellcheck="false"'))}</div>`;
    } else if (n.type === "doc") {
      html += row("Path", fileInput("d-path", d.path, 'spellcheck="false"'));
      html += `<div class="nm-insp-pair">${row("Mode", sel("d-mode", d.mode, M.DOC_MODES))}${row("Functions", input("d-functions", d.functions, 'spellcheck="false"'))}</div>`;
    } else if (n.type === "history") {
      html += `<div class="nm-insp-pair">${row("Mode", sel("d-mode", d.mode, M.HISTORY_MODES))}${row("Line", input("d-line", d.line || 1, 'type="number" min="1"'))}</div>`;
      html += `<div class="nm-insp-pair">${row("File", fileInput("d-path", d.path, 'spellcheck="false"'))}${row("Symbol", input("d-name", d.name, 'spellcheck="false" data-ac="symbol"'))}</div>`;
      if (d.mode === "blame") {
        const count = ctrl.modelOf(n)?.commits?.length || 0;
        html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="blame-expand-commits" ${count ? "" : "disabled"} title="One commit details node per blamed commit, wired from its row (existing ones are reused; asks first above ${BULK_CONFIRM_ABOVE})">⇶ Expand Commits to Nodes${count ? ` (${count})` : ""}</button><button type="button" class="nodemap-btn" data-insp="blame-expand-diffs" ${count ? "" : "disabled"} title="One commit diff node per blamed commit, wired from its row (existing ones are reused; asks first above ${BULK_CONFIRM_ABOVE})">⇶ Expand Diffs to Nodes${count ? ` (${count})` : ""}</button></div>`;
      }
    } else if (n.type === "commit") {
      html += row("Commit", input("d-commitHash", d.commitHash, 'spellcheck="false" placeholder="a1b2c3d4"'));
      html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="commit-spawn-diff" ${d.commitHash ? "" : "disabled"} title="Add (or connect) this commit's diff node">± Open Diff Node</button></div>`;
    } else if (n.type === "note") {
      html += check("d-markdown", d.markdown, "Render Markdown");
    } else if (n.type === "group") {
      const members = M.groupDescendants(ctrl.canvas.nodes, n.id).length;
      html += `<div class="inspector-field"><span class="inspector-label">Members</span><span class="inspector-val">${members} (drag nodes in or out of the frame)</span></div>`;
      const direct = ctrl.canvas.nodes.filter((x) => x.groupId === n.id && x.type !== "group").length;
      if (direct) html += layoutRow(`${direct} member${direct === 1 ? "" : "s"}`);
      html += `<div class="nm-insp-actions"><button type="button" class="nodemap-btn" data-insp="enter-submap">${d.subCanvas ? "🌀 Enter Sub-Map Portal" : "🌀 Convert to Sub-Map Portal"}</button>${ctrl.versionDiffTarget ? `<button type="button" class="nodemap-btn" data-insp="ver-apply-group">Apply ${escapeHtml(ctrl.versionDiffTarget)}</button>` : ""}</div>`;
    }
    const locks = Array.isArray(n.locks) ? n.locks : [];
    html += `<div class="inspector-field"><span class="inspector-label">Locking &amp; concurrency</span><div class="nm-es-list">${locks.map((lk, idx) =>
      `<div class="nm-lock-row"><div class="nm-es-row">${sel("node-lock-kind", lk.kind, M.LOCK_KINDS, M.LOCK_KINDS, `data-idx="${idx}" aria-label="Lock kind"`)}${input("node-lock-name", lk.lockName, `data-idx="${idx}" placeholder="lock name (e.g. mmap_lock)" spellcheck="false" aria-label="Lock symbol"`) }<button type="button" class="nodemap-btn nm-danger" data-insp="node-lock-del" data-idx="${idx}" title="Remove lock annotation">×</button></div>${input("node-lock-note", lk.note, `data-idx="${idx}" placeholder="rule / lock-order note" spellcheck="false" aria-label="Lock note"`)}</div>`).join("")}<button type="button" class="nodemap-btn" data-insp="node-lock-add">+ Add Lock Annotation</button></div></div>`;
    html += `<div class="nm-insp-actions">${M.TAB_OPENABLE_TYPES.includes(n.type) ? '<button type="button" class="nodemap-btn" data-insp="open-tab">↗ Open as tab</button>' : ""}<button type="button" class="nodemap-btn" data-insp="duplicate" title="Duplicate (Ctrl+D)">⧉ Duplicate</button><button type="button" class="nodemap-btn nm-danger" data-insp="delete">Delete</button></div>`;
    if (M.TAB_OPENABLE_TYPES.includes(n.type)) {
      const targets = ctrl.copyTargets();
      if (!targets.some((x) => x.id === ctrl.copyTarget)) ctrl.copyTarget = targets[0]?.id || "";
      html += targets.length
        ? `<div class="inspector-field"><span class="inspector-label">Copy to Session</span><div class="nm-insp-inline">${sel("copy-target", ctrl.copyTarget, targets.map((x) => x.id), targets.map((x) => x.name || x.id))}<button type="button" class="nodemap-btn" data-insp="copy-session" title="Open this node as a tab in that workspace session (the node stays)">Copy…</button></div></div>`
        : '<div class="inspector-field"><span class="inspector-label">Copy to Session</span><span class="inspector-val nm-hint">No other workspace session: create one with Switch Session</span></div>';
    }
  } else {
    html += `<div class="inspector-field"><span class="inspector-label">Canvas</span><span class="inspector-val">${ctrl.canvas.nodes.length} nodes · ${ctrl.canvas.edges.length} connections</span></div>`;
    html += `<div class="nm-insp-pair">${row("Grid", input("grid", st.grid, 'type="number" min="4" max="200"'))}${check("snap", st.snap, "Snap")}</div>`;
    html += bgRow("Canvas background", st.bgColor || "", "var(--bg-canvas)", "canvas-bg");
    html += layoutRow("");
    html += row("Tab render quality", sel("render-quality", st.renderQuality || "auto", M.RENDER_QUALITIES, ["Auto (sharper when zoomed in)", "1× (fastest)", "1.5×", "2× (sharpest)"], 'title="Embedded tab iframes render at this multiple of their size and are drawn scaled down, so their text stays sharp when zoomed in or scaled up"'));
    const fHops = ctrl.focusHops;
    html += `<div class="inspector-field"><span class="inspector-label">Focus / Trace</span><div class="nm-insp-grid">
      <button type="button" class="nodemap-btn" data-insp="focus-hops" data-value="off" aria-pressed="${fHops == null}">Off</button>
      <button type="button" class="nodemap-btn" data-insp="focus-hops" data-value="1" aria-pressed="${fHops === 1}">1-hop</button>
      <button type="button" class="nodemap-btn" data-insp="focus-hops" data-value="2" aria-pressed="${fHops === 2}">2-hop</button>
      <button type="button" class="nodemap-btn" data-insp="focus-hops" data-value="all" aria-pressed="${fHops === Infinity}">All</button>
    </div></div>`;
    const layers = st.layers || [];
    html += `<div class="inspector-field"><span class="inspector-label">Visibility layers</span><div class="nm-layer-list">${layers.map((l) =>
      `<div class="nm-layer-row"><input type="checkbox" data-insp="layer-toggle" data-id="${escapeHtml(l.id)}" ${l.visible !== false ? "checked" : ""} aria-label="Layer visible"/><input data-insp="layer-name" data-id="${escapeHtml(l.id)}" value="${escapeHtml(l.name)}" aria-label="Layer name" spellcheck="false"/><button type="button" class="nodemap-btn nm-danger" data-insp="layer-delete" data-id="${escapeHtml(l.id)}" title="Delete layer">×</button></div>`).join("")}
      <button type="button" class="nodemap-btn" data-insp="layer-add">+ New Layer</button></div></div>`;
    html += timeTravel(false);
    html += NodeMapTourPlayer.inspectorHtml(ctrl.canvas, ctrl.activeTourId);
    const engines = ctrl.followedEngines();
    html += `<div class="inspector-field"><span class="inspector-label">NodeMap sessions</span><div class="nm-es-list">${st.embedSessions.map((x) =>
      `<div class="nm-es-row"><input data-insp="es-name" data-id="${escapeHtml(x.id)}" value="${escapeHtml(x.name)}" aria-label="Session name" spellcheck="false"/><span class="nm-es-count" title="Tab nodes running in it">${ctrl.canvas.nodes.filter((n) => n.type === "tab" && M.embedSessionOf(ctrl.canvas, n).id === x.id).length}</span>` +
      `<button type="button" class="nodemap-btn nm-danger" data-insp="es-delete" data-id="${escapeHtml(x.id)}" title="Delete (its tab nodes move to the first session)"${st.embedSessions.length <= 1 ? " disabled" : ""}>×</button></div>`).join("")}` +
      `<button type="button" class="nodemap-btn" data-insp="es-add"${st.embedSessions.length >= M.EMBED_SESSIONS_MAX ? " disabled" : ""}>+ New Session</button></div></div>`;
    html += row("Follow session", sel("follow-session", st.followSession, ["", ...st.embedSessions.map((x) => x.id)], ["Host workspace", ...st.embedSessions.map((x) => `NodeMap: ${x.name}`)]));
    html += check("kconfig-collapse", st.kconfigCollapse, "Fold #ifdef branches the Kconfig engine compiles out", "Struct nodes fold the branches of #if / #ifdef blocks that the Kconfig engine's configuration leaves out");
    if (engines.length > 1 || (st.kconfigEngine && !engines.some((e) => e.tabId === st.kconfigEngine))) {
      const ids = ["", ...engines.map((e) => e.tabId)];
      const labels = ["Auto: the engine of each node's revision", ...engines.map((e) => `${e.title || e.tabId} · ${e.version || "?"}${e.arch ? ` · ${e.arch}` : ""}`)];
      if (st.kconfigEngine && !ids.includes(st.kconfigEngine)) {
        ids.push(st.kconfigEngine);
        labels.push("(closed engine)");
      }
      html += row("Kconfig engine", sel("kconfig-engine", st.kconfigEngine, ids, labels));
    } else {
      const e = engines[0];
      const none = st.followSession ? "None in this NodeMap session: embed a Kconfig tab in it and turn its engine on" : "None with a resolved configuration: open a Kconfig tab and turn its engine on";
      html += `<div class="inspector-field"><span class="inspector-label">Kconfig engine</span><span class="inspector-val">${escapeHtml(e ? `${e.title || e.tabId} · ${e.version || "?"}${e.arch ? ` · ${e.arch}` : ""} (nodes of ${e.version || "that revision"})` : none)}</span></div>`;
    }
    html += `<ul class="nm-help">
      <li>Drag background to pan; wheel to zoom; Ctrl+0 resets (100%), Ctrl+1 fits view</li>
      <li>Ctrl+F searches across sub-maps; ? opens full keyboard &amp; gesture shortcuts</li>
      <li>Alt+Drag clones selection; Shift+Drag locks axis; F2 renames selected node</li>
      <li>Drag a side or struct field dot to connect; Shift+dblclick adds a bend</li>
      <li>Ctrl+C / Ctrl+V copy &amp; paste across tabs; Ctrl+D duplicates; Ctrl+Z/Y undo/redo</li>
      <li>Drag a tab from a tab strip onto the canvas to copy it in (Shift: as a live tab)</li>
    </ul>`;
  }
  ctrl.inspectorEl.innerHTML = html;
  for (const el of ctrl.inspectorEl.querySelectorAll?.('input[data-tri="mixed"]') || []) el.indeterminate = true;
  wireInspectorAutocomplete(ctrl);
}

/** Node and scope (`client`, `version`) an Inspector picker of key `k` works in. */
export function inspectorPickContext(ctrl, k) {
  const node = ctrl.selected.size === 1 ? ctrl.byId([...ctrl.selected][0]) : null;
  const client = ctrl.clientFor(node || {});
  const d = node?.data || {};
  let version = node?.version || ctrl.currentVersion || "";
  if (node?.type === "diff" && d.mode !== "commit") version = (k === "d-oldPath" ? d.versionA : d.versionB) || version;
  if (node?.type === "cve" && d.release) version = d.release;
  return { node, client, version };
}

/** Nodes a Version Time-Travel Compare covers: the multi-selection, else the whole canvas (null). */
const compareScope = (ctrl) => (ctrl.selected.size > 1 ? [...ctrl.selected] : null);

/** Selected node ids a picker opened for: a pick lands only if the selection is still the same. */
const selectionKey = (ctrl) => [...ctrl.selected].sort().join(",");

/**
 * Put a picked value in the Inspector input `k` and commit it as if typed (one undo step); dropped
 * when the selection changed since the picker opened (`openedFor`). The input keeps the focus.
 */
export function commitInspectorPick(ctrl, k, value, openedFor = null) {
  if (openedFor !== null && openedFor !== selectionKey(ctrl)) return;
  const el = ctrl.inspectorEl?.querySelector?.(`[data-insp="${k}"]`);
  const target = el || { dataset: { insp: k }, value, tagName: "INPUT", type: "text" };
  target.value = value;
  if (!target.closest) target.closest = () => target;
  const res = onInspectorInput(ctrl, { target }, true);
  ctrl.inspectorEl?.querySelector?.(`[data-insp="${k}"]`)?.focus?.();
  return res;
}

/** Revision popover / File Explorer of an Inspector picker button (`data-insp="pick"`). */
export function openInspectorPicker(ctrl, btn) {
  const k = btn.dataset.for;
  const kind = btn.dataset.pick;
  const el = ctrl.inspectorEl?.querySelector?.(`[data-insp="${k}"]`);
  const { client, version } = inspectorPickContext(ctrl, k);
  const current = el?.value ?? "";
  const openedFor = selectionKey(ctrl);
  if (kind === "version") {
    return openVersionPicker(btn, { current: current || version, client, picker: ctrl.refPicker, onSelect: (ref) => commitInspectorPick(ctrl, k, ref, openedFor) });
  }
  if (kind === "file") {
    return openFileExplorer({ client, version, currentPath: current, openModalFn: ctrl.openModalFn, onSelect: (path) => commitInspectorPick(ctrl, k, path, openedFor) });
  }
  return null;
}

/** Struct pick: name, defining file and kind in one commit (the file the index knows for it). */
export function commitStructPick(ctrl, row) {
  const node = ctrl.selected.size === 1 ? ctrl.byId([...ctrl.selected][0]) : null;
  if (!node || node.type !== "struct" || !row?.name) return;
  const patch = { name: row.name };
  if (row.path && (row.name !== node.data.name || !node.data.path)) patch.path = row.path;
  const kind = structKindOf(row.kind);
  if (kind) patch.kind = kind;
  node.data = M.normalizeData(node.type, { ...node.data, ...patch });
  if (!node.customTitle) node.title = M.defaultTitle(node.type, node.data);
  ctrl.commit({ inspector: true, coalesce: `insp:d-name:${node.id}`, coalesceEnd: true });
}

/** Attach the symbol / struct / Kconfig autocompletes to the freshly rendered Inspector. */
function wireInspectorAutocomplete(ctrl) {
  const els = ctrl.inspectorEl?.querySelectorAll?.("input[data-ac]");
  if (!els?.length) return;
  for (const el of els) {
    const mode = el.dataset.ac;
    const k = el.dataset.insp;
    attachAutocomplete(el, {
      fetch: (q) => {
        const { node, client, version } = inspectorPickContext(ctrl, k);
        const arch = ctrl.inspectorEl?.querySelector?.('[data-insp="d-arch"]')?.value || node?.data?.arch || "x86_64";
        return fetchSuggestions(mode, client, version, q, { arch });
      },
      onPick: (row) => (mode === "struct" ? commitStructPick(ctrl, row) : commitInspectorPick(ctrl, k, row.name)),
    });
  }
}

export function onInspectorInput(ctrl, e, final) {
  const el = e.target.closest("[data-insp]");
  if (!el || el.tagName === "BUTTON") return;
  const k = el.dataset.insp;
  // A checkbox click fires `input` and then `change`: only the `change` applies it (one commit, one undo
  // step, and a toggle never runs twice)
  if (el.type === "checkbox" && !final) return;
  const val = el.type === "checkbox" ? el.checked : el.value;
  const edge = ctrl.selectedEdge ? ctrl.canvas.edges.find((x) => x.id === ctrl.selectedEdge) : null;
  const node = ctrl.selected.size === 1 ? ctrl.byId([...ctrl.selected][0]) : null;
  const int = (v) => (/^\d+$/.test(String(v).trim()) ? parseInt(v, 10) : null);
  const coalesce = `insp:${k}:${edge?.id || node?.id || (ctrl.selected.size ? [...ctrl.selected].join(",") : "canvas")}`;
  const coalesceEnd = Boolean(final);
  if (k === "color-custom" || k === "bg-custom" || k === "canvas-bg-custom") {
    if (!M.isCustomColor(val)) return;
    const opts = { inspector: final, coalesce, coalesceEnd };
    if (k === "bg-custom") return ctrl.setBackground(val, opts);
    if (k === "canvas-bg-custom") return ctrl.setCanvasBackground(val, opts);
    return ctrl.setColor(val, opts);
  }
  if (k === "copy-target") {
    ctrl.copyTarget = String(val || "");
    return;
  }
  if (k === "content-scale") {
    const pct = Number(val);
    if (!Number.isFinite(pct) || pct <= 0) return;
    const list = ctrl.selectedNodes().filter((x) => M.scalable(x.type));
    if (!list.length) return;
    for (const x of list) {
      M.setContentScale(x, pct / 100);
      ctrl.portOffsets.delete(x.id);
    }
    return ctrl.commit({ inspector: final, coalesce: `insp:content-scale:${list.map((x) => x.id).join(",")}`, coalesceEnd: Boolean(final) });
  }
  if (k === "multi-version") {
    if (final && String(val || "").trim()) ctrl.applyVersionToSelection(val, { toastResult: true });
    return;
  }
  if (k === "ver-target") {
    const prev = ctrl.versionDiffShown || "";
    ctrl.versionDiffTarget = String(val || "").trim();
    // One comparison per revision: a pick and the input's own change event do not load it twice
    if (final && ctrl.versionDiffTarget && ctrl.versionDiffTarget !== prev) ctrl.compareVersion(ctrl.versionDiffTarget, { ids: compareScope(ctrl) });
    return;
  }
  if (k === "tour-select") {
    ctrl.activeTourId = String(val || "");
    return ctrl.renderInspector();
  }
  if (k === "tour-title") {
    const t = (ctrl.canvas.tours || []).find((x) => x.id === el.dataset.id);
    if (!t || !final) return;
    t.title = String(val || "").trim().slice(0, 128) || "Walkthrough";
  } else if (k === "tour-step-title" || k === "tour-step-caption") {
    const t = (ctrl.canvas.tours || []).find((x) => x.id === el.dataset.id);
    const s = t?.steps?.find((x) => x.id === el.dataset.stepId);
    if (!s) return;
    if (k === "tour-step-title") s.title = String(val || "").trim().slice(0, 256) || "Step";
    else s.caption = String(val || "").slice(0, 2000);
  } else if (k === "layer-toggle") {
    if (M.setLayerVisibility(ctrl.canvas.settings, el.dataset.id, val) === null) return;
  } else if (k === "render-quality") {
    ctrl.canvas.settings.renderQuality = M.RENDER_QUALITIES.includes(val) ? val : "auto";
    ctrl.applyRenderQuality?.();
  } else if (k === "multi-autofit") {
    for (const n of ctrl.selectedNodes()) {
      if (!M.AUTOFIT_TYPES.includes(n.type)) continue;
      M.setAutoFit(n, Boolean(val));
      ctrl.portOffsets.delete(n.id);
      if (n.autoFit) ctrl.fitQueue.add(n.id);
    }
  } else if (k === "multi-hide-header") {
    for (const n of ctrl.selectedNodes()) {
      n.hideHeader = Boolean(val);
      ctrl.portOffsets.delete(n.id);
      if (n.autoFit) ctrl.fitQueue.add(n.id);
    }
  } else if (k === "layer-name") {
    if (!final) return;
    const layer = (ctrl.canvas.settings.layers || []).find((l) => l.id === el.dataset.id);
    if (!layer) return;
    layer.name = String(val || "").trim().slice(0, 128) || layer.id;
  } else if (k === "es-name") {
    if (!final) return;
    if (!M.renameEmbedSession(ctrl.canvas.settings, el.dataset.id, val)) return ctrl.renderInspector();
  } else if (k === "follow-session") {
    ctrl.canvas.settings.followSession = String(val || "");
    ctrl.syncFollowChannel();
    ctrl.onKconfigChanged();
  } else if (k === "kconfig-collapse") {
    ctrl.canvas.settings.kconfigCollapse = Boolean(val);
  } else if (k === "kconfig-engine") {
    ctrl.canvas.settings.kconfigEngine = String(val || "");
  } else if (k === "multi-style") {
    if (!M.STRUCT_STYLES.includes(val)) return;
    for (const n of ctrl.selectedNodes()) if (n.type === "struct") {
      n.data.style = val;
      ctrl.portOffsets.delete(n.id);
    }
  } else if (k === "multi-layer") {
    for (const n of ctrl.selectedNodes()) n.layerId = String(val || "");
  } else if (node && k === "layerId") {
    node.layerId = String(val || "");
  } else if (node && k === "hide-header") {
    node.hideHeader = Boolean(val);
    if (node.autoFit) ctrl.fitQueue.add(node.id);
  } else if (node && k === "autofit") {
    M.setAutoFit(node, Boolean(val));
    if (node.autoFit) ctrl.fitQueue.add(node.id);
  } else if (node && (k === "node-lock-kind" || k === "node-lock-name" || k === "node-lock-note")) {
    const idx = parseInt(el.dataset.idx, 10);
    if (!Number.isInteger(idx)) return;
    const patch = k === "node-lock-kind" ? { kind: val } : k === "node-lock-name" ? { lockName: val } : { note: val };
    if (!M.updateNodeLock(node, idx, patch)) return;
    if (node.autoFit) ctrl.fitQueue.add(node.id);
  } else if (k === "grid" && final) {
    ctrl.canvas.settings.grid = Math.max(4, Math.min(200, int(val) || M.GRID_DEFAULT));
    ctrl.applyTransform();
  } else if (k === "snap") {
    ctrl.canvas.settings.snap = Boolean(val);
    ctrl.rootEl.querySelector("[data-tb='snap']")?.setAttribute("aria-pressed", String(ctrl.canvas.settings.snap));
    ctrl.applyTransform();
  } else if (edge && k === "edge-preset") {
    if (!val) return;
    M.applyEdgePreset(edge, val);
  } else if (edge && k === "edge-lockKind") {
    edge.lockKind = M.LOCK_KINDS.includes(val) ? val : "";
  } else if (edge && k === "edge-layer") {
    edge.layerId = String(val || "");
  } else if (edge && k.startsWith("edge-")) {
    const f = k.slice(5);
    if (f === "label") edge.label = String(val).slice(0, 512);
    else Object.assign(edge, M.normalizeEdge({ ...edge, [f]: val }));
  } else if (node && k === "title") {
    if (!final) return;
    return ctrl.setTitle(node.id, val);
  } else if (node && (k === "w" || k === "h") && final) {
    const lim = M.SIZE_LIMITS[node.type];
    const v = int(val);
    if (v && node.autoFit) M.setScaledSize(node, k, v);
    else if (v) node[k] = k === "w" ? Math.max(lim.minW, Math.min(lim.maxW, v)) : Math.max(lim.minH, Math.min(lim.maxH, v));
    ctrl.portOffsets.delete(node.id);
  } else if (node && k === "version" && final) {
    node.version = String(val).trim();
  } else if (node && k.startsWith("d-")) {
    if (!final && el.tagName !== "SELECT") return;
    const f = k.slice(2);
    const raw = ["start", "end", "context", "tid", "secId", "line"].includes(f) ? int(val) : val;
    const reset = node.type === "maintainer" && (f === "query" || f === "path") ? { secId: null, title: "" } : {};
    node.data = M.normalizeData(node.type, { ...node.data, ...reset, [f]: typeof raw === "string" ? raw.trim() : raw });
    if (!node.customTitle) node.title = M.defaultTitle(node.type, node.data);
  } else return;
  ctrl.commit({ inspector: final && (el.tagName === "SELECT" || el.type === "checkbox" || k.startsWith("d-") || k === "w" || k === "h" || k === "es-name" || k === "layer-name" || k === "tour-title"), coalesce, coalesceEnd });
}

export function onInspectorClick(ctrl, e) {
  const btn = e.target.closest("button[data-insp]");
  if (!btn) return;
  const k = btn.dataset.insp;
  const node = ctrl.selected.size === 1 ? ctrl.byId([...ctrl.selected][0]) : null;
  if (k === "pick") openInspectorPicker(ctrl, btn);
  else if (k === "edit-mode") ctrl.setEditMode(!ctrl.canvas.settings.editMode);
  else if (k === "color") ctrl.setColor(btn.dataset.value || "");
  else if (k === "bg" || k === "bg-reset") ctrl.setBackground(k === "bg" ? btn.dataset.value || "" : "");
  else if (k === "canvas-bg" || k === "canvas-bg-reset") ctrl.setCanvasBackground(k === "canvas-bg" ? btn.dataset.value || "" : "");
  else if (k === "duplicate") ctrl.duplicateSelection();
  else if (k === "show-hidden" && node) ctrl.onStructAction(node, "show-hidden");
  else if (k === "node-lock-add" && node) {
    if (M.addNodeLock(node, { kind: "spinlock" })) {
      if (node.autoFit) ctrl.fitQueue.add(node.id);
      ctrl.commit();
    }
  } else if (k === "node-lock-del" && node) {
    if (M.removeNodeLock(node, parseInt(btn.dataset.idx, 10))) {
      if (node.autoFit) ctrl.fitQueue.add(node.id);
      ctrl.commit();
    }
  } else if (k === "field-lock-del" && node?.type === "struct") {
    M.setFieldLock(node.data, btn.dataset.key, null);
    if (node.autoFit) ctrl.fitQueue.add(node.id);
    ctrl.commit();
  } else if (k === "scale-reset") {
    const list = ctrl.selectedNodes().filter((x) => M.scalable(x.type));
    for (const x of list) {
      M.setContentScale(x, 1);
      ctrl.portOffsets.delete(x.id);
    }
    if (list.length) ctrl.commit();
  } else if (k === "es-add") {
    if (M.addEmbedSession(ctrl.canvas.settings)) ctrl.commit();
  } else if (k === "es-delete") {
    if (M.removeEmbedSession(ctrl.canvas, btn.dataset.id)) {
      ctrl.syncFollowChannel();
      ctrl.commit();
    }
  } else if (k === "multi-version-apply") {
    const inp = ctrl.inspectorEl?.querySelector?.("[data-insp='multi-version']");
    const ref = String(inp?.value || "").trim();
    if (ref) ctrl.applyVersionToSelection(ref, { toastResult: true });
  } else if (k === "blame-expand-commits" && node) ctrl.expandBlameToNodes(node.id, "commit");
  else if (k === "blame-expand-diffs" && node) ctrl.expandBlameToNodes(node.id, "diff");
  else if (k === "commit-spawn-diff" && node?.type === "commit") ctrl.spawnCommitDiff(node.id);
  else if (k === "copy-session" && node) ctrl.copyNodeToSession(node.id, ctrl.copyTarget);
  else if (k === "mail-expand" && node) ctrl.expandThreadToNodes(node.id);
  else if (k === "kc-expand" && node) ctrl.expandKconfigRelations(node.id);
  else if (k === "struct-expand-types" && node) ctrl.expandStructTypes(node.id);
  else if (k === "mail-full-open" && node?.type === "mail") {
    const d = node.data;
    const open = Object.keys(d.openMids || {});
    const allFull = open.every((m) => d.fullMids?.[m]);
    d.fullMids = allFull ? {} : Object.fromEntries(open.map((m) => [m, true]));
    if (node.autoFit) ctrl.fitQueue.add(node.id);
    ctrl.commit();
  } else if (k === "mail-unpin-all" && node?.type === "mail") {
    node.data.pinnedBodies = {};
    ctrl.commit();
  }
  else if (k === "cg-callers" && node) ctrl.expandCallgraph(node.id, "callers");
  else if (k === "cg-callees" && node) ctrl.expandCallgraph(node.id, "callees");
  else if (k === "cg-path" && node) ctrl.promptCallPath(node.id);
  else if (k === "enter-submap" && node) ctrl.enterSubMap(node.id);
  else if (k === "layout-dag") ctrl.autoLayoutDAG("dag");
  else if (k === "layout-force") ctrl.autoLayoutDAG("force");
  else if (k === "focus-hops") {
    const v = btn.dataset.value;
    ctrl.setFocusHops(v === "off" ? null : v === "all" ? Infinity : Number(v));
  } else if (k === "layer-add") {
    const l = M.addLayer(ctrl.canvas.settings, `Layer ${(ctrl.canvas.settings.layers?.length || 0) + 1}`);
    if (l) ctrl.commit();
  } else if (k === "layer-delete") {
    if (M.removeLayer(ctrl.canvas, btn.dataset.id)) ctrl.commit();
  } else if (k === "ver-compare") {
    const inp = ctrl.inspectorEl?.querySelector("[data-insp='ver-target']");
    ctrl.compareVersion(inp?.value || ctrl.versionDiffTarget, { ids: compareScope(ctrl) });
  } else if (k === "ver-clear") {
    ctrl.compareVersion("");
  } else if (k === "ver-apply") {
    const inp = ctrl.inspectorEl?.querySelector("[data-insp='ver-target']");
    const ref = inp?.value || ctrl.versionDiffTarget;
    // With several nodes selected it moves just those (the selection stays)
    if (ctrl.selected.size > 1) ctrl.applyVersionToSelection(ref, { toastResult: true });
    else ctrl.applyTargetVersion(ref);
  } else if (k === "ver-apply-group" && node) {
    ctrl.applyTargetVersion(ctrl.versionDiffTarget, node.id);
  } else if (k === "ver-revert") {
    ctrl.revertTargetVersion();
  } else if (k === "tour-add") {
    ctrl.canvas.tours = ctrl.canvas.tours || [];
    const t = createTour(`Tour ${ctrl.canvas.tours.length + 1}`);
    ctrl.canvas.tours.push(t);
    ctrl.activeTourId = t.id;
    ctrl.commit();
  } else if (k === "tour-delete") {
    ctrl.canvas.tours = (ctrl.canvas.tours || []).filter((x) => x.id !== btn.dataset.id);
    if (ctrl.activeTourId === btn.dataset.id) ctrl.activeTourId = ctrl.canvas.tours[0]?.id || "";
    ctrl.commit();
  } else if (k === "tour-capture") {
    const t = (ctrl.canvas.tours || []).find((x) => x.id === btn.dataset.id);
    if (t) {
      t.steps.push(captureTourStep({
        title: ctrl.tourPlayer.defaultStepTitle(t.steps.length),
        viewport: ctrl.canvas.viewport,
        selectedNodes: [...ctrl.selected],
        selectedEdges: ctrl.selectedEdge ? [ctrl.selectedEdge] : [],
      }));
      ctrl.commit();
    }
  } else if (k === "tour-play") {
    ctrl.tourPlayer.start(btn.dataset.id, 0);
  } else if (k === "tour-step-go") {
    ctrl.tourPlayer.start(btn.dataset.id, parseInt(btn.dataset.index, 10) || 0);
  } else if (k === "tour-step-up" || k === "tour-step-down") {
    const t = (ctrl.canvas.tours || []).find((x) => x.id === btn.dataset.id);
    if (t && reorderTourStep(t, btn.dataset.stepId, k === "tour-step-up" ? -1 : 1)) ctrl.commit();
  } else if (k === "tour-step-delete") {
    const t = (ctrl.canvas.tours || []).find((x) => x.id === btn.dataset.id);
    if (t && removeTourStep(t, btn.dataset.stepId)) ctrl.commit();
  } else if (k === "edge-clear-wp" && ctrl.selectedEdge) {
    const ed = ctrl.canvas.edges.find((x) => x.id === ctrl.selectedEdge);
    if (ed) {
      ed.waypoints = [];
      ctrl.commit();
    }
  } else if (k === "edge-callsite" && ctrl.selectedEdge) {
    const ed = ctrl.canvas.edges.find((x) => x.id === ctrl.selectedEdge);
    if (ed) ctrl.openCallSiteSource(ed);
  } else if (k === "align") ctrl.align(btn.dataset.value);
  else if (k === "distribute") ctrl.distribute(btn.dataset.value);
  else if (k === "group") ctrl.addGroup();
  else if (k === "fit") ctrl.fitView();
  else if (k === "delete") ctrl.deleteSelection();
  else if (k === "open-tab" && node) ctrl.openNodeAsTab(node.id);
  else if (k === "reset-title" && node) ctrl.setTitle(node.id, "");
  else if (k === "struct-types" && node) ctrl.onStructAction(node, "all-types");
  else if (k === "struct-guards" && node) ctrl.onStructAction(node, "guards");
  else if (k === "edge-reverse" && ctrl.selectedEdge) {
    const ed = ctrl.canvas.edges.find((x) => x.id === ctrl.selectedEdge);
    if (ed) {
      [ed.from, ed.to] = [ed.to, ed.from];
      [ed.fromSide, ed.toSide] = [ed.toSide, ed.fromSide];
      [ed.fromField, ed.toField] = [ed.toField, ed.fromField];
      ctrl.commit();
    }
  }
}
