/**
 * file_references.js - "Referenced by" of a file (GET /api/fs/references, webapp/API.md §4.2): the menu
 * groups and labels, pure (webapp/frontend/tests/file_references.test.mjs). The context menu escapes every
 * label it renders (§5.6).
 */
export const REFERENCE_GROUPS = [
  { key: "included_by", title: "Included by", icon: "#" },
  { key: "sourced_by", title: "Sourced by", icon: "⚙" },
  { key: "built_by", title: "Built by", icon: "🔨" },
  { key: "documented_in", title: "Documented in", icon: "📖" },
];

/** Total references of a response (for the toolbar badge). */
export function referenceCount(res) {
  const totals = res?.totals || {};
  return REFERENCE_GROUPS.reduce((n, g) => n + (Number(totals[g.key]) || (res?.[g.key] || []).length || 0), 0);
}

/** [{key, title, icon, total, items: [{path, line, label, title}]}] of the non-empty groups, in display order. */
export function referenceGroups(res) {
  const out = [];
  for (const g of REFERENCE_GROUPS) {
    const list = Array.isArray(res?.[g.key]) ? res[g.key] : [];
    if (!list.length) continue;
    const total = Number(res?.totals?.[g.key]) || list.length;
    out.push({
      ...g,
      total,
      items: list.map((it) => {
        const where = it.line ? `${it.path}:${it.line}` : String(it.path || "");
        let title = where;
        if (g.key === "built_by") title = `${it.rule || ""}${it.symbols?.length ? ` (CONFIG_${it.symbols.join(", CONFIG_")})` : ""}`.trim() || where;
        else if (g.key === "documented_in" && it.details) title = String(it.details);
        return { path: String(it.path || ""), line: Number(it.line) || 1, label: where, title };
      }),
    });
  }
  return out;
}

/**
 * Context menu items of a response: a disabled heading per group ("Included by (N)", "+M more" when the
 * list was capped), then one entry per reference; `open(path, line, newTab)` opens it.
 */
export function referenceMenuItems(res, open) {
  const groups = referenceGroups(res);
  if (!groups.length) {
    return [{ label: res?.indexed === false ? "This version of the file is not indexed" : "No file refers to this one", disabled: true }];
  }
  const items = [];
  groups.forEach((g, i) => {
    if (i) items.push({ separator: true });
    const more = g.total > g.items.length ? ` · showing ${g.items.length}` : "";
    items.push({ label: `${g.title} (${g.total})${more}`, icon: g.icon, disabled: true });
    for (const it of g.items) {
      items.push({
        label: it.label,
        title: `${it.title} (middle-click: new tab)`,
        onClick: () => open(it.path, it.line, false),
        onMiddleClick: () => open(it.path, it.line, true),
      });
    }
  });
  return items;
}
