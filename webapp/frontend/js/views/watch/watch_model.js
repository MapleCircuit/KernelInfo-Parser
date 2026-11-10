/**
 * watch_model.js - Watch list logic (G10, webapp/API.md §5.36): targets, which watches an update concerns,
 * how checks are grouped, and the unread bookkeeping. DOM-free, tested.
 *
 * watch: {target: {type: "file"|"dir"|"symbol"|"kconfig"|"section", path|name, arch?}, branch, base_sha}
 * watch-state: {watch_key, last_sha, last_checked, status, summary, changed_since, unread, result}
 */
const IDENT = /^(CONFIG_)?[A-Za-z_]\w{0,127}$/;
const PATH_OK = (p) => typeof p === "string" && p.length > 0 && p.length <= 1024 && !p.startsWith("/") && !p.split("/").includes("..");

export function validWatchTarget(t) {
  if (!t) return false;
  if (t.type === "file" || t.type === "dir") return PATH_OK(t.path);
  if (t.type === "symbol" || t.type === "kconfig") return IDENT.test(t.name || "");
  if (t.type === "section") return typeof t.name === "string" && t.name.trim().length > 0 && t.name.length <= 200;
  return false;
}

export function watchLabel(t) {
  switch (t?.type) {
    case "file": return t.path;
    case "dir": return `${t.path}/`;
    case "symbol": return `${t.name}()`;
    case "kconfig": return `CONFIG_${String(t.name).replace(/^CONFIG_/, "")}`;
    case "section": return `MAINTAINERS: ${t.name}`;
    default: return "?";
  }
}

export const sameWatch = (a, b) => a?.type === b?.type && watchLabel(a) === watchLabel(b);

/** Watches an update of these refs concerns (their branch moved or appeared). */
export function dueWatches(watches, { changed = [], added = [] } = {}) {
  const moved = new Set([...changed, ...added]);
  return watches.filter((w) => moved.has(w.data.branch));
}

/** Watches grouped by the check they need: one `POST /api/watch/changes` per (from, to). */
export function checkGroups(watches, states) {
  const groups = new Map();
  for (const w of watches) {
    const from = states.get(w.key)?.last_sha || w.data.base_sha;
    if (!from) continue;
    const id = `${from}|${w.data.branch}`;
    if (!groups.has(id)) groups.set(id, { from, to: w.data.branch, watches: [] });
    groups.get(id).watches.push(w);
  }
  return [...groups.values()];
}

/** The next watch-state from a check result: a change stays unread until marked read. */
export function nextState(prev, item, toSha, now = Date.now()) {
  if (item.error) return { ...(prev || {}), last_checked: now, error: item.error };
  const unread = Boolean(item.changed) || Boolean(prev?.unread);
  return {
    last_sha: toSha, last_checked: now, status: item.status, summary: item.summary, error: "",
    changed_since: item.changed ? prev?.last_sha || "" : prev?.changed_since || "",
    unread, result: item.changed ? item : prev?.result || null,
  };
}

export const unreadCount = (states) => [...states.values()].filter((s) => s?.unread).length;
