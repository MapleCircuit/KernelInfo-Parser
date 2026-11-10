/**
 * job_poll.js - Polling of background-job endpoints (webapp/API.md §2.15).
 *
 * An endpoint backed by a background job answers 202 Accepted with {state: "building", progress:
 * {done, total, note}, retry_after_ms} until its result is ready, then 200 with the body. `pollJob`
 * repeats the request with a backoff from 500 ms to 5 s (or the server's retry_after_ms, clamped to
 * that range), reports progress, and stops when the caller's AbortSignal fires. DOM-free so it can be
 * unit tested (webapp/frontend/tests/job_poll.test.mjs).
 */

export const JOB_POLL_MIN_MS = 500;
export const JOB_POLL_MAX_MS = 5000;

/** Delay before poll number `attempt` (0-based): doubling from 500 ms, capped at 5 s; the server's hint wins when larger. */
export function nextJobDelay(attempt, retryAfterMs = 0) {
  const backoff = Math.min(JOB_POLL_MAX_MS, JOB_POLL_MIN_MS * 2 ** Math.max(0, attempt));
  const hint = Number.isFinite(retryAfterMs) ? retryAfterMs : 0;
  return Math.max(JOB_POLL_MIN_MS, Math.min(JOB_POLL_MAX_MS, Math.max(backoff, hint)));
}

function abortError() {
  const err = new Error("aborted");
  err.name = "AbortError";
  return err;
}

/** Resolves after `ms`, or rejects with an AbortError when `signal` fires first. */
export function sleep(ms, signal = null) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const timer = setTimeout(() => {
      signal?.removeEventListener?.("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener?.("abort", onAbort, { once: true });
  });
}

/**
 * Polls until the job's result arrives. `fetchOnce()` resolves to {status, data}: 202 means "still
 * building" (data carries progress), anything else is the final answer. Resolves with that final data;
 * rejects with an AbortError when `signal` fires, or with whatever `fetchOnce` throws (HTTP 500 for a
 * failed job).
 */
export async function pollJob(fetchOnce, { onProgress = null, signal = null, wait = sleep, maxAttempts = Infinity } = {}) {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (signal?.aborted) throw abortError();
    const { status, data } = await fetchOnce();
    if (status !== 202) return data;
    if (onProgress) {
      try {
        onProgress({ ...(data?.progress || {}), state: data?.state || "building" });
      } catch (err) {
        console.warn("job progress callback failed:", err);
      }
    }
    await wait(nextJobDelay(attempt, data?.retry_after_ms), signal);
  }
  throw new Error("background job did not finish");
}

/** "3 / 10 · walking tags" style label for a progress object (empty when there is nothing to say). */
export function progressLabel(progress) {
  if (!progress) return "";
  const parts = [];
  if (progress.total > 0) parts.push(`${progress.done || 0} / ${progress.total}`);
  if (progress.note) parts.push(progress.note);
  return parts.join(" · ");
}

/** Fraction done in [0, 1], or null when the total is unknown. */
export function progressFraction(progress) {
  if (!progress || !(progress.total > 0)) return null;
  return Math.max(0, Math.min(1, (progress.done || 0) / progress.total));
}
