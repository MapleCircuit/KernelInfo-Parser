/**
 * nodemap_bridge.js - "Send to NodeMap" from other views (`webapp/API_FRONTEND.md` §5.3.5).
 *
 * `nodeSpecsForTab()` (pure) turns a tab into the node that shows it natively (code tab → source node,
 * man tab → man page node, pahole tab → struct node) or, for every other tab, a live tab node embedding
 * the tab's address. `sendToNodemap()` queues specs on an open NodeMap tab of the same project (or opens
 * one) as `pendingNodes`; the canvas adds them next to its viewport center and announces nothing else.
 */
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";

/** Tab types a tab node does not embed (the canvas itself; versionless chrome views stay tabs). */
export const NON_EMBEDDABLE_TABS = new Set(["nodemap"]);

/**
 * Node specs (`{type, data, version, repo, title?}`) for a tab. `hashOf(tab)` gives the tab's address
 * (`urlSync.paramsForTab(tab).toString()`); `live` forces a tab node even where a native node exists.
 */
export function nodeSpecsForTab(tab, hashOf, { live = false } = {}) {
  if (!tab || NON_EMBEDDABLE_TABS.has(tab.type)) return [];
  const base = { version: tab.version || "", repo: tab.repo || "" };
  if (!live) {
    if (tab.type === "code" && tab.path && !/\/$/.test(tab.path)) {
      const start = tab.cursorLine && tab.lineEnd && tab.lineEnd > tab.cursorLine ? tab.cursorLine : null;
      return [{ ...base, type: "source", data: start ? { path: tab.path, mode: "range", start, end: tab.lineEnd } : { path: tab.path, mode: "file" } }];
    }
    if (tab.type === "man" && (tab.manPath || tab.manPage)) {
      return [{ ...base, type: "man", data: { repo: tab.repo || "", version: tab.version || "", path: tab.manPath || "", name: tab.manPage || "", section: tab.manSec || "" } }];
    }
    if (tab.type === "pahole" && tab.structName) {
      return [{ ...base, type: "struct", data: { name: tab.structName, kind: "struct" } }];
    }
  }
  const hash = hashOf ? hashOf(tab) : "";
  if (!hash) return [];
  return [{ ...base, type: "tab", title: tab.title || "", data: { tabType: tab.type, hash } }];
}

/** Specs for the source view's context: the file, a line range, or one symbol's definition. */
export function sourceSpec({ path, version, repo = "", start = null, end = null, symbol = "", kind = "" }) {
  // A symbol slice may leave the file to the revision's index
  if (!path && !symbol) return null;
  if (symbol) return { type: "source", version, repo, data: { path, mode: "symbol", symbol, symbolKind: kind } };
  if (start) return { type: "source", version, repo, data: { path, mode: "range", start, end: end || start } };
  return { type: "source", version, repo, data: { path, mode: "file" } };
}

/** The NodeMap tab "Send to NodeMap" targets: one of the project in the active pane, else in any pane. */
export function targetNodemapTab(panes, activePaneId, repoOf, repo) {
  const ordered = [...panes].sort((a, b) => (a.id === activePaneId ? -1 : b.id === activePaneId ? 1 : 0));
  for (const p of ordered) {
    const t = p.tabs.find((x) => x.type === "nodemap" && repoOf(x) === repo);
    if (t) return t;
  }
  return null;
}

/** Queue node specs on the project's NodeMap (opened when none is) and show it. */
export function sendToNodemap(specs, { repo = state.activeRepo(), version = state.activeVersion() } = {}) {
  const list = (Array.isArray(specs) ? specs : [specs]).filter(Boolean);
  if (!list.length) return false;
  const target = targetNodemapTab(state.panes, state.activePaneId, (t) => state.repoOf(t), repo);
  if (target) {
    state.updateTabState(target.id, { pendingNodes: [...(target.pendingNodes || []), ...list] });
    state.focusTab(target.id);
    state.emit("nodemap:pending", { tabId: target.id });
  } else {
    state.openTab({ type: "nodemap", title: "NodeMap Canvas", repo, version, pendingNodes: list });
  }
  toast.success(list.length === 1 ? "Sent to NodeMap" : `Sent ${list.length} nodes to NodeMap`);
  return true;
}

/** Spec for a code token: struct/union tokens become struct nodes (the file is the fallback lookup place). */
export function tokenSpec({ name, typeId = null, astId = null, path = "", version = "", repo = "" }) {
  const raw = String(name || "").trim();
  if (!raw) return null;
  if (typeId === 28 || typeId === 34 || /^(struct|union)\s+/.test(raw)) {
    const kind = typeId === 34 || /^union\s/.test(raw) ? "union" : "struct";
    return { type: "struct", version, repo, data: { name: raw.replace(/^(struct|union)\s+/, ""), kind, hint: path } };
  }
  const func = typeId === 5 || typeId === 6;
  return { type: "symbol", version, repo, data: { name: raw.replace(/\(\)$/, ""), kind: func ? "func" : "symbol", astId: Number.isInteger(astId) && astId > 0 ? astId : null } };
}
