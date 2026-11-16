/**
 * release_sections.js - Insights · Release stats (H6), Merge window (H5) and Subsystem health (H7)
 * (webapp/API.md §5.30).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";
import { showPersonModal } from "../../components/person_modal.js";
import { registerInsightsSection } from "./insights_view.js";
import { toTsv } from "../sections/section_model.js";
import { compareReleases } from "../sections/release_order.js";
import { barChartSvg, hbarsHtml, kpiHtml, sparklineSvg } from "../../utils/svg_charts.js";
import { hasFeature, isManPagesRepo } from "../../utils/projects.js";

const n = (x) => (x == null ? "—" : Number(x).toLocaleString("en-US"));
const date = (t) => (t ? new Date(t * 1000).toISOString().slice(0, 10) : "");
const ROLE_LABEL = { authors: "Authors", reviewers: "Reviewed-by", testers: "Tested-by", reporters: "Reported-by", ackers: "Acked-by", signers: "Signed-off-by", first: "First-timers" };

// ------------------------------------------------------------------ H6 release stats
async function withLatestReleases(ctx) {
  const names = (await ctx.view?.defaultReleases?.()) || [];
  return ["latest", ...names.filter((x) => x !== "latest")];
}

function targetRev(ctx, reportRev) {
  return reportRev || (ctx.release === "latest" ? (ctx.view?.version || ctx.release) : ctx.release);
}

registerInsightsSection({
  id: "release-stats", label: "Release stats", order: 10, releaseScoped: true, keywords: "statistics contributors employers authors",
  visible: (repo) => hasFeature(repo, "commits"),
  hint: "Commits, contributors, first-timers, reviewers and employers of a release",
  releaseOptions: withLatestReleases,
  emptyText: "No commits in this release cycle yet.",
  async fetchList(ctx) {
    const r = await (ctx.api || api).getInsights(ctx.release, "release-stats", { ...ctx, cacheKey: `rstats2_${ctx.release}`, networkFirst: ctx.release === "latest" });
    this._targetRev = r.branch || r.head || ctx.release;
    const rows = [];
    for (const [role, list] of Object.entries(r.top || {})) {
      list.forEach((p, i) => rows.push({ role, rank: i + 1, ...p, key: `${role}:${p.email || p.person}` }));
    }
    (r.first_timers || []).forEach((p, i) => rows.push({ role: "first", rank: i + 1, person: p.person, email: p.email, count: p.commits, key: `first:${p.email}` }));
    // Trend over the releases before this one (cached statistics only)
    let trend = "";
    try {
      const all = (await ctx.view?.defaultReleases?.()) || [];
      const idx = all.indexOf(ctx.release);
      const start = idx >= 0 ? idx : 0;
      const window = all.slice(start, start + 12).reverse();
      if (window.length) {
        const series = await (ctx.api || api).request(`/api/insights/release-stats/series?${window.map((t) => `tag=${encodeURIComponent(t)}`).join("&")}`);
        const pts = (series.series || []).filter((x) => !x.pending);
        if (pts.length > 1) trend = `<div class="sv-trend">Commits, last ${pts.length} releases ${sparklineSvg(pts.map((x) => x.commits), { title: pts.map((x) => `${x.release}: ${x.commits}`).join(", ") })}</div>`;
      }
    } catch {
      trend = "";
    }
    const summaryHtml = kpiHtml([
      [n(r.commits), "commits", `${n(r.merges)} merges`], [n(r.authors), "authors"], [n(r.first_time_authors), "first-timers"],
      [r.days == null ? "—" : `${r.days} d`, "since " + (r.prev || "?")], [`+${n(r.lines_added)} −${n(r.lines_removed)}`, "lines", `${n(r.files_changed)} files`],
      [n(r.rc_count), "release candidates"],
    ]) + `<div class="sv-sub">Employers (by commits)</div>${hbarsHtml((r.employers || []).slice(0, 12).map((e) => ({ label: e.org, value: e.commits, title: `${e.authors} authors` })))}${trend}`;
    this._last = r;
    const endLabel = r.branch ? `${r.branch} (unreleased)` : r.release;
    const source = r.release === "latest"
      ? "unreleased cycle · computed from Git"
      : (r.source === "database" ? "from the parser's release statistics" : "computed from Git (not summarized by the parser yet)");
    return { rows, summaryHtml, note: `${r.release}: ${r.prev || "first release"}..${endLabel} · ${source}${r.first_time_authors == null ? " · first-timers: mainline releases only" : ""}` };
  },
  rowKey: (r) => r.key,
  rowText: (r) => `${r.person} ${r.email} ${ROLE_LABEL[r.role]}`,
  rowHtml: (r) => `<span class="sv-tag info">${escapeHtml(ROLE_LABEL[r.role] || r.role)}</span><span class="sv-row-main">${escapeHtml(r.person)}</span><span class="sv-row-sub">${n(r.count)}${r.share ? ` · ${(r.share * 100).toFixed(1)}%` : ""}</span>`,
  chips: Object.entries(ROLE_LABEL).map(([id, label]) => ({ id, label, test: (r) => r.role === id })),
  detailHtml: (r, ctx) => `<h2>${escapeHtml(r.person)}</h2>
    <dl class="sv-kv"><dt>Email</dt><dd>${escapeHtml(r.email)}</dd><dt>${escapeHtml(ROLE_LABEL[r.role])}</dt><dd>${n(r.count)} commit(s) in ${escapeHtml(ctx.release)}${r.share ? ` (${(r.share * 100).toFixed(2)}%)` : ""}</dd></dl>
    <p><a href="#" data-profile="${escapeHtml(r.email || r.person)}">Developer profile</a> ·
       <a href="#" data-query="${escapeHtml(`${r.role === "authors" || r.role === "first" ? `author:${r.email}` : `${{ reviewers: "reviewer", testers: "tested", reporters: "reported", ackers: "acked", signers: "signed" }[r.role]}:${r.email}`} in:${ctx.release}`)}">Their commits in ${escapeHtml(ctx.release)}</a></p>`,
  onDetailClick(e, ctx) {
    const rev = targetRev(ctx, this._targetRev);
    const prof = e.target.closest("[data-profile]");
    if (prof) {
      e.preventDefault();
      showPersonModal(rev, prof.dataset.profile);
      return true;
    }
    const q = e.target.closest("[data-query]");
    if (q) {
      e.preventDefault();
      state.openTab({ type: "commits", title: "Commits", query: q.dataset.query, version: rev, forceNew: ctx.forceNew || e.ctrlKey || e.metaKey });
      return true;
    }
    return false;
  },
  downloads: [
    { label: "People", ext: "tsv", build: (rows) => toTsv(rows, [["role", (r) => r.role], ["rank", (r) => r.rank], ["person", (r) => r.person], ["email", (r) => r.email], ["count", (r) => r.count]]) },
  ],
});

// ------------------------------------------------------------------ H5 merge window
const GENERIC_REF_WORDS = new Set([
  "for", "to", "from", "of", "in", "on", "the", "and",
  "linus", "torvalds", "upstream", "misc", "linux", "next",
  "master", "main", "head", "devel", "dev", "work",
  "branch", "branches", "tag", "tags", "pull", "pulls", "request", "merge",
  "fixes", "fix", "urgent", "updates", "update", "changes", "change", "cleanup", "cleanups",
  "stable", "testing", "current", "baseline",
]);

export function isGenericRef(ref) {
  const s = String(ref || "").trim();
  if (!s) return true;
  const tokens = s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  return tokens.every((t) => GENERIC_REF_WORDS.has(t) || /^v?\d+$/.test(t) || /^rc\d*$/.test(t));
}

export function repoNameFromUrl(url) {
  const clean = String(url || "").trim().replace(/[?#].*$/, "").replace(/\/+$/, "").replace(/\.git$/i, "");
  if (!clean) return "";
  const parts = clean.split("/").filter(Boolean);
  if (!parts.length) return "";
  const last = parts[parts.length - 1];
  if (/^(?:linux|linux-2\.6|kernel)$/i.test(last) && parts.length >= 2) {
    return `${parts[parts.length - 2]}/${last}`;
  }
  return last;
}

export function mergeRowTag(p) {
  const ref = String(p?.ref || "").trim();
  const repo = repoNameFromUrl(p?.source_url);
  if (ref && (!isGenericRef(ref) || !repo)) return ref;
  return repo || ref || "";
}

function renderMergeChart(chart, selectedPull) {
  if (!chart) return "";
  const markers = chart.rc1Marker ? [chart.rc1Marker] : [];
  let highlightX = null;
  if (selectedPull && selectedPull.day != null) {
    highlightX = Math.trunc(selectedPull.day);
    markers.push({ x: selectedPull.day, label: `d${selectedPull.day}`, color: "var(--accent-green)", dash: "", width: 1.5, y: 26 });
  }
  return barChartSvg(chart.pts, { markers, highlightX, unit: " commits", x0: chart.x0, x1: chart.x1 });
}

registerInsightsSection({
  id: "merge-window", label: "Merge window", order: 20, releaseScoped: true, keywords: "pulls merge window rc1 maintainers",
  visible: (repo) => hasFeature(repo, "commits") && !isManPagesRepo(repo) && (hasFeature(repo, "kconfig") || hasFeature(repo, "maintainers") || hasFeature(repo, "mail")),
  hint: "The pulls merged for a release, day by day",
  releaseOptions: withLatestReleases,
  emptyText: "No merge pulls in this cycle yet.",
  async fetchList(ctx) {
    const r = await (ctx.api || api).getInsights(ctx.release, "merge-window", { ...ctx, cacheKey: `mwin_${ctx.release}`, networkFirst: ctx.release === "latest" });
    this._targetRev = r.branch || r.head || ctx.release;
    const pts = (r.totals?.by_day || []).map((d) => ({ x: d.day, value: d.commits, label: `day ${d.day}` }));
    const dayVals = [...pts.map((d) => d.x), ...(r.pulls || []).filter((p) => p.day != null).map((p) => Math.trunc(p.day))];
    this._chart = pts.length ? {
      pts,
      x0: Math.min(0, ...dayVals),
      x1: Math.max(0, ...dayVals),
      rc1Marker: r.rc1_day != null ? { x: r.rc1_day, label: r.rc1 } : null,
    } : null;
    const summaryHtml = kpiHtml([[n(r.totals?.pulls), "pulls"], [n(r.totals?.commits), "commits pulled"], [n(r.totals?.before_rc1), "before rc1"], [r.rc1_day == null ? "—" : `day ${r.rc1_day}`, r.rc1 || "rc1"]])
      + `<div class="sv-chart-wrap">${renderMergeChart(this._chart, null)}</div>`;
    const endLabel = r.branch ? `${r.branch} (unreleased)` : r.release;
    return { rows: r.pulls || [], summaryHtml, note: `${r.prev || "?"}..${endLabel}` };
  },
  rowKey: (p) => p.merge,
  rowText: (p) => `${p.subject} ${p.source_url} ${p.ref} ${p.sender} ${p.section?.name || ""}`,
  toggles: [{ id: "sender", label: "Sender", title: "Show the pull sender in the list", stateKey: "showSender" }],
  rowHtml: (p, ctx = {}) => {
    const tag = mergeRowTag(p);
    const showSender = Boolean(ctx?.showSender);
    const label = tag || p.subject || p.sender;
    const sender = p.sender && p.sender !== label ? p.sender : "";
    return `<span class="sv-row-sub">d${p.day ?? "?"}</span>`
      + `<span class="sv-row-main" title="${escapeHtml(p.subject || label)}">${showSender && sender ? `${escapeHtml(sender)} <span class="sv-row-muted">· ${escapeHtml(label)}</span>` : escapeHtml(label)}</span>`
      + `${p.section ? `<span class="sv-tag info">${escapeHtml(p.section.name.slice(0, 24))}</span>` : ""}`
      + `<span class="sv-row-sub">${n(p.commits)}</span>`;
  },
  chips: [
    { id: "before", label: "before rc1", test: (p) => !p.after_rc1 },
    { id: "after", label: "after rc1", test: (p) => p.after_rc1 },
    { id: "big", label: "≥ 100 commits", test: (p) => p.commits >= 100 },
  ],
  detailHtml: (p, ctx) => `<h2>${escapeHtml(p.subject)}</h2>
    <dl class="sv-kv">
      <dt>Merge</dt><dd><a href="#" data-commit="${escapeHtml(p.merge)}">${escapeHtml(p.merge.slice(0, 12))}</a> · ${escapeHtml(date(p.date))} (day ${p.day ?? "?"}${p.after_rc1 ? ", after rc1" : ""})</dd>
      <dt>Source</dt><dd>${escapeHtml(p.source_url || "local branch")} ${p.ref ? `<code>${escapeHtml(p.ref)}</code>` : ""}</dd>
      ${p.sender ? `<dt>Sender</dt><dd>${escapeHtml(p.sender)}</dd>` : ""}
      <dt>Commits</dt><dd>${n(p.commits)} — <a href="#" data-query="${escapeHtml(`range:${p.parents[0]}..${p.parents[1]}`)}">list them</a></dd>
      ${p.section && hasFeature(ctx.view?.repo(), "maintainers") ? `<dt>Subsystem</dt><dd>${p.section.sec_id != null ? `<a href="#" data-section="${escapeHtml(p.section.sec_id)}">${escapeHtml(p.section.name)}</a>` : escapeHtml(p.section.name)}</dd>` : ""}
      ${p.thread && hasFeature(ctx.view?.repo(), "mail") ? `<dt>Pull request</dt><dd><a href="#" data-mid="${escapeHtml(p.thread.message_id)}" data-tid="${p.thread.thread_id}">[GIT PULL] thread</a></dd>` : ""}
    </dl>
    ${p.dirs?.length ? `<h3>Top directories</h3>${hbarsHtml(p.dirs.map((d) => ({ label: d.dir, value: d.percent })), { max: 100, format: (v) => `${v}%` })}` : ""}
    ${p.body ? `<h3>Tag message</h3><pre>${escapeHtml(p.body)}</pre>` : ""}`,
  afterDetail(_el, pull, ctx) {
    const wrap = ctx.view?.containerEl?.querySelector(".sv-chart-wrap");
    if (wrap && this._chart) wrap.innerHTML = renderMergeChart(this._chart, pull);
  },
  onDetailClick(e, ctx) {
    const nt = ctx.forceNew || e.ctrlKey || e.metaKey;
    const rev = targetRev(ctx, this._targetRev);
    const q = e.target.closest("[data-query]");
    if (q) {
      e.preventDefault();
      state.openTab({ type: "commits", title: "Commits", query: q.dataset.query, version: rev, forceNew: nt });
      return true;
    }
    const sec = e.target.closest("[data-section]");
    if (sec) {
      e.preventDefault();
      state.openTab({ type: "maintainers", title: "Maintainers", secId: parseInt(sec.dataset.section, 10), version: rev, forceNew: nt });
      return true;
    }
    const mail = e.target.closest("[data-mid]");
    if (mail) {
      e.preventDefault();
      state.openTab({ type: "mail", title: "LKML pull", threadId: parseInt(mail.dataset.tid, 10) || 0, messageId: mail.dataset.mid, forceNew: nt });
      return true;
    }
    return false;
  },
  downloads: [{
    label: "Pulls", ext: "tsv", build: (rows) => toTsv(rows, [["merge", (p) => p.merge], ["day", (p) => p.day], ["after_rc1", (p) => p.after_rc1],
    ["commits", (p) => p.commits], ["source", (p) => p.source_url], ["ref", (p) => p.ref], ["sender", (p) => p.sender], ["section", (p) => p.section?.name || ""], ["subject", (p) => p.subject]])
  }],
});

// ------------------------------------------------------------------ H7 subsystem health
const FLAG = { orphan_active: "orphan but active", silent_maintainers: "silent maintainers", single_author: "single author", no_review: "no review", no_activity: "no activity" };

registerInsightsSection({
  id: "subsystems", label: "Subsystem health", order: 30, releaseScoped: true, keywords: "maintainers health review bus factor orphan",
  visible: (repo) => hasFeature(repo, "maintainers"),
  hint: "Review coverage, bus factor and maintainer activity of every MAINTAINERS section",
  releaseOptions: withLatestReleases,
  emptyText: "No MAINTAINERS sections found.",
  windowOptions: [1, 2, 3, 4, 6, 8, 12], windowDefault: 4, windowLabel: (w) => `${w} release${w > 1 ? "s" : ""}`,
  async fetchList(ctx) {
    const r = await (ctx.api || api).getInsights(ctx.release, `subsystems?window=${ctx.window || 4}`, { ...ctx, cacheKey: `health_${ctx.release}_${ctx.window || 4}`, networkFirst: ctx.release === "latest" });
    this._targetRev = r.branch || r.head || ctx.release;
    this._window = r.window;
    const flagged = {};
    for (const s of r.sections) for (const f of s.flags) flagged[f] = (flagged[f] || 0) + 1;
    const endLabel = r.branch ? `${r.branch} (unreleased)` : r.window[r.window.length - 1];
    return {
      rows: r.sections, note: `${r.window[0]}..${endLabel}`,
      summaryHtml: kpiHtml([[n(r.sections.length), "sections"], ...Object.entries(FLAG).map(([k, l]) => [n(flagged[k] || 0), l])])
    };
  },
  rowKey: (s) => (s.sec_id != null ? String(s.sec_id) : String(s.name || "")),
  rowText: (s) => `${s.name} ${s.status} ${s.maintainers.map((m) => m.person).join(" ")}`,
  rowHtml: (s) => `<span class="sv-row-main">${escapeHtml(s.name)}</span>${s.flags.slice(0, 2).map((f) => `<span class="sv-tag ${f === "no_activity" ? "info" : "warn"}">${escapeHtml(FLAG[f])}</span>`).join("")}<span class="sv-row-sub">${n(s.commits)} · ${(s.review_coverage * 100).toFixed(0)}%</span>`,
  chips: Object.entries(FLAG).map(([id, label]) => ({ id, label, test: (s) => s.flags.includes(id) })),
  detailHtml(s, ctx) {
    const win = this._window || [];
    const openMaint = s.sec_id != null ? `<a href="#" data-section="${escapeHtml(s.sec_id)}">Open in Maintainers</a> · ` : "";
    const secFilter = s.sec_id != null ? `section:${s.sec_id}` : `section:${JSON.stringify(String(s.name || ""))}`;
    return `<h2>${escapeHtml(s.name)}</h2>
    <dl class="sv-kv">
      <dt>Status</dt><dd>${escapeHtml(s.status)}</dd>
      <dt>Commits</dt><dd>${n(s.commits)} over ${win.length} release(s) ${sparklineSvg(s.per_release, { title: win.map((t, i) => `${t}: ${s.per_release[i]}`).join(", ") })}</dd>
      <dt>Authors</dt><dd>${n(s.authors)} · bus factor ${n(s.bus_factor)}</dd>
      <dt>Review coverage</dt><dd>${(s.review_coverage * 100).toFixed(1)}% of commits carry a Reviewed-by or Acked-by from someone other than the author</dd>
      ${s.flags.length ? `<dt>Flags</dt><dd>${s.flags.map((f) => `<span class="sv-tag warn">${escapeHtml(FLAG[f])}</span>`).join(" ")}</dd>` : ""}
    </dl>
    <h3>Maintainers and reviewers — last activity in the window</h3>
    <table>${s.maintainers.map((m) => `<tr><td>${escapeHtml(m.role)}</td><td>${escapeHtml(m.person)}</td><td>${m.last_active ? escapeHtml(date(m.last_active)) : '<span class="sv-tag warn">none</span>'}</td></tr>`).join("") || "<tr><td>none listed</td></tr>"}</table>
    <h3>Top authors</h3><table>${s.top_authors.map((a) => `<tr><td>${escapeHtml(a.identity.replace(/^[ne]:/, ""))}</td><td>${n(a.commits)}</td></tr>`).join("")}</table>
    <p>${openMaint}<a href="#" data-query="${escapeHtml(`${secFilter} in:${ctx.release}`)}">Commits touching this section in ${escapeHtml(ctx.release)}</a></p>`;
  },
  onDetailClick(e, ctx) {
    const nt = ctx.forceNew || e.ctrlKey || e.metaKey;
    const rev = targetRev(ctx, this._targetRev);
    const sec = e.target.closest("[data-section]");
    if (sec) {
      e.preventDefault();
      state.openTab({ type: "maintainers", title: "Maintainers", secId: parseInt(sec.dataset.section, 10), version: rev, forceNew: nt });
      return true;
    }
    const q = e.target.closest("[data-query]");
    if (q) {
      e.preventDefault();
      state.openTab({ type: "commits", title: "Commits", query: q.dataset.query, version: rev, forceNew: nt });
      return true;
    }
    return false;
  },
  downloads: [{
    label: "Sections", ext: "tsv", build: (rows) => toTsv(rows, [["sec_id", (s) => s.sec_id ?? ""], ["name", (s) => s.name], ["status", (s) => s.status], ["commits", (s) => s.commits],
    ["authors", (s) => s.authors], ["review_coverage", (s) => s.review_coverage], ["bus_factor", (s) => s.bus_factor], ["flags", (s) => s.flags.join(",")]])
  }],
});

export { compareReleases };
