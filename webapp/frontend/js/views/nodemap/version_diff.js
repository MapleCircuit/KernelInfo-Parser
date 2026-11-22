/**
 * version_diff.js - Canvas-wide version time-travel diffing (`webapp/API_FRONTEND.md` §5.3).
 *
 * Compares struct layouts between the node's current revision and a target revision (`vB`),
 * computes added/removed members, type changes, and `#ifdef` condition changes, renders inline
 * diff badges, and applies/reverts version updates across the canvas or a group.
 */
import { groupDescendants } from "./canvas_model.js";
import { esc, flattenMembers } from "./struct_view.js";

function condKey(conds) {
  return Array.isArray(conds) ? conds.join(" && ") : "";
}

function memberKey(m, idx) {
  return m?.name ? `${m.kind}:${m.name}` : `${m?.kind || "field"}:#anon-${idx}`;
}

/**
 * Compare two struct layouts (`layoutA` = base, `layoutB` = target):
 * returns `{ addedFields, removedFields, changedTypes, changedGuards, unchangedCount,
 *   hasChanges, summaryText, fieldStatus }`.
 */
export function diffStructLayouts(layoutA, layoutB) {
  const membersA = flattenMembers(layoutA?.items || []);
  const membersB = flattenMembers(layoutB?.items || []);
  const mapA = new Map();
  const mapB = new Map();
  membersA.forEach((m, i) => mapA.set(memberKey(m, i), m));
  membersB.forEach((m, i) => mapB.set(memberKey(m, i), m));

  const addedFields = [];
  const removedFields = [];
  const changedTypes = [];
  const changedGuards = [];
  const fieldStatus = new Map();
  let unchangedCount = 0;

  for (const [k, mb] of mapB) {
    const ma = mapA.get(k);
    const label = mb.name || mb.type_name || "(anon)";
    if (!ma) {
      addedFields.push(label);
      if (mb.name) fieldStatus.set(mb.name, "added");
      continue;
    }
    const ta = String(ma.type || ma.type_name || ma.tag || "");
    const tb = String(mb.type || mb.type_name || mb.tag || "");
    const ca = condKey(ma.conds);
    const cb = condKey(mb.conds);
    let modified = false;
    if (ta !== tb) {
      changedTypes.push({ name: label, from: ta, to: tb });
      if (mb.name) fieldStatus.set(mb.name, "type");
      modified = true;
    }
    if (ca !== cb) {
      changedGuards.push({ name: label, from: ca || "(always)", to: cb || "(always)" });
      if (mb.name && !fieldStatus.has(mb.name)) fieldStatus.set(mb.name, "guard");
      modified = true;
    }
    if (!modified) unchangedCount += 1;
  }

  for (const [k, ma] of mapA) {
    if (!mapB.has(k)) {
      const label = ma.name || ma.type_name || "(anon)";
      removedFields.push(label);
      if (ma.name && !fieldStatus.has(ma.name)) fieldStatus.set(ma.name, "removed");
    }
  }

  const parts = [];
  if (addedFields.length) parts.push(`+${addedFields.length}`);
  if (removedFields.length) parts.push(`-${removedFields.length}`);
  if (changedTypes.length) parts.push(`~${changedTypes.length} type`);
  if (changedGuards.length) parts.push(`#${changedGuards.length} ifdef`);
  const hasChanges = parts.length > 0;
  const summaryText = hasChanges ? parts.join(" · ") : "unchanged";

  return {
    addedFields,
    removedFields,
    changedTypes,
    changedGuards,
    unchangedCount,
    hasChanges,
    summaryText,
    fieldStatus,
  };
}

/** Compact HTML banner drawn at the top of a struct node during Version Time-Travel comparison. */
export function renderStructDiffBadge(diff, versionA, versionB) {
  if (!diff) return "";
  if (!diff.hasChanges) {
    return `<div class="nm-ver-diff-badge nm-ver-diff-same" title="Identical between ${esc(versionA)} and ${esc(versionB)}">${esc(versionA)} → ${esc(versionB)}: unchanged</div>`;
  }
  const chips = [
    ...diff.addedFields.slice(0, 6).map((f) => `<span class="nm-vd-chip nm-vd-add">+${esc(f)}</span>`),
    ...diff.removedFields.slice(0, 6).map((f) => `<span class="nm-vd-chip nm-vd-rem">-${esc(f)}</span>`),
    ...diff.changedTypes.slice(0, 4).map((c) => `<span class="nm-vd-chip nm-vd-chg" title="${esc(c.from)} → ${esc(c.to)}">~${esc(c.name)}</span>`),
    ...diff.changedGuards.slice(0, 4).map((c) => `<span class="nm-vd-chip nm-vd-grd" title="${esc(c.from)} → ${esc(c.to)}">#${esc(c.name)}</span>`),
  ].join("");
  return `<div class="nm-ver-diff-badge"><span class="nm-vd-head">${esc(versionA)} → ${esc(versionB)}: ${esc(diff.summaryText)}</span><div class="nm-vd-chips">${chips}</div></div>`;
}

const VERSIONED_TYPES = new Set(["struct", "symbol", "source", "kconfig", "maintainer", "kapi", "doc", "history"]);

/** Switch `node.version` across the canvas (or within `groupId`) to `targetVersion`. */
export function applyCanvasVersion(nodes, targetVersion, { groupId = null } = {}) {
  const ver = String(targetVersion || "").trim();
  if (!ver) return { changedIds: [], previousVersions: {} };
  const scope = groupId
    ? new Set(groupDescendants(nodes, groupId).map((n) => n.id))
    : null;
  const changedIds = [];
  const previousVersions = {};
  for (const n of nodes || []) {
    if (scope && !scope.has(n.id)) continue;
    if (!VERSIONED_TYPES.has(n.type)) continue;
    if (n.version !== ver) {
      previousVersions[n.id] = n.version;
      n.version = ver;
      changedIds.push(n.id);
    }
  }
  return { changedIds, previousVersions };
}

/** Restore node revisions from `previousVersions` map (`nodeId -> version`). */
export function revertCanvasVersion(nodes, previousVersions = {}) {
  const revertedIds = [];
  for (const n of nodes || []) {
    if (Object.prototype.hasOwnProperty.call(previousVersions, n.id)) {
      n.version = previousVersions[n.id];
      revertedIds.push(n.id);
    }
  }
  return revertedIds;
}

/**
 * Node types whose content follows a revision when one is applied to selected nodes (the header's
 * Apply, the multi-selection Revision field): the canvas-wide set plus `cve` and `diff`. A `diff` in
 * file mode moves its "To" side (`versionB`), a `cve` node its release check (`release`, with the
 * revision of its file CVEs); a `tab` node moves the `v=` of its embedded address.
 * `note`, `group`, `man` and `mail` nodes have no revision.
 */
export const SELECTION_VERSIONED_TYPES = new Set([...VERSIONED_TYPES, "cve", "diff"]);

/** Revision `node` shows (what applying a revision changes), or null when it has none. */
export function revisionOf(node) {
  if (!node) return null;
  if (node.type === "diff" && node.data?.mode !== "commit") return String(node.data?.versionB || node.version || "");
  if (node.type === "cve") return String(node.data?.release || node.version || "");
  if (node.type === "tab") {
    const p = new URLSearchParams(String(node.data?.hash || ""));
    return p.has("v") ? p.get("v") : null;
  }
  return SELECTION_VERSIONED_TYPES.has(node.type) ? String(node.version || "") : null;
}

/**
 * Move `nodes` to revision `targetVersion`: `{ changedIds, unchanged, skipped, otherRepo }` (already
 * on it / without a revision / of another repository than `repo`, whose refs the target may not name).
 * Mutates the nodes; the caller invalidates and commits them as one step.
 */
export function retargetNodeVersions(nodes, targetVersion, { repo = null } = {}) {
  const ver = String(targetVersion || "").trim();
  const out = { changedIds: [], unchanged: 0, skipped: 0, otherRepo: 0 };
  if (!ver) return out;
  for (const n of nodes || []) {
    if (repo !== null && n.repo && n.repo !== repo && revisionOf(n) !== null) {
      out.otherRepo += 1;
      continue;
    }
    const cur = revisionOf(n);
    if (cur === null) {
      out.skipped += 1;
      continue;
    }
    if (cur === ver) {
      out.unchanged += 1;
      continue;
    }
    if (n.type === "tab") {
      const p = new URLSearchParams(String(n.data.hash || ""));
      p.set("v", ver);
      n.data = { ...n.data, hash: p.toString() };
    } else if (n.type === "diff" && n.data?.mode !== "commit") {
      n.data = { ...n.data, versionB: ver };
    } else if (n.type === "cve") {
      n.data = { ...n.data, release: ver };
    }
    n.version = ver;
    out.changedIds.push(n.id);
  }
  return out;
}
