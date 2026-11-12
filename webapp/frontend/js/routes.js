/**
 * routes.js - Deep-link routes of the tab types added by the feature plans (webapp/API.md §5.26).
 *
 * Each `registerTabRoute()` says how a tab type is addressed in the URL hash, which of its fields
 * are navigation (history entries) and when an open tab is reused (tab_routes.js). Kept apart from
 * the views (no DOM, no network) so the hash mapping is unit tested (webapp/frontend/tests/routes.test.mjs)
 * and registered before url_sync.js reads the first address.
 */
import { appendAll, hashBool, registerTabRoute, setIf } from "./tab_routes.js";
import { sectionParamsFromState, sectionStateFromParams } from "./views/sections/section_model.js";

// ---------------------------------------------------------------- full-text search (§5.34)
export const SEARCH_LIMITS = [500, 2000, 10000];

registerTabRoute("search", {
  badge: "[F]",
  keys: ["query", "regex", "caseSensitive", "word", "paths", "limit"],
  fromHash: (params) => ({
    title: params.get("q") ? `Search: ${params.get("q").slice(0, 24)}` : "Search",
    query: params.get("q") || "",
    regex: hashBool(params, "re"),
    caseSensitive: hashBool(params, "cs"),
    word: hashBool(params, "w"),
    paths: params.getAll("p").slice(0, 8),
    limit: SEARCH_LIMITS.includes(Number(params.get("n"))) ? Number(params.get("n")) : 0,
  }),
  toHash: (tab, params) => {
    setIf(params, "q", tab.query);
    setIf(params, "re", tab.regex);
    setIf(params, "cs", tab.caseSensitive);
    setIf(params, "w", tab.word);
    appendAll(params, "p", tab.paths);
    if (tab.limit && tab.limit !== 2000) params.set("n", String(tab.limit));
  },
  // The search is the tab's target: a new query or option is a navigation (history entry)
  navKey: (tab) => JSON.stringify([tab.query || "", !!tab.regex, !!tab.caseSensitive, !!tab.word, tab.paths || [], tab.limit || 0]),
});

// ---------------------------------------------------------------- watch list (§5.36)
registerTabRoute("watch", {
  badge: "[Y]",
  keys: [],
  fromHash: () => ({ title: "Watch list" }),
  toHash: () => {},
  navKey: () => "",
});

// ---------------------------------------------------------------- bookmarks & tours (§5.35); t=<tour> imports a shared tour once
registerTabRoute("bookmarks", {
  badge: "[B]",
  keys: [],
  fromHash: (params) => ({ title: "Bookmarks", importTour: params.get("t") || "" }),
  toHash: () => {},
  navKey: () => "",
});

// ---------------------------------------------------------------- patch series (§5.33); the draft is session state, not URL state
registerTabRoute("patch", {
  badge: "[W]",
  keys: [],
  fromHash: () => ({ title: "Patch series" }),
  toHash: () => {},
  navKey: () => "",
});

// ---------------------------------------------------------------- parser status (§5.37)
registerTabRoute("status", {
  badge: "[I]",
  keys: [],
  fromHash: () => ({ title: "Parser status" }),
  toHash: () => {},
  navKey: () => "",
});

// ---------------------------------------------------------------- Kernel API and Insights tabs (§5.29, §5.30)
const SECTION_KEYS = ["view", "query", "filter", "sel", "arch", "release", "window", "listCollapsed"];
for (const [type, title, badge] of [["api", "Kernel API", "[A]"], ["insights", "Insights", "[N]"]]) {
  registerTabRoute(type, {
    badge,
    keys: SECTION_KEYS,
    fromHash: (params) => {
      const st = sectionStateFromParams(params);
      return { title: `${title}${st.view ? ` · ${st.view}` : ""}`, ...st };
    },
    toHash: (tab, params) => sectionParamsFromState(tab, params),
    // The section, the selection, the release and the ARCH are navigation; the filter and chips replace the entry
    navKey: (tab) => `${tab.view || ""}:${tab.sel || ""}:${tab.release || ""}:${tab.arch || ""}:${tab.window || ""}`,
  });
}

// ---------------------------------------------------------------- man pages (§5.38): page/sec/mode/kv/krepo/path/q
// A man tab belongs to the man-pages repository (`repo=`) at one of its tags (`v=`), like any tab;
// kv/krepo name the kernel revision the reader came from. Addresses written before tabs carried their
// repository named them `mrepo` / `mv`: they still open the man-pages tab they address.
export const MAN_MODES = ["web", "term"];
const MAN_HASH = [["manPage", "page"], ["manSec", "sec"], ["manMode", "mode"],
  ["kernelVersion", "kv"], ["kernelRepo", "krepo"], ["manPath", "path"], ["manQuery", "q"]];
registerTabRoute("man", {
  badge: "[H]",
  keys: MAN_HASH.map(([k]) => k),
  fromHash: (params) => {
    const tab = Object.fromEntries(MAN_HASH.map(([k, h]) => [k, params.get(h) || ""]));
    if (!MAN_MODES.includes(tab.manMode)) tab.manMode = "";
    const legacyRepo = params.get("mrepo") || "";
    if (legacyRepo && /^[\w.@+-]{1,128}$/.test(legacyRepo)) {
      tab.repo = legacyRepo;
      tab.version = params.get("mv") || "";
    }
    const page = tab.manPage || (tab.manPath ? tab.manPath.split("/").pop().replace(/\.\d[a-z]*$/, "") : "");
    return { title: page ? `man ${page}${tab.manSec ? `(${tab.manSec})` : ""}` : "Man pages", ...tab };
  },
  toHash: (tab, params) => {
    for (const [k, h] of MAN_HASH) setIf(params, h, tab[k]);
  },
  // The page is navigation (the repository and tag are part of every tab's key); the display mode and the
  // index filter replace the entry
  navKey: (tab) => `${tab.manPath || ""}:${tab.manPage || ""}:${tab.manSec || ""}`,
});
