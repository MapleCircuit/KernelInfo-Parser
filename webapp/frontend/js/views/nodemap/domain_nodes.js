/**
 * domain_nodes.js - Content of the NodeMap's domain nodes (`webapp/API_FRONTEND.md` §5.3.2):
 * diffs, mailing-list threads, Kconfig symbols with relations, MAINTAINERS sections, CVE records,
 * Kernel API catalogs, Kernel Docs (`.rst` / kernel-doc), and Symbol History / File Blame.
 *
 * DOM-free (unit tested).
 */
import { MAIL_BODY_MAX } from "./canvas_model.js";
import { renderHtmlFragment } from "../docs/html_renderer.js";
import { renderDocument, renderKernelDocEntry } from "../docs/rst_renderer.js";
import { esc } from "./struct_view.js";
import { VIRTUAL_THRESHOLD, renderVirtualDiffWindow } from "./virtual_source.js";

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const cleanMid = (m) => String(m || "").trim().replace(/^<|>$/g, "");
const bareSym = (s) => String(s || "").trim().replace(/^CONFIG_/, "");

// ------------------------------------------------------------------ diff

export function parseUnifiedDiff(text, fallbackPath = "") {
  const files = [];
  let file = null;
  let hunk = null;
  let oldNo = 0;
  let newNo = 0;
  const startFile = (path, oldPath = path) => {
    file = { path, oldPath, status: "M", additions: 0, deletions: 0, binary: false, hunks: [] };
    files.push(file);
    hunk = null;
  };
  for (const line of String(text || "").replace(/\r\n/g, "\n").split("\n")) {
    const git = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (git) {
      startFile(git[2], git[1]);
      continue;
    }
    if (/^(---|\+\+\+) /.test(line) && !hunk) {
      const p = line.slice(4).replace(/^[ab]\//, "").replace(/\t.*$/, "");
      if (!file) startFile(p === "/dev/null" ? fallbackPath : p);
      if (line.startsWith("---")) {
        if (p === "/dev/null") file.status = "A";
        else file.oldPath = p;
      } else if (p === "/dev/null") file.status = "D";
      else file.path = p;
      continue;
    }
    const h = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/.exec(line);
    if (h) {
      if (!file) startFile(fallbackPath);
      oldNo = parseInt(h[1], 10);
      newNo = parseInt(h[3], 10);
      hunk = { header: line, heading: h[5] || "", lines: [] };
      file.hunks.push(hunk);
      continue;
    }
    if (!file) continue;
    if (/^Binary files /.test(line)) file.binary = true;
    if (/^new file mode/.test(line)) file.status = "A";
    else if (/^deleted file mode/.test(line)) file.status = "D";
    else if (/^rename from /.test(line)) file.status = "R";
    if (!hunk || !line || !"+- ".includes(line[0])) continue;
    const kind = line[0];
    const body = line.slice(1);
    if (kind === "+") {
      hunk.lines.push(["+", null, newNo++, body]);
      file.additions += 1;
    } else if (kind === "-") {
      hunk.lines.push(["-", oldNo++, null, body]);
      file.deletions += 1;
    } else hunk.lines.push([" ", oldNo++, newNo++, body]);
  }
  return files;
}

function commitFile(f) {
  const hunks = (Array.isArray(f?.hunks) ? f.hunks : []).map((h) => {
    const head = Number.isInteger(h.old_start)
      ? `@@ -${h.old_start},${h.old_count ?? 1} +${h.new_start},${h.new_count ?? 1} @@${h.heading ? ` ${h.heading}` : ""}`
      : `@@ ${h.heading || ""}`;
    return {
      header: head, heading: h.heading || "", lines: (Array.isArray(h.lines) ? h.lines : []).filter(Array.isArray).map((l) => [
        ["+", "-", " "].includes(l[0]) ? l[0] : " ",
        Number.isInteger(l[1]) ? l[1] : null,
        Number.isInteger(l[2]) ? l[2] : null,
        String(l[3] ?? ""),
      ])
    };
  });
  return {
    path: String(f?.path || ""),
    oldPath: String(f?.old_path || f?.path || ""),
    status: String(f?.status || "M"),
    additions: f?.additions || 0,
    deletions: f?.deletions || 0,
    binary: Boolean(f?.binary),
    truncated: Boolean(f?.truncated),
    hunks,
  };
}

export function diffPayload(data, res) {
  const r = isObj(res) ? res : {};
  let files;
  if (data.mode === "commit") {
    files = (Array.isArray(r.files) ? r.files : []).map(commitFile);
    if (data.path) {
      const only = files.filter((f) => f.path === data.path || f.oldPath === data.path);
      if (only.length) files = only;
    }
  } else {
    files = parseUnifiedDiff(r.diff || "", data.path);
    if (!files.length) files = [{ path: data.path, oldPath: data.oldPath || data.path, status: "M", additions: r.additions || 0, deletions: r.deletions || 0, binary: Boolean(r.is_binary), hunks: [] }];
    if (r.is_binary) files[0].binary = true;
  }
  return {
    mode: data.mode,
    versionA: data.versionA,
    versionB: data.versionB,
    commitHash: data.commitHash || String(r.commit_hash || ""),
    path: data.path,
    files,
    truncated: Boolean(r.truncated) || files.some((f) => f.truncated),
  };
}

export function diffRows(payload) {
  const rows = [];
  const files = payload?.files || [];
  const headers = files.length > 1 || payload?.mode === "commit";
  for (const f of files) {
    if (headers) {
      const name = f.oldPath && f.oldPath !== f.path ? `${f.oldPath} → ${f.path}` : f.path;
      rows.push({ kind: "file", old: null, new: null, text: `${f.status || "M"} ${name}  +${f.additions} −${f.deletions}` });
    }
    if (f.binary) rows.push({ kind: "note", old: null, new: null, text: "Binary file" });
    else if (!f.hunks.length) rows.push({ kind: "note", old: null, new: null, text: "No textual changes" });
    for (const h of f.hunks) {
      rows.push({ kind: "hunk", old: null, new: null, text: h.header });
      for (const [kind, o, n, t] of h.lines) rows.push({ kind, old: o, new: n, text: t });
    }
  }
  if (payload?.truncated) rows.push({ kind: "note", old: null, new: null, text: "… diff truncated by the server" });
  return rows;
}

const DIFF_CLASS = { "+": "nm-diff-add", "-": "nm-diff-del", " ": "nm-diff-ctx", hunk: "nm-diff-hunk", file: "nm-diff-file", note: "nm-diff-note" };

/** Live body of a diff node (virtualizes large diffs above `VIRTUAL_THRESHOLD`). */
export function renderDiffBody(payload, opts = {}) {
  const rows = diffRows(payload);
  if (!rows.length) return '<div class="nm-empty">No changes</div>';
  if (rows.length > VIRTUAL_THRESHOLD && opts.virtual !== false) {
    return `<div class="nm-diff nm-scroll nm-virtual">${renderVirtualDiffWindow(rows)}</div>`;
  }
  let html = "";
  for (const r of rows) {
    const cls = DIFF_CLASS[r.kind] || "nm-diff-ctx";
    if (r.kind === "file" || r.kind === "hunk" || r.kind === "note") {
      html += `<div class="nm-diff-row ${cls}"><span class="nm-diff-code">${esc(r.text)}</span></div>`;
    } else {
      html += `<div class="nm-diff-row ${cls}"><span class="nm-diff-no">${esc(r.old ?? "")}</span><span class="nm-diff-no">${esc(r.new ?? "")}</span><span class="nm-diff-sign">${r.kind === " " ? "" : esc(r.kind)}</span><span class="nm-diff-code">${esc(r.text)}</span></div>`;
    }
  }
  return `<div class="nm-diff nm-scroll">${html}</div>`;
}

// ------------------------------------------------------------------ mail

function refsOf(m) {
  const raw = m?.references;
  const list = Array.isArray(raw) ? raw : String(raw || "").split(/[\s,]+/);
  return list.map(cleanMid).filter(Boolean);
}

export function bodyExcerpt(body, max = 10) {
  const lines = String(body || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  for (const l of lines) {
    if (/^\s*>/.test(l)) continue;
    if (!out.length && !l.trim()) continue;
    if (/^-- $/.test(l) || /^---$/.test(l.trim())) break;
    out.push(l.replace(/\t/g, "    "));
    if (out.length >= max) break;
  }
  while (out.length && !out[out.length - 1].trim()) out.pop();
  return out;
}

/** Row anchor key of a message inside a mail node (`fromField` of the edges spawned from it). */
export const mailRowKey = (mid) => `mail:${cleanMid(mid)}`;

/** Row anchor key of a commit (blame) or release (symbol history) row of a history node. */
export const commitRowKey = (sha) => `commit:${String(sha || "")}`;
export const releaseRowKey = (vname) => `ver:${String(vname || "")}`;

/** A whole message body for display: LF line ends, tabs as 4 spaces, trailing blank lines dropped, capped. */
export function fullMailBody(body, max = MAIL_BODY_MAX) {
  let t = String(body || "").replace(/\r\n?/g, "\n").replace(/\t/g, "    ").replace(/\s+$/, "");
  if (t.length > max) {
    const note = `\n… (message cut at ${max} characters)`;
    t = `${t.slice(0, Math.max(0, max - note.length))}${note}`;
  }
  return t;
}

export function mailThreadModel(payload, data = {}, bodies = null) {
  const p = isObj(payload) ? payload : {};
  const raw = Array.isArray(p.messages) ? p.messages : (p.message_id || p.subject ? [p] : []);
  const msgs = raw.filter(isObj).map((m, i) => ({ m, i, mid: cleanMid(m.message_id || (Array.isArray(m.mids) ? m.mids[0] : "")) || `#${m.num ?? i}` }));
  const byMid = new Map();
  for (const x of msgs) {
    byMid.set(x.mid, x);
    for (const alt of Array.isArray(x.m.mids) ? x.m.mids : []) if (!byMid.has(cleanMid(alt))) byMid.set(cleanMid(alt), x);
  }
  const parentOf = new Map();
  for (const x of msgs) {
    const refs = refsOf(x.m);
    let parent = null;
    for (let k = refs.length - 1; k >= 0; k--) {
      const hit = byMid.get(refs[k]);
      if (hit && hit !== x) {
        parent = hit.mid;
        break;
      }
    }
    parentOf.set(x.mid, parent);
  }
  for (const x of msgs) {
    const seen = new Set([x.mid]);
    let cur = parentOf.get(x.mid);
    while (cur) {
      if (seen.has(cur)) {
        parentOf.set(x.mid, null);
        break;
      }
      seen.add(cur);
      cur = parentOf.get(cur);
    }
  }
  const time = (x) => Number(x.m.timestamp) || 0;
  const children = new Map();
  for (const x of msgs) {
    const key = parentOf.get(x.mid) || "";
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(x);
  }
  for (const list of children.values()) list.sort((a, b) => time(a) - time(b) || a.i - b.i);
  const rows = [];
  const focus = cleanMid(data.mid);
  const open = data.openMids || {};
  const fullMids = data.fullMids || {};
  const pinned = isObj(data.pinnedBodies) ? data.pinnedBodies : {};
  const bodyOf = (mid) => {
    if (typeof pinned[mid] === "string") return pinned[mid];
    const live = bodies?.get?.(mid);
    if (live !== undefined && live !== null) return String(live);
    if (data.single && mid === focus && data.fullBody) return String(data.fullBody);
    return mid === cleanMid(p.message_id) && typeof p.body === "string" ? p.body : null;
  };
  const walk = (key, depth, trail) => {
    const list = children.get(key) || [];
    list.forEach((x, idx) => {
      const last = idx === list.length - 1;
      const guides = depth ? [...trail.map((more) => (more ? "pipe" : "blank")), last ? "end" : "tee"] : [];
      const m = x.m;
      const isPinned = typeof pinned[x.mid] === "string";
      const full = (Boolean(data.single) && x.mid === focus) || Boolean(fullMids[x.mid]);
      const isOpen = Boolean(open[x.mid]) || full;
      const text = isOpen ? bodyOf(x.mid) : null;
      rows.push({
        mid: x.mid,
        parent: parentOf.get(x.mid) || null,
        depth,
        guides,
        subject: String(m.subject || "(no subject)"),
        author: String(m.author || m.from || ""),
        date: String(m.date_iso || m.date || ""),
        isPatch: Boolean(m.is_patch),
        isCover: Boolean(m.is_cover),
        patchVersion: m.patch_version || null,
        patchIdx: m.patch_idx ?? null,
        patchTotal: m.patch_total ?? null,
        focus: Boolean(focus) && x.mid === focus,
        open: isOpen,
        // the whole message (quotes, patch and signature kept) or its 10-line excerpt
        full,
        pinned: isPinned,
        loaded: text !== null,
        body: full && text !== null ? fullMailBody(text) : "",
        excerpt: isOpen && !full ? bodyExcerpt(text ?? "") : [],
      });
      walk(x.mid, Math.min(depth + 1, 24), depth ? [...trail, !last] : []);
    });
  };
  walk("", 0, []);
  return { tid: p.tid || null, total: p.total || rows.length, truncated: Boolean(p.truncated), single: Boolean(data.single), rows };
}

export function mailBadges(r) {
  const out = [];
  if (r.patchVersion && r.patchVersion > 1) out.push(`v${r.patchVersion}`);
  if (r.patchIdx !== null && r.patchTotal) out.push(`${r.patchIdx}/${r.patchTotal}`);
  if (r.isCover) out.push("cover");
  else if (r.isPatch) out.push("PATCH");
  return out;
}

/** CSS class of one line of a full message (quote depth, patch lines, signature). */
export function mailLineClass(line, state) {
  if (state.sig) return "nm-ml-sig";
  if (/^-- $/.test(line)) {
    state.sig = true;
    return "nm-ml-sig";
  }
  if (/^(diff --git |Index: |--- a\/|\+\+\+ b\/)/.test(line)) state.patch = true;
  const q = /^((?:>\s?)+)/.exec(line);
  if (q) return `nm-ml-q nm-ml-q${Math.min(3, (q[1].match(/>/g) || []).length)}`;
  if (!state.patch) return "";
  if (/^(diff --git |index |--- |\+\+\+ )/.test(line)) return "nm-ml-file";
  if (line.startsWith("@@")) return "nm-ml-hunk";
  if (line.startsWith("+")) return "nm-ml-add";
  if (line.startsWith("-")) return "nm-ml-del";
  return "";
}

/** Escaped HTML of a full message body, one span per line. */
export function mailBodyHtml(text) {
  const state = { patch: false, sig: false };
  return String(text || "").split("\n").map((l) => {
    const cls = mailLineClass(l, state);
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
  }).join("\n");
}

export function renderMailBody(model) {
  if (!model.rows.length) return '<div class="nm-empty">No messages</div>';
  let html = "";
  for (const r of model.rows) {
    const guides = r.guides.map((g) => `<span class="nm-mail-guide nm-g-${g}"></span>`).join("");
    const badges = mailBadges(r).map((b) => `<span class="nm-mail-badge">${esc(b)}</span>`).join("");
    const key = mailRowKey(r.mid);
    const ports = rowPorts(key);
    const depth = `style="--nm-mail-depth:${r.guides.length}"`;
    const selfMsg = model.single && r.focus;
    const acts = r.open && !selfMsg
      ? `<div class="nm-mail-acts" ${depth}><button type="button" class="nm-mail-act" data-act="mail-full" data-key="${esc(r.mid)}" aria-pressed="${r.full}" title="${r.full ? "Back to the start of the message" : "Show the whole message"}">${r.full ? "⤡ Excerpt" : "⤢ Show full message"}</button><button type="button" class="nm-mail-act${r.pinned ? " on" : ""}" data-act="mail-pin" data-key="${esc(r.mid)}" aria-pressed="${r.pinned}" title="${r.pinned ? "Stop saving this message with the NodeMap" : "Save the whole message with the NodeMap (kept across reloads and in exports)"}">📌 ${r.pinned ? "Pinned" : "Pin"}</button></div>`
      : "";
    let text = "";
    if (r.open && r.full) text = `<pre class="nm-mail-body${selfMsg ? " nm-mail-body-single" : ""}" ${depth}>${r.loaded ? mailBodyHtml(r.body) : "Loading…"}</pre>`;
    else if (r.open) text = `<pre class="nm-mail-excerpt" ${depth}>${r.excerpt.length ? esc(r.excerpt.join("\n")) : r.loaded ? "(empty message)" : "Loading…"}</pre>`;
    html += `<div class="nm-mail-row${r.focus ? " nm-mail-focus" : ""}" data-mid="${esc(r.mid)}" data-key="${esc(key)}">
      <div class="nm-mail-line nm-anchor-line">${ports}${guides}<button type="button" class="nm-mail-toggle" data-act="mail-open" data-key="${esc(r.mid)}" title="${r.open ? "Hide the message text" : "Show the start of the message text"}">${r.open ? "▾" : "▸"}</button>${badges}<span class="nm-mail-subj" title="${esc(r.subject)}">${esc(r.subject)}</span><button type="button" class="nm-mail-spawn" data-act="mail-spawn" data-key="${esc(r.mid)}" title="Add this message as a node connected to its row">⊕</button></div>
      <div class="nm-mail-meta" ${depth}>${esc(r.author)}${r.date ? ` · ${esc(r.date)}` : ""}</div>
      ${acts}${text}
    </div>`;
  }
  if (model.truncated || model.total > model.rows.length) html += `<div class="nm-empty">${model.rows.length} of ${model.total} messages</div>`;
  return `<div class="nm-mail nm-scroll">${html}</div>`;
}

export function mailThreadLayout(model, origin, { w = 300, h = 120, gapX = 80, gapY = 24 } = {}) {
  return model.rows.map((r, i) => ({
    mid: r.mid,
    parent: r.parent,
    x: Math.round(origin.x + r.depth * (w + gapX)),
    y: Math.round(origin.y + i * (h + gapY)),
  }));
}

// ------------------------------------------------------------------ kconfig

export const KCONFIG_RELATIONS = [
  { rel: "depends_on", label: "Depends on", edge: "depends on", dir: "out" },
  { rel: "selects", label: "Selects", edge: "selects", dir: "out" },
  { rel: "implies", label: "Implies", edge: "implies", dir: "out" },
  { rel: "depended_by", label: "Depended by", edge: "depends on", dir: "in" },
  { rel: "selected_by", label: "Selected by", edge: "selects", dir: "in" },
  { rel: "implied_by", label: "Implied by", edge: "implies", dir: "in" },
];
const IN_OF = { depends_on: "depended_by", selects: "selected_by", implies: "implied_by" };

export function kconfigModel(detail, graph, symbol) {
  const d = isObj(detail) ? detail : {};
  const name = bareSym(d.name || d.symbol_name || symbol);
  const sets = Object.fromEntries(KCONFIG_RELATIONS.map((g) => [g.rel, new Set()]));
  const add = (rel, n) => {
    const b = bareSym(n);
    if (b && b !== name && /^[A-Za-z0-9_]+$/.test(b)) sets[rel].add(b);
  };
  for (const rel of ["depends_on", "selects", "implies"]) for (const n of Array.isArray(d[rel]) ? d[rel] : []) add(rel, isObj(n) ? n.name || n.symbol : n);
  for (const e of Array.isArray(graph?.edges) ? graph.edges : []) {
    if (!isObj(e) || !IN_OF[e.type]) continue;
    if (bareSym(e.from) === name) add(e.type, e.to);
    else if (bareSym(e.to) === name) add(IN_OF[e.type], e.from);
  }
  const groups = KCONFIG_RELATIONS.map((g) => ({ ...g, names: [...sets[g.rel]].sort() })).filter((g) => g.names.length);
  return {
    name,
    type: String(d.type_name || d.type || ""),
    prompt: String(d.prompt || (Array.isArray(d.prompts) ? d.prompts[0]?.text || d.prompts[0] || "" : "") || ""),
    file: String(d.file_path || d.definition?.file || ""),
    line: d.line || d.line_s || d.definition?.line || null,
    help: String(d.help || ""),
    defined: d.defined_for_arch !== false,
    groups,
  };
}

export function kconfigValue(assignments, name) {
  if (!assignments) return "";
  const v = assignments[name] ?? assignments[`CONFIG_${name}`];
  if (v === undefined || v === null || v === "") return "";
  if (v === true) return "y";
  if (v === false) return "n";
  return String(v);
}

export function valueClass(v) {
  return v === "y" ? "y" : v === "m" ? "m" : v === "n" ? "n" : v ? "val" : "unset";
}

export function renderKconfigBody(model, value = null, onCanvas = null) {
  const badge = value === null ? '<span class="nm-kc-val nm-kc-none" title="No Kconfig engine followed">no engine</span>'
    : `<span class="nm-kc-val nm-kc-${valueClass(value)}" title="Value in the followed Kconfig engine">${esc(value || "unset")}</span>`;
  let html = `<div class="nm-kc-head"><span class="nm-kc-name">CONFIG_${esc(model.name)}</span>${model.type ? `<span class="nm-kc-type">${esc(model.type)}</span>` : ""}${badge}</div>`;
  if (model.prompt) html += `<div class="nm-kc-prompt">${esc(model.prompt)}</div>`;
  if (!model.defined) html += '<div class="nm-empty">Not defined for this ARCH</div>';
  for (const g of model.groups) {
    html += `<div class="nm-kc-group"><span class="nm-kc-rel">${esc(g.label)}</span><div class="nm-kc-chips">${g.names.map((n) =>
      `<button type="button" class="nm-kc-chip${onCanvas?.has(n) ? " on" : ""}" data-act="kc-rel" data-key="${esc(`${g.rel}:${n}`)}" title="${esc(`${onCanvas?.has(n) ? "Connect" : "Add"} CONFIG_${n} (${g.edge})`)}">${esc(n)}</button>`).join("")}</div></div>`;
  }
  if (!model.groups.length) html += '<div class="nm-empty">No relations</div>';
  return `<div class="nm-kc nm-scroll">${html}</div>`;
}

export function relationTargets(model, only = "") {
  const out = [];
  const seen = new Set();
  for (const g of model.groups) {
    for (const n of g.names) {
      if (only && only !== `${g.rel}:${n}`) continue;
      const key = `${g.dir}:${g.edge}:${n}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ name: n, rel: g.rel, edge: g.edge, dir: g.dir });
    }
  }
  return out;
}

// ------------------------------------------------------------------ maintainers

const splitList = (s) => String(s || "").split(/\s*[\n;]\s*|,\s+/).map((x) => x.trim()).filter(Boolean);

export function maintainerModel(res) {
  const r = isObj(res) ? res : {};
  const s = isObj(r.section) ? { ...r, ...r.section } : r;
  const people = (Array.isArray(s.members) ? s.members : Array.isArray(s.maintainers) ? s.maintainers : []).filter(isObj);
  const fmt = (m) => (m.email ? `${m.name || ""} <${m.email}>`.trim() : String(m.name || ""));
  const isReviewer = (m) => {
    const role = String(m.role || "").toUpperCase();
    return role ? role.startsWith("R") : m.role_type !== 1;
  };
  const pats = (Array.isArray(s.patterns) ? s.patterns : []).filter(isObj);
  const files = pats.filter((p) => String(p.pattern_type || "F").toUpperCase() === "F").map((p) => String(p.pattern));
  if (!files.length && Array.isArray(s.files)) for (const f of s.files.slice(0, 40)) files.push(String(f?.fname || f));
  return {
    secId: s.sec_id || null,
    title: String(s.name || s.title || ""),
    status: String(s.status || ""),
    maintainers: people.filter((m) => !isReviewer(m)).map(fmt).filter(Boolean),
    reviewers: people.filter(isReviewer).map(fmt).filter(Boolean),
    lists: splitList(s.mailing_list),
    trees: splitList(s.scm_tree),
    files,
    excludes: pats.filter((p) => String(p.pattern_type || "").toUpperCase() === "X").map((p) => String(p.pattern)),
    web: String(s.web_page || ""),
  };
}

export function maintainerLines(model) {
  const out = [];
  for (const [tag, list] of [["M", model.maintainers], ["R", model.reviewers], ["L", model.lists], ["T", model.trees], ["W", model.web ? [model.web] : []], ["F", model.files], ["X", model.excludes]]) {
    for (const v of list) out.push([tag, v]);
  }
  return out;
}

export function statusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s.startsWith("supported")) return "supported";
  if (s.startsWith("maintained")) return "maintained";
  if (s.startsWith("odd")) return "odd";
  if (s.startsWith("orphan") || s.startsWith("obsolete")) return "orphan";
  return "other";
}

export function renderMaintainerBody(model) {
  let html = `<div class="nm-mt-head"><span class="nm-mt-title">${esc(model.title || "(section)")}</span>${model.status ? `<span class="nm-mt-status nm-mt-${statusClass(model.status)}" title="S: ${esc(model.status)}">${esc(model.status)}</span>` : ""}</div>`;
  const lines = maintainerLines(model);
  if (!lines.length) html += '<div class="nm-empty">No entries</div>';
  for (const [tag, v] of lines) html += `<div class="nm-mt-row"><span class="nm-mt-tag nm-mt-tag-${tag}">${tag}:</span><span class="nm-mt-val">${esc(v)}</span></div>`;
  return `<div class="nm-mt nm-scroll">${html}</div>`;
}

// ------------------------------------------------------------------ CVE & fix-chain

/**
 * Model of a `cve` node from `getCveDetail()` or `getFileCves()`.
 */
export function cveModel(res, data = {}) {
  const r = isObj(res) ? res : {};
  if (Array.isArray(r.cves)) {
    return {
      mode: "file",
      filePath: String(r.file_path || data.filePath || ""),
      version: String(r.version || data.release || ""),
      total: Number(r.total) || r.cves.length,
      affectedCount: Number(r.affected_count) || 0,
      cves: r.cves.filter(isObj).slice(0, 60).map((c) => ({
        cveId: String(c.cve_id || c.id || ""),
        title: String(c.title || ""),
        state: String(c.state || "unknown"),
        severity: String(c.severity || ""),
        score: c.score ?? null,
      })),
    };
  }
  const cvss = isObj(r.cvss) ? r.cvss : {};
  const rs = isObj(r.release_state) ? r.release_state : {};
  const ranges = (Array.isArray(r.ranges) ? r.ranges : []).filter(isObj).slice(0, 20).map((rg) => ({
    introduced: String(rg.introduced || ""),
    introducedSubject: String(rg.introduced_subject || ""),
    fixed: String(rg.fixed || ""),
    fixedSubject: String(rg.fixed_subject || ""),
    inferred: (Array.isArray(rg.inferred_introduced) ? rg.inferred_introduced : []).filter(isObj).map((inf) => ({
      sha: String(inf.sha || ""),
      subject: String(inf.subject || ""),
    })),
  }));
  return {
    mode: "cve",
    cveId: String(r.id || r.cve_id || data.cveId || ""),
    title: String(r.title || ""),
    description: String(r.description || ""),
    severity: String(cvss.severity || r.severity || ""),
    score: cvss.score ?? r.score ?? null,
    kev: String(r.kev || ""),
    state: String(rs.state || ""),
    release: String(rs.release || data.release || ""),
    cwes: (Array.isArray(r.cwes) ? r.cwes : []).map(String),
    files: (Array.isArray(r.files) ? r.files : []).map(String).slice(0, 30),
    ranges,
    versionSummary: String(r.version_summary?.text || ""),
  };
}

export function renderCveBody(model) {
  if (!model) return '<div class="nm-empty">No CVE data</div>';
  if (model.mode === "file") {
    const head = `<div class="nm-cve-head"><span class="nm-cve-id">${esc(model.filePath || "File CVEs")}</span><span class="nm-cve-badge">${model.affectedCount} affected / ${model.total}</span></div>`;
    const list = model.cves.length
      ? model.cves.map((c) => `<div class="nm-cve-row"><span class="nm-cve-state nm-cve-${esc(c.state)}">${esc(c.state)}</span><button type="button" class="nm-cve-link" data-act="cve-spawn-cve" data-key="${esc(c.cveId)}" title="Spawn CVE node">${esc(c.cveId)}</button>${c.score != null ? `<span class="nm-cve-score">${esc(c.score)}</span>` : ""}<span class="nm-cve-subj" title="${esc(c.title)}">${esc(c.title)}</span></div>`).join("")
      : '<div class="nm-empty">No CVEs recorded for this file</div>';
    return `<div class="nm-cve nm-scroll">${head}${list}</div>`;
  }
  const sev = model.score != null ? `<span class="nm-cve-score ${model.score >= 7 ? "high" : model.score >= 4 ? "med" : "low"}">${esc(model.score)}${model.severity ? ` ${esc(model.severity)}` : ""}</span>` : "";
  const stateBadge = model.state && model.state !== "unknown" ? `<span class="nm-cve-state nm-cve-${esc(model.state)}">${esc(model.state)}${model.release ? ` (${esc(model.release)})` : ""}</span>` : "";
  const kevBadge = model.kev ? `<span class="nm-cve-state nm-cve-affected">KEV</span>` : "";
  let html = `<div class="nm-cve-head"><span class="nm-cve-id">${esc(model.cveId)}</span>${sev}${stateBadge}${kevBadge}</div>`;
  if (model.title) html += `<div class="nm-cve-title">${esc(model.title)}</div>`;
  if (model.versionSummary) html += `<div class="nm-cve-summary">${esc(model.versionSummary)}</div>`;
  if (model.ranges.length) {
    html += '<div class="nm-cve-sec"><span class="nm-cve-label">Fix Chain</span>';
    for (const rg of model.ranges) {
      const intro = rg.introduced || rg.inferred[0]?.sha || "";
      const introSubj = rg.introducedSubject || rg.inferred[0]?.subject || "";
      if (intro) {
        html += `<div class="nm-cve-commit"><span class="nm-cve-tag intro">intro</span><code class="nm-cve-sha">${esc(intro.slice(0, 10))}</code><span class="nm-cve-subj" title="${esc(introSubj)}">${esc(introSubj)}</span><button type="button" class="nm-cve-btn" data-act="cve-spawn-diff" data-key="${esc(intro)}:intro" title="Spawn commit diff node (introduced)">⊕ diff</button></div>`;
      }
      if (rg.fixed) {
        html += `<div class="nm-cve-commit"><span class="nm-cve-tag fix">fix</span><code class="nm-cve-sha">${esc(rg.fixed.slice(0, 10))}</code><span class="nm-cve-subj" title="${esc(rg.fixedSubject)}">${esc(rg.fixedSubject)}</span><button type="button" class="nm-cve-btn" data-act="cve-spawn-diff" data-key="${esc(rg.fixed)}:fixes" title="Spawn commit diff node (fixes)">⊕ diff</button></div>`;
      }
    }
    html += "</div>";
  }
  if (model.files.length) {
    html += '<div class="nm-cve-sec"><span class="nm-cve-label">Affected Files</span>';
    for (const f of model.files) {
      html += `<div class="nm-cve-file"><span class="nm-cve-fpath" title="${esc(f)}">${esc(f)}</span><button type="button" class="nm-cve-btn" data-act="cve-spawn-source" data-key="${esc(f)}" title="Spawn source node (affects)">⊕ src</button></div>`;
    }
    html += "</div>";
  }
  if (model.description) html += `<div class="nm-cve-desc">${esc(model.description.slice(0, 600))}${model.description.length > 600 ? "…" : ""}</div>`;
  return `<div class="nm-cve nm-scroll">${html}</div>`;
}

// ------------------------------------------------------------------ Kernel API / Boundary

export function kapiModel(res, data = {}) {
  const r = isObj(res) ? res : {};
  const catalog = String(data.catalog || "summary");
  const filter = String(data.symbol || data.query || "").trim().toLowerCase();
  const match = (s) => !filter || String(s || "").toLowerCase().includes(filter);
  const items = [];

  if (catalog === "summary" || isObj(r.counts)) {
    const counts = r.counts || {};
    for (const [k, v] of Object.entries(counts)) {
      items.push({ kind: "catalog", name: k, subtitle: `${v} entries`, cat: k });
    }
  }
  if (Array.isArray(r.syscalls) || Array.isArray(r.unnumbered)) {
    for (const s of [...(r.syscalls || []), ...(r.unnumbered || [])]) {
      if (!isObj(s)) continue;
      const entry = String(s.entry || (s.name ? `sys_${s.name}` : ""));
      if (!match(`${s.name} ${entry} ${s.definition?.path || ""}`)) continue;
      items.push({
        kind: "syscall",
        name: String(s.name || entry),
        subtitle: `${s.nr != null ? `#${s.nr}` : "unnumbered"}${s.abi ? ` ${s.abi}` : ""} · ${s.status || "defined"}`,
        path: String(s.definition?.path || ""),
        line: s.definition?.line_s || 1,
        symbol: entry,
        manName: String(s.name || ""),
        manSec: "2",
      });
    }
  }
  if (Array.isArray(r.exports)) {
    for (const e of r.exports) {
      if (!isObj(e) || !match(`${e.symbol} ${e.namespace || ""} ${e.path || ""}`)) continue;
      items.push({
        kind: "export",
        name: String(e.symbol || ""),
        subtitle: `${e.variant || "EXPORT_SYMBOL"}${e.namespace ? ` · ${e.namespace}` : ""}`,
        path: String(e.path || ""),
        line: e.line || 1,
        symbol: String(e.symbol || ""),
      });
    }
  }
  if (Array.isArray(r.sites)) {
    for (const e of r.sites) {
      if (!isObj(e)) continue;
      items.push({
        kind: "export",
        name: String(e.symbol || r.symbol || ""),
        subtitle: `${e.variant || "EXPORT_SYMBOL"}${e.owner?.modname ? ` · ${e.owner.modname}` : ""}`,
        path: String(e.path || ""),
        line: e.line || 1,
        symbol: String(e.symbol || r.symbol || ""),
      });
    }
  }
  if (Array.isArray(r.params)) {
    for (const p of r.params) {
      if (!isObj(p) || !match(`${p.full_name || p.name} ${p.handler || ""} ${p.path || ""}`)) continue;
      items.push({
        kind: "param",
        name: String(p.full_name || p.name || ""),
        subtitle: `${p.kind || "param"}${p.type ? ` · ${p.type}` : ""}`,
        path: String(p.path || ""),
        line: p.line || 1,
        symbol: String(p.handler || ""),
      });
    }
  }
  if (Array.isArray(r.tracepoints)) {
    for (const t of r.tracepoints) {
      if (!isObj(t) || !match(`${t.system} ${t.name} ${t.path || ""}`)) continue;
      items.push({
        kind: "tracepoint",
        name: t.system ? `${t.system}:${t.name}` : String(t.name || ""),
        subtitle: String(t.macro || t.kind || "event"),
        path: String(t.path || ""),
        line: t.line || 1,
        symbol: String(t.name || ""),
      });
    }
  }
  if (Array.isArray(r.modules)) {
    for (const m of r.modules) {
      if (!isObj(m) || !match(`${m.modname} ${m.license || ""} ${m.description || ""}`)) continue;
      items.push({
        kind: "module",
        name: `${m.modname || "mod"}.ko`,
        subtitle: String(m.license || m.description || ""),
        path: String(m.members?.[0] || ""),
        line: 1,
        symbol: String(m.init || "").replace(/^.*\(|\)$/g, ""),
      });
    }
  }
  if (Array.isArray(r.entries)) {
    for (const d of r.entries) {
      if (!isObj(d) || !match(`${d.bus} ${d.id} ${d.table} ${d.path}`)) continue;
      items.push({
        kind: "device",
        name: String(d.table || d.id || ""),
        subtitle: `${d.bus || ""} · ${d.id || ""}`,
        path: String(d.path || ""),
        line: d.line || 1,
        symbol: "",
      });
    }
  }

  return {
    catalog,
    version: String(r.version || ""),
    arch: String(r.arch || data.arch || ""),
    total: items.length,
    items: items.slice(0, 100),
  };
}

export function renderKapiBody(model) {
  if (!model) return '<div class="nm-empty">No Kernel API data</div>';
  const head = `<div class="nm-kapi-head"><span class="nm-kapi-cat">${esc(model.catalog)}</span>${model.arch ? `<span class="nm-kapi-arch">${esc(model.arch)}</span>` : ""}<span class="nm-kapi-cnt">${model.total} entries</span></div>`;
  if (!model.items.length) return `<div class="nm-kapi nm-scroll">${head}<div class="nm-empty">No matching Kernel API entries</div></div>`;
  const rows = model.items.map((it) => {
    if (it.kind === "catalog") {
      return `<div class="nm-kapi-row"><button type="button" class="nm-kapi-chip" data-act="kapi-catalog" data-key="${esc(it.cat)}">${esc(it.name)}</button><span class="nm-kapi-sub">${esc(it.subtitle)}</span></div>`;
    }
    const symBtn = it.symbol ? `<button type="button" class="nm-kapi-btn" data-act="kapi-spawn-symbol" data-key="${esc(it.symbol)}" title="Spawn symbol node">⊕ fn</button>` : "";
    const manBtn = it.manName ? `<button type="button" class="nm-kapi-btn" data-act="kapi-spawn-man" data-key="${esc(`${it.manName}:${it.manSec || "2"}`)}" title="Spawn man page">⊕ man</button>` : "";
    const srcBtn = it.path ? `<button type="button" class="nm-kapi-btn" data-act="kapi-spawn-source" data-key="${esc(`${it.path}:${it.line || 1}`)}" title="Spawn source slice">⊕ src</button>` : "";
    return `<div class="nm-kapi-row"><span class="nm-kapi-tag">${esc(it.kind)}</span><span class="nm-kapi-name" title="${esc(it.name)}">${esc(it.name)}</span><span class="nm-kapi-sub" title="${esc(it.subtitle)}">${esc(it.subtitle)}</span><span class="nm-kapi-acts">${symBtn}${manBtn}${srcBtn}</span></div>`;
  }).join("");
  return `<div class="nm-kapi nm-scroll">${head}${rows}</div>`;
}

// ------------------------------------------------------------------ Kernel Docs (.rst / kernel-doc)

/** kernel-doc entries and document characters a doc node renders. */
export const DOC_ENTRY_MAX = 120;
export const DOC_SOURCE_MAX = 400_000;
const DOC_HTML_CACHE = new WeakMap();

function stripRstTags(html) {
  return String(html || "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function parseRstSections(content, toc = []) {
  const lines = String(content || "").replace(/\r\n/g, "\n").split("\n");
  const sections = [];
  let cur = { heading: "Document", level: 1, lines: [] };
  const isAdorn = (l) => /^[=\-~^`*+#.:'"_]{3,}$/.test(String(l || "").trim());
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    const next = lines[i + 1];
    if (l.trim() && !/^\s/.test(l) && !/^\.\./.test(l) && next && isAdorn(next) && next.trim().length >= Math.max(3, l.trim().length - 2)) {
      if (cur.lines.length || sections.length) sections.push(cur);
      cur = { heading: l.trim(), level: 2, lines: [] };
      i += 1;
      continue;
    }
    const md = /^(#{1,4})\s+(.*)$/.exec(l);
    if (md) {
      if (cur.lines.length || sections.length) sections.push(cur);
      cur = { heading: md[2].trim(), level: md[1].length, lines: [] };
      continue;
    }
    cur.lines.push(l);
  }
  if (cur.lines.length || !sections.length) sections.push(cur);
  return sections.slice(0, 60).map((s) => ({
    heading: s.heading,
    level: s.level,
    text: s.lines.filter((x) => x.trim() && !/^\s*\.\.\s/.test(x)).slice(0, 12).join("\n"),
    tocCount: toc.length,
  }));
}

export function docModel(res, data = {}) {
  const r = isObj(res) ? res : {};
  const path = String(r.path || data.path || "");
  if (Array.isArray(r.entries)) {
    return {
      mode: "kernel-doc",
      path,
      title: path ? path.split("/").pop() : "kernel-doc",
      total: Number(r.total) || r.entries.length,
      entries: r.entries.filter(isObj).slice(0, DOC_ENTRY_MAX).map((e) => ({
        kind: String(e.kind || "doc"),
        name: String(e.name || ""),
        brief: String(e.brief || ""),
        signature: String(e.signature || ""),
        line: Number(e.line) || null,
        params: (Array.isArray(e.params) ? e.params : []).filter(isObj).map((p) => ({ name: String(p.name || ""), desc: String(p.desc || "") })),
        sections: (Array.isArray(e.sections) ? e.sections : []).filter(isObj).map((s) => ({ title: String(s.title || ""), body: String(s.body || "") })),
      })),
    };
  }
  const content = String(r.content || "").slice(0, DOC_SOURCE_MAX);
  const title = String(r.title || (path ? path.split("/").pop() : "Kernel Docs"));
  // `sections` is the plain outline the vector SVG export draws when the live page is not mounted
  const sections = content ? parseRstSections(content, r.toc || []) : (r.html ? [{ heading: title, level: 1, text: stripRstTags(r.html).slice(0, 1500) }] : []);
  return {
    mode: "rst",
    path,
    title,
    total: sections.length,
    sections,
    toc: Array.isArray(r.toc) ? r.toc.slice(0, 40) : [],
    html: renderedDocHtml(r, path, content),
  };
}

const DOC_DROP_BLOCK = /<(script|style|iframe|frame|object|embed|form|template|noscript|svg|math)\b[\s\S]*?<\/\1\s*>/gi;
const DOC_DROP_TAG = /<\/?(script|style|iframe|frame|frameset|object|embed|link|meta|base|form|input|button|textarea|select|template|noscript|svg|math)\b[^>]*>/gi;
// inline handlers, inline styles (no overlaying the canvas) and the NodeMap's own action attributes
const DOC_DROP_ATTR = /([\s/"'])(?:on[a-z]+|style|formaction|srcdoc|data-act|data-insp|data-node-id)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi;
const DOC_URL_ATTR = /([\s/"'])(href|src|xlink:href|action|poster|background)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi;

/** An attribute value as a browser reads its scheme: entities decoded, whitespace and controls dropped. */
function urlScheme(value) {
  const named = { tab: "\t", newline: "\n", colon: ":", lpar: "(", rpar: ")", sol: "/", amp: "&" };
  return String(value)
    .replace(/&#x([0-9a-f]+);?/gi, (_, h) => String.fromCodePoint(parseInt(h, 16) || 32))
    .replace(/&#(\d+);?/g, (_, d) => String.fromCodePoint(Number(d) || 32))
    .replace(/&([a-z]+);?/gi, (m, n) => named[n.toLowerCase()] ?? m)
    .replace(/[\u0000- \u007f-\u009f]+/g, "")
    .toLowerCase();
}

const unsafeUrl = (v) => {
  const u = urlScheme(v);
  return /^(javascript|vbscript|livescript):/.test(u) || (/^data:/.test(u) && !/^data:image\/(png|gif|jpe?g|webp)[;,]/.test(u));
};

/**
 * Defense in depth on top of the renderers' own escaping and `html_renderer.js` sanitizing: a doc node
 * shares the page with the canvas, so no script, frame, form, SVG / MathML island, style sheet, inline
 * handler or style, NodeMap action attribute, or script / non-image `data:` URL (entities decoded first).
 * Every pass repeats until nothing changes, so a removal never stitches a new attack together.
 */
export function scrubDocHtml(html) {
  let out = String(html || "");
  for (let guard = 0; guard < 16; guard++) {
    const next = out
      .replace(DOC_DROP_BLOCK, "")
      .replace(DOC_DROP_TAG, "")
      .replace(DOC_DROP_ATTR, "$1")
      .replace(DOC_URL_ATTR, (m, lead, name, dq, sq, bare) => (unsafeUrl(dq ?? sq ?? bare ?? "") ? `${lead}${name}="#"` : m));
    if (next === out) break;
    out = next;
  }
  return out;
}

/** Rendered page HTML of a document payload (`views/docs/` renderers), memoized per payload object. */
function renderedDocHtml(r, path, content) {
  const hit = DOC_HTML_CACHE.get(r);
  if (hit !== undefined && hit.path === path) return hit.html;
  let html = "";
  try {
    if (content) html = renderDocument(content, path || "doc.rst").html;
    else if (r.html) html = renderHtmlFragment(String(r.html), path);
  } catch {
    html = content ? `<pre class="plain-text">${esc(content)}</pre>` : "";
  }
  html = scrubDocHtml(html);
  DOC_HTML_CACHE.set(r, { path, html });
  return html;
}

/**
 * Body of a doc node: the document rendered by the Kernel Docs renderers (`.rst`, Markdown, HTML,
 * DocBook templates) or kernel-doc definition cards, inside a `.kdoc-body` so `docs.css` styles it.
 * Links keep their Kernel Docs attributes (`data-doc`, `data-src`, `data-symbol`, `data-kconfig`) and the
 * NodeMap turns them into connected nodes (`nodemap_gestures.js` `onDocLink`).
 */
export function renderDocBody(model) {
  if (!model) return '<div class="nm-empty">No documentation loaded</div>';
  if (model.mode === "kernel-doc") {
    if (!model.entries.length) return '<div class="nm-empty">No kernel-doc comments found</div>';
    const html = model.entries.map((e) => renderKernelDocEntry(e, { path: model.path, srcPath: model.path })).join("");
    const more = model.total > model.entries.length ? `<p class="nm-empty">${model.entries.length} of ${model.total} entries</p>` : "";
    return `<div class="nm-doc nm-doc-rendered nm-scroll"><div class="kdoc-body nm-doc-page">${html}${more}</div></div>`;
  }
  if (!model.html) return '<div class="nm-doc nm-scroll"><div class="nm-empty">Empty document</div></div>';
  return `<div class="nm-doc nm-doc-rendered nm-scroll"><div class="kdoc-body nm-doc-page">${model.html}</div></div>`;
}

// ------------------------------------------------------------------ Symbol History / File Blame

export function historyModel(res, data = {}) {
  const r = isObj(res) ? res : {};
  if (Array.isArray(r.lines) || data.mode === "blame") {
    const bySha = new Map();
    for (const l of Array.isArray(r.lines) ? r.lines : []) {
      if (!isObj(l) || !l.commit_hash) continue;
      const sha = String(l.commit_hash);
      let entry = bySha.get(sha);
      if (!entry) {
        entry = {
          commitHash: sha,
          author: String(l.author_name || l.author_email || "Unknown"),
          date: l.date ? new Date(l.date * 1000).toISOString().slice(0, 10) : "",
          subject: String(l.subject || ""),
          lineStart: l.line || 1,
          lineEnd: l.line || 1,
          count: 0,
        };
        bySha.set(sha, entry);
      }
      entry.lineStart = Math.min(entry.lineStart, l.line || entry.lineStart);
      entry.lineEnd = Math.max(entry.lineEnd, l.line || entry.lineEnd);
      entry.count += 1;
    }
    return {
      mode: "blame",
      path: String(r.path || data.path || ""),
      version: String(r.version || ""),
      commits: [...bySha.values()].slice(0, 80),
      totalLines: Array.isArray(r.lines) ? r.lines.length : 0,
    };
  }
  const timeline = (Array.isArray(r.timeline) ? r.timeline : []).filter(isObj);
  const changes = timeline.filter((t) => t.status !== "unchanged" && t.status !== "absent");
  const rows = (changes.length ? changes : timeline).slice(0, 80).map((t) => ({
    vname: String(t.vname || ""),
    prevVname: String(t.prev_vname || ""),
    status: String(t.status || ""),
    path: String(t.path || r.path || data.path || ""),
    lineS: t.line_s != null ? (Number(t.line_s) || null) : null,
    lineE: t.line_e != null ? (Number(t.line_e) || null) : null,
    added: Number(t.lines_added) || 0,
    removed: Number(t.lines_removed) || 0,
  }));
  return {
    mode: "symbol",
    name: String(r.ast_name || data.name || ""),
    path: String(r.path || data.path || ""),
    branch: String(r.branch || ""),
    totalVersions: Number(r.total_versions) || timeline.length,
    changeCount: Number(r.change_count) || changes.length,
    rows,
  };
}

/** Left / right row ports of an anchored row (`fromField` = `key`). */
const rowPorts = (key) => `<span class="nm-port nm-port-field nm-port-left" data-field="${esc(key)}" data-side="left"></span><span class="nm-port nm-port-field nm-port-right" data-field="${esc(key)}" data-side="right"></span>`;

export function renderHistoryBody(model) {
  if (!model) return '<div class="nm-empty">No history loaded</div>';
  if (model.mode === "blame") {
    const head = `<div class="nm-hist-head"><span>${esc(model.path || "Blame")}</span><span class="nm-hist-cnt">${model.commits.length} commits · ${model.totalLines} lines</span></div>`;
    if (!model.commits.length) return `<div class="nm-hist nm-scroll">${head}<div class="nm-empty">No blame lines</div></div>`;
    const rows = model.commits.map((c) => `<div class="nm-hist-row" data-key="${esc(commitRowKey(c.commitHash))}">${rowPorts(commitRowKey(c.commitHash))}
      <code class="nm-hist-sha">${esc(c.commitHash.slice(0, 8))}</code>
      <span class="nm-hist-author">${esc(c.author)}</span>
      <span class="nm-hist-subj" title="${esc(c.subject)}">${esc(c.subject)}</span>
      <span class="nm-hist-lines">L${c.lineStart}${c.lineEnd !== c.lineStart ? `–${c.lineEnd}` : ""} (${c.count})</span>
      <button type="button" class="nm-hist-btn" data-act="hist-spawn-commit" data-key="${esc(c.commitHash)}" title="Spawn commit diff node">⊕ diff</button>
    </div>`).join("");
    return `<div class="nm-hist nm-scroll">${head}${rows}</div>`;
  }
  const head = `<div class="nm-hist-head"><span>${esc(model.name || model.path || "History")}</span><span class="nm-hist-cnt">${model.changeCount} changes / ${model.totalVersions} releases</span></div>`;
  if (!model.rows.length) return `<div class="nm-hist nm-scroll">${head}<div class="nm-empty">No history stops found</div></div>`;
  const rows = model.rows.map((r) => `<div class="nm-hist-row" data-key="${esc(releaseRowKey(r.vname))}">${rowPorts(releaseRowKey(r.vname))}
    <span class="nm-hist-ver">${esc(r.vname)}</span>
    <span class="nm-hist-st nm-hist-${esc(r.status)}">${esc(r.status)}</span>
    <span class="nm-hist-delta">${r.added || r.removed ? `+${esc(r.added)} −${esc(r.removed)}` : (r.lineS ? `L${esc(r.lineS)}` : "")}</span>
    ${r.prevVname && r.path ? `<button type="button" class="nm-hist-btn" data-act="hist-spawn-diff" data-key="${esc(`${r.prevVname}..${r.vname}:${r.path}`)}" title="Spawn diff ${esc(r.prevVname)}..${esc(r.vname)}">⊕ diff</button>` : ""}
  </div>`).join("");
  return `<div class="nm-hist nm-scroll">${head}${rows}</div>`;
}
