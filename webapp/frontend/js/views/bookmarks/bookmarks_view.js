/**
 * bookmarks_view.js - Bookmarks, notes and code tours tab (G05, tab type `bookmarks`, webapp/API.md §5.35).
 *
 * Lists the repository's bookmarks (grouped, searchable, reorderable, each with an optional Markdown note) and
 * tours (play, edit steps, export as .kiptour.json, copy a link, import). A link `#tab=bookmarks&t=<tour>`
 * imports the tour it carries as a new one (never overwriting) and offers to play it. Everything lives in the
 * `user` store (S06) and follows edits made in other windows.
 */
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { escapeHtml, downloadText } from "../../components/modal.js";
import { copyToClipboard } from "../../utils/clipboard.js";
import { localDB } from "../../db.js";
import { renderMarkdownFragment } from "../docs/md_renderer.js";
import { decodeTour, encodeTour, sortBookmarks, targetLabel, validateTour } from "./bookmark_model.js";
import { deleteTour, getNote, listBookmarks, listTours, openTarget, removeBookmark, saveTour, setNote, updateBookmark } from "./bookmark_store.js";
import { playTour } from "./tour_player.js";

const LINK_MAX = 8000;

export class BookmarksView {
  constructor() {
    this.containerEl = null;
    this.query = "";
    this.openNotes = new Set();
    this.editTour = null;
    this.unsub = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    containerEl.innerHTML = `<div class="bm-view">
      <div class="bm-head"><h2>Bookmarks &amp; tours</h2>
        <input type="search" class="bm-search" placeholder="Filter bookmarks…" aria-label="Filter bookmarks" value="${escapeHtml(this.query)}" />
      </div>
      <div class="bm-cols"><section class="bm-col bm-marks"></section><section class="bm-col bm-tours"></section></div></div>`;
    const root = containerEl.firstElementChild;
    root.querySelector(".bm-search").addEventListener("input", (e) => {
      this.query = e.target.value.trim().toLowerCase();
      this.renderMarks();
    });
    root.addEventListener("click", (e) => this.onClick(e));
    root.addEventListener("auxclick", (e) => {
      if (e.button === 1) this.onClick(e, true);
    });
    root.addEventListener("change", (e) => this.onChange(e));
    this.unsub?.();
    this.unsub = localDB.onUserChanged(() => {
      if (this.containerEl?.isConnected) this.refresh();
    });
    if (tabData.importTour) await this.importLink(tabData.importTour);
    await this.refresh();
  }

  dispose() {
    this.unsub?.();
    this.unsub = null;
  }

  async refresh() {
    this.marks = sortBookmarks(await listBookmarks());
    this.tours = await listTours();
    this.notes = new Map();
    for (const m of this.marks) {
      const n = await getNote(m.key);
      if (n) this.notes.set(m.key, n);
    }
    this.renderMarks();
    this.renderTours();
  }

  renderMarks() {
    const host = this.containerEl?.querySelector(".bm-marks");
    if (!host) return;
    const q = this.query;
    const shown = (this.marks || []).filter((m) => !q || `${m.data.label} ${targetLabel(m.data.target)} ${m.data.group || ""} ${this.notes.get(m.key)?.data.markdown || ""}`.toLowerCase().includes(q));
    const groups = new Map();
    for (const m of shown) {
      const g = m.data.group || "";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(m);
    }
    const tourOpts = (this.tours || []).map((t) => `<option value="${escapeHtml(t.key)}">${escapeHtml(t.data.title)}</option>`).join("");
    host.innerHTML = `<h3>Bookmarks <span class="kconfig-muted">${this.marks?.length || 0}</span></h3>
      ${!this.marks?.length ? `<div class="bm-empty">No bookmarks yet. In a code tab press <kbd>Ctrl+Alt+B</kbd> on a line, or use <em>Bookmark</em> in a token's menu; commits, mail threads and Kconfig options are bookmarked from the palette (<em>Bookmark this tab</em>).</div>` : ""}
      ${[...groups.entries()].map(([g, list]) => `<div class="bm-group"><div class="bm-group-name">${escapeHtml(g || "Ungrouped")}</div>
        ${list.map((m) => {
          const note = this.notes.get(m.key);
          const open = this.openNotes.has(m.key);
          return `<div class="bm-row" data-key="${escapeHtml(m.key)}">
            <div class="bm-line"><a href="#" data-open="${escapeHtml(m.key)}" title="${escapeHtml(targetLabel(m.data.target))}">${escapeHtml(m.data.label)}</a>
              <span class="kconfig-muted bm-target">${escapeHtml(targetLabel(m.data.target))}${m.data.target.rev ? ` @ ${escapeHtml(m.data.target.rev)}` : ""}</span>${note ? '<span class="bm-has-note" title="Has a note">📝</span>' : ""}</div>
            ${m.data.target.snippet ? `<code class="bm-snippet">${escapeHtml(m.data.target.snippet)}</code>` : ""}
            <div class="bm-actions">
              <input class="bm-label" value="${escapeHtml(m.data.label)}" aria-label="Label" />
              <input class="bm-groupin" value="${escapeHtml(m.data.group || "")}" placeholder="group" aria-label="Group" />
              <button type="button" data-act="up" title="Move up">↑</button><button type="button" data-act="down" title="Move down">↓</button>
              <button type="button" data-act="note">${open ? "Hide note" : note ? "Note" : "+ Note"}</button>
              ${tourOpts ? `<select data-act="to-tour" aria-label="Add to tour"><option value="">+ to tour…</option>${tourOpts}</select>` : ""}
              <button type="button" data-act="delete" title="Delete this bookmark and its note">🗑</button>
            </div>
            ${open ? `<div class="bm-note"><textarea rows="5" placeholder="Markdown note">${escapeHtml(note?.data.markdown || "")}</textarea>
              <div class="bm-note-actions"><button type="button" data-act="save-note">Save note</button></div>
              ${note ? `<div class="kdoc-body bm-note-view">${renderMarkdownFragment(note.data.markdown)}</div>` : ""}</div>` : ""}
          </div>`;
        }).join("")}</div>`).join("")}`;
  }

  renderTours() {
    const host = this.containerEl?.querySelector(".bm-tours");
    if (!host) return;
    const ed = this.editTour;
    host.innerHTML = `<h3>Code tours <span class="kconfig-muted">${this.tours?.length || 0}</span></h3>
      <div class="bm-tour-new"><input class="bm-tour-title" placeholder="New tour title" aria-label="New tour title" /><button type="button" data-act="new-tour">New tour</button>
        <button type="button" data-act="import-tour" title="Import a .kiptour.json file (as a new tour)">Import…</button></div>
      ${(this.tours || []).map((t) => `<div class="bm-tour" data-tour="${escapeHtml(t.key)}">
        <div class="bm-line"><strong>${escapeHtml(t.data.title)}</strong> <span class="kconfig-muted">${t.data.steps.length} step${t.data.steps.length === 1 ? "" : "s"}</span></div>
        ${t.data.description ? `<div class="kconfig-muted">${escapeHtml(t.data.description.slice(0, 200))}</div>` : ""}
        <div class="bm-actions"><button type="button" data-act="play" ${t.data.steps.length ? "" : "disabled"}>▶ Play</button><button type="button" data-act="edit">${ed?.key === t.key ? "Close editor" : "Edit"}</button>
          <button type="button" data-act="export">Export</button><button type="button" data-act="link" title="Copy a link that carries the whole tour">Copy link</button><button type="button" data-act="delete-tour">🗑</button></div>
        ${ed?.key === t.key ? this.tourEditorHtml(ed) : ""}
      </div>`).join("")}`;
  }

  tourEditorHtml(ed) {
    return `<div class="bm-tour-editor">
      <label>Title <input data-tf="title" value="${escapeHtml(ed.tour.title)}" /></label>
      <label>Description <textarea data-tf="description" rows="2">${escapeHtml(ed.tour.description || "")}</textarea></label>
      ${ed.tour.steps.map((s, i) => `<div class="bm-step" data-step="${i}"><div class="bm-line"><strong>${i + 1}.</strong> <a href="#" data-step-open="${i}">${escapeHtml(targetLabel(s.target))}</a>
        <button type="button" data-step-act="up">↑</button><button type="button" data-step-act="down">↓</button><button type="button" data-step-act="remove">✕</button></div>
        <input data-sf="title" value="${escapeHtml(s.title || "")}" placeholder="Step title" />
        <textarea data-sf="markdown" rows="3" placeholder="What to look at here (Markdown)">${escapeHtml(s.markdown || "")}</textarea></div>`).join("") || `<div class="kconfig-muted">Add steps with “+ to tour…” on a bookmark, or “Add this line to a tour” in a code tab's menu.</div>`}
      <div class="bm-actions"><button type="button" data-act="save-tour" class="active">Save tour</button></div></div>`;
  }

  async onClick(e, middle = false) {
    const t = e.target;
    const row = t.closest(".bm-row");
    const rec = row ? this.marks.find((m) => m.key === row.dataset.key) : null;
    const tourEl = t.closest(".bm-tour");
    const tour = tourEl ? this.tours.find((x) => x.key === tourEl.dataset.tour) : null;
    const open = t.closest("[data-open]");
    if (open && rec) {
      e.preventDefault();
      openTarget(rec.data.target, { forceNew: middle || e.ctrlKey || e.metaKey });
      return;
    }
    const stepOpen = t.closest("[data-step-open]");
    if (stepOpen && this.editTour) {
      e.preventDefault();
      openTarget(this.editTour.tour.steps[+stepOpen.dataset.stepOpen].target, { forceNew: middle });
      return;
    }
    const btn = t.closest("button");
    if (!btn || middle) return;
    const act = btn.dataset.act;
    if (rec && act === "delete") {
      await removeBookmark(rec.key);
      toast.success(`Bookmark ${rec.data.label} deleted`);
    } else if (rec && (act === "up" || act === "down")) {
      const same = this.marks.filter((m) => (m.data.group || "") === (rec.data.group || ""));
      const i = same.indexOf(rec);
      const j = act === "up" ? i - 1 : i + 1;
      if (j < 0 || j >= same.length) return;
      const order = same.map((m, k) => (k === i ? j : k === j ? i : k));
      for (let k = 0; k < same.length; k++) if ((same[k].data.order ?? -1) !== order[k]) await updateBookmark(same[k], { order: order[k] });
    } else if (rec && act === "note") {
      if (this.openNotes.has(rec.key)) this.openNotes.delete(rec.key);
      else this.openNotes.add(rec.key);
      this.renderMarks();
    } else if (rec && act === "save-note") {
      await setNote(rec.key, row.querySelector(".bm-note textarea").value);
      toast.success("Note saved");
    } else if (act === "new-tour") {
      const title = this.containerEl.querySelector(".bm-tour-title").value.trim();
      if (!title) {
        toast.info("Give the tour a title");
        return;
      }
      const saved = await saveTour({ title, description: "", steps: [] });
      this.editTour = { key: saved.key, tour: structuredClone(saved.data) };
    } else if (act === "import-tour") {
      this.importFile();
    } else if (tour && act === "play") {
      playTour(tour.data);
    } else if (tour && act === "edit") {
      this.editTour = this.editTour?.key === tour.key ? null : { key: tour.key, tour: structuredClone(tour.data) };
      this.renderTours();
    } else if (tour && act === "export") {
      downloadText(`${tour.data.title.replace(/[^\w.-]+/g, "_").slice(0, 60) || "tour"}.kiptour.json`, JSON.stringify({ format: "kiptour", version: 1, tour: tour.data }, null, 1), "application/json");
    } else if (tour && act === "link") {
      const t2 = await encodeTour(tour.data);
      const link = `${location.origin}${location.pathname}#tab=bookmarks&t=${t2}`;
      if (link.length > LINK_MAX) {
        toast.warning("This tour is too large for a link: use Export and share the file");
        return;
      }
      if (await copyToClipboard(link)) toast.success("Tour link copied");
    } else if (tour && act === "delete-tour") {
      if (!confirm(`Delete the tour “${tour.data.title}” and its ${tour.data.steps.length} steps?`)) return;
      await deleteTour(tour.key);
      if (this.editTour?.key === tour.key) this.editTour = null;
    } else if (tour && act === "save-tour" && this.editTour) {
      this.readEditor();
      try {
        await saveTour(this.editTour.tour, tour);
        toast.success("Tour saved");
      } catch (err) {
        toast.error(err.message);
      }
    } else if (btn.dataset.stepAct && this.editTour) {
      this.readEditor();
      const i = +btn.closest("[data-step]").dataset.step;
      const steps = this.editTour.tour.steps;
      if (btn.dataset.stepAct === "remove") steps.splice(i, 1);
      else {
        const j = btn.dataset.stepAct === "up" ? i - 1 : i + 1;
        if (j >= 0 && j < steps.length) [steps[i], steps[j]] = [steps[j], steps[i]];
      }
      this.renderTours();
    }
  }

  /** Copy the editor's fields into the edited tour (before re-rendering it). */
  readEditor() {
    const host = this.containerEl.querySelector(".bm-tour-editor");
    if (!host || !this.editTour) return;
    const tour = this.editTour.tour;
    tour.title = host.querySelector('[data-tf="title"]').value;
    tour.description = host.querySelector('[data-tf="description"]').value;
    host.querySelectorAll("[data-step]").forEach((el) => {
      const s = tour.steps[+el.dataset.step];
      s.title = el.querySelector('[data-sf="title"]').value;
      s.markdown = el.querySelector('[data-sf="markdown"]').value;
    });
  }

  async onChange(e) {
    const row = e.target.closest(".bm-row");
    const rec = row ? this.marks.find((m) => m.key === row.dataset.key) : null;
    if (!rec) return;
    if (e.target.classList.contains("bm-label")) await updateBookmark(rec, { label: e.target.value.trim() || targetLabel(rec.data.target) });
    else if (e.target.classList.contains("bm-groupin")) await updateBookmark(rec, { group: e.target.value.trim() });
    else if (e.target.dataset.act === "to-tour" && e.target.value) {
      const tour = this.tours.find((t) => t.key === e.target.value);
      if (!tour) return;
      const data = structuredClone(tour.data);
      data.steps.push({ target: rec.data.target, title: rec.data.label, markdown: this.notes.get(rec.key)?.data.markdown || "" });
      await saveTour(data, tour);
      toast.success(`Added to “${tour.data.title}” (step ${data.steps.length})`);
    }
  }

  importFile() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.onchange = async () => {
      const f = input.files?.[0];
      if (!f) return;
      try {
        if (f.size > 2 << 20) throw new Error("A tour is limited to 1 MB");
        const doc = JSON.parse(await f.text());
        const saved = await saveTour(validateTour(doc?.format === "kiptour" ? doc.tour : doc));
        toast.success(`Imported the tour “${saved.data.title}” (${saved.data.steps.length} steps)`);
      } catch (err) {
        toast.error(`Import failed: ${err.message}`);
      }
    };
    input.click();
  }

  /** A tour link: imported as a new tour (a copy, nothing overwritten), then the link parameter is dropped. */
  async importLink(param) {
    state.updateTabState(this.tabId, { importTour: "" }, { replace: true });
    try {
      const tour = await decodeTour(param);
      const saved = await saveTour(tour);
      toast.success(`Tour “${saved.data.title}” added (${saved.data.steps.length} steps)`);
      if (saved.data.steps.length) playTour(saved.data);
    } catch (err) {
      toast.error(`This tour link is invalid: ${err.message}`);
    }
  }
}
