/**
 * bookmark_store.js - Bookmarks, notes and tours in the `user` store (G05 on S06), and opening their targets.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { localDB } from "../../db.js";
import { makeRecord, userKey } from "../../utils/user_records.js";
import { sameTarget, targetLabel, validTarget, validateTour } from "./bookmark_model.js";

export const repoName = () => api.repo || "";

export async function listBookmarks() {
  return localDB.userList("bookmark", repoName());
}

/** Add a bookmark, or remove it when the same place is already bookmarked (toggle). -> {added, record} */
export async function toggleBookmark(target, { label = "", group = "" } = {}) {
  if (!validTarget(target)) throw new Error("This place cannot be bookmarked");
  const existing = (await listBookmarks()).find((r) => sameTarget(r.data.target, target));
  if (existing) {
    await removeBookmark(existing.key);
    return { added: false, record: existing };
  }
  const count = (await listBookmarks()).length;
  const rec = makeRecord("bookmark", repoName(), { target, label: label || targetLabel(target), group, order: count });
  await localDB.userPut(rec);
  return { added: true, record: rec };
}

export async function updateBookmark(rec, fields) {
  const next = { ...rec, updated_at: Date.now(), data: { ...rec.data, ...fields } };
  await localDB.userPut(next);
  return next;
}

/** Remove a bookmark and its note. */
export async function removeBookmark(key) {
  await localDB.userDelete(key);
  const note = await getNote(key);
  if (note) await localDB.userDelete(note.key);
}

const noteKey = (bookmarkKey) => userKey(repoName(), "note", `for-${bookmarkKey}`);

export async function getNote(bookmarkKey) {
  return localDB.userGet(noteKey(bookmarkKey));
}

/** Set (or with empty text, remove) the Markdown note of a bookmark. */
export async function setNote(bookmarkKey, markdown) {
  const key = noteKey(bookmarkKey);
  if (!String(markdown || "").trim()) return localDB.userDelete(key);
  const old = await localDB.userGet(key);
  const rec = makeRecord("note", repoName(), { bookmark_key: bookmarkKey, markdown: String(markdown).slice(0, 50000) },
    { id: `for-${bookmarkKey}`, created: old?.created_at });
  return localDB.userPut(rec);
}

export async function listTours() {
  return localDB.userList("tour", repoName());
}

export async function saveTour(tour, rec = null) {
  const data = validateTour(tour);
  const next = rec ? { ...rec, updated_at: Date.now(), data } : makeRecord("tour", repoName(), data);
  await localDB.userPut(next);
  return next;
}

export async function deleteTour(key) {
  return localDB.userDelete(key);
}

/** Open a target in its tab type (a line at its pinned commit). */
export async function openTarget(t, { forceNew = false } = {}) {
  if (!validTarget(t)) return false;
  switch (t.type) {
    case "line":
      state.openTab({ type: "code", title: t.path.split("/").pop(), path: t.path, cursorLine: t.line, version: t.sha || t.rev || state.currentVersion, forceNew });
      return true;
    case "symbol": {
      const { openDefinition } = await import("../../utils/definition_picker.js");
      return openDefinition(t.rev || state.currentVersion, t.name, { forceNew });
    }
    case "kconfig":
      state.openTab({ type: "kconfig", title: `KConfig (${t.arch || "x86_64"})`, arch: t.arch || "x86_64", symbol: String(t.name).replace(/^CONFIG_/, ""), version: t.rev || state.currentVersion, forceNew });
      return true;
    case "commit":
      state.openTab({ type: "commits", title: `Commit ${t.hash.slice(0, 7)}`, commitId: t.hash, listCollapsed: true, forceNew });
      return true;
    case "mail":
      state.openTab({ type: "mail", title: "Mailing List", threadId: t.tid || 0, messageId: t.mid || "", listMode: "tree", forceNew });
      return true;
    default:
      return false;
  }
}

/** The bookmark target of the active tab (commit, mail thread, Kconfig option), or null. */
export function targetOfTab(tab) {
  if (!tab) return null;
  if (tab.type === "commits" && tab.commitId) return { type: "commit", hash: tab.commitId };
  if (tab.type === "mail" && (tab.threadId || tab.messageId)) return { type: "mail", tid: tab.threadId || 0, mid: tab.messageId || "" };
  if (tab.type === "kconfig" && tab.symbol) return { type: "kconfig", name: tab.symbol, arch: tab.arch, rev: tab.version };
  return null;
}
