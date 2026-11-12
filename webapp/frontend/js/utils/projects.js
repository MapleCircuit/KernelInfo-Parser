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

/** First available repository of a project (`preferred` first when it is one), "" when none is configured. */
export function repoOfProject(slug, preferred = "") {
  const ok = (p) => projectSlug(p) === slug && p.available !== false;
  if (preferred && ok(projectOf(preferred))) return preferred;
  return PROJECTS.find(ok)?.repo || "";
}

export const manPagesRepo = (preferred = "") => repoOfProject("man-pages", preferred);
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
