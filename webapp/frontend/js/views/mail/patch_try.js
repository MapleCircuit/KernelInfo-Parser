/**
 * patch_try.js - "Try on revision…" for mailing-list patches (P1, webapp/API.md §5.22).
 *
 * The chosen messages are applied in memory to a revision (`POST /api/mail/apply`, nothing is written):
 * the results dialog lists each patch, file and hunk (applied, at an offset, failed with the nearest
 * region, created, deleted, unsupported), and **Open patched file** opens the file at that revision in
 * ⇄ Diff mode against the patched text (diff source kind `patch`, §5.21).
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { openModal, escapeHtml } from "../../components/modal.js";
import { refPicker } from "../../components/ref_picker.js";
import { toast } from "../../components/toast.js";

const BADGE = { applied: "ok", offset: "warn", created: "ok", deleted: "ok", renamed: "ok", failed: "bad", unsupported: "bad", "no-diff": "muted" };

/** Messages of the newest version of a series, in [PATCH i/N] order (the cover letter first: it may carry base-commit). */
export function seriesMessages(messages) {
  const own = (messages || []).filter((m) => (m.is_patch || m.is_cover) && !/^\s*re\s*:/i.test(m.subject || ""));
  if (!own.length) return [];
  const version = Math.max(...own.map((m) => m.patch_version || 1));
  const byIdx = new Map();
  for (const m of own.filter((x) => (x.patch_version || 1) === version)) {
    const idx = m.is_cover ? 0 : m.patch_idx || 1;
    if (!byIdx.has(idx)) byIdx.set(idx, m);
  }
  return [...byIdx.entries()].sort((a, b) => a[0] - b[0]).map(([, m]) => m);
}

/** Pick a revision next to `anchorEl`, then apply `mids` to it. */
export function pickAndTry(anchorEl, mids, label) {
  refPicker.open(anchorEl, {
    current: state.currentVersion,
    onSelect: (ref) => tryPatches(ref, mids, label),
  });
}

export async function tryPatches(revision, mids, label = "") {
  let res;
  toast.info(`Applying ${mids.length} message${mids.length === 1 ? "" : "s"} to ${revision}…`);
  try {
    res = await api.applyMailPatches(revision, mids);
  } catch (err) {
    toast.error(`Could not apply: ${err.message}`);
    return null;
  }
  showResults(res, label);
  return res;
}

function fileRow(res, f, pi, fi) {
  const hunks = f.hunks || [];
  const failed = hunks.filter((h) => h.status === "failed");
  const canOpen = ["applied", "offset", "renamed", "deleted"].includes(f.status);
  const hunkHtml = hunks.map((h, hi) => h.status === "failed"
    ? `<li class="ptry-hunk bad">Hunk ${hi + 1}: failed at line ${h.expected_line}; nearest region line
        <a href="#" data-open-line="${h.nearest.line}" data-path="${escapeHtml(f.old_path || f.path)}">${h.nearest.line}</a>
        (${h.nearest.matched}/${h.nearest.of} lines match)<pre class="ptry-ctx">${escapeHtml((h.context || []).join("\n"))}</pre></li>`
    : `<li class="ptry-hunk">Hunk ${hi + 1}: ${h.status} at line ${h.line}${h.offset ? ` (offset ${h.offset > 0 ? "+" : ""}${h.offset} lines)` : ""}</li>`).join("");
  return `<details class="ptry-file" ${failed.length ? "open" : ""}>
    <summary><span class="ptry-badge ${BADGE[f.status] || "muted"}">${escapeHtml(f.status)}</span>
      <code>${escapeHtml(f.old_path && f.old_path !== f.path ? `${f.old_path} → ${f.path}` : f.path)}</code>
      ${f.reason ? `<span class="kconfig-muted">${escapeHtml(f.reason)}</span>` : ""}
      ${canOpen && res.patches[pi].status === "applied" ? `<button class="code-btn ptry-open" type="button" data-path="${escapeHtml(f.path)}" title="Open the file at ${escapeHtml(res.base.revision)} in ⇄ Diff mode against the patched text">Open patched file</button>` : ""}
      ${f.status === "created" && res.patches[pi].status === "applied" ? `<button class="code-btn ptry-view" type="button" data-path="${escapeHtml(f.path)}">View new file</button>` : ""}
    </summary>
    ${hunks.length ? `<ul class="ptry-hunks">${hunkHtml}</ul>` : ""}
  </details>`;
}

export function showResults(res, label = "") {
  const s = res.summary;
  const hint = res.base_commit;
  const modal = openModal({
    title: `${label || "Patches"} on ${res.base.revision}`,
    subtitle: `${s.applied}/${s.patches} patch${s.patches === 1 ? "" : "es"} apply${s.offset ? ` (${s.offset} at an offset)` : ""}, ${s.failed} fail · base ${res.base.sha.slice(0, 12)} · in memory only`,
    width: "900px",
    className: "ptry-modal",
    footerHtml: hint && hint.resolved && hint.sha !== res.base.sha
      ? `<button class="code-btn active" id="ptry-base" type="button" title="The series names its base (git format-patch --base)">Try on base-commit ${escapeHtml(hint.commit.slice(0, 12))}</button>` : "",
  });
  modal.body.innerHTML = `
    ${hint && !hint.resolved ? `<div class="kconfig-muted" style="margin-bottom:8px;">The series names base-commit ${escapeHtml(hint.commit.slice(0, 12))}, which this repository does not have.</div>` : ""}
    ${res.patches.map((p, pi) => `<section class="ptry-patch">
      <div class="ptry-head"><span class="ptry-badge ${BADGE[p.status] || "muted"}">${escapeHtml(p.status === "no-diff" ? "no diff" : p.offset ? "offset" : p.status)}</span>
        <strong>${escapeHtml(p.subject || p.message_id)}</strong></div>
      ${p.files.map((f, fi) => fileRow(res, f, pi, fi)).join("")}
    </section>`).join("")}`;
  const openFile = (path, forceNew) => {
    state.openTab({
      type: "code", title: `${path.split("/").pop()} (patched)`, path, version: res.base.revision, forceNew,
      diffWith: { kind: "patch", applyId: res.apply_id, mids: res.message_ids, revision: res.base.revision, path },
    });
  };
  modal.body.addEventListener("click", async (e) => {
    const open = e.target.closest(".ptry-open");
    if (open) {
      e.preventDefault();
      modal.close();
      openFile(open.dataset.path, true);
      return;
    }
    const view = e.target.closest(".ptry-view");
    if (view) {
      e.preventDefault();
      try {
        const f = await api.getAppliedFile(res.apply_id, view.dataset.path);
        const m = openModal({ title: view.dataset.path, subtitle: "New file created by the patch", width: "900px" });
        m.body.innerHTML = `<pre class="mail-raw-pre"><code>${escapeHtml(f.text || "")}</code></pre>`;
      } catch (err) {
        toast.error(err.message);
      }
      return;
    }
    const line = e.target.closest("[data-open-line]");
    if (line) {
      e.preventDefault();
      state.openTab({ type: "code", title: line.dataset.path.split("/").pop(), path: line.dataset.path, version: res.base.revision, cursorLine: Number(line.dataset.openLine), forceNew: true });
    }
  });
  const baseBtn = modal.footer.querySelector("#ptry-base");
  if (baseBtn) {
    baseBtn.onclick = () => {
      modal.close();
      tryPatches(hint.sha, res.message_ids, label);
    };
  }
}

/**
 * The diff source of a code tab in `patch` mode: re-applies when the server evicted the result. `client` is
 * the code tab's repository client (view.api).
 */
export async function patchedFileDiff(target, client = api) {
  const reapply = async () => {
    const res = await client.applyMailPatches(target.revision, target.mids || []);
    target.applyId = res.apply_id;
    return client.getAppliedFile(res.apply_id, target.path);
  };
  // A deep link carries the message ids only: the result is rebuilt by applying them again
  if (!target.applyId) return reapply();
  try {
    return await client.getAppliedFile(target.applyId, target.path);
  } catch (err) {
    if (err.status !== 410) throw err;
    return reapply();
  }
}
