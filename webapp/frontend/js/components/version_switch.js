/**
 * version_switch.js - Guarded revision switch ("Apply" in the header).
 *
 * The header's Tag, RC, Branch and Commit controls only *select* a revision (`state.selectedVersion`).
 * **Apply** moves the active tab to that revision through `requestVersionSwitch`, and **New Tab**
 * opens a repository explorer on it (`openVersionInNewTab`) — a tab's revision never changes on its
 * own, and links opened from a tab follow that tab's revision.
 *
 * Applying it to a KConfig tab also carries that tab's configuration over to the target revision and
 * drops the one saved for the revision it leaves, which reaches beyond the switch itself, so it is
 * confirmed first (see webapp/API.md section 5.0). Restoring a revision from the URL hash does not
 * prompt.
 */
import { state, kconfigProfileKey } from "../state.js";
import { openModal, escapeHtml } from "./modal.js";

/** Apply a revision to the active tab; true when it happened. */
export async function requestVersionSwitch(target) {
  const ref = typeof target === "string" ? target.trim() : "";
  if (!ref) return false;
  state.setSelectedVersion(ref);
  const tab = state.getActiveTab();
  if (!tab || tab.version === ref) return false;
  if (tab.type === "kconfig") {
    const plan = kconfigCarryPlan(tab, ref);
    if (!(await confirmKconfigVersionSwitch(tab, ref, plan))) return false;
    applyKconfigCarryPlan(tab, plan);
  }
  state.setVersion(ref);
  return true;
}

/** Open a repository explorer on the revision selected in the header ("New Tab"). */
export function openVersionInNewTab(version = state.selectedVersion) {
  const ref = typeof version === "string" ? version.trim() : "";
  if (!ref) return false;
  state.openTab({ type: "explorer", title: `Explorer ${ref}`, path: "", version: ref, forceNew: true });
  return true;
}

/**
 * What applying `target` to a KConfig tab does to its configuration: its values are carried over to
 * the target revision, the configuration saved for the revision it leaves is dropped, and the tab
 * takes a free name when the target already has a configuration under its own.
 */
function kconfigCarryPlan(tab, target) {
  const fromKey = state.kconfigProfileKeyOf(tab);
  const values = state.getKconfigProfile(fromKey) || {};
  const name = tab.configName || "";
  const taken = state.kconfigConfigNames(target, tab.arch, tab.id);
  const toName = taken.has(name) ? state.nextKconfigConfigName(target, tab.arch, tab.id) : name;
  return {
    fromKey,
    toKey: kconfigProfileKey(target, tab.arch, toName),
    values,
    count: Object.keys(values).length,
    toName,
    renamed: toName !== name,
    holdsEngine: state.isKconfigEngineOwner(tab.id),
    toolchain: state.getKconfigToolchain(fromKey),
  };
}

/** Carry the configuration (and toolchain profile) to the target revision and drop the old one. */
function applyKconfigCarryPlan(tab, plan) {
  tab.configName = plan.toName;
  if (plan.count) state.saveKconfigProfileValues(plan.toKey, plan.values);
  if (Object.keys(plan.toolchain || {}).length) {
    state.setKconfigToolchain(plan.toKey, JSON.parse(JSON.stringify(plan.toolchain)));
  }
  // The revision being left keeps no configuration: this tab takes its values with it
  state.setKconfigToolchain(plan.fromKey, null);
  state.dropKconfigProfile(plan.fromKey);
}

/** Dialog listing what applying a revision does to a KConfig tab; resolves true to go ahead. */
function confirmKconfigVersionSwitch(tab, target, plan) {
  return new Promise((resolve) => {
    let confirmed = false;
    const from = tab.version || state.currentVersion;
    const modal = openModal({
      title: `Apply ${target} to ${tab.title}?`,
      subtitle: `Its ${plan.count} current value${plan.count === 1 ? "" : "s"} move to ${target}; nothing is kept for ${from}`,
      width: "600px",
      footerHtml: `<button class="code-btn active" id="kconfig-confirm-version" type="button">Apply ${escapeHtml(target)}</button>`,
      onClose: () => resolve(confirmed),
    });
    modal.body.innerHTML = `
      <div class="kconfig-muted" style="display:flex;flex-direction:column;gap:8px;">
        <div>This tab reloads the Kconfig model of <code>${escapeHtml(target)}</code> for ARCH=${escapeHtml(tab.arch || "")} and its current configuration is applied to it: ${plan.count ? `its <strong>${plan.count}</strong> value${plan.count === 1 ? "" : "s"} are loaded against that model, and options <code>${escapeHtml(target)}</code> does not define are dropped` : "it has no values yet, so the target's default defconfig is loaded"}.</div>
        <div><strong>The configuration saved for <code>${escapeHtml(from)}</code> is not kept</strong>: it moves with this tab instead of staying behind, so switching back later starts from these same values (or from that revision's default defconfig).</div>
        ${plan.holdsEngine ? `<div><strong>This tab holds the constraint engine</strong>: the carried values are resolved by the engine against <code>${escapeHtml(target)}</code>'s model, and the code tabs following this engine (<code>#ifdef</code> dimming, build banners, per-architecture definitions) follow the result.</div>` : `<div>The engine is off in this tab, so the carried values are not resolved; they are loaded as they are.</div>`}
        ${plan.renamed ? `<div><code>${escapeHtml(target)}</code> already has a configuration named ${escapeHtml(tab.configName || "unnamed")} in another tab, so this one becomes <strong>${escapeHtml(plan.toName)}</strong>.</div>` : ""}
        <div><strong>Close</strong> keeps this tab, its configuration and the explored revision on <code>${escapeHtml(from)}</code>.</div>
      </div>`;
    const ok = modal.footer.querySelector("#kconfig-confirm-version");
    ok.onclick = () => {
      confirmed = true;
      modal.close();
    };
    ok.focus();
  });
}
