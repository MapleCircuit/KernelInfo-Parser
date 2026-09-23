/**
 * fidelity_clipboard.js - 100% Copy Fidelity Raw Buffer Handler.
 * Intercepts clipboard copy events to ensure exact, byte-for-byte
 * code extraction from raw file buffers without DOM/HTML artifacts.
 */

export class FidelityClipboard {
  constructor(getRawLinesCallback) {
    this.getRawLines = getRawLinesCallback;
  }

  attach(element) {
    element.addEventListener("copy", (e) => this.handleCopy(e));
  }

  handleCopy(e) {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return;

    // Determine selected line range
    const anchorRow = selection.anchorNode?.parentElement?.closest(".code-row");
    const focusRow = selection.focusNode?.parentElement?.closest(".code-row");

    if (anchorRow && focusRow) {
      const lineA = parseInt(anchorRow.dataset.lineNo || "1", 10);
      const lineB = parseInt(focusRow.dataset.lineNo || "1", 10);
      const startLine = Math.min(lineA, lineB);
      const endLine = Math.max(lineA, lineB);

      const rawLines = this.getRawLines();
      if (Array.isArray(rawLines) && rawLines.length > 0) {
        if (startLine === endLine) {
          // Substring copy on single line
          const selectedText = selection.toString().replace(/\r\n/g, "\n");
          e.clipboardData.setData("text/plain", selectedText);
        } else {
          // Multi-line full fidelity copy from raw buffer
          const slice = rawLines.slice(startLine - 1, endLine);
          const fullText = slice.join("\n");
          e.clipboardData.setData("text/plain", fullText);
        }
        e.preventDefault();
      }
    }
  }
}
