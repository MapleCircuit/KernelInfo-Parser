/**
 * kconfig_fragments.js - Configuration fragments merged the way scripts/kconfig/merge_config.sh does (K2, webapp/API.md §5.8).
 *
 * Fragments (`kvm_guest.config`, a pasted partial .config…) are overlaid on the tab's values in order, later
 * ones winning, and the result is resolved by kconfig (olddefconfig). The dry run (S08) then reports every
 * change, every requested value kconfig did not keep ("Value requested for CONFIG_X not in final .config")
 * and the symbols the model does not define.
 */
import { KconfigParser } from "./kconfig_parser.js";
import { kconfigDryRun } from "./kconfig_dry_run.js";

/** {NAME: value} of one fragment (bare names; `# CONFIG_X is not set` is n). */
export function parseFragment(text) {
  const out = {};
  for (const [key, value] of Object.entries(KconfigParser.parse(text))) out[key.slice(7)] = value;
  return out;
}

/**
 * Overlay fragments `[{name, text}]` on `base` ({NAME: value}): `{merged, requested, source, overridden}`.
 * `requested` holds what the fragments ask for, `source` which fragment set each value, `overridden` the
 * values one fragment set and a later one changed (merge_config.sh's "Value of CONFIG_X is redefined").
 */
export function overlayFragments(base, fragments) {
  const merged = { ...(base || {}) };
  const requested = {};
  const source = {};
  const overridden = [];
  for (const frag of fragments || []) {
    for (const [name, value] of Object.entries(parseFragment(frag.text))) {
      if (name in requested && requested[name] !== value) overridden.push({ name, from: requested[name], to: value, by: frag.name, was: source[name] });
      merged[name] = value;
      requested[name] = value;
      source[name] = frag.name;
    }
  }
  return { merged, requested, source, overridden };
}

/** Dry-run a merge on a throwaway engine: the S08 result (its `requested` Map: what the fragments ask for)
 * plus the overlay's `merged`, `source` and `overridden`. */
export function fragmentDryRun(model, startState, currentValues, fragments) {
  const overlay = overlayFragments(currentValues, fragments);
  const run = kconfigDryRun(model, startState, (probe, ctx) => {
    for (const [name, value] of Object.entries(overlay.requested)) ctx.expect(name, value);
    const report = probe.loadAssignments(overlay.merged);
    probe.setAllNewSymbols("default");
    return { unknown: report.unknown };
  });
  return { ...run, merged: overlay.merged, source: overlay.source, overridden: overlay.overridden };
}
