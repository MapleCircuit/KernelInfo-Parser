/**
 * patch_modal.js - In-browser patch staging: edit a file and generate a git format-patch email.
 */
import { api } from "../api.js";
import { toast } from "./toast.js";
import { copyToClipboard } from "../utils/clipboard.js";
import { openModal, escapeHtml, downloadText } from "./modal.js";
import { shortRef } from "./ref_picker.js";

const DRAFT_KEY = "kernel_ide_patch_identity";

function loadIdentity() {
  try {
    return JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveIdentity(identity) {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(identity));
  } catch {
    // Storage unavailable: identity simply is not remembered
  }
}

export async function showPatchModal(version, filePath, originalContent = null) {
  const modal = openModal({
    title: "✎ Create patch",
    subtitle: `${filePath} @ ${shortRef(version)}`,
    width: "980px",
    footerHtml: `<button class="code-btn active" id="patch-generate" type="button">Generate patch</button>`,
  });
  modal.body.innerHTML = `<div class="ki-modal-empty">Loading ${escapeHtml(filePath)}…</div>`;

  let original = originalContent;
  if (original === null) {
    try {
      const res = await api.getFileContent(version, filePath);
      original = res.content || "";
    } catch (err) {
      modal.body.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
      return;
    }
  }

  const identity = loadIdentity();
  const base = filePath.split("/").slice(-2, -1)[0] || filePath.split("/")[0];
  modal.body.innerHTML = `
    <div class="ki-form-row">
      <label>Subject<input id="patch-subject" type="text" value="${escapeHtml(`${base}: `)}" /></label>
    </div>
    <div class="ki-form-row">
      <label>Author name<input id="patch-author" type="text" value="${escapeHtml(identity.name || "")}" placeholder="Your Name" /></label>
      <label>Author email<input id="patch-email" type="email" value="${escapeHtml(identity.email || "")}" placeholder="you@example.com" /></label>
    </div>
    <div class="ki-form-row">
      <label>Description<textarea class="ki-textarea" id="patch-body" style="min-height:70px;" spellcheck="true" placeholder="Explain what the change does and why."></textarea></label>
    </div>
    <div class="ki-form-row"><label>File content (edit below)</label></div>
    <textarea class="ki-textarea" id="patch-editor" spellcheck="false"></textarea>
    <div id="patch-output" style="margin-top:12px;"></div>`;
  const editor = modal.body.querySelector("#patch-editor");
  editor.value = original;
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const { selectionStart: s, selectionEnd: t } = editor;
      editor.setRangeText("\t", s, t, "end");
    }
  });

  modal.footer.querySelector("#patch-generate").onclick = async () => {
    const out = modal.body.querySelector("#patch-output");
    const modified = editor.value;
    if (modified === original) {
      toast.info("No changes to the file yet");
      return;
    }
    const payload = {
      file_path: filePath,
      original_content: original,
      modified_content: modified,
      commit_subject: modal.body.querySelector("#patch-subject").value.trim() || "[PATCH] update file",
      commit_body: modal.body.querySelector("#patch-body").value,
      author_name: modal.body.querySelector("#patch-author").value.trim() || "Kernel Developer",
      author_email: modal.body.querySelector("#patch-email").value.trim() || "dev@kernel.org",
    };
    saveIdentity({ name: payload.author_name, email: payload.author_email });
    out.innerHTML = `<div class="ki-modal-empty">Formatting patch…</div>`;
    try {
      const res = await api.formatPatch(version, payload);
      const patch = res.patch || res.formatted_patch || res.content || "";
      const lines = patch.split("\n").map((l) => {
        const cls = l.startsWith("+") && !l.startsWith("+++") ? "diff-add" : l.startsWith("-") && !l.startsWith("---") ? "diff-del" : l.startsWith("@@") ? "diff-hunk" : "";
        return `<div class="${cls}">${escapeHtml(l) || " "}</div>`;
      }).join("");
      out.innerHTML = `
        <div class="diff-box">
          <div class="diff-header" style="display:flex;justify-content:space-between;align-items:center;">
            <span>git format-patch preview</span>
            <span style="display:flex;gap:6px;">
              <button class="code-btn" id="patch-copy" type="button">Copy</button>
              <button class="code-btn" id="patch-download" type="button">Download .patch</button>
            </span>
          </div>
          <div class="diff-content" style="max-height:360px;overflow:auto;">${lines}</div>
        </div>`;
      out.querySelector("#patch-copy").onclick = async () => {
        if (await copyToClipboard(patch)) toast.success("Patch copied to clipboard");
      };
      out.querySelector("#patch-download").onclick = () => {
        const slug = payload.commit_subject.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 52);
        downloadText(`0001-${slug || "patch"}.patch`, patch, "text/x-diff");
      };
      out.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      out.innerHTML = `<div class="ki-modal-error">${escapeHtml(err.message)}</div>`;
    }
  };
}
