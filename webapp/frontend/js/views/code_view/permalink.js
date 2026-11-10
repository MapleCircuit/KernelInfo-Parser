/**
 * permalink.js - Line ranges and links to them (G06, webapp/API.md §5.21, §5.26).
 *
 * Pure and DOM-free (webapp/frontend/tests/permalink.test.mjs):
 * - `line=<a>` / `line=<a>-<b>` hash values;
 * - permalinks pinned to a commit SHA (never drift when a branch moves);
 * - Markdown snippets with a fenced code block;
 * - links to the same lines on the repository's own web viewer (kernel.org cgit, GitHub, GitLab).
 */

const HEX_RE = /^[0-9a-f]{7,40}$/i;

/** "12" -> {start: 12, end: 12}; "30-12" -> {start: 12, end: 30}; invalid -> null. */
export function parseLineSpec(text) {
  const m = /^\s*(\d{1,7})(?:\s*-\s*(\d{1,7}))?\s*$/.exec(String(text ?? ""));
  if (!m) return null;
  let a = parseInt(m[1], 10);
  let b = m[2] ? parseInt(m[2], 10) : a;
  if (a < 1 || b < 1) return null;
  if (b < a) [a, b] = [b, a];
  return { start: a, end: b };
}

/** {12, 12} -> "12"; {12, 30} -> "12-30". */
export function formatLineSpec(start, end = start) {
  const a = Math.max(1, Math.min(start, end || start));
  const b = Math.max(start, end || start);
  return b > a ? `${a}-${b}` : String(a);
}

/** The commit a permalink pins: a SHA stays as is, a tag or branch takes the SHA of its stamp ("<sha12>.<…>"). */
export function pinnedSha(version, stamp) {
  if (HEX_RE.test(String(version || ""))) return String(version).toLowerCase();
  const sha = String(stamp || "").split(".")[0];
  return HEX_RE.test(sha) ? sha.toLowerCase() : "";
}

/** Hash of a code tab showing `path` at `rev`, lines start-end, with the file diff it shows (§5.26). */
export function codeHash({ rev, path, start = 1, end = start, diffWith = null, diffLayout = "" }) {
  const params = new URLSearchParams();
  params.set("v", rev);
  params.set("tab", "code");
  params.set("file", path);
  if (start > 1 || end > start) params.set("line", formatLineSpec(start, end));
  if (diffWith?.version) {
    params.set("dv", diffWith.version);
    if (diffWith.path && diffWith.path !== path) params.set("dp", diffWith.path);
    if (diffWith.swap) params.set("ds", "1");
    if (diffLayout === "split") params.set("dm", "split");
  }
  return `#${params.toString()}`;
}

const FENCE_LANG = {
  c: "c", rust: "rust", python: "python", shell: "sh", perl: "perl", assembly: "asm", makefile: "make",
  kconfig: "kconfig", dts: "dts", yaml: "yaml", raw: "",
};

/** Markdown fence language of a grammar id (webapp/frontend/js/views/code_view/syntax/registry.js). */
export function fenceLanguage(grammarId) {
  return FENCE_LANG[grammarId] ?? "";
}

/**
 * Markdown snippet:
 *   [`path:a-b` @ v3.0 (sha12)](link)
 *   ```c
 *   <lines>
 *   ```
 * The fence is longer than any backtick run of the code; a backtick in the label is escaped.
 */
export function markdownSnippet({ path, start, end, revLabel, sha, link, lines, lang = "" }) {
  const spec = formatLineSpec(start, end);
  const label = `${path}:${spec}`.replace(/`/g, "\\`");
  const pin = sha && revLabel !== sha ? ` (${sha.slice(0, 12)})` : "";
  const body = (lines || []).join("\n");
  let fence = "```";
  const runs = body.match(/`+/g) || [];
  const longest = runs.reduce((n, r) => Math.max(n, r.length), 0);
  if (longest >= 3) fence = "`".repeat(longest + 1);
  return `[\`${label}\` @ ${revLabel}${pin}](${link})\n${fence}${lang}\n${body}\n${fence}\n`;
}

/**
 * Link to the same lines on the repository's web viewer, from its `git_url`; "" when the host is not
 * one we know how to address.
 * - git.kernel.org (cgit): https://git.kernel.org/pub/scm/<repo>.git/tree/<path>?id=<sha>#n<a>
 * - github.com: https://github.com/<owner>/<repo>/blob/<sha>/<path>#L<a>-L<b>
 * - gitlab.com: https://gitlab.com/<group>/<repo>/-/blob/<sha>/<path>#L<a>-<b>
 */
export function externalLink(gitUrl, path, sha, start, end = start) {
  if (!gitUrl || !sha || !path) return "";
  let url;
  try {
    url = new URL(String(gitUrl).replace(/^git@([^:]+):/, "https://$1/"));
  } catch {
    return "";
  }
  const host = url.hostname.toLowerCase();
  const repoPath = url.pathname.replace(/\/+$/, "").replace(/\.git$/, "");
  const encPath = path.split("/").map(encodeURIComponent).join("/");
  const a = Math.min(start, end);
  const b = Math.max(start, end);
  if (host === "git.kernel.org") {
    // cgit serves https://git.kernel.org/pub/scm/... (clone URLs may lack the /pub/scm prefix)
    const scm = repoPath.startsWith("/pub/scm/") ? repoPath : `/pub/scm${repoPath}`;
    return `https://git.kernel.org${scm}.git/tree/${encPath}?id=${sha}#n${a}`;
  }
  if (host === "github.com") {
    return `https://github.com${repoPath}/blob/${sha}/${encPath}#L${a}${b > a ? `-L${b}` : ""}`;
  }
  if (host === "gitlab.com") {
    return `https://gitlab.com${repoPath}/-/blob/${sha}/${encPath}#L${a}${b > a ? `-${b}` : ""}`;
  }
  return "";
}

/** Scroll position centering lines start-end in a viewport of `viewportH` pixels. */
export function centeredScrollTop(start, end, rowH, viewportH) {
  const mid = ((start + end) / 2 - 1) * rowH + rowH / 2;
  return Math.max(0, Math.round(mid - viewportH / 2));
}
