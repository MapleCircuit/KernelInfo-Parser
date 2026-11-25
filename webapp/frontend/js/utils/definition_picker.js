/**
 * definition_picker.js - Choose which of several per-architecture definitions "Go to Definition" opens.
 *
 * The backend ranks a symbol's definitions for the ARCH of `state.definitionContext()` (the KConfig
 * engine's ARCH while it is on with a resolved configuration, else x86_64), putting first those the
 * file the symbol is used in reaches through its #include chain (`fromFile`). Among the leading candidate
 * files, the picker then skips definitions the configuration does not compile: files whose Kbuild
 * condition is "n" (GET /webapi/kconfig/{v}/build) and lines inside #if/#ifdef branches compiled out
 * (the same evaluation as #ifdef dimming, with the ARCH's compiler macros such as __x86_64__).
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { DirectiveEvaluator, PREPROCESSED_RE, archMacros } from "../views/kconfig/directive_evaluator.js";
import { configLookup, evalBuild } from "../views/kconfig/build_condition.js";
import { viewOf } from "./active_views.js";

const MAX_CANDIDATE_FILES = 6;

/** First of `defs` (backend rank order) the configuration compiles, or null when none is known to be. */
async function pickConfiguredDefinition(version, defs, ctx = state.definitionContext(), client = api) {
  const files = [...new Set(defs.map((d) => d.file).filter(Boolean))].slice(0, MAX_CANDIDATE_FILES);
  const candidates = defs.filter((d) => files.includes(d.file));
  if (candidates.length < 2) return null;

  const lookup = configLookup(ctx);
  let build = {};
  try {
    build = (await client.getBuildConditions(version, files, ctx.arch))?.files || {};
  } catch (err) {
    console.warn("Build conditions unavailable:", err);
  }
  const evaluator = new DirectiveEvaluator(ctx.assignments, { partial: !ctx.strict, macros: archMacros(ctx.arch, ctx.assignments) });
  const dimmed = new Map();
  const dimmedLines = async (file) => {
    if (!dimmed.has(file)) {
      let lines = new Set();
      if (PREPROCESSED_RE.test(file)) {
        try {
          const res = await client.getFileContent(version, file);
          lines = evaluator.evaluateDimmedLines((res?.content || "").split(/\r?\n/));
        } catch (err) {
          console.warn(`Could not evaluate #ifdef branches of ${file}:`, err);
        }
      }
      dimmed.set(file, lines);
    }
    return dimmed.get(file);
  };

  for (const d of candidates) {
    const cond = build[d.file];
    if (cond && evalBuild(cond.expr, lookup) === "n") continue;
    if ((await dimmedLines(d.file)).has(d.line_s)) continue;
    return d;
  }
  return null;
}

/**
 * getSymbolDetail() whose definition is the one the active configuration compiles (`client`: the repository's).
 * `pos`: `{line, col}` of the token in `fromFile` (a local variable, parameter or field resolves to its
 * declaration) and `kind` ("type": struct/union/enum/typedef definitions first).
 */
async function resolveDefinition(version, name, fromFile = null, client = api, pos = {}) {
  const ctx = state.definitionContext();
  const detail = await client.getSymbolDetail(version, name, ctx.arch, fromFile, pos);
  if (!detail || detail.is_kconfig || !Array.isArray(detail.definitions) || detail.definitions.length < 2) return detail;
  const pick = await pickConfiguredDefinition(version, detail.definitions, ctx, client);
  if (!pick || (pick.file === detail.file_path && pick.line_s === detail.line_s)) return detail;
  return {
    ...detail,
    file_path: pick.file,
    line: pick.line_s,
    line_s: pick.line_s,
    line_e: pick.line_e,
    definition: { ...detail.definition, file: pick.file, line_s: pick.line_s, line_e: pick.line_e, ast_id: pick.ast_id, type_name: pick.type_name },
  };
}

/**
 * Reveal `line` of `path` in place when the active tab is the code tab of that file and revision (scroll,
 * select and flash the line); false when it is not, so the caller opens a tab instead.
 */
export function revealInActiveCodeTab(path, version, line, { repo = "" } = {}) {
  const tab = state.getActiveTab?.();
  if (!tab || tab.type !== "code" || tab.path !== path || (tab.version || state.currentVersion) !== version) return false;
  if (repo && state.repoOf?.(tab) !== repo) return false;
  const view = viewOf(tab.id);
  if (!view?.revealLine || view.currentFilePath !== path) return false;
  view.revealLine(line || 1);
  return true;
}

/** Open `path` at `line` of `version` in a code tab, revealing it in place when it is the active one. */
export function openCodeLocation(version, path, line, { forceNew = false, repo = "" } = {}) {
  if (!path) return false;
  if (!forceNew && revealInActiveCodeTab(path, version, line, { repo })) return true;
  state.openTab({
    ...(repo ? { repo } : {}),
    type: "code",
    title: path.split("/").pop(),
    path,
    cursorLine: line || 1,
    version,
    forceNew: Boolean(forceNew),
  });
  return true;
}

/**
 * Open the definition of `name` (used in `fromFile`, when known) in a code tab; false when none was found.
 * `repo` names the repository to look in and open the tab in (default: the active tab's). `line` / `col` (the
 * token's position in `fromFile`) resolve a local variable to its declaration; `kind: "type"` opens the type
 * definition of `name` (a struct/union/enum tag or typedef name). A definition in the file of the active code
 * tab is revealed in place.
 */
export async function openDefinition(version, name, { forceNew = false, fromFile = null, repo = "", line = null, col = null, kind = null } = {}) {
  const pos = { ...(line ? { line } : {}), ...(col ? { col } : {}), ...(kind ? { kind } : {}) };
  const detail = await resolveDefinition(version, name, fromFile, repo ? api.forRepo(repo) : api, pos);
  if (!detail || !detail.file_path) return false;
  return openCodeLocation(version, detail.file_path, detail.definition?.line_s || detail.line_s || detail.line || 1, { forceNew, repo });
}
