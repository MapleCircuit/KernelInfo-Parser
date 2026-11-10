/**
 * commit_query.js - Structured commit queries of the Commits tab's search box (H8, webapp/API.md §5.12).
 *
 *   path:fs/ext4 reviewer:"Jan Kara" since:2011-01-01 until:2011-07-22 in:v3.0 merges:none journal
 *
 * Recognized keys become chips; anything else (unknown keys included) is free text matched against
 * subjects and messages. Pure and DOM-free (webapp/frontend/tests/commit_query.test.mjs).
 */

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

/** Tokens of a query: words, key:value and key:"quoted value" (quotes keep spaces). */
export function tokenize(text) {
  const out = [];
  const re = /(\S+?):"([^"]*)"?|"([^"]*)"?|(\S+)/g;
  let m;
  while ((m = re.exec(String(text || ""))) !== null) {
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
