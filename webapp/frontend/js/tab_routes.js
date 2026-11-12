/**
 * tab_routes.js - Registry of tab types added after the core ones (webapp/API.md §5.26).
 *
 * A tab type registers how it is addressed, so the deep-link code (url_sync.js), tab reuse
 * (state.openTab) and the tab strip (tabs.js) handle it without a case of their own:
 *
 *   registerTabRoute("search", {
 *     badge: "[F]",                         // tab strip badge
 *     keys: ["query", "regex", ...],        // tab fields that address what it shows (kept in history snapshots,
 *                                           //   copied onto a reused tab; "" / 0 / null clear them)
 *     fromHash(params, version) -> tabData, // a #tab=<type>&... link -> state.openTab() argument
 *     toHash(tab, params),                  // the tab -> hash parameters (leave out empty/default values)
 *     navKey(tab) -> string,                // what changes push a history entry (others replace it)
 *     sameTab(existing, tabData) -> bool,   // reuse an open tab (default: any tab of that type)
 *   });
 *
 * DOM-free and dependency-free (imported by state.js).
 */
const ROUTES = new Map();

export function registerTabRoute(type, route) {
  ROUTES.set(type, { keys: [], ...route });
}

export function tabRoute(type) {
  return ROUTES.get(type) || null;
}

/** Every key registered routes keep on their tabs (for history snapshots). */
export function routeKeys() {
  const out = new Set();
  for (const r of ROUTES.values()) for (const k of r.keys || []) out.add(k);
  return [...out];
}

/** Helpers for hash parameters. */
export const hashBool = (params, key) => params.get(key) === "1";
export function setIf(params, key, value) {
  if (value === undefined || value === null || value === "" || value === false || value === 0) return;
  params.set(key, value === true ? "1" : String(value));
}
export function appendAll(params, key, values) {
  for (const v of values || []) if (v !== "" && v != null) params.append(key, String(v));
}

/**
 * Repository a hash addresses (`repo=<configured repository name>`, §5.26), "" when it names none.
 * Shareable links carry it so a link into another project opens there.
 */
export function hashRepo(params) {
  const r = (params && typeof params.get === "function" ? params.get("repo") : "") || "";
  return /^[\w.@+-]{1,128}$/.test(r) ? r : "";
}
