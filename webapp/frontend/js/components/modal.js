/**
 * modal.js - Minimal shared modal dialog (backdrop, header, scrollable body, footer).
 */
import { shortcuts } from "../utils/shortcuts.js";
import { scaleCssLength } from "../utils/ui_scale.js";
export { safeHttpUrl } from "../utils/url_safety.js";

/**
 * @param {{ title: string, subtitle?: string, width?: string, footerHtml?: string, className?: string, onClose?: () => void }} opts
 * @returns {{ el: HTMLElement, body: HTMLElement, footer: HTMLElement, close: () => void }}
 */
export function openModal({ title, subtitle = "", width = "760px", footerHtml = "", className = "", onClose = null }) {
  const backdrop = document.createElement("div");
  backdrop.className = "ki-modal-backdrop";
  backdrop.innerHTML = `
    <div class="ki-modal ${className}" style="width:${scaleCssLength(width)};" role="dialog" aria-modal="true">
      <div class="ki-modal-header">
        <div class="ki-modal-titles">
          <h3 class="ki-modal-title"></h3>
          ${subtitle ? `<div class="ki-modal-subtitle"></div>` : ""}
        </div>
        <button class="ki-modal-x" type="button" title="Close" aria-label="Close">&times;</button>
      </div>
      <div class="ki-modal-body"></div>
      <div class="ki-modal-footer">${footerHtml}<button class="code-btn ki-modal-close" type="button">Close</button></div>
    </div>`;
  backdrop.querySelector(".ki-modal-title").textContent = title;
  if (subtitle) backdrop.querySelector(".ki-modal-subtitle").textContent = subtitle;
  document.body.appendChild(backdrop);

  const onKey = (e) => {
    if (e.key !== "Escape") return;
    // Stacked dialogs (a file browser opened from a form): Escape closes the topmost one only
    const open = document.querySelectorAll?.(".ki-modal-backdrop");
    if (open?.length && open[open.length - 1] !== backdrop) return;
    close();
  };
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    if (backdrop.isConnected) backdrop.remove();
    document.removeEventListener("keydown", onKey);
    if (onClose) onClose();
  }
  document.addEventListener("keydown", onKey);
  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) close();
  });
  backdrop.querySelector(".ki-modal-x").onclick = close;
  backdrop.querySelector(".ki-modal-close").onclick = close;

  return {
    el: backdrop.querySelector(".ki-modal"),
    body: backdrop.querySelector(".ki-modal-body"),
    footer: backdrop.querySelector(".ki-modal-footer"),
    close,
  };
}

export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Trigger a browser download of text content. */
export function downloadText(filename, text, mime = "text/plain") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

shortcuts.register({ id: "modal.close", keys: ["Escape"], scope: "modal", docOnly: true, description: "Close the dialog (nothing is applied)" });
