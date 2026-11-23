/**
 * nodemap_dialogs.js - NodeMap modal dialogs and menus: + Add Node forms, Export menu & multi-format
 * export dialogs, Call-Path modal, Struct Field Lock modal, Canvas Library / Templates modal,
 * Impact Map builder dialog, and keyboard shortcuts cheat sheet.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { urlSync } from "../../url_sync.js";
import { toast } from "../../components/toast.js";
import { contextMenu } from "../../components/context_menu.js";
import { escapeHtml, openModal } from "../../components/modal.js";
import { copyToClipboard } from "../../utils/clipboard.js";
import { isTabFeatureEnabled } from "../../utils/projects.js";
import { isAlwaysVersionlessTab } from "../../tab_routes.js";
import * as M from "./canvas_model.js";
import { commitSpec, cveSpec, docSpec, historySpec, kapiSpec } from "./nodemap_bridge.js";
import { PNG_DEFAULT_SCALE, PNG_SCALES, pngSize, svgSize } from "./nodemap_export.js";
import { canvasToDot, canvasToMermaid } from "./nodemap_export_formats.js";
import {
  KERNEL_TEMPLATES, buildKernelTemplate, collectReferencedTabIds, deleteNamedCanvas,
  duplicateNamedCanvas, findOrphanedTabCanvases, listNamedCanvases, loadNamedCanvas,
  renameNamedCanvas, saveNamedCanvas,
} from "./nodemap_library.js";
import { generateImpactMap } from "./impact_map.js";
import { pickButtonHtml, wireFormPickers } from "./nodemap_pickers.js";
import { sessionManager } from "../../session_manager.js";

/** Tab types a KernelInfo tab node offers (feature-gated per project). */
export const EMBED_TAB_TYPES = [
  ["explorer", "Repository Explorer"], ["code", "Source Editor"], ["kconfig", "Kconfig"], ["commits", "Commits"],
  ["maintainers", "Maintainers"], ["mail", "Mailing List"], ["pahole", "Struct Layout"], ["callgraph", "Callgraph"],
  ["diff", "Compare"], ["treemap", "Treemap"], ["search", "Search"], ["api", "Kernel API"], ["insights", "Insights"],
  ["cve", "CVE Records"], ["man", "Man Pages"], ["watch", "Watch List"], ["bookmarks", "Bookmarks"], ["status", "Status"],
];

export const ADD_MENU = [
  ["struct", "Struct / Union…", "🧱"], ["source", "Source File / Slice…", "📄"], ["diff", "Diff…", "±"], ["commit", "Commit…", "⎇"],
  ["mail", "Mailing List Thread…", "✉"], ["kconfig", "Kconfig Symbol…", "⚙"], ["maintainer", "Maintainers…", "👥"],
  ["cve", "CVE / Fix-Chain…", "🛡"], ["kapi", "Kernel API…", "🔌"], ["doc", "Kernel Docs (.rst)…", "📖"],
  ["history", "Symbol History / Blame…", "🕰"], ["man", "Man Page…", "📘"], ["tab", "KernelInfo Tab…", "🗔"],
  ["symbol", "Symbol…", "🔣"], ["note", "Note Card", "📝"], ["group", "Group", "▭"],
];

export const SHORTCUT_SECTIONS = [
  {
    title: "Navigation & View",
    items: [
      ["Drag canvas / Space+Drag / Middle drag", "Pan canvas"],
      ["Mouse wheel / Ctrl+Wheel over node", "Zoom at cursor"],
      ["Ctrl+0 / Cmd+0", "Reset pan & zoom (100%)"],
      ["Ctrl+1 / Cmd+1", "Fit canvas or selection"],
      ["Ctrl+F / Cmd+F", "Search across canvas & sub-maps"],
      ["?", "Show this shortcuts cheat sheet"],
    ],
  },
  {
    title: "Selection & Authoring",
    items: [
      ["Shift+Drag canvas / Select tool", "Marquee selection"],
      ["Alt+Drag node header", "Clone selection on drag"],
      ["Shift+Drag node header", "Axis-lock move (horizontal / vertical)"],
      ["F2", "Rename selected node"],
      ["Arrow / Shift+Arrow", "Nudge 1 px / 1 grid step"],
      ["Double-click background", "Add Note Card here"],
      ["Ctrl+C / Ctrl+V / Ctrl+D", "Copy / Paste / Duplicate"],
      ["Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z", "Undo / Redo"],
      ["Delete / Backspace", "Delete selected nodes or edge"],
    ],
  },
  {
    title: "Connections & Walkthroughs",
    items: [
      ["Drag side or field dot", "Connect nodes or struct fields"],
      ["Shift+Double-click edge", "Add bend waypoint"],
      ["Double-click waypoint", "Remove waypoint"],
      ["Double-click edge", "Edit edge label"],
      ["Alt+Double-click call edge", "Open call-site source"],
      ["Left / Right / Esc (in Tour)", "Step through Code Tour"],
    ],
  },
];

export function showAddMenu(ctrl, x, y) {
  contextMenu.renderMenu(x, y, {
    header: { title: "Add Node", sub: "Placed at the center of the view" },
    items: [
      ...ADD_MENU.map(([type, label, icon]) => ({
        label,
        icon,
        onClick: () => {
          if (type === "note") ctrl.addNode({ type: "note", data: { text: "" } });
          else if (type === "group") ctrl.addGroup();
          else openNodeDialog(ctrl, type);
        },
      })),
      { separator: true },
      { label: "Patch / Commit Impact Map…", icon: "🗺", onClick: () => openImpactMapDialog(ctrl) },
      { label: "Templates & Canvas Library…", icon: "📚", onClick: () => openLibraryDialog(ctrl, "templates") },
    ],
  });
}

export function showExportMenu(ctrl, x, y) {
  contextMenu.renderMenu(x, y, {
    header: { title: "Export", sub: "This canvas" },
    items: [
      { label: "Canvas JSON (re-importable)", icon: "{}", onClick: () => ctrl.exportJSON() },
      { label: "SVG image…", icon: "🖼", onClick: () => openImageExport(ctrl, "svg") },
      { label: "PNG image…", icon: "🖼", onClick: () => openImageExport(ctrl, "png") },
      { separator: true },
      { label: "Interactive HTML (.html)…", icon: "🌐", onClick: () => openFormatExportModal(ctrl, "html") },
      { label: "Mermaid (.mmd)…", icon: "🧜", onClick: () => openFormatExportModal(ctrl, "mermaid") },
      { label: "Graphviz (.dot)…", icon: "◈", onClick: () => openFormatExportModal(ctrl, "dot") },
    ],
  });
}

export function openNodeDialog(ctrl, type, existing = null) {
  const v = existing?.version || ctrl.currentVersion || "";
  // `pick`: "version" / "file" adds the picker button (`nodemap_pickers.js`); `data-ac` autocompletes
  const field = (name, label, value = "", extra = "", pick = "") => {
    const inp = `<input name="${name}" value="${escapeHtml(value ?? "")}" ${extra}/>`;
    return `<label class="nm-form-row"><span>${escapeHtml(label)}</span>${pick ? `<div class="nm-pick-wrap">${inp}${pickButtonHtml(pick, name)}</div>` : inp}</label>`;
  };
  let body = field("version", "Revision", v, 'spellcheck="false"', "version");
  let title = "";
  if (type === "struct") {
    title = "Add Struct / Union";
    body += field("name", "Name", "", 'placeholder="task_struct" spellcheck="false" required data-ac="struct"');
    body += `<label class="nm-form-row"><span>Kind</span><select name="kind"><option value="struct">struct</option><option value="union">union</option></select></label>`;
    body += field("path", "Defining file (needed for unparsed revisions)", "", 'placeholder="include/linux/sched.h" spellcheck="false"', "file");
  } else if (type === "source") {
    title = "Add Source File / Slice";
    body += field("path", "File", "", 'placeholder="kernel/fork.c" spellcheck="false" required', "file");
    body += `<label class="nm-form-row"><span>Show</span><select name="mode"><option value="file">Whole file</option><option value="range">Line range</option><option value="symbol">Symbol definition</option></select></label>`;
    body += `<div class="nm-form-pair">${field("start", "From line", "", 'type="number" min="1"')}${field("end", "To line", "", 'type="number" min="1"')}</div>`;
    body += `<div class="nm-form-pair">${field("symbol", "Symbol", "", 'placeholder="copy_process" spellcheck="false" data-ac="symbol"')}
      <label class="nm-form-row"><span>Kind</span><select name="symbolKind">${M.SYMBOL_KINDS.map((k) => `<option value="${k}">${k || "any"}</option>`).join("")}</select></label></div>`;
    body += field("context", "Context lines around a symbol", "0", 'type="number" min="0" max="200"');
  } else if (type === "man") {
    title = "Add Man Page";
    body += field("name", "Page", "", 'placeholder="open" spellcheck="false" required');
    body += field("section", "Section", "", 'placeholder="2" spellcheck="false"');
  } else if (type === "symbol") {
    title = "Add Symbol";
    body += field("name", "Symbol", "", 'placeholder="schedule" spellcheck="false" required data-ac="symbol"');
    body += `<label class="nm-form-row"><span>Kind</span><select name="kind"><option value="func">function</option><option value="symbol">other</option></select></label>`;
  } else if (type === "diff") {
    title = "Add Diff";
    body += `<label class="nm-form-row"><span>Show</span><select name="mode"><option value="file">One file between two revisions</option><option value="commit">A commit</option></select></label>`;
    body += `<div class="nm-form-pair">${field("versionA", "From revision (file diff)", "", 'placeholder="v2.6.39" spellcheck="false"', "version")}${field("versionB", "To revision (file diff)", v, 'spellcheck="false"', "version")}</div>`;
    body += field("path", "File (all files of a commit when empty)", "", 'placeholder="kernel/fork.c" spellcheck="false"', "file");
    body += field("oldPath", "Old path (renamed file)", "", 'spellcheck="false"', "file");
    body += field("commitHash", "Commit (commit diff)", "", 'placeholder="a1b2c3d4" spellcheck="false"');
  } else if (type === "commit") {
    title = "Add Commit";
    body += field("commitHash", "Commit hash (full or abbreviated)", "", 'placeholder="a1b2c3d4" spellcheck="false" required');
  } else if (type === "mail") {
    title = "Add Mailing List Thread";
    body += field("tid", "Thread id", "", 'type="number" min="1"');
    body += field("mid", "Message-ID (focused message, or the thread it starts)", "", 'placeholder="20110101.12345@example.org" spellcheck="false"');
  } else if (type === "kconfig") {
    title = "Add Kconfig Symbol";
    body += field("symbol", "Symbol", "", 'placeholder="SMP" spellcheck="false" required data-ac="kconfig"');
    body += field("arch", "ARCH", "x86_64", 'spellcheck="false"');
  } else if (type === "maintainer") {
    title = "Add Maintainers Section";
    body += field("query", "Section name", "", 'placeholder="SCHEDULER" spellcheck="false"');
    body += field("path", "Or the section of a file", "", 'placeholder="kernel/sched/core.c" spellcheck="false"', "file");
    body += field("secId", "Or a section id", "", 'type="number" min="1"');
  } else if (type === "cve") {
    title = "Add CVE / Fix-Chain";
    body += field("cveId", "CVE ID", "", 'placeholder="CVE-2024-1234" spellcheck="false"');
    body += field("release", "Release state check (optional)", v, 'placeholder="v6.6" spellcheck="false"', "version");
    body += field("filePath", "Or all CVEs for a file", "", 'placeholder="mm/slub.c" spellcheck="false"', "file");
  } else if (type === "kapi") {
    title = "Add Kernel API / Boundary";
    body += `<label class="nm-form-row"><span>Catalog</span><select name="catalog">${M.KAPI_CATALOGS.map((c) => `<option value="${c}">${c}</option>`).join("")}</select></label>`;
    body += field("symbol", "Symbol / Name filter", "", 'placeholder="openat" spellcheck="false"');
    body += field("query", "Query", "", 'placeholder="pci" spellcheck="false"');
    body += field("arch", "ARCH", "x86_64", 'spellcheck="false"');
  } else if (type === "doc") {
    title = "Add Kernel Docs (.rst / kernel-doc)";
    body += field("path", "Documentation or source file", "", 'placeholder="Documentation/core-api/kernel-api.rst" spellcheck="false" required', "file");
    body += `<label class="nm-form-row"><span>Mode</span><select name="mode">${M.DOC_MODES.map((m) => `<option value="${m}">${m}</option>`).join("")}</select></label>`;
    body += field("functions", "kernel-doc filter (optional)", "", 'placeholder="kmalloc" spellcheck="false"');
  } else if (type === "history") {
    title = "Add Symbol History / Blame";
    body += `<label class="nm-form-row"><span>Mode</span><select name="mode"><option value="symbol">Symbol timeline</option><option value="blame">File blame commits</option></select></label>`;
    body += field("path", "File path", "", 'placeholder="kernel/fork.c" spellcheck="false" required', "file");
    body += `<div class="nm-form-pair">${field("line", "Line", "1", 'type="number" min="1"')}${field("name", "Symbol name (optional)", "", 'placeholder="copy_process" spellcheck="false" data-ac="symbol"')}</div>`;
  } else if (type === "tab") {
    title = "Add KernelInfo Tab";
    const repo = ctrl.repo;
    const types = EMBED_TAB_TYPES.filter(([t]) => isTabFeatureEnabled(repo, t));
    const open = state.panes.flatMap((p) => p.tabs).filter((t) => t.type !== "nodemap");
    body += `<label class="nm-form-row"><span>Open tab (embeds what it shows)</span><select name="openTab"><option value="">— a new view instead —</option>${open.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(`${t.title || t.type} · ${t.type}`)}</option>`).join("")}</select></label>`;
    body += `<label class="nm-form-row"><span>New view</span><select name="tabType">${types.map(([t, l]) => `<option value="${t}">${escapeHtml(l)}</option>`).join("")}</select></label>`;
    body += field("extra", "Address parameters (optional)", "", 'placeholder="file=kernel/fork.c&amp;line=120" spellcheck="false"');
  }
  const open = ctrl.openModalFn || openModal;
  const modal = open({ title, subtitle: "The node is placed at the center of the view", width: "480px", footerHtml: '<button type="button" class="code-btn primary" data-act="add">Add</button>' });
  modal.body.innerHTML = `<form class="nm-form">${body}</form>`;
  const form = modal.body.querySelector("form");
  wireFormPickers(form, dialogPickerContext(ctrl, form, type));
  form.querySelector("input[name='name'], input[name='path'], input[name='symbol'], input[name='tid'], input[name='query'], input[name='cveId'], input[name='commitHash']")?.focus?.();
  const submit = async () => {
    const f = Object.fromEntries(new FormData(form).entries());
    const spec = await specFromForm(ctrl, type, f);
    if (!spec) return;
    modal.close();
    ctrl.addNode(spec);
  };
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  modal.footer.querySelector("[data-act='add']").onclick = submit;
}

/**
 * Scope of a dialog's pickers: the repository of the NodeMap, the form's Revision (a file diff
 * browses the "From" tree for its old path and the "To" tree for its path), and its ARCH.
 */
export function dialogPickerContext(ctrl, form, type) {
  const val = (n) => String(form?.querySelector?.(`[name="${n}"]`)?.value || "").trim();
  return {
    client: ctrl.clientFor({}),
    version: () => val("version") || ctrl.currentVersion || "",
    arch: () => val("arch") || "x86_64",
    fileVersion: (name) => {
      if (type === "diff" && val("mode") !== "commit") return (name === "oldPath" ? val("versionA") : val("versionB")) || val("version");
      if (type === "cve" && name === "filePath") return val("release") || val("version");
      return val("version");
    },
    picker: ctrl.refPicker,
    openModalFn: ctrl.openModalFn,
  };
}

export async function specFromForm(ctrl, type, f) {
  const version = String(f.version || ctrl.currentVersion || "").trim();
  const int = (v) => (/^\d+$/.test(String(v || "").trim()) ? parseInt(v, 10) : null);
  if (type === "struct") {
    const name = String(f.name || "").trim().replace(/^(struct|union)\s+/, "");
    if (!/^[A-Za-z_]\w*$/.test(name)) return toast.warning("Enter a struct or union name"), null;
    return { type, version, data: { name, kind: f.kind, path: String(f.path || "").trim() } };
  }
  if (type === "source") {
    const path = String(f.path || "").trim();
    if (!path) return toast.warning("Enter a file path"), null;
    return { type, version, data: { path, mode: f.mode, start: int(f.start), end: int(f.end), symbol: String(f.symbol || "").trim(), symbolKind: f.symbolKind, context: int(f.context) || 0 } };
  }
  if (type === "symbol") {
    const name = String(f.name || "").trim();
    if (!name) return toast.warning("Enter a symbol name"), null;
    return { type, version, data: { name, kind: f.kind } };
  }
  if (type === "man") {
    const name = String(f.name || "").trim();
    if (!name) return toast.warning("Enter a man page name"), null;
    return { type, version, data: { name, section: String(f.section || "").trim() } };
  }
  if (type === "diff") {
    const commitHash = String(f.commitHash || "").trim();
    const path = String(f.path || "").trim();
    if (f.mode === "commit") {
      if (!/^[0-9a-f]{4,64}$/i.test(commitHash)) return toast.warning("Enter a commit hash"), null;
      return { type, version, data: { mode: "commit", commitHash, path } };
    }
    const versionA = String(f.versionA || "").trim();
    const versionB = String(f.versionB || version).trim();
    if (!path || !versionA || !versionB) return toast.warning("Enter a file and both revisions"), null;
    return { type, version: versionB, data: { mode: "file", versionA, versionB, path, oldPath: String(f.oldPath || "").trim() } };
  }
  if (type === "commit") {
    const spec = commitSpec({ commitHash: f.commitHash, version });
    if (!spec) return toast.warning("Enter a commit hash (4-40 hex digits)"), null;
    return spec;
  }
  if (type === "mail") {
    const tid = int(f.tid);
    const mid = String(f.mid || "").trim().replace(/^<|>$/g, "");
    if (!tid && !mid) return toast.warning("Enter a thread id or a Message-ID"), null;
    return { type, version, data: { tid, mid } };
  }
  if (type === "kconfig") {
    const symbol = String(f.symbol || "").trim().replace(/^CONFIG_/, "");
    if (!/^[A-Za-z0-9_]+$/.test(symbol)) return toast.warning("Enter a Kconfig symbol"), null;
    return { type, version, data: { symbol, arch: String(f.arch || "").trim() || "x86_64" } };
  }
  if (type === "maintainer") {
    const secId = int(f.secId);
    const query = String(f.query || "").trim();
    const path = String(f.path || "").trim();
    if (!secId && !query && !path) return toast.warning("Enter a section name, a file or a section id"), null;
    return { type, version, data: { secId, query, path } };
  }
  if (type === "cve") {
    const spec = cveSpec({ cveId: f.cveId, release: f.release, filePath: f.filePath, version });
    if (!spec) return toast.warning("Enter a CVE ID or a file path"), null;
    return spec;
  }
  if (type === "kapi") {
    return kapiSpec({ catalog: f.catalog, symbol: f.symbol, query: f.query, arch: f.arch, version });
  }
  if (type === "doc") {
    const spec = docSpec({ path: f.path, mode: f.mode, functions: f.functions, version });
    if (!spec) return toast.warning("Enter a documentation or source file path"), null;
    return spec;
  }
  if (type === "history") {
    const spec = historySpec({ mode: f.mode, path: f.path, line: int(f.line) || 1, name: f.name, version });
    if (!spec) return toast.warning("Enter a file path or symbol name"), null;
    return spec;
  }
  if (type === "tab") {
    if (f.openTab) {
      const t = state.findTab(f.openTab);
      if (!t) return null;
      return { type, version: t.version || version, repo: t.repo || ctrl.repo, title: t.title, data: { tabType: t.type, hash: urlSync.paramsForTab(t).toString() } };
    }
    const params = new URLSearchParams(String(f.extra || "").replace(/^[#?]/, ""));
    if (ctrl.repo) params.set("repo", ctrl.repo);
    if (!isAlwaysVersionlessTab(f.tabType) && version) params.set("v", version);
    params.set("tab", f.tabType);
    return { type, version, data: { tabType: f.tabType, hash: params.toString() } };
  }
  return null;
}

export function openImageExport(ctrl, kind) {
  const png = kind === "png";
  const { w, h } = svgSize(ctrl.canvasSvg({ foreign: false }));
  const open = ctrl.openModalFn || openModal;
  const modal = open({
    title: png ? "Export PNG Image" : "Export SVG Image",
    subtitle: `The whole canvas: ${w} × ${h} canvas units`,
    width: "420px",
    footerHtml: '<button type="button" class="code-btn primary" data-act="export">Export</button>',
  });
  const scales = PNG_SCALES.map((s) => `<option value="${s}"${s === PNG_DEFAULT_SCALE ? " selected" : ""}>${s}×</option>`).join("");
  modal.body.innerHTML = `<form class="nm-form">
      <label class="nm-insp-check"><input type="checkbox" name="transparent"/> <span>Transparent background</span></label>
      ${png ? `<label class="nm-form-row"><span>Scale (sharper text and lines at higher factors)</span><select name="scale">${scales}</select></label><div class="nm-export-size" aria-live="polite"></div>` : ""}
    </form>`;
  const q = (sel) => modal.body.querySelector(sel);
  const transEl = q("[name='transparent']");
  const scaleEl = q("[name='scale']");
  const sizeEl = q(".nm-export-size");
  const update = () => {
    if (!sizeEl) return;
    const z = pngSize(w, h, Number(scaleEl?.value) || PNG_DEFAULT_SCALE);
    sizeEl.textContent = `Output: ${z.w} × ${z.h} px${z.clamped ? " (reduced to the browser's canvas limit)" : ""}`;
  };
  scaleEl?.addEventListener?.("change", update);
  update();
  const submit = () => {
    const opts = { transparent: Boolean(transEl?.checked), scale: Number(scaleEl?.value) || PNG_DEFAULT_SCALE };
    modal.close();
    if (png) ctrl.exportPNG(opts);
    else ctrl.exportSVG(opts);
  };
  q("form")?.addEventListener?.("submit", (e) => {
    e.preventDefault();
    submit();
  });
  const btn = modal.footer?.querySelector?.("[data-act='export']");
  if (btn) btn.onclick = submit;
  return modal;
}

export function openFormatExportModal(ctrl, format) {
  const open = ctrl.openModalFn || openModal;
  if (format === "html") {
    const modal = open({
      title: "Export Interactive HTML",
      subtitle: "Self-contained pan/zoom viewer with embedded Code Tour player (offline file://)",
      width: "440px",
      footerHtml: '<button type="button" class="code-btn primary" data-act="export">Download .html</button>',
    });
    modal.body.innerHTML = `<form class="nm-form">
      <label class="nm-form-row"><span>Document title</span><input name="title" value="KernelInfo NodeMap" spellcheck="false"/></label>
      <label class="nm-insp-check"><input type="checkbox" name="transparent"/> <span>Transparent SVG background</span></label>
    </form>`;
    const submit = () => {
      const form = modal.body.querySelector("form");
      const title = form?.querySelector("[name='title']")?.value || "KernelInfo NodeMap";
      const transparent = Boolean(form?.querySelector("[name='transparent']")?.checked);
      modal.close();
      ctrl.exportInteractiveHTML({ title, transparent });
    };
    modal.body.querySelector("form")?.addEventListener?.("submit", (e) => {
      e.preventDefault();
      submit();
    });
    const btn = modal.footer?.querySelector?.("[data-act='export']");
    if (btn) btn.onclick = submit;
    return modal;
  }

  const isMermaid = format === "mermaid";
  const ext = isMermaid ? "mmd" : "dot";
  const label = isMermaid ? "Mermaid Flowchart (.mmd)" : "Graphviz DOT (.dot)";
  const modal = open({
    title: `Export ${label}`,
    subtitle: "Preserves visible layers, group subgraphs, lock badges, and semantic edges",
    width: "640px",
    footerHtml: '<button type="button" class="code-btn" data-act="copy">Copy to Clipboard</button><button type="button" class="code-btn primary" data-act="export">Download .' + ext + "</button>",
  });
  const buildText = (dir) => (isMermaid ? canvasToMermaid(ctrl.canvas, { direction: dir }) : canvasToDot(ctrl.canvas, { direction: dir }));
  modal.body.innerHTML = `<form class="nm-form">
    <label class="nm-form-row"><span>Layout direction</span><select name="direction"><option value="LR">Left → Right (LR)</option><option value="TB">Top → Bottom (TB)</option><option value="RL">Right → Left (RL)</option><option value="BT">Bottom → Top (BT)</option></select></label>
    <label class="nm-form-row"><span>Preview</span><textarea name="preview" class="nm-export-preview" rows="14" readonly spellcheck="false"></textarea></label>
  </form>`;
  const dirEl = modal.body.querySelector("[name='direction']");
  const preEl = modal.body.querySelector("[name='preview']");
  const refresh = () => {
    if (preEl) preEl.value = buildText(dirEl?.value || "LR");
  };
  dirEl?.addEventListener?.("change", refresh);
  refresh();

  const copyBtn = modal.footer?.querySelector?.("[data-act='copy']");
  if (copyBtn) {
    copyBtn.onclick = async () => {
      const ok = await copyToClipboard(preEl?.value || "");
      if (ok) toast.success(`Copied ${label} to clipboard`);
    };
  }
  const expBtn = modal.footer?.querySelector?.("[data-act='export']");
  if (expBtn) {
    expBtn.onclick = () => {
      const dir = dirEl?.value || "LR";
      modal.close();
      if (isMermaid) ctrl.exportMermaid({ direction: dir });
      else ctrl.exportDot({ direction: dir });
    };
  }
  return modal;
}

export function promptCallPath(ctrl, fromId) {
  const from = ctrl.byId(fromId);
  if (!from) return;
  const open = ctrl.openModalFn || openModal;
  const modal = open({
    title: `Find Call Path from ${from.data.name || M.displayTitle(from)}()`,
    width: "420px",
    footerHtml: '<button type="button" class="code-btn primary" data-act="find">Expand Paths</button>',
  });
  modal.body.innerHTML = `<form class="nm-form">
    <label class="nm-form-row"><span>Target function</span><input name="to" placeholder="schedule" required spellcheck="false" data-ac="symbol"/></label>
    <div class="nm-form-pair">
      <label class="nm-form-row"><span>Max depth</span><input name="maxDepth" type="number" min="1" max="12" value="6"/></label>
      <label class="nm-form-row"><span>Paths (k)</span><input name="k" type="number" min="1" max="10" value="3"/></label>
    </div>
  </form>`;
  const form = modal.body.querySelector("form");
  wireFormPickers(form, {
    client: ctrl.clientFor(from),
    version: () => from.version || ctrl.currentVersion || "",
    picker: ctrl.refPicker,
    openModalFn: ctrl.openModalFn,
  });
  form.querySelector("input[name='to']")?.focus?.();
  const submit = async () => {
    const f = Object.fromEntries(new FormData(form).entries());
    const target = String(f.to || "").trim();
    if (!target) return;
    modal.close();
    await ctrl.expandCallPath(fromId, target, {
      maxDepth: parseInt(f.maxDepth, 10) || 6,
      k: parseInt(f.k, 10) || 3,
    });
  };
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  const btn = modal.footer?.querySelector?.("[data-act='find']");
  if (btn) btn.onclick = submit;
}

export function promptAttachNode(ctrl, fromId, fieldKey = null) {
  const from = ctrl.byId(fromId);
  if (!from) return;
  const name = prompt(`Symbol or "struct name" to attach to "${M.displayTitle(from)}":`, "");
  if (!name || !name.trim()) return;
  const n = ctrl.nodeFromLegacySymbol({ name: name.trim(), type: /^(struct|union)\s/.test(name.trim()) ? "struct" : "symbol" });
  if (!n) return;
  n.x = from.x + from.w + 100;
  n.y = from.y;
  ctrl.canvas.nodes.push(n);
  const edge = M.createEdge({ from: from.id, to: n.id, fromField: fieldKey, fromSide: fieldKey ? "right" : "auto" });
  if (edge) ctrl.canvas.edges.push(edge);
  ctrl.selected = new Set([n.id]);
  ctrl.commit();
}

export function promptFieldLock(ctrl, node, fieldKey) {
  if (!node || node.type !== "struct" || !fieldKey) return null;
  const cur = node.data.fieldLocks?.[fieldKey] || null;
  const open = ctrl.openModalFn || openModal;
  const modal = open({
    title: `Locking Annotation · ${fieldKey}`,
    subtitle: `${node.data.kind} ${node.data.name}`,
    width: "420px",
    footerHtml: '<button type="button" class="code-btn" data-act="clear">Clear Lock</button><button type="button" class="code-btn primary" data-act="save">Save</button>',
  });
  modal.body.innerHTML = `<form class="nm-form">
    <label class="nm-form-row"><span>Lock / synchronization kind</span>
      <select name="kind">${M.LOCK_KINDS.map((k) => `<option value="${k}"${(cur?.kind || "rcu") === k ? " selected" : ""}>${k}</option>`).join("")}</select>
    </label>
    <label class="nm-form-row"><span>Lock symbol / guard expression (optional)</span>
      <input name="lockName" value="${escapeHtml(cur?.lockName || "")}" placeholder="e.g. mm->mmap_lock, tasklist_lock" spellcheck="false"/>
    </label>
  </form>`;
  const form = modal.body.querySelector("form");
  const apply = (lock) => {
    M.setFieldLock(node.data, fieldKey, lock);
    ctrl.renderer?.invalidate?.(node.id);
    if (node.autoFit) ctrl.fitQueue.add(node.id);
    modal.close();
    ctrl.commit();
  };
  const submit = () => {
    const kind = form?.querySelector("[name='kind']")?.value || "";
    const lockName = form?.querySelector("[name='lockName']")?.value || "";
    apply({ kind, lockName });
  };
  form?.addEventListener?.("submit", (e) => {
    e.preventDefault();
    submit();
  });
  const saveBtn = modal.footer?.querySelector?.("[data-act='save']");
  if (saveBtn) saveBtn.onclick = submit;
  const clearBtn = modal.footer?.querySelector?.("[data-act='clear']");
  if (clearBtn) clearBtn.onclick = () => apply(null);
  return modal;
}

export function openLibraryDialog(ctrl, initialTab = "library") {
  const open = ctrl.openModalFn || openModal;
  const modal = open({
    title: "NodeMap Library & Templates",
    subtitle: "Save named maps, launch kernel templates, or prune orphaned closed-tab caches",
    width: "700px",
  });
  let tab = initialTab === "templates" ? "templates" : "library";

  const refresh = async () => {
    const activeTabIds = collectReferencedTabIds({
      openPanes: state.panes || [],
      sessionsIndex: (ctrl.sessions || sessionManager).getSessionsIndex?.() || [],
      readSession: (sid) => (ctrl.sessions || sessionManager).readSession?.(sid),
      storage: globalThis.localStorage,
    });
    if (ctrl.tab?.id) activeTabIds.add(ctrl.tab.id);
    const [saved, orphans] = await Promise.all([
      listNamedCanvases(ctrl.userDb, {}, globalThis.localStorage),
      findOrphanedTabCanvases(ctrl.userDb, {
        activeTabIds,
        fallbackStorage: globalThis.localStorage,
      }),
    ]);

    const libHtml = `
      <form class="nm-form nm-lib-save-form">
        <div class="nm-insp-inline">
          <input name="name" placeholder="Name current canvas (e.g. VFS dentry locking)" spellcheck="false" required />
          <button type="submit" class="code-btn primary">Save Current As…</button>
        </div>
      </form>
      <div class="nm-lib-list">
        ${saved.length ? saved.map((rec) => `
          <div class="nm-lib-card" data-lib-key="${escapeHtml(rec.key)}">
            <div class="nm-lib-meta">
              <strong>${escapeHtml(rec.name)}</strong>
              <span class="nm-hint">${rec.nodeCount} nodes · ${rec.edgeCount} edges${rec.updated_at ? ` · ${escapeHtml(new Date(rec.updated_at).toLocaleDateString())}` : ""}</span>
            </div>
            <div class="nm-lib-acts">
              <button type="button" class="code-btn primary" data-lib-act="load" data-key="${escapeHtml(rec.key)}">Load</button>
              <button type="button" class="code-btn" data-lib-act="rename" data-key="${escapeHtml(rec.key)}">Rename</button>
              <button type="button" class="code-btn" data-lib-act="dup" data-key="${escapeHtml(rec.key)}">Duplicate</button>
              <button type="button" class="code-btn nm-danger" data-lib-act="del" data-key="${escapeHtml(rec.key)}">Delete</button>
            </div>
          </div>`).join("") : '<div class="nm-empty">No saved maps yet. Save the current canvas above or start from a Kernel Template.</div>'}
      </div>
      <div class="nm-lib-prune">
        <span class="nm-hint">${orphans.length} orphaned closed-tab cache${orphans.length === 1 ? "" : "s"} (&gt;14d unreferenced)</span>
        <button type="button" class="code-btn" data-lib-act="prune" ${orphans.length ? "" : "disabled"}>Prune unused tab caches</button>
      </div>`;

    const tplHtml = `
      <div class="nm-tpl-grid">
        ${KERNEL_TEMPLATES.map((t) => `
          <div class="nm-tpl-card">
            <div class="nm-tpl-head"><span>${escapeHtml(t.icon)} ${escapeHtml(t.name)}</span><span class="nm-badge">${escapeHtml(t.category)}</span></div>
            <p class="nm-tpl-desc">${escapeHtml(t.description)}</p>
            <button type="button" class="code-btn primary" data-lib-act="template" data-tpl="${escapeHtml(t.id)}">Use Template</button>
          </div>`).join("")}
      </div>`;

    modal.body.innerHTML = `
      <div class="nm-lib-tabs" role="tablist">
        <button type="button" class="nodemap-btn" role="tab" aria-selected="${tab === "library"}" data-lib-tab="library">Saved Maps (${saved.length})</button>
        <button type="button" class="nodemap-btn" role="tab" aria-selected="${tab === "templates"}" data-lib-tab="templates">Kernel Templates (${KERNEL_TEMPLATES.length})</button>
      </div>
      ${tab === "library" ? libHtml : tplHtml}`;

    modal.body.querySelector(".nm-lib-save-form")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const inp = ev.target.querySelector("[name='name']");
      const name = String(inp?.value || "").trim();
      if (!name) return;
      await saveNamedCanvas(ctrl.userDb, { name, repo: ctrl.repo, canvas: ctrl.rootCanvas || ctrl.canvas }, globalThis.localStorage);
      toast.success(`Saved map "${name}" to Library`);
      refresh();
    });

    modal.body.querySelectorAll("[data-lib-tab]").forEach((b) => {
      b.onclick = () => {
        tab = b.dataset.libTab;
        refresh();
      };
    });

    modal.body.querySelectorAll("[data-lib-act]").forEach((b) => {
      b.onclick = async () => {
        const act = b.dataset.libAct;
        const key = b.dataset.key;
        if (act === "load") {
          const loaded = await loadNamedCanvas(ctrl.userDb, key, globalThis.localStorage);
          if (loaded?.canvas) {
            ctrl.loadCanvasDocument(loaded.canvas);
            modal.close();
            toast.success(`Loaded "${loaded.name}"`);
          }
        } else if (act === "rename") {
          const curName = b.closest(".nm-lib-card")?.querySelector("strong")?.textContent || "";
          const next = prompt("Rename saved map:", curName);
          if (next && next.trim()) {
            await renameNamedCanvas(ctrl.userDb, key, next.trim(), globalThis.localStorage);
            refresh();
          }
        } else if (act === "dup") {
          await duplicateNamedCanvas(ctrl.userDb, key, "", globalThis.localStorage);
          refresh();
        } else if (act === "del") {
          await deleteNamedCanvas(ctrl.userDb, key, globalThis.localStorage);
          refresh();
        } else if (act === "prune") {
          const res = await ctrl.pruneOrphanedCanvases();
          toast.success(`Pruned ${res.prunedCount} orphaned tab cache${res.prunedCount === 1 ? "" : "s"}`);
          refresh();
        } else if (act === "template") {
          const tplCanvas = buildKernelTemplate(b.dataset.tpl, { version: ctrl.currentVersion || "v3.0", repo: ctrl.repo || "linux" });
          if (tplCanvas) {
            ctrl.loadCanvasDocument(tplCanvas);
            modal.close();
            toast.success("Loaded kernel template");
          }
        }
      };
    });
  };

  refresh();
  return modal;
}

export function openImpactMapDialog(ctrl) {
  const open = ctrl.openModalFn || openModal;
  const modal = open({
    title: "Build Patch / Commit Impact Map",
    subtitle: "Generates a layered Changes → Touched Structs/Functions → Callers & Maintainers map",
    width: "520px",
    footerHtml: '<button type="button" class="code-btn primary" data-act="build">Build Impact Map</button>',
  });
  modal.body.innerHTML = `<form class="nm-form">
    <label class="nm-form-row"><span>Revision</span><div class="nm-pick-wrap"><input name="version" value="${escapeHtml(ctrl.currentVersion || "v3.0")}" spellcheck="false"/>${pickButtonHtml("version", "version")}</div></label>
    <label class="nm-form-row"><span>Commit hash (auto-fetches commit diff, structs, callers &amp; maintainers)</span><input name="commitHash" placeholder="e.g. a1b2c3d4e5f6" spellcheck="false"/></label>
    <label class="nm-form-row"><span>Or modified files / unified diff (one file path per line or raw diff)</span><textarea name="diffOrFiles" rows="5" placeholder="kernel/sched/core.c&#10;include/linux/sched.h" spellcheck="false"></textarea></label>
  </form>`;
  const form = modal.body.querySelector("form");
  wireFormPickers(form, { client: ctrl.clientFor({}), version: () => form?.querySelector?.("[name='version']")?.value || ctrl.currentVersion || "", picker: ctrl.refPicker, openModalFn: ctrl.openModalFn });
  const submit = async () => {
    const f = Object.fromEntries(new FormData(form).entries());
    const version = String(f.version || ctrl.currentVersion || "v3.0").trim();
    const commitHash = String(f.commitHash || "").trim();
    const raw = String(f.diffOrFiles || "").trim();
    const looksLikeDiff = /^(diff --git|---|@@)/m.test(raw);
    const files = !looksLikeDiff && raw
      ? raw.split(/[\r\n,]+/).map((p) => p.trim()).filter(Boolean).map((path) => ({ path }))
      : [];
    if (!commitHash && !raw) {
      toast.warning("Enter a commit hash or modified file path(s)");
      return;
    }
    const btn = modal.footer?.querySelector?.("[data-act='build']");
    if (btn) btn.disabled = true;
    try {
      const baseApi = ctrl.api || api;
      const client = typeof baseApi.forRepo === "function" && ctrl.repo ? baseApi.forRepo(ctrl.repo) : baseApi;
      const mapCanvas = await generateImpactMap({
        api: client,
        version,
        repo: ctrl.repo || "linux",
        commitHash,
        files,
        diffText: looksLikeDiff ? raw : "",
      });
      modal.close();
      ctrl.loadCanvasDocument(mapCanvas);
      toast.success(`Generated Impact Map (${mapCanvas.nodes.length} nodes)`);
    } catch (err) {
      toast.error(`Impact Map failed: ${err.message}`);
      if (btn) btn.disabled = false;
    }
  };
  form?.addEventListener?.("submit", (e) => {
    e.preventDefault();
    submit();
  });
  const btn = modal.footer?.querySelector?.("[data-act='build']");
  if (btn) btn.onclick = submit;
  return modal;
}

export function openShortcutsDialog(ctrl) {
  const open = ctrl.openModalFn || openModal;
  const modal = open({
    title: "NodeMap Shortcuts & Gestures",
    subtitle: "Press ? anywhere on the canvas to open this reference",
    width: "660px",
  });
  modal.body.innerHTML = `<div class="nm-shortcuts-grid">${SHORTCUT_SECTIONS.map((sec) => `
    <section class="nm-sc-section">
      <h4>${escapeHtml(sec.title)}</h4>
      <div class="nm-sc-list">
        ${sec.items.map(([k, desc]) => `<div class="nm-sc-row"><kbd class="nm-kbd">${escapeHtml(k)}</kbd><span>${escapeHtml(desc)}</span></div>`).join("")}
      </div>
    </section>`).join("")}</div>`;
  return modal;
}
