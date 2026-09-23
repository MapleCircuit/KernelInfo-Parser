/**
 * person_modal.js - Interactive Developer Profile Modal.
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { toast } from "./toast.js";

export async function showPersonModal(version, personIdentifier) {
  if (!personIdentifier) return;
  const currentVersion = version || state.currentVersion || "v3.0";

  // Remove existing modals if open
  document.querySelectorAll(".person-modal-backdrop").forEach((m) => m.remove());

  const backdrop = document.createElement("div");
  backdrop.className = "person-modal-backdrop";
  backdrop.style.cssText = `
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    background: rgba(0, 0, 0, 0.7);
    backdrop-filter: blur(4px);
    z-index: 99999;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    box-sizing: border-box;
    animation: fadeIn 0.15s ease-out;
  `;

  const modal = document.createElement("div");
  modal.className = "person-modal-content";
  modal.style.cssText = `
    background: var(--bg-secondary, #1e1e1e);
    border: 1px solid var(--border-color, #333);
    border-radius: 8px;
    width: 100%;
    max-width: 600px;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 12px 36px rgba(0, 0, 0, 0.6);
    overflow: hidden;
  `;

  modal.innerHTML = `
    <div style="padding: 16px 20px; border-bottom: 1px solid var(--border-color, #333); display: flex; justify-content: space-between; align-items: center; background: var(--bg-tertiary, #252526);">
      <div style="font-weight: 600; font-size: 14px; color: var(--text-primary, #ccc); display: flex; align-items: center; gap: 8px;">
        <span>👤 Developer Profile</span>
      </div>
      <button class="person-modal-close" style="background: none; border: none; color: var(--text-muted, #888); font-size: 20px; cursor: pointer; padding: 0 4px; line-height: 1;">&times;</button>
    </div>
    <div class="person-modal-body" style="padding: 24px 20px; overflow-y: auto; flex: 1;">
      <div style="text-align: center; color: var(--text-muted, #888); padding: 30px;">
        Loading developer profile...
      </div>
    </div>
  `;

  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);

  const close = () => {
    window.removeEventListener("keydown", onKeyDown);
    backdrop.remove();
  };

  const onKeyDown = (e) => {
    if (e.key === "Escape") close();
  };
  window.addEventListener("keydown", onKeyDown);

  backdrop.onclick = (e) => {
    if (e.target === backdrop) close();
  };
  modal.querySelector(".person-modal-close").onclick = close;

  const bodyEl = modal.querySelector(".person-modal-body");

  try {
    const data = await api.getPersonProfile(currentVersion, personIdentifier);
    const p = data.person || data;
    const name = p.name || data.name || String(personIdentifier);
    const email = p.email || data.email || "";
    const inCredits = Boolean(data.in_credits || (data.credits && (data.credits.description || data.credits.web_page)));
    const credits = data.credits || {};
    const stats = data.contribution_stats || {};
    const sections = data.sections || data.subsystems || [];
    const authoredCount = stats.authored_commits ?? (data.recent_commits ? data.recent_commits.length : 0);

    // Initial avatar letters
    const initials = (name.replace(/[^A-Za-z]/g, "").substring(0, 2) || "U").toUpperCase();

    bodyEl.innerHTML = `
      <!-- Profile Header -->
      <div style="display: flex; gap: 16px; align-items: center; margin-bottom: 20px;">
        <div style="width: 52px; height: 52px; border-radius: 50%; background: linear-gradient(135deg, var(--accent-blue, #388bfd), #8957e5); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 20px; flex-shrink: 0; box-shadow: 0 2px 8px rgba(0,0,0,0.3);">
          ${initials}
        </div>
        <div style="flex: 1; min-width: 0;">
          <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
            <span style="font-size: 18px; font-weight: 600; color: var(--text-primary, #eee);">${escapeHtml(name)}</span>
            ${inCredits ? `<span style="background: rgba(227, 179, 65, 0.15); color: #e3b341; border: 1px solid rgba(227, 179, 65, 0.3); font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 4px;">⭐ CREDITS</span>` : ""}
          </div>
          ${email ? `<div style="font-size: 12px; color: var(--accent-blue, #58a6ff); font-family: var(--font-mono, monospace); margin-top: 2px;">${escapeHtml(email)}</div>` : ""}
        </div>
      </div>

      <!-- Quick Metrics Grid -->
      <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 20px;">
        <div style="background: var(--bg-tertiary, #252526); padding: 12px; border-radius: 6px; border: 1px solid var(--border-color, #333);">
          <div style="font-size: 10px; text-transform: uppercase; color: var(--text-muted, #888); font-weight: 600;">Authored Commits</div>
          <div style="font-size: 20px; font-weight: bold; color: var(--accent-blue, #58a6ff); margin-top: 4px;">${authoredCount}</div>
        </div>
        <div style="background: var(--bg-tertiary, #252526); padding: 12px; border-radius: 6px; border: 1px solid var(--border-color, #333);">
          <div style="font-size: 10px; text-transform: uppercase; color: var(--text-muted, #888); font-weight: 600;">Maintained Subsystems</div>
          <div style="font-size: 20px; font-weight: bold; color: var(--accent-green, #3fb950); margin-top: 4px;">${sections.length}</div>
        </div>
      </div>

      <!-- CREDITS Record -->
      ${credits.description || credits.web_page || credits.pgp_key || credits.snail_mail ? `
        <div style="background: var(--bg-tertiary, #252526); padding: 14px; border-radius: 6px; border: 1px solid var(--border-color, #333); margin-bottom: 16px;">
          <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted, #888); margin-bottom: 8px;">biographical credits record</div>
          <div style="font-size: 12px; color: var(--text-secondary, #ccc); display: flex; flex-direction: column; gap: 6px;">
            ${credits.description ? `<div><strong>Description:</strong> ${escapeHtml(credits.description)}</div>` : ""}
            ${credits.web_page ? `<div><strong>Web Page:</strong> <a href="${escapeHtml(credits.web_page)}" target="_blank" style="color:var(--accent-blue, #58a6ff);">${escapeHtml(credits.web_page)} ↗</a></div>` : ""}
            ${credits.pgp_key ? `<div><strong>PGP Key:</strong> <code style="font-family:var(--font-mono, monospace); font-size: 11px; color: var(--accent-orange, #f0883e);">${escapeHtml(credits.pgp_key)}</code></div>` : ""}
            ${credits.snail_mail ? `<div><strong>Postal / Snail Mail:</strong><pre style="margin-top:4px; font-family:inherit; white-space:pre-wrap; color:var(--text-muted, #888); font-size:11px;">${escapeHtml(credits.snail_mail)}</pre></div>` : ""}
          </div>
        </div>
      ` : ""}

      <!-- Maintained Subsystems List -->
      ${sections.length > 0 ? `
        <div style="background: var(--bg-tertiary, #252526); padding: 14px; border-radius: 6px; border: 1px solid var(--border-color, #333); margin-bottom: 16px;">
          <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted, #888); margin-bottom: 8px;">Maintained Subsystems (${sections.length})</div>
          <div style="display: flex; flex-direction: column; gap: 6px; max-height: 140px; overflow-y: auto;">
            ${sections.map((sec) => `
              <div style="display: flex; justify-content: space-between; align-items: center; padding: 6px 8px; background: var(--bg-secondary, #1e1e1e); border-radius: 4px; font-size: 11px;">
                <span style="font-weight: 600; color: var(--text-primary, #eee);">${escapeHtml(sec.name)}</span>
                <span style="color: var(--accent-blue, #58a6ff); font-size: 10px; background: rgba(88, 166, 255, 0.1); padding: 2px 6px; border-radius: 3px;">${escapeHtml(sec.role_name || (sec.role_type === 1 ? 'Maintainer' : 'Reviewer'))}</span>
              </div>
            `).join("")}
          </div>
        </div>
      ` : ""}

      <!-- Action Buttons -->
      <div style="display: flex; gap: 10px; justify-content: flex-end; margin-top: 24px; padding-top: 14px; border-top: 1px solid var(--border-color, #333);">
        <button id="modal-filter-commits-btn" class="code-btn" style="padding: 6px 14px; font-size: 12px; cursor: pointer; border-radius: 4px; border: 1px solid var(--border-color, #444); background: var(--bg-tertiary, #2a2d2e); color: var(--text-primary, #eee); display: flex; align-items: center; gap: 6px;">
          <span>🔍 View Commits</span>
        </button>
        <button id="modal-open-maintainers-btn" class="code-btn active" style="padding: 6px 14px; font-size: 12px; cursor: pointer; border-radius: 4px; border: 1px solid var(--accent-blue, #388bfd); background: var(--accent-blue, #388bfd); color: #fff; display: flex; align-items: center; gap: 6px;">
          <span>📋 Open in Maintainers Tab &rarr;</span>
        </button>
      </div>
    `;

    // Bind action buttons
    const filterCommitsBtn = bodyEl.querySelector("#modal-filter-commits-btn");
    if (filterCommitsBtn) {
      filterCommitsBtn.onclick = () => {
        close();
        state.openTab({
          type: "commits",
          title: `Commits: ${name}`,
          query: name || email,
          version: currentVersion
        });
      };
    }

    const openMaintainersBtn = bodyEl.querySelector("#modal-open-maintainers-btn");
    if (openMaintainersBtn) {
      openMaintainersBtn.onclick = () => {
        close();
        state.openTab({
          type: "maintainers",
          title: `Person: ${name}`,
          person: email || name,
          version: currentVersion
        });
      };
    }
  } catch (err) {
    bodyEl.innerHTML = `
      <div style="padding: 24px; text-align: center;">
        <div style="font-size: 14px; font-weight: 600; color: var(--accent-red, #f85149); margin-bottom: 8px;">
          Profile Not Found
        </div>
        <div style="font-size: 12px; color: var(--text-muted, #888); margin-bottom: 16px;">
          Could not locate developer record for: <code>${escapeHtml(String(personIdentifier))}</code>
        </div>
        <button id="modal-search-commits-fallback-btn" class="code-btn active" style="padding: 6px 14px; font-size: 12px; cursor: pointer; border-radius: 4px; border: 1px solid var(--accent-blue, #388bfd); background: var(--accent-blue, #388bfd); color: #fff;">
          Search Commits for "${escapeHtml(String(personIdentifier))}"
        </button>
      </div>
    `;

    const searchFallbackBtn = bodyEl.querySelector("#modal-search-commits-fallback-btn");
    if (searchFallbackBtn) {
      searchFallbackBtn.onclick = () => {
        close();
        state.openTab({
          type: "commits",
          title: `Commits: ${personIdentifier}`,
          query: String(personIdentifier),
          version: currentVersion
        });
      };
    }
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
