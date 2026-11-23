/**
 * webdriver_bidi.mjs - Dependency-free WebDriver client for real-browser checks of the webapp (README.md).
 *
 * Talks to a running `geckodriver` (Firefox) over WebDriver classic for the session, navigation and
 * screenshots, and over WebDriver BiDi (`webSocketUrl: true`, Node's global `WebSocket`) for script
 * evaluation. Scripts MUST run through BiDi `script.evaluate` in the page realm: classic `execute/async`
 * runs in a sandbox whose dynamic `import()` gets a module graph of its own, so `state` & co. would be
 * separate instances that the app never sees.
 *
 *   const b = await openBrowser({ width: 1500, height: 1000 });
 *   await b.goto("http://127.0.0.1:8765/");
 *   const v = await b.evaluate(`${APP_PRELUDE} return state.panes.length;`);
 *   await b.screenshot("/path/out.png");
 *   await b.close();
 */
import { writeFileSync } from "node:fs";

export const DEFAULT_WEBDRIVER = process.env.WEBDRIVER || "http://127.0.0.1:4444";

/**
 * Page-script prelude: the app's own module instances (`state`, the `base` URL of the build's modules),
 * and `wait(fn, ms)` polling until `fn()` is truthy (null on timeout). Prepend it to `evaluate()` bodies.
 */
export const APP_PRELUDE = `
  const entry = document.querySelector('script[data-entry]').dataset.entry;
  const base = entry.replace(/app\\.js$/, "");
  const { state } = await import(base + "state.js");
  const wait = async (fn, ms = 15000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      let v = null;
      try { v = await fn(); } catch { v = null; }
      if (v) return v;
      await new Promise((r) => setTimeout(r, 100));
    }
    return null;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
`;

/**
 * Page-script helper: reads a Blob as base64 (`blobToBase64(blob)`). The app's CSP blocks `fetch()` of
 * `blob:` URLs, so a download is captured as its Blob (wrap `URL.createObjectURL`) and returned this way.
 */
export const BLOB_HELPER = `
  const blobToBase64 = async (blob) => {
    const buf = new Uint8Array(await blob.arrayBuffer());
    let bin = "";
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return btoa(bin);
  };
`;

export async function openBrowser({ webdriver = DEFAULT_WEBDRIVER, width = 1500, height = 1000, headless = true, scriptTimeoutMs = 120000 } = {}) {
  const wd = async (method, path, body) => {
    const r = await fetch(webdriver + path, { method, headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    const j = await r.json();
    if (j.value && j.value.error) throw new Error(`${j.value.error}: ${j.value.message}`);
    return j.value;
  };
  const args = [...(headless ? ["-headless"] : []), "-width", String(width), "-height", String(height)];
  const sess = await wd("POST", "/session", { capabilities: { alwaysMatch: { browserName: "firefox", webSocketUrl: true, "moz:firefoxOptions": { args } } } });
  const sid = sess.sessionId;
  const S = (p) => `/session/${sid}${p}`;
  await wd("POST", S("/timeouts"), { script: scriptTimeoutMs, pageLoad: 60000 });
  const ws = new WebSocket(sess.capabilities.webSocketUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) {
      pending.get(d.id)(d);
      pending.delete(d.id);
    }
  };
  const bidi = (method, params) => new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  return {
    wd,
    bidi,
    async goto(url, settleMs = 4000) {
      await wd("POST", S("/url"), { url });
      if (settleMs) await new Promise((r) => setTimeout(r, settleMs));
    },
    async refresh(settleMs = 5000) {
      await wd("POST", S("/refresh"), {});
      if (settleMs) await new Promise((r) => setTimeout(r, settleMs));
    },
    /**
     * Run `body` (the inside of an async function; may `await`, should `return` JSON-serializable data)
     * in the page realm. Resolves with the returned value; throws with the page's error and stack.
     */
    async evaluate(body, label = "evaluate") {
      const expression = `(async () => { try { const v = await (async () => { ${body} })(); return JSON.stringify({ ok: true, v }); } catch (e) { return JSON.stringify({ ok: false, e: String(e) + " | " + (e && e.stack) }); } })()`;
      const context = await wd("GET", S("/window"));
      const r = await bidi("script.evaluate", { expression, target: { context }, awaitPromise: true });
      if (r.error) throw new Error(`${label}: ${r.error}: ${r.message}`);
      if (r.result?.type === "exception") throw new Error(`${label}: ${JSON.stringify(r.result.exceptionDetails?.text)}`);
      const out = JSON.parse(r.result.result.value);
      if (!out.ok) throw new Error(`${label}: ${out.e}`);
      return out.v;
    },
    async screenshot(path) {
      writeFileSync(path, Buffer.from(await wd("GET", S("/screenshot")), "base64"));
    },
    async close() {
      try {
        ws.close();
      } catch {
        // already closed
      }
      await wd("DELETE", S("")).catch(() => {});
    },
  };
}
