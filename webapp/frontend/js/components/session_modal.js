/**
 * session_modal.js - Session Picker & Workspace Session Management Modal.
 * Prompts user to choose, duplicate, or create a session when opening a new tab or switching sessions.
 */
import { sessionManager } from "../session_manager.js";
import { state } from "../state.js";
import { toast } from "./toast.js";
import { escapeHtml } from "./modal.js";

function formatRelativeTime(timestamp) {
  if (!timestamp) return "Just now";
  const diffMs = Date.now() - timestamp;
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "Just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.floor(diffHour / 24);
  return `${diffDay}d ago`;
}

export function showSessionPickerModal({ allowClose = false, onSelect = null } = {}) {
  // If a modal is already open, don't duplicate
  const existing = document.getElementById("session-picker-modal");
  if (existing) return;

  const backdrop = document.createElement("div");
  backdrop.id = "session-picker-modal";
  backdrop.className = "ki-modal-backdrop session-modal-backdrop";

  const renderModalContent = () => {
    const sessions = sessionManager.getSessionsIndex();
    const activeId = sessionManager.activeSessionId;

    const cardsHtml = sessions.length > 0
      ? sessions.map((s) => {
          const isActive = s.id === activeId;
          const isOtherActive = sessionManager.isSessionActiveInOtherWindow(s.id);
          const preview = (s.previewTabs || []).slice(0, 4).map((t) =>
            `<span class="session-tab-chip" title="${escapeHtml(t.title || '')}">` +
            `<span class="chip-type">${escapeHtml(t.type || 'tab')}</span> ` +
            `${escapeHtml(t.title || 'Untitled')}` +
            `</span>`
          ).join("");

          let badgeHtml = "";
          if (isActive) {
            badgeHtml = '<span class="session-card-badge">Active in this window</span>';
          } else if (isOtherActive) {
            badgeHtml = '<span class="session-card-badge" style="background: rgba(210, 153, 34, 0.2); color: var(--accent-yellow); border: 1px solid rgba(210, 153, 34, 0.4);" title="Opening it here signs that window out of this session; use Duplicate to work on a copy instead.">Open in another tab · Open takes it over</span>';
          }

          return `
            <div class="session-card ${isActive ? 'current-session' : ''}" data-session-id="${escapeHtml(s.id)}">
              <div class="session-card-main">
                <div class="session-card-header">
                  <div class="session-card-name-group">
                    <span class="session-card-name">${escapeHtml(s.name || 'Unnamed Session')}</span>
                    ${badgeHtml}
                  </div>
                  <span class="session-card-time">${formatRelativeTime(s.updatedAt)}</span>
                </div>
                <div class="session-card-meta">
                  <span class="session-meta-tag" style="font-family: var(--font-mono); font-size: 10px; color: var(--accent-blue);" title="Session ID">ID: ${escapeHtml(s.id)}</span>
                  <span class="session-meta-tag">Ref: ${escapeHtml(s.version || 'v3.0')}</span>
                  ${s.repo ? `<span class="session-meta-tag">Repo: ${escapeHtml(s.repo)}</span>` : ''}
                  <span class="session-meta-tag">${s.tabCount || 0} tab${(s.tabCount || 0) === 1 ? '' : 's'}</span>
                </div>
                ${preview ? `<div class="session-card-tabs">${preview}</div>` : ''}
              </div>
              <div class="session-card-actions">
                <button type="button" class="session-btn session-btn-open" data-open-id="${escapeHtml(s.id)}">
                  Open
                </button>
                <button type="button" class="session-btn session-btn-duplicate" data-dup-id="${escapeHtml(s.id)}" title="Duplicate this session into a new session">
                  Duplicate
                </button>
                <button type="button" class="session-btn session-btn-delete" title="Delete this session" data-delete-id="${escapeHtml(s.id)}">
                  &times;
                </button>
              </div>
            </div>
          `;
        }).join("")
      : `
        <div class="session-empty-state">
          <p>No saved workspace sessions found.</p>
          <p style="font-size:11px;color:var(--text-muted);margin-top:4px;">Create a new session to get started.</p>
        </div>
      `;

    backdrop.innerHTML = `
      <div class="ki-modal session-modal-content" style="max-width: 660px; width: 90vw;">
        <div class="ki-modal-header session-modal-top">
          <div>
            <h2 class="ki-modal-title" style="display:flex;align-items:center;gap:8px;">
              <span>🗂 Workspace Sessions</span>
            </h2>
            <div style="font-size:11px;color:var(--text-secondary);margin-top:2px;">
              Select a session to resume in this browser tab, or create a new workspace session.
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;">
            <button type="button" id="btn-modal-new-session" class="session-btn-primary">
              ➕ New Session
            </button>
            ${allowClose ? '<button type="button" class="ki-modal-close" id="btn-modal-close">&times;</button>' : ''}
          </div>
        </div>

        <div class="ki-modal-body session-modal-list">
          ${cardsHtml}
        </div>
      </div>
    `;

    // Bind event handlers
    const closeBtn = backdrop.querySelector("#btn-modal-close");
    if (closeBtn) {
      closeBtn.onclick = () => {
        backdrop.remove();
      };
    }

    const newBtn = backdrop.querySelector("#btn-modal-new-session");
    if (newBtn) {
      newBtn.onclick = () => {
        const count = sessions.length;
        const name = prompt("Enter a name for the new session:", `Session ${count + 1}`);
        if (name === null) return;
        const newSession = sessionManager.createSession({
          name: name.trim() || `Session ${count + 1}`,
          version: state.currentVersion || "v3.0",
          repo: state.currentRepo || "",
          pin: false
        });
        state.adoptSession(newSession);
        backdrop.remove();
        if (onSelect) onSelect(newSession.id);
      };
    }

    // A session is open in one window at a time (webapp/API.md §5.23). Opening one that is already
    // in use elsewhere takes it over: pinning it broadcasts the claim, the other window is signed
    // out of it on the spot and shows this picker, so neither window saves over the other. Nothing
    // is lost — that window's session is still in the list, and **Duplicate** opens a copy instead.
    const openSession = (sid) => {
      const item = sessions.find((s) => s.id === sid);
      const takenOver = sessionManager.isSessionActiveInOtherWindow(sid);
      const sessionData = sessionManager.loadSession(sid);
      if (sessionData) {
        state.adoptSession(sessionData);
        backdrop.remove();
        if (takenOver) {
          toast.info(`'${item?.name || sessionData.name || sid}' was open in another window; that window was signed out of it.`);
        }
        if (onSelect) onSelect(sid);
      }
    };

    backdrop.querySelectorAll(".session-btn-open").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        openSession(btn.dataset.openId);
      };
    });

    backdrop.querySelectorAll(".session-btn-duplicate").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const sid = btn.dataset.dupId;
        const dup = sessionManager.duplicateSession(sid);
        if (dup) {
          state.adoptSession(dup);
          backdrop.remove();
          if (onSelect) onSelect(dup.id);
        }
      };
    });

    backdrop.querySelectorAll(".session-card").forEach((card) => {
      card.onclick = (e) => {
        // Ignore clicks on buttons inside card
        if (e.target.closest("button")) return;
        openSession(card.dataset.sessionId);
      };
    });

    backdrop.querySelectorAll(".session-btn-delete").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const sid = btn.dataset.deleteId;
        const item = sessions.find((s) => s.id === sid);
        const name = item?.name || "this session";
        if (confirm(`Are you sure you want to delete session '${name}'? This cannot be undone.`)) {
          sessionManager.deleteSession(sid);
          renderModalContent();
        }
      };
    });
  };

  renderModalContent();
  document.body.appendChild(backdrop);
}
