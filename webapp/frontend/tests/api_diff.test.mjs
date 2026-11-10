import { test } from "node:test";
import assert from "node:assert/strict";
import { apiDiffMarkdown, apiDiffRows, apiDiffTsv, filterApiRows } from "../js/views/diff/api_diff.js";

const RES = {
  a: "v3.0", b: "v3.1", arch: "x86_64", kinds: ["syscalls", "exports"],
  syscalls: { added: [{ key: "getcpu/", nr: 309, args: "unsigned *cpu" }], removed: [], changed: [] },
  exports: { added: [], removed: [{ key: "old_fn", variant: "plain" }], changed: [{ key: "bc_svc", field: "variant", from: "plain", to: "gpl", path: "net/x.c" }] },
  counts: {},
};

test("rows, filters and exports of the API diff", () => {
  const rows = apiDiffRows(RES);
  assert.deepEqual(rows.map((r) => `${r.kind}:${r.status}:${r.key}`), ["syscalls:added:getcpu", "exports:removed:old_fn", "exports:changed:bc_svc"]);
  assert.equal(rows[2].detail, "variant: plain → gpl");
  assert.equal(filterApiRows(rows, new Set(["exports"]), new Set(), "").length, 2);
  assert.equal(filterApiRows(rows, new Set(), new Set(["added"]), "").length, 1);
  assert.equal(filterApiRows(rows, new Set(), new Set(), "svc").length, 1);
  const md = apiDiffMarkdown(RES, rows);
  assert.ok(md.startsWith("# Kernel API changes v3.0 → v3.1 (ARCH=x86_64)"));
  assert.ok(md.includes("- `getcpu` — nr 309 (unsigned *cpu)"));
  assert.equal(apiDiffTsv(rows).split("\n")[0], "kind\tstatus\tname\tdetail\tpath");
});
