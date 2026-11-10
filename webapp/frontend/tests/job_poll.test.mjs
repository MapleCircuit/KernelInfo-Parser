import { test } from "node:test";
import assert from "node:assert/strict";
import { nextJobDelay, pollJob, progressLabel, progressFraction, sleep } from "../js/utils/job_poll.js";

test("backoff doubles from 500 ms and caps at 5 s; the server hint can only raise it", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5].map((a) => nextJobDelay(a)), [500, 1000, 2000, 4000, 5000, 5000]);
  assert.equal(nextJobDelay(0, 3000), 3000);
  assert.equal(nextJobDelay(0, 60000), 5000);
  assert.equal(nextJobDelay(3, 100), 4000);
  assert.equal(nextJobDelay(0, NaN), 500);
});

test("pollJob reports progress until the final body", async () => {
  const answers = [
    { status: 202, data: { state: "building", progress: { done: 1, total: 4, note: "a" }, retry_after_ms: 500 } },
    { status: 202, data: { state: "building", progress: { done: 3, total: 4, note: "b" } } },
    { status: 200, data: { ok: true } },
  ];
  const seen = [];
  const waits = [];
  const data = await pollJob(async () => answers.shift(), {
    onProgress: (p) => seen.push(`${p.done}/${p.total}:${p.note}`),
    wait: async (ms) => { waits.push(ms); },
  });
  assert.deepEqual(data, { ok: true });
  assert.deepEqual(seen, ["1/4:a", "3/4:b"]);
  assert.deepEqual(waits, [500, 1000]);
});

test("pollJob stops on abort", async () => {
  const controller = new AbortController();
  let calls = 0;
  const p = pollJob(async () => { calls++; return { status: 202, data: {} }; }, { signal: controller.signal });
  setTimeout(() => controller.abort(), 20);
  await assert.rejects(p, { name: "AbortError" });
  assert.equal(calls, 1);
  await assert.rejects(sleep(10, controller.signal), { name: "AbortError" });
});

test("pollJob propagates failures", async () => {
  await assert.rejects(pollJob(async () => { throw new Error("background job failed: boom"); }), /boom/);
});

test("progress labels", () => {
  assert.equal(progressLabel({ done: 3, total: 10, note: "walking" }), "3 / 10 · walking");
  assert.equal(progressLabel({ note: "queued" }), "queued");
  assert.equal(progressLabel(null), "");
  assert.equal(progressFraction({ done: 5, total: 10 }), 0.5);
  assert.equal(progressFraction({ done: 5 }), null);
});
