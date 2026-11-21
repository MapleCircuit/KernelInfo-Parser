/**
 * struct_view.js - NodeMap struct node body from `GET /api/nodemap/{v}/struct/{name}`
 * (`webapp/API_ENDPOINTS.md` §4.14): fields, nested struct/union members, and the `#if` / `#ifdef` /
 * `#ifndef` / `#elif` / `#else` / `#endif` lines guarding them.
 *
 * DOM-free (unit tested). `structRows()` turns a layout and the node's struct state into display rows
 * (shared by the HTML body and the SVG export); `renderStructBody()` draws them in the node's `list` or
 * `table` style. Toggles are `data-act` buttons the controller delegates:
 *   collapse     (data-key)  fold a nested struct/union                       (always)
 *   guard-fold   (data-key)  fold / unfold one #if branch                     (always)
 *   toggle-type  (data-key)  one member's type, an aggregate's with its subtree (Edit Mode)
 *   hide-item    (data-key)  hide / restore a member, aggregate or #if block  (Edit Mode)
 *   all-types, guards        struct-wide type and guard-line toggles         (Edit Mode)
 * Every member row carries `data-key` (canvas_model.structItemKey) so edges can attach to a field.
 */
import { structItemKey, typeVisible } from "./canvas_model.js";

export function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** Number of preprocessor directive rows anywhere in a layout. */
export function countDirectives(items) {
  let n = 0;
  for (const it of items || []) {
    if (it.kind === "directive") n += 1;
    else if (it.children) n += countDirectives(it.children);
  }
  return n;
}

/** Every field/aggregate of a layout, depth first (directives left out). */
export function flattenMembers(items, out = []) {
  for (const it of items || []) {
    if (it.kind === "directive") continue;
    out.push(it);
    if (it.children) flattenMembers(it.children, out);
  }
  return out;
}

// ------------------------------------------------------------------ #if blocks and Kconfig truth

/** Tri-state of a condition (the values `DirectiveEvaluator` uses). */
export const TRUTH = { TRUE: 1, FALSE: 0, UNKNOWN: -1 };
const T = TRUTH.TRUE, F = TRUTH.FALSE, U = TRUTH.UNKNOWN;
const tri = (v) => (v === T || v === true ? T : v === F || v === false ? F : U);
const not = (v) => (v === U ? U : v === T ? F : T);
const and = (a, b) => (a === F || b === F ? F : a === T && b === T ? T : U);

const OPENERS = new Set(["if", "ifdef", "ifndef"]);

/**
 * The items of one layout level grouped into `#if` blocks:
 *   {kind: "guard", key, open, end, branches: [{key, dir, items}]}
 * (`key` of a block and of each branch is the structItemKey of its directive line). Members outside
 * any block, and directives that close or continue nothing, stay as they are; a block left open at the
 * end of the level ends there.
 */
export function guardTree(items) {
  const root = [];
  const stack = [];
  const cur = () => (stack.length ? stack[stack.length - 1].branch.items : root);
  for (const it of items || []) {
    if (it.kind !== "directive") {
      cur().push(it);
      continue;
    }
    const d = it.directive;
    if (OPENERS.has(d)) {
      const branch = { key: structItemKey(it), dir: it, items: [] };
      const block = { kind: "guard", key: branch.key, open: it, end: null, branches: [branch] };
      cur().push(block);
      stack.push({ block, branch });
    } else if ((d === "elif" || d === "else") && stack.length) {
      const top = stack[stack.length - 1];
      top.branch = { key: structItemKey(it), dir: it, items: [] };
      top.block.branches.push(top.branch);
    } else if (d === "endif" && stack.length) {
      stack.pop().block.end = it;
    } else {
      cur().push(it);
    }
  }
  return root;
}

/** Macro name of an `#ifdef` / `#ifndef` line. */
function macroOf(dir) {
  const src = dir.expr || String(dir.text || "").replace(/^\s*#\s*\w+/, "");
  return (/[A-Za-z_]\w*/.exec(src.replace(/\/\*.*?\*\/|\/\/.*$/g, "")) || [""])[0];
}

/**
 * Truth of one directive's own condition under `evaluator` (a `DirectiveEvaluator`, or anything with
 * `isDefined(macro)` and `evalExpression(expr)`): `#else` is true, no evaluator is unknown.
 */
export function directiveTruth(evaluator, dir) {
  if (!evaluator) return U;
  try {
    const d = dir.directive;
    if (d === "else") return T;
    if (d === "ifdef") return tri(evaluator.isDefined(macroOf(dir)));
    if (d === "ifndef") return not(tri(evaluator.isDefined(macroOf(dir))));
    if (d === "if" || d === "elif") {
      const expr = dir.expr || String(dir.text || "").replace(/^\s*#\s*(?:el)?if\b/, "");
      return tri(evaluator.evalExpression(expr));
    }
  } catch {
    // an expression the evaluator rejects decides nothing
  }
  return U;
}

/** Truth of each branch of a block: its own condition and every earlier branch not taken. */
export function branchTruths(block, evaluator) {
  const out = [];
  let none = T; // no earlier branch taken
  for (const br of block.branches) {
    const own = directiveTruth(evaluator, br.dir);
    out.push(and(none, own));
    none = and(none, not(own));
  }
  return out;
}

const TRUTH_NAME = { [T]: "true", [F]: "false", [U]: "unknown" };

// ------------------------------------------------------------------ display rows

const condTitle = (conds) => (conds && conds.length ? `Built when: ${conds.join(" && ")}` : "");

/**
 * Display rows of a struct layout under the node's struct state `data`:
 *   opts.editMode   hidden items show dimmed with a restore button (else they are left out)
 *   opts.evaluator  Kconfig evaluator; branches it compiles out start folded (null: no auto-collapse)
 * Returns `{rows, tracks, typeCh, hidden}`: `tracks` is the number of nested `#if` bar tracks,
 * `typeCh` the width (ch) of the type column in table style, `hidden` how many items are hidden.
 * Row kinds: field, agg, pp (a directive line), summary (a folded branch). Each row has `depth`
 * (aggregate nesting) and `bars` ([level, "full"|"start"|"mid"|"end"] of the guard tracks it crosses).
 */
export function structRows(layout, data, opts = {}) {
  const editMode = Boolean(opts.editMode);
  const evaluator = opts.evaluator || null;
  const showGuards = Boolean(data.showGuards);
  const hiddenMap = data.hiddenItems || {};
  const manual = data.collapsedGuards || {};
  const rows = [];
  let tracks = 0;
  let typeCh = 0;
  let hiddenHit = 0;
  const isHidden = (key) => {
    const h = Boolean(hiddenMap[key]);
    if (h) hiddenHit += 1;
    return h;
  };
  const barsAt = (level, cap = null) => {
    if (!showGuards) return [];
    const out = [];
    for (let l = 0; l < level; l++) out.push([l, "full"]);
    if (cap) out.push([level, cap]);
    const n = level + (cap ? 1 : 0);
    if (n > tracks) tracks = n;
    return out;
  };
  const noteType = (text, depth) => {
    if (text) typeCh = Math.max(typeCh, String(text).length + depth * 2);
  };

  const walk = (items, depth, level, eff, dimmed) => {
    for (const node of guardTree(items)) {
      if (node.kind === "guard") block(node, depth, level, eff, dimmed);
      else if (node.kind === "directive") {
        if (showGuards) rows.push({ kind: "pp", key: structItemKey(node), depth, bars: barsAt(level), text: node.text || `#${node.directive} ${node.expr || ""}`, directive: node.directive, line: node.line, dim: dimmed, dead: eff === F });
      } else member(node, depth, level, eff, dimmed);
    }
  };

  const member = (it, depth, level, eff, dimmed) => {
    const key = structItemKey(it);
    const own = isHidden(key);
    if (own && !editMode) return;
    const dim = dimmed || own;
    const showType = typeVisible(data, key);
    const base = { key, depth, bars: barsAt(level), line: it.line || null, conds: it.conds || [], hidden: own, dim, dead: eff === F, showType };
    if (it.kind === "aggregate") {
      const folded = Boolean(data.collapsed?.[key]);
      const typeText = showType && it.type_name ? it.type_name.replace(/^(struct|union)\s+/, "") : "";
      noteType(`${it.tag} ${typeText}`, depth);
      rows.push({ ...base, kind: "agg", tag: it.tag, typeText, label: it.type_name || `${it.tag} {…}`, name: it.name || "(anonymous)", folded, count: flattenMembers(it.children).length });
      if (!folded) walk(it.children || [], depth + 1, level, eff, dim);
    } else {
      if (showType) noteType(it.type, depth);
      rows.push({ ...base, kind: "field", type: showType ? it.type || "" : "", name: it.name, suffix: `${it.array || ""}${it.bits != null ? ` : ${it.bits}` : ""}` });
    }
  };

  const block = (b, depth, level, eff, dimmed) => {
    const own = isHidden(b.key);
    if (own && !editMode) return;
    const dim = dimmed || own;
    const truths = branchTruths(b, evaluator);
    b.branches.forEach((br, i) => {
      const truth = and(eff, truths[i]);
      const count = flattenMembers(br.items).length;
      const auto = Boolean(evaluator) && truth === F;
      const m = manual[br.key];
      const folded = count > 0 && (typeof m === "boolean" ? m : auto);
      const cap = i === 0 ? "start" : "mid";
      const text = br.dir.text || `#${br.dir.directive} ${br.dir.expr || ""}`.trim();
      const head = {
        key: br.key, blockKey: b.key, opener: i === 0, depth, line: br.dir.line, text, directive: br.dir.directive,
        truth: TRUTH_NAME[truth], auto, folded, count, hidden: i === 0 && own, dim, dead: eff === F,
      };
      if (folded) rows.push({ ...head, kind: "summary", bars: barsAt(level, cap) });
      else if (showGuards || (i === 0 && own)) rows.push({ ...head, kind: "pp", bars: barsAt(level, cap) });
      if (!folded) walk(br.items, depth, level + 1, truth, dim);
    });
    if (b.end && showGuards) rows.push({ kind: "pp", key: structItemKey(b.end), blockKey: b.key, closer: true, depth, bars: barsAt(level, "end"), text: b.end.text || "#endif", directive: "endif", line: b.end.line, dim, dead: eff === F });
  };

  walk(layout?.items || [], 0, 0, T, false);
  // `hidden` counts the hidden items this layout reaches (inside a folded part they are not visited)
  return { rows, tracks, typeCh: Math.max(4, Math.min(48, typeCh + 1)), hidden: hiddenHit };
}

// ------------------------------------------------------------------ HTML

const BAR_W = 6; // canvas units per #if track (struct rows are zoom-controlled, not UI-scaled)

function barsHtml(bars) {
  return bars.map(([l, part]) => `<span class="nm-pp-bar nm-pp-${part} nm-pp-l${l % 4}" style="--nm-pp-level:${l}"></span>`).join("");
}

function hideBtn(r, editMode, key) {
  if (!editMode) return "";
  return `<button type="button" class="nm-sf-hide${r.hidden ? " on" : ""}" data-act="hide-item" data-key="${esc(key)}" title="${r.hidden ? "Restore" : "Hide"} this ${r.kind === "field" ? "member" : r.kind === "agg" ? "aggregate and its members" : "#if block"}">${r.hidden ? "↺" : "✕"}</button>`;
}

function rowClass(r, extra) {
  return `nm-sf ${extra}${r.dim ? " nm-sf-dim" : ""}${r.hidden ? " nm-sf-hidden" : ""}${r.dead ? " nm-sf-dead" : ""}`;
}

function rowHtml(r, data, editMode, table) {
  const ports = (key) => [
    `<span class="nm-port nm-port-field nm-port-left" data-field="${esc(key)}" data-side="left"></span>`,
    `<span class="nm-port nm-port-field nm-port-right" data-field="${esc(key)}" data-side="right"></span>`,
  ];
  const line = `<span class="nm-sf-line">${r.line ? esc(r.line) : ""}</span>`;
  const style = `style="--nm-depth:${r.depth}"`;
  if (r.kind === "pp" || r.kind === "summary") {
    const fold = r.count ? `<button type="button" class="nm-sf-fold" data-act="guard-fold" data-key="${esc(r.key)}" aria-expanded="${r.folded ? "false" : "true"}" title="${r.folded ? "Show" : "Fold"} the ${r.count} member${r.count === 1 ? "" : "s"} of this branch">${r.folded ? "▸" : "▾"}</button>` : "";
    const truth = r.truth && r.truth !== "unknown" ? ` nm-pp-${r.truth}` : "";
    const summary = r.kind === "summary"
      ? `<button type="button" class="nm-sf-ppsum-label" data-act="guard-fold" data-key="${esc(r.key)}" title="Show the members of this branch">${r.count} member${r.count === 1 ? "" : "s"}${r.auto ? " · compiled out" : " · folded"}</button>`
      : "";
    const dirClass = r.directive ? ` nm-pp-${esc(r.directive)}` : "";
    return `<div class="${rowClass(r, `nm-sf-pp${r.kind === "summary" ? " nm-sf-ppsum" : ""}${dirClass}${truth}`)}" data-pp-key="${esc(r.key)}" ${style} title="line ${esc(r.line)}${r.truth === "false" ? " · compiled out by the Kconfig engine" : r.truth === "true" ? " · built by the Kconfig engine" : ""}">
    ${barsHtml(r.bars)}<span class="nm-sf-ctl">${fold}</span><span class="nm-sf-pptext">${esc(r.text)}</span>${summary}${line}${r.opener ? hideBtn(r, editMode, r.blockKey) : ""}
  </div>`;
  }
  const [pl, pr] = ports(r.key);
  const guarded = r.conds.length && !data.showGuards;
  const tt = editMode ? `<button type="button" class="nm-sf-tt${r.showType ? " on" : ""}" data-act="toggle-type" data-key="${esc(r.key)}" title="${r.showType ? "Hide" : "Show"} the type${r.kind === "agg" ? "s inside" : " of"} ${esc(r.kind === "agg" ? r.label : r.name)}">T</button>` : "";
  const marker = guarded ? `<span class="nm-sf-guard" aria-label="${esc(condTitle(r.conds))}">#</span>` : "";
  if (r.kind === "agg") {
    const fold = `<button type="button" class="nm-sf-fold" data-act="collapse" data-key="${esc(r.key)}" aria-expanded="${r.folded ? "false" : "true"}" title="${r.folded ? "Expand" : "Collapse"} ${esc(r.label)}">${r.folded ? "▸" : "▾"}</button>`;
    const typeCell = table
      ? `<span class="nm-sf-type nm-sf-cell"><span class="nm-sf-tag">${esc(r.tag)}</span>${r.typeText ? ` ${esc(r.typeText)}` : ""}</span>`
      : `<span class="nm-sf-tag">${esc(r.tag)}</span>${r.typeText ? `<span class="nm-sf-type">${esc(r.typeText)}</span>` : ""}`;
    return `<div class="${rowClass(r, `nm-sf-agg${guarded ? " nm-guarded" : ""}`)}" data-key="${esc(r.key)}" ${style} title="${esc(condTitle(r.conds))}">
    ${pl}${barsHtml(r.bars)}<span class="nm-sf-ctl">${fold}${tt}</span>${typeCell}<span class="nm-sf-name${table ? " nm-sf-cell" : ""}">${esc(r.name)}${r.folded ? `<span class="nm-sf-count"> {${r.count}}</span>` : ""}</span>${marker}${line}${hideBtn(r, editMode, r.key)}${pr}
  </div>`;
  }
  const suffix = r.suffix ? `<span class="nm-sf-suffix">${esc(r.suffix)}</span>` : "";
  const typeCell = table
    ? `<span class="nm-sf-type nm-sf-cell">${esc(r.type)}</span>`
    : (r.showType ? `<span class="nm-sf-type">${esc(r.type)}</span>` : "");
  return `<div class="${rowClass(r, `nm-sf-field${guarded ? " nm-guarded" : ""}`)}" data-key="${esc(r.key)}" ${style} title="${esc(condTitle(r.conds))}">
    ${pl}${barsHtml(r.bars)}<span class="nm-sf-ctl">${tt}</span>${typeCell}<span class="nm-sf-name${table ? " nm-sf-cell" : ""}">${esc(r.name)}${suffix}</span>${marker}${line}${hideBtn(r, editMode, r.key)}${pr}
  </div>`;
}

/**
 * Body HTML of a struct node: the toolbar (struct-wide type and guard toggles in Edit Mode, source of
 * the span) and the member rows in the node's style. `layout` is the endpoint's response, `data` the
 * node's struct state, `opts` as for `structRows()`.
 */
export function renderStructBody(layout, data, opts = {}) {
  const editMode = Boolean(opts.editMode);
  const table = data.style === "table";
  const directives = countDirectives(layout.items);
  const model = opts.model || structRows(layout, data, opts);
  const outer = layout.outer_conds?.length ? `<div class="nm-sf-outer" title="Conditions around the whole definition">${esc(layout.outer_conds.join(" && "))}</div>` : "";
  const src = `${layout.file_path || ""}:${layout.line_s || ""}`;
  const chips = editMode
    ? `<button type="button" class="nm-chip${data.showTypes ? " on" : ""}" data-act="all-types" aria-pressed="${data.showTypes}" title="Show or hide every member type">Types</button>
      <button type="button" class="nm-chip${data.showGuards ? " on" : ""}" data-act="guards" aria-pressed="${data.showGuards}" ${directives ? "" : "disabled"} title="${directives ? `Show or hide the ${directives} preprocessor lines` : "No preprocessor lines inside this definition"}">#ifdef${directives ? ` ${directives}` : ""}</button>`
    : "";
  const hiddenNote = editMode && model.hidden ? `<span class="nm-chip nm-chip-note" title="Hidden items show dimmed in Edit Mode">${model.hidden} hidden</span>` : "";
  const rowsHtml = model.rows.map((r) => rowHtml(r, data, editMode, table)).join("");
  const vars = `--nm-pp-tracks:${model.tracks};--nm-gutter:${model.tracks ? model.tracks * BAR_W + BAR_W : 0}px;--nm-type-col:${model.typeCh}ch`;
  return `<div class="nm-struct-bar">
      ${chips}${hiddenNote}
      <span class="nm-struct-src" title="${esc(`${src} (span from ${layout.span_source === "db" ? "the index" : "the source text"})`)}">${esc(src)}</span>
    </div>
    ${outer}
    <div class="nm-struct-rows nm-scroll nm-style-${table ? "table" : "list"}" style="${vars}">${rowsHtml || '<div class="nm-empty">No members</div>'}</div>`;
}

/** Find a layout item by key (depth first). */
export function findItem(items, key) {
  for (const it of items || []) {
    if (it.kind !== "directive" && structItemKey(it) === key) return it;
    const hit = it.children ? findItem(it.children, key) : null;
    if (hit) return hit;
  }
  return null;
}

/** Whether the Kconfig engine folds branch `key` on its own (the baseline a manual toggle compares to). */
export function guardAutoCollapsed(layout, key, evaluator) {
  if (!evaluator || !layout) return false;
  let hit = null;
  const walk = (items, eff) => {
    for (const n of guardTree(items)) {
      if (hit !== null) return;
      if (n.kind === "guard") {
        const truths = branchTruths(n, evaluator);
        n.branches.forEach((br, i) => {
          const t = and(eff, truths[i]);
          if (br.key === key) hit = t === F;
          if (hit === null) walk(br.items, t);
        });
      } else if (n.kind === "aggregate") walk(n.children || [], eff);
    }
  };
  walk(layout.items || [], T);
  return Boolean(hit);
}
