/**
 * impact_map.js - Automated Patch / Commit Impact Maps for NodeMap.
 * Parses unified diffs or structured commit diffs to construct a 3-column layered NodeMap canvas
 * (Changes -> Touched Data & Symbols -> Blast Radius & Ownership).
 *
 * DOM-free (unit tested).
 */
import { createEdge, createNode, emptyCanvas, newId, normalizeCanvas } from "./canvas_model.js";
import { parseUnifiedDiff } from "./domain_nodes.js";

const MAX_EXTRACTED_STRUCTS = 24;
const MAX_EXTRACTED_FUNCTIONS = 24;
const MAX_STRUCT_NODES = 10;
const MAX_FUNCTION_NODES = 12;
const MAX_CALLERS_PER_FN = 4;
const MAX_TOTAL_CALLER_NODES = 16;
const MAX_MAINTAINER_NODES = 6;
const MAX_CALLGRAPH_QUERIES = 5;

const STRUCT_RE = /\b(?:struct|union)\s+([A-Za-z_]\w*)\b/g;
const HEADING_FN_RE = /([A-Za-z_]\w*)\s*\(/;
const SIG_LINE_RE = /^[a-zA-Z_][\w\s\*]*\b([A-Za-z_]\w*)\s*\([^;]*$/;

const NON_FUNC_IDENTIFIERS = new Set([
  "if", "else", "for", "while", "do", "switch", "case", "return", "sizeof", "typeof",
  "__typeof__", "alignof", "__alignof__", "defined", "__attribute__", "__asm__", "asm",
  "struct", "union", "enum", "typedef", "static", "inline", "const", "volatile", "extern",
  "likely", "unlikely", "BUG_ON", "WARN_ON", "WARN_ON_ONCE", "BUILD_BUG_ON",
  "EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL", "MODULE_LICENSE", "MODULE_AUTHOR", "MODULE_DESCRIPTION",
]);

const NON_STRUCT_NAMES = new Set([
  "const", "volatile", "static", "inline", "restrict", "unsigned", "signed",
  "void", "int", "char", "long", "short", "if", "else", "for", "while", "return", "sizeof",
]);

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

function pushUnique(list, seen, value, max) {
  if (!value || seen.has(value) || list.length >= max) return;
  seen.add(value);
  list.push(value);
}

function normalizeInputFile(raw) {
  if (typeof raw === "string") {
    const path = raw.trim();
    return path ? { path, oldPath: path, status: "M", additions: 0, deletions: 0, hunks: [] } : null;
  }
  if (!isObj(raw)) return null;
  const path = String(raw.path || raw.new_path || raw.old_path || raw.oldPath || "").trim();
  if (!path) return null;
  const oldPath = String(raw.oldPath || raw.old_path || path).trim() || path;
  const status = String(raw.status || raw.change_type || "M").trim() || "M";
  const hunks = Array.isArray(raw.hunks) ? raw.hunks : [];
  let additions = Number(raw.additions);
  let deletions = Number(raw.deletions);
  if (!Number.isFinite(additions) || !Number.isFinite(deletions)) {
    let add = 0;
    let del = 0;
    for (const h of hunks) {
      for (const l of Array.isArray(h?.lines) ? h.lines : []) {
        const k = Array.isArray(l) ? l[0] : (typeof l === "string" ? l[0] : "");
        if (k === "+") add += 1;
        else if (k === "-") del += 1;
      }
    }
    if (!Number.isFinite(additions)) additions = add;
    if (!Number.isFinite(deletions)) deletions = del;
  }
  return {
    path,
    oldPath,
    status,
    additions,
    deletions,
    versionA: typeof raw.versionA === "string" ? raw.versionA : "",
    versionB: typeof raw.versionB === "string" ? raw.versionB : "",
    hunks,
  };
}

function mergeNormalizedFiles(parsedFiles, structuredFiles) {
  const byPath = new Map();
  for (const raw of [...parsedFiles, ...structuredFiles]) {
    const f = normalizeInputFile(raw);
    if (!f) continue;
    const prev = byPath.get(f.path);
    if (!prev) {
      byPath.set(f.path, f);
      continue;
    }
    if (!prev.hunks.length && f.hunks.length) prev.hunks = f.hunks;
    if (!prev.additions && f.additions) prev.additions = f.additions;
    if (!prev.deletions && f.deletions) prev.deletions = f.deletions;
    if (prev.status === "M" && f.status !== "M") prev.status = f.status;
    if (!prev.versionA && f.versionA) prev.versionA = f.versionA;
    if (!prev.versionB && f.versionB) prev.versionB = f.versionB;
  }
  return [...byPath.values()];
}

function hunkHeadingText(h) {
  if (typeof h?.heading === "string" && h.heading.trim()) return h.heading.trim();
  const header = String(h?.header || "");
  const m = /^@@[^@]*@@\s*(.*)$/.exec(header);
  return m ? m[1].trim() : "";
}

function scanStructsInText(text, out, seen) {
  STRUCT_RE.lastIndex = 0;
  let m;
  while ((m = STRUCT_RE.exec(text)) !== null) {
    const name = m[1];
    if (!NON_STRUCT_NAMES.has(name)) pushUnique(out, seen, name, MAX_EXTRACTED_STRUCTS);
  }
}

function extractHeadingFunction(heading) {
  if (!heading) return "";
  const m = HEADING_FN_RE.exec(heading);
  if (!m) return "";
  const name = m[1];
  return NON_FUNC_IDENTIFIERS.has(name) ? "" : name;
}

function extractSignatureLineFunction(lineText) {
  const trimmed = String(lineText || "").trimEnd();
  if (!trimmed || /^\s/.test(trimmed)) return "";
  const m = SIG_LINE_RE.exec(trimmed);
  if (!m) return "";
  const name = m[1];
  return NON_FUNC_IDENTIFIERS.has(name) ? "" : name;
}

function analyzeDiffInternal({ diffText = "", files = [] } = {}) {
  const parsedFromText = typeof diffText === "string" && diffText.trim() ? parseUnifiedDiff(diffText) : [];
  const structured = Array.isArray(files) ? files : [];
  const merged = mergeNormalizedFiles(parsedFromText, structured);

  const structs = [];
  const seenStructs = new Set();
  const functions = [];
  const seenFuncs = new Set();
  const structFiles = new Map();
  const funcFiles = new Map();

  for (const f of merged) {
    for (const h of f.hunks) {
      const heading = hunkHeadingText(h);
      if (heading) {
        const beforeStructs = structs.length;
        scanStructsInText(heading, structs, seenStructs);
        for (let i = beforeStructs; i < structs.length; i++) structFiles.set(structs[i], f.path);

        const hFn = extractHeadingFunction(heading);
        if (hFn) {
          pushUnique(functions, seenFuncs, hFn, MAX_EXTRACTED_FUNCTIONS);
          if (!funcFiles.has(hFn)) funcFiles.set(hFn, f.path);
        }
      }

      for (const line of Array.isArray(h?.lines) ? h.lines : []) {
        let kind = "";
        let body = "";
        if (Array.isArray(line)) {
          kind = String(line[0] || "");
          body = String(line[3] ?? "");
        } else if (typeof line === "string" && line.length > 0) {
          if (line.startsWith("+++") || line.startsWith("---")) continue;
          kind = line[0];
          body = line.slice(1);
        }
        if (kind !== "+" && kind !== "-") continue;

        const beforeStructs = structs.length;
        scanStructsInText(body, structs, seenStructs);
        for (let i = beforeStructs; i < structs.length; i++) structFiles.set(structs[i], f.path);

        const sFn = extractSignatureLineFunction(body);
        if (sFn) {
          pushUnique(functions, seenFuncs, sFn, MAX_EXTRACTED_FUNCTIONS);
          if (!funcFiles.has(sFn)) funcFiles.set(sFn, f.path);
        }
      }
    }
  }

  return {
    files: merged.map((f) => ({
      path: f.path,
      oldPath: f.oldPath,
      status: f.status,
      additions: f.additions,
      deletions: f.deletions,
      versionA: f.versionA,
      versionB: f.versionB,
    })),
    structs,
    functions,
    structFiles,
    funcFiles,
  };
}

export function extractImpactFromDiff({ diffText = "", files = [] } = {}) {
  const res = analyzeDiffInternal({ diffText, files });
  return {
    files: res.files.map(({ path, oldPath, status, additions, deletions }) => ({
      path,
      oldPath,
      status,
      additions,
      deletions,
    })),
    structs: res.structs,
    functions: res.functions,
  };
}

function dedupeStrings(items) {
  const out = [];
  const seen = new Set();
  for (const x of Array.isArray(items) ? items : []) {
    const s = String(x || "").trim();
    if (!s || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

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

function normalizeMaintainerInput(raw) {
  if (typeof raw === "string") {
    const q = raw.trim();
    return q ? { secId: null, title: q, query: q, path: "" } : null;
  }
  if (!isObj(raw)) return null;
  const secId = Number.isInteger(raw.secId) && raw.secId > 0
    ? raw.secId
    : Number.isInteger(raw.sec_id) && raw.sec_id > 0 ? raw.sec_id : null;
  const query = String(raw.query || raw.name || "").trim();
  const path = String(raw.path || "").trim();
  const title = String(raw.title || raw.name || query || path || "").trim();
  if (!secId && !title && !query && !path) return null;
  return { secId, title, query: query || title, path };
}

export function buildImpactMap({
  version = "v3.0",
  repo = "linux",
  title = "Patch / Commit Impact Map",
  commitHash = "",
  files = [],
  diffText = "",
  structs = [],
  functions = [],
  callers = {},
  maintainers = [],
} = {}) {
  const v = String(version || "v3.0");
  const r = String(repo || "linux");
  const cleanCommit = /^[0-9a-f]{4,64}$/i.test(String(commitHash || "").trim())
    ? String(commitHash).trim().toLowerCase()
    : "";

  const analyzed = analyzeDiffInternal({ diffText, files });
  const mergedFiles = analyzed.files;
  const mergedStructs = dedupeStrings([...(Array.isArray(structs) ? structs : []), ...analyzed.structs]).slice(0, MAX_STRUCT_NODES);
  const mergedFunctions = dedupeStrings([...(Array.isArray(functions) ? functions : []), ...analyzed.functions]).slice(0, MAX_FUNCTION_NODES);

  const nodes = [];
  const edges = [];

  // Optional summary note card when title or commitHash is supplied
  const cleanTitle = String(title || "").trim();
  if (cleanTitle || cleanCommit) {
    const totalAdd = mergedFiles.reduce((s, f) => s + (Number(f.additions) || 0), 0);
    const totalDel = mergedFiles.reduce((s, f) => s + (Number(f.deletions) || 0), 0);
    const lines = [
      `### ${cleanTitle || "Patch / Commit Impact Map"}`,
      cleanCommit ? `- **Commit:** \`${cleanCommit.slice(0, 12)}\`` : "- **Scope:** Working patch / diff",
      `- **Changed:** ${mergedFiles.length} file(s) (+${totalAdd} / -${totalDel})`,
      `- **Touched:** ${mergedStructs.length} struct(s), ${mergedFunctions.length} function(s)`,
    ];
    for (const f of mergedFiles.slice(0, 4)) {
      lines.push(`  - \`${f.path}\` (${f.status} +${f.additions} / -${f.deletions})`);
    }
    if (mergedFiles.length > 4) lines.push(`  - … and ${mergedFiles.length - 4} more file(s)`);
    nodes.push(createNode("note", {
      x: 80,
      y: 20,
      w: 620,
      h: 130,
      version: v,
      repo: r,
      color: "yellow",
      customTitle: cleanTitle || `Commit Impact: ${cleanCommit.slice(0, 12)}`,
      data: { text: lines.join("\n"), markdown: true },
    }));
  }

  // Column groups (3-column layered layout: x=80, x=780, x=1220)
  const colY = 180;
  const padTop = 48;
  const padBottom = 28;
  const gapY = 24;

  const gChanges = createNode("group", {
    x: 80, y: colY, w: 620, h: 360, version: v, repo: r, color: "orange",
    customTitle: "Patch / Commit Changes",
  });
  const gTouched = createNode("group", {
    x: 780, y: colY, w: 380, h: 360, version: v, repo: r, color: "blue",
    customTitle: "Touched Data & Symbols",
  });
  const gBlast = createNode("group", {
    x: 1220, y: colY, w: 420, h: 360, version: v, repo: r, color: "green",
    customTitle: "Blast Radius & Ownership",
  });

  // Column 1: Changes (x=80 group, children at x=100)
  const changeNodes = [];
  const changeByPath = new Map();
  let curY = colY + padTop;
  const filesToRender = mergedFiles.slice(0, 16);
  for (const f of filesToRender) {
    const base = f.path.split("/").pop() || f.path;
    let cNode;
    if (cleanCommit) {
      cNode = createNode("diff", {
        x: 100, y: curY, w: 580, h: 240, version: v, repo: r, groupId: gChanges.id, color: "orange",
        data: { mode: "commit", commitHash: cleanCommit, path: f.path, oldPath: f.oldPath !== f.path ? f.oldPath : "" },
      });
    } else if (f.versionA && f.versionB) {
      cNode = createNode("diff", {
        x: 100, y: curY, w: 580, h: 240, version: v, repo: r, groupId: gChanges.id, color: "orange",
        data: { mode: "file", versionA: f.versionA, versionB: f.versionB, path: f.path, oldPath: f.oldPath !== f.path ? f.oldPath : "" },
      });
    } else {
      cNode = createNode("source", {
        x: 100, y: curY, w: 580, h: 220, version: v, repo: r, groupId: gChanges.id, color: "orange",
        customTitle: `Δ ${base}`,
        data: { mode: "file", path: f.path },
      });
    }
    changeNodes.push(cNode);
    changeByPath.set(f.path, cNode);
    curY += cNode.h + gapY;
  }
  if (!changeNodes.length && cleanCommit) {
    const cNode = createNode("diff", {
      x: 100, y: curY, w: 580, h: 260, version: v, repo: r, groupId: gChanges.id, color: "orange",
      data: { mode: "commit", commitHash: cleanCommit, path: "" },
    });
    changeNodes.push(cNode);
    curY += cNode.h + gapY;
  }
  gChanges.h = Math.max(240, curY - colY - gapY + padBottom);

  // Column 2: Touched Data & Symbols (x=780 group, children at x=800)
  const col2Nodes = [];
  const col2SymByName = new Map();
  const modEdges = [];
  curY = colY + padTop;

  for (const sName of mergedStructs) {
    const hintPath = analyzed.structFiles.get(sName) || "";
    const sNode = createNode("struct", {
      x: 800, y: curY, w: 340, h: 220, version: v, repo: r, groupId: gTouched.id, color: "purple",
      data: { name: sName, kind: "struct", hint: hintPath },
    });
    col2Nodes.push(sNode);
    curY += sNode.h + gapY;

    const srcChange = (hintPath && changeByPath.get(hintPath)) || changeNodes[0] || null;
    if (srcChange) {
      const e = createEdge({ from: srcChange.id, to: sNode.id, label: "modifies", color: "orange", arrow: "forward" });
      if (e) {
        edges.push(e);
        modEdges.push(e);
      }
    }
  }

  for (const fnName of mergedFunctions) {
    const hintPath = analyzed.funcFiles.get(fnName) || "";
    const fnNode = createNode("symbol", {
      x: 800, y: curY, w: 280, h: 110, version: v, repo: r, groupId: gTouched.id, color: "blue",
      data: { name: fnName, kind: "func" },
    });
    col2Nodes.push(fnNode);
    col2SymByName.set(fnName, fnNode);
    curY += fnNode.h + gapY;

    const srcChange = (hintPath && changeByPath.get(hintPath)) || changeNodes[0] || null;
    if (srcChange) {
      const e = createEdge({ from: srcChange.id, to: fnNode.id, label: "modifies", color: "orange", arrow: "forward" });
      if (e) {
        edges.push(e);
        modEdges.push(e);
      }
    }
  }
  gTouched.h = Math.max(240, curY - colY - gapY + padBottom);

  // Column 3: Blast Radius & Ownership (x=1220 group, children at x=1240)
  const col3Nodes = [];
  const col3CallerByName = new Map();
  const callEdges = [];
  const maintEdges = [];
  let callerCount = 0;
  curY = colY + padTop;

  const callersMap = isObj(callers) ? callers : {};
  const fnTargets = mergedFunctions.length ? mergedFunctions : Object.keys(callersMap);

  for (const fnName of fnTargets) {
    const calleeNode = col2SymByName.get(fnName);
    if (!calleeNode) continue;
    const rawList = Array.isArray(callersMap[fnName]) ? callersMap[fnName] : [];
    let perFn = 0;
    for (const raw of rawList) {
      if (perFn >= MAX_CALLERS_PER_FN) break;
      const callerName = typeof raw === "string"
        ? raw.trim()
        : String(raw?.name || raw?.function || raw?.caller || "").trim();
      if (!callerName || callerName === fnName) continue;

      let callerNode = col2SymByName.get(callerName) || col3CallerByName.get(callerName) || null;
      if (!callerNode) {
        if (callerCount >= MAX_TOTAL_CALLER_NODES) continue;
        callerNode = createNode("symbol", {
          x: 1240, y: curY, w: 280, h: 110, version: v, repo: r, groupId: gBlast.id, color: "blue",
          data: { name: callerName, kind: "func", astId: Number.isInteger(raw?.ast_id) ? raw.ast_id : null },
        });
        col3CallerByName.set(callerName, callerNode);
        col3Nodes.push(callerNode);
        callerCount += 1;
        curY += callerNode.h + gapY;
      }
      perFn += 1;

      if (!edges.some((x) => x.from === callerNode.id && x.to === calleeNode.id && x.label === "calls")) {
        const sitePath = isObj(raw) ? String(raw.call_site_path || raw.file_path || raw.path || "").trim() : "";
        const siteLine = isObj(raw) ? (Number(raw.call_site_line ?? raw.line ?? raw.line_no) || 1) : null;
        const e = createEdge({
          from: callerNode.id,
          to: calleeNode.id,
          label: "calls",
          color: "blue",
          arrow: "forward",
          callSite: sitePath ? { path: sitePath, line: siteLine } : null,
        });
        if (e) {
          edges.push(e);
          callEdges.push(e);
        }
      }
    }
  }

  const seenMaint = new Set();
  const maintList = [];
  for (const rawM of Array.isArray(maintainers) ? maintainers : []) {
    const m = normalizeMaintainerInput(rawM);
    if (!m) continue;
    const key = m.secId ? `id:${m.secId}` : `q:${(m.title || m.query || m.path).toLowerCase()}`;
    if (seenMaint.has(key)) continue;
    seenMaint.add(key);
    maintList.push(m);
    if (maintList.length >= MAX_MAINTAINER_NODES) break;
  }

  for (const m of maintList) {
    const mNode = createNode("maintainer", {
      x: 1240, y: curY, w: 380, h: 240, version: v, repo: r, groupId: gBlast.id, color: "green",
      data: m,
    });
    col3Nodes.push(mNode);
    curY += mNode.h + gapY;

    const fromId = gChanges.id || changeNodes[0]?.id;
    if (fromId) {
      const e = createEdge({ from: fromId, to: mNode.id, label: "maintains", color: "green", style: "dashed", arrow: "forward" });
      if (e) {
        edges.push(e);
        maintEdges.push(e);
      }
    }
  }
  gBlast.h = Math.max(240, curY - colY - gapY + padBottom);

  nodes.push(gChanges, gTouched, gBlast, ...changeNodes, ...col2Nodes, ...col3Nodes);

  const tours = [
    mkTour("Impact Walkthrough", [
      {
        title: "1. Changes",
        caption: `${mergedFiles.length} modified file(s) included in this patch or commit.`,
        nodeIds: [gChanges.id, ...changeNodes.map((n) => n.id)],
        edgeIds: [],
        viewport: { x: 20, y: 0, zoom: 0.85 },
      },
      {
        title: "2. Touched Data & Symbols",
        caption: `${mergedStructs.length} struct(s) and ${mergedFunctions.length} function(s) impacted directly.`,
        nodeIds: [gTouched.id, ...col2Nodes.map((n) => n.id)],
        edgeIds: modEdges.map((e) => e.id),
        viewport: { x: -480, y: 0, zoom: 0.85 },
      },
      {
        title: "3. Blast Radius & Ownership",
        caption: `${col3CallerByName.size} caller(s) and ${maintList.length} maintainer section(s) affected.`,
        nodeIds: [gBlast.id, ...col3Nodes.map((n) => n.id)],
        edgeIds: [...callEdges.map((e) => e.id), ...maintEdges.map((e) => e.id)],
        viewport: { x: -860, y: 0, zoom: 0.85 },
      },
    ]),
  ];

  return normalizeCanvas({
    ...emptyCanvas(),
    nodes,
    edges,
    tours,
  });
}

export async function generateImpactMap({
  api,
  version = "v3.0",
  repo = "linux",
  title = "",
  commitHash = "",
  files = [],
  diffText = "",
} = {}) {
  let effectiveFiles = Array.isArray(files) ? files : [];
  const cleanCommit = String(commitHash || "").trim();

  if (cleanCommit && !effectiveFiles.length && typeof api?.getCommitDiff === "function") {
    try {
      const diffRes = await api.getCommitDiff(version, cleanCommit);
      if (Array.isArray(diffRes?.files)) effectiveFiles = diffRes.files;
    } catch {
      effectiveFiles = [];
    }
  }

  const extracted = extractImpactFromDiff({ diffText, files: effectiveFiles });
  const targetFns = extracted.functions.slice(0, MAX_CALLGRAPH_QUERIES);
  const touchedPaths = extracted.files.map((f) => f.path).filter(Boolean);

  const cgPromises = typeof api?.getCallgraph === "function"
    ? targetFns.map((fn) => api.getCallgraph(version, fn).then((res) => ({ fn, res })))
    : [];

  const maintPromise = (touchedPaths.length || diffText) && typeof api?.matchMaintainers === "function"
    ? api.matchMaintainers(version, diffText || "", touchedPaths)
    : null;

  const [cgSettled, maintSettled] = await Promise.all([
    Promise.allSettled(cgPromises),
    maintPromise ? Promise.allSettled([maintPromise]) : Promise.resolve([]),
  ]);

  const callers = {};
  for (const item of cgSettled) {
    if (item.status !== "fulfilled" || !item.value) continue;
    const { fn, res } = item.value;
    if (Array.isArray(res?.callers)) callers[fn] = res.callers;
  }

  const maintainers = [];
  if (maintSettled[0]?.status === "fulfilled" && isObj(maintSettled[0].value)) {
    const maintRes = maintSettled[0].value;
    const secIdByName = new Map();
    for (const fEntry of Array.isArray(maintRes.files) ? maintRes.files : []) {
      for (const s of Array.isArray(fEntry?.subsystems) ? fEntry.subsystems : []) {
        if (s?.name && Number.isInteger(s.sec_id) && s.sec_id > 0 && !secIdByName.has(s.name)) {
          secIdByName.set(s.name, s.sec_id);
        }
      }
    }
    for (const sub of Array.isArray(maintRes.subsystems) ? maintRes.subsystems : []) {
      const subName = typeof sub === "string" ? sub.trim() : String(sub?.name || sub?.title || "").trim();
      if (!subName) continue;
      const desc = { query: subName, title: subName };
      const secId = typeof sub === "object" && Number.isInteger(sub?.sec_id) ? sub.sec_id : secIdByName.get(subName);
      if (secId) desc.secId = secId;
      maintainers.push(desc);
    }
  }

  return buildImpactMap({
    version,
    repo,
    title: title || (cleanCommit ? `Commit Impact: ${cleanCommit.slice(0, 12)}` : "Patch Impact Map"),
    commitHash: cleanCommit,
    files: effectiveFiles,
    diffText,
    structs: extracted.structs,
    functions: extracted.functions,
    callers,
    maintainers,
  });
}
