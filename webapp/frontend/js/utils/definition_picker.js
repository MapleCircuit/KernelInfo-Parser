/**
 * definition_picker.js - Choose which of several per-architecture definitions "Go to Definition" opens.
 *
 * The backend ranks a symbol's definitions for the ARCH of `state.definitionContext()` (the KConfig
 * engine's ARCH while it is on with a resolved configuration, else x86_64), putting first those the
 * file the symbol is used in reaches through its #include chain (`fromFile`). Among the leading candidate
 * files, the picker then skips definitions the configuration does not compile: files whose Kbuild
 * condition is "n" (GET /api/kconfig/{v}/build) and lines inside #if/#ifdef branches compiled out
 * (the same evaluation as #ifdef dimming, with the ARCH's compiler macros such as __x86_64__).
 */
import { api } from "../api.js";
import { state } from "../state.js";
import { DirectiveEvaluator, PREPROCESSED_RE, archMacros } from "../views/kconfig/directive_evaluator.js";
import { configLookup, evalBuild } from "../views/kconfig/build_condition.js";

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

/** getSymbolDetail() whose definition is the one the active configuration compiles (`client`: the repository's). */
async function resolveDefinition(version, name, fromFile = null, client = api) {
  const ctx = state.definitionContext();
  const detail = await client.getSymbolDetail(version, name, ctx.arch, fromFile);
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
 * Open the definition of `name` (used in `fromFile`, when known) in a code tab; false when none was found.
 * `repo` names the repository to look in and open the tab in (default: the active tab's).
 */
export async function openDefinition(version, name, { forceNew = false, fromFile = null, repo = "" } = {}) {
  const detail = await resolveDefinition(version, name, fromFile, repo ? api.forRepo(repo) : api);
  if (!detail || !detail.file_path) return false;
  state.openTab({
    ...(repo ? { repo } : {}),
    type: "code",
    title: detail.file_path.split("/").pop(),
    path: detail.file_path,
    cursorLine: detail.definition?.line_s || detail.line_s || detail.line || 1,
    version,
    forceNew: Boolean(forceNew),
  });
  return true;
}
