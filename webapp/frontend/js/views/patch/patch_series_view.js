/**
 * patch_series_view.js - Patch series tab (P3, tab type `patch`, webapp/API.md §5.33).
 *
 * Staged files (from the patch modal's **Add to series**) are grouped into ordered patches, each with its
 * subject and description; a cover letter and the recipients (suggested from MAINTAINERS) complete the
 * series, and **Generate** formats it (`POST /api/version/{base}/patch/series`) with a check that it applies
 * cleanly to its base, an mbox download and per-patch copies. The draft is session state (`patchDrafts`).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { openModal, escapeHtml, downloadText } from "../../components/modal.js";
import { copyToClipboard } from "../../utils/clipboard.js";
import { runCheckpatch } from "../../components/checkpatch_panel.js";
import { addPatch, assignFile, draftProblems, emptyDraft, movePatch, parseAddresses, removeEdit, removePatch, seriesPayload, updatePatch } from "./series_model.js";

const IDENTITY_KEY = "kernel_ide_patch_identity";

function savedIdentity() {
  try {
    return JSON.parse(localStorage.getItem(IDENTITY_KEY) || "{}");
  } catch {
    return {};
  }
}

export class PatchSeriesView {
  constructor() {
    this.containerEl = null;
    this.unsub = null;
  }

  get draft() {
    return state.getPatchDrafts() || emptyDraft(state.currentVersion);
  }

  save(draft, { rerender = true } = {}) {
    this.quiet = !rerender;
    state.setPatchDrafts(draft);
    this.quiet = false;
  }

  render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    this.unsub?.();
    this.unsub = state.subscribe("patch:drafts", () => {
      if (!this.quiet && this.containerEl?.isConnected) this.renderBody();
    });
    containerEl.innerHTML = `<div class="pseries-view"></div>`;
    containerEl.firstElementChild.addEventListener("click", (e) => this.onClick(e));
    containerEl.firstElementChild.addEventListener("change", (e) => this.onChange(e));
    containerEl.firstElementChild.addEventListener("input", (e) => this.onInput(e));
    this.renderBody();
  }

  dispose() {
    this.unsub?.();
    this.unsub = null;
  }

  renderBody() {
    const root = this.containerEl?.querySelector(".pseries-view");
    if (!root) return;
    const d = this.draft;
    if (!d.author.name && !d.author.email) {
      const id = savedIdentity();
      d.author = { name: id.name || "", email: id.email || "" };
    }
    const files = Object.keys(d.edits);
    const options = (sel) => d.series.map((p, i) => `<option value="${escapeHtml(p.id)}" ${p.id === sel ? "selected" : ""}>${i + 1}. ${escapeHtml(p.subject || "(no subject)")}</option>`).join("");
    const patchOf = (path) => d.series.find((p) => p.files.includes(path))?.id || "";
    const problems = draftProblems(d);
    root.innerHTML = `
      <div class="pseries-head">
        <h2>Patch series</h2>
        <span class="kconfig-muted">base <code>${escapeHtml(d.base || state.currentVersion)}</code> · ${files.length} file${files.length === 1 ? "" : "s"} · ${d.series.length} patch${d.series.length === 1 ? "" : "es"}</span>
        <button class="code-btn" type="button" data-act="clear" ${files.length ? "" : "disabled"} title="Drop every staged file and patch">Discard draft</button>
      </div>
      ${files.length ? "" : `<div class="pseries-empty">No staged files. In a code tab, use <strong>✎ Create patch</strong> and <strong>Add to series</strong> for each file to change.</div>`}
      <div class="pseries-cols">
        <section class="pseries-col">
          <h3>Staged files</h3>
          ${files.map((path) => `<div class="pseries-file">
            <code title="${escapeHtml(path)}">${escapeHtml(path)}</code>
            <select data-assign="${escapeHtml(path)}" aria-label="Patch of ${escapeHtml(path)}">${options(patchOf(path))}</select>
            <button class="code-btn" type="button" data-open="${escapeHtml(path)}" title="Open the file at the base revision">Open</button>
            <button class="code-btn" type="button" data-edit="${escapeHtml(path)}" title="Edit the staged text again">Edit</button>
            <button class="code-btn" type="button" data-remove="${escapeHtml(path)}" title="Unstage this file">✕</button>
          </div>`).join("") || `<div class="kconfig-muted">—</div>`}
        </section>
        <section class="pseries-col">
          <h3>Patches <button class="code-btn" type="button" data-act="add-patch">+ Patch</button></h3>
          ${d.series.map((p, i) => `<div class="pseries-patch" data-pid="${escapeHtml(p.id)}">
            <div class="pseries-patch-head"><strong>[PATCH ${i + 1}/${d.series.length}]</strong>
              <button class="code-btn" type="button" data-move="-1" ${i === 0 ? "disabled" : ""} title="Move up">↑</button>
              <button class="code-btn" type="button" data-move="1" ${i === d.series.length - 1 ? "disabled" : ""} title="Move down">↓</button>
              <button class="code-btn" type="button" data-act="remove-patch" ${d.series.length === 1 ? "disabled" : ""} title="Remove (its files go to the previous patch)">✕</button></div>
            <input type="text" data-field="subject" value="${escapeHtml(p.subject)}" placeholder="subsystem: what it does" aria-label="Subject of patch ${i + 1}" />
            <textarea data-field="body" rows="3" placeholder="Why the change is needed" aria-label="Description of patch ${i + 1}">${escapeHtml(p.body)}</textarea>
            <div class="kconfig-muted pseries-patch-files">${p.files.map(escapeHtml).join(", ") || "no files"}</div>
          </div>`).join("")}
        </section>
        <section class="pseries-col">
          <h3>Author &amp; recipients</h3>
          <label>Name<input type="text" data-top="name" value="${escapeHtml(d.author.name)}" /></label>
          <label>Email<input type="email" data-top="email" value="${escapeHtml(d.author.email)}" /></label>
          <label>Version<input type="number" min="1" max="99" data-top="version" value="${d.version || 1}" /></label>
          ${d.series.length > 1 ? `<label>Cover letter subject<input type="text" data-top="cover-subject" value="${escapeHtml(d.cover.subject)}" /></label>
          <label>Cover letter<textarea rows="4" data-top="cover-body">${escapeHtml(d.cover.body)}</textarea></label>` : ""}
          <label>To<textarea rows="2" data-top="to" placeholder="Name &lt;email&gt;, one per line">${escapeHtml(d.to.join("\n"))}</textarea></label>
          <label>Cc<textarea rows="3" data-top="cc">${escapeHtml(d.cc.join("\n"))}</textarea></label>
          <button class="code-btn" type="button" data-act="suggest" ${files.length ? "" : "disabled"} title="Maintainers and lists of every touched file (MAINTAINERS)">Suggest recipients</button>
          <div class="pseries-problems">${problems.map((p) => `<div>• ${escapeHtml(p)}</div>`).join("")}</div>
          <button class="code-btn active" type="button" data-act="generate" ${problems.length ? "disabled" : ""}>Generate series</button>
        </section>
      </div>`;
  }

  onInput(e) {
    const el = e.target;
    const d = this.draft;
    const pid = el.closest("[data-pid]")?.dataset.pid;
    if (pid && el.dataset.field) {
      this.save(updatePatch(d, pid, { [el.dataset.field]: el.value }), { rerender: false });
      this.refreshProblems();
      return;
    }
    const top = el.dataset.top;
    if (!top) return;
    const next = structuredClone(d);
    if (top === "name" || top === "email") next.author[top] = el.value;
    else if (top === "version") next.version = Math.max(1, Math.min(99, parseInt(el.value, 10) || 1));
    else if (top === "cover-subject") next.cover.subject = el.value;
    else if (top === "cover-body") next.cover.body = el.value;
    else if (top === "to" || top === "cc") next[top] = parseAddresses(el.value);
    this.save(next, { rerender: false });
    this.refreshProblems();
  }

  refreshProblems() {
    const problems = draftProblems(this.draft);
    const box = this.containerEl.querySelector(".pseries-problems");
    if (box) box.innerHTML = problems.map((p) => `<div>• ${escapeHtml(p)}</div>`).join("");
    const gen = this.containerEl.querySelector('[data-act="generate"]');
    if (gen) gen.disabled = problems.length > 0;
  }

  onChange(e) {
    const sel = e.target.closest("select[data-assign]");
    if (sel) this.save(assignFile(this.draft, sel.dataset.assign, sel.value));
  }

  async onClick(e) {
    const btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    const d = this.draft;
    const pid = btn.closest("[data-pid]")?.dataset.pid;
    if (btn.dataset.open) {
      state.openTab({ type: "code", title: btn.dataset.open.split("/").pop(), path: btn.dataset.open, version: d.base, forceNew: e.ctrlKey || e.metaKey });
    } else if (btn.dataset.edit) {
      const { showPatchModal } = await import("../../components/patch_modal.js");
      showPatchModal(d.base, btn.dataset.edit, null, { staged: d.edits[btn.dataset.edit]?.modified });
    } else if (btn.dataset.remove) {
      this.save(removeEdit(d, btn.dataset.remove));
    } else if (btn.dataset.move && pid) {
      this.save(movePatch(d, pid, parseInt(btn.dataset.move, 10)));
    } else if (btn.dataset.act === "add-patch") {
      this.save(addPatch(d));
    } else if (btn.dataset.act === "remove-patch" && pid) {
      this.save(removePatch(d, pid));
    } else if (btn.dataset.act === "clear") {
      // Dropping the draft is the request itself, but it loses work: ask once
      const ok = await new Promise((resolve) => {
        let yes = false;
        const m = openModal({ title: "Discard the patch series draft?", subtitle: `${Object.keys(d.edits).length} staged files and ${d.series.length} patches are dropped`, width: "480px",
          footerHtml: `<button class="code-btn active" id="pseries-discard" type="button">Discard</button>`, onClose: () => resolve(yes) });
        m.body.innerHTML = `<div class="kconfig-muted">Close keeps the draft.</div>`;
        m.footer.querySelector("#pseries-discard").onclick = () => { yes = true; m.close(); };
      });
      if (ok) this.save(null);
    } else if (btn.dataset.act === "suggest") {
      await this.suggestRecipients(btn);
    } else if (btn.dataset.act === "generate") {
      await this.generate(btn);
    }
  }

  /** Recipients from MAINTAINERS for every touched file; the user's own entries are kept. */
  async suggestRecipients(btn) {
    const d = this.draft;
    btn.disabled = true;
    try {
      const res = await api.matchMaintainers(d.base, "", Object.keys(d.edits));
      const next = structuredClone(d);
      const merge = (have, add) => [...new Set([...have, ...(add || [])])];
      next.to = merge(next.to, res.suggested_to);
      next.cc = merge(next.cc, (res.suggested_cc || []).filter((a) => !next.to.includes(a)));
      this.save(next);
      toast.success(`${(res.suggested_to || []).length} maintainers, ${(res.suggested_cc || []).length} lists and reviewers suggested`);
    } catch (err) {
      toast.error(`Could not match maintainers: ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  }

  async generate(btn) {
    const d = this.draft;
    try {
      localStorage.setItem(IDENTITY_KEY, JSON.stringify({ name: d.author.name, email: d.author.email }));
    } catch {
      // not remembered
    }
    btn.disabled = true;
    let res;
    try {
      res = await api.formatPatchSeries(d.base, seriesPayload(d));
    } catch (err) {
      toast.error(`Could not generate the series: ${err.message}`);
      return;
    } finally {
      btn.disabled = false;
    }
    const modal = openModal({
      title: `Patch series (${res.patches.length} message${res.patches.length === 1 ? "" : "s"})`,
      subtitle: res.check.applies ? `Applies cleanly to ${d.base} (${res.base.slice(0, 12)})` : `Does NOT apply cleanly to ${d.base}`,
      width: "980px",
      footerHtml: `<button class="code-btn active" id="pseries-mbox" type="button">Download mbox</button>`,
    });
    modal.body.innerHTML = `
      <div class="pseries-check ${res.check.applies ? "ok" : "bad"}">${res.check.applies ? "✓" : "✗"} ${res.check.patches.map((p) => `${escapeHtml(p.name)}: ${escapeHtml(p.status)}`).join(" · ")}</div>
      <pre class="pseries-stat">${escapeHtml(res.diffstat)}</pre>
      ${res.patches.map((p, i) => `<details class="pseries-out" ${i === 0 ? "open" : ""}><summary><code>${escapeHtml(p.name)}</code> ${escapeHtml(p.subject)}
        <button class="code-btn" type="button" data-copy="${i}">Copy</button><button class="code-btn" type="button" data-dl="${i}">Download</button>
        ${p.name.startsWith("0000-") ? "" : `<button class="code-btn" type="button" data-cp="${i}" title="scripts/checkpatch.pl of a release tag, run on the server">checkpatch</button>`}</summary>
        <div class="cp-host" data-cp-out="${i}"></div>
        <pre class="mail-raw-pre"><code>${escapeHtml(p.text)}</code></pre></details>`).join("")}`;
    modal.body.addEventListener("click", async (e) => {
      const c = e.target.closest("[data-copy]");
      const dl = e.target.closest("[data-dl]");
      const cpb = e.target.closest("[data-cp]");
      if (cpb) {
        e.preventDefault();
        const i = +cpb.dataset.cp;
        cpb.closest("details").open = true;
        const pre = cpb.closest("details").querySelector("pre code");
        await runCheckpatch(modal.body.querySelector(`[data-cp-out="${i}"]`), d.base, res.patches[i].text, {
          onJump: (line) => {
            const lines = res.patches[i].text.split("\n");
            pre.innerHTML = lines.map((l, k) => (k === line - 1 ? `<mark class="cp-focus">${escapeHtml(l) || " "}</mark>` : escapeHtml(l))).join("\n");
            pre.querySelector("mark")?.scrollIntoView({ block: "center" });
          },
        });
      } else if (c) {
        e.preventDefault();
        if (await copyToClipboard(res.patches[+c.dataset.copy].text)) toast.success("Patch copied");
      } else if (dl) {
        e.preventDefault();
        const p = res.patches[+dl.dataset.dl];
        downloadText(p.name, p.text, "text/x-diff");
      }
    });
    modal.footer.querySelector("#pseries-mbox").onclick = () => {
      const first = res.patches.find((p) => !p.name.startsWith("0000-")) || res.patches[0];
      downloadText(`${first.name.replace(/^\d+-/, "").replace(/\.patch$/, "")}.mbox`, res.mbox, "application/mbox");
    };
  }
}
