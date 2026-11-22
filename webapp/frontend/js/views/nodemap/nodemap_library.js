/**
 * nodemap_library.js - Named NodeMap canvas library (IndexedDB USER_STORE + localStorage fallback),
 * orphaned tab canvas pruning, and one-click kernel starter templates.
 *
 * DOM-free (unit tested).
 */
import {
  STORAGE_PREFIX,
  USER_CANVAS_KIND,
  applyEdgePreset,
  createEdge,
  createNode,
  emptyCanvas,
  newId,
  normalizeCanvas,
  serializeCanvas,
  setFieldLock,
} from "./canvas_model.js";

export const NAMED_CANVAS_KIND = "nodemap-named";
export const NAMED_STORAGE_PREFIX = "kip-nodemap-named:";
export const DEFAULT_ORPHAN_RETENTION_MS = 14 * 24 * 60 * 60 * 1000;

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

function safeParse(raw) {
  if (typeof raw !== "string" || !raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function storageKeys(storage) {
  if (!storage) return [];
  const keys = [];
  try {
    if (typeof storage.length === "number" && typeof storage.key === "function") {
      for (let i = 0; i < storage.length; i++) {
        const k = storage.key(i);
        if (typeof k === "string") keys.push(k);
      }
      return keys;
    }
    if (storage._m instanceof Map) return [...storage._m.keys()].map(String);
    return Object.keys(storage);
  } catch {
    return [];
  }
}

function idFromNamedKey(key) {
  const s = String(key || "");
  return s.startsWith(NAMED_STORAGE_PREFIX) ? (s.slice(NAMED_STORAGE_PREFIX.length) || s) : s;
}

function normalizeStoredCanvas(rec) {
  if (!isObj(rec)) return null;
  if (rec.data) return normalizeCanvas(rec.data);
  if (rec.canvas) return normalizeCanvas(rec.canvas);
  return normalizeCanvas(rec);
}

function summaryFromRecord(rec, fallbackKey = "") {
  const key = String(rec?.key || fallbackKey);
  const canvasObj = isObj(rec?.data) ? rec.data : isObj(rec?.canvas) ? rec.canvas : null;
  const nodeCount = Number.isFinite(rec?.nodeCount)
    ? rec.nodeCount
    : Array.isArray(canvasObj?.nodes) ? canvasObj.nodes.length : 0;
  const edgeCount = Number.isFinite(rec?.edgeCount)
    ? rec.edgeCount
    : Array.isArray(canvasObj?.edges) ? canvasObj.edges.length : 0;
  return {
    key,
    id: String(rec?.id || idFromNamedKey(key)),
    name: String(rec?.name || "").trim() || "Untitled Map",
    repo: String(rec?.repo || ""),
    created_at: Number(rec?.created_at) || 0,
    updated_at: Number(rec?.updated_at) || 0,
    nodeCount,
    edgeCount,
  };
}

// ------------------------------------------------------------------ Named Canvas Library CRUD

export async function saveNamedCanvas(
  userDb,
  { key = "", name = "Untitled Map", repo = "", canvas = null, created_at = null } = {},
  fallbackStorage = globalThis.localStorage,
) {
  const cleanKey = String(key || "").trim();
  const id = cleanKey ? idFromNamedKey(cleanKey) : newId("nm");
  const finalKey = cleanKey || `${NAMED_STORAGE_PREFIX}${id}`;
  const cleanName = String(name ?? "").trim() || "Untitled Map";
  const cleanRepo = String(repo || "");
  const normalizedCanvas = (canvas ? normalizeCanvas(canvas) : null) || emptyCanvas();
  const data = serializeCanvas(normalizedCanvas);

  let prevCreatedAt = Number(created_at) || 0;
  if (!prevCreatedAt && cleanKey) {
    try {
      const existingIdb = typeof userDb?.userGet === "function" ? await userDb.userGet(finalKey) : null;
      const existingLs = safeParse(fallbackStorage?.getItem?.(finalKey));
      prevCreatedAt = Number(existingIdb?.created_at || existingLs?.created_at) || 0;
    } catch {
      prevCreatedAt = 0;
    }
  }

  const now = Date.now();
  const createdAt = prevCreatedAt || now;
  const updatedAt = now;
  const nodeCount = normalizedCanvas.nodes.length;
  const edgeCount = normalizedCanvas.edges.length;

  const storedRecord = {
    key: finalKey,
    kind: NAMED_CANVAS_KIND,
    repo: cleanRepo,
    id,
    name: cleanName,
    created_at: createdAt,
    updated_at: updatedAt,
    nodeCount,
    edgeCount,
    data,
  };

  const record = {
    key: finalKey,
    name: cleanName,
    repo: cleanRepo,
    created_at: createdAt,
    updated_at: updatedAt,
    nodeCount,
    edgeCount,
    canvas: normalizedCanvas,
  };

  if (userDb && typeof userDb.userPut === "function") {
    try {
      const putOk = await userDb.userPut(storedRecord);
      if (putOk) return { ok: true, key: finalKey, record, backend: "indexeddb" };
    } catch {
      // Fall back to localStorage below
    }
  }

  let lsOk = false;
  if (fallbackStorage && typeof fallbackStorage.setItem === "function") {
    try {
      fallbackStorage.setItem(finalKey, JSON.stringify(storedRecord));
      lsOk = true;
    } catch {
      lsOk = false;
    }
  }
  return { ok: lsOk, key: finalKey, record, backend: "localStorage" };
}

export async function listNamedCanvases(
  userDb,
  { repo = null } = {},
  fallbackStorage = globalThis.localStorage,
) {
  const byKey = new Map();
  const matchesRepo = (r) => repo === null || repo === undefined || String(r?.repo || "") === String(repo);

  if (userDb && typeof userDb.userList === "function") {
    try {
      const idbList = await userDb.userList(NAMED_CANVAS_KIND, repo);
      for (const raw of Array.isArray(idbList) ? idbList : []) {
        if (!isObj(raw) || !raw.key) continue;
        if (raw.kind && raw.kind !== NAMED_CANVAS_KIND) continue;
        if (!matchesRepo(raw)) continue;
        const item = summaryFromRecord(raw, raw.key);
        const prev = byKey.get(item.key);
        if (!prev || item.updated_at > prev.updated_at) byKey.set(item.key, item);
      }
    } catch {
      // Fall back to localStorage entries only
    }
  }

  if (fallbackStorage && typeof fallbackStorage.getItem === "function") {
    for (const k of storageKeys(fallbackStorage)) {
      if (!k.startsWith(NAMED_STORAGE_PREFIX)) continue;
      let parsed = null;
      try {
        parsed = safeParse(fallbackStorage.getItem(k));
      } catch {
        parsed = null;
      }
      if (!isObj(parsed)) continue;
      if (parsed.kind && parsed.kind !== NAMED_CANVAS_KIND) continue;
      if (!matchesRepo(parsed)) continue;
      const item = summaryFromRecord(parsed, k);
      const prev = byKey.get(item.key);
      if (!prev || item.updated_at > prev.updated_at) byKey.set(item.key, item);
    }
  }

  return [...byKey.values()].sort((a, b) => (b.updated_at - a.updated_at) || a.name.localeCompare(b.name));
}

export async function loadNamedCanvas(userDb, key, fallbackStorage = globalThis.localStorage) {
  const cleanKey = String(key || "").trim();
  if (!cleanKey) return null;

  let idbRec = null;
  let idbCanvas = null;
  if (userDb && typeof userDb.userGet === "function") {
    try {
      const rec = await userDb.userGet(cleanKey);
      const canvas = normalizeStoredCanvas(rec);
      if (canvas) {
        idbRec = rec;
        idbCanvas = canvas;
      }
    } catch {
      idbRec = null;
    }
  }

  let lsRec = null;
  let lsCanvas = null;
  if (fallbackStorage && typeof fallbackStorage.getItem === "function") {
    try {
      const rec = safeParse(fallbackStorage.getItem(cleanKey));
      const canvas = normalizeStoredCanvas(rec);
      if (canvas) {
        lsRec = rec;
        lsCanvas = canvas;
      }
    } catch {
      lsRec = null;
    }
  }

  if (!idbCanvas && !lsCanvas) return null;
  const useLs = Boolean(
    lsCanvas && (!idbCanvas || (Number(lsRec?.updated_at) || 0) > (Number(idbRec?.updated_at) || 0)),
  );
  const chosen = useLs ? lsRec : idbRec;
  const canvas = useLs ? lsCanvas : idbCanvas;
  const backend = useLs ? "localStorage" : "indexeddb";

  return {
    key: String(chosen?.key || cleanKey),
    name: String(chosen?.name || "").trim() || "Untitled Map",
    repo: String(chosen?.repo || ""),
    created_at: Number(chosen?.created_at) || 0,
    updated_at: Number(chosen?.updated_at) || 0,
    canvas,
    backend,
  };
}

export async function renameNamedCanvas(userDb, key, newName, fallbackStorage = globalThis.localStorage) {
  const cleanKey = String(key || "").trim();
  const cleanName = String(newName ?? "").trim();
  if (!cleanKey || !cleanName) return null;

  const existing = await loadNamedCanvas(userDb, cleanKey, fallbackStorage);
  if (!existing) return null;

  const res = await saveNamedCanvas(
    userDb,
    {
      key: existing.key,
      name: cleanName,
      repo: existing.repo,
      canvas: existing.canvas,
      created_at: existing.created_at,
    },
    fallbackStorage,
  );
  return res.ok ? res.record : null;
}

export async function duplicateNamedCanvas(userDb, key, newName = "", fallbackStorage = globalThis.localStorage) {
  const cleanKey = String(key || "").trim();
  if (!cleanKey) return null;

  const existing = await loadNamedCanvas(userDb, cleanKey, fallbackStorage);
  if (!existing) return null;

  const cleanNew = String(newName ?? "").trim();
  const copyName = cleanNew || `${existing.name || "Untitled Map"} (Copy)`;

  const res = await saveNamedCanvas(
    userDb,
    {
      key: "",
      name: copyName,
      repo: existing.repo,
      canvas: existing.canvas,
    },
    fallbackStorage,
  );
  return res.ok ? res.record : null;
}

export async function deleteNamedCanvas(userDb, key, fallbackStorage = globalThis.localStorage) {
  const cleanKey = String(key || "").trim();
  if (!cleanKey) return false;

  let deleted = false;
  if (userDb && typeof userDb.userDelete === "function") {
    try {
      const ok = await userDb.userDelete(cleanKey);
      if (ok) deleted = true;
    } catch {
      // Ignore IDB delete error, still try fallbackStorage
    }
  }

  if (fallbackStorage && typeof fallbackStorage.removeItem === "function") {
    try {
      let hadItem = true;
      if (typeof fallbackStorage.getItem === "function") {
        const val = fallbackStorage.getItem(cleanKey);
        hadItem = val !== null && val !== undefined;
      }
      fallbackStorage.removeItem(cleanKey);
      if (hadItem) deleted = true;
    } catch {
      // Ignore storage error
    }
  }

  return deleted;
}

// ------------------------------------------------------------------ Orphaned Tab Canvas Pruning

function collectTabsFromPanes(panes, out) {
  for (const pane of Array.isArray(panes) ? panes : []) {
    for (const tab of Array.isArray(pane?.tabs) ? pane.tabs : []) {
      if (tab?.id) out.add(String(tab.id));
    }
  }
}

export function collectReferencedTabIds({
  openPanes = [],
  sessionsIndex = [],
  readSession = null,
  storage = globalThis.localStorage,
} = {}) {
  const ids = new Set();
  collectTabsFromPanes(openPanes, ids);

  for (const s of Array.isArray(sessionsIndex) ? sessionsIndex : []) {
    const sid = typeof s === "string" ? s : s?.id;
    if (isObj(s) && Array.isArray(s.panes)) collectTabsFromPanes(s.panes, ids);
    if (!sid) continue;
    let rec = null;
    try {
      rec = typeof readSession === "function"
        ? readSession(sid)
        : safeParse(storage?.getItem?.(`kip_session_${sid}`));
    } catch {
      rec = null;
    }
    if (isObj(rec)) collectTabsFromPanes(rec.panes, ids);
  }

  return ids;
}

export async function findOrphanedTabCanvases(
  userDb,
  {
    activeTabIds = new Set(),
    maxAgeMs = DEFAULT_ORPHAN_RETENTION_MS,
    now = Date.now(),
    fallbackStorage = globalThis.localStorage,
    skipZeroTimestamp = false,
  } = {},
) {
  if (maxAgeMs === Infinity) return [];
  const ageLimit = Number.isFinite(Number(maxAgeMs)) ? Math.max(0, Number(maxAgeMs)) : DEFAULT_ORPHAN_RETENTION_MS;
  const activeSet = activeTabIds instanceof Set
    ? activeTabIds
    : new Set(Array.isArray(activeTabIds) ? activeTabIds.map(String) : []);

  const entries = new Map();

  if (userDb && typeof userDb.userList === "function") {
    try {
      const list = await userDb.userList(USER_CANVAS_KIND);
      for (const rec of Array.isArray(list) ? list : []) {
        if (!isObj(rec) || !rec.key) continue;
        if (rec.kind && rec.kind !== USER_CANVAS_KIND) continue;
        const key = String(rec.key);
        const tabId = key.startsWith(STORAGE_PREFIX) ? key.slice(STORAGE_PREFIX.length) : String(rec.id || "");
        const ts = Number(rec.updated_at || rec.created_at || 0);
        entries.set(key, { key, tabId, hasIdb: true, hasLs: false, ts });
      }
    } catch {
      // Continue with fallbackStorage scan
    }
  }

  if (fallbackStorage && typeof fallbackStorage.getItem === "function") {
    for (const k of storageKeys(fallbackStorage)) {
      if (!k.startsWith(STORAGE_PREFIX)) continue;
      const tabId = k.slice(STORAGE_PREFIX.length);
      let lsTs = 0;
      try {
        const parsed = safeParse(fallbackStorage.getItem(k));
        lsTs = Number(parsed?.updated_at || parsed?.created_at || 0);
      } catch {
        lsTs = 0;
      }
      const existing = entries.get(k);
      if (existing) {
        existing.hasLs = true;
        if (!existing.ts && lsTs) existing.ts = lsTs;
      } else {
        entries.set(k, { key: k, tabId, hasIdb: false, hasLs: true, ts: lsTs });
      }
    }
  }

  const orphans = [];
  for (const entry of entries.values()) {
    if (entry.tabId && activeSet.has(entry.tabId)) continue;
    if (entry.ts === 0 && skipZeroTimestamp) continue;
    if (now - entry.ts < ageLimit) continue;
    const backend = entry.hasIdb && entry.hasLs
      ? "indexeddb+localStorage"
      : entry.hasIdb ? "indexeddb" : "localStorage";
    orphans.push({
      key: entry.key,
      tabId: entry.tabId,
      backend,
      updated_at: entry.ts,
    });
  }
  return orphans;
}

export async function pruneOrphanedTabCanvases(userDb, opts = {}) {
  const orphans = await findOrphanedTabCanvases(userDb, opts);
  const fallbackStorage = opts.fallbackStorage !== undefined ? opts.fallbackStorage : globalThis.localStorage;
  const prunedKeys = [];

  for (const orphan of orphans) {
    let removed = false;
    if (orphan.backend.includes("indexeddb") && userDb && typeof userDb.userDelete === "function") {
      try {
        const ok = await userDb.userDelete(orphan.key);
        if (ok) removed = true;
      } catch {
        // Ignore IDB error
      }
    }
    if (orphan.backend.includes("localStorage") && fallbackStorage && typeof fallbackStorage.removeItem === "function") {
      try {
        fallbackStorage.removeItem(orphan.key);
        removed = true;
      } catch {
        // Ignore storage error
      }
    }
    if (removed) prunedKeys.push(orphan.key);
  }

  return { prunedCount: prunedKeys.length, prunedKeys };
}

// ------------------------------------------------------------------ 4 One-Click Kernel Starter Templates

export const KERNEL_TEMPLATES = [
  {
    id: "syscall-flow",
    name: "Syscall-to-Driver Flow",
    category: "Execution Path",
    icon: "🔀",
    description: "Userspace syscall man page -> kapi entry -> VFS/driver function -> core struct -> implementation slice.",
  },
  {
    id: "cve-root-cause",
    name: "CVE Root Cause & Fix",
    category: "Security",
    icon: "🛡",
    description: "CVE record -> introducing commit diff -> fix commit diff -> affected struct/source -> subsystem maintainer.",
  },
  {
    id: "struct-locking",
    name: "Struct & Lock Ownership",
    category: "Concurrency",
    icon: "🔒",
    description: "Central struct with lock-annotated fields & node locks connected to protected child structs and invariant notes.",
  },
  {
    id: "subsystem-overview",
    name: "Subsystem Overview",
    category: "Architecture",
    icon: "🧭",
    description: "Layered group portals with maintainer ownership, Kconfig options, Kernel Docs, and Kernel API boundary.",
  },
];

function mkTour(title, steps) {
  return {
    id: newId("tour"),
    title,
    steps: steps.map((s) => ({
      id: newId("step"),
      title: s.title,
      caption: s.caption || "",
      nodeIds: s.nodeIds || [],
      edgeIds: s.edgeIds || [],
      viewport: s.viewport || { x: 0, y: 0, zoom: 0.85 },
    })),
  };
}

function buildSyscallFlowTemplate(version, repo) {
  const gBoundary = createNode("group", {
    x: 40, y: 60, w: 880, h: 420, version, repo, color: "cyan",
    customTitle: "Userspace & Syscall Boundary",
  });
  const gCore = createNode("group", {
    x: 980, y: 60, w: 720, h: 600, version, repo, color: "blue",
    customTitle: "VFS Dispatch & Core Data",
  });

  const manNode = createNode("man", {
    x: 80, y: 120, w: 380, h: 310, version, repo, groupId: gBoundary.id, color: "cyan",
    customTitle: "Userspace: openat(2)",
    data: { repo, version, name: "openat", section: "2", path: "man2/openat.2" },
  });
  const kapiNode = createNode("kapi", {
    x: 520, y: 120, w: 360, h: 310, version, repo, groupId: gBoundary.id, color: "blue",
    data: { catalog: "syscalls", symbol: "openat", arch: "x86_64" },
  });
  const fnNode = createNode("symbol", {
    x: 1020, y: 120, w: 260, h: 120, version, repo, groupId: gCore.id, color: "blue",
    data: { name: "do_sys_openat2", kind: "func" },
  });
  const structNode = createNode("struct", {
    x: 1340, y: 120, w: 320, h: 270, version, repo, groupId: gCore.id, color: "purple",
    data: { name: "file", kind: "struct", path: "include/linux/fs.h" },
  });
  const srcNode = createNode("source", {
    x: 1020, y: 410, w: 540, h: 220, version, repo, groupId: gCore.id, color: "green",
    data: { path: "fs/open.c", mode: "symbol", symbol: "do_sys_openat2", symbolKind: "function" },
  });

  const e1 = createEdge({ from: manNode.id, to: kapiNode.id, label: "invokes", color: "cyan", arrow: "forward" });
  const e2 = createEdge({ from: kapiNode.id, to: fnNode.id, label: "calls", color: "blue", arrow: "forward" });
  const e3 = createEdge({ from: fnNode.id, to: structNode.id, label: "points to", color: "purple", arrow: "forward" });
  const e4 = createEdge({ from: fnNode.id, to: srcNode.id, label: "implemented in", color: "green", style: "dashed", arrow: "forward" });

  return normalizeCanvas({
    ...emptyCanvas(),
    nodes: [gBoundary, gCore, manNode, kapiNode, fnNode, structNode, srcNode],
    edges: [e1, e2, e3, e4],
    tours: [
      mkTour("Syscall-to-Driver Walkthrough", [
        {
          title: "1. Userspace & Syscall ABI",
          caption: "Start from the openat(2) man page and inspect its Kernel API syscall entry.",
          nodeIds: [gBoundary.id, manNode.id, kapiNode.id],
          edgeIds: [e1.id],
          viewport: { x: 40, y: 20, zoom: 0.9 },
        },
        {
          title: "2. VFS Dispatch & Source Slice",
          caption: "Trace syscall entry into do_sys_openat2() and inspect its definition in fs/open.c.",
          nodeIds: [kapiNode.id, fnNode.id, srcNode.id],
          edgeIds: [e2.id, e4.id],
          viewport: { x: -460, y: 20, zoom: 0.9 },
        },
        {
          title: "3. Core Kernel Struct",
          caption: "Inspect the struct file fields manipulated during open path setup.",
          nodeIds: [gCore.id, fnNode.id, structNode.id],
          edgeIds: [e3.id],
          viewport: { x: -720, y: 20, zoom: 0.9 },
        },
      ]),
    ],
  });
}

function buildCveRootCauseTemplate(version, repo) {
  const cveNode = createNode("cve", {
    x: 80, y: 100, w: 400, h: 330, version, repo, color: "red",
    customTitle: "CVE Record (Set CVE ID)",
    data: { cveId: "CVE-2024-0001", release: version },
  });
  const introNode = createNode("diff", {
    x: 560, y: 80, w: 460, h: 250, version, repo, color: "orange",
    customTitle: "Introducing Commit Diff",
    data: { mode: "commit", commitHash: "", path: "" },
  });
  const fixNode = createNode("diff", {
    x: 560, y: 380, w: 460, h: 250, version, repo, color: "green",
    customTitle: "Fix Commit Diff",
    data: { mode: "commit", commitHash: "", path: "" },
  });
  const structNode = createNode("struct", {
    x: 1100, y: 80, w: 320, h: 250, version, repo, color: "purple",
    data: { name: "task_struct", kind: "struct", path: "include/linux/sched.h" },
  });
  const srcNode = createNode("source", {
    x: 1100, y: 380, w: 460, h: 250, version, repo, color: "blue",
    data: { path: "kernel/fork.c", mode: "file" },
  });
  const maintNode = createNode("maintainer", {
    x: 80, y: 480, w: 400, h: 250, version, repo, color: "yellow",
    data: { query: "SCHEDULER", title: "Subsystem Maintainer" },
  });

  const e1 = createEdge({ from: cveNode.id, to: introNode.id, label: "introduced by", color: "orange", style: "dashed", arrow: "forward" });
  const e2 = createEdge({ from: cveNode.id, to: fixNode.id, label: "fixes", color: "green", arrow: "forward" });
  const e3 = createEdge({ from: introNode.id, to: structNode.id, label: "modifies", color: "orange", arrow: "forward" });
  const e4 = createEdge({ from: fixNode.id, to: srcNode.id, label: "patches", color: "green", arrow: "forward" });
  const e5 = createEdge({ from: srcNode.id, to: maintNode.id, label: "maintained by", color: "yellow", style: "dashed", arrow: "forward" });

  return normalizeCanvas({
    ...emptyCanvas(),
    nodes: [cveNode, introNode, fixNode, structNode, srcNode, maintNode],
    edges: [e1, e2, e3, e4, e5],
    tours: [
      mkTour("CVE Root Cause Walkthrough", [
        {
          title: "1. CVE Advisory & Maintainer",
          caption: "Inspect affected release status, severity, and owning subsystem maintainers.",
          nodeIds: [cveNode.id, maintNode.id],
          edgeIds: [],
          viewport: { x: 20, y: -20, zoom: 0.9 },
        },
        {
          title: "2. Introducing Commit & Affected Struct",
          caption: "Spawn or link the introducing commit diff to see how the vulnerable state arose.",
          nodeIds: [cveNode.id, introNode.id, structNode.id],
          edgeIds: [e1.id, e3.id],
          viewport: { x: -380, y: 0, zoom: 0.85 },
        },
        {
          title: "3. Fix Commit & Source Context",
          caption: "Review the fix patch alongside the affected kernel source file.",
          nodeIds: [cveNode.id, fixNode.id, srcNode.id],
          edgeIds: [e2.id, e4.id, e5.id],
          viewport: { x: -380, y: -180, zoom: 0.85 },
        },
      ]),
    ],
  });
}

function buildStructLockingTemplate(version, repo) {
  const mmNode = createNode("struct", {
    x: 80, y: 120, w: 340, h: 320, version, repo, color: "orange",
    locks: [
      { kind: "rwsem", lockName: "mmap_lock", note: "Serializes VMA tree edits and address-space teardown" },
      { kind: "spinlock", lockName: "page_table_lock", note: "Protects leaf page-table entries; nests inside mmap_lock" },
    ],
    data: { name: "mm_struct", kind: "struct", path: "include/linux/mm_types.h" },
  });
  setFieldLock(mmNode.data, "mmap_lock", { kind: "rwsem", lockName: "mmap_lock" });
  setFieldLock(mmNode.data, "mm_mt", { kind: "rwsem", lockName: "mmap_lock" });
  setFieldLock(mmNode.data, "pgd", { kind: "spinlock", lockName: "page_table_lock" });

  const vmaNode = createNode("struct", {
    x: 540, y: 80, w: 340, h: 300, version, repo, color: "purple",
    locks: [
      { kind: "rwsem", lockName: "mmap_lock", note: "Read-locked for traversal, write-locked for split/merge" },
      { kind: "rcu", lockName: "vma_lock", note: "Per-VMA lock for speculative page fault fastpath" },
    ],
    data: { name: "vm_area_struct", kind: "struct", path: "include/linux/mm_types.h" },
  });
  setFieldLock(vmaNode.data, "vm_start", { kind: "rwsem", lockName: "mmap_lock" });
  setFieldLock(vmaNode.data, "vm_end", { kind: "rwsem", lockName: "mmap_lock" });
  setFieldLock(vmaNode.data, "vm_flags", { kind: "rwsem", lockName: "mmap_lock" });

  const asNode = createNode("struct", {
    x: 1000, y: 80, w: 340, h: 300, version, repo, color: "cyan",
    locks: [
      { kind: "rwsem", lockName: "i_mmap_rwsem", note: "Protects interval tree of shared VMAs" },
      { kind: "spinlock", lockName: "i_pages.xa_lock", note: "Protects page-cache XArray entries" },
    ],
    data: { name: "address_space", kind: "struct", path: "include/linux/fs.h" },
  });
  setFieldLock(asNode.data, "i_mmap", { kind: "rwsem", lockName: "i_mmap_rwsem" });

  const noteNode = createNode("note", {
    x: 540, y: 440, w: 420, h: 210, version, repo, color: "yellow",
    locks: [
      { kind: "lock-order", lockName: "mmap_lock -> i_mmap_rwsem -> page_table_lock", note: "Never acquire mmap_lock while holding i_mmap_rwsem" },
    ],
    customTitle: "Lock Ordering & Concurrency Invariants",
    data: {
      markdown: true,
      text: "### Concurrency Rules\n- `mmap_lock` (rwsem) guards `mm_struct` VMA layout mutations.\n- `i_mmap_rwsem` nests **inside** `mmap_lock`.\n- `page_table_lock` (spinlock) is innermost during PTE updates.\n- RCU / per-VMA locks protect lockless fault lookup paths.",
    },
  });

  const e1 = createEdge({ from: mmNode.id, to: vmaNode.id, fromField: "mm_mt", lockKind: "rwsem" });
  applyEdgePreset(e1, "locks");
  const e2 = createEdge({ from: vmaNode.id, to: asNode.id, fromField: "vm_file", lockKind: "rwsem" });
  applyEdgePreset(e2, "locks");
  const e3 = createEdge({ from: noteNode.id, to: mmNode.id, lockKind: "lock-order" });
  applyEdgePreset(e3, "locks");

  return normalizeCanvas({
    ...emptyCanvas(),
    nodes: [mmNode, vmaNode, asNode, noteNode],
    edges: [e1, e2, e3],
    tours: [
      mkTour("Struct & Lock Ownership Walkthrough", [
        {
          title: "1. Root Struct & Field Locks",
          caption: "Inspect mmap_lock and page_table_lock annotations on mm_struct fields.",
          nodeIds: [mmNode.id],
          edgeIds: [],
          viewport: { x: 40, y: 0, zoom: 0.9 },
        },
        {
          title: "2. Protected Child Structs",
          caption: "Follow lock-annotated edges to vm_area_struct and address_space.",
          nodeIds: [mmNode.id, vmaNode.id, asNode.id],
          edgeIds: [e1.id, e2.id],
          viewport: { x: -320, y: 0, zoom: 0.85 },
        },
        {
          title: "3. Lock Ordering Invariants",
          caption: "Verify the canonical lock hierarchy documented in the invariant note.",
          nodeIds: [noteNode.id, mmNode.id, vmaNode.id],
          edgeIds: [e3.id],
          viewport: { x: -160, y: -140, zoom: 0.85 },
        },
      ]),
    ],
  });
}

function buildSubsystemOverviewTemplate(version, repo) {
  const layerGov = { id: newId("lyr"), name: "Governance & Docs", visible: true, color: "yellow" };
  const layerApi = { id: newId("lyr"), name: "API & Internals", visible: true, color: "blue" };

  const gGov = createNode("group", {
    x: 60, y: 80, w: 460, h: 640, version, repo, color: "yellow", layerId: layerGov.id,
    customTitle: "Ownership & Kconfig",
  });
  const gApi = createNode("group", {
    x: 580, y: 80, w: 540, h: 640, version, repo, color: "cyan", layerId: layerApi.id,
    customTitle: "Documentation & Kernel API Boundary",
  });

  const innerStruct = createNode("struct", {
    x: 100, y: 120, w: 320, h: 280, version, repo, color: "purple",
    data: { name: "page", kind: "struct", path: "include/linux/mm_types.h" },
  });
  const innerSource = createNode("source", {
    x: 500, y: 120, w: 480, h: 280, version, repo, color: "blue",
    data: { path: "mm/page_alloc.c", mode: "file" },
  });
  const innerEdge = createEdge({ from: innerSource.id, to: innerStruct.id, label: "points to", color: "purple", arrow: "forward" });
  const subCanvas = normalizeCanvas({
    ...emptyCanvas(),
    nodes: [innerStruct, innerSource],
    edges: [innerEdge],
  });

  const gPortal = createNode("group", {
    x: 1180, y: 80, w: 400, h: 340, version, repo, color: "purple", layerId: layerApi.id,
    customTitle: "Core Internals (Sub-Map Portal)",
    data: { subCanvas },
  });

  const maintNode = createNode("maintainer", {
    x: 90, y: 130, w: 400, h: 270, version, repo, groupId: gGov.id, layerId: layerGov.id, color: "yellow",
    data: { query: "MEMORY MANAGEMENT", title: "MEMORY MANAGEMENT" },
  });
  const kcNode = createNode("kconfig", {
    x: 90, y: 430, w: 400, h: 260, version, repo, groupId: gGov.id, layerId: layerGov.id, color: "orange",
    data: { symbol: "MMU", arch: "x86_64" },
  });

  const docNode = createNode("doc", {
    x: 610, y: 130, w: 480, h: 270, version, repo, groupId: gApi.id, layerId: layerGov.id, color: "green",
    data: { path: "Documentation/mm/index.rst", mode: "auto" },
  });
  const kapiNode = createNode("kapi", {
    x: 610, y: 430, w: 480, h: 260, version, repo, groupId: gApi.id, layerId: layerApi.id, color: "cyan",
    data: { catalog: "exports", query: "alloc_pages", arch: "x86_64" },
  });

  const noteNode = createNode("note", {
    x: 1180, y: 460, w: 400, h: 220, version, repo, layerId: layerApi.id, color: "purple",
    customTitle: "Sub-Map Portal Guide",
    data: {
      markdown: true,
      text: "### Layered Subsystem Map\n- Use **Visibility Layers** in the Inspector to isolate governance vs internals.\n- Enter the **Core Internals** group portal to inspect nested structs and source slices.",
    },
  });

  const e1 = createEdge({ from: maintNode.id, to: kcNode.id, label: "owns", color: "yellow", arrow: "forward" });
  const e2 = createEdge({ from: kcNode.id, to: kapiNode.id, label: "depends on", color: "orange", arrow: "forward" });
  const e3 = createEdge({ from: docNode.id, to: kapiNode.id, label: "documents", color: "green", style: "dashed", arrow: "forward" });
  const e4 = createEdge({ from: kapiNode.id, to: gPortal.id, label: "backed by", color: "purple", arrow: "forward" });

  return normalizeCanvas({
    ...emptyCanvas(),
    nodes: [gGov, gApi, gPortal, maintNode, kcNode, docNode, kapiNode, noteNode],
    edges: [e1, e2, e3, e4],
    settings: {
      ...emptyCanvas().settings,
      layers: [layerGov, layerApi],
    },
    tours: [
      mkTour("Subsystem Overview Walkthrough", [
        {
          title: "1. Maintainer & Kconfig Ownership",
          caption: "Review MAINTAINERS responsibility and controlling Kconfig options.",
          nodeIds: [gGov.id, maintNode.id, kcNode.id],
          edgeIds: [e1.id],
          viewport: { x: 40, y: -20, zoom: 0.85 },
        },
        {
          title: "2. Documentation & Kernel API Boundary",
          caption: "Cross-check subsystem .rst docs with exported Kernel API symbols.",
          nodeIds: [gApi.id, docNode.id, kapiNode.id],
          edgeIds: [e2.id, e3.id],
          viewport: { x: -420, y: -20, zoom: 0.85 },
        },
        {
          title: "3. Core Internals Sub-Map Portal",
          caption: "Drill into the Core Internals group portal to explore underlying structs and code.",
          nodeIds: [gPortal.id, noteNode.id],
          edgeIds: [e4.id],
          viewport: { x: -820, y: -20, zoom: 0.85 },
        },
      ]),
    ],
  });
}

export function buildKernelTemplate(templateId, { version = "v3.0", repo = "linux" } = {}) {
  const v = String(version || "v3.0");
  const r = String(repo || "linux");
  switch (templateId) {
    case "syscall-flow":
      return buildSyscallFlowTemplate(v, r);
    case "cve-root-cause":
      return buildCveRootCauseTemplate(v, r);
    case "struct-locking":
      return buildStructLockingTemplate(v, r);
    case "subsystem-overview":
      return buildSubsystemOverviewTemplate(v, r);
    default:
      return null;
  }
}
