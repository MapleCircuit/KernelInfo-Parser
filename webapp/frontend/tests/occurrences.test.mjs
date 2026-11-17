/**
 * occurrences.test.mjs - The code view's usage highlighter (§5.21): set, file/version sync, clear, and tokens
 * without a position.
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Module singletons start intervals/timers: keep them unreffed so the test process exits cleanly.
for (const name of ["setInterval", "setTimeout"]) {
  const real = globalThis[name];
  globalThis[name] = (...args) => {
    const t = real(...args);
    t?.unref?.();
    return t;
  };
}

const DEV = {
  found: true, kind: "local", name: "dev", file: "drivers/pci/syscall.c",
  type: "struct pci_dev *", type_ref: { kind: "struct", name: "pci_dev", pointer: 1, array: false },
  type_def: { file: "include/linux/pci.h", line: 227, kind: "struct", name: "pci_dev" },
  decl: { line: 89, col: 18 }, init: null, function: { name: "sys_pciconfig_write", line_s: 86, line_e: 136 },
  access: "r", uses: [{ line: 89, col: 18, access: "decl" }, { line: 98, col: 2, access: "w" }, { line: 99, col: 7, access: "r" }],
  counts: { decl: 1, r: 5, w: 1, rw: 0, addr: 0 },
  inferred: [{ callee: "pci_get_bus_and_slot", line: 98, return_type: "struct pci_dev *", agrees: true, file: "drivers/pci/search.c", def_line: 154 }],
  effective_type: "struct pci_dev *", last_assign: { line: 98, col: 2 }, member: null,
};

test("OccurrenceHighlighter: set, sync on another file, clear, positionless tokens", async () => {
  const { OccurrenceHighlighter } = await import("../js/views/code_view/occurrences.js");
  let renders = 0;
  const calls = [];
  const view = {
    currentVersion: "v3.0", currentFilePath: "drivers/pci/syscall.c", fileDiff: null,
    rawLines: Object.assign(new Array(130).fill(""), { 88: "\tstruct pci_dev *dev;", 97: "\tdev = pci_get_bus_and_slot(bus, dfn);" }),
    updateVisibleLines: () => renders++,
    api: { getSymbolScope: async (...a) => { calls.push(a); return { found: false }; } },
  };
  const occ = new OccurrenceHighlighter(view);
  assert.equal(occ.set({ found: false }), false);
  assert.equal(occ.set(DEV, "dev"), true);
  assert.deepEqual([...occ.active.ranges.keys()], [89, 98]);
  assert.equal(renders, 1);
  view.currentFilePath = "drivers/pci/probe.c";
  occ.sync();
  assert.equal(occ.active, null, "another file drops the highlight");
  view.currentFilePath = "drivers/pci/syscall.c";
  occ.set({ scope: DEV });
  occ.clear();
  assert.equal(occ.active, null);
  assert.equal(renders, 3);
  // A token that is not in a row of a C file asks nothing and clears
  assert.equal(await occ.fromToken({ dataset: { name: "dev" }, closest: () => null }), null);
  assert.equal(calls.length, 0);
  occ.dispose();
});
