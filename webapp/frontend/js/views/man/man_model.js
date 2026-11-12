/**
 * man_model.js - Pure helpers of the man pages tab (webapp/API_FRONTEND.md §5.38): page paths,
 * section order and titles, the index grouping and filter, and the deep links into the linux repo.
 * DOM-free (unit tested by webapp/frontend/tests/man_render.test.mjs).
 */

/** A man page file of the man-pages repository: `man2/read.2`, `man/man2const/EPOLL_CTL_ADD.2const`. */
export const MAN_PATH_RE = /^(man\/)?man\d[a-z]*\/[^/]+\.\d[a-z]*$/;

export function isManPagePath(path) {
  return MAN_PATH_RE.test(String(path || ""));
}

/** (name, section) of a page path, or null. */
export function pageOfPath(path) {
  const m = /^(?:man\/)?man(\d[a-z]*)\/([^/]+)\.(\d[a-z]*)$/.exec(String(path || ""));
  return m && m[1] === m[3] ? { name: m[2], section: m[3] } : null;
}

/** Search order of a bare name (like man(1)); suffixed sections follow their base section. */
export const SECTION_ORDER = ["2", "3", "1", "8", "5", "4", "7", "6", "9"];

export function sectionRank(section) {
  const s = String(section || "");
  const base = SECTION_ORDER.indexOf(s.charAt(0));
  return (base === -1 ? SECTION_ORDER.length : base) * 100 + (s.length > 1 ? 1 + (s.charCodeAt(1) % 97) : 0);
}

const SECTION_TITLES = {
  1: "User commands", 2: "System calls", 3: "Library functions", 4: "Special files and devices",
  5: "File formats and configuration files", 6: "Games", 7: "Overviews, conventions and miscellany",
  8: "System administration", 9: "Kernel routines",
};
const SUFFIX_TITLES = { const: "constants", type: "types", head: "headers", attr: "attributes", p: "POSIX" };

export function sectionTitle(section) {
  const s = String(section || "");
  const base = SECTION_TITLES[s.charAt(0)] || `Section ${s}`;
  const suffix = s.slice(1);
  if (!suffix) return base;
  return `${base} — ${SUFFIX_TITLES[suffix] || suffix}`;
}

/** "read(2)" */
export const manLabel = (name, section) => `${name || "?"}${section ? `(${section})` : ""}`;

/** `name(section)` of a reference text such as an rst `:manpage:` target; null when it is not one. */
export function parseManRef(text) {
  const m = /^\s*([\w.+:@-]+)\s*\((\d[a-z]*)\)\s*$/.exec(String(text || ""));
  return m ? { name: m[1], section: m[2] } : null;
}

/** man7.org address of a page (the fallback when the man-pages repository has no copy of it). */
export function man7Url(name, section) {
  const s = String(section || "1");
  return `https://man7.org/linux/man-pages/man${encodeURIComponent(s.charAt(0))}/${encodeURIComponent(name)}.${encodeURIComponent(s)}.html`;
}

/**
 * Index sections of `/api/man/{v}/index` pages: [{section, title, pages}] in section order, each list
 * sorted by name, keeping the pages whose name or summary hold every word of `filter`.
 */
export function groupIndex(pages, filter = "") {
  const words = String(filter || "").toLowerCase().split(/\s+/).filter(Boolean);
  const groups = new Map();
  for (const p of Array.isArray(pages) ? pages : []) {
    if (!p || !p.name) continue;
    if (words.length) {
      const hay = `${p.name} ${p.summary || ""} ${p.name}(${p.section || ""})`.toLowerCase();
      if (!words.every((w) => hay.includes(w))) continue;
    }
    const sec = String(p.section || "");
    if (!groups.has(sec)) groups.set(sec, []);
    groups.get(sec).push(p);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => sectionRank(a) - sectionRank(b) || a.localeCompare(b))
    .map(([section, list]) => ({ section, title: sectionTitle(section), pages: list.sort((a, b) => String(a.name).localeCompare(String(b.name))) }));
}

/** Natural comparison of tag names ("man-pages-6.10" > "man-pages-6.9.1" > "man-pages-6.9"). */
export function compareTags(a, b) {
  const pa = String(a || "").split(/(\d+)/);
  const pb = String(b || "").split(/(\d+)/);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? "";
    const y = pb[i] ?? "";
    if (x === y) continue;
    if (/^\d+$/.test(x) && /^\d+$/.test(y)) return parseInt(x, 10) - parseInt(y, 10);
    return x < y ? -1 : 1;
  }
  return 0;
}

/** Newest of tag names (natural order), "" for none. */
export function newestTag(names) {
  let best = "";
  for (const n of names || []) if (n && (!best || compareTags(n, best) > 0)) best = n;
  return best;
}

/** Tab data (state.openTab) of the Kernel API syscalls section on `name` in a linux repository at `version`. */
export function linuxApiTab({ repo = "", version = "", name = "" }) {
  // Every field the link addresses is given: a reused Kernel API tab drops its filter and chips
  return { type: "api", title: "Kernel API · syscalls", view: "syscalls", arch: "x86_64", sel: name, query: "", filter: "", repo, version };
}

/** Tab data of a whole-word, case-sensitive search for a symbol in a linux repository at `version`. */
export function linuxSymbolTab({ repo = "", version = "", symbol = "" }) {
  return { type: "search", title: `Search: ${symbol.slice(0, 24)}`, query: symbol, word: true, caseSensitive: true, regex: false, repo, version };
}

/**
 * Tab fields of a man tab showing one page or the index: the man-pages repository and tag are the tab's own
 * `repo` / `version` (every tab's, §5.18), the rest are routes.js keys.
 */
export function manTabFields({ repo = "", version = "", name = "", section = "", path = "", mode = "", kernel = "", kernelRepo = "" } = {}) {
  return {
    repo, version, manPage: name, manSec: section, manPath: path, manMode: mode,
    kernelVersion: kernel, kernelRepo,
  };
}

export const manTabTitle = (tab) => (tab.manPage || tab.manPath
  ? `man ${manLabel(tab.manPage || pageOfPath(tab.manPath)?.name || tab.manPath.split("/").pop(), tab.manSec || pageOfPath(tab.manPath)?.section || "")}`
  : "Man pages");
