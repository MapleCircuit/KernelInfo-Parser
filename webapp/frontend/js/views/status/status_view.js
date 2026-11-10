/**
 * status_view.js - Parser status tab (tab type `status`, G11, webapp/API.md §5.37).
 *
 * What the parser has done for the active repository (GET /api/system/ingest): the catalog counters,
 * the versions being parsed (an entry not updated for 30 minutes is *stale*: an interrupted run, rolled
 * back by the parser's next start), recent completions, the database's table sizes (sortable), the
 * server's sync tokens, background jobs and caches. Refreshed every 15 s while the tab is visible.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";
import { ago, formatDuration, formatMb, sortTables } from "./status_model.js";

const REFRESH_MS = 15000;

export class StatusView {
  constructor() {
    this.containerEl = null;
    this.timer = null;
    this.data = null;
    this.sortKey = "total";
    this.sortDir = "desc";
    this.onVisibility = () => {
      if (!document.hidden) this.refresh();
    };
  }

  render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    containerEl.innerHTML = `
      <div class="status-view">
        <div class="status-head">
          <h2>Parser status</h2>
          <span class="status-repo"></span>
          <span class="status-updated" aria-live="polite"></span>
          <button type="button" class="code-btn status-refresh" title="Refresh now">⟳ Refresh</button>
        </div>
        <div class="status-body"><div class="status-empty">Loading…</div></div>
      </div>`;
    containerEl.querySelector(".status-refresh").onclick = () => this.refresh();
    containerEl.querySelector(".status-body").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-sort]");
      if (th) {
        const key = th.dataset.sort;
        if (this.sortKey === key) this.sortDir = this.sortDir === "desc" ? "asc" : "desc";
        else this.sortDir = key === "name" ? "asc" : "desc";
        this.sortKey = key;
        this.renderBody();
        return;
      }
      const v = e.target.closest("[data-version]");
      if (v) {
        e.preventDefault();
        state.setSelectedVersion(v.dataset.version);
      }
    });
    document.addEventListener("visibilitychange", this.onVisibility);
    this.refresh();
    clearInterval(this.timer);
    this.timer = setInterval(() => {
      // Paused while the page is hidden or another tab is shown (like the heartbeat, §5.1)
      if (document.hidden || !this.containerEl?.isConnected) return;
      this.refresh();
    }, REFRESH_MS);
  }

  async refresh() {
    if (!this.containerEl?.isConnected) return;
    try {
      this.data = await api.getIngestStatus();
      this.error = null;
    } catch (err) {
      this.error = err.message;
    }
    if (!this.containerEl?.isConnected) return;
    this.renderBody();
  }

  renderBody() {
    const body = this.containerEl.querySelector(".status-body");
    const d = this.data;
    this.containerEl.querySelector(".status-repo").textContent = d?.repository ? `· ${d.repository}` : "";
    this.containerEl.querySelector(".status-updated").textContent = this.error
      ? `Refresh failed: ${this.error}` : (d?.generated_at ? `updated ${ago(d.generated_at)}` : "");
    if (!d) {
      body.innerHTML = `<div class="status-empty">${escapeHtml(this.error || "No status yet.")}</div>`;
      return;
    }
    const c = d.catalog || {};
    const card = (val, label, cls = "") => `<div class="status-card ${cls}"><div class="status-val">${escapeHtml(val)}</div><div class="status-lbl">${escapeHtml(label)}</div></div>`;
    const parsing = d.parsing || [];
    const tables = sortTables(d.db?.tables || [], this.sortKey, this.sortDir);
    const arrow = (k) => (this.sortKey === k ? (this.sortDir === "desc" ? " ▾" : " ▴") : "");
    const server = d.server || {};
    body.innerHTML = `
      ${d.available === false ? `<div class="status-warn">The database is unavailable: ${escapeHtml(d.error || "")}</div>` : ""}
      <div class="status-cards">
        ${card((c.tags ?? "?").toLocaleString?.("en-US") ?? String(c.tags ?? "?"), "tags in Git")}
        ${card((c.parsed || 0).toLocaleString("en-US"), "parsed")}
        ${card(String(parsing.length), "parsing now", parsing.some((p) => p.stale) ? "warn" : parsing.length ? "busy" : "")}
        ${card(c.git_only == null ? "?" : c.git_only.toLocaleString("en-US"), "git only (not parsed)")}
        ${card(formatMb(d.db?.total_mb || 0), "database")}
      </div>
      <section class="status-section">
        <h3>Parsing now</h3>
        ${parsing.length ? `<table class="status-table"><thead><tr><th>Version</th><th>Commit</th><th>Last update</th><th></th></tr></thead><tbody>
          ${parsing.map((p) => `<tr class="${p.stale ? "stale" : ""}"><td><a href="#" data-version="${escapeHtml(p.version)}">${escapeHtml(p.version)}</a></td><td><code>${escapeHtml(p.commit)}</code></td><td>${escapeHtml(ago(p.updated_at))}</td>
            <td>${p.stale ? '<span class="status-badge warn" title="Not updated for 30 minutes: an interrupted run, rolled back when the parser starts again">stale</span>' : '<span class="status-badge busy">parsing</span>'}</td></tr>`).join("")}
        </tbody></table>` : '<div class="status-empty">Nothing is being parsed.</div>'}
      </section>
      <section class="status-section">
        <h3>Recent completions</h3>
        ${(d.recent || []).length ? `<table class="status-table"><thead><tr><th>Version</th><th>Commit</th><th>Finished</th></tr></thead><tbody>
          ${d.recent.map((r) => `<tr><td><a href="#" data-version="${escapeHtml(r.version)}">${escapeHtml(r.version)}</a></td><td><code>${escapeHtml(r.commit)}</code></td><td title="${escapeHtml(new Date(r.finished_at * 1000).toISOString())}">${escapeHtml(ago(r.finished_at))}</td></tr>`).join("")}
        </tbody></table>` : '<div class="status-empty">No version finished yet.</div>'}
      </section>
      <section class="status-section">
        <h3>Database tables <span class="status-sub">${tables.length} tables · ${escapeHtml(formatMb(d.db?.total_mb || 0))}</span></h3>
        <div class="status-scroll"><table class="status-table sortable"><thead><tr>
          <th data-sort="name">Table${arrow("name")}</th><th data-sort="rows_estimate" class="num">Rows (estimate)${arrow("rows_estimate")}</th>
          <th data-sort="data_mb" class="num">Data${arrow("data_mb")}</th><th data-sort="index_mb" class="num">Indexes${arrow("index_mb")}</th><th data-sort="total" class="num">Total${arrow("total")}</th>
        </tr></thead><tbody>
          ${tables.map((t) => `<tr><td><code>${escapeHtml(t.name)}</code></td><td class="num">${(t.rows_estimate || 0).toLocaleString("en-US")}</td><td class="num">${escapeHtml(formatMb(t.data_mb))}</td><td class="num">${escapeHtml(formatMb(t.index_mb))}</td><td class="num">${escapeHtml(formatMb((t.data_mb || 0) + (t.index_mb || 0)))}</td></tr>`).join("")}
        </tbody></table></div>
      </section>
      <section class="status-section status-grid">
        <div>
          <h3>Sync tokens</h3>
          <table class="status-table"><tbody>${Object.entries(d.sync || {}).map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td><code>${escapeHtml(v || "—")}</code></td></tr>`).join("")}</tbody></table>
        </div>
        <div>
          <h3>Server</h3>
          <table class="status-table"><tbody>
            <tr><td>Uptime</td><td>${escapeHtml(formatDuration(server.uptime_s))}</td></tr>
            <tr><td>Catalog warm-up</td><td>${escapeHtml(server.prewarm || "?")}</td></tr>
            <tr><td>Background jobs</td><td>${(server.jobs?.building || []).length} building · ${server.jobs?.done ?? 0} kept · ${server.jobs?.failed ?? 0} failed</td></tr>
            ${(server.jobs?.building || []).map((j) => `<tr><td class="status-sub">↳ ${escapeHtml(j.key)}</td><td>${escapeHtml(j.progress?.note || "")} ${j.progress?.total ? `${j.progress.done}/${j.progress.total}` : ""}</td></tr>`).join("")}
            ${(server.caches || []).map((x) => `<tr><td>${escapeHtml(x.name)}</td><td>${x.size} / ${x.maxsize ?? "∞"}</td></tr>`).join("")}
            ${(server.disk_cache?.namespaces || []).map((n) => `<tr><td>disk: ${escapeHtml(n.name)}</td><td>${n.entries} files · ${escapeHtml(formatMb(n.bytes / 1048576))}</td></tr>`).join("")}
            ${server.disk_cache ? `<tr><td>disk cache cap</td><td>${escapeHtml(formatMb(server.disk_cache.max_mb))}</td></tr>` : ""}
          </tbody></table>
        </div>
      </section>`;
  }

  dispose() {
    clearInterval(this.timer);
    this.timer = null;
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.containerEl = null;
  }
}
