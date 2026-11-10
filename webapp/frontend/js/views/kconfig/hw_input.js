/**
 * hw_input.js - Tailor a configuration to a machine's modules (K1, webapp/API.md §5.8 "Hardware…").
 *
 * Input parsing (`lsmod`, `lspci -k`, plain lists) and the three operations, all run on a throwaway engine:
 *  - trim (`make localmodconfig`, scripts/kconfig/streamline_config.pl): tristate options at m that gate a
 *    module-capable object and that no listed module needs go to n, to a fixed point; an option whose removal
 *    would lower a needed one is kept;
 *  - ensure: the listed modules that are not built get their required options raised (through auto-solve);
 *  - built-in (`make localyesconfig`): the listed modules' required tristate options go from m to y.
 */

const MODNAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$/;
const norm = (name) => name.replace(/-/g, "_");

/** Module names from `lsmod`, `lspci -k` or a whitespace/comma separated list (deduplicated, `-` -> `_`). */
export function parseModuleList(text) {
  const out = [];
  const seen = new Set();
  const add = (raw) => {
    const name = String(raw || "").trim();
    if (!MODNAME_RE.test(name)) return;
    const n = norm(name);
    if (!seen.has(n)) {
      seen.add(n);
      out.push(n);
    }
  };
  const lines = String(text || "").split(/\r?\n/);
  const isLsmod = lines.some((l) => /^Module\s+Size\s+Used by/.test(l));
  const isLspci = lines.some((l) => /^\s*Kernel (driver in use|modules):/.test(l));
  for (const line of lines) {
    if (isLspci) {
      const m = /^\s*Kernel (?:driver in use|modules):\s*(.*)$/.exec(line);
      if (m) m[1].split(/[,\s]+/).forEach(add);
    } else if (isLsmod) {
      if (/^Module\s+Size/.test(line)) continue;
      const m = /^(\S+)\s+\d+\s+\d+/.exec(line);
      if (m) add(m[1]);
    } else {
      line.split(/[,\s]+/).forEach(add);
    }
  }
  return out;
}

const TRI = ["n", "m", "y"];

/**
 * localmodconfig on `probe` (mutated): returns {disabled: [name], kept: [{name, why}]}.
 * `needed` are the symbols the listed modules require; `gates` the symbols gating any module-capable object.
 */
export function trimModules(probe, needed, gates, { maxPasses = 4 } = {}) {
  const keep = new Set(needed);
  const neededNow = () => new Map([...keep].filter((n) => probe.isDefined(n)).map((n) => [n, probe.symTri(n)]));
  const kept = new Map();
  const disabled = new Set();
  for (let pass = 0; pass < maxPasses; pass++) {
    let progressed = false;
    const candidates = gates.filter((n) => !keep.has(n) && !disabled.has(n) && probe.isDefined(n)
      && probe.syms[n].type === "tristate" && probe.symTri(n) === 1);
    for (const name of candidates) {
      if (probe.symTri(name) !== 1) continue; // lowered already by an earlier step
      const base = neededNow();
      const saved = probe.captureState();
      if (!probe.setValue(name, "n") || probe.symTri(name) !== 0) {
        probe.restoreState(saved);
        kept.set(name, probe.refusalReason(name, 0) || "kconfig keeps it");
        continue;
      }
      const lost = [...base].find(([n, v]) => probe.symTri(n) < v);
      if (lost) {
        probe.restoreState(saved);
        kept.set(name, `needed by ${lost[0]}`);
        continue;
      }
      kept.delete(name);
      disabled.add(name);
      progressed = true;
    }
    if (!progressed) break;
  }
  return { disabled: [...disabled].sort(), kept: [...kept].map(([name, why]) => ({ name, why })).sort((a, b) => a.name.localeCompare(b.name)) };
}

/** localyesconfig for the listed modules: their required tristate options at m go to y. */
export function builtInModules(probe, needed) {
  const raised = [];
  let pending = needed.filter((n) => probe.isDefined(n) && probe.syms[n].type === "tristate" && probe.symTri(n) === 1);
  // A dependency may be raised after the option that needs it: repeat until nothing more moves
  for (let progressed = true; progressed && pending.length;) {
    progressed = false;
    const next = [];
    for (const name of pending) {
      if (probe.symTri(name) === 2) continue;
      if (probe.setValue(name, "y") && probe.symTri(name) === 2) {
        raised.push(name);
        progressed = true;
      } else {
        next.push(name);
      }
    }
    pending = next;
  }
  const refused = pending.map((name) => ({ name, why: probe.refusalReason(name, 2) || "kconfig keeps it at m" }));
  return { raised, refused };
}

/**
 * Ensure the listed modules build: for each required option at n, ask `solve(name, value, currentValues)`
 * (auto-solve: {toggles_needed: [{symbol, set_to}], blocked_by}) and apply its toggles on `probe`.
 * Resolves {toggles: [{symbol, set_to, for}], blocked: [{name, module, why}]}; at most `limit` modules.
 */
export async function ensureModules(probe, modules, solve, { limit = 200, onProgress = null } = {}) {
  const toggles = [];
  const blocked = [];
  const entries = Object.entries(modules).slice(0, limit);
  let i = 0;
  for (const [mod, info] of entries) {
    if (onProgress) onProgress(++i, entries.length, mod);
    for (const name of info.required_symbols || []) {
      if (!probe.isDefined(name) || probe.symTri(name) > 0) continue;
      const want = probe.syms[name].type === "tristate" && probe.isDefined(probe.modules) && probe.symTri(probe.modules) === 2 ? "m" : "y";
      let res;
      try {
        res = await solve(name, want, probe.configValues());
      } catch (err) {
        blocked.push({ name, module: mod, why: err.message || String(err) });
        break;
      }
      for (const t of res.toggles_needed || []) {
        const to = t.set_to || t.to || "y";
        if (probe.setValue(t.symbol, to)) toggles.push({ symbol: t.symbol, set_to: to, for: mod });
      }
      if (probe.symTri(name) === 0) {
        blocked.push({ name, module: mod, why: (res.blocked_by || []).join("; ") || "no legal path" });
        break; // the first blocked option of a module stops it
      }
    }
  }
  return { toggles, blocked };
}

/** Value letter of a tristate (for tests and labels). */
export const triChar = (probe, name) => TRI[probe.symTri(name)];
