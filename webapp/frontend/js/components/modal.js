/**
 * modal.js - Minimal shared modal dialog (backdrop, header, scrollable body, footer).
 */

/**
 * @param {{ title: string, subtitle?: string, width?: string, footerHtml?: string, className?: string }} opts
 * @returns {{ el: HTMLElement, body: HTMLElement, footer: HTMLElement, close: () => void }}
 */
export function openModal({ title, subtitle = "", width = "760px", footerHtml = "", className = "" }) {
  const backdrop = document.createElement("div");
  backdrop.className = "ki-modal-backdrop";
  backdrop.innerHTML = `
    <div class="ki-modal ${className}" style="width:${width};" role="dialog" aria-modal="true">
      <div class="ki-modal-header">
        <div class="ki-modal-titles">
          <h3 class="ki-modal-title"></h3>
          ${subtitle ? `<div class="ki-modal-subtitle"></div>` : ""}
        </div>
        <button class="ki-modal-x" type="button" title="Close">&times;</button>
      </div>
      <div class="ki-modal-body"></div>
      <div class="ki-modal-footer">${footerHtml}<button class="code-btn ki-modal-close" type="button">Close</button></div>
    </div>`;
  backdrop.querySelector(".ki-modal-title").textContent = title;
  if (subtitle) backdrop.querySelector(".ki-modal-subtitle").textContent = subtitle;
  document.body.appendChild(backdrop);

  const onKey = (e) => {
    if (e.key === "Escape") close();
  };
  function close() {
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
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
