/**
 * devices_section.js - Kernel API · Devices (N4, webapp/API.md §5.29): which driver handles a piece of
 * hardware. Paste `lspci -nn`, `lsusb`, modalias, compatible or ACPI ids; browse the device tables.
 */
import { api } from "../../api.js";
import { escapeHtml } from "../../components/modal.js";
import { toast } from "../../components/toast.js";
import { registerApiSection } from "./kernel_api_view.js";
import { builtUnderEngine } from "./api_sections.js";
import { toTsv } from "../sections/section_model.js";

const PASTE = `<div class="dev-paste"><h3>Find drivers</h3>
  <textarea class="dev-input" rows="5" spellcheck="false" placeholder="Paste lspci -nn, lsusb or modalias lines, vendor:device ids, compatible strings or ACPI ids…"></textarea>
  <button type="button" class="code-btn dev-find">Find drivers</button>
  <div class="dev-results"></div></div>`;

function resultsHtml(res, ctx) {
  if (!res.lines.length) return '<p class="sv-empty">No device id recognized in the input.</p>';
  return res.lines.map((l) => `<div class="dev-line"><div class="dev-input-line"><code>${escapeHtml(l.input)}</code> <span class="sv-row-sub">${escapeHtml(l.ids.join(", "))}</span></div>
    ${l.matches.length ? l.matches.map((m) => {
      const built = m.expr ? builtUnderEngine(m.expr, ctx.version, ctx.arch) : "";
      return `<div class="dev-match"><span class="sv-tag ${m.match_kind === "exact" ? "ok" : "info"}">${escapeHtml(m.match_kind)}</span>
        <b>${escapeHtml(m.module || "(not a module of this ARCH)")}</b>
        ${m.options.map((o) => `<a href="#" data-kconfig="${escapeHtml(o)}">CONFIG_${escapeHtml(o)}</a>`).join(" ")}
        <a href="#" data-open-file="${escapeHtml(m.path)}" data-line="${m.line}">${escapeHtml(m.table)}</a>
        ${built ? `<span class="sv-tag ${built === "n" ? "bad" : "ok"}">built: ${escapeHtml(built)}</span>` : ""}</div>`;
    }).join("") : '<div class="sv-empty">No driver of this revision lists it.</div>'}</div>`).join("");
}

registerApiSection({
  id: "devices", label: "Devices", order: 60, archScoped: true, keywords: "hardware driver pci usb lspci modalias device table",
  hint: "Which driver handles a device: paste lspci -nn / lsusb / modalias",
  async fetchList(ctx) {
    const r = await api.getKapi(ctx.version, `devices?arch=${encodeURIComponent(ctx.arch)}`, { ...ctx, cacheKey: "kapi_devices" });
    return { rows: r.entries, note: `${r.tables.toLocaleString("en-US")} device tables`, summaryHtml: PASTE };
  },
  rowKey: (e) => `${e.path}:${e.table}:${e.id}:${e.text}`,
  rowText: (e) => `${e.bus} ${e.id} ${e.table} ${e.path}`,
  rowHtml: (e) => `<span class="sv-tag info">${escapeHtml(e.bus)}</span><code class="sv-row-main">${escapeHtml(e.id)}</code><span class="sv-row-sub">${escapeHtml(e.table)}</span>`,
  chips: ["pci", "usb", "of", "acpi", "i2c", "platform", "pcmcia", "pnp"].map((b) => ({ id: b, label: b, test: (e) => e.bus === b })),
  detailHtml: (e, ctx) => `<h2>${escapeHtml(e.id)}</h2>
    <dl class="sv-kv"><dt>Bus</dt><dd>${escapeHtml(e.bus)}</dd><dt>Table</dt><dd><a href="#" data-open-file="${escapeHtml(e.path)}" data-line="${e.line}">${escapeHtml(e.table)}</a> (${escapeHtml(e.path)})</dd></dl>
    <pre>${escapeHtml(e.text)}</pre>`,
  afterDetail() {},
  downloads: [{ label: "Device entries", ext: "tsv", build: (rows) => toTsv(rows, [["bus", (e) => e.bus], ["id", (e) => e.id], ["table", (e) => e.table], ["path", (e) => `${e.path}:${e.line}`]]) }],
  onDetailClick(e, ctx) {
    const k = e.target.closest("[data-kconfig]");
    if (!k) return false;
    e.preventDefault();
    import("../../state.js").then(({ state }) => state.openTab({ type: "kconfig", title: `KConfig (${ctx.arch})`, arch: ctx.arch, symbol: k.dataset.kconfig, version: ctx.version }));
    return true;
  },
});

/** The paste box lives in the summary block: wired once per render through event delegation. */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest?.(".dev-find");
  if (!btn) return;
  const box = btn.closest(".dev-paste");
  const view = box.closest(".sv-container");
  const text = box.querySelector(".dev-input").value;
  const results = box.querySelector(".dev-results");
  const version = view?.closest("[data-tab-version]")?.dataset.tabVersion;
  const state = (await import("../../state.js")).state;
  const tab = state.getActiveTab();
  const arch = tab?.arch || state.kconfigArch();
  results.innerHTML = '<p class="sv-empty">Matching…</p>';
  try {
    const res = await api.request(`/api/kapi/${encodeURIComponent(version || tab?.version || state.currentVersion)}/devices/match`, {
      method: "POST", body: JSON.stringify({ text, arch }), timeoutMs: 90000,
    });
    results.innerHTML = resultsHtml(res, { version: tab?.version || state.currentVersion, arch });
  } catch (err) {
    results.innerHTML = "";
    toast.error(`Matching failed: ${err.message}`);
  }
});
document.addEventListener("click", (e) => {
  const a = e.target.closest?.(".dev-results [data-open-file], .dev-results [data-kconfig]");
  if (!a) return;
  e.preventDefault();
  import("../../state.js").then(({ state }) => {
    const tab = state.getActiveTab();
    if (a.dataset.openFile) state.openTab({ type: "code", title: a.dataset.openFile.split("/").pop(), path: a.dataset.openFile, cursorLine: parseInt(a.dataset.line, 10) || 1, version: tab?.version || state.currentVersion });
    else state.openTab({ type: "kconfig", title: `KConfig (${tab?.arch || state.kconfigArch()})`, arch: tab?.arch || state.kconfigArch(), symbol: a.dataset.kconfig, version: tab?.version || state.currentVersion });
  });
});

export { resultsHtml };
