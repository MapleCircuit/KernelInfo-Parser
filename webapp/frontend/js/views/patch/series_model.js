/**
 * series_model.js - Patch series drafts (P3, webapp/API.md §5.33): staged file edits grouped into ordered patches.
 *
 * A draft (session state `patchDrafts`, shared by every tab of the session):
 *   { base, author: {name, email}, edits: {path: {originalBlob, modified}},
 *     series: [{id, subject, body, files: [path]}], cover: {subject, body}, to: [], cc: [], version }
 * Each staged file belongs to exactly one patch. Every function returns a new draft (no mutation).
 */

export function emptyDraft(base = "") {
  return { base, author: { name: "", email: "" }, edits: {}, series: [], cover: { subject: "", body: "" }, to: [], cc: [], version: 1 };
}

let seq = 0;
const newId = () => `p${Date.now().toString(36)}${(seq++).toString(36)}`;

/** The blob id `git hash-object` gives a text (UTF-8), computed in the browser. */
export async function gitBlobSha(text) {
  const body = new TextEncoder().encode(text);
  const head = new TextEncoder().encode(`blob ${body.length}\0`);
  const all = new Uint8Array(head.length + body.length);
  all.set(head, 0);
  all.set(body, head.length);
  const digest = await crypto.subtle.digest("SHA-1", all);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Stage an edit. A draft on another base cannot take it (`{error}`); a file edited again keeps its patch;
 * a new file goes to `patchId`, else the last patch, else a new one named from the subject.
 */
export function addEdit(draft, { base, path, originalBlob, modified, subject = "", body = "", patchId = "" }) {
  const d = structuredClone(draft || emptyDraft(base));
  if (!d.base) d.base = base;
  if (d.base !== base && Object.keys(d.edits).length) return { error: `The series is based on ${d.base}; this file is at ${base}` };
  d.base = base;
  d.edits[path] = { originalBlob, modified };
  if (d.series.some((p) => p.files.includes(path))) return { draft: d };
  let target = d.series.find((p) => p.id === patchId) || (subject ? null : d.series[d.series.length - 1]);
  if (!target) {
    target = { id: newId(), subject: subject || defaultSubject(path), body, files: [] };
    d.series.push(target);
  }
  target.files.push(path);
  return { draft: d };
}

export function defaultSubject(path) {
  const parts = path.split("/");
  return `${parts.length > 1 ? parts[parts.length - 2] : parts[0]}: `;
}

export function removeEdit(draft, path) {
  const d = structuredClone(draft);
  delete d.edits[path];
  for (const p of d.series) p.files = p.files.filter((f) => f !== path);
  return d;
}

/** Move a file to another patch (each file belongs to exactly one). */
export function assignFile(draft, path, patchId) {
  const d = structuredClone(draft);
  if (!d.edits[path] || !d.series.some((p) => p.id === patchId)) return d;
  for (const p of d.series) p.files = p.files.filter((f) => f !== path);
  d.series.find((p) => p.id === patchId).files.push(path);
  return d;
}

export function addPatch(draft, subject = "") {
  const d = structuredClone(draft);
  d.series.push({ id: newId(), subject, body: "", files: [] });
  return d;
}

/** Remove a patch; its files go to the previous patch (or the next one). The last patch cannot go. */
export function removePatch(draft, patchId) {
  const d = structuredClone(draft);
  const idx = d.series.findIndex((p) => p.id === patchId);
  if (idx < 0 || d.series.length === 1) return d;
  const [gone] = d.series.splice(idx, 1);
  const heir = d.series[Math.max(0, idx - 1)];
  heir.files.push(...gone.files);
  return d;
}

export function movePatch(draft, patchId, delta) {
  const d = structuredClone(draft);
  const idx = d.series.findIndex((p) => p.id === patchId);
  const to = idx + delta;
  if (idx < 0 || to < 0 || to >= d.series.length) return d;
  const [p] = d.series.splice(idx, 1);
  d.series.splice(to, 0, p);
  return d;
}

export function updatePatch(draft, patchId, fields) {
  const d = structuredClone(draft);
  const p = d.series.find((x) => x.id === patchId);
  if (p) Object.assign(p, fields);
  return d;
}

/** Problems that keep the series from being generated (empty list: ready). */
export function draftProblems(draft) {
  const out = [];
  if (!draft || !Object.keys(draft.edits || {}).length) out.push("No staged files");
  if (!String(draft?.author?.name || "").trim() || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(draft?.author?.email || "").trim())) out.push("Author name and email are needed");
  for (const [i, p] of (draft?.series || []).entries()) {
    const subj = String(p.subject || "").trim();
    if (!subj || /:\s*$/.test(subj)) out.push(`Patch ${i + 1} needs a subject`);
    if (!p.files.length) out.push(`Patch ${i + 1} changes no file`);
  }
  return out;
}

/** The body of `POST /api/version/{base}/patch/series`. */
export function seriesPayload(draft) {
  const n = draft.series.filter((p) => p.files.length).length;
  return {
    base_commit: draft.base,
    author: { name: draft.author.name.trim(), email: draft.author.email.trim() },
    to: draft.to.filter(Boolean),
    cc: draft.cc.filter(Boolean),
    version: draft.version || 1,
    cover: n > 1 ? { subject: draft.cover.subject, body: draft.cover.body } : null,
    patches: draft.series.filter((p) => p.files.length).map((p) => ({
      subject: p.subject,
      body: p.body,
      files: p.files.map((path) => ({ path, original_blob: draft.edits[path].originalBlob, modified_content: draft.edits[path].modified })),
    })),
  };
}

/** Parse a To/Cc text area (one address per line or comma separated). */
export function parseAddresses(text) {
  return String(text || "").split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
}
