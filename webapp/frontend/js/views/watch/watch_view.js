/**
 * watch_view.js - Watch list tab (G10, tab type `watch`, webapp/API.md §5.36).
 */
import { state } from "../../state.js";
import { localDB } from "../../db.js";
import { toast } from "../../components/toast.js";
import { escapeHtml } from "../../components/modal.js";
import { watchLabel } from "./watch_model.js";
import { checkWatches, listWatches, markRead, removeWatch, watchStates } from "./watch_runtime.js";

const STATUS_CLS = { modified: "bad", added: "warn", removed: "bad", cosmetic: "muted", same: "ok" };

export class WatchView {
  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    containerEl.innerHTML = `<div class="bm-view watch-view">
      <div class="bm-head"><h2>Watch list</h2>
        <button type="button" class="code-btn active" data-act="check">Check now</button>
        <button type="button" class="code-btn" data-act="read">Mark all read</button>
        <label class="kconfig-muted"><input type="checkbox" data-act="notify" ${localStorage.getItem("kip-watch-notify") === "1" ? "checked" : ""}/> Browser notification when the page is hidden</label>
      </div>
      <div class="kconfig-muted watch-help">Watched items are checked when their branch moves or a new version of it is indexed. Add watches from a token's menu (<em>Watch symbol</em>, <em>Watch this file</em>) or the palette (<em>Watch this tab</em>: a Kconfig option, a MAINTAINERS section, an explorer directory).</div>
      <div class="watch-list"></div></div>`;
    const root = containerEl.firstElementChild;
    root.addEventListener("click", (e) => this.onClick(e));
    root.querySelector('[data-act="notify"]').addEventListener("change", async (e) => {
      if (e.target.checked && "Notification" in window && Notification.permission !== "granted") {
        const p = await Notification.requestPermission();
        if (p !== "granted") {
          e.target.checked = false;
          toast.info("Notifications were not allowed by the browser");
          return;
        }
      }
      try {
        localStorage.setItem("kip-watch-notify", e.target.checked ? "1" : "0");
      } catch {
        // not kept
      }
    });
    this.unsub?.();
    this.unsub = localDB.onUserChanged((d) => {
      if ((!d.kind || d.kind.startsWith("watch") || d.kind === "*") && this.containerEl?.isConnected) this.refresh();
    });
    await this.refresh();
  }

  dispose() {
    this.unsub?.();
  }

  async refresh() {
    const watches = await listWatches();
    const states = await watchStates();
    const host = this.containerEl?.querySelector(".watch-list");
    if (!host) return;
    host.innerHTML = watches.length ? watches.map((w) => {
      const s = states.get(w.key) || {};
      const r = s.result;
      const t = w.data.target;
      return `<div class="bm-row watch-row ${s.unread ? "watch-unread" : ""}" data-key="${escapeHtml(w.key)}">
        <div class="bm-line"><strong>${escapeHtml(watchLabel(t))}</strong>
          <span class="kconfig-muted">${escapeHtml(t.type)} · follows <code>${escapeHtml(w.data.branch)}</code></span>
          ${s.status ? `<span class="ptry-badge ${STATUS_CLS[s.status] || "muted"}">${escapeHtml(s.status)}</span>` : '<span class="kconfig-muted">not checked yet</span>'}
          ${s.unread ? '<span class="watch-new">new</span>' : ""}</div>
        <div class="kconfig-muted">${s.last_checked ? `checked ${new Date(s.last_checked).toLocaleString()} at ${escapeHtml(String(s.last_sha || "").slice(0, 12))}` : ""}${s.error ? ` · ${escapeHtml(s.error)}` : ""}</div>
        ${r ? `<div class="watch-change">Changed${s.changed_since ? ` since ${escapeHtml(s.changed_since.slice(0, 12))}` : ""}: ${escapeHtml(r.summary || r.status)}
          ${r.paths ? `<ul class="watch-paths">${r.paths.slice(0, 12).map((p) => `<li><code>${escapeHtml(p.status)}</code> ${escapeHtml(p.path)}</li>`).join("")}${r.paths.length > 12 ? `<li>… ${r.paths.length - 12} more</li>` : ""}</ul>` : ""}
          <div class="bm-actions">${this.linksHtml(w, s)}</div></div>` : ""}
        <div class="bm-actions"><button type="button" data-act="remove">Stop watching</button>${s.unread ? '<button type="button" data-act="read-one">Mark read</button>' : ""}</div>
      </div>`;
    }).join("") : `<div class="bm-empty">Nothing watched yet.</div>`;
  }

  linksHtml(w, s) {
    const t = w.data.target;
    const from = s.changed_since || w.data.base_sha;
    const to = s.last_sha;
    const out = [];
    if ((t.type === "file" || t.type === "dir") && from && to) out.push(`<button type="button" data-link="compare">Compare ${escapeHtml(from.slice(0, 8))}…${escapeHtml(to.slice(0, 8))}</button>`);
    if (t.type === "file" || t.type === "dir" || t.type === "section") out.push(`<button type="button" data-link="commits">Commits</button>`);
    if ((t.type === "symbol" || t.type === "kconfig") && s.result?.links?.history?.path) out.push(`<button type="button" data-link="history">History</button>`);
    return out.join("");
  }

  async onClick(e) {
    const btn = e.target.closest("button");
    if (!btn) return;
    const act = btn.dataset.act;
    const row = btn.closest(".watch-row");
    if (act === "check") {
      btn.disabled = true;
      const n = await checkWatches();
      btn.disabled = false;
      toast.success(n ? `${n} watched item${n === 1 ? "" : "s"} changed` : "No watched item changed");
      return;
    }
    if (act === "read") return markRead();
    if (!row) return;
    const key = row.dataset.key;
    if (act === "remove") return removeWatch(key);
    if (act === "read-one") return markRead([key]);
    const w = (await listWatches()).find((x) => x.key === key);
    const s = (await watchStates()).get(key) || {};
    if (!w) return;
    const t = w.data.target;
    const link = btn.dataset.link;
    if (link === "compare") {
      state.openTab({ type: "diff", title: `Compare ${t.path}`, versionA: s.changed_since || w.data.base_sha, versionB: s.last_sha, query: t.path, forceNew: true });
    } else if (link === "commits") {
      const q = t.type === "section" ? `section:"${t.name}"` : `path:${t.path}`;
      state.openTab({ type: "commits", title: "Commits", query: q, version: w.data.branch, forceNew: true });
    } else if (link === "history") {
      const h = s.result.links.history;
      const m = await import("../../components/symbol_history_modal.js");
      if (t.type === "kconfig") m.showKconfigHistoryModal(h.version, String(t.name).replace(/^CONFIG_/, ""));
      else m.showSymbolHistoryModal(h.version, h.path, h.line, null, t.name);
    }
    if (s.unread) markRead([key]);
  }
}
