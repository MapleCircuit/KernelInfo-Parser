/**
 * kconfig_workflows.js - Configuration workflows of the KConfig tab (webapp/API.md §5.8, §5.0):
 *  - **Fragments…** (K2): merge `*.config` fragments like scripts/kconfig/merge_config.sh;
 *  - **Hardware…** (K1): trim / ensure / build in the modules a machine uses (`lsmod`, `lspci -k`);
 *  - **New options** (K3): review the options `make oldconfig` would ask about, one at a time.
 *
 * Every consequence is computed on a throwaway engine (kconfig_dry_run.js) and shown in the change preview
 * (components/change_preview.js); it is applied to the tab only on confirmation, as one undo step.
 */
import { api } from "../../api.js";
import { toast } from "../../components/toast.js";
import { openModal, escapeHtml } from "../../components/modal.js";
import { showChangePreview } from "../../components/change_preview.js";
import { applyDryRun, kconfigDryRun, sideEffects } from "./kconfig_dry_run.js";
import { fragmentDryRun, overlayFragments } from "./kconfig_fragments.js";
import { builtInModules, ensureModules, parseModuleList, trimModules } from "./hw_input.js";
import { evalBuild } from "./build_condition.js";
import { KconfigEngine } from "./kconfig_engine.js";

const plural = (n, word) => `${n.toLocaleString("en-US")} ${word}${n === 1 ? "" : "s"}`;

/** Apply a confirmed dry run to the tab as one history step. */
function commitRun(view, run) {
  applyDryRun(view.engine, run);
  view.changed();
}

// ------------------------------------------------------------------------------------------------ K2
export async function showFragmentsDialog(view) {
  if (!view.engine.loaded) return;
  let listing = [];
  try {
    listing = (await api.getKconfigFragments(view.currentVersion, view.currentArch)).fragments || [];
  } catch (err) {
    toast.error(`Could not list fragments: ${err.message}`);
  }
  const order = [];
  const modal = openModal({
    title: "Merge configuration fragments",
    subtitle: `merge_config.sh semantics: later fragments win, then kconfig resolves the result (${view.currentVersion}, ARCH=${view.currentArch})`,
    width: "720px",
    footerHtml: `<button class="code-btn active" id="kfrag-preview" type="button">Preview changes</button>`,
  });
  modal.body.innerHTML = `
    <div class="kfrag-list">${listing.length
      ? listing.map((f, i) => `<label class="kfrag-item"><input type="checkbox" data-idx="${i}" /> <span class="kfrag-order"></span> <code>${escapeHtml(f.path)}</code></label>`).join("")
      : `<div class="kconfig-muted">No in-tree fragments at ${escapeHtml(view.currentVersion)} (kernel/configs/*.config exists since v3.17).</div>`}</div>
    <label class="kconfig-muted" for="kfrag-text" style="display:block;margin:10px 0 4px;">Your fragment (applied last) — CONFIG_X=y, # CONFIG_Y is not set…</label>
    <textarea id="kfrag-text" class="kfrag-text" rows="8" spellcheck="false" placeholder="CONFIG_KVM=y&#10;# CONFIG_DEBUG_INFO is not set"></textarea>
    <div style="margin-top:6px;"><input type="file" id="kfrag-file" accept=".config,text/plain,*" /></div>`;
  modal.body.querySelectorAll("input[data-idx]").forEach((cb) => {
    cb.onchange = () => {
      const idx = Number(cb.dataset.idx);
      const at = order.indexOf(idx);
      if (cb.checked && at < 0) order.push(idx);
      if (!cb.checked && at >= 0) order.splice(at, 1);
      modal.body.querySelectorAll("input[data-idx]").forEach((other) => {
        const pos = order.indexOf(Number(other.dataset.idx));
        other.parentElement.querySelector(".kfrag-order").textContent = pos >= 0 ? `${pos + 1}.` : "";
      });
    };
  });
  const textEl = modal.body.querySelector("#kfrag-text");
  modal.body.querySelector("#kfrag-file").onchange = async (e) => {
    const file = e.target.files?.[0];
    if (file) textEl.value = await file.text();
  };
  modal.footer.querySelector("#kfrag-preview").onclick = async () => {
    const fragments = [];
    try {
      for (const idx of order) {
        const res = await api.getKconfigDefconfigContent(view.currentVersion, listing[idx].path, view.currentArch, view.toolchain);
        fragments.push({ name: listing[idx].name, text: res.content || "" });
      }
    } catch (err) {
      toast.error(`Could not read the fragment: ${err.message}`);
      return;
    }
    if (textEl.value.trim()) fragments.push({ name: "your fragment", text: textEl.value });
    if (!fragments.length) {
      toast.info("Tick a fragment or paste one");
      return;
    }
    modal.close();
    await mergeFragments(view, fragments);
  };
}

export async function mergeFragments(view, fragments) {
  const label = fragments.map((f) => f.name).join(" + ");
  if (!view.engineActive) {
    // Raw editing: the fragments' values are exactly what is asked for; nothing else changes
    const { merged, requested } = overlayFragments(view.rawValues || {}, fragments);
    const changed = Object.keys(requested).filter((k) => String((view.rawValues || {})[k] ?? "") !== String(requested[k]));
    if (!changed.length) {
      toast.info(`${label}: nothing to change`);
      return;
    }
    view.rawValues = merged;
    view.changed();
    toast.success(`Merged ${label}: ${plural(changed.length, "raw value")} set (engine off: not resolved)`);
    return;
  }
  const run = fragmentDryRun(view.engine.model, view.engine.captureState(), view.engine.configValues(), fragments);
  const side = sideEffects(run);
  const direct = run.changes.filter((c) => run.requested.has(c.name)).map((c) => ({ ...c, why: `from ${run.source[c.name]}` }));
  const notes = [
    ...run.unknown.length ? [`Unknown for ARCH=${view.currentArch} (ignored): ${run.unknown.join(" ")}`] : [],
    ...run.overridden.map((o) => `CONFIG_${o.name}: ${o.was} sets ${o.from}, ${o.by} redefines it to ${o.to}`),
  ];
  if (!side.length && !run.refused.length && !run.unknown.length) {
    if (!direct.length) {
      toast.info(`${label}: the configuration already has these values`);
      return;
    }
    commitRun(view, run);
    toast.success(`Merged ${label}: ${plural(direct.length, "value")} changed`);
    return;
  }
  const ok = await showChangePreview({
    title: `Merge ${label}?`,
    summary: `${plural(direct.length, "requested change")}, ${plural(side.length, "other change")}, ${run.refused.length} not honoured`,
    intro: "The fragments are overlaid on this configuration and kconfig resolves the result (olddefconfig). Close leaves the configuration exactly as it is.",
    sections: [
      { label: "Requested by the fragments", rows: direct },
      { label: "Other options that change", rows: side },
      { label: "Not honoured (value requested, not in the final .config)", rows: run.refused.map((r) => ({ name: r.name, from: r.wanted, to: r.got, why: r.reason })), columns: ["Option", "Requested", "Final", "Why"] },
    ],
    notes,
    confirmLabel: run.changes.length ? `Apply ${plural(run.changes.length, "change")}` : null,
    onInspect: (name) => view.selectAndInspectSymbol(name),
  });
  if (ok && run.changes.length) {
    commitRun(view, run);
    toast.success(`Merged ${label}`);
  }
}

// ------------------------------------------------------------------------------------------------ K1
const MODES = {
  trim: ["Trim (localmodconfig)", "Disable the modules (tristate options at m) the listed ones do not need"],
  ensure: ["Ensure", "Enable the listed modules the configuration does not build (with their dependencies)"],
  builtin: ["Built-in (localyesconfig)", "Build the listed modules into the kernel (m → y)"],
};

export function showHardwareDialog(view) {
  if (!view.engine.loaded) return;
  const modal = openModal({
    title: "Tailor this configuration to a machine",
    subtitle: `Paste lsmod, lspci -k or module names (${view.currentVersion}, ARCH=${view.currentArch})`,
    width: "720px",
    footerHtml: view.engineActive ? `<button class="code-btn active" id="khw-preview" type="button">Preview changes</button>` : `<button class="code-btn active" id="khw-engine" type="button">⚡ Enable Engine</button>`,
  });
  if (!view.engineActive) {
    modal.body.innerHTML = `<div class="kconfig-muted">These operations resolve options through kconfig, so they need this tab's engine. Enabling it first shows what resolving the current values changes (nothing is applied without confirmation).</div>`;
    modal.footer.querySelector("#khw-engine").onclick = async () => {
      modal.close();
      await view.setEngineActive(true);
      if (view.engineActive) showHardwareDialog(view);
    };
    return;
  }
  modal.body.innerHTML = `
    <textarea id="khw-text" class="kfrag-text" rows="10" spellcheck="false" placeholder="Module                  Size  Used by&#10;e1000e                286720  0&#10;snd_hda_intel          61440  3"></textarea>
    <div class="khw-modes">${Object.entries(MODES).map(([id, [label, help]], i) => `<label title="${escapeHtml(help)}"><input type="radio" name="khw-mode" value="${id}" ${i === 0 ? "checked" : ""} /> ${escapeHtml(label)}</label>`).join("")}</div>
    <div class="kconfig-muted khw-status" aria-live="polite"></div>`;
  const status = modal.body.querySelector(".khw-status");
  const btn = modal.footer.querySelector("#khw-preview");
  btn.onclick = async () => {
    const names = parseModuleList(modal.body.querySelector("#khw-text").value);
    if (!names.length) {
      status.textContent = "No module names found.";
      return;
    }
    const mode = modal.body.querySelector("input[name=khw-mode]:checked").value;
    btn.disabled = true;
    try {
      const plan = await planHardware(view, names, mode, (msg) => { status.textContent = msg; });
      modal.close();
      await confirmHardware(view, plan, mode);
    } catch (err) {
      status.textContent = `Failed: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  };
}

/** Compute the dry run of one hardware operation. */
export async function planHardware(view, names, mode, say = () => {}) {
  say(`Looking up ${plural(names.length, "module")}…`);
  const res = await api.getKbuildModules(view.currentVersion, view.currentArch, names, {
    gates: mode === "trim",
    onProgress: (p) => say(`Building the Kbuild index… ${p?.done ?? ""}/${p?.total ?? ""}`),
  });
  const modules = res.modules || {};
  const needed = [...new Set(Object.values(modules).flatMap((m) => m.required_symbols || []))];
  const engine = view.engine;
  const start = engine.captureState();
  const out = { names, modules, unmatched: res.unmatched || [], needed };
  if (mode === "trim") {
    say(`Trimming ${plural((res.module_gates || []).length, "module option")}…`);
    out.run = kconfigDryRun(engine.model, start, (p) => { out.trim = trimModules(p, needed, res.module_gates || []); });
  } else if (mode === "builtin") {
    out.run = kconfigDryRun(engine.model, start, (p) => { out.builtin = builtInModules(p, needed); });
  } else {
    const probe = new KconfigEngine(engine.model);
    probe.restoreState(start);
    const values = probe.configValues();
    const built = (m) => m.objects.some((o) => evalBuild(o.expr, (n) => values[n] ?? "n") !== "n");
    const missing = Object.fromEntries(Object.entries(modules).filter(([, m]) => !built(m)));
    out.alreadyBuilt = Object.keys(modules).length - Object.keys(missing).length;
    const solve = (name, value, current) => api.autosolveKconfig(view.currentVersion, {
      target_symbol: name, value, arch: view.currentArch, toolchain: view.toolchain, current_values: current,
    });
    out.ensure = await ensureModules(probe, missing, solve, { onProgress: (i, n, mod) => say(`Solving ${mod} (${i}/${n})…`) });
    out.run = kconfigDryRun(engine.model, start, (p) => {
      for (const t of out.ensure.toggles) p.setValue(t.symbol, t.set_to);
    });
  }
  return out;
}

async function confirmHardware(view, plan, mode) {
  const { run } = plan;
  const notes = [];
  if (plan.unmatched.length) notes.push(`Not matched (no object at ${view.currentVersion}, ARCH=${view.currentArch}: out of tree, renamed or another ARCH): ${plan.unmatched.join(" ")}`);
  if (mode === "ensure" && plan.alreadyBuilt) notes.push(`${plural(plan.alreadyBuilt, "listed module")} already built`);
  const neededBy = new Map();
  for (const [mod, m] of Object.entries(plan.modules)) for (const s of m.required_symbols || []) if (!neededBy.has(s)) neededBy.set(s, mod);
  const forOf = new Map((plan.ensure?.toggles || []).map((t) => [t.symbol, t.for]));
  const rows = run.changes.map((c) => {
    if (mode === "trim" && plan.trim.disabled.includes(c.name)) return { ...c, why: "not needed by the listed modules" };
    if (mode === "builtin" && plan.builtin.raised.includes(c.name)) return { ...c, why: `built in for ${neededBy.get(c.name) || "a listed module"}` };
    if (mode === "ensure" && forOf.has(c.name)) return { ...c, why: neededBy.get(c.name) === forOf.get(c.name) ? `needed by ${forOf.get(c.name)}` : `dependency of ${forOf.get(c.name)}` };
    return c;
  });
  const asked = new Set(mode === "trim" ? plan.trim.disabled : mode === "builtin" ? plan.builtin.raised : [...forOf.keys()]);
  const sections = [
    { label: mode === "trim" ? "Disabled" : mode === "builtin" ? "Built in" : "Enabled", rows: rows.filter((r) => asked.has(r.name)) },
    { label: "Other options that change as a consequence", rows: rows.filter((r) => !asked.has(r.name)) },
  ];
  if (plan.trim?.kept.length) sections.push({ label: "Kept (still at m)", rows: plan.trim.kept, fields: ["why"] });
  if (plan.builtin?.refused.length) sections.push({ label: "Not built in", rows: plan.builtin.refused, fields: ["why"] });
  if (plan.ensure?.blocked.length) sections.push({ label: "Blocked (auto-solve found no legal path)", rows: plan.ensure.blocked.map((b) => ({ name: b.name, from: b.module, why: b.why })), fields: ["from", "why"], columns: ["Option", "Module", "Why"] });
  if (!run.changes.length) {
    if (sections.slice(1).some((s) => s.rows.length) || notes.length) {
      await showChangePreview({ title: "Nothing to change", summary: "The configuration already matches these modules", sections, notes, confirmLabel: null, onInspect: (n) => view.selectAndInspectSymbol(n) });
    } else {
      toast.info("The configuration already matches these modules");
    }
    return;
  }
  const ok = await showChangePreview({
    title: `${MODES[mode][0]}: ${plural(run.changes.length, "change")}`,
    summary: `${plural(plan.names.length, "listed module")}, ${plan.unmatched.length} not matched`,
    intro: `${MODES[mode][1]}. Close leaves the configuration exactly as it is; Ctrl+Z undoes the whole change after applying it.`,
    sections,
    notes,
    confirmLabel: `Apply ${plural(run.changes.length, "change")}`,
    onInspect: (n) => view.selectAndInspectSymbol(n),
  });
  if (ok) {
    commitRun(view, run);
    toast.success(`${MODES[mode][0]}: ${plural(run.changes.length, "change")} applied`);
  }
}

// ------------------------------------------------------------------------------------------------ K3
/** The tab's pending review of new options, when it belongs to the configuration shown. */
export function pendingReview(view) {
  const r = view.newReview;
  return r && r.key === view.profileKey && Array.isArray(r.names) && r.idx < r.names.length ? r : null;
}

export function setReview(view, review) {
  view.newReview = review;
  view.persistTabState({ newReview: review ? { ...review } : null });
  renderReview(view);
}

/** Start a review of the options `make oldconfig` would ask about now (the standalone entry). */
export function startReview(view, { from = "", quiet = false } = {}) {
  if (!view.engine.loaded) return;
  if (!view.engineActive) {
    if (!quiet) toast.info("Enable the engine to review new options: they are the ones kconfig would ask about");
    return;
  }
  const names = view.engine.newSymbols().map((n) => n.name);
  if (!names.length) {
    if (!quiet) toast.info("No new options: every visible option has a value");
    setReview(view, null);
    return;
  }
  setReview(view, { key: view.profileKey, names, idx: 0, from, open: !quiet });
}

/** Banner (N new options — Review / Accept defaults) or the stepper, above the tree. */
export function renderReview(view) {
  const host = view.containerEl?.querySelector("#kconfig-new-banner");
  if (!host) return;
  const r = pendingReview(view);
  if (!r || !view.engine.loaded) {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  host.hidden = false;
  const left = r.names.length - r.idx;
  if (!r.open) {
    host.innerHTML = `<div class="knew-banner"><span><strong>${plural(left, "new option")}</strong>${r.from ? ` since ${escapeHtml(r.from)}` : ""}, set to their defaults</span>
      <button class="code-btn active" type="button" data-act="review">Review</button>
      <button class="code-btn" type="button" data-act="accept" title="Keep the defaults (nothing changes)">Accept defaults</button></div>`;
  } else {
    const name = r.names[r.idx];
    const e = view.engine;
    const s = e.syms[name];
    const entry = s ? e.newSymbols().find((n) => n.name === name) : null;
    const node = entry ? e.nodes[entry.nodeId] : null;
    let choices = "";
    const type = s ? e.getType(name) : "";
    if (s && s.is_choice) {
      choices = s.members.filter((m) => e.isDefined(m) && e.visibility(m) > 0).map((m) => `<button class="code-btn ${e.choiceSelection(name) === m ? "active" : ""}" type="button" data-set="${escapeHtml(m)}" data-val="y">${escapeHtml(e.nodes.find((n) => n.sym === m)?.prompt || m)}</button>`).join("");
    } else if (type === "bool" || type === "tristate") {
      choices = ["n", "m", "y"].filter((v, tri) => e.tristateWithinRange(name, tri)).map((v) => `<button class="code-btn ${e.value(name) === v ? "active" : ""}" type="button" data-set="${escapeHtml(name)}" data-val="${v}">${v}</button>`).join("");
    } else if (s) {
      choices = `<input class="knew-input" type="text" value="${escapeHtml(e.value(name))}" aria-label="Value of CONFIG_${escapeHtml(name)}" /><button class="code-btn" type="button" data-act="set-text">Set</button>`;
    }
    host.innerHTML = `<div class="knew-step">
      <div class="knew-head"><span class="kconfig-muted">New option ${r.idx + 1} / ${r.names.length}${r.from ? ` since ${escapeHtml(r.from)}` : ""}</span>
        <button class="code-btn" type="button" data-act="close" title="Hide the stepper (the review stays pending)">Hide</button></div>
      <div class="knew-path kconfig-muted">${escapeHtml((entry?.menuPath || []).join(" → "))}</div>
      <div class="knew-prompt"><strong>${escapeHtml(node?.prompt || name)}</strong> <code>${s && !s.is_choice ? `CONFIG_${escapeHtml(name)}` : "choice"}</code> <span class="kconfig-muted">${escapeHtml(type)}</span></div>
      ${entry?.depends ? `<div class="kconfig-muted knew-dep">depends on ${escapeHtml(entry.depends)}</div>` : ""}
      <div class="knew-values">${choices || `<span class="kconfig-muted">No longer asked (its menu is hidden now)</span>`}</div>
      <div class="knew-nav">
        <button class="code-btn" type="button" data-act="back" ${r.idx === 0 ? "disabled" : ""}>← Back</button>
        <button class="code-btn active" type="button" data-act="next">Keep ${escapeHtml(s ? (s.is_choice ? e.choiceSelection(name) || "default" : e.value(name) || '""') : "")} →</button>
        <button class="code-btn" type="button" data-act="accept" title="Keep the defaults of the remaining options (nothing changes)">Accept defaults for the rest</button>
      </div></div>`;
    if (s && !s.is_choice && view.selectedSymbol !== name) view.selectAndInspectSymbol(name, { keepFilter: true });
  }
  host.onclick = async (ev) => {
    const btn = ev.target.closest("button");
    if (!btn) return;
    const cur = pendingReview(view);
    if (!cur) return;
    const act = btn.dataset.act;
    if (act === "review") setReview(view, { ...cur, open: true });
    else if (act === "close") setReview(view, { ...cur, open: false });
    else if (act === "accept") {
      setReview(view, null);
      toast.success("New options keep their defaults");
    } else if (act === "back") setReview(view, { ...cur, idx: Math.max(0, cur.idx - 1) });
    else if (act === "next") advance(view, cur);
    else if (act === "set-text") {
      const input = host.querySelector(".knew-input");
      if (await view.applyValue(cur.names[cur.idx], input.value)) advance(view, cur);
    } else if (btn.dataset.set) {
      // Each answer is a direct request; lowering what other options need still asks first (§5.0)
      if (await view.applyValue(btn.dataset.set, btn.dataset.val)) advance(view, cur);
    }
  };
}

function advance(view, cur) {
  const idx = cur.idx + 1;
  if (idx >= cur.names.length) {
    setReview(view, null);
    toast.success(`Reviewed ${plural(cur.names.length, "new option")}`);
  } else {
    setReview(view, { ...cur, idx });
  }
}
