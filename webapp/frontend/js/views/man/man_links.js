/**
 * man_links.js - Links from the kernel to its man pages (webapp/API_FRONTEND.md §5.38): a syscall's
 * "man 2 read" (Kernel API tab) and an rst `:manpage:` role. `GET /api/man/resolve` names the
 * man-pages tag released on or before the kernel revision; the page opens in a man tab of the man-pages
 * repository (next to the kernel's tabs, in this workspace) when it has it, else its man7.org address opens
 * in a new browser tab.
 *
 * The links keep their man7.org href, so a middle-click still opens man7.org directly.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { escapeHtml } from "../../components/modal.js";
import { man7Url, manTabFields, manTabTitle } from "./man_model.js";

/** `<a>` of a man page reference: data-man-link/-sec carry the page, href the man7.org fallback. */
export function manLinkHtml(name, section, label = null, cls = "") {
  return `<a href="${escapeHtml(man7Url(name, section))}" class="man-link${cls ? ` ${cls}` : ""}" data-man-link="${escapeHtml(name)}" data-man-sec="${escapeHtml(section)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(`${name}(${section}) — man page`)}">${label ?? escapeHtml(`${name}(${section})`)}</a>`;
}

/** Tab data of a man tab on a resolved page (`GET /api/man/resolve` answer). */
export function manTabOfResolve(res, { kernel = "", kernelRepo = "", mode = "" } = {}) {
  const fields = manTabFields({ repo: res.repo, version: res.version, name: res.name, section: res.section, path: "", mode, kernel: res.kernel || kernel, kernelRepo });
  if (!mode) delete fields.manMode; // a reused man tab keeps the mode it shows
  return { type: "man", ...fields, title: manTabTitle(fields) };
}

/**
 * Open name(section) as seen from the kernel repository `repo` (default: the active tab's) at `kernel`: a man
 * tab of the man-pages repository on our copy, else man7.org. Returns true when a man tab was opened.
 */
export async function openManReference(name, section, { kernel = state.activeVersion(), repo = state.activeRepo(), forceNew = false } = {}) {
  if (!name) return false;
  let res = null;
  try {
    res = await api.forRepo(repo).resolveMan(name, section, kernel);
  } catch (err) {
    console.warn(`Man page lookup of ${name}(${section}) failed:`, err);
  }
  if (res && res.available) {
    state.openTab({ ...manTabOfResolve(res, { kernel, kernelRepo: repo }), forceNew });
    return true;
  }
  const url = (res && res.url) || man7Url(name, section);
  // (a noopener window.open returns null: nothing to check)
  window.open(url, "_blank", "noopener,noreferrer");
  toast.info(`${name}(${section}) is not in a man-pages repository here: opened man7.org`);
  return false;
}

/** Click handler part: a click on a `[data-man-link]` inside `e` opens it (true when handled). */
export function handleManLinkClick(e, { kernel = state.activeVersion(), repo = state.activeRepo(), forceNew = false } = {}) {
  const a = e.target?.closest?.("[data-man-link]");
  if (!a || e.button === 1) return false;
  e.preventDefault();
  openManReference(a.dataset.manLink, a.dataset.manSec || "", { kernel, repo, forceNew: forceNew || e.ctrlKey || e.metaKey });
  return true;
}
