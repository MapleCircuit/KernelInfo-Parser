import { test } from "node:test";
import assert from "node:assert/strict";
import { buildSessionExport, planSessionImport, resolveSessionImport, validateSessionFile } from "../js/utils/session_transfer.js";

const SESSION = {
  name: "Work", version: "v3.0", splitMode: "single", activePaneId: "pane-1",
  panes: [{ id: "pane-1", activeTabId: "t1", tabs: [
    { id: "t1", type: "code", title: "main.c", path: "init/main.c", version: "v3.0" },
    { id: "t2", type: "diff", title: "Compare", versionA: "v3.0", versionB: "v3.1" },
    { id: "t3", type: "kconfig", title: "KConfig (x86_64)", arch: "x86_64", version: "v3.0" },
  ] }],
  kconfigEngineTabIds: ["t3"], kconfigEngineProfiles: { t3: { version: "v3.0", arch: "x86_64", configName: "", profileKey: "v3.0|x86_64" } },
};
const PROFILES = { "v3.0|x86_64": { EXT4_FS: "y", KVM: "m" }, "v3.1|arm": { X: "y" } };

test("export holds the session and only the configurations it uses", () => {
  const doc = buildSessionExport({ session: SESSION, profiles: PROFILES, toolchains: {}, userRecords: [{ id: "x" }], now: 0 });
  assert.deepEqual(Object.keys(doc.kconfig.profiles), ["v3.0|x86_64"]);
  assert.equal(doc.user.records.length, 1);
  assert.deepEqual(Object.keys(buildSessionExport({ session: SESSION, profiles: PROFILES, includeAll: true }).kconfig.profiles).sort(), Object.keys(PROFILES).sort());
  const back = validateSessionFile(JSON.stringify(doc));
  assert.equal(back.session.panes[0].tabs.length, 3);
  assert.deepEqual(back.profiles["v3.0|x86_64"], PROFILES["v3.0|x86_64"]);
});

test("bad files are refused", () => {
  const doc = buildSessionExport({ session: SESSION, profiles: PROFILES });
  const bad = (mutate) => { const d = structuredClone(doc); mutate(d); return JSON.stringify(d); };
  assert.throws(() => validateSessionFile(bad((d) => { d.session.panes[0].tabs[0].type = "evil"; })), /Unknown tab type/);
  assert.throws(() => validateSessionFile(bad((d) => { d.session.panes[0].tabs[0].path = "../../etc/passwd"; })), /Invalid path/);
  assert.throws(() => validateSessionFile(bad((d) => { d.format = "x"; })), /Not a KernelInfo/);
  assert.throws(() => validateSessionFile("{"), /JSON/);
  assert.throws(() => validateSessionFile("x".repeat((5 << 20) + 1)), /5 MB/);
});

test("import plan and conflict choices", () => {
  const imported = validateSessionFile(JSON.stringify(buildSessionExport({ session: SESSION, profiles: { ...PROFILES, "v3.1|arm": { X: "y" } }, includeAll: true })));
  const local = { "v3.0|x86_64": { EXT4_FS: "n" }, "v3.1|arm": { X: "y" } };
  const plan = planSessionImport(imported, local);
  assert.deepEqual(plan, { adds: [], same: ["v3.1|arm"], conflicts: [{ key: "v3.0|x86_64", local: 1, imported: 2 }] });
  const keep = resolveSessionImport(imported, plan, {}, () => "v2");
  assert.deepEqual(keep.profiles, {}, "Keep mine writes nothing");
  const use = resolveSessionImport(imported, plan, { "v3.0|x86_64": "use" }, () => "v2");
  assert.deepEqual(use.profiles, { "v3.0|x86_64": { EXT4_FS: "y", KVM: "m" } });
  const copy = resolveSessionImport(imported, plan, { "v3.0|x86_64": "copy" }, () => "v2");
  assert.deepEqual(Object.keys(copy.profiles), ["v3.0|x86_64|v2"]);
  assert.equal(copy.session.panes[0].tabs[2].configName, "v2", "the imported tab follows its copy");
  assert.equal(copy.session.kconfigEngineProfiles.t3.profileKey, "v3.0|x86_64|v2");
  assert.equal(imported.session.panes[0].tabs[2].configName, undefined, "the plan input is not mutated");
});
