/**
 * watch_runtime.js - Watch list runtime (G10): stores watches in the `user` store (S06), checks them when
 * their branch moves (`versions:updated`, §2.11) or on demand (`POST /api/watch/changes`, §4.12), and keeps
 * the header bell's unread count. Imported once by app.js.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { localDB } from "../../db.js";
import { toast } from "../../components/toast.js";
import { makeRecord, userKey } from "../../utils/user_records.js";
import { pinnedSha } from "../code_view/permalink.js";
import { checkGroups, dueWatches, nextState, sameWatch, unreadCount, validWatchTarget, watchLabel } from "./watch_model.js";

const repo = () => api.repo || "";
const stateKey = (watchKey) => userKey(repo(), "watch-state", `for-${watchKey}`);

export async function listWatches() {
  return localDB.userList("watch", repo());
}

export async function watchStates() {
  const recs = await localDB.userList("watch-state", repo());
  return new Map(recs.map((r) => [r.data.watch_key, r.data]));
}

/** The branch a new watch follows: the header's branch scope, else the mainline branch (a tag never moves). */
function defaultBranch() {
  if (state.selectedBranch) return state.selectedBranch;
  const stamps = api.versionStamps || {};
  return ["master", "main"].find((b) => b in stamps) || state.currentVersion || "master";
}

/** Watch a target (again: stop watching). */
export async function toggleWatch(target, branch = defaultBranch()) {
  if (!validWatchTarget(target)) throw new Error("This cannot be watched");
  const existing = (await listWatches()).find((w) => sameWatch(w.data.target, target) && w.data.branch === branch);
  if (existing) {
    await removeWatch(existing.key);
    return { added: false, label: watchLabel(target) };
  }
  const base = pinnedSha(branch, api.versionStamps?.[branch]) || branch;
  await localDB.userPut(makeRecord("watch", repo(), { target, branch, base_sha: base }));
  return { added: true, label: watchLabel(target) };
}

export async function removeWatch(key) {
  await localDB.userDelete(key);
  await localDB.userDelete(stateKey(key));
}

async function saveState(watchKey, data) {
  const rec = makeRecord("watch-state", repo(), { watch_key: watchKey, ...data }, { id: `for-${watchKey}` });
  await localDB.userPut(rec);
}

export async function markRead(keys = null) {
  const states = await watchStates();
  for (const [k, s] of states) if (s.unread && (!keys || keys.includes(k))) await saveState(k, { ...s, unread: false });
}

/** Check watches (all, or those an update concerns). Returns the number of items that changed. */
export async function checkWatches(watches = null, { quiet = false } = {}) {
  const all = watches || (await listWatches());
  if (!all.length) return 0;
  const states = await watchStates();
  let changed = 0;
  for (const g of checkGroups(all, states)) {
    let res;
    try {
      res = await api.request("/api/watch/changes", {
        method: "POST", timeoutMs: 120000,
        body: JSON.stringify({ from: g.from, to: g.to, items: g.watches.map((w) => ({ key: w.key, ...w.data.target })) }),
      });
    } catch (err) {
      if (!quiet) toast.error(`Watch check ${g.to} failed: ${err.message}`);
      continue;
    }
    for (const item of res.items) {
      const next = nextState(states.get(item.key), item, res.to_sha);
      if (item.changed) changed++;
      await saveState(item.key, next);
    }
  }
  return changed;
}

/** The header bell: unread count, click opens the Watch tab. */
async function updateBell() {
  const btn = document.getElementById("watch-bell");
  if (!btn) return;
  const n = unreadCount(await watchStates());
  const count = btn.querySelector(".watch-bell-count");
  count.textContent = n ? String(n) : "";
  count.hidden = !n;
  btn.title = n ? `${n} watched item${n === 1 ? "" : "s"} changed — open the Watch list` : "Watch list: files, symbols, Kconfig options and MAINTAINERS sections you follow";
}

export function initWatchRuntime() {
  const btn = document.getElementById("watch-bell");
  if (btn) btn.onclick = () => state.openTab({ type: "watch", title: "Watch list" });
  localDB.onUserChanged((d) => {
    if (!d.kind || d.kind.startsWith("watch") || d.kind === "*") updateBell();
  });
  state.subscribe("versions:updated", async (ev) => {
    const due = dueWatches(await listWatches(), ev || {});
    if (!due.length) return;
    const n = await checkWatches(due, { quiet: true });
    if (n) {
      toast.info(`${n} watched item${n === 1 ? "" : "s"} changed in ${[...new Set(due.map((w) => w.data.branch))].join(", ")}`);
      if (document.hidden && localStorage.getItem("kip-watch-notify") === "1" && "Notification" in window && Notification.permission === "granted") {
        try {
          new Notification("KernelInfo watch list", { body: `${n} watched item${n === 1 ? "" : "s"} changed` });
        } catch {
          // notifications unavailable
        }
      }
    }
  });
  updateBell();
}
