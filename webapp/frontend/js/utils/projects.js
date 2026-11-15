/**
 * projects.js - The configured repositories of `GET /api/projects` and which project each one holds
 * (webapp/API_FRONTEND.md §5.13): the linux kernel, the Linux man-pages, or another one. app.js fills it
 * whenever the project list is read; views ask it which repository to address for man pages or for
 * the kernel. DOM-free.
 */
let PROJECTS = [];

/** Project slug of a catalog entry: its `project` (e.g. "linux", "man-pages"), else its `slug`. */
export const projectSlug = (p) => String((p && (p.project || p.slug)) || "");

export function setProjects(list) {
  PROJECTS = Array.isArray(list) ? list.filter((p) => p && p.repo) : [];
}

export function projects() {
  return PROJECTS;
}

export function projectOf(repo) {
  return PROJECTS.find((p) => p.repo === repo) || null;
}

export const isManPagesRepo = (repo) => projectSlug(projectOf(repo)) === "man-pages";
export const isLinuxRepo = (repo) => projectSlug(projectOf(repo)) === "linux";

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

export const TAB_FEATURE = Object.freeze({
  kconfig: "kconfig",
  api: "api",
  nodemap: "nodemap",
  maintainers: "maintainers",
  commits: "commits",
  mail: "mail",
  insights: "insights",
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
export const linuxRepo = (preferred = "") => repoOfProject("linux", preferred);

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
