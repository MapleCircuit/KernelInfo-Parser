/**
 * struct_view.js - NodeMap struct node body from `GET /api/nodemap/{v}/struct/{name}` and
 * `GET /api/tools/{v}/pahole/{name}` (`webapp/API_ENDPOINTS.md` §4.14): fields, nested struct/union
 * members, `#if` / `#ifdef` lines, pointer/type follow links, and `layout` cache-line view.
 *
 * DOM-free (unit tested).
 */
import { OUTER_KEY, formatLockBadge, structItemKey, typeVisible } from "./canvas_model.js";

function findFieldLock(data, key, name) {
  const map = data?.fieldLocks;
  if (!map || typeof map !== "object") return null;
  if (key && map[key]) return map[key];
  if (name) {
    const suffix = `:${name}`;
    for (const [k, v] of Object.entries(map)) {
      if (k === name || k.endsWith(suffix)) return v;
    }
  }
  return null;
}

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

/**
 * Parse a referenced `struct` or `union` type from a C member type string (`struct task_struct *`,
 * `const union foo`, `struct list_head`, etc.). Returns `{ kind, name, isPointer, edgeLabel }` or null.
 */
export function extractReferencedStruct(typeStr) {
  const s = String(typeStr || "").trim();
  if (!s || /\(\s*\*/.test(s)) {
    // Function pointers: still inspect return/arg structs? Avoid noisy matches unless simple struct ptr
    const fm = /\b(struct|union)\s+([A-Za-z_]\w*)\b/.exec(s);
    if (!fm) return null;
    return { kind: fm[1], name: fm[2], isPointer: s.includes("*"), edgeLabel: s.includes("*") ? "points to" : "embeds" };
  }
  const m = /\b(struct|union)\s+([A-Za-z_]\w*)\b/.exec(s);
  if (!m) return null;
  const isPointer = s.includes("*");
  return {
    kind: m[1],
    name: m[2],
    isPointer,
    edgeLabel: isPointer ? "points to" : "embeds",
  };
}

// ------------------------------------------------------------------ non-standard C types ("Expand Types")

/**
 * Names that never become a type node: C keywords, qualifiers and storage classes, the standard C
 * primitives, and the fixed-width / address-sized scalar typedefs of libc and the kernel (`u8`..`u64`,
 * `__le32`, `size_t`, `dma_addr_t`, ...). Everything else in type position is a non-standard type.
 */
const QUALIFIERS = new Set([
  // keywords, qualifiers, storage classes
  "const", "volatile", "restrict", "__restrict", "__restrict__", "static", "extern", "inline", "__inline", "__inline__",
  "register", "auto", "typedef", "_Atomic", "_Noreturn", "_Thread_local", "__extension__",
  // sparse / section annotations that sit in member types
  "__user", "__kernel", "__iomem", "__percpu", "__rcu", "__force", "__bitwise", "__private", "__must_check",
  "__read_mostly", "__packed", "__randomize_layout", "__nocast", "__safe", "__data_racy",
]);

const SCALARS = new Set([
  // standard C primitives
  "void", "char", "short", "int", "long", "float", "double", "bool", "_Bool", "_Complex", "__int128",
  "signed", "__signed__", "unsigned",
  // libc / C99 scalar typedefs
  "size_t", "ssize_t", "ptrdiff_t", "intptr_t", "uintptr_t", "intmax_t", "uintmax_t", "wchar_t", "off_t", "loff_t",
  "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
  "u_int8_t", "u_int16_t", "u_int32_t", "u_int64_t", "u_char", "u_short", "u_int", "u_long", "uint", "ulong", "ushort", "uchar",
  // kernel fixed-width and endian scalars
  "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64", "u128", "s128",
  "__u8", "__u16", "__u32", "__u64", "__s8", "__s16", "__s32", "__s64", "__u128", "__s128",
  "__le16", "__le32", "__le64", "__be16", "__be32", "__be64", "__sum16", "__wsum",
  // POSIX / kernel scalar ids, sizes and times
  "pid_t", "uid_t", "gid_t", "dev_t", "ino_t", "mode_t", "umode_t", "nlink_t", "time_t", "clock_t", "key_t",
  "timer_t", "clockid_t", "fmode_t", "sector_t", "blkcnt_t", "ktime_t", "__kernel_pid_t", "__kernel_uid_t",
  "__kernel_gid_t", "__kernel_time_t", "__kernel_off_t", "__kernel_loff_t",
  // kernel address-sized scalars
  "phys_addr_t", "dma_addr_t", "resource_size_t", "__kernel_size_t", "__kernel_ssize_t",
]);

/**
 * Names that never become a type node: C keywords, qualifiers, storage classes and sparse
 * annotations (skipped), plus the standard C primitives and the fixed-width / address-sized scalar
 * typedefs of libc and the kernel (`u8`..`u64`, `__le32`, `size_t`, `dma_addr_t`, ...), which make a
 * member scalar. Every other identifier in type position is a non-standard type.
 */
export const STANDARD_C_TYPES = new Set([...QUALIFIERS, ...SCALARS]);

const TAG_KINDS = new Set(["struct", "union", "enum"]);

const CALL_ANNOTATION = /\b(?:__attribute__|__aligned|__section|typeof|__typeof|__typeof__|__cacheline_aligned\w*|____cacheline_aligned\w*|__counted_by\w*|__guarded_by|__pt_guarded_by)\s*\(/g;

/** `type` stripped of comments and of call-like annotations with their balanced argument list (`__attribute__((...))`, `typeof(x)`). */
function cleanTypeText(type) {
  const s = String(type || "").replace(/\/\*.*?\*\/|\/\/.*$/g, " ");
  let out = "";
  let at = 0;
  CALL_ANNOTATION.lastIndex = 0;
  for (let m = CALL_ANNOTATION.exec(s); m; m = CALL_ANNOTATION.exec(s)) {
    let depth = 0;
    let end = s.length;
    for (let i = m.index + m[0].length - 1; i < s.length; i++) {
      if (s[i] === "(") depth += 1;
      else if (s[i] === ")" && --depth === 0) {
        end = i + 1;
        break;
      }
    }
    out += `${s.slice(at, m.index)} `;
    at = end;
    CALL_ANNOTATION.lastIndex = end;
  }
  return out + s.slice(at);
}

/** The first type specifier of one declaration segment (`const struct file *filp` -> struct file). */
function segmentRef(seg, label) {
  const toks = String(seg).match(/[A-Za-z_]\w*|\*/g) || [];
  const isPointer = toks.includes("*");
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i];
    if (t === "*") continue;
    if (TAG_KINDS.has(t)) {
      const name = toks[i + 1];
      if (!name || name === "*" || TAG_KINDS.has(name) || STANDARD_C_TYPES.has(name)) return null;
      return { kind: t, name, isPointer, edgeLabel: label || (isPointer ? "points to" : t === "enum" ? "type" : "embeds") };
    }
    if (QUALIFIERS.has(t)) continue;
    // bare compiler / section annotations (`____cacheline_aligned_in_smp`, `__randomize_layout`)
    if (/^_{2,}[a-z]\w*$/.test(t) && !SCALARS.has(t) && !/_t$/.test(t)) continue;
    // a scalar specifier decides the segment (`unsigned long flags`, `u32 len`)
    if (SCALARS.has(t)) return null;
    // macros and constants (`DECLARE_BITMAP`, `NR_CPUS`) are not types
    if (/^[A-Z0-9_]+$/.test(t)) return null;
    return { kind: "typedef", name: t, isPointer, edgeLabel: label || (isPointer ? "points to" : "type") };
  }
  return null;
}

/** Split `s` at top-level commas. */
function splitTop(s) {
  const out = [];
  let depth = 0;
  let cur = "";
  for (const ch of s) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    if (ch === "," && depth === 0) {
      out.push(cur);
      cur = "";
    } else cur += ch;
  }
  out.push(cur);
  return out;
}

/**
 * Every non-standard C type a member type string references, deduplicated in order:
 * `struct` / `union` / `enum` tags and kernel or custom typedefs (`spinlock_t`, `atomic_t`, `gfp_t`).
 * A function pointer (`int (*)(struct inode *, struct file *)`) yields its return and parameter types
 * (edge label `type`); parameter names never count. Standard primitives and scalar typedefs
 * (`STANDARD_C_TYPES`) are left out. Each entry: `{ kind, name, isPointer, edgeLabel }` with
 * `kind` in `struct` | `union` | `enum` | `typedef`.
 */
export function extractTypeRefs(typeStr) {
  const s = cleanTypeText(typeStr).trim();
  if (!s) return [];
  const fp = /\(\s*\*/.exec(s);
  const refs = [];
  if (fp) {
    refs.push(segmentRef(s.slice(0, fp.index), "type"));
    // the parameter list is the parenthesized group right after the `(*name)` declarator
    const close = s.indexOf(")", fp.index);
    const open = close >= 0 ? s.indexOf("(", close) : -1;
    if (open >= 0) {
      let depth = 0;
      let end = -1;
      for (let i = open; i < s.length; i++) {
        if (s[i] === "(") depth += 1;
        else if (s[i] === ")" && --depth === 0) {
          end = i;
          break;
        }
      }
      for (const p of splitTop(s.slice(open + 1, end < 0 ? s.length : end))) refs.push(segmentRef(p, "type"));
    }
  } else refs.push(segmentRef(s, ""));
  const seen = new Set();
  return refs.filter((r) => {
    if (!r) return false;
    const k = `${r.kind} ${r.name}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

/** The first non-standard type a member type references (`extractTypeRefs`), or null. */
export function extractNonStandardType(typeStr) {
  return extractTypeRefs(typeStr)[0] || null;
}

/** Type string of a layout item (aggregates carry their tag apart from the type name). */
const itemTypeText = (it) => (it.kind === "aggregate" ? (it.type_name ? `${it.tag} ${String(it.type_name).replace(/^(struct|union)\s+/, "")}` : "") : it.type || "");

/** The referenced type of member `key` of a struct node: `layout` items or `pahole` rows (`po:` keys). */
export function typeRefOfField(layout, key, pahole = null) {
  if (!key) return null;
  if (String(key).startsWith("po:")) {
    const row = pahole ? paholeRows(pahole, { showTypes: true }).rows.find((r) => r.key === key) : null;
    return row ? extractNonStandardType(row.type) : null;
  }
  const item = layout ? findItem(layout.items, key) : null;
  return item ? extractNonStandardType(itemTypeText(item)) : null;
}

/**
 * The unique non-standard types referenced by the visible members of a struct node (`structRows`
 * under `data` and `opts`, or the pahole rows in `layout` style): `[{ key, ref }]`, `key` being the
 * first member that references `ref` (the field the edge anchors to). The struct itself is left out.
 */
export function collectExpandableTypes(layout, data, opts = {}) {
  const out = [];
  const seen = new Set([`${data?.kind || "struct"} ${data?.name || ""}`]);
  const add = (key, refs) => {
    for (const ref of refs) {
      const k = `${ref.kind} ${ref.name}`;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push({ key, ref });
    }
  };
  if (data?.style === "layout") {
    const pahole = opts.pahole || layout?.pahole || null;
    if (!pahole || pahole.unavailable) return out;
    for (const r of paholeRows(pahole, { ...data, showTypes: true }).rows) if (r.kind === "field") add(r.key, extractTypeRefs(r.type));
    return out;
  }
  if (!layout) return out;
  const model = structRows(layout, { ...data, showTypes: true, typeOverrides: {} }, { ...opts, editMode: false, model: undefined });
  for (const r of model.rows) {
    if (r.kind !== "field" && r.kind !== "agg") continue;
    const item = findItem(layout.items, r.key);
    if (item) add(r.key, extractTypeRefs(itemTypeText(item)));
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
 * Truth of one directive's own condition under `evaluator`.
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
 *   opts.versionDiff optional diff result (`version_diff.js`) highlighting added/changed rows
 */
export function structRows(layout, data, opts = {}) {
  const editMode = Boolean(opts.editMode);
  const evaluator = opts.evaluator || null;
  const diffStatus = opts.versionDiff?.fieldStatus || null;
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
        if (showGuards) rows.push({ kind: "pp", key: structItemKey(node), depth, ppLevel: level, bars: barsAt(level), text: node.text || `#${node.directive} ${node.expr || ""}`, directive: node.directive, line: node.line, dim: dimmed, dead: eff === F });
      } else member(node, depth, level, eff, dimmed);
    }
  };

  const member = (it, depth, level, eff, dimmed) => {
    const key = structItemKey(it);
    const own = isHidden(key);
    if (own && !editMode) return;
    const dim = dimmed || own;
    const showType = typeVisible(data, key);
    const diff = it.name && diffStatus ? diffStatus.get(it.name) || "" : "";
    const lock = findFieldLock(data, key, it.name);
    const base = { key, depth, bars: barsAt(level), line: it.line || null, conds: it.conds || [], hidden: own, dim, dead: eff === F, showType, diff, lock };
    if (it.kind === "aggregate") {
      const folded = Boolean(data.collapsed?.[key]);
      const typeText = showType && it.type_name ? it.type_name.replace(/^(struct|union)\s+/, "") : "";
      const refType = extractNonStandardType(itemTypeText(it));
      noteType(`${it.tag} ${typeText}`, depth);
      rows.push({ ...base, kind: "agg", tag: it.tag, typeText, label: it.type_name || `${it.tag} {…}`, name: it.name || "(anonymous)", folded, count: flattenMembers(it.children).length, refType });
      if (!folded) walk(it.children || [], depth + 1, level, eff, dim);
    } else {
      if (showType) noteType(it.type, depth);
      const refType = extractNonStandardType(it.type);
      rows.push({ ...base, kind: "field", type: showType ? it.type || "" : "", name: it.name, suffix: `${it.array || ""}${it.bits != null ? ` : ${it.bits}` : ""}`, refType });
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
        key: br.key, blockKey: b.key, opener: i === 0, depth, ppLevel: level, line: br.dir.line, text, directive: br.dir.directive,
        truth: TRUTH_NAME[truth], auto, folded, count, hidden: i === 0 && own, dim, dead: eff === F,
      };
      if (folded) rows.push({ ...head, kind: "summary", bars: barsAt(level, cap) });
      else if (showGuards || (i === 0 && own)) rows.push({ ...head, kind: "pp", bars: barsAt(level, cap) });
      if (!folded) walk(br.items, depth, level + 1, truth, dim);
    });
    if (b.end && showGuards) rows.push({ kind: "pp", key: structItemKey(b.end), blockKey: b.key, closer: true, depth, ppLevel: level, bars: barsAt(level, "end"), text: b.end.text || "#endif", directive: "endif", line: b.end.line, dim, dead: eff === F });
  };

  walk(layout?.items || [], 0, 0, T, false);
  const outerHidden = Boolean(layout?.outer_conds?.length) && isHidden(OUTER_KEY);
  return { rows, tracks, typeCh: Math.max(4, Math.min(48, typeCh + 1)), hidden: hiddenHit, outerHidden };
}

// ------------------------------------------------------------------ pahole layout rows (`style === "layout"`)

/**
 * Display rows for `style === "layout"` from `GET /api/tools/{v}/pahole/{name}` (`pahole`):
 * interleaves 64-byte cache-line boundary markers (`kind: "cacheline"`), padding hole rows
 * (`kind: "hole"`), and member rows (`kind: "field"`) with `offset`, `size`, `cachelineIdx`, and `refType`.
 */
export function paholeRows(pahole, data = {}) {
  const members = Array.isArray(pahole?.members) ? pahole.members : [];
  const rows = [];
  let lastCl = -1;
  let typeCh = 8;
  for (let i = 0; i < members.length; i++) {
    const m = members[i];
    const off = Number(m.offset) || 0;
    const sz = Number(m.size) || 0;
    const pad = Number(m.padding_before) || 0;
    const cl = Number.isInteger(m.cacheline_idx) ? m.cacheline_idx : Math.floor(off / 64);
    if (pad > 0) {
      const holeOff = Math.max(0, off - pad);
      const holeCl = Math.floor(holeOff / 64);
      if (holeCl !== lastCl) {
        lastCl = holeCl;
        rows.push({ kind: "cacheline", cl: holeCl, offset: holeCl * 64, key: `cl:${holeCl}` });
      }
      rows.push({ kind: "hole", offset: holeOff, size: pad, key: `hole:${holeOff}` });
    }
    if (cl !== lastCl) {
      lastCl = cl;
      rows.push({ kind: "cacheline", cl, offset: cl * 64, key: `cl:${cl}` });
    }
    const type = data.showTypes !== false ? String(m.type || "") : "";
    if (type) typeCh = Math.max(typeCh, type.length + 1);
    const key = `po:${off}:${m.name || i}`;
    rows.push({
      kind: "field",
      key,
      offset: off,
      size: sz,
      cachelineIdx: cl,
      crosses: Boolean(m.crosses_cacheline),
      bitfield: m.bitfield_width != null ? `:${m.bitfield_width}` : "",
      type,
      name: String(m.name || "(anon)"),
      estimated: Boolean(m.estimated),
      refType: extractNonStandardType(m.type),
      lock: findFieldLock(data, key, m.name),
    });
  }
  return {
    rows,
    typeCh: Math.max(6, Math.min(40, typeCh)),
    totalSize: Number(pahole?.total_size) || 0,
    alignment: Number(pahole?.alignment) || 0,
    paddingBytes: Number(pahole?.padding_bytes) || 0,
    holesCount: Number(pahole?.holes_count) || 0,
    cacheLines: Number(pahole?.cache_lines_used) || 0,
    isEstimate: Boolean(pahole?.is_estimate),
    optimization: pahole?.optimization || null,
  };
}

// ------------------------------------------------------------------ HTML

const BAR_W = 6; // canvas units per #if track (struct rows are zoom-controlled, not UI-scaled)

function barsHtml(bars, cell = false) {
  const html = bars.map(([l, part]) => `<span class="nm-pp-bar nm-pp-${part} nm-pp-l${l % 4}" style="--nm-pp-level:${l}"></span>`).join("");
  return cell ? `<span class="nm-sf-ppcell" aria-hidden="true">${html}</span>` : html;
}

function hideBtn(r, editMode, key) {
  if (!editMode) return "";
  return `<button type="button" class="nm-sf-hide${r.hidden ? " on" : ""}" data-act="hide-item" data-key="${esc(key)}" title="${r.hidden ? "Restore" : "Hide"} this ${r.kind === "field" ? "member" : r.kind === "agg" ? "aggregate and its members" : "#if block"}">${r.hidden ? "↺" : "✕"}</button>`;
}

function followBtn(r) {
  if (!r.refType) return "";
  return `<button type="button" class="nm-sf-follow" data-act="follow-type" data-key="${esc(r.key)}" title="Follow ${esc(r.refType.kind)} ${esc(r.refType.name)} (${esc(r.refType.edgeLabel)})">↗</button>`;
}

function lockBadgeHtml(lock) {
  if (!lock?.kind) return "";
  const label = formatLockBadge(lock);
  const title = `Protected by ${lock.kind}${lock.lockName ? `: ${lock.lockName}` : ""}`;
  return `<span class="nm-lock-badge nm-lock-${esc(lock.kind)}" title="${esc(title)}">${esc(label)}</span>`;
}

function lockBtn(r, editMode) {
  if (!editMode) return "";
  const has = Boolean(r.lock?.kind);
  return `<button type="button" class="nm-sf-lock${has ? " on" : ""}" data-act="field-lock" data-key="${esc(r.key)}" title="${has ? `Edit lock (${esc(r.lock.kind)}${r.lock.lockName ? `: ${esc(r.lock.lockName)}` : ""})` : "Annotate locking / concurrency"}">🔒</button>`;
}

function rowClass(r, extra) {
  const diffCls = r.diff ? ` nm-sf-diff-${r.diff}` : "";
  return `nm-sf ${extra}${r.dim ? " nm-sf-dim" : ""}${r.hidden ? " nm-sf-hidden" : ""}${r.dead ? " nm-sf-dead" : ""}${diffCls}`;
}

function rowHtml(r, data, editMode, table, cell = false) {
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
    const indent = r.ppLevel ? `<span class="nm-pp-indent" style="--nm-pp-lvl:${r.ppLevel}"></span>` : "";
    return `<div class="${rowClass(r, `nm-sf-pp${r.kind === "summary" ? " nm-sf-ppsum" : ""}${dirClass}${truth}`)}" data-pp-key="${esc(r.key)}" ${style} title="line ${esc(r.line)}${r.truth === "false" ? " · compiled out by the Kconfig engine" : r.truth === "true" ? " · built by the Kconfig engine" : ""}">
    ${barsHtml(r.bars, cell)}${indent}<span class="nm-sf-ctl">${fold}</span><span class="nm-sf-pptext">${esc(r.text)}</span>${summary}${line}${r.opener ? hideBtn(r, editMode, r.blockKey) : ""}
  </div>`;
  }
  const [pl, pr] = ports(r.key);
  const guarded = r.conds.length && !data.showGuards;
  const tt = editMode ? `<button type="button" class="nm-sf-tt${r.showType ? " on" : ""}" data-act="toggle-type" data-key="${esc(r.key)}" title="${r.showType ? "Hide" : "Show"} the type${r.kind === "agg" ? "s inside" : " of"} ${esc(r.kind === "agg" ? r.label : r.name)}">T</button>` : "";
  const marker = guarded ? `<span class="nm-sf-guard" aria-label="${esc(condTitle(r.conds))}">#</span>` : "";
  const fl = followBtn(r);
  const lk = lockBadgeHtml(r.lock);
  const lkBtn = lockBtn(r, editMode);
  if (r.kind === "agg") {
    const fold = `<button type="button" class="nm-sf-fold" data-act="collapse" data-key="${esc(r.key)}" aria-expanded="${r.folded ? "false" : "true"}" title="${r.folded ? "Expand" : "Collapse"} ${esc(r.label)}">${r.folded ? "▸" : "▾"}</button>`;
    const typeCell = table
      ? `<span class="nm-sf-type nm-sf-cell"><span class="nm-sf-tag">${esc(r.tag)}</span>${r.typeText ? ` ${esc(r.typeText)}` : ""}</span>`
      : `<span class="nm-sf-tag">${esc(r.tag)}</span>${r.typeText ? `<span class="nm-sf-type">${esc(r.typeText)}</span>` : ""}`;
    return `<div class="${rowClass(r, `nm-sf-agg${guarded ? " nm-guarded" : ""}`)}" data-key="${esc(r.key)}" ${style} title="${esc(condTitle(r.conds))}">
    ${pl}${barsHtml(r.bars, cell)}<span class="nm-sf-ctl">${fold}${tt}</span>${typeCell}<span class="nm-sf-name${table ? " nm-sf-cell" : ""}">${esc(r.name)}${r.folded ? `<span class="nm-sf-count"> {${r.count}}</span>` : ""}</span>${lk}${lkBtn}${fl}${marker}${line}${hideBtn(r, editMode, r.key)}${pr}
  </div>`;
  }
  const suffix = r.suffix ? `<span class="nm-sf-suffix">${esc(r.suffix)}</span>` : "";
  const typeCell = table
    ? `<span class="nm-sf-type nm-sf-cell">${esc(r.type)}</span>`
    : (r.showType ? `<span class="nm-sf-type">${esc(r.type)}</span>` : "");
  return `<div class="${rowClass(r, `nm-sf-field${guarded ? " nm-guarded" : ""}`)}" data-key="${esc(r.key)}" ${style} title="${esc(condTitle(r.conds))}">
    ${pl}${barsHtml(r.bars, cell)}<span class="nm-sf-ctl">${tt}</span>${typeCell}<span class="nm-sf-name${table ? " nm-sf-cell" : ""}">${esc(r.name)}${suffix}</span>${lk}${lkBtn}${fl}${marker}${line}${hideBtn(r, editMode, r.key)}${pr}
  </div>`;
}

/** Render the 3rd struct window style `layout` (memory offsets, holes, 64-byte cache-line dividers). */
export function renderPaholeBody(pahole, data = {}, opts = {}) {
  if (!pahole || pahole.unavailable) {
    const reason = pahole?.error ? `: ${esc(pahole.error)}` : "";
    return `<div class="nm-status">Memory layout (pahole) unavailable for this revision${reason}. Switch to list or table style to inspect AST fields.</div>`;
  }
  const m = paholeRows(pahole, data);
  const summary = `<div class="nm-layout-summary"><span>${m.totalSize}B</span><span>align ${m.alignment}B</span><span>${m.cacheLines} cache line${m.cacheLines === 1 ? "" : "s"}</span>${m.paddingBytes ? `<span class="nm-layout-pad-badge">${m.paddingBytes}B pad (${m.holesCount} hole${m.holesCount === 1 ? "" : "s"})</span>` : ""}${m.isEstimate ? '<span class="nm-layout-est">est.</span>' : ""}</div>`;
  const rowsHtml = m.rows.map((r) => {
    if (r.kind === "cacheline") {
      return `<div class="nm-sf nm-sf-cl" data-pp-key="${esc(r.key)}"><span class="nm-sf-cl-label">Cache line ${r.cl} · +${r.offset}B</span></div>`;
    }
    if (r.kind === "hole") {
      return `<div class="nm-sf nm-sf-hole" data-pp-key="${esc(r.key)}"><span class="nm-sf-off">+${r.offset}</span><span class="nm-sf-sz">${r.size}B</span><span class="nm-sf-hole-label">/* ${r.size} byte padding hole */</span></div>`;
    }
    const fl = followBtn(r);
    const lk = lockBadgeHtml(r.lock);
    const lkBtn = lockBtn(r, Boolean(opts.editMode));
    return `<div class="nm-sf nm-sf-field nm-sf-layout${r.crosses ? " nm-sf-crosses" : ""}" data-key="${esc(r.key)}">
      <span class="nm-port nm-port-field nm-port-left" data-field="${esc(r.key)}" data-side="left"></span>
      <span class="nm-sf-off">+${r.offset}</span><span class="nm-sf-sz">${r.size}B</span>
      ${r.type ? `<span class="nm-sf-type nm-sf-cell">${esc(r.type)}</span>` : ""}
      <span class="nm-sf-name nm-sf-cell">${esc(r.name)}${r.bitfield ? `<span class="nm-sf-suffix">${esc(r.bitfield)}</span>` : ""}</span>
      ${lk}${lkBtn}${fl}${r.crosses ? '<span class="nm-sf-cross-tag" title="Crosses 64-byte cache-line boundary">⇄CL</span>' : ""}
      <span class="nm-port nm-port-field nm-port-right" data-field="${esc(r.key)}" data-side="right"></span>
    </div>`;
  }).join("");
  const diffHtml = opts.diffHtml || "";
  return `${diffHtml}${summary}<div class="nm-struct-rows nm-scroll nm-style-layout" style="--nm-type-col:${m.typeCh}ch">${rowsHtml || '<div class="nm-empty">No members</div>'}</div>`;
}

function outerHtml(layout, model, editMode) {
  if (!layout.outer_conds?.length || (model.outerHidden && !editMode)) return "";
  const h = model.outerHidden;
  const btn = editMode
    ? `<button type="button" class="nm-sf-hide${h ? " on" : ""}" data-act="hide-item" data-key="${OUTER_KEY}" title="${h ? "Restore" : "Hide"} the conditions around the whole definition">${h ? "↺" : "✕"}</button>`
    : "";
  return `<div class="nm-sf-outer${h ? " nm-sf-dim nm-sf-hidden" : ""}" data-key="${OUTER_KEY}" title="Conditions around the whole definition"><span class="nm-sf-outer-text">${esc(layout.outer_conds.join(" && "))}</span>${btn}</div>`;
}

/**
 * Body HTML of a struct node in `list`, `table`, or `layout` style.
 */
export function renderStructBody(layout, data, opts = {}) {
  if (data.style === "layout") {
    return renderPaholeBody(opts.pahole || layout?.pahole || null, data, opts);
  }
  const editMode = Boolean(opts.editMode);
  const table = data.style === "table";
  const directives = countDirectives(layout.items);
  const model = opts.model || structRows(layout, data, opts);
  const outer = outerHtml(layout, model, editMode);
  const diffBanner = opts.diffHtml || "";
  const chips = editMode
    ? `<button type="button" class="nm-chip${data.showTypes ? " on" : ""}" data-act="all-types" aria-pressed="${data.showTypes}" title="Show or hide every member type">Types</button>
      <button type="button" class="nm-chip${data.showGuards ? " on" : ""}" data-act="guards" aria-pressed="${data.showGuards}" ${directives ? "" : "disabled"} title="${directives ? `Show or hide the ${directives} preprocessor lines` : "No preprocessor lines inside this definition"}">#ifdef${directives ? ` ${directives}` : ""}</button>`
    : "";
  const typeCount = editMode ? collectExpandableTypes(layout, data, opts).length : 0;
  const expandChip = typeCount ? `<button type="button" class="nm-chip" data-act="expand-types" title="Add or connect a node for each of the ${typeCount} non-standard types the visible members use">⇶ Types ${typeCount}</button>` : "";
  const hiddenNote = editMode && model.hidden ? `<span class="nm-chip nm-chip-note" title="Hidden items show dimmed in Edit Mode">${model.hidden} hidden</span>` : "";
  const cell = table && model.tracks > 0;
  const rowsHtml = model.rows.map((r) => rowHtml(r, data, editMode, table, cell)).join("");
  const vars = `--nm-pp-tracks:${model.tracks};--nm-gutter:${model.tracks ? model.tracks * BAR_W + BAR_W : 0}px;--nm-type-col:${model.typeCh}ch`;
  const bar = editMode ? `<div class="nm-struct-bar">${chips}${expandChip}${hiddenNote}</div>` : "";
  return `${bar}${diffBanner}${outer}
    <div class="nm-struct-rows nm-scroll nm-style-${table ? "table" : "list"}${cell ? " nm-pp-cells" : ""}" style="${vars}">${rowsHtml || '<div class="nm-empty">No members</div>'}</div>`;
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
