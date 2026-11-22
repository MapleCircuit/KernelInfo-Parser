/**
 * worker_pool.js - Pool of static ES module Web Workers for NodeMap heavy work (`nodemap_worker.js`)
 * with lazy worker spawning, generation-token cancellation, AbortSignal support, FIFO queueing,
 * and automatic main-thread fallback in Node.js tests or environments where Workers fail/are absent.
 */
import { runWorkerJob } from "./nodemap_worker.js";

/**
 * Pool of `nodemap_worker.js` module workers with automatic main-thread fallback.
 */
export class NodeMapWorkerPool {
  /**
   * @param {object} [opts]
   * @param {number} [opts.maxWorkers]
   */
  constructor(opts = {}) {
    const { maxWorkers } = opts || {};
    const hw = (typeof navigator !== "undefined" && navigator.hardwareConcurrency) || 2;
    this.size = maxWorkers || Math.max(1, Math.min(4, hw - 1));
    this.workers = [];
    this.queue = [];
    this.nextId = 1;
    this.tokens = new Map();
    this.disposed = false;
    this._disposeEpoch = 0;
    this._workerFailures = 0;
    this._failedWorkerRef = null;
  }

  /** Whether Web Workers can currently be spawned by this pool. */
  get workerSupported() {
    return typeof Worker !== "undefined" && !this.disposed && this._failedWorkerRef !== Worker;
  }

  /** Increment and return the latest generation token for `key`. */
  nextToken(key) {
    if (!key) return 0;
    const next = (this.tokens.get(key) || 0) + 1;
    this.tokens.set(key, next);
    return next;
  }

  /** Whether `token` is still the latest generation for `key` (unkeyed jobs are always current). */
  isCurrent(key, token) {
    return !key || this.tokens.get(key) === token;
  }

  /** Invalidate any queued or in-flight jobs associated with `key`, resolving them to `null`. */
  cancel(key) {
    if (!key) return;
    this.nextToken(key);
    const kept = [];
    for (const job of this.queue) {
      if (job.key === key) this._settleCanceled(job);
      else kept.push(job);
    }
    this.queue = kept;
    for (const entry of this.workers) {
      for (const [id, job] of entry.pending) {
        if (job.key === key) {
          entry.pending.delete(id);
          this._settleCanceled(job);
        }
      }
    }
  }

  _settleCanceled(job) {
    if (job.settled) return;
    job.settled = true;
    job.cleanup?.();
    job.resolve(null);
  }

  _isStale(job) {
    return Boolean(
      job.settled ||
      job.signal?.aborted ||
      job.disposeEpoch !== this._disposeEpoch ||
      !this.isCurrent(job.key, job.token)
    );
  }

  /** Lazily spawn up to `this.size` module workers; returns `null` if creation is unsupported or throws. */
  _ensureWorker() {
    if (!this.workerSupported || this.workers.length >= this.size) return null;
    let worker;
    try {
      worker = new Worker(new URL("./nodemap_worker.js", import.meta.url), { type: "module" });
    } catch {
      if (typeof Worker !== "undefined") this._failedWorkerRef = Worker;
      return null;
    }
    const entry = { worker, busy: false, pending: new Map() };
    worker.onmessage = (ev) => this._onWorkerMessage(entry, ev);
    worker.onerror = (err) => this._onWorkerError(entry, err);
    this.workers.push(entry);
    return entry;
  }

  async _runFallback(job) {
    if (this._isStale(job)) {
      this._settleCanceled(job);
      return;
    }
    try {
      const res = await runWorkerJob(job.type, job.payload);
      if (this._isStale(job)) {
        this._settleCanceled(job);
        return;
      }
      job.settled = true;
      job.cleanup?.();
      job.resolve(res);
    } catch (err) {
      if (this._isStale(job)) {
        this._settleCanceled(job);
        return;
      }
      job.settled = true;
      job.cleanup?.();
      job.reject(err);
    }
  }

  _dispatch(entry, job) {
    if (this._isStale(job)) {
      this._settleCanceled(job);
      this._drainQueue();
      return;
    }
    const id = this.nextId++;
    entry.busy = true;
    entry.pending.set(id, job);
    try {
      entry.worker.postMessage({ id, type: job.type, payload: job.payload });
    } catch {
      entry.pending.delete(id);
      if (entry.pending.size === 0) entry.busy = false;
      this._runFallback(job);
      this._drainQueue();
    }
  }

  _onWorkerMessage(entry, ev) {
    const { id, ok, result, error } = ev?.data || {};
    const job = entry.pending.get(id);
    entry.pending.delete(id);
    if (entry.pending.size === 0) entry.busy = false;
    this._workerFailures = 0;
    this._failedWorkerRef = null;

    if (job && !job.settled) {
      if (this._isStale(job)) {
        this._settleCanceled(job);
      } else if (ok) {
        job.settled = true;
        job.cleanup?.();
        job.resolve(result);
      } else {
        // Worker returned an execution error: fall back to main thread so environment-specific issues recover
        this._runFallback(job);
      }
    }
    this._drainQueue();
  }

  _onWorkerError(entry, _err) {
    const idx = this.workers.indexOf(entry);
    if (idx !== -1) this.workers.splice(idx, 1);
    else return;
    try {
      entry.worker.terminate?.();
    } catch {
      // ignore termination failure on broken worker
    }
    const orphaned = [...entry.pending.values()];
    entry.pending.clear();
    entry.busy = false;

    this._workerFailures += 1;
    if (this._workerFailures >= this.size && typeof Worker !== "undefined") {
      this._failedWorkerRef = Worker;
    }

    for (const job of orphaned) {
      if (this.disposed || this._isStale(job)) this._settleCanceled(job);
      else this._runFallback(job);
    }
    this._drainQueue();
  }

  _drainQueue() {
    while (this.queue.length) {
      const next = this.queue[0];
      if (this._isStale(next)) {
        this.queue.shift();
        this._settleCanceled(next);
        continue;
      }
      let entry = this.workers.find((w) => !w.busy);
      if (!entry && this.workers.length < this.size) {
        entry = this._ensureWorker();
      }
      if (entry && !entry.busy) {
        this.queue.shift();
        this._dispatch(entry, next);
        continue;
      }
      // If no workers remain and spawning failed/is broken, drain remaining queued jobs to main-thread fallback
      if (this.workers.length === 0) {
        this.queue.shift();
        this._runFallback(next);
        continue;
      }
      break;
    }
  }

  /**
   * Execute `type` with `payload`, returning `null` if canceled via `key`, `signal`, or `dispose()`.
   *
   * @param {"tokenize"|"kconfig"|"layout"|"svg"|"png"} type
   * @param {object} [payload]
   * @param {object} [opts]
   * @param {string} [opts.key=""]
   * @param {AbortSignal|null} [opts.signal=null]
   * @returns {Promise<object|null>}
   */
  async exec(type, payload, opts = {}) {
    const { key = "", signal = null } = opts || {};
    if (this.disposed || signal?.aborted) return null;
    const token = key ? this.nextToken(key) : 0;
    const disposeEpoch = this._disposeEpoch;

    if (!this.workerSupported) {
      const res = await runWorkerJob(type, payload);
      if (signal?.aborted || disposeEpoch !== this._disposeEpoch || !this.isCurrent(key, token)) {
        return null;
      }
      return res;
    }

    let entry = this.workers.find((w) => !w.busy);
    if (!entry && this.workers.length < this.size) {
      entry = this._ensureWorker();
    }
    if (!entry && this.workers.length === 0) {
      const res = await runWorkerJob(type, payload);
      if (signal?.aborted || disposeEpoch !== this._disposeEpoch || !this.isCurrent(key, token)) {
        return null;
      }
      return res;
    }

    return new Promise((resolve, reject) => {
      const job = {
        type,
        payload,
        key,
        token,
        signal,
        disposeEpoch,
        settled: false,
        resolve,
        reject,
        cleanup: null,
      };
      if (signal && typeof signal.addEventListener === "function") {
        const onAbort = () => {
          const qIdx = this.queue.indexOf(job);
          if (qIdx !== -1) this.queue.splice(qIdx, 1);
          this._settleCanceled(job);
        };
        signal.addEventListener("abort", onAbort, { once: true });
        job.cleanup = () => signal.removeEventListener?.("abort", onAbort);
      }

      if (entry && !entry.busy) {
        this._dispatch(entry, job);
      } else {
        this.queue.push(job);
      }
    });
  }

  /** Tokenize source lines in a worker (or fallback). */
  tokenizeSource(payload, opts) {
    return this.exec("tokenize", payload, opts);
  }

  /** Batch-evaluate Kconfig visibility for struct nodes in a worker (or fallback). */
  evaluateKconfigBatch(payload, opts) {
    return this.exec("kconfig", payload, opts);
  }

  /** Compute DAG or force-directed graph layout in a worker (or fallback). */
  computeLayout(payload, opts) {
    return this.exec("layout", payload, opts);
  }

  /** Assemble standalone SVG document in a worker (or fallback). */
  assembleSvg(payload, opts) {
    return this.exec("svg", payload, opts);
  }

  /** Rasterize an SVG document to a PNG Blob via OffscreenCanvas in a worker (or fallback). */
  rasterizePng(payload, opts) {
    return this.exec("png", payload, opts);
  }

  /** Terminate all active workers, clear queued/pending jobs cleanly (resolving to `null`), and mark disposed. */
  dispose() {
    this.disposed = true;
    this._disposeEpoch += 1;
    for (const entry of this.workers) {
      for (const job of entry.pending.values()) {
        this._settleCanceled(job);
      }
      entry.pending.clear();
      try {
        entry.worker.terminate?.();
      } catch {
        // ignore termination errors
      }
    }
    this.workers = [];
    for (const job of this.queue) {
      this._settleCanceled(job);
    }
    this.queue = [];
    this.tokens.clear();
  }

  /** Re-enable worker spawning after a previous `dispose()`. */
  reset() {
    this.dispose();
    this.disposed = false;
    this.nextId = 1;
    this._workerFailures = 0;
    this._failedWorkerRef = null;
  }
}

export const workerPool = new NodeMapWorkerPool();
