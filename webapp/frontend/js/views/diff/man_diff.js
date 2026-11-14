/**
 * man_diff.js - "Man pages" mode helpers of the Compare view (webapp/API_FRONTEND.md §5.11):
 * filtering, section grouping, delta badges and exports for GET /api/man/diff. DOM-free.
 */
import { manLabel, sectionRank, sectionTitle } from "../man/man_model.js";

export const MAN_STATUSES = ["added", "modified", "deleted"];
export const MAN_STATUS_LABELS = { added: "A", modified: "M", deleted: "D" };
export const MAN_DOWNLOAD_FORMATS = [
  { key: "txt", label: "Entries (.txt)", title: "One name(section) entry per line" },
  { key: "md", label: "Summary (.md)", title: "Markdown release summary grouped by added, modified and deleted" },
  { key: "tsv", label: "Table (.tsv)", title: "Status, name, section, path, old path, summary and changes per entry" },
  { key: "json", label: "JSON (.json)", title: "Revisions, filters, summary and every matching entry" },
];

/** Distinct sections in `entries`, sorted by man(1) search order. */
export function manDiffSections(entries) {
  const counts = new Map();
  for (const e of entries || []) {
    const sec = String(e?.section || "");
    if (!sec) continue;
    counts.set(sec, (counts.get(sec) || 0) + 1);
  }
  return [...counts.entries()]
    .sort(([a], [b]) => sectionRank(a) - sectionRank(b) || a.localeCompare(b))
    .map(([section, count]) => ({ section, title: sectionTitle(section), count }));
}

/** Filter man diff entries by status chips, optional section, and multi-word query. */
export function filterManEntries(entries, statuses, section = "", query = "") {
  const words = String(query || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  const sec = String(section || "");
  return (entries || []).filter((e) => {
    if (statuses?.size > 0 && !statuses.has(e.status)) return false;
    if (sec && e.section !== sec) return false;
    if (words.length) {
      const hay = [
        e.name,
        manLabel(e.name, e.section),
        e.summary,
        e.summary_a,
        e.summary_b,
        e.path,
        e.path_a,
        e.path_b,
        e.so,
        e.so_a,
        e.so_b,
        ...(e.fn_added || []),
        ...(e.fn_removed || []),
        ...(e.aliases_added || []),
        ...(e.aliases_removed || []),
      ].filter(Boolean).join(" ").toLowerCase();
      if (!words.every((w) => hay.includes(w))) return false;
    }
    return true;
  });
}

function deltaText(added, removed, unit) {
  if (added && removed) return `+${added} / −${removed} ${unit}`;
  return added ? `+${added} ${unit}` : `−${removed} ${unit}`;
}

/** Structured change badges for one man diff entry. */
export function manDiffBadges(entry) {
  const badges = [];
  if (!entry) return badges;
  if (entry.status !== "modified") {
    if (entry.so) badges.push({ kind: "so", text: `→ ${entry.so}`, title: `Alias of ${entry.so}` });
    return badges;
  }
  const fnAdd = entry.fn_added || [];
  const fnRem = entry.fn_removed || [];
  if (fnAdd.length || fnRem.length) {
    const parts = [];
    if (fnAdd.length) parts.push(`+${fnAdd.join(", +")}`);
    if (fnRem.length) parts.push(`−${fnRem.join(", −")}`);
    badges.push({ kind: "fn", text: deltaText(fnAdd.length, fnRem.length, "fn"), title: `SYNOPSIS functions: ${parts.join("; ")}` });
  }
  const alAdd = entry.aliases_added || [];
  const alRem = entry.aliases_removed || [];
  if (alAdd.length || alRem.length) {
    const parts = [];
    if (alAdd.length) parts.push(`+${alAdd.join(", +")}`);
    if (alRem.length) parts.push(`−${alRem.join(", −")}`);
    badges.push({ kind: "alias", text: deltaText(alAdd.length, alRem.length, "alias"), title: `NAME aliases: ${parts.join("; ")}` });
  }
  if (entry.so_changed) {
    if (entry.so_b && !entry.so_a) {
      badges.push({ kind: "so", text: `→ ${entry.so_b}`, title: `Converted to alias of ${entry.so_b}` });
    } else if (!entry.so_b && entry.so_a) {
      badges.push({ kind: "so", text: "alias → page", title: `Previously an alias of ${entry.so_a}` });
    } else {
      badges.push({ kind: "so", text: `→ ${entry.so_b}`, title: `Alias target changed: ${entry.so_a} → ${entry.so_b}` });
    }
  } else if (entry.so) {
    badges.push({ kind: "so", text: `→ ${entry.so}`, title: `Alias of ${entry.so}` });
  }
  if (entry.summary_changed) {
    badges.push({
      kind: "summary",
      text: "summary",
      title: `Summary changed\nBefore: ${entry.summary_a || "(none)"}\nAfter: ${entry.summary_b || "(none)"}`,
    });
  }
  if (entry.path_a && entry.path_b && entry.path_a !== entry.path_b) {
    badges.push({ kind: "moved", text: `${entry.path_a} →`, title: `Moved from ${entry.path_a} to ${entry.path_b}` });
  }
  return badges;
}

/** Markdown summary for release notes. */
export function manDiffMarkdown(result, entries) {
  const a = result?.a || "";
  const b = result?.b || "";
  const s = result?.summary || {};
  const lines = [
    `# Man pages changes ${a} → ${b}`,
    "",
    `Added: ${s.added ?? 0} · Modified: ${s.modified ?? 0} · Deleted: ${s.deleted ?? 0} · Unchanged: ${s.unchanged ?? 0}`,
    "",
  ];
  const titles = { added: "Added", modified: "Modified", deleted: "Deleted" };
  for (const status of MAN_STATUSES) {
    const group = (entries || []).filter((e) => e.status === status);
    if (!group.length) continue;
    lines.push(`## ${titles[status]} (${group.length})`, "");
    for (const e of group) {
      const badges = manDiffBadges(e).map((bg) => bg.text).join(", ");
      const tail = [e.summary ? e.summary : "", badges ? `[${badges}]` : ""].filter(Boolean).join(" ");
      lines.push(`- \`${manLabel(e.name, e.section)}\`${tail ? ` — ${tail}` : ""}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

/** Tab-separated export of filtered man diff entries. */
export function manDiffTsv(entries) {
  const c = (v) => String(v ?? "").replace(/[\t\r\n]+/g, " ");
  const header = ["status", "name", "section", "path", "old_path", "summary", "changes"].join("\t");
  const rows = (entries || []).map((e) => {
    const path = e.path_b || e.path_a || e.path || "";
    const oldPath = e.status === "modified" && e.path_a && e.path_b && e.path_a !== e.path_b ? e.path_a : "";
    const changes = manDiffBadges(e).map((bg) => bg.text).join("; ");
    return [MAN_STATUS_LABELS[e.status] || "?", e.name, e.section, path, oldPath, e.summary || "", changes].map(c).join("\t");
  });
  return [header, ...rows].join("\n") + "\n";
}
