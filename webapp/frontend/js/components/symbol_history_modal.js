/**
 * symbol_history_modal.js - Release slider over the history of one definition or Kconfig symbol.
 *
 * Stops are the releases where the definition changed (plus the latest one); runs of releases
 * without change collapse into "…" gaps that expand on click. Release candidates are hidden
 * unless "Include RCs" is ticked. Each stop shows the code (or diff to the previous stop), the
 * Kconfig properties that changed, and on demand the commits behind the change (`git log -L`).
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { openModal, escapeHtml } from "./modal.js";
import { shortRef } from "./ref_picker.js";

const STATUS_LABEL = {
  introduced: "introduced",
  added: "re-added",
  unchanged: "unchanged",
  modified: "modified",
  cosmetic: "formatting",
  removed: "removed",
  absent: "absent",
  unindexed: "not indexed",
  unparsed: "not in index",
};
const CHANGE_STATUSES = new Set(["introduced", "added", "modified", "cosmetic", "removed"]);
const FIELD_LABEL = {
  kind: "kind",
  type: "type",
  prompt: "prompt",
  choice: "choice",
  help: "help",
  defaults: "default",
  depends_on: "depends on",
  inherited_depends: "inherited depends",
  selects: "select",
  implies: "imply",
  ranges: "range",
  options: "option",
};
const PLAY_INTERVAL_MS = 1400;

/** History of the definition at filePath:line (anchored on `name` when the cursor is on a symbol). */
export function showSymbolHistoryModal(version, filePath, line, astId = null, name = "") {
  openHistory({
    title: name || `${filePath.split("/").pop()}:${line}`,
    subtitle: `${filePath}:${line} @ ${shortRef(version)}`,
    version,
    load: (includeRc) => api.getSymbolHistory(version, filePath, line, astId, { name, includeRc }),
  });
}

/** History of CONFIG_<name> across every Kconfig file defining it. */
export function showKconfigHistoryModal(version, name) {
  const sym = String(name || "").replace(/^CONFIG_/, "");
  openHistory({
    title: `CONFIG_${sym}`,
    subtitle: `Kconfig symbol @ ${shortRef(version)}`,
    version,
    load: (includeRc) => api.getKconfigHistory(version, sym, { includeRc }),
  });
}

function openHistory({ title, subtitle, version, load }) {
  const modal = openModal({ title: `🕰 History: ${title}`, subtitle, width: "960px" });
  const ui = { includeRc: false, expandAll: false, expanded: new Set(), pos: 0, view: "diff", showAllDefs: false, timer: null, res: null };

  const alive = () => document.body.contains(modal.el);
  const stopPlay = () => {
    clearInterval(ui.timer);
    ui.timer = null;
  };
  const onKey = (e) => {
    if (!alive()) {
      document.removeEventListener("keydown", onKey);
      stopPlay();
      return;
    }
    if (e.target.closest && e.target.closest("input[type=text], textarea")) return;
    if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    else return;
    e.preventDefault();
  };
  document.addEventListener("keydown", onKey);

  async function fetchAndRender(keepVersion = null) {
    stopPlay();
    modal.body.innerHTML = `<div class="ki-modal-empty">Tracing the definition across indexed releases…</div>`;
    try {
      ui.res = await load(ui.includeRc);
    } catch (err) {
      if (alive()) modal.body.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
      return;
    }
    if (!alive()) return;
    const res = ui.res;
    modal.el.querySelector(".ki-modal-title").textContent = `🕰 History: ${res.ast_name || title}`;
    // Select the last stop at or before the version the history was opened from
    const stops = computeStops(res.timeline || [], ui).filter((s) => s.type === "stop");
    const wantIdx = res.timeline.findIndex((t) => t.vname === (keepVersion || version));
    const atOrBefore = wantIdx < 0 ? -1 : stops.reduce((best, s, n) => (s.idx <= wantIdx ? n : best), -1);
    ui.pos = atOrBefore >= 0 ? atOrBefore : Math.max(0, stops.length - 1);
    render();
  }

  function stopList() {
    return computeStops(ui.res.timeline || [], ui);
  }

  function step(delta) {
    const stops = stopList().filter((s) => s.type === "stop");
    const next = ui.pos + delta;
    if (next < 0 || next >= stops.length) return false;
    ui.pos = next;
    render();
    return true;
  }

  function render() {
    const res = ui.res;
    const timeline = res.timeline || [];
    const items = stopList();
    const stops = items.filter((s) => s.type === "stop");
    ui.pos = Math.min(ui.pos, Math.max(0, stops.length - 1));
    const current = stops.length ? timeline[stops[ui.pos].idx] : null;
    const isKconfig = res.kind === "kconfig";
    const releases = timeline.length;

    modal.body.innerHTML = `
      <div class="symhist-meta">
        ${isKconfig
          ? `<span>Defined in <strong>${(res.paths || []).length}</strong> Kconfig file${(res.paths || []).length === 1 ? "" : "s"}</span>`
          : `<span>File <code>${escapeHtml(res.path)}</code></span>${res.type_name ? `<span>${escapeHtml(res.type_name)}</span>` : ""}`}
        <span>Present in <strong>${res.total_versions}</strong> of ${releases} ${ui.includeRc ? "indexed tags" : "releases"}</span>
        <span><strong>${res.change_count}</strong> change${res.change_count === 1 ? "" : "s"}</span>
      </div>
      <div class="symhist-controls">
        <button class="code-btn" data-act="prev" type="button" title="Previous change (←)">◀</button>
        <button class="code-btn" data-act="play" type="button" title="Play through the changes">${ui.timer ? "⏸ Pause" : "▶ Play"}</button>
        <button class="code-btn" data-act="next" type="button" title="Next change (→)">▶</button>
        <input type="range" min="0" max="${Math.max(0, stops.length - 1)}" value="${ui.pos}" step="1" ${stops.length < 2 ? "disabled" : ""} aria-label="Change">
        <label title="Show every release instead of collapsing unchanged ones"><input type="checkbox" data-act="expand" ${ui.expandAll ? "checked" : ""}> All releases</label>
        <label title="Also list release candidates (more detail)"><input type="checkbox" data-act="rc" ${ui.includeRc ? "checked" : ""}> Include RCs</label>
      </div>
      <div class="symhist-track">
        ${items.length === 0 ? `<span class="ki-modal-empty">No indexed release in this repository yet.</span>` : items.map((s) => {
          if (s.type === "gap") {
            return `<button class="symhist-gap" type="button" data-gap="${s.key}" title="${s.count} release${s.count === 1 ? "" : "s"} without change (${escapeHtml(timeline[s.from].vname)} … ${escapeHtml(timeline[s.to].vname)}), click to expand">…</button>`;
          }
          const t = timeline[s.idx];
          const n = stops.indexOf(s);
          return `<button class="symhist-chip ${t.status} ${t.is_release ? "" : "rc"} ${n === ui.pos ? "selected" : ""}" type="button" data-stop="${n}" title="${escapeHtml(t.vname)}: ${STATUS_LABEL[t.status] || t.status}">${escapeHtml(t.vname)}<small>${STATUS_LABEL[t.status] || t.status}</small></button>`;
        }).join("")}
      </div>
      <div class="symhist-detail">${current ? renderDetail(current, res, ui) : ""}</div>`;

    bind(current);
  }

  function bind(current) {
    const body = modal.body;
    body.querySelector('[data-act="prev"]').onclick = () => step(-1);
    body.querySelector('[data-act="next"]').onclick = () => step(1);
    body.querySelector('[data-act="play"]').onclick = () => {
      if (ui.timer) {
        stopPlay();
        render();
        return;
      }
      const stops = stopList().filter((s) => s.type === "stop");
      if (ui.pos >= stops.length - 1) ui.pos = 0;
      ui.timer = setInterval(() => {
        if (!alive() || !step(1)) {
          stopPlay();
          if (alive()) render();
        }
      }, PLAY_INTERVAL_MS);
      render();
    };
    body.querySelector('input[type="range"]').oninput = (e) => {
      ui.pos = parseInt(e.target.value, 10);
      render();
    };
    body.querySelector('[data-act="expand"]').onchange = (e) => {
      const keep = current?.vname;
      ui.expandAll = e.target.checked;
      selectVersion(keep);
    };
    body.querySelector('[data-act="rc"]').onchange = (e) => {
      ui.includeRc = e.target.checked;
      ui.expanded.clear();
      fetchAndRender(current?.vname);
    };
    body.querySelectorAll("[data-stop]").forEach((btn) => {
      btn.onclick = () => {
        ui.pos = parseInt(btn.dataset.stop, 10);
        render();
      };
    });
    body.querySelectorAll("[data-gap]").forEach((btn) => {
      btn.onclick = () => {
        const keep = current?.vname;
        ui.expanded.add(btn.dataset.gap);
        selectVersion(keep);
      };
    });
    body.querySelectorAll("[data-view]").forEach((btn) => {
      btn.onclick = () => {
        ui.view = btn.dataset.view;
        render();
      };
    });
    const allDefs = body.querySelector('[data-act="all-defs"]');
    if (allDefs) {
      allDefs.onclick = () => {
        ui.showAllDefs = !ui.showAllDefs;
        render();
      };
    }
    body.querySelectorAll("[data-open-path]").forEach((btn) => {
      btn.onclick = () => {
        modal.close();
        stopPlay();
        state.openTab({
          type: "code",
          title: btn.dataset.openPath.split("/").pop(),
          path: btn.dataset.openPath,
          version: btn.dataset.openVersion,
          cursorLine: parseInt(btn.dataset.openLine, 10) || 1,
          forceNew: true,
        });
      };
    });
    body.querySelectorAll("[data-commits]").forEach((btn) => {
      btn.onclick = () => loadCommits(btn, current);
    });
  }

  function selectVersion(vname) {
    const items = stopList().filter((s) => s.type === "stop");
    const idx = items.findIndex((s) => ui.res.timeline[s.idx].vname === vname);
    if (idx >= 0) ui.pos = idx;
    render();
  }

  async function loadCommits(btn, entry) {
    const def = entry.defs[parseInt(btn.dataset.commits, 10)];
    const box = btn.closest(".symhist-def").querySelector(".symhist-commits");
    box.hidden = false;
    box.innerHTML = `<div class="ki-modal-empty">Running git log over ${escapeHtml(entry.prev_vname || "history")} → ${escapeHtml(entry.vname)}…</div>`;
    const removed = def.status === "removed";
    let res;
    try {
      res = await api.getHistoryCommits({
        path: def.path,
        to: entry.vname,
        from: entry.prev_vname,
        lineS: removed ? null : def.line_s,
        lineE: removed ? null : def.line_e,
      });
    } catch (err) {
      box.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
      return;
    }
    const scope = removed ? "touching the file" : `touching lines ${def.line_s}–${def.line_e}`;
    box.innerHTML = res.commits.length === 0
      ? `<div class="ki-modal-empty">No commit ${scope} in ${escapeHtml(entry.prev_vname || "")}..${escapeHtml(entry.vname)}.</div>`
      : `<div class="symhist-status unchanged">${res.commits.length} commit${res.commits.length === 1 ? "" : "s"} ${scope}${entry.prev_vname ? ` since ${escapeHtml(entry.prev_vname)}` : ""}</div>` +
        res.commits.map((c) => `
          <div class="symhist-commit">
            <code data-hash="${escapeHtml(c.commit_hash)}" title="Open commit">${escapeHtml(c.commit_hash_short)}</code>
            <span class="symhist-commit-subject">${escapeHtml(c.subject)}</span>
            <span class="symhist-commit-meta">${escapeHtml(c.author_name)} · ${c.author_date ? new Date(c.author_date * 1000).toISOString().slice(0, 10) : ""}</span>
          </div>`).join("");
    box.querySelectorAll("[data-hash]").forEach((el) => {
      el.onclick = () => {
        modal.close();
        stopPlay();
        state.openTab({ type: "commits", title: "Commits", version: entry.vname, commitId: el.dataset.hash, forceNew: true });
      };
    });
  }

  fetchAndRender();
}

/**
 * Slider items: a stop per change (plus the first and last versions and expanded releases),
 * and a gap item for each run of releases without change between two stops.
 */
function computeStops(timeline, ui) {
  if (!timeline.length) return [];
  // The newest version with indexed data closes the slider even when nothing changed there
  let lastKnown = timeline.length - 1;
  while (lastKnown > 0 && (timeline[lastKnown].status === "unindexed" || timeline[lastKnown].status === "unparsed")) lastKnown--;
  const isStop = (t, i) => ui.expandAll || i === 0 || i === lastKnown || CHANGE_STATUSES.has(t.status);
  const items = [];
  let runStart = -1;
  const flushRun = (end) => {
    if (runStart < 0) return;
    const key = `${timeline[runStart].vname}..${timeline[end].vname}`;
    if (ui.expanded.has(key)) {
      for (let i = runStart; i <= end; i++) items.push({ type: "stop", idx: i });
    } else {
      items.push({ type: "gap", key, from: runStart, to: end, count: end - runStart + 1 });
    }
    runStart = -1;
  };
  timeline.forEach((t, i) => {
    if (isStop(t, i)) {
      flushRun(i - 1);
      items.push({ type: "stop", idx: i });
    } else if (runStart < 0) {
      runStart = i;
    }
  });
  flushRun(timeline.length - 1);
  return items;
}

function renderDetail(entry, res, ui) {
  const isKconfig = res.kind === "kconfig";
  const range = entry.prev_vname ? `${escapeHtml(entry.prev_vname)} → ${escapeHtml(entry.vname)}` : escapeHtml(entry.vname);
  const counts = entry.lines_added || entry.lines_removed
    ? `<span class="diff-add">+${entry.lines_added}</span> <span class="diff-del">−${entry.lines_removed}</span>`
    : "";
  const note = {
    introduced: entry.prev_vname ? "" : "first indexed version",
    unparsed: "the file still names this symbol but its definition is missing from the index for this version",
    unindexed: "this version of the file has not been parsed",
    absent: "not defined in this version",
  }[entry.status] || "";

  let defs = entry.defs || [];
  const changedDefs = defs.filter((d) => d.status !== "unchanged");
  const hiddenDefs = isKconfig && !ui.showAllDefs && changedDefs.length > 0 ? defs.length - changedDefs.length : 0;
  if (hiddenDefs) defs = changedDefs;

  return `
    <div class="symhist-detail-head">
      <span class="symhist-version">${escapeHtml(entry.vname)}</span>
      <span class="symhist-status ${entry.status}">${STATUS_LABEL[entry.status] || entry.status}</span>
      <span class="ki-modal-subtitle">${range}</span>
      ${counts}
      ${note ? `<span class="symhist-status unchanged">${escapeHtml(note)}</span>` : ""}
      <span class="symhist-spacer"></span>
      <button class="code-btn ${ui.view === "diff" ? "active" : ""}" data-view="diff" type="button">Diff</button>
      <button class="code-btn ${ui.view === "code" ? "active" : ""}" data-view="code" type="button">Code</button>
      ${isKconfig && (hiddenDefs || ui.showAllDefs) ? `<button class="code-btn" data-act="all-defs" type="button">${ui.showAllDefs ? "Only changed files" : `Show all ${entry.defs.length} definitions`}</button>` : ""}
    </div>
    ${defs.length === 0 ? `<div class="ki-modal-empty">${escapeHtml(note || "No definition in this version.")}</div>` : defs.map((d) => renderDef(d, entry, res, ui, entry.defs.indexOf(d))).join("")}`;
}

function renderDef(d, entry, res, ui, defIdx) {
  const code = d.tag_id ? (res.snippets || {})[String(d.tag_id)] || "" : "";
  const where = d.line_s ? `${d.path}:${d.line_s}` : d.path;
  const openVersion = d.status === "removed" ? entry.prev_vname : entry.vname;
  const hasDiff = d.diff && d.diff.length;
  let body;
  if (d.status === "removed") {
    body = `<div class="ki-modal-empty">Definition removed from <code>${escapeHtml(d.path)}</code> (was at lines ${d.line_s}–${d.line_e} in ${escapeHtml(entry.prev_vname || "")}).</div>`;
  } else if (ui.view === "diff" && hasDiff) {
    body = `<div class="diff-box symhist-code"><div class="diff-content">${compactDiff(d.diff).map((x) => x.type === "skip"
      ? `<div class="diff-hunk">⋯ ${x.count} unchanged line${x.count === 1 ? "" : "s"}</div>`
      : `<div class="${x.type === "add" ? "diff-add" : x.type === "del" ? "diff-del" : ""}">${x.type === "add" ? "+" : x.type === "del" ? "-" : " "}${escapeHtml(x.text)}</div>`).join("")}</div></div>`;
  } else {
    body = `<div class="diff-box symhist-code"><div class="diff-content">${escapeHtml(code) || "<span class=\"symhist-status unchanged\">(code unavailable)</span>"}</div></div>`;
  }
  return `
    <div class="symhist-def">
      <div class="symhist-def-head">
        <code>${escapeHtml(where)}</code>
        <span class="symhist-status ${d.status}">${STATUS_LABEL[d.status] || d.status}</span>
        ${d.lines_added || d.lines_removed ? `<span class="diff-add">+${d.lines_added}</span><span class="diff-del">−${d.lines_removed}</span>` : ""}
        <span class="symhist-spacer"></span>
        <button class="code-btn" data-commits="${defIdx}" type="button" title="Commits behind this change (git log -L)">Commits</button>
        ${openVersion ? `<button class="code-btn" type="button" data-open-path="${escapeHtml(d.path)}" data-open-version="${escapeHtml(openVersion)}" data-open-line="${d.line_s || 1}">Open @ ${escapeHtml(shortRef(openVersion))}</button>` : ""}
      </div>
      ${d.changes && d.changes.length ? renderFieldChanges(d.changes) : ""}
      ${d.facts && !(d.changes && d.changes.length) && (d.status === "introduced" || d.status === "added") ? renderFacts(d.facts) : ""}
      ${body}
      <div class="symhist-commits" hidden></div>
    </div>`;
}

/** Keep DIFF_CONTEXT unchanged lines around each change; longer unchanged runs become one "skip" line. */
const DIFF_CONTEXT = 3;
function compactDiff(diff) {
  const keep = diff.map(() => false);
  diff.forEach((x, i) => {
    if (x.type === "ctx") return;
    for (let j = Math.max(0, i - DIFF_CONTEXT); j <= Math.min(diff.length - 1, i + DIFF_CONTEXT); j++) keep[j] = true;
  });
  const out = [];
  diff.forEach((x, i) => {
    if (keep[i]) out.push(x);
    else if (out.length && out[out.length - 1].type === "skip") out[out.length - 1].count++;
    else out.push({ type: "skip", count: 1 });
  });
  return out;
}

function renderFieldChanges(changes) {
  return `<table class="symhist-fields">${changes.map((c) => {
    const label = FIELD_LABEL[c.field] || c.field;
    if (Array.isArray(c.added) || Array.isArray(c.removed)) {
      return `<tr><td>${escapeHtml(label)}</td><td>${(c.removed || []).map((x) => `<code class="diff-del">− ${escapeHtml(x)}</code>`).join("")}${(c.added || []).map((x) => `<code class="diff-add">+ ${escapeHtml(x)}</code>`).join("")}${!(c.added || []).length && !(c.removed || []).length ? `<span class="symhist-status unchanged">reordered</span>` : ""}</td></tr>`;
    }
    if (c.field === "help") return `<tr><td>help</td><td><span class="symhist-status modified">text changed</span> (see diff)</td></tr>`;
    return `<tr><td>${escapeHtml(label)}</td><td>${c.before ? `<code class="diff-del">− ${escapeHtml(c.before)}</code>` : ""}${c.after ? `<code class="diff-add">+ ${escapeHtml(c.after)}</code>` : ""}</td></tr>`;
  }).join("")}</table>`;
}

function renderFacts(f) {
  const rows = [];
  if (f.type) rows.push(["type", `${f.kind} ${f.type}`]);
  if (f.prompt) rows.push(["prompt", f.prompt]);
  for (const key of ["defaults", "depends_on", "inherited_depends", "selects", "implies", "ranges"]) {
    if (f[key] && f[key].length) rows.push([FIELD_LABEL[key], f[key].join("  ·  ")]);
  }
  if (f.choice) rows.push(["choice", f.choice]);
  return rows.length ? `<table class="symhist-fields">${rows.map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(v)}</td></tr>`).join("")}</table>` : "";
}
