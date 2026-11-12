/**
 * file_diff.js - Full-file diff of a code tab against any file at any revision (toolbar "⇄ Diff").
 *
 * The server sends only the changed lines (`GET /api/tools/diff/blobs`, -U0 hunks); the unchanged
 * lines are the same on both sides, so the full diff is rebuilt here from the file the tab already
 * shows (`buildDiffRows`). `pairDiffRows` aligns the same rows into side-by-side pairs.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { refPicker, shortRef } from "../../components/ref_picker.js";
import { escapeHtml } from "../../components/modal.js";
import { debounce } from "../../utils/debounce.js";

/**
 * The release tag preceding `target` (the latest tag when target is a commit): a release is compared
 * with the previous release, a release candidate with the previous tag. Shared with the Compare view.
 */
export async function previousTag(target, client = api) {
  try {
    const res = await client.getVersionsCached(true);
    const all = (res.versions || []).filter((v) => typeof v === "string" || v.ref_type !== 3);
    const isPre = (name) => /-(rc|pre|alpha|beta)\d*$/.test(name);
    const tags = all.map((v) => (typeof v === "string" ? v : v.vname)).filter((n) => n && (isPre(target) || !isPre(n) || n === target));
    const idx = tags.indexOf(target);
    if (idx > 0) return tags[idx - 1];
    if (idx === -1 && tags.length > 0) return tags[tags.length - 1];
    return tags.find((t) => t !== target) || "v3.0";
  } catch {
    return "v3.0";
  }
}

/**
 * Full diff rows from -U0 hunks and the lines of the side the tab shows.
 * Row: { k: " " | "+" | "-", o: old line | null, n: new line | null, t: text, s: shown-side line | 0 }.
 * `blocks` are [first, last] row indexes of each change block. A truncated diff stops after its last
 * hunk: the lines past it cannot be placed.
 */
export function buildDiffRows(hunks, shownLines, shownIsNew, truncated = false) {
  // "a\nb\n" splits into ["a", "b", ""]: Git counts two lines, not three
  const total = shownLines.length - (shownLines.length > 1 && shownLines[shownLines.length - 1] === "" ? 1 : 0);
  const rows = [];
  const blocks = [];
  let o = 1;
  let n = 1;
  const ctx = () => {
    const s = shownIsNew ? n : o;
    rows.push({ k: " ", o, n, t: shownLines[s - 1] ?? "", s });
    o++;
    n++;
  };
  const clean = (t) => (t.endsWith("\r") ? t.slice(0, -1) : t);
  for (const h of hunks || []) {
    const firstDel = h.lines.find((r) => r[0] === "-");
    const firstAdd = h.lines.find((r) => r[0] === "+");
    if (firstDel) while (o < firstDel[1]) ctx();
    else if (firstAdd) while (n < firstAdd[2]) ctx();
    const start = rows.length;
    for (const [k, ol, nl, text] of h.lines) {
      if (k === "-") {
        rows.push({ k, o: ol, n: null, t: clean(text), s: shownIsNew ? 0 : ol });
        o = ol + 1;
      } else if (k === "+") {
        rows.push({ k, o: null, n: nl, t: clean(text), s: shownIsNew ? nl : 0 });
        n = nl + 1;
      } else {
        ctx();
      }
    }
    if (rows.length > start) blocks.push([start, rows.length - 1]);
  }
  if (!truncated) while ((shownIsNew ? n : o) <= total) ctx();
  return { rows, blocks };
}

/** Side-by-side pairs { l, r } (base, target; null = filler) of the unified rows, with their change blocks. */
export function pairDiffRows(rows) {
  const pairs = [];
  const blocks = [];
  let i = 0;
  while (i < rows.length) {
    if (rows[i].k === " ") {
      pairs.push({ l: rows[i], r: rows[i] });
      i++;
      continue;
    }
    const dels = [];
    const adds = [];
    while (i < rows.length && rows[i].k !== " ") {
      (rows[i].k === "-" ? dels : adds).push(rows[i]);
      i++;
    }
    const start = pairs.length;
    for (let j = 0; j < Math.max(dels.length, adds.length); j++) pairs.push({ l: dels[j] || null, r: adds[j] || null });
    blocks.push([start, pairs.length - 1]);
  }
  return { pairs, blocks };
}

/** `v3.0` for the same path, `v3.0:kernel/sched.c` for another one. */
export function diffSideLabel(version, path, ownPath) {
  return path === ownPath ? shortRef(version) : `${shortRef(version)}:${path}`;
}

/** Open code tabs other than `ownTabId`, as diff candidates. */
function openCodeTabs(ownTabId) {
  const seen = new Set();
  const out = [];
  for (const pane of state.panes || []) {
    for (const t of pane.tabs || []) {
      if (t.type !== "code" || !t.path || t.id === ownTabId) continue;
      const key = `${t.version}|${t.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ version: t.version || state.currentVersion, path: t.path });
    }
  }
  return out.slice(0, 12);
}

let openPicker = null;

/**
 * Popover choosing the file to diff with: a revision (shared ref picker), a path (searched at that
 * revision), quick picks and the direction. `onPick({ version, path, swap })` runs on **Show diff**;
 * nothing changes when it is dismissed.
 */
export async function showDiffPicker(anchorEl, { tabId, version, path, current, onPick, client = api }) {
  if (openPicker) {
    const same = openPicker.anchorEl === anchorEl;
    openPicker.close();
    if (same) return;
  }
  const prev = await previousTag(version, client);
  let selVersion = current?.version || prev;
  let selPath = current?.path || path;
  let swap = Boolean(current?.swap);

  const pop = document.createElement("div");
  pop.className = "engine-picker file-diff-picker";
  const rect = anchorEl.getBoundingClientRect();
  pop.style.top = `${rect.bottom + 6}px`;
  pop.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 428))}px`;

  const quick = [{ version: prev, path, label: `This file @ ${shortRef(prev)}`, meta: "previous tag" }];
  for (const t of openCodeTabs(tabId)) {
    if (t.version === version && t.path === path) continue;
    quick.push({ version: t.version, path: t.path, label: diffSideLabel(t.version, t.path, path), meta: "open tab" });
  }

  pop.innerHTML = `
    <div class="engine-picker-header">Diff ${escapeHtml(shortRef(version))}:${escapeHtml(path.split("/").pop())} with…</div>
    <div class="file-diff-form">
      <label class="file-diff-label">Revision</label>
      <button type="button" class="code-btn file-diff-rev" title="Pick a release tag or commit"></button>
      <label class="file-diff-label">File</label>
      <div class="file-diff-path-wrap">
        <input type="text" class="file-diff-path" spellcheck="false" autocomplete="off" placeholder="Path at that revision" />
        <div class="file-diff-suggest" hidden></div>
      </div>
      <label class="file-diff-swap"><input type="checkbox" class="file-diff-swap-cb" /> Picked file is the target (this tab is the base)</label>
    </div>
    <div class="engine-picker-header">Quick picks</div>
    <div class="engine-picker-list file-diff-quick">
      ${quick.map((q, i) => `
        <div class="engine-picker-item" data-quick="${i}">
          <span class="engine-picker-title">${escapeHtml(q.label)}</span>
          <span class="engine-picker-meta">${escapeHtml(q.meta)} · ${escapeHtml(q.path)}</span>
        </div>`).join("")}
    </div>
    <div class="file-diff-actions">
      <span class="file-diff-summary"></span>
      <button type="button" class="code-btn file-diff-cancel">Cancel</button>
      <button type="button" class="code-btn active file-diff-go">Show diff</button>
    </div>`;
  document.body.appendChild(pop);

  const revBtn = pop.querySelector(".file-diff-rev");
  const pathInput = pop.querySelector(".file-diff-path");
  const suggest = pop.querySelector(".file-diff-suggest");
  const swapCb = pop.querySelector(".file-diff-swap-cb");
  const summary = pop.querySelector(".file-diff-summary");

  const refresh = () => {
    revBtn.textContent = `${shortRef(selVersion)} ▾`;
    pathInput.value = selPath;
    swapCb.checked = swap;
    const other = diffSideLabel(selVersion, selPath, path);
    const own = shortRef(version);
    summary.textContent = swap ? `${own} → ${other}` : `${other} → ${own}`;
  };
  refresh();

  const close = () => {
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", onKey, true);
    refPicker.close();
    pop.remove();
    if (openPicker?.pop === pop) openPicker = null;
  };
  const outside = (e) => {
    // The ref picker opened from this popover is a popover of its own
    if (pop.contains(e.target) || anchorEl.contains(e.target) || e.target.closest?.(".ref-picker")) return;
    close();
  };
  const go = () => {
    const p = pathInput.value.trim().replace(/^\/+|\/+$/g, "");
    if (!p) {
      pathInput.focus();
      return;
    }
    close();
    onPick({ version: selVersion, path: p, swap });
  };
  const onKey = (e) => {
    if (e.key === "Escape" && !e.target.closest?.(".ref-picker")) {
      e.stopPropagation();
      close();
    }
  };
  setTimeout(() => document.addEventListener("mousedown", outside, true), 0);
  document.addEventListener("keydown", onKey, true);
  openPicker = { pop, anchorEl, close };

  revBtn.onclick = () => refPicker.open(revBtn, {
    current: selVersion,
    onSelect: (ref) => {
      selVersion = ref;
      refresh();
    },
  });
  swapCb.onchange = () => {
    swap = swapCb.checked;
    refresh();
  };
  pop.querySelector(".file-diff-cancel").onclick = close;
  pop.querySelector(".file-diff-go").onclick = go;
  pop.querySelectorAll("[data-quick]").forEach((row) => {
    row.onclick = () => {
      const q = quick[Number(row.dataset.quick)];
      selVersion = q.version;
      selPath = q.path;
      go();
    };
  });

  // Path suggestions come from the revision picked above (the file may live elsewhere there)
  let seq = 0;
  const runSearch = debounce(async () => {
    const q = pathInput.value.trim();
    const mine = ++seq;
    if (q.length < 2) {
      suggest.hidden = true;
      return;
    }
    let files = [];
    try {
      files = (await client.searchFiles(selVersion, q, 12))?.files || [];
    } catch {
      files = [];
    }
    if (mine !== seq || !pop.isConnected) return;
    suggest.innerHTML = files.map((f) => `<div class="file-diff-suggest-item" data-path="${escapeHtml(f.path)}">${escapeHtml(f.path)}</div>`).join("")
      || `<div class="file-diff-suggest-empty">No file matches at ${escapeHtml(shortRef(selVersion))}</div>`;
    suggest.hidden = false;
    suggest.querySelectorAll("[data-path]").forEach((el) => {
      el.onmousedown = (e) => {
        e.preventDefault();
        selPath = el.dataset.path;
        suggest.hidden = true;
        refresh();
      };
    });
  }, 200);
  pathInput.addEventListener("input", () => {
    selPath = pathInput.value;
    runSearch();
  });
  pathInput.addEventListener("blur", () => setTimeout(() => { suggest.hidden = true; }, 150));
  pathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      go();
    }
  });
}
