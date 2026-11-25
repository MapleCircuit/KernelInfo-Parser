/**
 * commit_query.js - Structured commit queries of the Commits tab's search box (H8, webapp/API.md §5.12).
 *
 *   path:fs/ext4 reviewer:"Jan Kara" since:2011-01-01 until:2011-07-22 in:v3.0 merges:none journal
 *
 * Recognized keys become chips; anything else (unknown keys included) is free text matched against
 * subjects and messages. Pure and DOM-free (webapp/frontend/tests/commit_query.test.mjs).
 */

import { escapeHtml } from "./relations_render.js";

export const ROLE_KEYS = ["reviewer", "acked", "tested", "reported", "suggested", "signed", "codev", "cc", "fixes", "link"];
export const QUERY_KEYS = ["path", "author", "committer", ...ROLE_KEYS, "since", "until", "in", "range", "merges", "section"];

export const KEY_HELP = {
  path: "touched path or glob (repeatable): path:fs/ext4  path:*.h  path:!drivers/",
  author: "author name or email", committer: "committer name or email",
  reviewer: "Reviewed-by trailer", acked: "Acked-by trailer", tested: "Tested-by trailer", reported: "Reported-by trailer",
  suggested: "Suggested-by trailer", signed: "Signed-off-by trailer", codev: "Co-developed-by trailer", cc: "Cc trailer",
  fixes: "Fixes: trailer text (an abbreviated id or a subject)", link: "Link: trailer text",
  since: "YYYY-MM-DD", until: "YYYY-MM-DD", in: "a release's cycle: in:v3.0 (v2.6.39..v3.0)",
  range: "an explicit range: range:v3.0..v3.1", merges: "only | none", section: "MAINTAINERS section id or name",
};

export const QUERY_HELP_GROUPS = [
  {
    title: "Files & Subsystems",
    items: [
      { key: "path", syntax: "path:<glob>", desc: "Touched file, directory, or glob (repeatable, up to 8; prefix ! to exclude).", example: "path:fs/ext4  path:*.h  path:!drivers/" },
      { key: "section", syntax: "section:<id|name>", desc: "MAINTAINERS section id or exact name; expands its F: include and X: exclude patterns.", example: 'section:"EXT4 FILE SYSTEM"' },
    ],
  },
  {
    title: "People & Trailers",
    items: [
      { key: "author", syntax: "author:<text>", desc: "Author name or email (case-insensitive literal text match).", example: 'author:"Linus Torvalds"' },
      { key: "committer", syntax: "committer:<text>", desc: "Committer name or email (case-insensitive literal text match).", example: 'committer:"Greg Kroah-Hartman"' },
      { key: "reviewer", syntax: "reviewer:<text>", desc: "Reviewed-by: trailer value.", example: 'reviewer:"Jan Kara"' },
      { key: "acked", syntax: "acked:<text>", desc: "Acked-by: trailer value.", example: "acked:torvalds" },
      { key: "tested", syntax: "tested:<text>", desc: "Tested-by: trailer value.", example: "tested:syzbot" },
      { key: "reported", syntax: "reported:<text>", desc: "Reported-by: trailer value.", example: "reported:syzbot" },
      { key: "suggested", syntax: "suggested:<text>", desc: "Suggested-by: trailer value.", example: "suggested:akpm" },
      { key: "signed", syntax: "signed:<text>", desc: "Signed-off-by: trailer value.", example: "signed:tytso" },
      { key: "codev", syntax: "codev:<text>", desc: "Co-developed-by: trailer value.", example: 'codev:"Jane Doe"' },
      { key: "cc", syntax: "cc:<text>", desc: "Cc: trailer value (person, list, or stable note).", example: "cc:stable" },
      { key: "fixes", syntax: "fixes:<text>", desc: "Fixes: trailer text (abbreviated commit id or subject).", example: "fixes:ext4" },
      { key: "link", syntax: "link:<text>", desc: "Link: trailer text (lore URL or Message-ID).", example: "link:lore.kernel.org" },
    ],
  },
  {
    title: "Dates, Releases & Ranges",
    items: [
      { key: "since", syntax: "since:YYYY-MM-DD", desc: "Commits authored on or after this ISO date.", example: "since:2011-01-01" },
      { key: "until", syntax: "until:YYYY-MM-DD", desc: "Commits authored on or before this ISO date (inclusive through 23:59:59).", example: "until:2011-07-22" },
      { key: "in", syntax: "in:<release>", desc: "Commits in a release's cycle (previous release..tag, or prev..branch for latest).", example: "in:v3.0" },
      { key: "range", syntax: "range:<a..b>", desc: "Explicit Git revision range (overrides in: and the active tab version).", example: "range:v3.0..v3.1" },
    ],
  },
  {
    title: "Commit Type",
    items: [
      { key: "merges", syntax: "merges:only|none|all", desc: "Show only merge commits, exclude merges, or include all (default).", example: "merges:none" },
    ],
  },
];

export const QUERY_EXAMPLES = [
  { query: "path:fs/ext4 merges:none", note: "Non-merge commits touching fs/ext4" },
  { query: "path:*.h path:!drivers/ in:v3.0", note: "Header changes outside drivers/ in the v3.0 cycle" },
  { query: 'author:"Linus Torvalds" merges:only', note: "Merge commits authored by Linus Torvalds" },
  { query: 'reviewer:"Jan Kara" journal', note: 'Commits reviewed by Jan Kara mentioning "journal" in the message' },
  { query: "since:2011-05-01 until:2011-07-22 path:mm/", note: "Commits touching mm/ within a date window" },
  { query: "range:v3.0..v3.1 merges:none", note: "Non-merge commits across an explicit Git range" },
];

/** HTML for the in-pane Commit Search Guide rendered in place of commit details. */
export function queryHelpHtml({ hasSelectedCommit = false, selectedCommitLabel = "" } = {}) {
  const closeLabel = hasSelectedCommit
    ? (selectedCommitLabel ? `← Back to commit ${escapeHtml(selectedCommitLabel)}` : "← Back to commit")
    : "✕ Close";

  const groupsHtml = QUERY_HELP_GROUPS.map((g) => `
    <div class="cq-help-group">
      <div class="cq-help-group-title">${escapeHtml(g.title)}</div>
      <div class="cq-help-table">
        ${g.items.map((it) => `
          <div class="cq-help-row">
            <div class="cq-help-key-col">
              <button type="button" class="cq-help-key-btn" data-insert-key="${escapeHtml(it.key)}:" title="Append ${escapeHtml(it.key)}: to search box">
                <code>${escapeHtml(it.syntax)}</code>
              </button>
            </div>
            <div class="cq-help-desc-col">
              <span>${escapeHtml(it.desc)}</span>
              <code class="cq-help-inline-ex">${escapeHtml(it.example)}</code>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `).join("");

  const examplesHtml = QUERY_EXAMPLES.map((ex) => `
    <button type="button" class="cq-help-example" data-example="${escapeHtml(ex.query)}" title="Run this example query">
      <code>${escapeHtml(ex.query)}</code>
      <span>${escapeHtml(ex.note)}</span>
    </button>
  `).join("");

  return `
    <div class="cq-help-panel" id="commit-query-help-panel">
      <div class="commit-header cq-help-header">
        <div class="cq-help-title-row">
          <div>
            <div class="commit-detail-title">Commit Search Guide</div>
            <div class="cq-help-subtitle">Search the timeline by commit hash prefix, plain text, or structured <code>git log</code> filters.</div>
          </div>
          <button type="button" class="code-btn cq-help-close" title="Close commit search guide">${closeLabel}</button>
        </div>
      </div>

      <div class="m-card">
        <div class="m-card-title">Search Modes</div>
        <div class="cq-help-modes">
          <div class="cq-help-mode">
            <div class="cq-help-mode-title">1. Timeline <span class="cq-help-badge">empty box</span></div>
            <div class="cq-help-mode-desc">Leave the search box empty to browse the indexed commit timeline around the active version, with <b>Load 50 More Commits</b> at both the top and bottom.</div>
          </div>
          <div class="cq-help-mode">
            <div class="cq-help-mode-title">2. Hash Prefix &amp; Indexed Text <span class="cq-help-badge">no recognized key:</span></div>
            <ul class="cq-help-list">
              <li><b>Hash prefix</b> (4–40 hex characters, e.g. <code>1da177e4</code>): fast binary prefix lookup on indexed commits, falling back to <code>git rev-parse</code> for unparsed commits.</li>
              <li><b>Plain text</b>: case-insensitive literal substring match against <b>commit subject</b> and <b>author name</b> (<code>%</code> and <code>_</code> match literally). Unknown <code>word:value</code> tokens stay plain text.</li>
            </ul>
          </div>
          <div class="cq-help-mode">
            <div class="cq-help-mode-title">3. Structured Query <span class="cq-help-badge">key:value</span></div>
            <ul class="cq-help-list">
              <li>Any recognized key switches to <code>git log</code> search (newest first, downward pagination, 45 s timeout) and renders removable blue chips below the search bar.</li>
              <li>All filters and any remaining free text (shown as a <code>text:</code> chip) must match together (<b>AND</b> / <code>--all-match</code>), case-insensitively. In structured mode, free text searches the <b>full commit message</b> (subject, body, and trailers).</li>
              <li>Wrap values containing spaces in double quotes (<code>reviewer:"Jan Kara"</code>). Typing a key prefix suggests <code>key:</code> completions; queries stay synced in the URL (<code>&amp;q=</code>).</li>
            </ul>
          </div>
        </div>
      </div>

      <div class="m-card">
        <div class="m-card-title">Structured Filter Keys <span class="cq-help-hint">Click any key to append it to the search box</span></div>
        <div class="cq-help-groups">${groupsHtml}</div>
      </div>

      <div class="m-card">
        <div class="m-card-title">Example Queries <span class="cq-help-hint">Click an example to run it</span></div>
        <div class="cq-help-examples">${examplesHtml}</div>
      </div>
    </div>
  `;
}

const MAX_QUERY_LEN = 4096;

/** Tokens of a query: words, key:value and key:"quoted value" (quotes keep spaces). */
export function tokenize(text) {
  const out = [];
  const s = String(text || "").slice(0, MAX_QUERY_LEN);
  const re = /(\S+?):"([^"]*)"?|"([^"]*)"?|(\S+)/g;
  let m;
  while ((m = re.exec(s)) !== null) {
    if (m[1] !== undefined) out.push({ raw: m[0], key: m[1].toLowerCase(), value: m[2] });
    else if (m[3] !== undefined) out.push({ raw: m[0], key: "", value: m[3] });
    else {
      const t = m[4];
      const idx = t.indexOf(":");
      if (idx > 0 && idx < t.length - 1) out.push({ raw: t, key: t.slice(0, idx).toLowerCase(), value: t.slice(idx + 1) });
      else out.push({ raw: t, key: "", value: t });
    }
  }
  return out;
}

/** Parsed query: {chips: [{key, value, index}], text, paths, author, committer, roles, since, until, release, range, merges, section}. */
export function parseCommitQuery(text) {
  const tokens = tokenize(text);
  const q = { chips: [], text: "", paths: [], author: "", committer: "", roles: [], since: "", until: "",
    release: "", range: "", merges: "", section: "" };
  const words = [];
  tokens.forEach((tok, index) => {
    if (!tok.key || !QUERY_KEYS.includes(tok.key) || !tok.value) {
      words.push(tok.key && !QUERY_KEYS.includes(tok.key) ? tok.raw : tok.value);
      return;
    }
    q.chips.push({ key: tok.key, value: tok.value, index });
    if (tok.key === "path") q.paths.push(tok.value);
    else if (ROLE_KEYS.includes(tok.key)) q.roles.push(`${tok.key}:${tok.value}`);
    else if (tok.key === "in") q.release = tok.value;
    else q[tok.key] = tok.value;
  });
  q.text = words.join(" ").trim();
  return q;
}

/** Whether the text needs the structured endpoint (at least one recognized key). */
export function isStructured(text) {
  return parseCommitQuery(text).chips.length > 0;
}

/** Query string of GET /api/commits/{v}/query for a parsed query. */
export function queryParams(q, page = 1, limit = 50) {
  const p = new URLSearchParams();
  for (const path of q.paths) p.append("path", path);
  for (const r of q.roles) p.append("role", r);
  for (const key of ["author", "committer", "since", "until", "release", "range", "merges", "section", "text"]) {
    if (q[key]) p.set(key, q[key]);
  }
  p.set("page", String(page));
  p.set("limit", String(limit));
  return p;
}

/** The query text without the chip at token index `index` (the URL keeps reproducing the query). */
export function removeToken(text, index) {
  const tokens = tokenize(text);
  return tokens.filter((_, i) => i !== index).map((t) => t.raw).join(" ");
}

/** Keys completing a word being typed ("rev" -> ["reviewer:"]). */
export function completeKey(word) {
  const w = String(word || "").toLowerCase();
  if (!w || w.includes(":")) return [];
  return QUERY_KEYS.filter((k) => k.startsWith(w) && k !== w).map((k) => `${k}:`);
}
