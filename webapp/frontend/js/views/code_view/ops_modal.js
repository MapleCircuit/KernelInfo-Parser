/**
 * ops_modal.js - Ops implementations (N1, webapp/API.md §5.6): who fills a struct's function-pointer field
 * (`GET /api/tools/{v}/implementations?struct=&field=`) and which ops tables use a function (`?function=`).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { openModal, escapeHtml } from "../../components/modal.js";
import { pollJob } from "../../utils/job_poll.js";
import { viewOf } from "../../utils/active_views.js";
import { enclosingStruct, tokenRole } from "./ops_model.js";

const COMMON = ["file_operations", "inode_operations", "super_operations", "address_space_operations", "dentry_operations",
  "net_device_ops", "ethtool_ops", "proto_ops", "vm_operations_struct", "seq_operations", "block_device_operations"];

function implRows(items, version) {
  const byFile = new Map();
  for (const it of items) {
    const key = it.path;
    if (!byFile.has(key)) byFile.set(key, []);
    byFile.get(key).push(it);
  }
  return [...byFile.entries()].map(([path, rows]) => `<div class="ops-file"><div class="ops-path">${escapeHtml(path)}</div>
    ${rows.map((r) => `<div class="ops-row">
      <a href="#" data-def="${escapeHtml(r.function || "")}" title="Go to the definition">${escapeHtml(r.function || r.field)}</a>
      <a href="#" data-open="${escapeHtml(path)}" data-line="${r.line}" class="kconfig-muted" title="Open the entry">line ${r.line}</a>
      in <a href="#" data-open="${escapeHtml(path)}" data-line="${r.table?.line ?? r.table_line}" title="Open the table">${escapeHtml(r.table?.name ?? r.table)}</a>${r.struct ? ` <span class="kconfig-muted">(struct ${escapeHtml(r.struct)}.${escapeHtml(r.field)})</span>` : ""}
    </div>`).join("")}</div>`).join("");
}

export function showOpsModal({ version = state.activeVersion?.() || state.currentVersion, name = "", filePath = "", line = 0, tabId = null } = {}) {
  const lines = viewOf(tabId || state.getActiveTab()?.id)?.rawLines || [];
  const isStruct = /(_operations|_ops|_struct)$/.test(name);
  const role = isStruct ? "" : tokenRole(lines[line - 1] || "", name);
  const guess = isStruct ? name : (enclosingStruct(lines, line) || "file_operations");
  const modal = openModal({ title: "Ops implementations", subtitle: `Function pointers set by struct initializers at ${version} (source index: macro-generated tables are not seen)`, width: "860px", className: "ops-modal" });
  modal.body.innerHTML = `
    <div class="ops-form">
      <label>struct <input id="ops-struct" list="ops-structs" value="${escapeHtml(guess)}" spellcheck="false" /></label>
      <label>field <input id="ops-field" value="${escapeHtml(role === "field" ? name : role === "function" ? (/\.\s*([A-Za-z_]\w*)\s*=/.exec(lines[line - 1] || "")?.[1] || "") : "")}" placeholder="(all fields)" spellcheck="false" /></label>
      <button class="code-btn active" id="ops-find" type="button">Find implementations</button>
      <datalist id="ops-structs">${COMMON.map((s) => `<option value="${s}">`).join("")}</datalist>
    </div>
    <div class="ops-form">
      <label>function <input id="ops-func" value="${escapeHtml(isStruct ? "" : name)}" spellcheck="false" /></label>
      <button class="code-btn" id="ops-reverse" type="button">Find ops tables using it</button>
    </div>
    <div class="ops-status kconfig-muted" aria-live="polite"></div>
    <div class="ops-results"></div>`;
  const status = modal.body.querySelector(".ops-status");
  const results = modal.body.querySelector(".ops-results");
  const q = (sel) => modal.body.querySelector(sel).value.trim();
  const findImpl = async () => {
    const struct = q("#ops-struct");
    const field = q("#ops-field");
    if (!struct) return;
    status.textContent = `Looking up struct ${struct} initializers…`;
    results.innerHTML = "";
    try {
      const params = new URLSearchParams({ struct });
      if (field) params.set("field", field);
      const res = await api.requestJob(`/api/tools/${encodeURIComponent(version)}/implementations?${params}`, {
        // 202 while the revision's source index is assembled (§2.19); then every struct answers at once
        timeoutMs: 600000, onProgress: (p) => { status.textContent = `Indexing the sources of ${version}: ${p?.note || "…"} ${p?.total ? `${p.done ?? 0}/${p.total}` : ""}`; },
      });
      status.textContent = `${res.tables.toLocaleString("en-US")} struct ${res.struct} tables in ${res.files} files, ${res.positional_skipped} positional entries not resolved`;
      if (field) {
        results.innerHTML = res.implementations.length
          ? `<h4>.${escapeHtml(field)} — ${res.implementations.length.toLocaleString("en-US")} implementations</h4>${implRows(res.implementations, version)}`
          : `<div class="kconfig-muted">No initializer sets .${escapeHtml(field)}. Fields: ${res.fields.map((f) => escapeHtml(f.field)).join(", ")}</div>`;
      } else {
        results.innerHTML = `<div class="ops-fields">${res.fields.map((f) => `<a href="#" data-field="${escapeHtml(f.field)}">.${escapeHtml(f.field)} <span class="kconfig-muted">${f.count}</span></a>`).join("")}</div>`;
      }
    } catch (err) {
      status.textContent = `Failed: ${err.message}`;
    }
  };
  const findReverse = async () => {
    const fn = q("#ops-func");
    if (!fn) return;
    status.textContent = `Looking for ops tables using ${fn}…`;
    results.innerHTML = "";
    try {
      const res = await api.requestJob(`/api/tools/${encodeURIComponent(version)}/implementations?${new URLSearchParams({ function: fn })}`, {
        timeoutMs: 600000, onProgress: (p) => { status.textContent = `Indexing the sources of ${version}: ${p?.note || "…"} ${p?.total ? `${p.done ?? 0}/${p.total}` : ""}`; },
      });
      status.textContent = `${res.uses.length} use${res.uses.length === 1 ? "" : "s"} of ${fn} in struct initializers`;
      results.innerHTML = res.uses.length ? implRows(res.uses.map((u) => ({ ...u, function: fn })), version) : "";
    } catch (err) {
      status.textContent = `Failed: ${err.message}`;
    }
  };
  modal.body.querySelector("#ops-find").onclick = findImpl;
  modal.body.querySelector("#ops-reverse").onclick = findReverse;
  const open = async (e, forceNew) => {
    const a = e.target.closest("a[data-open], a[data-def], a[data-field]");
    if (!a) return;
    e.preventDefault();
    if (a.dataset.field) {
      modal.body.querySelector("#ops-field").value = a.dataset.field;
      findImpl();
      return;
    }
    // Middle-click / Ctrl+click opens in a new tab and keeps the dialog (§5.9)
    if (!forceNew) modal.close();
    if (a.dataset.open) {
      state.openTab({ type: "code", title: a.dataset.open.split("/").pop(), path: a.dataset.open, cursorLine: Number(a.dataset.line) || 1, version, forceNew });
    } else {
      const { openDefinition } = await import("../../utils/definition_picker.js");
      openDefinition(version, a.dataset.def, { forceNew, fromFile: filePath || null });
    }
  };
  results.addEventListener("click", (e) => open(e, e.ctrlKey || e.metaKey));
  results.addEventListener("auxclick", (e) => { if (e.button === 1) open(e, true); });
  // A struct name lists its fields, a `.field =` token its implementations, anything else the tables using it
  if (isStruct || role === "field") findImpl();
  else if (name) findReverse();
}
