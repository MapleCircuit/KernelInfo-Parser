/**
 * version_switch.js - Guarded revision switch.
 *
 * Changing the revision repoints the active tab, which for a KConfig tab means rebuilding its
 * Kconfig model and re-resolving its configuration — a change beyond the one the user asked for,
 * so it is confirmed first (see webapp/API.md section 5.0). Every user-initiated version change
 * goes through `requestVersionSwitch`; restoring a revision from the URL hash does not.
 */
import { state } from "../state.js";
import { openModal, escapeHtml } from "./modal.js";

/** Ask before a revision switch that would carry a KConfig tab with it; true when it happened. */
export async function requestVersionSwitch(target) {
  if (!target || typeof target !== "string" || target === state.currentVersion) return false;
  const tab = state.getActiveTab();
  if (tab && tab.type === "kconfig" && !(await confirmKconfigVersionSwitch(tab, target))) return false;
  state.setVersion(target);
  return true;
}

/** Dialog listing what switching revision does to a KConfig tab; resolves true to go ahead. */
function confirmKconfigVersionSwitch(tab, target) {
  return new Promise((resolve) => {
    let confirmed = false;
    const from = tab.version || state.currentVersion;
    const holdsEngine = state.isKconfigEngineOwner(tab.id);
    const modal = openModal({
      title: `Switch ${tab.title} to ${target}?`,
      subtitle: `Its Kconfig model is rebuilt for ${target}`,
      width: "560px",
      footerHtml: `<button class="code-btn active" id="kconfig-confirm-version" type="button">Switch to ${escapeHtml(target)}</button>`,
      onClose: () => resolve(confirmed),
    });
    modal.body.innerHTML = `
      <div class="kconfig-muted" style="display:flex;flex-direction:column;gap:8px;">
        <div>Kconfig options, dependencies and defaults differ between revisions, so this tab reloads the model of <code>${escapeHtml(target)}</code> for ARCH=${escapeHtml(tab.arch || "")}.</div>
        <div>Its configuration for <code>${escapeHtml(from)}</code> stays saved and comes back when you switch this tab back. <code>${escapeHtml(target)}</code> starts from its own saved configuration, or from its default defconfig the first time.</div>
        ${holdsEngine ? `<div><strong>This tab holds the constraint engine</strong>: the configuration saved for <code>${escapeHtml(target)}</code> is resolved by the engine against that revision's model and becomes the published one, so <code>#ifdef</code> dimming, build banners and per-architecture definitions follow it.</div>` : ""}
        <div><strong>Close</strong> keeps this tab (and the explored revision) on <code>${escapeHtml(from)}</code>.</div>
      </div>`;
    const ok = modal.footer.querySelector("#kconfig-confirm-version");
    ok.onclick = () => {
      confirmed = true;
      modal.close();
    };
    ok.focus();
  });
}
