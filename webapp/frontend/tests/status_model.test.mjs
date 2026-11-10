import { test } from "node:test";
import assert from "node:assert/strict";
import { ago, formatDuration, formatMb, sortTables, statusLine } from "../js/views/status/status_model.js";

test("relative times, sizes, durations", () => {
  const now = 1_000_000_000_000;
  assert.equal(ago(now / 1000 - 10, now), "just now");
  assert.equal(ago(now / 1000 - 125, now), "2 min ago");
  assert.equal(ago(now / 1000 - 7200, now), "2 h ago");
  assert.equal(ago(0, now), "");
  assert.equal(formatMb(42291), "41.3 GB");
  assert.equal(formatMb(512), "512 MB");
  assert.equal(formatMb(2.5), "2.5 MB");
  assert.equal(formatDuration(45), "45 s");
  assert.equal(formatDuration(3725), "1 h 02 min");
});

test("settings line", () => {
  const now = 1_000_000_000_000;
  const st = { catalog: { parsed: 312 }, parsing: [{ version: "v3.2-rc4", updated_at: now / 1000 - 120, stale: false }], db: { total_mb: 42291 } };
  assert.equal(statusLine(st, now), "312 parsed · 1 parsing (v3.2-rc4, 2 min ago) · DB 41.3 GB");
  assert.equal(statusLine({ catalog: { parsed: 3 }, parsing: [], db: {} }, now), "3 parsed · idle");
  assert.equal(statusLine({ available: false }), "Database unavailable");
  assert.match(statusLine({ catalog: {}, parsing: [{ version: "v1", updated_at: 1, stale: true }] }, now), /1 stale/);
});

test("table sorting", () => {
  const t = [{ name: "b", data_mb: 1, index_mb: 1 }, { name: "a", data_mb: 5, index_mb: 0 }, { name: "c", data_mb: 0, index_mb: 3 }];
  assert.deepEqual(sortTables(t).map((x) => x.name), ["a", "c", "b"]);
  assert.deepEqual(sortTables(t, "name", "asc").map((x) => x.name), ["a", "b", "c"]);
  assert.deepEqual(sortTables(t, "index_mb", "desc").map((x) => x.name), ["c", "b", "a"]);
});
