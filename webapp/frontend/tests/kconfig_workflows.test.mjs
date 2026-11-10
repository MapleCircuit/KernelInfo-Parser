import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { KconfigEngine } from "../js/views/kconfig/kconfig_engine.js";
import { applyDryRun, kconfigDryRun, previewIsEmpty, sideEffects } from "../js/views/kconfig/kconfig_dry_run.js";
import { fragmentDryRun, overlayFragments, parseFragment } from "../js/views/kconfig/kconfig_fragments.js";
import { previewOrApply } from "../js/components/change_preview.js";

const RAW = JSON.parse(fs.readFileSync(new URL("./fixtures/kconfig_tiny.json", import.meta.url), "utf8"));

function engine(config = "") {
  const e = new KconfigEngine(RAW);
  e.loadConfig(config);
  e.setAllNewSymbols("default");
  return e;
}

test("dry run: side effects with kconfig reasons, live engine untouched", () => {
  const e = engine();
  const before = e.writeConfig();
  const run = kconfigDryRun(e.model, e.captureState(), (p, ctx) => ctx.want("NET", "n"));
  assert.equal(e.writeConfig(), before);
  assert.deepEqual(run.changes.map((c) => [c.name, c.from, c.to]), [["INET", "y", "n"], ["IPV6", "m", "n"], ["NET", "y", "n"]]);
  assert.equal(run.changes.find((c) => c.name === "NET").why, "requested");
  assert.equal(run.changes.find((c) => c.name === "INET").why, "depends on NET [=n]");
  assert.deepEqual(sideEffects(run).map((c) => c.name), ["INET", "IPV6"]);
  assert.deepEqual(run.refused, []);
  applyDryRun(e, run);
  assert.equal(e.value("INET"), "n");
});

test("dry run: select, choice and refused requests", () => {
  const e = engine();
  const off = kconfigDryRun(e.model, e.captureState(), (p, ctx) => ctx.want("FEATURE", "n"));
  assert.equal(off.changes.find((c) => c.name === "LIB").why, "no longer selected: FEATURE [=n]");
  const hz = kconfigDryRun(e.model, e.captureState(), (p, ctx) => ctx.want("HZ_250", "y"));
  assert.deepEqual(hz.changes.map((c) => [c.name, c.to, c.why]), [["HZ_100", "n", "choice: HZ_250 selected"], ["HZ_250", "y", "requested"]]);
  const kvm = kconfigDryRun(e.model, e.captureState(), (p, ctx) => ctx.want("KVM", "y"));
  assert.deepEqual(kvm.changes, []);
  assert.equal(kvm.refused.length, 1);
  assert.equal(kvm.refused[0].name, "KVM");
  assert.match(kvm.refused[0].reason, /prompt not visible: VIRT/);
  const unk = kconfigDryRun(e.model, e.captureState(), (p, ctx) => ctx.want("NOPE", "y"));
  assert.deepEqual(unk.unknown, ["NOPE"]);
});

test("previewOrApply: no dialog when nothing else changes; Close applies nothing", async () => {
  let shown = 0;
  let applied = 0;
  const show = async () => { shown++; return false; };
  const apply = () => { applied++; };
  assert.deepEqual(await previewOrApply({ title: "t", sections: [{ label: "x", rows: [] }], apply, show }), { applied: true, shown: false });
  assert.deepEqual(await previewOrApply({ title: "t", sections: [{ label: "x", rows: [{ name: "A" }] }], apply, show }), { applied: false, shown: true });
  assert.equal(shown, 1);
  assert.equal(applied, 1);
  assert.equal(previewIsEmpty([]), true);
});

test("fragments: parsing, overlay order and provenance", () => {
  assert.deepEqual(parseFragment('CONFIG_A=y\n# CONFIG_B is not set\nCONFIG_S="x y"\n# comment'), { A: "y", B: "n", S: "x y" });
  const o = overlayFragments({ A: "n", C: "m" }, [{ name: "one", text: "CONFIG_A=y\nCONFIG_B=y" }, { name: "two", text: "# CONFIG_B is not set" }]);
  assert.deepEqual(o.merged, { A: "y", B: "n", C: "m" });
  assert.deepEqual(o.source, { A: "one", B: "two" });
  assert.deepEqual(o.overridden, [{ name: "B", from: "y", to: "n", by: "two", was: "one" }]);
});

test("fragments: merge reports changes, values not honoured and unknown symbols", () => {
  const e = engine();
  const cur = e.configValues();
  const before = e.writeConfig();
  const same = fragmentDryRun(e.model, e.captureState(), cur, [{ name: "same", text: "CONFIG_NET=y\nCONFIG_LOG_BUF=17\n" }]);
  assert.deepEqual(same.changes, []);
  assert.deepEqual(same.refused, []);
  const kvm = fragmentDryRun(e.model, e.captureState(), cur, [{ name: "kvm", text: "CONFIG_KVM=y\nCONFIG_LOG_BUF=19\nCONFIG_MISSING=y\n" }]);
  assert.deepEqual(kvm.changes.map((c) => [c.name, c.from, c.to]), [["LOG_BUF", "17", "19"]]);
  assert.deepEqual(kvm.refused.map((r) => r.name), ["KVM"]);
  assert.deepEqual(kvm.unknown, ["MISSING"]);
  assert.ok(kvm.requested instanceof Map && kvm.requested.get("LOG_BUF") === "19");
  assert.deepEqual(sideEffects(kvm), []);
  assert.equal(kvm.source.LOG_BUF, "kvm");
  const virt = fragmentDryRun(e.model, e.captureState(), cur, [{ name: "virt", text: "CONFIG_VIRT=y\nCONFIG_KVM=m\n" }]);
  assert.deepEqual(virt.changes.map((c) => [c.name, c.to]), [["KVM", "m"], ["VIRT", "y"]]);
  assert.deepEqual(virt.refused, []);
  assert.equal(e.writeConfig(), before, "a dry run leaves the configuration byte-identical");
});

test("newSymbols: oldconfig's questions in menu order", () => {
  const e = new KconfigEngine(RAW);
  e.loadConfig("CONFIG_MODULES=y\nCONFIG_NET=y\nCONFIG_INET=y\nCONFIG_FEATURE=y\n# CONFIG_VIRT is not set\nCONFIG_HZ_100=y\nCONFIG_LOG_BUF=17\n");
  const names = e.newSymbols().map((n) => [n.name, n.kind, n.value]);
  assert.deepEqual(names, [["IPV6", "symbol", "m"], ["NAME", "symbol", "tiny"], ["CRYPTO", "symbol", "n"], ["DM_CRYPT", "symbol", "n"]]); // CRYPTO_AES: its menu (depends on CRYPTO) is hidden
  assert.deepEqual(e.newSymbols()[0].menuPath, ["Networking", "net", "inet"]); // implicit submenus (menu_finalize)
  const fresh = new KconfigEngine(RAW);
  fresh.loadConfig("CONFIG_MODULES=y\n");
  const kinds = fresh.newSymbols().map((n) => `${n.name}:${n.kind}`);
  assert.ok(kinds.includes("CHOICE_0:choice") || kinds.some((k) => k.endsWith(":choice")), kinds.join(" "));
  assert.ok(!kinds.some((k) => k.startsWith("KVM") || k.startsWith("LIB") || k.startsWith("HZ_")), kinds.join(" "));
  // Accepting every default equals the old carry (no user values for new options) byte for byte
  const a = new KconfigEngine(RAW);
  a.loadConfig("CONFIG_MODULES=y\nCONFIG_NET=y\n");
  const carried = a.writeConfig();
  a.setAllNewSymbols("default");
  assert.equal(a.writeConfig(), carried);
});

import { builtInModules, ensureModules, parseModuleList, trimModules } from "../js/views/kconfig/hw_input.js";

test("hardware input: lsmod, lspci -k and plain lists", () => {
  const lsmod = "Module                  Size  Used by\nsnd_hda_intel          61440  3\ne1000e                286720  0\next4                  1007616  2\nmbcache                16384  1 ext4\n";
  assert.deepEqual(parseModuleList(lsmod), ["snd_hda_intel", "e1000e", "ext4", "mbcache"]);
  const lspci = "00:19.0 Ethernet controller: Intel Corporation 82574L\n\tSubsystem: Dell Device 04aa\n\tKernel driver in use: e1000e\n\tKernel modules: e1000e\n00:1b.0 Audio device: Intel\n\tKernel modules: snd_hda_intel, snd-sof-pci\n";
  assert.deepEqual(parseModuleList(lspci), ["e1000e", "snd_hda_intel", "snd_sof_pci"]);
  assert.deepEqual(parseModuleList("kvm-intel, kvm\n  ext4 ;rm -rf"), ["kvm_intel", "kvm", "ext4"]);
});

test("trim: localmodconfig fixed point keeps what needed modules need", () => {
  const e = engine("CONFIG_MODULES=y\nCONFIG_VIRT=y\nCONFIG_KVM=m\nCONFIG_CRYPTO=m\nCONFIG_CRYPTO_AES=m\nCONFIG_DM_CRYPT=m\n");
  const before = e.writeConfig();
  let trim;
  const run = kconfigDryRun(e.model, e.captureState(), (p) => { trim = trimModules(p, ["CRYPTO_AES", "IPV6"], ["CRYPTO", "CRYPTO_AES", "DM_CRYPT", "KVM", "IPV6", "NET"]); });
  assert.deepEqual(trim.disabled, ["DM_CRYPT", "KVM"]);
  assert.deepEqual(trim.kept, [{ name: "CRYPTO", why: "needed by CRYPTO_AES" }]);
  assert.deepEqual(run.changes.map((c) => [c.name, c.from, c.to]), [["DM_CRYPT", "m", "n"], ["KVM", "m", "n"]]);
  assert.equal(e.writeConfig(), before);
  const yes = kconfigDryRun(e.model, e.captureState(), (p) => builtInModules(p, ["CRYPTO_AES", "CRYPTO"]));
  assert.deepEqual(yes.changes.map((c) => [c.name, c.to]), [["CRYPTO", "y"], ["CRYPTO_AES", "y"]]);
});

test("ensure: auto-solve toggles applied in order, blocked options reported", async () => {
  const e = engine("CONFIG_MODULES=y\n");
  const probe = new KconfigEngine(e.model);
  probe.restoreState(e.captureState());
  const calls = [];
  const solve = async (name, value) => {
    calls.push(`${name}=${value}`);
    if (name === "KVM") return { toggles_needed: [{ symbol: "VIRT", set_to: "y" }, { symbol: "KVM", set_to: "m" }] };
    return { toggles_needed: [], blocked_by: ["unreachable"] };
  };
  const res = await ensureModules(probe, { kvm: { required_symbols: ["KVM"] }, aes: { required_symbols: ["CRYPTO_AES"] } }, solve);
  assert.deepEqual(calls, ["KVM=m", "CRYPTO_AES=m"]);
  assert.deepEqual(res.toggles.map((t) => `${t.symbol}=${t.set_to}`), ["VIRT=y", "KVM=m"]);
  assert.deepEqual(res.blocked, [{ name: "CRYPTO_AES", module: "aes", why: "unreachable" }]);
  assert.equal(probe.value("KVM"), "m");
  assert.equal(e.value("KVM"), "n");
});
