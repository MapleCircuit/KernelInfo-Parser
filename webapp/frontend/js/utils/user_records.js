/**
 * user_records.js - Records of the `user` store (S06, webapp/API.md §5.5): bookmarks, notes, tours, watches.
 * Pure helpers: keys, record shape, export documents and import merging.
 */
export const USER_KINDS = ["bookmark", "note", "tour", "watch", "watch-state"];
export const EXPORT_FORMAT = "kip-user";

export const userKey = (repo, kind, id) => `${repo || ""}::${kind}::${id}`;

let seq = 0;
export function newId() {
  return `${Date.now().toString(36)}${(seq++).toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

/** A record for `data` (a new id unless `id` is given); `now` for tests. */
export function makeRecord(kind, repo, data, { id = newId(), now = Date.now(), created = null } = {}) {
  if (!USER_KINDS.includes(kind)) throw new Error(`Unknown user record kind: ${kind}`);
  return { key: userKey(repo, kind, id), kind, repo: repo || "", id, created_at: created ?? now, updated_at: now, data };
}

/** Records of `kinds` (all when empty) and of `repo` (all when null). */
export function filterRecords(records, { kinds = [], repo = null } = {}) {
  return records.filter((r) => (!kinds.length || kinds.includes(r.kind)) && (repo === null || r.repo === repo));
}

export function exportDocument(records, now = Date.now()) {
  return { format: EXPORT_FORMAT, version: 1, exported_at: new Date(now).toISOString(), records };
}

/** Valid records of an export document (or a bare array); throws on another format. */
export function importRecords(doc) {
  if (!Array.isArray(doc) && doc && typeof doc === "object") {
    if (doc.format === "kip-session") {
      throw new Error("This is a session export — import it from Workspace Sessions → Import…");
    }
    if (doc.format === "kiptour") {
      throw new Error("This is a code tour export — import it from the Bookmarks & Tours tab");
    }
  }
  const list = Array.isArray(doc) ? doc : doc?.format === EXPORT_FORMAT ? doc.records : null;
  if (!Array.isArray(list)) throw new Error("Not a KernelInfo user data export");
  return list
    .filter((r) => r && typeof r === "object" && USER_KINDS.includes(r.kind) && typeof r.id === "string" && r.id && r.data && typeof r.data === "object" && !Array.isArray(r.data))
    .map((r) => ({ ...r, repo: r.repo || "", key: userKey(r.repo, r.kind, r.id) }));
}

/**
 * What importing `incoming` into `existing` (Map key -> record) writes: mode `skip-existing` keeps what is there,
 * `overwrite` replaces it, `copy` imports every record under a new id. -> {put: [...], skipped}
 */
export function mergeImport(existing, incoming, mode = "skip-existing", now = Date.now()) {
  const put = [];
  let skipped = 0;
  const seen = new Set(existing.keys());
  for (const r of incoming) {
    if (mode === "copy") {
      put.push(makeRecord(r.kind, r.repo, r.data, { now, created: r.created_at ?? now }));
    } else if (seen.has(r.key) && mode !== "overwrite") {
      skipped++;
    } else {
      seen.add(r.key);
      put.push({ ...r, updated_at: r.updated_at ?? now, created_at: r.created_at ?? now });
    }
  }
  return { put, skipped };
}

/** Parse JSON text of a user data export and validate its records. */
export function parseUserDataText(text) {
  let doc;
  try {
    doc = JSON.parse(String(text || "").replace(/^\uFEFF/, ""));
  } catch {
    throw new Error("Not a valid JSON file");
  }
  const incoming = importRecords(doc);
  const rawList = Array.isArray(doc) ? doc : doc.records;
  if (rawList.length > 0 && incoming.length === 0) {
    throw new Error("No valid user records found in this export");
  }
  return incoming;
}

/** Import a user data export JSON string into `db` (`localDB`); operates locally without network access. */
export async function importUserDataText(text, db) {
  const incoming = parseUserDataText(text);
  await db.ready;
  if ("db" in db && !db.db) throw new Error("Local browser storage (IndexedDB) is unavailable");
  const existing = new Map((await db.userList()).map((r) => [r.key, r]));
  const { put, skipped } = mergeImport(existing, incoming, "skip-existing");
  for (const r of put) {
    const ok = await db.userPut(r);
    if (!ok) throw new Error("Could not save user data to local browser storage");
  }
  return { imported: put.length, skipped };
}
