/**
 * shortcuts_overlay.js - Keyboard shortcut overlay (G09, webapp/API.md §5.31.1).
 *
 * Lists every entry of the shortcut registry grouped by where it applies, searchable by key or
 * description, and rebinds the dispatched ones: **Edit** captures the next chord (Escape cancels), a
 * chord another entry of the same scope already uses is refused and named inline, **Reset** restores
 * one entry and **Reset all** every one. Changes are stored as overrides (localStorage
 * "kip-shortcuts") and apply at once.
 */
import { openModal, escapeHtml } from "./modal.js";
import { shortcuts, groupShortcuts, captureStep, formatChord } from "../utils/shortcuts.js";

let openOverlay = null;

export function showShortcutsOverlay() {
  if (openOverlay) {
    openOverlay.body.querySelector(".kbd-search")?.focus();
    return;
  }
  const mac = shortcuts.isMac;
  const modal = openModal({
    title: "Keyboard shortcuts",
    subtitle: `${mac ? "⌘" : "Ctrl"} is shown for Mod · entries marked (fixed) are handled by their own view`,
    width: "760px",
    className: "kbd-overlay",
    footerHtml: '<button type="button" class="code-btn kbd-reset-all" title="Restore every default binding">Reset all</button>',
    onClose: () => {
      stopCapture();
      document.removeEventListener("shortcuts:changed", onChanged);
      openOverlay = null;
    },
  });
  openOverlay = modal;
  let query = "";
  let capture = null; // {id, el}
  let message = null; // {id, text}

  modal.body.innerHTML = `
    <input type="search" class="kbd-search" placeholder="Filter by key or action…" spellcheck="false" aria-label="Filter shortcuts" />
    <div class="kbd-groups"></div>`;
  const search = modal.body.querySelector(".kbd-search");
  const groupsEl = modal.body.querySelector(".kbd-groups");

  function render() {
    const groups = groupShortcuts(shortcuts.list(), query, mac);
    const conflicts = shortcuts.conflicts();
    const conflicted = new Set(conflicts.flatMap((c) => c.ids));
    groupsEl.innerHTML = groups.length ? groups.map((g) => `
      <section class="kbd-group">
        <h4>${escapeHtml(g.title)}</h4>
        <table class="kbd-table"><tbody>
          ${g.entries.map((e) => {
            const capturing = capture?.id === e.id;
            const keys = capturing
              ? '<span class="kbd-capturing">Press a key combination… (Escape cancels)</span>'
              : (e.labels.length ? e.labels.map((l) => `<kbd>${escapeHtml(l)}</kbd>`).join(" ") : '<span class="kbd-none">unbound</span>');
            const actions = e.docOnly || !e.rebindable
              ? '<span class="kbd-fixed">(fixed)</span>'
              : `<button type="button" class="kbd-edit" data-id="${escapeHtml(e.id)}">${capturing ? "Cancel" : "Edit"}</button>${e.customized ? `<button type="button" class="kbd-reset" data-id="${escapeHtml(e.id)}" title="Default: ${escapeHtml(e.defaultKeys.map((k) => formatChord(k, mac)).join(", ") || "none")}">Reset</button>` : ""}`;
            const msg = message?.id === e.id ? `<div class="kbd-msg">${escapeHtml(message.text)}</div>` : "";
            return `<tr class="${e.customized ? "customized" : ""}${conflicted.has(e.id) ? " conflict" : ""}">
              <td class="kbd-desc">${escapeHtml(e.description || e.id)}${msg}</td>
              <td class="kbd-keys">${keys}</td>
              <td class="kbd-actions">${actions}</td>
            </tr>`;
          }).join("")}
        </tbody></table>
      </section>`).join("") : '<div class="kbd-empty">No shortcut matches.</div>';
  }

  function stopCapture() {
    if (!capture) return;
    document.removeEventListener("keydown", onCaptureKey, true);
    shortcuts.suspend(false);
    capture = null;
  }

  function onCaptureKey(e) {
    if (!capture) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const step = captureStep(e, mac);
    if (step.state === "waiting") return;
    const id = capture.id;
    stopCapture();
    if (step.state === "captured") {
      const res = shortcuts.setOverride(id, [step.chord]);
      if (!res.ok) {
        const other = res.conflicts.flatMap((c) => c.ids).filter((x) => x !== id);
        const names = other.map((x) => shortcuts.get(x)?.description || x).join(", ");
        message = { id, text: `${formatChord(step.chord, mac)} is already used by: ${names || res.error || "another entry"}` };
      } else {
        message = null;
      }
    }
    render();
  }

  function onChanged() {
    if (!capture) render();
  }
  document.addEventListener("shortcuts:changed", onChanged);

  groupsEl.addEventListener("click", (e) => {
    const edit = e.target.closest(".kbd-edit");
    const reset = e.target.closest(".kbd-reset");
    if (edit) {
      const id = edit.dataset.id;
      if (capture?.id === id) {
        stopCapture();
      } else {
        stopCapture();
        capture = { id };
        message = null;
        shortcuts.suspend(true);
        document.addEventListener("keydown", onCaptureKey, true);
      }
      render();
    } else if (reset) {
      shortcuts.resetOverride(reset.dataset.id);
      message = null;
      render();
    }
  });
  search.addEventListener("input", () => {
    query = search.value;
    render();
  });
  modal.footer.querySelector(".kbd-reset-all").onclick = () => {
    stopCapture();
    shortcuts.resetAll();
    message = null;
    render();
  };
  render();
  search.focus();
}

shortcuts.register({
  id: "help.shortcuts", keys: ["?"], scope: "global", description: "Show keyboard shortcuts",
  handler: () => showShortcutsOverlay(),
});
