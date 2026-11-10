/**
 * kconfig_dry_run.js - Compute the full consequence of a Kconfig action before applying it (S08, webapp/API.md §5.0, §5.32).
 *
 * A dry run copies the tab's engine state into a throwaway `KconfigEngine` built from the same decoded model,
 * runs the action on it and compares every option's value before and after:
 *
 *     const run = kconfigDryRun(engine.model, engine.captureState(), (probe, ctx) => {
 *       ctx.want("KVM", "y");                         // setValue + remember what was asked
 *     });
 *     run.changes   // [{name, from, to, why}]   every option whose value differs (the requested ones too)
 *     run.refused   // [{name, wanted, got, reason}]   requests kconfig did not keep
 *     run.unknown   // names the model does not define (for this ARCH)
 *     run.state     // the probe's captureState(), applied to the live engine on confirmation
 *
 * The live engine is never touched; `applyDryRun(live, run)` puts the probe's state into it, so the applied
 * result is exactly the one that was shown.
 */
import { KconfigEngine } from "./kconfig_engine.js";

const TRI_CHAR = ["n", "m", "y"];
export const NOT_SET = "(not set)";

const isTri = (s) => s.type === "bool" || s.type === "tristate";

/** {name: value} of every option as the user sees it: n/m/y for bool/tristate, the text (or null when not written) otherwise. */
export function valueMap(engine) {
  const out = {};
  for (const [name, s] of Object.entries(engine.syms)) {
    if (s.type === "unknown" || s.is_choice || !engine.isDefined(name)) continue;
    out[name] = isTri(s) ? TRI_CHAR[engine.symTri(name)] : engine.writes(name) ? engine.symStr(name) : null;
  }
  return out;
}

/** The request as the value map would show it (y/m/n for bool/tristate, hex with 0x). */
function normalizeWanted(s, value) {
  const v = String(value ?? "");
  if (isTri(s)) return v.slice(0, 1).toLowerCase() || "n";
  if (s.type === "hex" && v && !/^0x/i.test(v)) return `0x${v}`;
  return v;
}

/** Why an option ended at its value after the action (kconfig's own terms). */
export function changeReason(engine, name, from, to, requested) {
  const s = engine.syms[name];
  if (requested && requested.has(name)) return "requested";
  if (s.choice) {
    const sel = engine.choiceSelection ? engine.choiceSelection(s.choice) : null;
    return sel ? `choice: ${sel} selected` : "choice member";
  }
  if (isTri(s)) {
    const toTri = TRI_CHAR.indexOf(to);
    const fromTri = TRI_CHAR.indexOf(from);
    if (toTri > fromTri && s.rev_dep >= 0 && engine.revDep(name) >= toTri && toTri > 0) return `selected by ${engine.exprStr(s.rev_dep, true)}`;
    if (toTri < fromTri && s.dir_dep >= 0 && engine.dirDep(name) < fromTri) return `depends on ${engine.exprStr(s.dir_dep, true)}`;
    if (toTri < fromTri && s.rev_dep >= 0 && engine.revDep(name) < fromTri) return `no longer selected: ${engine.exprStr(s.rev_dep, true)}`;
  } else if (to === null && s.dir_dep >= 0 && engine.dirDep(name) === 0) {
    return `depends on ${engine.exprStr(s.dir_dep, true)}`;
  }
  if (s.prompts.length && engine.visibility(name) === 0) return `prompt hidden: ${s.prompts.map((p) => engine.exprStr(p, true)).join(" || ")}`;
  return "default";
}

/**
 * Run `mutate(probe, ctx)` on a throwaway engine started from `startState` and report the consequence.
 * `ctx.want(name, value)` sets a value and records the request; `ctx.expect(name, value)` only records it
 * (for mutations that load assignments). `mutate` may return `{unknown: [...]}`.
 */
export function kconfigDryRun(model, startState, mutate) {
  const probe = new KconfigEngine(model);
  if (startState) probe.restoreState(startState);
  const before = valueMap(probe);
  const requested = new Map();
  const unknown = new Set();
  const record = (name, value) => {
    if (!probe.syms[name] || probe.syms[name].type === "unknown") {
      unknown.add(name);
      return false;
    }
    requested.set(name, normalizeWanted(probe.syms[name], value));
    return true;
  };
  const ctx = {
    engine: probe,
    want: (name, value) => record(name, value) && probe.setValue(name, value),
    expect: (name, value) => record(name, value),
  };
  const extra = mutate(probe, ctx) || {};
  for (const name of extra.unknown || []) unknown.add(name);
  const after = valueMap(probe);
  const changes = [];
  for (const name of Object.keys(probe.syms)) {
    if (!(name in before) && !(name in after)) continue;
    const from = name in before ? before[name] : null;
    const to = name in after ? after[name] : null;
    if (from === to) continue;
    changes.push({ name, from: from ?? NOT_SET, to: to ?? NOT_SET, why: changeReason(probe, name, from, to, requested) });
  }
  const refused = [];
  for (const [name, wanted] of requested) {
    const got = after[name] ?? null;
    if (got === wanted || (got === null && wanted === "n")) continue;
    const s = probe.syms[name];
    const reason = probe.refusalReason(name, isTri(s) ? TRI_CHAR.indexOf(wanted) : wanted) || (got === null ? "not written: its dependencies are not met" : "kconfig resolved another value");
    refused.push({ name, wanted, got: got ?? NOT_SET, reason });
  }
  changes.sort((a, b) => a.name.localeCompare(b.name));
  refused.sort((a, b) => a.name.localeCompare(b.name));
  return { changes, refused, unknown: [...unknown].sort(), requested, before, after, state: probe.captureState(), engine: probe };
}

/** Put the state a dry run computed into the live engine (the caller then commits it as one history step). */
export function applyDryRun(live, run) {
  live.restoreState(run.state);
}

/** Changes other than the ones the user asked for (what §5.0 requires confirming). */
export function sideEffects(run) {
  return run.changes.filter((c) => !run.requested.has(c.name));
}

/** True when a preview has nothing to show: every section is empty (§5.0: then no dialog is shown). */
export function previewIsEmpty(sections) {
  return !(sections || []).some((s) => (s.rows || []).length);
}
