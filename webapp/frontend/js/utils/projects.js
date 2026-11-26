/**
 * projects.js - The configured repositories of `GET /webapi/projects` and which project each one holds
 * (webapp/API_FRONTEND.md §5.13): the linux kernel, the Linux man-pages, or another one. app.js fills it
 * whenever the project list is read; views ask it which repository to address for man pages or for
 * the kernel. DOM-free.
 */
let PROJECTS = [];

/** Project slug of a catalog entry: its `project` (e.g. "linux", "man-pages"), else its `slug`. */
export const projectSlug = (p) => String((p && (p.project || p.slug)) || "");

export function setProjects(list) {
  PROJECTS = Array.isArray(list)
    ? list
        .filter((p) => p && p.repo)
        .map((p) => ({
          ...p,
          insights: p.insights && typeof p.insights === "object" ? { ...p.insights } : null,
        }))
    : [];
}

export function projects() {
  return PROJECTS;
}

export function projectOf(repo) {
  return PROJECTS.find((p) => p.repo === repo) || null;
}

export const isManPagesRepo = (repo) => projectSlug(projectOf(repo)) === "man-pages";

export const DEFAULT_KERNEL_FEATURES = Object.freeze({
  kconfig: true,
  api: true,
  nodemap: true,
  maintainers: true,
  commits: true,
  mail: true,
  insights: true,
  stable: true,
  cve: true,
  man: true,
  pahole: true,
  callgraph: true,
  docs: true,
  patch: true,
  compare: true,
});

export const DEFAULT_MANPAGES_FEATURES = Object.freeze({
  kconfig: false,
  api: false,
  nodemap: false,
  maintainers: false,
  commits: true,
  mail: false,
  insights: true,
  stable: false,
  cve: false,
  man: true,
  pahole: false,
  callgraph: false,
  docs: false,
  patch: true,
  compare: true,
});

export const DEFAULT_KERNEL_INSIGHTS = Object.freeze({
  merge_window: true,
  lead_mergers: Object.freeze(["Linus Torvalds"]),
  maintainers_file: "MAINTAINERS",
  catchall_section: "THE REST",
  generic_repo_names: Object.freeze(["linux", "linux-2.6", "kernel"]),
  generic_ref_words: Object.freeze(["linus", "torvalds", "linux", "next"]),
  milestone_rc: 1,
  orphan_min_commits: 5,
  health_min_commits: 10,
  no_review_threshold: 0.10,
});

export const DEFAULT_MANPAGES_INSIGHTS = Object.freeze({
  merge_window: false,
  lead_mergers: Object.freeze([]),
  maintainers_file: "",
  catchall_section: "",
  generic_repo_names: Object.freeze(["man-pages"]),
  generic_ref_words: Object.freeze(["man", "pages", "next"]),
  milestone_rc: 0,
  orphan_min_commits: 5,
  health_min_commits: 10,
  no_review_threshold: 0.10,
});

export function defaultProjectInsights(slug, entry = null) {
  const s = String(slug || "").trim();
  if (s === "man-pages") {
    return {
      ...DEFAULT_MANPAGES_INSIGHTS,
      lead_mergers: [...DEFAULT_MANPAGES_INSIGHTS.lead_mergers],
      generic_repo_names: [...DEFAULT_MANPAGES_INSIGHTS.generic_repo_names],
      generic_ref_words: [...DEFAULT_MANPAGES_INSIGHTS.generic_ref_words],
    };
  }
  if (!s || s === "linux") {
    return {
      ...DEFAULT_KERNEL_INSIGHTS,
      lead_mergers: [...DEFAULT_KERNEL_INSIGHTS.lead_mergers],
      generic_repo_names: [...DEFAULT_KERNEL_INSIGHTS.generic_repo_names],
      generic_ref_words: [...DEFAULT_KERNEL_INSIGHTS.generic_ref_words],
    };
  }
  const lowSlug = s.toLowerCase();
  const repoNames = [lowSlug];
  if (entry) {
    const name = String(entry.name || entry.repo || "").trim().toLowerCase();
    if (name && !repoNames.includes(name)) repoNames.push(name);
    const gitUrl = String(entry.git_url || "").trim().replace(/\/+$/, "");
    if (gitUrl) {
      let base = gitUrl.split("/").pop() || "";
      if (base.endsWith(".git")) base = base.slice(0, -4);
      base = base.trim().toLowerCase();
      if (base && !repoNames.includes(base)) repoNames.push(base);
    }
  }
  const refWords = [lowSlug];
  if (!refWords.includes("next")) refWords.push("next");
  return {
    ...DEFAULT_KERNEL_INSIGHTS,
    lead_mergers: [],
    generic_repo_names: repoNames,
    generic_ref_words: refWords,
  };
}

export const TAB_FEATURE = Object.freeze({
  kconfig: "kconfig",
  api: "api",
  nodemap: "nodemap",
  maintainers: "maintainers",
  commits: "commits",
  mail: "mail",
  insights: "insights",
  cve: "cve",
  man: "man",
  pahole: "pahole",
  callgraph: "callgraph",
  patch: "patch",
  diff: "compare",
});

export function repoFeatures(repo) {
  const base = isManPagesRepo(repo) ? DEFAULT_MANPAGES_FEATURES : DEFAULT_KERNEL_FEATURES;
  const overrides = projectOf(repo)?.features;
  return overrides && typeof overrides === "object" ? { ...base, ...overrides } : { ...base };
}

export function repoInsights(repo) {
  const p = projectOf(repo);
  const slug = p ? projectSlug(p) : (repo === "man-pages" ? "man-pages" : "linux");
  const base = defaultProjectInsights(slug, p);
  const overrides = p?.insights;
  return overrides && typeof overrides === "object" ? { ...base, ...overrides } : { ...base };
}

export function hasFeature(repo, feature) {
  if (!feature) return true;
  const feats = repoFeatures(repo);
  if (Array.isArray(feature)) return feature.every((f) => !f || Boolean(feats[f]));
  return Boolean(feats[feature]);
}

export const tabTypeFeature = (type) => TAB_FEATURE[type] || "";

export function isTabFeatureEnabled(repo, type) {
  const feat = tabTypeFeature(type);
  if (!feat) return true;
  if (type === "man") return hasFeature(repo, "man") && (isManPagesRepo(repo) || Boolean(manPagesRepo()));
  return hasFeature(repo, feat);
}

/** First available repository of a project (`preferred` first when it is one), "" when none is configured. */
export function repoOfProject(slug, preferred = "") {
  const ok = (p) => p && projectSlug(p) === slug && p.available !== false;
  if (preferred && ok(projectOf(preferred))) return preferred;
  return PROJECTS.find(ok)?.repo || "";
}

export function manPagesRepo(preferred = "") {
  const ok = (p) => p && projectSlug(p) === "man-pages" && p.available !== false && hasFeature(p.repo, "man");
  if (preferred && ok(projectOf(preferred))) return preferred;
  return PROJECTS.find(ok)?.repo || "";
}

/**
 * Tab types whose address holds in any repository (§5.18): the header's Project moves such an active tab to
 * the picked project at its default revision, like Apply moves it to another revision. Each entry gives
 * the fields to set there ("" clears one): what addresses something of the old repository is dropped.
 * Any other active tab stays where it is and the project's default view opens next to it.
 */
export const PROJECT_MOVABLE_TABS = {
  explorer: (tab, label = "") => ({ path: "", title: `Explorer${label ? ` · ${label}` : ""}` }),
  commits: () => ({ commitId: "", listCollapsed: "" }),
  search: () => ({}),
  treemap: () => ({ path: "" }),
  status: () => ({}),
};

/** Short project badge of a tab of another repository than the linux kernel ("" for linux / unknown). */
export function projectBadge(repo) {
  if (!repo) return "";
  const p = projectOf(repo);
  if (p && projectSlug(p) === "linux") return "";
  if (!p && /^linux$/i.test(repo)) return "";
  return String((p && (p.name || p.slug)) || repo);
}
