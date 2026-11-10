/**
 * hover_card.js - Hover cards on code tokens (G04, webapp/API.md §5.6).
 *
 * Resting the pointer on a symbol shows its kind, signature, definition (the one Go to Definition opens for the
 * followed ARCH and this file), the kernel-doc summary and quick actions; a CONFIG_ token shows the Kconfig
 * prompt, type, dependencies and the value of the followed engine. `HoverIntent` is the timing state machine
 * (DOM-free, tested); `HoverCard` wires it to a code view.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";
import { HoverIntent, hoverSettings, hoverable, kindLabel } from "./hover_model.js";

export { HoverIntent, hoverSettings, hoverable, kindLabel };

export class HoverCard {
  /** `view`: the code view (currentVersion, currentFilePath, engineTabId, linesContainer, viewportEl). */
  constructor(view) {
    this.view = view;
    this.cache = new Map();
    this.abort = null;
    this.cardEl = null;
    this.lastFetch = 0;
    const { delay } = hoverSettings();
    this.intent = new HoverIntent({ delay, onShow: (el) => this.show(el), onHide: () => this.removeCard() });
  }

  attach(container, viewport) {
    container.addEventListener("mousemove", (e) => {
      const settings = hoverSettings();
      if (!settings.enabled || e.buttons) return;
      this.intent.delay = settings.delay;
      if (this.busy()) {
        this.intent.suppress(true);
        return;
      }
      this.intent.suppress(false);
      const el = e.target.closest?.(".ast-token");
      if (el && hoverable(el)) this.intent.rest(el);
      else this.intent.leave();
    });
    container.addEventListener("mouseleave", () => this.intent.leave());
    container.addEventListener("mousedown", () => this.intent.hide());
    container.addEventListener("click", () => this.intent.hide(), true);
    container.addEventListener("contextmenu", () => this.intent.hide(), true);
    viewport?.addEventListener("scroll", () => this.intent.hide(), { passive: true });
    this.onKey = (e) => {
      if (e.key === "Escape" && this.cardEl) this.intent.hide();
    };
    document.addEventListener("keydown", this.onKey);
  }

  dispose() {
    this.intent.hide();
    document.removeEventListener("keydown", this.onKey);
  }

  /** A menu, the inspector, a dialog or a text selection is in the way. */
  busy() {
    if (document.querySelector(".context-menu, .ki-modal-backdrop")) return true;
    const sel = window.getSelection?.();
    return Boolean(sel && !sel.isCollapsed && String(sel).trim());
  }

  async show(el) {
    const name = el.dataset.name || el.textContent;
    const v = this.view.currentVersion;
    const ctx = state.definitionContext(this.view.engineTabId ?? null);
    const key = `${v}|${ctx.arch}|${this.view.currentFilePath}|${name}`;
    this.renderCard(el, { name, loading: true });
    let data = this.cache.get(key);
    if (!data) {
      if (!api.isConnected) return this.renderCard(el, { name, error: "Server unreachable" });
      const wait = Math.max(0, 300 - (performance.now() - this.lastFetch));
      if (wait) await new Promise((r) => setTimeout(r, wait));
      if (this.intent.shown !== el) return;
      this.abort?.abort();
      this.abort = new AbortController();
      this.lastFetch = performance.now();
      try {
        data = await api.getSymbolHover(v, name, ctx.arch, this.view.currentFilePath, this.abort.signal);
        this.cache.set(key, data);
      } catch (err) {
        if (err?.name === "AbortError") return;
        if (this.intent.shown === el) this.renderCard(el, { name, error: err.message });
        return;
      }
    }
    if (this.intent.shown !== el) return;
    this.renderCard(el, { name, data, value: data?.kind === "kconfig" ? ctx.assignments?.[data.kconfig?.name] ?? (ctx.strict ? "n" : undefined) : undefined, engine: ctx.engineTabId });
  }

  renderCard(el, { name, loading = false, error = "", data = null, value, engine }) {
    this.removeCard(false);
    const card = document.createElement("div");
    card.className = "hover-card";
    card.setAttribute("role", "tooltip");
    let html = "";
    if (loading) html = `<div class="hc-head"><code>${escapeHtml(name)}</code></div><div class="hc-muted">Loading…</div>`;
    else if (error) html = `<div class="hc-head"><code>${escapeHtml(name)}</code></div><div class="hc-muted">${escapeHtml(error)}</div>`;
    else if (!data?.found) html = `<div class="hc-head"><code>${escapeHtml(name)}</code></div><div class="hc-muted">No definition in this revision</div>`;
    else if (data.kind === "kconfig") {
      const k = data.kconfig || {};
      html = `<div class="hc-head"><span class="hc-kind">Kconfig ${escapeHtml(k.type || "")}</span><code>CONFIG_${escapeHtml(k.name || "")}</code></div>
        ${k.prompt ? `<div class="hc-prompt">${escapeHtml(k.prompt)}</div>` : ""}
        <div class="hc-value">${value !== undefined ? `Value: <strong>${escapeHtml(value)}</strong> <span class="hc-muted">(${engine ? "followed engine" : "baseline"})</span>` : `<span class="hc-muted">No engine followed: value unknown</span>`}</div>
        ${k.depends_on?.length ? `<div class="hc-muted">depends on ${escapeHtml(k.depends_on.join(", "))}</div>` : ""}
        ${k.selects?.length ? `<div class="hc-muted">selects ${escapeHtml(k.selects.join(", "))}</div>` : ""}
        ${k.selected_by?.length ? `<div class="hc-muted">Selected by: ${escapeHtml(k.selected_by.join(", "))}</div>` : ""}
        ${k.implied_by?.length ? `<div class="hc-muted">Implied by: ${escapeHtml(k.implied_by.join(", "))}</div>` : ""}
        ${k.help ? `<div class="hc-doc">${escapeHtml(k.help)}${k.more ? " …" : ""}</div>` : ""}
        <div class="hc-loc">${escapeHtml(data.file)}:${data.line}</div>`;
    } else {
      const doc = data.doc;
      html = `<div class="hc-head"><span class="hc-kind">${escapeHtml(kindLabel(data.kind))}</span><code>${escapeHtml(name)}</code></div>
        ${data.signature ? `<pre class="hc-sig">${escapeHtml(data.signature)}</pre>` : ""}
        ${data.field_offset !== undefined ? `<div class="hc-offset hc-muted">offset: ${data.field_offset}${data.bit_offset ? `.${data.bit_offset}` : ""} bytes, cacheline ${data.cacheline} (offset ${data.cacheline_offset})</div>` : ""}
        ${doc ? `<div class="hc-doc"><strong>${escapeHtml(doc.summary)}</strong>${doc.description ? `<br>${escapeHtml(doc.description)}` : ""}${doc.more ? " …" : ""}</div>
          ${doc.params && doc.params.length ? `<ul class="hc-params">${doc.params.slice(0, 6).map((p) => `<li><code>${p.name && p.name.startsWith("@") ? "" : "@"}${escapeHtml(p.name || "")}</code> ${escapeHtml(p.text || "")}</li>`).join("")}</ul>` : ""}` : ""}
        <div class="hc-loc">${escapeHtml(data.file)}:${data.line}${data.definitions > 1 ? ` <span class="hc-muted">(+${data.definitions - 1} other definition${data.definitions > 2 ? "s" : ""})</span>` : ""}</div>`;
    }
    if (data?.found) {
      html += `<div class="hc-actions"><button type="button" data-hc="def">Go to definition</button><button type="button" data-hc="refs">References</button><button type="button" data-hc="hist">History</button></div>`;
    }
    card.innerHTML = html;
    document.body.appendChild(card);
    const r = el.getBoundingClientRect();
    const cw = Math.min(card.offsetWidth || 420, window.innerWidth - 16);
    const ch = card.offsetHeight || 160;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - cw - 8));
    const below = r.bottom + 6 + ch < window.innerHeight;
    card.style.left = `${left}px`;
    card.style.top = `${below ? r.bottom + 6 : Math.max(8, r.top - ch - 6)}px`;
    card.addEventListener("mouseenter", () => this.intent.enterCard());
    card.addEventListener("mouseleave", () => this.intent.leaveCard());
    card.addEventListener("click", (e) => {
      const act = e.target.closest("[data-hc]")?.dataset.hc;
      if (act) this.act(act, name, data);
    });
    this.cardEl = card;
  }

  async act(act, name, data) {
    const v = this.view.currentVersion;
    this.intent.hide();
    const { contextMenu } = await import("../../components/context_menu.js");
    if (act === "def") {
      const { openDefinition } = await import("../../utils/definition_picker.js");
      openDefinition(v, name, { fromFile: this.view.currentFilePath });
    } else if (act === "refs") {
      const xref = await api.getSymbolXref(v, name, 250);
      contextMenu.showXrefModal(name, xref, v);
    } else if (act === "hist") {
      const m = await import("../../components/symbol_history_modal.js");
      if (data?.kind === "kconfig") m.showKconfigHistoryModal(v, data.kconfig?.name || name);
      else m.showSymbolHistoryModal(v, data.file, data.line, null, name);
    }
  }

  removeCard(abort = true) {
    if (abort) this.abort?.abort();
    this.cardEl?.remove();
    this.cardEl = null;
  }
}
