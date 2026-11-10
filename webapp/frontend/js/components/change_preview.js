/**
 * change_preview.js - The reusable §5.0 confirmation dialog (S08, webapp/API.md §5.32).
 *
 *     const ok = await showChangePreview({
 *       title, summary,                                      // what will change and why (one line)
 *       intro,                                               // optional HTML-free paragraph
 *       sections: [{ label, rows: [{ name, from, to, why }], prefix = "CONFIG_", columns, fields }],
 *       notes: ["…"],                                        // unmatched inputs, unknown symbols…
 *       confirmLabel, onInspect(name),                       // clicking a name inspects it and cancels
 *     });
 *
 * `fields` picks the row keys shown after the name (default from, to, why; `columns` are their headers). A
 * null `confirmLabel` makes a report with Close only. Close, Escape and the backdrop resolve false; only the
 * confirm button resolves true. Every value is escaped.
 * `previewOrApply({…, apply})` shows nothing and applies at once when every section is empty (§5.0: no dialog
 * when nothing else would change).
 */
import { openModal, escapeHtml } from "./modal.js";
import { previewIsEmpty } from "../views/kconfig/kconfig_dry_run.js";

const MAX_ROWS = 500;
const FILTER_AT = 30;

function sectionHtml(sec, idx) {
  const rows = sec.rows || [];
  if (!rows.length) return "";
  const prefix = sec.prefix ?? "CONFIG_";
  const fields = sec.fields || ["from", "to", "why"];
  const cols = sec.columns || ["Option", ...fields.map((f) => ({ from: "Now", to: "After", why: "Why" }[f] || f))];
  const body = rows.slice(0, MAX_ROWS).map((r) => {
    const text = `${prefix}${r.name} ${fields.map((f) => r[f] ?? "").join(" ")}`.toLowerCase();
    return `<tr data-name="${escapeHtml(r.name)}" data-text="${escapeHtml(text)}" title="Inspect ${escapeHtml(prefix + r.name)} (closes this dialog, nothing is applied)">`
      + `<td class="cp-name">${escapeHtml(prefix + r.name)}</td>`
      + fields.map((f) => `<td${f === "why" ? ' class="kconfig-muted"' : ""}>${escapeHtml(r[f] ?? "")}</td>`).join("")
      + "</tr>";
  }).join("");
  return `<section class="cp-section" data-section="${idx}">
    <h4 class="cp-label">${escapeHtml(sec.label)} <span class="cp-count">${rows.length.toLocaleString("en-US")}</span></h4>
    <table class="kconfig-cmp-table cp-table"><thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>
    ${rows.length > MAX_ROWS ? `<div class="kconfig-muted cp-more">… and ${(rows.length - MAX_ROWS).toLocaleString("en-US")} more</div>` : ""}
  </section>`;
}

export function showChangePreview({ title, summary = "", intro = "", sections = [], notes = [], confirmLabel = "Apply", onInspect = null, width = "860px" }) {
  return new Promise((resolve) => {
    let confirmed = false;
    const modal = openModal({
      title,
      subtitle: summary,
      width,
      className: "change-preview",
      footerHtml: confirmLabel ? `<button class="code-btn active cp-confirm" type="button">${escapeHtml(confirmLabel)}</button>` : "",
      onClose: () => resolve(confirmed),
    });
    const total = sections.reduce((n, s) => n + (s.rows || []).length, 0);
    modal.body.innerHTML = `
      ${intro ? `<div class="kconfig-muted cp-intro">${escapeHtml(intro)}</div>` : ""}
      ${total > FILTER_AT ? `<input type="search" class="cp-filter" placeholder="Filter ${total.toLocaleString("en-US")} rows…" aria-label="Filter the changes" />` : ""}
      ${sections.map(sectionHtml).join("")}
      ${notes.length ? `<ul class="cp-notes">${notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>` : ""}`;
    const filter = modal.body.querySelector(".cp-filter");
    if (filter) {
      filter.addEventListener("input", () => {
        const q = filter.value.trim().toLowerCase();
        for (const sec of modal.body.querySelectorAll(".cp-section")) {
          let shown = 0;
          for (const tr of sec.querySelectorAll("tbody tr")) {
            const hit = !q || tr.dataset.text.includes(q);
            tr.hidden = !hit;
            if (hit) shown++;
          }
          sec.querySelector(".cp-count").textContent = q ? `${shown} of ${sec.querySelectorAll("tbody tr").length}` : String(sec.querySelectorAll("tbody tr").length);
        }
      });
    }
    if (onInspect) {
      modal.body.querySelectorAll("tbody tr[data-name]").forEach((tr) => {
        tr.classList.add("cp-link");
        tr.onclick = () => {
          modal.close();
          onInspect(tr.dataset.name);
        };
      });
    }
    const ok = modal.footer.querySelector(".cp-confirm");
    if (ok) {
      ok.onclick = () => {
        confirmed = true;
        modal.close();
      };
      ok.focus();
    }
  });
}

/**
 * Show the preview unless it is empty, then run `apply()` when confirmed (or straight away when empty).
 * Resolves `{applied, shown}`. `show` is injectable for tests.
 */
export async function previewOrApply({ apply, show = showChangePreview, ...preview }) {
  if (previewIsEmpty(preview.sections)) {
    await apply();
    return { applied: true, shown: false };
  }
  const ok = await show(preview);
  if (ok) await apply();
  return { applied: Boolean(ok), shown: true };
}
