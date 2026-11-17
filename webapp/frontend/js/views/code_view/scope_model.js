/**
 * scope_model.js - DOM-free parts of local-scope resolution in the code view (§5.6): the position a token
 * request carries, the hover-card section of a ScopeInfo payload (`GET /symbols/{v}/scope`, the `scope` member
 * of hover/detail/xref), the usage ranges the occurrence highlighter paints and the XRef access badges.
 *
 * Positions follow the backend's convention: `line` 1-based, `col` the 1-based UTF-8 byte column of the
 * identifier's first character (the `/fs/file` token columns).
 */
import { escapeHtml } from "../../components/modal.js";
import { buildByteToCharMap, byteColToCharIdx, charIdxToByteCol } from "../../utils/text_coordinates.js";

/** Files the scope model resolves locally (C sources and headers); everything else behaves as before. */
export const isScopeFile = (path) => /\.[ch]$/i.test(path || "");

export const SCOPE_KIND_LABEL = {
  param: "parameter", local: "local variable", static_local: "static local", file: "file variable", field: "struct field",
};
export const scopeKindLabel = (kind) => SCOPE_KIND_LABEL[kind] || String(kind || "variable").replace(/_/g, " ");

/** Access of a use: CSS suffix, XRef badge, and the "here" phrase of a field. */
export const ACCESS_KINDS = ["decl", "r", "w", "rw", "addr"];
export const ACCESS_BADGE = { decl: "Decl", r: "Read", w: "Write", rw: "ReadWrite", addr: "AddressOf" };
const ACCESS_HERE = { decl: "declared here", r: "read here", w: "written here", rw: "read and written here", addr: "address taken here" };
export const accessKind = (a) => (ACCESS_KINDS.includes(a) ? a : "r");

/** The ScopeInfo of a payload (a ScopeInfo itself or a hover/detail/xref response carrying `scope`), or null. */
export function scopeOf(payload) {
  if (!payload || typeof payload !== "object") return null;
  const s = payload.scope && typeof payload.scope === "object" ? payload.scope : payload;
  return s.found && s.kind && Array.isArray(s.uses) ? s : (s.found && s.kind ? { ...s, uses: [] } : null);
}

// --- Position of a token ---

/**
 * Characters of text before `target` inside `root` (document order; `childNodes`, `nodeType` 3 = text), or -1
 * when `target` is not below `root`.
 */
export function textOffsetOf(root, target) {
  let offset = 0;
  const walk = (node) => {
    if (node === target) return true;
    if (node.nodeType === 3) {
      offset += (node.nodeValue || "").length;
      return false;
    }
    for (const child of node.childNodes || []) if (walk(child)) return true;
    return false;
  };
  for (const child of root?.childNodes || []) if (walk(child)) return offset;
  return root === target ? 0 : -1;
}

/**
 * 1-based byte column of `name` in `lineText` for a token whose text starts at character `charStart`: the name
 * at that character, else its first whole-word occurrence from there (a token span wider than its name), else
 * `charStart` itself.
 */
export function tokenColumn(lineText, charStart, name = "") {
  const text = String(lineText ?? "");
  let at = Math.max(0, Math.min(Number(charStart) || 0, text.length));
  if (name && !text.startsWith(name, at)) {
    const re = new RegExp(`(?<![A-Za-z0-9_])${name.replace(/[^A-Za-z0-9_]/g, "")}(?![A-Za-z0-9_])`, "g");
    re.lastIndex = at;
    const m = /^[A-Za-z_]\w*$/.test(name) ? re.exec(text) : null;
    if (m) at = m.index;
  }
  return charIdxToByteCol(text, at);
}

/**
 * `{line, col}` of a rendered token span (`.ast-token` inside `.code-text` of a `.code-row[data-line-no]`), or
 * null when it is not on a line of the file.
 */
export function tokenPosition(span) {
  const row = span?.closest?.(".code-row");
  const cell = span?.closest?.(".code-text");
  const line = parseInt(row?.dataset?.lineNo || "", 10);
  if (!line || !cell) return null;
  const start = textOffsetOf(cell, span);
  if (start < 0) return { line, col: 1 };
  const name = (span.dataset?.name || span.textContent || "").trim();
  return { line, col: tokenColumn(cell.textContent || "", start, name) };
}

// --- Occurrence ranges ---

const WORD = /[A-Za-z0-9_]/;

/**
 * Usage ranges per line: Map(line -> [{start, end, access}]) in 0-based characters of the line text
 * (`lineText(line)`), from the ScopeInfo `uses` (`{line, col, access}`, byte columns). Each range covers the
 * identifier at the use's column; when the column is not on an identifier, the whole-word `name` nearest to it on
 * that line is taken. Ranges are sorted and never overlap.
 */
export function occurrenceRanges(uses, lineText, name = "") {
  const out = new Map();
  const maps = new Map();
  for (const u of uses || []) {
    const line = Number(u?.line);
    const text = line > 0 ? lineText(line) : null;
    if (typeof text !== "string" || !text) continue;
    if (!maps.has(line)) maps.set(line, buildByteToCharMap(text));
    let start = byteColToCharIdx(maps.get(line), u.col || 1);
    let end = start;
    while (end < text.length && WORD.test(text[end])) end++;
    if (end === start || (start > 0 && WORD.test(text[start - 1]))) {
      const near = nearestWord(text, name, start);
      if (!near) continue;
      [start, end] = near;
    }
    if (!out.has(line)) out.set(line, []);
    out.get(line).push({ start, end, access: accessKind(u.access) });
  }
  for (const [line, list] of out) {
    list.sort((a, b) => a.start - b.start);
    const merged = [];
    for (const r of list) {
      const last = merged[merged.length - 1];
      if (last && r.start < last.end) continue;
      merged.push(r);
    }
    out.set(line, merged);
  }
  return out;
}

function nearestWord(text, name, from) {
  if (!name || !/^[A-Za-z_]\w*$/.test(name)) return null;
  const re = new RegExp(`(?<![A-Za-z0-9_])${name}(?![A-Za-z0-9_])`, "g");
  let best = null;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (!best || Math.abs(m.index - from) < Math.abs(best[0] - from)) best = [m.index, m.index + name.length];
  }
  return best;
}

// --- Hover card section ---

/** Where "Go to declaration" leads: the field's definition, else the declaration in the viewed file. */
export function scopeDeclTarget(scope) {
  if (!scope) return null;
  if (scope.kind === "field" || scope.member) {
    const fd = scope.member?.field_def;
    return fd?.file ? { file: fd.file, line: fd.line || 1 } : null;
  }
  return scope.decl?.line ? { file: scope.file, line: scope.decl.line } : null;
}

/** Where "Go to type definition" leads: `{file, line}` of `type_def`, else `{name}` to look up by tag/typedef name. */
export function scopeTypeTarget(scope) {
  if (!scope) return null;
  if (scope.type_def?.file) return { file: scope.type_def.file, line: scope.type_def.line || 1, name: scope.type_def.name || "" };
  const ref = scope.type_ref;
  if (ref?.name && ref.kind !== "builtin") return { name: ref.name };
  return null;
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "5 reads · 1 write · 1 address-of" (zero counts left out; "" when there are none). */
export function formatCounts(counts) {
  if (!counts) return "";
  const parts = [];
  if (counts.r) parts.push(plural(counts.r, "read"));
  if (counts.w) parts.push(plural(counts.w, "write"));
  if (counts.rw) parts.push(plural(counts.rw, "read-write"));
  if (counts.addr) parts.push(plural(counts.addr, "address-of", "address-ofs"));
  return parts.join(" · ");
}

/** A link of the card: `data-goto-file` / `data-goto-line` (the card's click handler navigates). */
function gotoLink(file, line, inner, title = "") {
  if (!file) return inner;
  return `<a href="#" class="hc-link" data-goto-file="${escapeHtml(file)}" data-goto-line="${Number(line) || 1}"${title ? ` title="${escapeHtml(title)}"` : ""}>${inner}</a>`;
}

/**
 * Card body of a ScopeInfo (escaped HTML): kind badge, signature, type (linked to its definition), member path,
 * declaration / initialization / last assignment lines, inferred callee return types, usage counts.
 * `signature`: the hover payload's, when there is one.
 */
export function formatScopeCard(scope, { name = "", signature = "" } = {}) {
  const shown = scope.name || name;
  const isField = scope.kind === "field" || Boolean(scope.member);
  const here = scope.file;
  const lineLink = (pos, label) => gotoLink(here, pos.line, `L${Number(pos.line) || 1}`, `${label} at ${here}:${pos.line}`);
  let html = `<div class="hc-head"><span class="hc-kind hc-kind-scope hc-scope-${escapeHtml(scope.kind)}">${escapeHtml(scopeKindLabel(scope.kind))}</span><code>${escapeHtml(shown)}</code></div>`;
  const sig = signature || (scope.type ? `${scope.type}${/[*\s]$/.test(scope.type) ? "" : " "}${shown}` : "");
  if (sig) html += `<pre class="hc-sig">${escapeHtml(sig)}</pre>`;

  if (scope.type) {
    const td = scope.type_def;
    const inner = `<code>${escapeHtml(scope.type)}</code>`;
    html += `<div class="hc-type">type ${td?.file ? gotoLink(td.file, td.line, inner, `${td.kind || ""} ${td.name || ""} — ${td.file}:${td.line}`.trim()) : inner}</div>`;
  } else {
    html += `<div class="hc-type hc-muted">type unknown${scope.effective_type ? ` — inferred <code>${escapeHtml(scope.effective_type)}</code>` : ""}</div>`;
  }

  if (isField && scope.member) {
    const m = scope.member;
    const st = m.struct;
    const stName = st ? `${st.kind || "struct"} ${st.name || ""}`.trim() : (m.base_type || "");
    const stHtml = stName ? (st?.file ? gotoLink(st.file, st.line, `<code>${escapeHtml(stName)}</code>`, `${st.file}:${st.line}`) : `<code>${escapeHtml(stName)}</code>`) : "";
    const path = `${m.base || ""}${m.op || "."}${(m.path || [shown]).join(m.op || ".")}`;
    html += `<div class="hc-member">field <code>${escapeHtml(shown)}</code>${stHtml ? ` of ${stHtml}` : ""} <span class="hc-muted">via <code>${escapeHtml(path)}</code></span></div>`;
    if (scope.access) html += `<div class="hc-access hc-acc-${accessKind(scope.access)}">${escapeHtml(ACCESS_HERE[accessKind(scope.access)])}</div>`;
    if (m.field_def?.file) html += `<div class="hc-loc">defined at ${gotoLink(m.field_def.file, m.field_def.line, `${escapeHtml(m.field_def.file)}:${Number(m.field_def.line) || 1}`)}</div>`;
  } else {
    const lines = [];
    if (scope.decl?.line) lines.push(`declared at ${lineLink(scope.decl, "Declared")}`);
    if (scope.init?.line && scope.init.line !== scope.decl?.line) lines.push(`initialized at ${lineLink(scope.init, "Initialized")}`);
    else if (scope.init?.line) lines.push("initialized there");
    if (scope.last_assign?.line && scope.last_assign.line !== scope.init?.line) lines.push(`last assigned at ${lineLink(scope.last_assign, "Last assigned")}`);
    if (lines.length) html += `<div class="hc-lines">${lines.join(" · ")}</div>`;
    if (scope.function?.name) html += `<div class="hc-muted hc-fn">in <code>${escapeHtml(scope.function.name)}()</code></div>`;
  }

  const inferred = (scope.inferred || []).filter((x) => x && x.callee);
  if (inferred.length) {
    html += `<ul class="hc-inferred">${inferred.slice(0, 4).map((x) => {
      const callee = gotoLink(x.file, x.def_line, `<code>${escapeHtml(x.callee)}()</code>`, x.file ? `${x.file}:${x.def_line}` : "");
      const mark = x.agrees === true
        ? ` <span class="hc-agree" title="Matches the declared type">✓</span>`
        : x.agrees === false ? ` <span class="hc-disagree" title="Differs from the declared type">✗</span>` : "";
      return `<li>${x.line ? `${lineLink({ line: x.line }, "Assigned")} ` : ""}= ${callee}${x.return_type ? ` → <code>${escapeHtml(x.return_type)}</code>` : ""}${mark}</li>`;
    }).join("")}</ul>`;
  }

  const counts = formatCounts(scope.counts);
  if (counts) html += `<div class="hc-counts hc-muted">${escapeHtml(counts)}${isField ? " in this function" : ""}</div>`;
  return html;
}

/** Action buttons of a scope card (`data-hc` values handled by HoverCard.act). */
export function scopeActions(scope) {
  const acts = [];
  if (scopeDeclTarget(scope)) acts.push(["decl", scope.kind === "field" || scope.member ? "Go to field definition" : "Go to declaration"]);
  if (scopeTypeTarget(scope)) acts.push(["typedef", "Go to type definition"]);
  if (scope.uses?.length) acts.push(["hl", "Highlight usages"]);
  acts.push(["refs", "References"]);
  return acts.map(([k, l]) => `<button type="button" data-hc="${k}">${escapeHtml(l)}</button>`).join("");
}

// --- XRef modal ---

/** Badge of an XRef reference: `{label, cls}` (`cls` "" for references without an access). */
export function xrefBadge(ref) {
  const acc = ACCESS_KINDS.includes(ref?.access) ? ref.access : "";
  return { label: ref?.type || (acc ? ACCESS_BADGE[acc] : "Call"), cls: acc ? `xref-acc xref-acc-${acc}` : "" };
}

/** Modal title of an XRef payload carrying a ScopeInfo: "Uses of parameter dev in sys_pciconfig_write()". */
export function scopeXrefTitle(scope, name) {
  if (!scope) return "";
  const fn = scope.function?.name ? ` in ${scope.function.name}()` : "";
  return `Uses of ${scopeKindLabel(scope.kind)} ${scope.name || name}${fn}`;
}
