/**
 * insights_view.js - Insights tab (tab type `insights`, S05, webapp/API.md §5.30): reports over the
 * history (release stats, merge window, subsystem health, stable backports, security).
 * Sections register themselves with `registerInsightsSection()`.
 */
import { api } from "../../api.js";
import { escapeHtml } from "../../components/modal.js";
import { shortRef } from "../../components/ref_picker.js";
import { parseTag, seriesOf } from "../sections/release_order.js";
import { SectionedView } from "../sections/sectioned_view.js";

const SECTIONS = [];

export function registerInsightsSection(section) {
  const i = SECTIONS.findIndex((s) => s.id === section.id);
  if (i >= 0) SECTIONS[i] = section;
  else SECTIONS.push(section);
  SECTIONS.sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
}

export function insightsSections() {
  return SECTIONS;
}

/**
 * Pick the release parameter an Insights section queries for, given the resolved tag context of the
 * tab's header revision (`GET /webapi/insights/resolve`).
 */
export function resolveSectionRelease(sectionId, resolved, fallbackVersion = "") {
  if (!resolved) return fallbackVersion || "";
  if (sectionId === "merge-window") {
    if (resolved.rev === "latest" || (resolved.latest && (!resolved.exact || !resolved.final))) return "latest";
    return resolved.final || "";
  }
  if (sectionId === "stable") {
    return resolved.series || resolved.final || resolved.tag || "";
  }
  if (sectionId === "security") {
    return resolved.tag || "";
  }
  // release-stats, subsystems
  if (resolved.rev === "latest" || (!resolved.exact && resolved.latest)) return "latest";
  return resolved.tag || "";
}

function fallbackResolved(version) {
  const p = parseTag(version);
  if (!p) return { rev: version, tag: version === "latest" ? "" : version, exact: true, latest: version === "latest", source: "tag", final: null, series: null };
  const ser = seriesOf(version) || null;
  return { rev: version, tag: version, exact: true, latest: false, source: "tag", final: p.final ? version : ser, series: ser };
}

export class InsightsView extends SectionedView {
  constructor() {
    super({ type: "insights", title: "Insights", sections: SECTIONS });
    this.resolved = null;
    this._resolveError = null;
  }

  async prepareRender() {
    this.resolved = null;
    this._resolveError = null;
    this.abort?.abort();
    this.abort = new AbortController();
    const signal = this.abort.signal;
    const ver = this.version;
    const client = this.api || api;
    if (ver && typeof client.resolveInsightsTag === "function") {
      try {
        this.resolved = await client.resolveInsightsTag(ver, { signal });
      } catch (err) {
        if (err?.name === "AbortError" || signal.aborted) return;
        this._resolveError = err?.message || String(err);
      }
    } else if (ver) {
      this.resolved = fallbackResolved(ver);
    }
    if (signal.aborted || !this.containerEl?.isConnected) return;
    this.updateDetectedBadge();
  }

  updateDetectedBadge() {
    const badge = this.containerEl?.querySelector(".sv-detected");
    if (!badge) return;
    const res = this.resolved;
    const target = resolveSectionRelease(this.section?.id, res, this.version);
    if (res && !res.exact && (target || res.tag)) {
      const label = target === "latest" && res.tag ? `latest (${res.tag}+)` : `tag: ${target || res.tag}`;
      badge.textContent = label;
      badge.title = `Autodetected ${target || res.tag} for ${shortRef(this.version)}`;
      badge.hidden = false;
    } else if (res && res.exact && target && target !== this.version && this.version !== "latest") {
      badge.textContent = target === "latest" ? "latest" : `series: ${target}`;
      badge.title = `Showing ${target} for ${this.version}`;
      badge.hidden = false;
    } else {
      badge.hidden = true;
      badge.textContent = "";
      badge.title = "";
    }
  }

  ctx(extra = {}) {
    const base = super.ctx(extra);
    if (!("release" in extra)) {
      base.release = this._resolveError ? "" : resolveSectionRelease(this.section?.id, this.resolved, this.version);
    }
    base.resolved = this.resolved;
    return base;
  }

  applyFilter(opts = {}) {
    super.applyFilter(opts);
    if (this._resolveError) {
      this.setStatus(`Could not load: ${this._resolveError}`);
    }
  }

  loadList() {
    if (this._resolveError) {
      this.rows = [];
      const summary = this.containerEl?.querySelector(".sv-summary");
      if (summary) {
        summary.hidden = true;
        summary.innerHTML = "";
      }
      this.applyFilter();
      return;
    }
    return super.loadList();
  }

  showDetail(row, key) {
    if (this._resolveError) {
      const el = this.containerEl?.querySelector(".sv-detail");
      if (el) el.innerHTML = `<div class="sv-empty">Could not load: ${escapeHtml(this._resolveError)}</div>`;
      return;
    }
    return super.showDetail(row, key);
  }
}
