/**
 * session_transfer.js - Session export / import (G07, webapp/API.md §5.23): the `kip-session` file, its
 * validation (the file is untrusted) and the import plan (configurations added, identical or in conflict).
 * DOM-free, tested.
 */
export const SESSION_FORMAT = "kip-session";
export const MAX_SESSION_BYTES = 5 << 20;
export const TAB_TYPES = ["code", "kconfig", "explorer", "mail", "nodemap", "maintainers", "commits", "pahole", "callgraph",
  "diff", "treemap", "search", "status", "patch", "bookmarks", "api", "insights"];

export const profileKeyOf = (version, arch, name = "") => `${version}|${arch}${String(name || "").trim() ? `|${String(name).trim()}` : ""}`;

/** Configuration keys a session uses: its KConfig tabs and its engines. */
export function referencedProfileKeys(session) {
  const keys = new Set();
  for (const pane of session.panes || []) {
    for (const t of pane.tabs || []) if (t.type === "kconfig") keys.add(profileKeyOf(t.version || session.version, t.arch || "x86_64", t.configName));
  }
  for (const d of Object.values(session.kconfigEngineProfiles || {})) if (d?.profileKey) keys.add(d.profileKey);
  return [...keys];
}

export function buildSessionExport({ session, profiles = {}, toolchains = {}, userRecords = null, includeAll = false, build = "", repo = "", now = Date.now() }) {
  const keys = includeAll ? Object.keys(profiles) : referencedProfileKeys(session).filter((k) => k in profiles);
  const pick = (map, ks) => Object.fromEntries(ks.filter((k) => k in map).map((k) => [k, map[k]]));
  const doc = {
    format: SESSION_FORMAT, version: 1, exported_at: new Date(now).toISOString(), build, repository: repo || session.repo || "",
    session: {
      name: session.name || "", version: session.version || "", selectedBranch: session.selectedBranch || "", splitMode: session.splitMode || "single",
      activePaneId: session.activePaneId || "", panes: session.panes || [], kconfigEngineTabIds: session.kconfigEngineTabIds || [],
      kconfigEngineProfiles: session.kconfigEngineProfiles || {},
    },
    kconfig: { profiles: pick(profiles, keys), toolchains: pick(toolchains, includeAll ? Object.keys(toolchains) : keys) },
  };
  if (userRecords) doc.user = { records: userRecords };
  return doc;
}

const SAFE_PATH = (p) => typeof p === "string" && p.length <= 1024 && !p.startsWith("/") && !p.includes("\0") && !p.split("/").includes("..");
const STR = (v, max = 512) => (typeof v === "string" ? v.slice(0, max) : "");

/** The validated, cleaned content of a session file; throws with the reason when it cannot be imported. */
export function validateSessionFile(text) {
  if (typeof text !== "string" || text.length > MAX_SESSION_BYTES) throw new Error("A session file is limited to 5 MB");
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new Error("Not a JSON file");
  }
  if (doc?.format !== SESSION_FORMAT) throw new Error("Not a KernelInfo session export");
  if (!(doc.version >= 1 && doc.version <= 1)) throw new Error(`Unsupported session format version ${doc.version}`);
  const s = doc.session || {};
  if (!Array.isArray(s.panes) || !s.panes.length || s.panes.length > 8) throw new Error("The session has no panes");
  const panes = s.panes.map((p, i) => {
    if (!Array.isArray(p?.tabs) || p.tabs.length > 200) throw new Error(`Pane ${i + 1} is invalid`);
    const tabs = p.tabs.map((t) => {
      if (!t || typeof t !== "object" || !TAB_TYPES.includes(t.type)) throw new Error(`Unknown tab type: ${STR(t?.type, 40) || "?"}`);
      for (const k of ["path", "filePath"]) if (t[k] !== undefined && t[k] !== "" && !SAFE_PATH(t[k])) throw new Error(`Invalid path in a ${t.type} tab`);
      const clean = {};
      for (const [k, v] of Object.entries(t)) {
        if (typeof v === "string") clean[k] = v.slice(0, 4096);
        else if (typeof v === "number" || typeof v === "boolean" || v === null) clean[k] = v;
        else if (typeof v === "object" && JSON.stringify(v).length <= 65536) clean[k] = JSON.parse(JSON.stringify(v));
      }
      clean.id = STR(t.id, 64) || `tab-${Math.random().toString(36).slice(2, 9)}`;
      return clean;
    });
    return { id: STR(p.id, 64) || `pane-${i + 1}`, activeTabId: STR(p.activeTabId, 64), tabs };
  });
  const objOf = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
  const profiles = {};
  for (const [k, v] of Object.entries(objOf(doc.kconfig?.profiles))) {
    if (typeof k === "string" && k.length <= 300 && v && typeof v === "object") profiles[k] = Object.fromEntries(Object.entries(v).filter(([n, x]) => /^[A-Za-z0-9_]{1,200}$/.test(n) && typeof x === "string" && x.length <= 4096));
  }
  const toolchains = {};
  for (const [k, v] of Object.entries(objOf(doc.kconfig?.toolchains))) if (k.length <= 300 && v && typeof v === "object") toolchains[k] = JSON.parse(JSON.stringify(v));
  return {
    repository: STR(doc.repository, 200), build: STR(doc.build, 80), exported_at: STR(doc.exported_at, 40),
    session: {
      name: STR(s.name, 200) || "Imported session", version: STR(s.version, 200), selectedBranch: STR(s.selectedBranch, 200),
      splitMode: ["single", "horizontal", "vertical"].includes(s.splitMode) ? s.splitMode : "single", activePaneId: STR(s.activePaneId, 64), panes,
      kconfigEngineTabIds: Array.isArray(s.kconfigEngineTabIds) ? s.kconfigEngineTabIds.filter((x) => typeof x === "string").slice(0, 50) : [],
      kconfigEngineProfiles: objOf(s.kconfigEngineProfiles),
    },
    profiles, toolchains,
    user: Array.isArray(doc.user?.records) ? doc.user.records : [],
  };
}

const sameValues = (a, b) => {
  const ka = Object.keys(a || {});
  return ka.length === Object.keys(b || {}).length && ka.every((k) => String(a[k]) === String((b || {})[k]));
};

/** Which imported configurations are new, identical or in conflict with the local ones. */
export function planSessionImport(imported, localProfiles) {
  const adds = [];
  const same = [];
  const conflicts = [];
  for (const [key, values] of Object.entries(imported.profiles)) {
    if (!(key in localProfiles)) adds.push(key);
    else if (sameValues(localProfiles[key], values)) same.push(key);
    else conflicts.push({ key, local: Object.keys(localProfiles[key] || {}).length, imported: Object.keys(values).length });
  }
  return { adds, same, conflicts };
}

/**
 * What to write for the chosen conflict resolutions (`keep` | `use` | `copy`), with the session rewritten for
 * copies: `{session, profiles, toolchains, copies: {oldKey: newKey}}`. `freeName(version, arch)` gives a free name.
 */
export function resolveSessionImport(imported, plan, choices, freeName) {
  const profiles = {};
  const toolchains = {};
  const copies = {};
  for (const key of plan.adds) {
    profiles[key] = imported.profiles[key];
    if (imported.toolchains[key]) toolchains[key] = imported.toolchains[key];
  }
  for (const c of plan.conflicts) {
    const choice = choices[c.key] || "keep";
    if (choice === "use") {
      profiles[c.key] = imported.profiles[c.key];
      if (imported.toolchains[c.key]) toolchains[c.key] = imported.toolchains[c.key];
    } else if (choice === "copy") {
      const [version, arch] = c.key.split("|");
      const name = freeName(version, arch);
      const nk = profileKeyOf(version, arch, name);
      copies[c.key] = { key: nk, name };
      profiles[nk] = imported.profiles[c.key];
      if (imported.toolchains[c.key]) toolchains[nk] = imported.toolchains[c.key];
    }
  }
  const session = JSON.parse(JSON.stringify(imported.session));
  for (const pane of session.panes) {
    for (const t of pane.tabs) {
      if (t.type !== "kconfig") continue;
      const copy = copies[profileKeyOf(t.version || session.version, t.arch || "x86_64", t.configName)];
      if (copy) {
        t.configName = copy.name;
        t.title = `KConfig (${t.arch || "x86_64"}) · ${copy.name}`;
      }
    }
  }
  for (const d of Object.values(session.kconfigEngineProfiles)) {
    const copy = d && copies[d.profileKey];
    if (copy) {
      d.profileKey = copy.key;
      d.configName = copy.name;
    }
  }
  return { session, profiles, toolchains, copies };
}
