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
  const list = Array.isArray(doc) ? doc : doc?.format === EXPORT_FORMAT ? doc.records : null;
  if (!Array.isArray(list)) throw new Error("Not a KernelInfo user data export");
  return list.filter((r) => r && typeof r === "object" && USER_KINDS.includes(r.kind) && typeof r.id === "string" && r.id && "data" in r)
    .map((r) => ({ ...r, repo: r.repo || "", key: userKey(r.repo, r.kind, r.id) }));
}

/**
 * What importing `incoming` into `existing` (Map key -> record) writes: mode `skip-existing` keeps what is there,
 * `overwrite` replaces it, `copy` imports every record under a new id. -> {put: [...], skipped}
 */
export function mergeImport(existing, incoming, mode = "skip-existing", now = Date.now()) {
  const put = [];
  let skipped = 0;
  for (const r of incoming) {
    if (mode === "copy") {
      put.push(makeRecord(r.kind, r.repo, r.data, { now, created: r.created_at ?? now }));
    } else if (existing.has(r.key) && mode !== "overwrite") {
      skipped++;
    } else {
      put.push({ ...r, updated_at: r.updated_at ?? now, created_at: r.created_at ?? now });
    }
  }
  return { put, skipped };
}
