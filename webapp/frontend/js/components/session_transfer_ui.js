/**
 * session_transfer_ui.js - Session export / import dialogs (G07, webapp/API.md §5.23).
 *
 * Export writes a `kip-session` JSON file (the session, the configurations it uses, optionally every saved
 * configuration and the user data). Import validates the file, then shows what it adds and asks, per
 * configuration that differs from the local one, whether to keep the local one (default), use the imported one
 * or import it as a copy; Close imports nothing (§5.0). With nothing to decide it imports at once.
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { localDB } from "../db.js";
import { sessionManager } from "../session_manager.js";
import { toast } from "./toast.js";
import { openModal, escapeHtml, downloadText } from "./modal.js";
import { buildSessionExport, planSessionImport, profileKeyOf, referencedProfileKeys, remapSessionRepos, resolveSessionImport, validateSessionFile } from "../utils/session_transfer.js";
import { importRecords, mergeImport } from "../utils/user_records.js";

export async function exportSessionDialog(sessionId) {
  if (sessionId === sessionManager.activeSessionId) state.flushState?.();
  const session = sessionManager.readSession(sessionId);
  if (!session) {
    toast.error("Session not found");
    return;
  }
  const profiles = sessionManager.getSharedKconfigProfiles();
  const used = referencedProfileKeys(session).filter((k) => k in profiles).length;
  const userCount = (await localDB.userList()).length;
  const modal = openModal({ title: `Export session “${session.name}”`, subtitle: "A JSON file you can import in another browser or give to a colleague", width: "520px",
    footerHtml: `<button class="code-btn active" id="sx-go" type="button">Export</button>` });
  modal.body.innerHTML = `<div class="sx-opts">
    <div>${(session.panes || []).reduce((n, p) => n + (p.tabs?.length || 0), 0)} tabs, ${used} configuration${used === 1 ? "" : "s"} they use (and their toolchain profiles).</div>
    <label><input type="checkbox" id="sx-all" /> Include every saved configuration (${Object.keys(profiles).length})</label>
    <label><input type="checkbox" id="sx-user" ${userCount ? "" : "disabled"} /> Include bookmarks, notes and tours (${userCount})</label></div>`;
  modal.footer.querySelector("#sx-go").onclick = async () => {
    const doc = buildSessionExport({
      session, profiles, toolchains: sessionManager.getSharedKconfigToolchains(),
      includeAll: modal.body.querySelector("#sx-all").checked,
      userRecords: modal.body.querySelector("#sx-user").checked ? await localDB.userList() : null,
      build: window.KIP_BUILD || "", repo: session.repo || state.currentRepo || "",
    });
    const slug = (session.name || "session").replace(/[^\w.-]+/g, "_").slice(0, 40);
    downloadText(`kip-session-${slug}-${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(doc, null, 1), "application/json");
    modal.close();
    toast.success(`Session “${session.name}” exported`);
  };
}

export function importSessionDialog(onDone = null) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = async () => {
    const f = input.files?.[0];
    if (f) await importSessionText(await f.text(), onDone);
  };
  input.click();
}

function freeNameFactory(localProfiles) {
  const taken = new Set(Object.keys(localProfiles));
  return (version, arch) => {
    for (let n = 2; ; n++) {
      const name = `imported${n === 2 ? "" : ` ${n - 1}`}`;
      const key = profileKeyOf(version, arch, name);
      if (!taken.has(key)) {
        taken.add(key);
        return name;
      }
    }
  };
}

export async function importSessionText(text, onDone = null) {
  let imported;
  try {
    imported = validateSessionFile(text);
  } catch (err) {
    toast.error(`Cannot import: ${err.message}`);
    return;
  }
  // Every tab keeps its project; tabs of a repository this server does not have move to the active one
  const fallback = state.activeRepo() || "";
  let known = [];
  try {
    known = ((await api.getProjects?.())?.projects || []).flatMap((p) => [p.repo, p.slug].filter(Boolean));
  } catch {
    // projects unavailable: keep the exported names
  }
  const mapped = remapSessionRepos(imported.session.panes, { fileRepo: imported.repository, known, fallback });
  if (mapped.missing.length) {
    if (!confirm(`This session has tabs of ${mapped.missing.map((r) => `“${r}”`).join(", ")}, which this server does not have.\n\nOpen those tabs in “${fallback}” instead?`)) return;
  }
  imported.session.panes = mapped.panes;
  let repo = imported.repository;
  if (repo && known.length && !known.includes(repo)) repo = fallback;
  const localProfiles = sessionManager.getSharedKconfigProfiles();
  const plan = planSessionImport(imported, localProfiles);
  const tabCount = imported.session.panes.reduce((n, p) => n + p.tabs.length, 0);
  const apply = async (choices) => {
    const res = resolveSessionImport(imported, plan, choices, freeNameFactory(localProfiles));
    const { panes, idMap } = sessionManager.constructor.remapPaneTabIds(res.session.panes);
    const engineProfiles = {};
    for (const [oldId, d] of Object.entries(res.session.kconfigEngineProfiles)) if (idMap[oldId]) engineProfiles[idMap[oldId]] = { ...d };
    const names = new Set(sessionManager.getSessionsIndex().map((s) => s.name));
    let name = `${res.session.name} (imported)`;
    for (let n = 2; names.has(name); n++) name = `${res.session.name} (imported ${n})`;
    const created = sessionManager.createSession({
      name, repo, version: res.session.version || state.currentVersion, selectedBranch: res.session.selectedBranch,
      splitMode: res.session.splitMode, panes, pin: false,
      kconfigEngineTabIds: res.session.kconfigEngineTabIds.map((id) => idMap[id]).filter(Boolean), kconfigEngineProfiles: engineProfiles,
      kconfigProfiles: res.profiles, kconfigToolchains: res.toolchains,
    });
    let userNote = "";
    if (imported.user.length) {
      const existing = new Map((await localDB.userList()).map((r) => [r.key, r]));
      const { put, skipped } = mergeImport(existing, importRecords(imported.user), "skip-existing");
      for (const r of put) await localDB.userPut(r);
      userNote = `, ${put.length} user records${skipped ? ` (${skipped} already here)` : ""}`;
    }
    toast.success(`Imported session “${name}”: ${tabCount} tabs, ${Object.keys(res.profiles).length} configurations written${userNote}`);
    onDone?.(created);
  };
  if (!plan.conflicts.length) {
    await apply({});
    return;
  }
  const modal = openModal({ title: `Import session “${imported.session.name}”`, subtitle: `${tabCount} tabs · ${plan.adds.length} new configurations · ${plan.same.length} identical · ${plan.conflicts.length} differ from yours`,
    width: "760px", footerHtml: `<button class="code-btn active" id="si-go" type="button">Import</button>` });
  modal.body.innerHTML = `<div class="kconfig-muted" style="margin-bottom:8px;">The session is imported as a new session; nothing of yours changes unless you choose so below. Close imports nothing.</div>
    ${plan.adds.length ? `<div><strong>Added</strong>: ${plan.adds.map((k) => `<code>${escapeHtml(k)}</code>`).join(", ")}</div>` : ""}
    <table class="kconfig-cmp-table si-table"><thead><tr><th>Configuration</th><th>Yours</th><th>Imported</th><th>Use</th></tr></thead><tbody>
    ${plan.conflicts.map((c) => `<tr><td><code>${escapeHtml(c.key)}</code></td><td>${c.local} values</td><td>${c.imported} values</td>
      <td><select data-key="${escapeHtml(c.key)}"><option value="keep">Keep mine</option><option value="use">Use imported (replaces mine)</option><option value="copy">Import as a copy</option></select></td></tr>`).join("")}
    </tbody></table>`;
  modal.footer.querySelector("#si-go").onclick = async () => {
    const choices = Object.fromEntries([...modal.body.querySelectorAll("select[data-key]")].map((s) => [s.dataset.key, s.value]));
    modal.close();
    await apply(choices);
  };
}
