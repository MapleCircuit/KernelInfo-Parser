/**
 * nodemap_pickers.js - Interactive field pickers of the NodeMap Inspector and dialogs
 * (`webapp/API_FRONTEND.md` §5.3): a revision popover (the shared `refPicker`, unscoped), a debounced
 * symbol / struct / Kconfig autocomplete, and a File Explorer modal over the revision's tree.
 *
 * Every picker only fills its input: the Inspector commits the value through `onInspectorInput`
 * (one undo step, content reload), a dialog reads it on submit. Free text keeps working when a lookup
 * finds nothing (unparsed revisions have no symbol index). The search models (`AutocompleteModel`,
 * `FileExplorerModel`) hold no DOM, so the node tests drive them directly.
 */
import { escapeHtml, openModal } from "../../components/modal.js";
import { refPicker } from "../../components/ref_picker.js";
import { debounce } from "../../utils/debounce.js";

/** Picker button glyph and tooltip per kind. */
export const PICK_KINDS = {
  version: { icon: "🏷", title: "Browse tags and commits…" },
  file: { icon: "📁", title: "Browse files…" },
};

/**
 * `<button>` placed beside a picker input; `forKey` names the input (its `data-insp` key in the
 * Inspector, `insp: true`, or its form `name` in a dialog).
 */
export function pickButtonHtml(kind, forKey, { insp = false } = {}) {
  const k = PICK_KINDS[kind];
  if (!k) return "";
  return `<button type="button" class="nodemap-btn nm-pick-btn"${insp ? ' data-insp="pick"' : ""} data-pick="${escapeHtml(kind)}" data-for="${escapeHtml(forKey)}" title="${escapeHtml(k.title)}" aria-label="${escapeHtml(k.title)}">${k.icon}</button>`;
}

/** Wrap an input's HTML with its picker button. */
export function withPickHtml(inputHtml, kind, forKey, opts = {}) {
  return `<div class="nm-pick-wrap">${inputHtml}${pickButtonHtml(kind, forKey, opts)}</div>`;
}

/** Set an input's value and fire `input` + `change` so form listeners see a picked value. */
export function setInputValue(input, value) {
  if (!input) return;
  input.value = value;
  if (typeof Event === "function" && typeof input.dispatchEvent === "function") {
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

// ------------------------------------------------------------------ revision picker

/**
 * Open the shared revision picker (unscoped, like the Compare view) under `anchor`; the picked ref
 * goes to `onSelect`. `picker` is injectable for tests.
 */
export function openVersionPicker(anchor, { current = "", client = null, onSelect, picker = refPicker } = {}) {
  picker = picker || refPicker;
  const opts = { current: String(current || "").trim(), scoped: false, onSelect: (ref) => { if (ref) onSelect?.(ref); } };
  if (client) opts.client = client;
  return picker.open(anchor, opts);
}

// ------------------------------------------------------------------ symbol autocomplete

const STRUCTISH = /struct|union/i;

/** Suggestion rows `{ name, kind, path, line, detail }` of a symbol / struct / Kconfig search. */
export async function fetchSuggestions(mode, client, version, query, { arch = "x86_64", limit = 25 } = {}) {
  const q = String(query || "").trim();
  if (!q || !client) return [];
  if (mode === "kconfig") {
    const res = await client.searchKconfig(version, q, limit, arch || "x86_64");
    return (res?.symbols || []).map((s) => ({
      name: String(s.name || "").replace(/^CONFIG_/, ""),
      kind: s.type_name || s.type || "kconfig",
      path: s.file_path || "",
      line: s.line_s || null,
      detail: s.prompt || "",
    })).filter((s) => s.name);
  }
  const res = await client.searchSymbols(version, q, mode === "struct" ? 50 : limit);
  let rows = (res?.symbols || []).map((s) => ({
    name: s.name || s.symbol_name || "",
    kind: s.type_name || "",
    path: s.file_path || "",
    line: s.line_s || null,
    detail: "",
  })).filter((s) => s.name);
  if (mode === "struct") {
    const structs = rows.filter((r) => STRUCTISH.test(r.kind));
    if (structs.length) rows = structs;
  }
  const seen = new Set();
  return rows.filter((r) => {
    const key = `${r.name}\u0000${r.kind}\u0000${r.path}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, limit);
}

/** `struct` / `union` of a struct-search kind label (`C_union` → union), else null. */
export function structKindOf(kind) {
  const m = /(struct|union)/i.exec(String(kind || ""));
  return m ? m[1].toLowerCase() : null;
}

/**
 * Search state of one autocomplete: a request counter drops out-of-order responses, `highlight`
 * follows ArrowUp/ArrowDown and `current()` is what Enter picks.
 */
export class AutocompleteModel {
  constructor(fetch) {
    this.fetch = fetch;
    this.reqId = 0;
    this.items = [];
    this.highlight = -1;
    this.query = "";
  }

  /** Run a search; resolves to the rows, or null when a later query superseded it. */
  async run(query) {
    const id = ++this.reqId;
    const q = String(query || "").trim();
    this.query = q;
    if (!q) {
      this.items = [];
      this.highlight = -1;
      return this.items;
    }
    let rows;
    try {
      rows = await this.fetch(q);
    } catch {
      rows = [];
    }
    if (id !== this.reqId) return null;
    this.items = Array.isArray(rows) ? rows : [];
    this.highlight = this.items.length ? 0 : -1;
    return this.items;
  }

  cancel() {
    this.reqId += 1;
    this.items = [];
    this.highlight = -1;
  }

  move(delta) {
    if (!this.items.length) return -1;
    this.highlight = (this.highlight + delta + this.items.length) % this.items.length;
    return this.highlight;
  }

  current() {
    return this.highlight >= 0 ? this.items[this.highlight] || null : null;
  }
}

/**
 * Attach a keyboard-navigable suggestion dropdown to `input`. `fetch(q)` returns rows of
 * `fetchSuggestions`; `onPick(row)` receives the chosen one (the input already holds its name).
 * Returns the model (also on `input._nmAutocomplete`); attaching twice is a no-op.
 */
export function attachAutocomplete(input, { fetch, onPick, delay = 180 } = {}) {
  if (!input || input._nmAutocomplete) return input?._nmAutocomplete || null;
  const model = new AutocompleteModel(fetch);
  input._nmAutocomplete = model;
  input.setAttribute?.("autocomplete", "off");
  let list = null;

  // An open list closes on a click elsewhere, a scroll or a resize (it is placed under the input
  // once), and when the Inspector re-renders the input away
  const onOutside = (e) => {
    if (list && !list.contains(e.target) && e.target !== input) close();
  };
  const onMove = (e) => {
    if (list && !list.contains(e?.target)) close();
  };
  const close = () => {
    if (!list) return;
    list.remove();
    list = null;
    document.removeEventListener("mousedown", onOutside, true);
    window.removeEventListener("scroll", onMove, true);
    window.removeEventListener("resize", onMove);
  };
  const pick = (row) => {
    if (!row) return;
    model.cancel();
    close();
    input.value = row.name;
    // The pick fires input/change on its own fields: no new search for the name just picked
    input._nmAcSuppress = true;
    try {
      onPick?.(row);
    } finally {
      input._nmAcSuppress = false;
    }
  };
  const render = () => {
    if (!model.items.length || !input.isConnected) return close();
    if (!list) {
      list = document.createElement("div");
      list.className = "nm-ac-list";
      list.setAttribute("role", "listbox");
      document.body.appendChild(list);
      document.addEventListener("mousedown", onOutside, true);
      window.addEventListener("scroll", onMove, true);
      window.addEventListener("resize", onMove);
    }
    const r = input.getBoundingClientRect();
    list.style.top = `${r.bottom + 2}px`;
    list.style.left = `${Math.max(4, r.left)}px`;
    list.style.minWidth = `${r.width}px`;
    list.innerHTML = model.items.map((it, i) =>
      `<div class="nm-ac-item${i === model.highlight ? " highlight" : ""}" role="option" data-idx="${i}" title="${escapeHtml(it.path ? `${it.path}${it.line ? `:${it.line}` : ""}` : it.name)}">` +
      `<span class="nm-ac-name">${escapeHtml(it.name)}</span>` +
      `${it.kind ? `<span class="nm-ac-kind">${escapeHtml(it.kind)}</span>` : ""}` +
      `<span class="nm-ac-meta">${escapeHtml(it.detail || (it.path ? `${it.path}${it.line ? `:${it.line}` : ""}` : ""))}</span></div>`).join("");
    list.querySelectorAll(".nm-ac-item").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        e.preventDefault();
        pick(model.items[Number(el.dataset.idx)]);
      });
    });
    list.querySelector(".nm-ac-item.highlight")?.scrollIntoView?.({ block: "nearest" });
  };
  const search = debounce(async () => {
    const rows = await model.run(input.value);
    if (rows) render();
  }, delay);

  input.addEventListener("input", () => {
    if (input._nmAcSuppress) return;
    // The rows of the previous query go at once: Enter must not pick one of them while the next loads
    model.cancel();
    close();
    search();
  });
  input.addEventListener("keydown", (e) => {
    if (!list || !model.items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      model.move(e.key === "ArrowDown" ? 1 : -1);
      render();
    } else if (e.key === "Enter") {
      const row = model.current();
      if (!row) return;
      e.preventDefault();
      e.stopPropagation();
      pick(row);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      search.cancel();
      model.cancel();
      close();
    }
  });
  input.addEventListener("blur", () => {
    search.cancel();
    model.cancel();
    setTimeout(close, 120);
  });
  return model;
}

// ------------------------------------------------------------------ file explorer

/** Directory a File Explorer opens in for an input's current path: its parent ("" is the root). */
export function initialDirOf(path) {
  const p = String(path || "").trim().replace(/^\/+/, "");
  if (!p) return "";
  if (p.endsWith("/")) return p.replace(/\/+$/, "");
  const i = p.lastIndexOf("/");
  return i > 0 ? p.slice(0, i) : "";
}

/**
 * Browsing state of the File Explorer: `open(dir)` lists a directory (`getDirectoryTree`),
 * `search(q)` lists matching files (`searchFiles`); a stale response is dropped.
 */
export class FileExplorerModel {
  constructor({ client, version, dir = "" }) {
    this.client = client;
    this.version = version;
    this.dir = dir;
    this.filter = "";
    this.rows = [];
    this.error = "";
    this.reqId = 0;
  }

  /** Breadcrumbs `[{ label, dir }]`, the root first. */
  crumbs() {
    const out = [{ label: "root", dir: "" }];
    const parts = this.dir ? this.dir.split("/").filter(Boolean) : [];
    parts.forEach((p, i) => out.push({ label: p, dir: parts.slice(0, i + 1).join("/") }));
    return out;
  }

  parentDir() {
    const i = this.dir.lastIndexOf("/");
    return i > 0 ? this.dir.slice(0, i) : "";
  }

  async open(dir = "") {
    const id = ++this.reqId;
    this.dir = String(dir || "").replace(/^\/+|\/+$/g, "");
    this.filter = "";
    try {
      const res = await this.client.getDirectoryTree(this.version, this.dir);
      if (id !== this.reqId) return false;
      this.rows = (res?.entries || []).map((e) => ({
        type: e.type === "dir" ? "dir" : "file",
        name: e.name || "",
        path: e.path || (this.dir ? `${this.dir}/${e.name}` : e.name),
      }));
      this.error = "";
    } catch (err) {
      if (id !== this.reqId) return false;
      this.rows = [];
      this.error = err?.message || "Could not list this directory";
    }
    return true;
  }

  async search(query) {
    const q = String(query || "").trim();
    if (!q) return this.open(this.dir);
    const id = ++this.reqId;
    this.filter = q;
    try {
      const res = await this.client.searchFiles(this.version, q, 100);
      if (id !== this.reqId) return false;
      this.rows = (res?.files || []).map((f) => ({ type: "file", name: f.name || f.path, path: f.path }));
      this.error = "";
    } catch (err) {
      if (id !== this.reqId) return false;
      this.rows = [];
      this.error = err?.message || "File search failed";
    }
    return true;
  }
}

/**
 * Open the File Explorer modal for `version` (`client` is the repository-scoped API); a picked
 * file path goes to `onSelect` and closes it. Starts in the parent directory of `currentPath`.
 */
export function openFileExplorer({ client, version, currentPath = "", onSelect, openModalFn = openModal, title = "Choose a File" }) {
  openModalFn = openModalFn || openModal;
  const model = new FileExplorerModel({ client, version, dir: initialDirOf(currentPath) });
  const modal = openModalFn({
    title,
    subtitle: `Repository tree at ${version || "the selected revision"}`,
    width: "560px",
    className: "nm-fx-modal",
    footerHtml: '<button type="button" class="code-btn primary" data-act="fx-select" disabled>Select</button>',
  });
  modal.body.innerHTML = `<div class="nm-fx">
    <div class="nm-fx-crumbs" aria-label="Directory"></div>
    <input type="search" class="nm-fx-filter" placeholder="Filter / search files…" spellcheck="false" autocomplete="off" aria-label="Search files"/>
    <div class="nm-fx-list" role="listbox"><div class="nm-fx-empty">Loading…</div></div>
  </div>`;
  const crumbsEl = modal.body.querySelector(".nm-fx-crumbs");
  const filterEl = modal.body.querySelector(".nm-fx-filter");
  const listEl = modal.body.querySelector(".nm-fx-list");
  const selectBtn = modal.footer?.querySelector?.("[data-act='fx-select']");
  let highlight = -1;
  let shown = [];

  const choose = (path) => {
    if (!path) return;
    modal.close();
    onSelect?.(path);
  };
  const activate = (row) => {
    if (!row) return;
    if (row.type === "up") go(model.parentDir());
    else if (row.type === "dir") go(row.path);
    else choose(row.path);
  };
  const render = () => {
    if (!listEl) return;
    if (crumbsEl) {
      crumbsEl.innerHTML = model.filter
        ? `<span class="nm-fx-crumb-label">Files matching “${escapeHtml(model.filter)}”</span>`
        : model.crumbs().map((c, i, all) =>
          `<button type="button" class="nm-fx-crumb${i === all.length - 1 ? " current" : ""}" data-dir="${escapeHtml(c.dir)}">${escapeHtml(c.label)}</button>`).join('<span class="nm-fx-sep">/</span>');
    }
    shown = [...(!model.filter && model.dir ? [{ type: "up", name: "..", path: model.parentDir() }] : []), ...model.rows];
    if (highlight >= shown.length) highlight = shown.length - 1;
    const sel = shown[highlight];
    if (selectBtn) selectBtn.disabled = !(sel && sel.type === "file");
    if (model.error) {
      listEl.innerHTML = `<div class="nm-fx-empty nm-error">${escapeHtml(model.error)}</div>`;
      return;
    }
    if (!shown.length) {
      listEl.innerHTML = `<div class="nm-fx-empty">${model.filter ? "No matching files." : "Empty directory."}</div>`;
      return;
    }
    listEl.innerHTML = shown.map((r, i) =>
      `<div class="nm-fx-row nm-fx-${r.type}${i === highlight ? " highlight" : ""}" role="option" data-idx="${i}" title="${escapeHtml(r.path)}">` +
      `<span class="nm-fx-icon">${r.type === "file" ? "📄" : r.type === "up" ? "↰" : "📁"}</span>` +
      `<span class="nm-fx-name">${escapeHtml(model.filter ? r.path : r.name)}</span></div>`).join("");
    listEl.querySelectorAll(".nm-fx-row").forEach((el) => {
      el.addEventListener("click", () => activate(shown[Number(el.dataset.idx)]));
    });
    listEl.querySelector(".nm-fx-row.highlight")?.scrollIntoView?.({ block: "nearest" });
  };
  const go = async (dir) => {
    highlight = -1;
    if (filterEl) filterEl.value = "";
    if (await model.open(dir)) render();
  };
  const runFilter = debounce(async () => {
    highlight = -1;
    if (await model.search(filterEl?.value || "")) {
      if (model.filter && model.rows.length) highlight = 0;
      render();
    }
  }, 200);

  crumbsEl?.addEventListener("click", (e) => {
    const b = e.target.closest?.("[data-dir]");
    if (b) go(b.dataset.dir || "");
  });
  filterEl?.addEventListener("input", runFilter);
  filterEl?.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!shown.length) return;
      highlight = (highlight + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
      render();
    } else if (e.key === "Enter") {
      e.preventDefault();
      activate(shown[highlight]);
    }
  });
  if (selectBtn) selectBtn.onclick = () => { const r = shown[highlight]; if (r?.type === "file") choose(r.path); };
  filterEl?.focus?.();
  const ready = model.open(model.dir).then(async (ok) => {
    // A remembered directory that no longer exists falls back to the root
    if (ok && model.error && model.dir) await model.open("");
    render();
  });
  return { modal, model, ready, activate, choose };
}

// ------------------------------------------------------------------ form wiring (dialogs)

/**
 * Wire the pickers of a dialog form: `[data-pick]` buttons (revision / file) beside `[name]` inputs,
 * and `input[data-ac]` autocompletes (`symbol`, `struct`, `kconfig`). `ctx` supplies
 * `client`, `version()` (the form's revision), `arch()`, and the injectable `picker` / `openModalFn`.
 */
export function wireFormPickers(form, ctx) {
  if (!form || typeof form.querySelectorAll !== "function") return;
  const byName = (n) => form.querySelector(`[name="${n}"]`);
  const versionOf = () => String(ctx.version?.() || "").trim();
  for (const btn of form.querySelectorAll("[data-pick]")) {
    btn.addEventListener("click", (e) => {
      e?.preventDefault?.();
      e?.stopPropagation?.();
      const input = byName(btn.dataset.for);
      if (!input) return;
      if (btn.dataset.pick === "version") {
        openVersionPicker(btn, { current: input.value || versionOf(), client: ctx.client, picker: ctx.picker || refPicker, onSelect: (ref) => setInputValue(input, ref) });
      } else if (btn.dataset.pick === "file") {
        const ver = (ctx.fileVersion?.(btn.dataset.for)) || versionOf();
        openFileExplorer({ client: ctx.client, version: ver, currentPath: input.value, openModalFn: ctx.openModalFn || openModal, onSelect: (path) => { setInputValue(input, path); input.focus?.(); } });
      }
    });
  }
  for (const input of form.querySelectorAll("input[data-ac]")) {
    const mode = input.dataset.ac;
    attachAutocomplete(input, {
      fetch: (q) => fetchSuggestions(mode, ctx.client, versionOf(), q, { arch: ctx.arch?.() || "x86_64" }),
      onPick: (row) => {
        setInputValue(input, row.name);
        if (mode === "struct") fillStructFields(row, { path: byName("path"), kind: byName("kind") }, { force: true });
      },
    });
  }
}

/**
 * After a struct pick, fill the defining file (and the struct / union kind) the search knows; the
 * file is kept when the user already typed one and `force` is false.
 */
export function fillStructFields(row, { path, kind }, { force = false } = {}) {
  if (path && row?.path && (force || !String(path.value || "").trim())) setInputValue(path, row.path);
  const k = structKindOf(row?.kind);
  if (kind && k && kind.value !== k) setInputValue(kind, k);
}
