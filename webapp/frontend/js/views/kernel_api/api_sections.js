/**
 * api_sections.js - Kernel API tab sections (webapp/API.md §5.29): Syscalls (A1), Exports (A2),
 * Parameters (A3), Tracepoints (A4), Modules (A5).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";
import { registerApiSection } from "./kernel_api_view.js";
import { toTsv } from "../sections/section_model.js";
import { configLookup, evalBuild } from "../kconfig/build_condition.js";
import { openDefinition } from "../../utils/definition_picker.js";
import { showSymbolHistoryModal } from "../../components/symbol_history_modal.js";

const fileLink = (path, line, label = null, version = "") =>
  `<a href="#" data-open-file="${escapeHtml(path)}" data-line="${line || 1}"${version ? ` data-version="${escapeHtml(version)}"` : ""}>${escapeHtml(label || `${path}:${line || 1}`)}</a>`;

/** Value of a Kbuild condition under the KConfig engine running for this revision and ARCH ("" without one). */
export function builtUnderEngine(expr, version, arch) {
  const engine = state.getEngine(state.defaultEngineTabIdFor({ version }));
  if (!engine || (arch && engine.arch !== arch) || !state.kconfigConfigActive(engine.tabId)) return "";
  try {
    return evalBuild(expr, configLookup({ assignments: engine.assignments || {} }));
  } catch {
    return "";
  }
}

function exprText(e) {
  if (typeof e === "string") return e;
  if (!e || typeof e !== "object") return String(e);
  if (e.sym) return `CONFIG_${e.sym}`;
  if (e.def) return `defined(CONFIG_${e.def})`;
  if (e.not) return `!(${exprText(e.not)})`;
  if (e.and) return e.and.map(exprText).join(" && ");
  if (e.or) return `(${e.or.map(exprText).join(" || ")})`;
  if (e.eq) return `${JSON.stringify(e.eq[0])} == ${JSON.stringify(e.eq[1])}`;
  return JSON.stringify(e);
}

const optionLinks = (opts, version) => (opts || []).map((o) => `<a href="#" data-kconfig="${escapeHtml(o)}" data-version="${escapeHtml(version)}">CONFIG_${escapeHtml(o)}</a>`).join(", ");

function commonClicks(e, ctx) {
  const nt = ctx.forceNew || e.ctrlKey || e.metaKey;
  const k = e.target.closest("[data-kconfig]");
  if (k) {
    e.preventDefault();
    // Opening the KConfig view on an option changes no value: no §5.0 dialog
    const arch = ctx.arch || state.kconfigArch();
    state.openTab({ type: "kconfig", title: `KConfig (${arch})`, arch, symbol: k.dataset.kconfig, version: ctx.version, forceNew: nt });
    return true;
  }
  const def = e.target.closest("[data-definition]");
  if (def) {
    e.preventDefault();
    openDefinition(ctx.version, def.dataset.definition, { forceNew: nt });
    return true;
  }
  const hist = e.target.closest("[data-history-path]");
  if (hist) {
    e.preventDefault();
    showSymbolHistoryModal(ctx.version, hist.dataset.historyPath, parseInt(hist.dataset.historyLine, 10) || 1);
    return true;
  }
  const cg = e.target.closest("[data-callgraph]");
  if (cg) {
    e.preventDefault();
    state.openTab({ type: "callgraph", title: `Callgraph: ${cg.dataset.callgraph}`, functionName: cg.dataset.callgraph, version: ctx.version, forceNew: nt });
    return true;
  }
  return false;
}

// ------------------------------------------------------------------ A1 syscalls
registerApiSection({
  id: "syscalls", label: "Syscalls", order: 10, archScoped: true, keywords: "system calls syscall table",
  hint: "System call numbers, entry points and SYSCALL_DEFINE signatures",
  async fetchList(ctx) {
    const r = await api.getKapi(ctx.version, `syscalls?arch=${encodeURIComponent(ctx.arch)}`, { ...ctx, cacheKey: `kapi_sys_${ctx.arch}` });
    const rows = [...r.syscalls.map((s) => ({ ...s, key: `${s.abi}:${s.nr}:${s.name}` })),
      ...r.unnumbered.map((u) => ({ nr: null, abi: "", name: u.name, entry: `sys_${u.name}`, status: "unnumbered", definition: u, key: `u:${u.name}` }))];
    return { rows, note: `from ${r.sources.map((s) => s.path).join(", ") || "no table found"}` };
  },
  rowKey: (r) => r.key,
  rowText: (r) => `${r.nr ?? ""} ${r.name} ${r.entry} ${(r.definition?.args || []).map((a) => a.type).join(" ")}`,
  rowHtml: (r) => `<span class="sv-row-sub" style="min-width:34px;text-align:right">${r.nr ?? "—"}</span><span class="sv-row-main">${escapeHtml(r.name)}<span class="sv-row-sub"> (${(r.definition?.args || []).length})</span></span>`
    + `${r.abi ? `<span class="sv-tag info">${escapeHtml(r.abi)}</span>` : ""}${r.status !== "defined" ? `<span class="sv-tag ${r.status === "ni_syscall" ? "warn" : "bad"}">${escapeHtml(r.status.replace("_", " "))}</span>` : ""}`,
  chips: [
    { id: "defined", label: "defined", test: (r) => r.status === "defined" },
    { id: "ni", label: "sys_ni_syscall", test: (r) => r.status === "ni_syscall" },
    { id: "nodef", label: "no definition", test: (r) => r.status === "no_definition" },
    { id: "unnumbered", label: "unnumbered", test: (r) => r.status === "unnumbered" },
  ],
  detailHtml: (r, ctx) => {
    const d = r.definition;
    const sig = d ? `long ${r.entry}(${(d.args || []).map((a) => `${a.type} ${a.name}`).join(", ") || "void"})` : "";
    return `<h2>${escapeHtml(r.name)}</h2>
      <dl class="sv-kv"><dt>Number</dt><dd>${r.nr ?? "none on this ARCH"} ${r.abi ? `(${escapeHtml(r.abi)})` : ""}</dd>
      <dt>Entry point</dt><dd><code>${escapeHtml(r.entry)}</code>${r.compat_entry ? ` · compat <code>${escapeHtml(r.compat_entry)}</code>` : ""}</dd>
      <dt>Status</dt><dd>${escapeHtml(r.status)}</dd>
      ${d ? `<dt>Definition</dt><dd>${fileLink(d.path, d.line_s, null, ctx.version)} <code>${escapeHtml(d.macro)}</code></dd>` : ""}</dl>
      ${sig ? `<pre>${escapeHtml(sig)}</pre>` : ""}
      <p>${d ? `<a href="#" data-definition="${escapeHtml(r.entry)}">Go to definition</a> · <a href="#" data-history-path="${escapeHtml(d.path)}" data-history-line="${d.line_s}">History</a> · <a href="#" data-callgraph="${escapeHtml(r.entry)}">Callers</a> · ` : ""}
      <a href="https://man7.org/linux/man-pages/man2/${encodeURIComponent(r.name)}.2.html" target="_blank" rel="noopener noreferrer">man 2 ${escapeHtml(r.name)} ↗</a></p>`;
  },
  onDetailClick: commonClicks,
  downloads: [{ label: "Syscalls", ext: "tsv", build: (rows) => toTsv(rows, [["nr", (r) => r.nr ?? ""], ["abi", (r) => r.abi], ["name", (r) => r.name], ["entry", (r) => r.entry],
    ["status", (r) => r.status], ["definition", (r) => (r.definition ? `${r.definition.path}:${r.definition.line_s}` : "")]]) }],
});

// ------------------------------------------------------------------ A2 exports
const VARIANT = { plain: "EXPORT_SYMBOL", gpl: "GPL", gpl_future: "GPL_FUTURE", ns: "NS", ns_gpl: "NS_GPL", unused: "UNUSED", unused_gpl: "UNUSED_GPL" };
registerApiSection({
  id: "exports", label: "Exports", order: 20, keywords: "export symbol gpl module api",
  hint: "EXPORT_SYMBOL* of the revision and the object that exports them",
  async fetchList(ctx) {
    const r = await api.getKapi(ctx.version, "exports", { ...ctx, cacheKey: "kapi_exports" });
    return { rows: r.exports, note: `${r.total.toLocaleString("en-US")} exports · ${(r.counts.gpl || 0).toLocaleString("en-US")} GPL-only` };
  },
  rowKey: (r) => `${r.symbol}@${r.path}:${r.line}`,
  rowText: (r) => `${r.symbol} ${r.namespace} ${r.path}`,
  rowHtml: (r) => `<span class="sv-row-main">${escapeHtml(r.symbol)}</span>${r.variant !== "plain" ? `<span class="sv-tag ${r.variant.includes("gpl") ? "warn" : "info"}">${escapeHtml(VARIANT[r.variant] || r.variant)}</span>` : ""}${r.namespace ? `<span class="sv-tag info">${escapeHtml(r.namespace)}</span>` : ""}<span class="sv-row-sub">${escapeHtml(r.path.split("/").slice(-2).join("/"))}</span>`,
  chips: Object.entries(VARIANT).map(([id, label]) => ({ id, label, test: (r) => r.variant === id })),
  fetchDetail: (ctx, key, row) => api.request(`/api/kapi/${encodeURIComponent(ctx.version)}/exports/${encodeURIComponent(row?.symbol || key.split("@")[0])}?arch=${encodeURIComponent(state.kconfigArch())}`),
  detailHtml: (d, ctx) => `<h2>${escapeHtml(d.symbol)}</h2>
    <h3>Exported at</h3>${d.sites.map((s) => {
      const built = s.owner ? builtUnderEngine(s.owner.expr, ctx.version, "") : "";
      return `<div>${fileLink(s.path, s.line, null, ctx.version)} <span class="sv-tag info">${escapeHtml(VARIANT[s.variant])}</span>${s.namespace ? ` namespace <code>${escapeHtml(s.namespace)}</code>` : ""}
        <div class="sv-row-sub">${s.owner ? `${escapeHtml(s.owner.modname)} (<code>${escapeHtml(s.owner.target)}</code>) when <code>${escapeHtml(exprText(s.owner.expr))}</code>${built ? ` · built: <b>${escapeHtml(built)}</b>` : ""}` : "no Kbuild object found for this file"}</div></div>`;
    }).join("")}
    <p><a href="#" data-definition="${escapeHtml(d.symbol)}">Go to definition</a> · <a href="#" data-symbol="${escapeHtml(d.symbol)}">Search uses</a></p>`,
  onDetailClick: commonClicks,
  downloads: [
    { label: "Symbols", ext: "txt", build: (rows) => [...new Set(rows.map((r) => r.symbol))].join("\n") + "\n" },
    { label: "Exports", ext: "tsv", build: (rows) => toTsv(rows, [["symbol", (r) => r.symbol], ["variant", (r) => r.variant], ["namespace", (r) => r.namespace], ["path", (r) => `${r.path}:${r.line}`]]) },
  ],
});

// ------------------------------------------------------------------ A3 parameters
registerApiSection({
  id: "params", label: "Parameters", order: 30, archScoped: true, keywords: "boot command line module parameters setup",
  hint: "Boot (__setup, early_param) and module parameters with their documentation",
  async fetchList(ctx) {
    const r = await api.getKapi(ctx.version, `params?arch=${encodeURIComponent(ctx.arch)}`, { ...ctx, cacheKey: `kapi_params_${ctx.arch}` });
    this._doc = r.doc_path;
    const stale = r.stale_docs.map((s) => ({ kind: "stale", name: s.name, full_name: s.name, doc: { line: s.line, tags: s.tags }, path: r.doc_path, line: s.line, key: `stale:${s.name}` }));
    return { rows: [...r.params.map((p) => ({ ...p, key: `${p.kind}:${p.full_name}:${p.path}:${p.line}` })), ...stale], note: r.doc_path ? `documentation: ${r.doc_path}` : "no kernel-parameters.txt" };
  },
  rowKey: (r) => r.key,
  rowText: (r) => `${r.full_name} ${r.kind} ${r.type || ""} ${r.path}`,
  rowHtml: (r) => `<span class="sv-tag info">${escapeHtml(r.kind)}</span><span class="sv-row-main">${escapeHtml(r.full_name)}${r.takes_value && r.kind !== "module" ? "=" : ""}</span>${r.undocumented ? '<span class="sv-tag warn">undocumented</span>' : ""}${r.kind === "stale" ? '<span class="sv-tag bad">stale doc?</span>' : ""}<span class="sv-row-sub">${escapeHtml(r.type || "")}</span>`,
  chips: [
    { id: "setup", label: "boot", test: (r) => r.kind === "setup" }, { id: "early", label: "early", test: (r) => r.kind === "early" },
    { id: "core", label: "core", test: (r) => r.kind === "core" }, { id: "module", label: "module", test: (r) => r.kind === "module" },
    { id: "undocumented", label: "undocumented", test: (r) => r.undocumented, title: "boot parameters with no entry in kernel-parameters.txt" },
    { id: "stale", label: "stale docs", test: (r) => r.kind === "stale", title: "documented names no definition matches (heuristic: names can be built by macros)" },
  ],
  async fetchDetail(ctx, key, row) {
    if (!row) return null;
    const doc = row.doc ? await api.request(`/api/kapi/${encodeURIComponent(ctx.version)}/params/doc?line=${row.doc.line}`).catch(() => null) : null;
    return { ...row, docText: doc };
  },
  detailHtml: (r, ctx) => (!r ? '<div class="sv-empty">Not in this list.</div>' : `<h2>${escapeHtml(r.full_name)}</h2>
    <dl class="sv-kv"><dt>Kind</dt><dd>${escapeHtml(r.kind)}</dd>
    ${r.type ? `<dt>Type</dt><dd><code>${escapeHtml(r.type)}</code></dd>` : ""}${r.perm ? `<dt>Permissions</dt><dd><code>${escapeHtml(r.perm)}</code></dd>` : ""}
    ${r.handler ? `<dt>Handler</dt><dd><a href="#" data-definition="${escapeHtml(r.handler)}">${escapeHtml(r.handler)}</a></dd>` : ""}
    ${r.description ? `<dt>Description</dt><dd>${escapeHtml(r.description)}</dd>` : ""}
    <dt>Defined in</dt><dd>${fileLink(r.path, r.line, null, ctx.version)}</dd></dl>
    ${r.snippet ? `<pre>${escapeHtml(r.snippet)}</pre>` : ""}
    ${r.docText ? `<h3>Documentation ${fileLink(r.docText.path, r.docText.line, `${r.docText.path}:${r.docText.line}`, ctx.version)}</h3><pre>${escapeHtml(r.docText.text)}</pre>` : (r.kind !== "module" ? '<p class="sv-empty">No entry in kernel-parameters.txt.</p>' : "")}`),
  onDetailClick: commonClicks,
  downloads: [{ label: "Parameters", ext: "tsv", build: (rows) => toTsv(rows, [["kind", (r) => r.kind], ["name", (r) => r.full_name], ["type", (r) => r.type || ""], ["perm", (r) => r.perm || ""], ["path", (r) => `${r.path}:${r.line}`], ["documented", (r) => (r.doc ? "yes" : "no")]]) }],
});

// ------------------------------------------------------------------ A4 tracepoints
registerApiSection({
  id: "tracepoints", label: "Tracepoints", order: 40, keywords: "trace events tracepoint ftrace",
  hint: "TRACE_EVENT definitions: prototype, fields, format and call sites",
  async fetchList(ctx) {
    const r = await api.getKapi(ctx.version, "tracepoints", { ...ctx, cacheKey: "kapi_tp" });
    const rows = [...r.tracepoints].sort((a, b) => a.system.localeCompare(b.system) || a.name.localeCompare(b.name));
    return { rows, note: `${new Set(rows.map((t) => t.system)).size} systems` };
  },
  rowKey: (r) => r.name,
  rowText: (r) => `${r.system} ${r.name} ${r.class}`,
  rowHtml: (r) => `<span class="sv-row-sub" style="min-width:90px">${escapeHtml(r.system)}</span><span class="sv-row-main">${escapeHtml(r.name)}</span>${r.kind !== "event" ? `<span class="sv-tag info">${escapeHtml(r.kind)}</span>` : ""}`,
  chips: [{ id: "event", label: "events", test: (r) => r.kind === "event" }, { id: "class", label: "classes", test: (r) => r.kind === "class" }, { id: "raw", label: "raw", test: (r) => r.kind === "raw" }],
  async fetchDetail(ctx, key, row) {
    const sites = row?.kind === "class" ? null : await api.request(`/api/kapi/${encodeURIComponent(ctx.version)}/tracepoints/${encodeURIComponent(key)}`).catch(() => null);
    return { ...(row || { name: key }), sites };
  },
  detailHtml: (t, ctx) => `<h2>${escapeHtml(t.name)}</h2>
    <dl class="sv-kv"><dt>System</dt><dd>${escapeHtml(t.system || "")}</dd><dt>Defined</dt><dd>${t.path ? fileLink(t.path, t.line, null, ctx.version) : ""} <code>${escapeHtml(t.macro || "")}</code></dd>
    ${t.class ? `<dt>Class</dt><dd>${escapeHtml(t.class)}</dd>` : ""}${t.kind === "event" ? `<dt>tracefs</dt><dd><code>events/${escapeHtml(t.system)}/${escapeHtml(t.name)}/</code></dd>` : ""}</dl>
    ${t.proto ? `<h3>Prototype</h3><pre>${escapeHtml(t.proto)}</pre>` : ""}
    ${(t.fields || []).length ? `<h3>Fields</h3><table>${t.fields.map((f) => `<tr><td><code>${escapeHtml(f.type)}</code></td><td>${escapeHtml(f.name)}</td><td class="sv-row-sub">${escapeHtml(f.kind)}${f.size ? ` [${escapeHtml(f.size)}]` : ""}</td></tr>`).join("")}</table>` : ""}
    ${t.printk ? `<h3>Format</h3><pre>${escapeHtml(t.printk)}</pre>` : ""}
    ${t.sites ? `<h3>Call sites (${t.sites.call_sites.length}${t.sites.truncated ? "+" : ""})</h3>${t.sites.call_sites.map((s) => `<div>${fileLink(s.path, s.line, null, ctx.version)} <code class="sv-row-sub">${escapeHtml(s.text)}</code></div>`).join("") || '<p class="sv-empty">No caller found.</p>'}` : ""}`,
  downloads: [{ label: "Tracepoints", ext: "tsv", build: (rows) => toTsv(rows, [["system", (r) => r.system], ["name", (r) => r.name], ["kind", (r) => r.kind], ["class", (r) => r.class], ["path", (r) => `${r.path}:${r.line}`]]) }],
});

// ------------------------------------------------------------------ A5 modules
const licenseOf = (m) => (!m.license ? "missing" : /^GPL( v2)?$|^GPL v2$/.test(m.license) ? "gpl" : /dual|\//i.test(m.license) ? "dual" : "other");
registerApiSection({
  id: "modules", label: "Modules", order: 50, archScoped: true, keywords: "modules ko driver license firmware",
  hint: "Loadable modules: options, license, description, firmware, device tables",
  async fetchList(ctx) {
    const r = await api.getKapi(ctx.version, `modules?arch=${encodeURIComponent(ctx.arch)}`, { ...ctx, cacheKey: `kapi_mod_${ctx.arch}` });
    const rows = r.modules.map((m) => ({ ...m, built: builtUnderEngine(m.expr, ctx.version, ctx.arch) }));
    const engineNote = rows.some((m) => m.built) ? " · built: values from the KConfig engine of this revision" : "";
    return { rows, note: `ARCH=${r.arch}${engineNote}` };
  },
  rowKey: (m) => m.modname,
  rowText: (m) => `${m.modname} ${m.options.join(" ")} ${m.description} ${m.license}`,
  rowHtml: (m) => `<span class="sv-row-main">${escapeHtml(m.modname)} <span class="sv-row-sub">${escapeHtml(m.description.slice(0, 60))}</span></span>${m.built ? `<span class="sv-tag ${m.built === "m" ? "ok" : m.built === "y" ? "info" : "bad"}">${escapeHtml(m.built)}</span>` : ""}<span class="sv-row-sub">${escapeHtml(m.license || "no license")}</span>`,
  chips: [
    { id: "gpl", label: "GPL", test: (m) => licenseOf(m) === "gpl" }, { id: "dual", label: "dual", test: (m) => licenseOf(m) === "dual" },
    { id: "other", label: "other license", test: (m) => licenseOf(m) === "other" }, { id: "nolicense", label: "no license", test: (m) => licenseOf(m) === "missing" },
    { id: "firmware", label: "firmware", test: (m) => m.firmware.length > 0 }, { id: "devtable", label: "device table", test: (m) => m.device_tables.length > 0 },
    { id: "asmodule", label: "built as module", test: (m) => m.built === "m" }, { id: "notbuilt", label: "not built", test: (m) => m.built === "n" },
  ],
  fetchDetail: (ctx, key) => api.request(`/api/kapi/${encodeURIComponent(ctx.version)}/modules/${encodeURIComponent(key)}?arch=${encodeURIComponent(ctx.arch)}`, { timeoutMs: 60000 }),
  detailHtml: (m, ctx) => `<h2>${escapeHtml(m.modname)}.ko</h2>
    <dl class="sv-kv"><dt>Object</dt><dd><code>${escapeHtml(m.target)}</code></dd>
    <dt>Built when</dt><dd><code>${escapeHtml(exprText(m.expr))}</code>${builtUnderEngine(m.expr, ctx.version, ctx.arch) ? ` · built: <b>${escapeHtml(builtUnderEngine(m.expr, ctx.version, ctx.arch))}</b>` : ""}</dd>
    <dt>Options</dt><dd>${optionLinks(m.options, ctx.version) || "—"}</dd>
    <dt>License</dt><dd>${escapeHtml(m.license || "missing")}</dd>
    ${m.description ? `<dt>Description</dt><dd>${escapeHtml(m.description)}</dd>` : ""}
    ${m.authors.length ? `<dt>Authors</dt><dd>${m.authors.map(escapeHtml).join("<br>")}</dd>` : ""}
    ${m.init ? `<dt>Init</dt><dd><a href="#" data-callgraph="${escapeHtml(m.init.replace(/^.*\(|\)$/g, ""))}">${escapeHtml(m.init)}</a> (callgraph)</dd>` : ""}
    ${m.firmware.length ? `<dt>Firmware</dt><dd>${m.firmware.map(escapeHtml).join("<br>")}</dd>` : ""}
    ${m.aliases.length ? `<dt>Aliases</dt><dd>${m.aliases.slice(0, 20).map(escapeHtml).join("<br>")}</dd>` : ""}</dl>
    <h3>Sources</h3>${m.members.map((p) => `<div>${fileLink(p, 1, p, ctx.version)}</div>`).join("")}
    ${m.device_tables.length ? `<h3>Device tables</h3>${m.device_tables.map((t) => `<div>${escapeHtml(t.bus)}: ${fileLink(t.path, t.line, t.table, ctx.version)} <a href="#" data-devtable="${escapeHtml(t.table)}">devices</a></div>`).join("")}` : ""}
    ${m.params?.length ? `<h3>Parameters</h3><table>${m.params.map((p) => `<tr><td>${escapeHtml(p.full_name)}</td><td><code>${escapeHtml(p.type)}</code></td><td>${escapeHtml(p.description || "")}</td></tr>`).join("")}</table>` : ""}
    ${m.exports?.length ? `<h3>Exports (${m.exports.length})</h3><div>${m.exports.slice(0, 100).map((e) => `<code>${escapeHtml(e.symbol)}</code>`).join(" ")}</div>` : ""}
    ${(m.maintainers || []).length ? `<h3>Maintainers</h3>${m.maintainers.slice(0, 8).map((s) => `<div><a href="#" data-maint="${escapeHtml(s.sec_id ?? "")}">${escapeHtml(s.name || s.section || "")}</a></div>`).join("")}` : ""}`,
  onDetailClick(e, ctx) {
    if (commonClicks(e, ctx)) return true;
    const mt = e.target.closest("[data-maint]");
    if (mt && mt.dataset.maint) {
      e.preventDefault();
      state.openTab({ type: "maintainers", title: "Maintainers", secId: parseInt(mt.dataset.maint, 10), version: ctx.version, forceNew: ctx.forceNew || e.ctrlKey || e.metaKey });
      return true;
    }
    const dt = e.target.closest("[data-devtable]");
    if (dt) {
      e.preventDefault();
      state.openTab({ type: "api", title: "Kernel API · Devices", view: "devices", query: dt.dataset.devtable, arch: ctx.arch, version: ctx.version, forceNew: ctx.forceNew || e.ctrlKey || e.metaKey });
      return true;
    }
    return false;
  },
  downloads: [{ label: "Modules", ext: "tsv", build: (rows) => toTsv(rows, [["module", (m) => m.modname], ["target", (m) => m.target], ["options", (m) => m.options.join(" ")], ["license", (m) => m.license], ["built", (m) => m.built], ["description", (m) => m.description]]) }],
});

export { exprText };
