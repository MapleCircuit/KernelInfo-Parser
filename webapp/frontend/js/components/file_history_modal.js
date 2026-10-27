/**
 * file_history_modal.js - Interactive File Git Commit History Modal.
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { toast } from "./toast.js";
import { copyToClipboard } from "../utils/clipboard.js";
import { requestVersionSwitch } from "./version_switch.js";

export async function showFileHistoryModal(version, filePath) {
  // Remove existing modals if open
  document.querySelectorAll(".file-history-modal-backdrop").forEach((m) => m.remove());

  const backdrop = document.createElement("div");
  backdrop.className = "file-history-modal-backdrop";
  backdrop.style.cssText = `
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    background: rgba(0, 0, 0, 0.65);
    backdrop-filter: blur(4px);
    z-index: 10000;
    display: flex;
    align-items: center;
    justify-content: center;
    animation: fadeIn 0.15s ease-out;
  `;

  const modal = document.createElement("div");
  modal.className = "file-history-modal-content";
  modal.style.cssText = `
    background: var(--bg-secondary, #1e1e1e);
    border: 1px solid var(--border-color, #333);
    border-radius: 8px;
    width: 760px;
    max-width: 95vw;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
    overflow: hidden;
  `;

  const cleanPath = (filePath || "").replace(/^\/+/, "");

  modal.innerHTML = `
    <div style="padding: 14px 18px; border-bottom: 1px solid var(--border-color, #333); display: flex; justify-content: space-between; align-items: center; background: var(--bg-tertiary, #252526);">
      <h3 style="margin: 0; font-size: 14px; font-weight: 600; color: var(--text-primary, #fff); display: flex; align-items: center; gap: 8px;">
        <span>📜 File Commit History:</span>
        <code style="color: var(--accent-blue, #61afef); font-size: 13px;">${cleanPath}</code>
      </h3>
      <button class="file-history-modal-close" style="background: none; border: none; color: var(--text-muted, #888); font-size: 20px; cursor: pointer; padding: 0 4px; line-height: 1;">&times;</button>
    </div>
    <div class="file-history-modal-body" style="padding: 16px 20px; overflow-y: auto; flex: 1;">
      <div style="padding: 30px; text-align: center; color: var(--text-muted);">⏳ Loading Git commit history for ${cleanPath}...</div>
    </div>
    <div style="padding: 10px 18px; border-top: 1px solid var(--border-color, #333); display: flex; justify-content: flex-end; background: var(--bg-tertiary, #252526);">
      <button class="code-btn file-history-close-btn" style="padding: 4px 14px; font-size: 12px;">Close</button>
    </div>
  `;

  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);

  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKeyDown);
  };

  const onKeyDown = (e) => {
    if (e.key === "Escape") close();
  };
  document.addEventListener("keydown", onKeyDown);

  backdrop.onclick = (e) => {
    if (e.target === backdrop) close();
  };
  modal.querySelector(".file-history-modal-close").onclick = close;
  modal.querySelector(".file-history-close-btn").onclick = close;

  const bodyEl = modal.querySelector(".file-history-modal-body");

  try {
    const data = await api.getFileHistory(version, cleanPath, 100);
    const commits = (data && data.commits) ? data.commits : [];
    const lastChanged = (data && data.last_changed) || (commits.length > 0 ? commits[0] : null);

    let summaryHtml = "";
    if (lastChanged) {
      summaryHtml = `
        <div style="background: rgba(97, 175, 239, 0.08); border: 1px solid rgba(97, 175, 239, 0.25); border-radius: 6px; padding: 12px 16px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
          <div>
            <div style="font-size: 11px; color: var(--accent-blue, #61afef); font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">🕒 Last Changed Commit (${escapeHtml(version)})</div>
            <div style="font-size: 14px; font-weight: bold; color: var(--text-primary, #fff); margin-top: 4px;">${escapeHtml(lastChanged.subject || 'No subject')}</div>
            <div style="font-size: 12px; color: var(--text-muted, #888); margin-top: 4px;">
              <span>Author: <strong>${escapeHtml(lastChanged.author_name || 'Unknown')}</strong> &lt;${escapeHtml(lastChanged.author_email || '')}&gt;</span>
              <span style="margin-left: 12px;">📅 ${escapeHtml(lastChanged.author_date_iso || '')}</span>
            </div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <span class="badge" style="font-family: monospace; font-size: 12px; padding: 3px 8px; background: var(--bg-tertiary); border: 1px solid var(--border-color); color: var(--accent-blue);"># ${escapeHtml(lastChanged.commit_hash_short || (lastChanged.commit_hash || '').substring(0, 10))}</span>
            <button class="code-btn active" id="btn-summary-explore" style="padding: 3px 10px; font-size: 11px;">Explore ➔</button>
          </div>
        </div>
      `;
    }

    let commitsListHtml = "";
    if (commits.length === 0) {
      commitsListHtml = `<div style="padding: 30px; text-align: center; color: var(--text-muted);">No Git commits found modifying this file up to ${escapeHtml(version)}.</div>`;
    } else {
      commitsListHtml = `
        <div style="display: flex; flex-direction: column; gap: 10px;">
          ${commits.map((c) => `
            <div class="commit-history-item" style="background: var(--bg-tertiary, #252526); border: 1px solid var(--border-color, #333); border-radius: 6px; padding: 12px 14px;" data-hash="${c.commit_hash}">
              <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span class="badge" style="font-family: monospace; font-size: 11px; padding: 2px 6px; background: rgba(97, 175, 239, 0.15); color: var(--accent-blue, #61afef); border: 1px solid rgba(97, 175, 239, 0.3);"># ${escapeHtml(c.commit_hash_short || c.commit_hash.substring(0, 10))}</span>
                  <span style="font-weight: 600; font-size: 12px; color: var(--text-primary, #fff);">${escapeHtml(c.author_name || 'Unknown')}</span>
                  <span style="font-size: 11px; color: var(--text-muted, #888);">&lt;${escapeHtml(c.author_email || '')}&gt;</span>
                </div>
                <span style="font-size: 11px; color: var(--text-muted, #888);">📅 ${escapeHtml(c.author_date_iso || '')}</span>
              </div>
              <div style="font-size: 13px; color: var(--text-primary, #ddd); margin: 6px 0; font-weight: 500;">
                ${escapeHtml(c.subject || '')}
              </div>
              <div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px;">
                <button class="code-btn btn-copy-sha" data-hash="${c.commit_hash}" style="padding: 2px 8px; font-size: 11px;">📋 Copy SHA</button>
                <button class="code-btn active btn-explore-commit" data-hash="${c.commit_hash}" style="padding: 2px 8px; font-size: 11px;">Explore at Commit ➔</button>
                <button class="code-btn btn-view-commit" data-hash="${c.commit_hash}" style="padding: 2px 8px; font-size: 11px; background: rgba(97, 175, 239, 0.15); color: var(--accent-blue, #61afef); border: 1px solid rgba(97, 175, 239, 0.3);">View Commit ➔</button>
              </div>
            </div>
          `).join("")}
        </div>
      `;
    }

    bodyEl.innerHTML = summaryHtml + commitsListHtml;

    // Attach actions
    const summaryExploreBtn = bodyEl.querySelector("#btn-summary-explore");
    if (summaryExploreBtn && lastChanged) {
      summaryExploreBtn.onclick = async () => {
        close();
        await requestVersionSwitch(lastChanged.commit_hash);
        state.openTab({
          type: "code",
          title: cleanPath.split("/").pop(),
          path: cleanPath,
          version: lastChanged.commit_hash,
          forceNew: true,
        });
        toast.info(`Exploring ${cleanPath} at commit ${lastChanged.commit_hash_short}`);
      };
    }

    bodyEl.querySelectorAll(".btn-copy-sha").forEach((btn) => {
      btn.onclick = async () => {
        const hash = btn.dataset.hash;
        const ok = await copyToClipboard(hash);
        if (ok) {
          toast.success(`Copied commit SHA ${hash.slice(0, 8)}`);
        } else {
          toast.error("Failed to copy commit SHA");
        }
      };
    });

    bodyEl.querySelectorAll(".btn-explore-commit").forEach((btn) => {
      btn.onclick = async () => {
        const hash = btn.dataset.hash;
        close();
        await requestVersionSwitch(hash);
        state.openTab({
          type: "code",
          title: cleanPath.split("/").pop(),
          path: cleanPath,
          version: hash,
          forceNew: true,
        });
        toast.info(`Exploring ${cleanPath} at commit ${hash.slice(0, 8)}`);
      };
    });

    bodyEl.querySelectorAll(".btn-view-commit").forEach((btn) => {
      btn.onclick = () => {
        const hash = btn.dataset.hash;
        close();
        if (typeof window !== "undefined" && typeof window.openCommitModal === "function") {
          window.openCommitModal(hash);
        } else {
          state.openTab({
            type: "commits",
            title: "Commits",
            version: version,
            commitId: hash,
            forceNew: true,
          });
        }
      };
    });
  } catch (err) {
    bodyEl.innerHTML = `
      <div style="padding: 20px; color: var(--accent-red, #ff5555); text-align: center;">
        Failed to load commit history: ${err.message}
      </div>
    `;
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
