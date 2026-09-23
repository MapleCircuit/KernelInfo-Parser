/**
 * clipboard.js - Universal resilient clipboard utility.
 * Supports secure contexts (navigator.clipboard) and non-secure HTTP contexts (document.execCommand fallback).
 */

export async function copyToClipboard(text) {
  if (typeof text !== "string") {
    text = String(text ?? "");
  }

  // 1. Try modern Async Clipboard API if available
  if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.warn("[Clipboard] navigator.clipboard.writeText failed, attempting execCommand fallback:", err);
    }
  }

  // 2. Fallback to hidden textarea with document.execCommand('copy')
  if (typeof document !== "undefined") {
    try {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.left = "-999999px";
      textarea.style.top = "-999999px";
      textarea.setAttribute("readonly", "");
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();

      const successful = document.execCommand("copy");
      textarea.remove();
      if (successful) return true;
    } catch (e) {
      console.error("[Clipboard] execCommand fallback failed:", e);
    }
  }

  return false;
}
