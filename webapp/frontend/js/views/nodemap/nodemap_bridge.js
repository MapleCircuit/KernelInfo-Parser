/**
 * nodemap_bridge.js - "Send to NodeMap" from other views (`webapp/API_FRONTEND.md` §5.3.5).
 *
 * `nodeSpecsForTab()` (pure) turns a tab into the node that shows it natively (code tab → source node,
 * man tab → man page node, pahole tab → struct node, a Compare tab showing one file and a Commits tab
 * showing one commit → diff node, a Mailing List tab on a thread → mail node, a Kconfig tab on a symbol →
 * kconfig node, a Maintainers tab on a section → maintainer node) or, for every other tab, a live tab
 * node embedding the tab's address. `tabForNode()` (pure) is the reverse: the workspace tab a node opens
 * as (Open as tab, Copy to Session). `sendToNodemap()` queues specs on an open NodeMap tab of the same project (or opens
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
    if (tab.type === "diff" && tab.path && (tab.versionA || tab.versionB)) {
      return [{ ...base, type: "diff", data: { mode: "file", versionA: tab.versionA || "", versionB: tab.versionB || tab.version || "", path: tab.path, oldPath: tab.oldPath || "" } }];
    }
    if (tab.type === "commits" && tab.commitId) {
      return [{ ...base, type: "diff", data: { mode: "commit", commitHash: tab.commitId, path: "" } }];
    }
    if (tab.type === "mail" && (tab.threadId || tab.messageId)) {
      return [{ ...base, type: "mail", data: { tid: Number(tab.threadId) || null, mid: tab.messageId || "", subject: tab.mailSubject || "" } }];
    }
    if (tab.type === "kconfig" && tab.symbol) {
      return [{ ...base, type: "kconfig", data: { symbol: tab.symbol, arch: tab.arch || "x86_64" } }];
    }
    if (tab.type === "maintainers" && tab.secId) {
      return [{ ...base, type: "maintainer", data: { secId: Number(tab.secId) || null, title: tab.section || "" } }];
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

/** Spec of a diff node: one file between two revisions, or one commit (optionally one of its files). */
export function diffSpec({ versionA = "", versionB = "", path = "", oldPath = "", commitHash = "", version = "", repo = "" }) {
  if (commitHash) return { type: "diff", version: version || versionB, repo, data: { mode: "commit", commitHash, path } };
  if (!path) return null;
  return { type: "diff", version: version || versionB, repo, data: { mode: "file", versionA, versionB, path, oldPath } };
}

/** Spec of a mail node: a thread (`tid`), focused on one message (`mid`) when given. */
export function mailSpec({ tid = null, mid = "", subject = "", version = "", repo = "" }) {
  if (!tid && !mid) return null;
  return { type: "mail", version, repo, data: { tid: Number(tid) || null, mid: String(mid || ""), subject } };
}

/** Spec of a Kconfig symbol node. */
export function kconfigSpec({ symbol, arch = "x86_64", version = "", repo = "" }) {
  const name = String(symbol || "").trim().replace(/^CONFIG_/, "");
  if (!/^[A-Za-z0-9_]+$/.test(name)) return null;
  return { type: "kconfig", version, repo, data: { symbol: name, arch } };
}

/** Spec of a MAINTAINERS section node (by id, or resolved from a path or a name query). */
export function maintainerSpec({ secId = null, title = "", path = "", query = "", version = "", repo = "" }) {
  if (!secId && !path && !query) return null;
  return { type: "maintainer", version, repo, data: { secId: Number(secId) || null, title, path, query } };
}

/** Parameters of a tab address (`hash`) a tab node embeds, as the fields of a workspace tab. */
function tabFieldsOfHash(hash) {
  const p = new URLSearchParams(String(hash || "").replace(/^#/, ""));
  const out = {};
  const file = p.get("file");
  if (file) out.path = file;
  const line = /^(\d+)(?:-(\d+))?$/.exec(p.get("line") || "");
  if (line) {
    out.cursorLine = parseInt(line[1], 10);
    if (line[2] && parseInt(line[2], 10) > out.cursorLine) out.lineEnd = parseInt(line[2], 10);
  }
  for (const [k, f] of [["arch", "arch"], ["cfg", "configName"], ["q", "query"], ["symbol", "symbol"]]) if (p.get(k)) out[f] = p.get(k);
  if (p.get("v")) out.version = p.get("v");
  if (p.get("repo")) out.repo = p.get("repo");
  return out;
}

/**
 * The workspace tab a node opens as (`state.openTab()` descriptor, no id): `node` the canvas node,
 * `loaded` its loaded content (a struct layout's definition opens the file; a source symbol slice opens
 * at its line), `fallback` `{repo, version}` of the canvas. Null for nodes without a tab (notes, groups).
 */
export function tabForNode(node, loaded = null, fallback = {}) {
  if (!node) return null;
  const d = node.data || {};
  const base = { repo: node.repo || fallback.repo || "", version: node.version || fallback.version || "" };
  const file = (p) => String(p || "").split("/").pop();
  switch (node.type) {
    case "source": {
      const line = d.mode === "range" ? d.start : loaded?.symbol?.line_s;
      return { type: "code", title: file(d.path || loaded?.path), path: d.path || loaded?.path || "", cursorLine: line || 1, ...(d.mode === "range" && d.end > d.start ? { lineEnd: d.end } : {}), ...base };
    }
    case "struct":
      if (loaded?.file_path) return { type: "code", title: file(loaded.file_path), path: loaded.file_path, cursorLine: loaded.line_s, ...base };
      return { type: "pahole", title: `struct ${d.name}`, structName: d.name, ...base };
    case "man":
      return d.path ? { type: "man", repo: d.repo, version: d.version, manPath: d.path, manPage: d.name, manSec: d.section, title: d.name ? `${d.name}(${d.section || "?"})` : file(d.path) } : null;
    case "symbol":
      if (d.kind === "func") return { type: "callgraph", title: `Callgraph: ${d.name}`, functionName: d.name, ...base };
      return { type: "search", title: `Search: ${d.name}`, query: d.name, word: true, ...base };
    case "tab":
      return d.tabType ? { type: d.tabType, title: node.customTitle || node.title || `${d.tabType} tab`, ...base, ...tabFieldsOfHash(d.hash) } : null;
    case "diff":
      if (d.mode === "commit") return d.commitHash ? { type: "commits", title: `Commit ${d.commitHash.slice(0, 12)}`, commitId: d.commitHash, ...base } : null;
      return { type: "diff", title: `Compare ${d.versionA}..${d.versionB}`, versionA: d.versionA, versionB: d.versionB, ...base, version: d.versionB || base.version };
    case "mail":
      return { type: "mail", title: d.subject || "Mailing List", threadId: d.tid || 0, messageId: d.mid || "", ...base };
    case "kconfig":
      return { type: "kconfig", title: `KConfig (${d.arch})`, symbol: d.symbol, arch: d.arch, ...base };
    case "maintainer":
      return { type: "maintainers", title: d.title ? `Maintainers: ${d.title}` : "Maintainers", ...(d.secId ? { secId: d.secId } : {}), ...(d.title ? { section: d.title } : {}), ...base };
    default:
      return null;
  }
}
