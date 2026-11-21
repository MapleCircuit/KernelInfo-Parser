/**
 * struct_view.js - HTML of a NodeMap struct node body from `GET /api/nodemap/{v}/struct/{name}`
 * (`webapp/API_ENDPOINTS.md` §4.14): fields, nested struct/union members, and the `#if` / `#ifdef` /
 * `#ifndef` / `#elif` / `#else` / `#endif` lines guarding them.
 *
 * DOM-free (unit tested). Toggles are `data-act` buttons the renderer delegates:
 *   toggle-type  (data-key)  one member's type, an aggregate's type with every member inside it
 *   collapse     (data-key)  fold a nested struct/union
 * Every row carries `data-key` (canvas_model.structItemKey) so edges can attach to a field.
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

const condTitle = (conds) => (conds && conds.length ? `Built when: ${conds.join(" && ")}` : "");

function fieldRow(it, data, depth) {
  const key = structItemKey(it);
  const showType = typeVisible(data, key);
  const guarded = it.conds && it.conds.length;
  const suffix = `${it.array || ""}${it.bits != null ? ` : ${it.bits}` : ""}`;
  return `<div class="nm-sf nm-sf-field${guarded ? " nm-guarded" : ""}" data-key="${esc(key)}" style="--nm-depth:${depth}" title="${esc(condTitle(it.conds))}">
    <span class="nm-port nm-port-field nm-port-left" data-field="${esc(key)}" data-side="left"></span>
    <button type="button" class="nm-sf-tt${showType ? " on" : ""}" data-act="toggle-type" data-key="${esc(key)}" title="${showType ? "Hide" : "Show"} the type of ${esc(it.name)}">T</button>
    ${showType ? `<span class="nm-sf-type">${esc(it.type)}</span>` : ""}
    <span class="nm-sf-name">${esc(it.name)}</span>${suffix ? `<span class="nm-sf-suffix">${esc(suffix)}</span>` : ""}
    ${guarded && !data.showGuards ? `<span class="nm-sf-guard" aria-label="${esc(condTitle(it.conds))}">#</span>` : ""}
    <span class="nm-sf-line">${it.line ? esc(it.line) : ""}</span>
    <span class="nm-port nm-port-field nm-port-right" data-field="${esc(key)}" data-side="right"></span>
  </div>`;
}

function directiveRow(it, depth) {
  return `<div class="nm-sf nm-sf-pp nm-pp-${esc(it.directive)}" style="--nm-depth:${depth};--nm-pp-depth:${it.depth || 0}" title="line ${esc(it.line)}">
    <span class="nm-sf-pptext">${esc(it.text || `#${it.directive} ${it.expr || ""}`)}</span>
  </div>`;
}

function aggregateRows(it, data, depth) {
  const key = structItemKey(it);
  const showType = typeVisible(data, key);
  const folded = Boolean(data.collapsed?.[key]);
  const label = it.type_name || `${it.tag} {…}`;
  const head = `<div class="nm-sf nm-sf-agg${it.conds?.length ? " nm-guarded" : ""}" data-key="${esc(key)}" style="--nm-depth:${depth}" title="${esc(condTitle(it.conds))}">
    <span class="nm-port nm-port-field nm-port-left" data-field="${esc(key)}" data-side="left"></span>
    <button type="button" class="nm-sf-fold" data-act="collapse" data-key="${esc(key)}" aria-expanded="${folded ? "false" : "true"}" title="${folded ? "Expand" : "Collapse"} ${esc(label)}">${folded ? "▸" : "▾"}</button>
    <button type="button" class="nm-sf-tt${showType ? " on" : ""}" data-act="toggle-type" data-key="${esc(key)}" title="${showType ? "Hide" : "Show"} the types inside ${esc(label)}">T</button>
    <span class="nm-sf-tag">${esc(it.tag)}</span>${showType && it.type_name ? `<span class="nm-sf-type">${esc(it.type_name.replace(/^(struct|union)\s+/, ""))}</span>` : ""}
    <span class="nm-sf-name">${esc(it.name || "(anonymous)")}</span>
    <span class="nm-sf-line">${it.line ? esc(it.line) : ""}</span>
    <span class="nm-port nm-port-field nm-port-right" data-field="${esc(key)}" data-side="right"></span>
  </div>`;
  if (folded) return head;
  return head + renderItems(it.children || [], data, depth + 1);
}

function renderItems(items, data, depth) {
  let html = "";
  for (const it of items) {
    if (it.kind === "directive") {
      if (data.showGuards) html += directiveRow(it, depth);
    } else if (it.kind === "aggregate") {
      html += aggregateRows(it, data, depth);
    } else {
      html += fieldRow(it, data, depth);
    }
  }
  return html;
}

/**
 * Body HTML of a struct node: the toolbar (struct-wide type and guard toggles, source of the span) and
 * the member rows. `layout` is the endpoint's response, `data` the node's struct state.
 */
export function renderStructBody(layout, data) {
  const directives = countDirectives(layout.items);
  const outer = layout.outer_conds?.length ? `<div class="nm-sf-outer" title="Conditions around the whole definition">${esc(layout.outer_conds.join(" && "))}</div>` : "";
  const src = `${layout.file_path || ""}:${layout.line_s || ""}`;
  return `<div class="nm-struct-bar">
      <button type="button" class="nm-chip${data.showTypes ? " on" : ""}" data-act="all-types" aria-pressed="${data.showTypes}" title="Show or hide every member type">Types</button>
      <button type="button" class="nm-chip${data.showGuards ? " on" : ""}" data-act="guards" aria-pressed="${data.showGuards}" ${directives ? "" : "disabled"} title="${directives ? `Show or hide the ${directives} preprocessor lines` : "No preprocessor lines inside this definition"}">#ifdef${directives ? ` ${directives}` : ""}</button>
      <span class="nm-struct-src" title="${esc(`${src} (span from ${layout.span_source === "db" ? "the index" : "the source text"})`)}">${esc(src)}</span>
    </div>
    ${outer}
    <div class="nm-struct-rows nm-scroll">${renderItems(layout.items || [], data, 0) || '<div class="nm-empty">No members</div>'}</div>`;
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
