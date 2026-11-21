/**
 * domain_nodes.js - Content of the NodeMap's domain nodes (`webapp/API_FRONTEND.md` §5.3.2): diffs,
 * mailing-list threads, Kconfig symbols with their relations and MAINTAINERS sections.
 *
 * DOM-free (unit tested). Each node type has a pure model built from the API payloads (`diffPayload()`,
 * `mailThreadModel()`, `kconfigModel()`, `maintainerModel()`), the live body HTML drawn from it
 * (`render*Body()`), and the layout helpers that spawn connected nodes (`mailThreadLayout()`,
 * `relationTargets()`). `nodemap_export.js` draws the same models as SVG vectors.
 */
import { esc } from "./struct_view.js";

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const cleanMid = (m) => String(m || "").trim().replace(/^<|>$/g, "");
const bareSym = (s) => String(s || "").trim().replace(/^CONFIG_/, "");

// ------------------------------------------------------------------ diff

/**
 * Files of a unified diff text (`git diff` output): `[{path, oldPath, status, additions, deletions,
 * binary, hunks: [{header, lines: [[kind, oldNo, newNo, text]]}]}]`, `kind` one of `+`, `-`, ` `.
 * `fallbackPath` names the file of a diff without `diff --git` / `+++` headers.
 */
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

/** A commit-diff file (`git_service.parse_unified_diff`) in the node's file shape. */
function commitFile(f) {
  const hunks = (Array.isArray(f?.hunks) ? f.hunks : []).map((h) => {
    const head = Number.isInteger(h.old_start)
      ? `@@ -${h.old_start},${h.old_count ?? 1} +${h.new_start},${h.new_count ?? 1} @@${h.heading ? ` ${h.heading}` : ""}`
      : `@@ ${h.heading || ""}`;
    return { header: head, heading: h.heading || "", lines: (Array.isArray(h.lines) ? h.lines : []).filter(Array.isArray).map((l) => [
      ["+", "-", " "].includes(l[0]) ? l[0] : " ",
      Number.isInteger(l[1]) ? l[1] : null,
      Number.isInteger(l[2]) ? l[2] : null,
      String(l[3] ?? ""),
    ]) };
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

/**
 * Loaded content of a diff node: `data` (the node payload) with the response of `getFileDiff()`
 * (`{diff, additions, deletions, is_binary, truncated}`) or `getCommitDiff()` (`{files: [...]}`), as
 * `{mode, versionA, versionB, commitHash, path, files, truncated}`; a commit diff keeps only `data.path`
 * when it names one of its files.
 */
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

/**
 * Display rows of a diff payload: `{kind, old, new, text}` with `kind` `file` (a file header, shown when
 * the payload has several files or is a commit), `hunk` (`@@` header), `+`, `-`, ` ` or `note`.
 */
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

/** Live body of a diff node (old/new line numbers, sign, text). */
export function renderDiffBody(payload) {
  const rows = diffRows(payload);
  if (!rows.length) return '<div class="nm-empty">No changes</div>';
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

/** Reference ids of a message (list or whitespace-separated `<id>` text), oldest first. */
function refsOf(m) {
  const raw = m?.references;
  const list = Array.isArray(raw) ? raw : String(raw || "").split(/[\s,]+/);
  return list.map(cleanMid).filter(Boolean);
}

/** First lines of a message body without quoted text, for the expandable excerpt. */
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

/**
 * Reply tree of a thread payload (`getMailThread()`: `{tid, total, messages}`, or one `getMailMessage()`
 * response): rows in reply order (depth-first, each level by date) as `{mid, parent, depth, guides,
 * subject, author, date, isPatch, isCover, patchVersion, patchIdx, patchTotal, focus, open, excerpt}`.
 * A message's parent is the last of its references inside the thread; a `data.single` node (one message
 * on its own) shows its message open. `guides` per depth level (1..depth)
 * says how the connector is drawn there: `pipe` (an ancestor's later sibling continues), `blank`, `tee`
 * (this row, siblings follow) or `end` (this row, the last child). `data.openMids` marks expanded
 * messages, `bodies` (mid -> body text) gives their excerpts.
 */
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
  // A reference cycle (bad headers) must not hide messages: a message whose chain loops is a root
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
  const walk = (key, depth, trail) => {
    const list = children.get(key) || [];
    list.forEach((x, idx) => {
      const last = idx === list.length - 1;
      const guides = depth ? [...trail.map((more) => (more ? "pipe" : "blank")), last ? "end" : "tee"] : [];
      const m = x.m;
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
        open: Boolean(open[x.mid]) || (Boolean(data.single) && x.mid === focus),
        excerpt: open[x.mid] || (data.single && x.mid === focus) ? bodyExcerpt(bodies?.get?.(x.mid) ?? (x.mid === cleanMid(p.message_id) ? p.body : "")) : [],
      });
      // Deep threads stay readable: deeper replies hang at the last drawn level
      walk(x.mid, Math.min(depth + 1, 24), depth ? [...trail, !last] : []);
    });
  };
  walk("", 0, []);
  return { tid: p.tid || null, total: p.total || rows.length, truncated: Boolean(p.truncated), rows };
}

/** Badges of a message row (`[v3]`, `3/7`, `PATCH`, `cover`). */
export function mailBadges(r) {
  const out = [];
  if (r.patchVersion && r.patchVersion > 1) out.push(`v${r.patchVersion}`);
  if (r.patchIdx !== null && r.patchTotal) out.push(`${r.patchIdx}/${r.patchTotal}`);
  if (r.isCover) out.push("cover");
  else if (r.isPatch) out.push("PATCH");
  return out;
}

/** Live body of a mail node: the reply tree with connector guides, badges and expandable excerpts. */
export function renderMailBody(model) {
  if (!model.rows.length) return '<div class="nm-empty">No messages</div>';
  let html = "";
  for (const r of model.rows) {
    const guides = r.guides.map((g) => `<span class="nm-mail-guide nm-g-${g}"></span>`).join("");
    const badges = mailBadges(r).map((b) => `<span class="nm-mail-badge">${esc(b)}</span>`).join("");
    html += `<div class="nm-mail-row${r.focus ? " nm-mail-focus" : ""}" data-mid="${esc(r.mid)}">
      <div class="nm-mail-line">${guides}<button type="button" class="nm-mail-toggle" data-act="mail-open" data-key="${esc(r.mid)}" title="${r.open ? "Hide the message text" : "Show the start of the message text"}">${r.open ? "▾" : "▸"}</button>${badges}<span class="nm-mail-subj" title="${esc(r.subject)}">${esc(r.subject)}</span><button type="button" class="nm-mail-spawn" data-act="mail-spawn" data-key="${esc(r.mid)}" title="Add this message as a node connected to its parent">⊕</button></div>
      <div class="nm-mail-meta" style="--nm-mail-depth:${r.guides.length}">${esc(r.author)}${r.date ? ` · ${esc(r.date)}` : ""}</div>
      ${r.open ? `<pre class="nm-mail-excerpt" style="--nm-mail-depth:${r.guides.length}">${r.excerpt.length ? esc(r.excerpt.join("\n")) : "Loading…"}</pre>` : ""}
    </div>`;
  }
  if (model.truncated || model.total > model.rows.length) html += `<div class="nm-empty">${model.rows.length} of ${model.total} messages</div>`;
  return `<div class="nm-mail nm-scroll">${html}</div>`;
}

/**
 * Canvas places of the message nodes "Expand Thread to Nodes" creates, as a tree right of `origin`
 * `{x, y}`: one column per reply depth, one row per message in reply order. `[{mid, parent, x, y}]`.
 */
export function mailThreadLayout(model, origin, { w = 300, h = 120, gapX = 80, gapY = 24 } = {}) {
  return model.rows.map((r, i) => ({
    mid: r.mid,
    parent: r.parent,
    x: Math.round(origin.x + r.depth * (w + gapX)),
    y: Math.round(origin.y + i * (h + gapY)),
  }));
}

// ------------------------------------------------------------------ kconfig

/** Relation groups of a Kconfig node: edge label, direction (out = this symbol is the edge's source). */
export const KCONFIG_RELATIONS = [
  { rel: "depends_on", label: "Depends on", edge: "depends on", dir: "out" },
  { rel: "selects", label: "Selects", edge: "selects", dir: "out" },
  { rel: "implies", label: "Implies", edge: "implies", dir: "out" },
  { rel: "depended_by", label: "Depended by", edge: "depends on", dir: "in" },
  { rel: "selected_by", label: "Selected by", edge: "selects", dir: "in" },
  { rel: "implied_by", label: "Implied by", edge: "implies", dir: "in" },
];
const IN_OF = { depends_on: "depended_by", selects: "selected_by", implies: "implied_by" };

/**
 * Model of a Kconfig node from `getKconfigSymbol()` (`detail`) and `getKconfigGraph()` (`graph`,
 * `{edges: [{from, to, type}]}`): `{name, type, prompt, file, line, help, defined, groups: [{rel, label,
 * edge, dir, names}]}`; empty groups are left out. Names are bare (no `CONFIG_`).
 */
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

/**
 * Value of a symbol in a Kconfig engine's assignments (`y`, `m`, `n`, a string / number value, or `""`
 * when unset). Assignments carry bare and `CONFIG_` keys.
 */
export function kconfigValue(assignments, name) {
  if (!assignments) return "";
  const v = assignments[name] ?? assignments[`CONFIG_${name}`];
  if (v === undefined || v === null || v === "") return "";
  if (v === true) return "y";
  if (v === false) return "n";
  return String(v);
}

/** CSS modifier of a value badge. */
export function valueClass(v) {
  return v === "y" ? "y" : v === "m" ? "m" : v === "n" ? "n" : v ? "val" : "unset";
}

/**
 * Live body of a Kconfig node. `value` is the followed engine's value (`null`: no engine), `onCanvas`
 * (Set of bare names) marks relations already on the canvas.
 */
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

/**
 * Targets "Expand Relations" (or one chip, `only` = `rel:NAME`) links a Kconfig node to:
 * `[{name, rel, edge, dir}]` in group order, each name once.
 */
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

/**
 * Model of a MAINTAINERS section (`getMaintainerSection()` response or its `section`): `{secId, title,
 * status, maintainers, reviewers, lists, trees, files, excludes, web}` (people as `Name <email>`).
 */
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

/** Lines of a maintainer node: `[tag, text]` with the MAINTAINERS letters (`M`, `R`, `L`, `T`, `F`, `X`, `W`). */
export function maintainerLines(model) {
  const out = [];
  for (const [tag, list] of [["M", model.maintainers], ["R", model.reviewers], ["L", model.lists], ["T", model.trees], ["W", model.web ? [model.web] : []], ["F", model.files], ["X", model.excludes]]) {
    for (const v of list) out.push([tag, v]);
  }
  return out;
}

/** CSS modifier of a section status (`Maintained`, `Supported`, `Odd Fixes`, `Orphan`, `Obsolete`). */
export function statusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s.startsWith("supported")) return "supported";
  if (s.startsWith("maintained")) return "maintained";
  if (s.startsWith("odd")) return "odd";
  if (s.startsWith("orphan") || s.startsWith("obsolete")) return "orphan";
  return "other";
}

/** Live body of a maintainer node. */
export function renderMaintainerBody(model) {
  let html = `<div class="nm-mt-head"><span class="nm-mt-title">${esc(model.title || "(section)")}</span>${model.status ? `<span class="nm-mt-status nm-mt-${statusClass(model.status)}" title="S: ${esc(model.status)}">${esc(model.status)}</span>` : ""}</div>`;
  const lines = maintainerLines(model);
  if (!lines.length) html += '<div class="nm-empty">No entries</div>';
  for (const [tag, v] of lines) html += `<div class="nm-mt-row"><span class="nm-mt-tag nm-mt-tag-${tag}">${tag}:</span><span class="nm-mt-val">${esc(v)}</span></div>`;
  return `<div class="nm-mt nm-scroll">${html}</div>`;
}
