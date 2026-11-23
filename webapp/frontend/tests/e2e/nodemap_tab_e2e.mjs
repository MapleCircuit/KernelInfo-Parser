/**
 * nodemap_tab_e2e.mjs - Real-browser check of NodeMap tab nodes (README.md; `webapp/API_FRONTEND.md` §5.3.1, §5.3.4).
 *
 * Needs a running webapp (BASE, default http://127.0.0.1:8765/) and geckodriver (WEBDRIVER, default
 * http://127.0.0.1:4444). Opens a NodeMap with a live code tab node and a note, then checks:
 *   keep-alive      the tab iframe survives switching to another workspace tab and back (no reload)
 *   keep changes    scrolling inside the tab lands in node.data.viewState and the stored canvas, no reload
 *   png             PNG export embeds the live tab (report: dom / painted / placeholder), frame survives
 *   fallback        with every <foreignObject> snapshot refused, the tab is painted from its DOM (and the
 *                   2× supersampling comes back after the export)
 *   frozen          in-tab changes are not saved, the node turns dirty, returning to the NodeMap resets it
 *   reload          after a page reload the node comes back at its saved hash and scroll position
 * Writes nodemap_<theme>.png, nodemap_fallback_<theme>.png and screen_<theme>.png to OUT (default: cwd).
 * Exit code 1 when a check fails.
 *
 *   THEME=light OUT=/tmp/e2e node webapp/frontend/tests/e2e/nodemap_tab_e2e.mjs
 */
import { APP_PRELUDE, BLOB_HELPER, openBrowser } from "./webdriver_bidi.mjs";
import { writeFileSync } from "node:fs";

const BASE = process.env.BASE || "http://127.0.0.1:8765/";
const OUT = process.env.OUT || ".";
const THEME = process.env.THEME || "dark";
const CODE_HASH = process.env.CODE_HASH || "repo=linux&v=v3.0&tab=code&file=init%2Fmain.c";

const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok: Boolean(ok) });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

// The controller of the NodeMap opened below (captured through its prototype) and its tab node's frame
const NM = `
  const nm = window.__nm;
  const tabNode = nm.canvas.nodes.find((n) => n.type === "tab");
  const frameOf = () => nm.renderer.element(tabNode.id).querySelector("iframe.nm-iframe");
  const scrollerOf = (f) => [...f.contentDocument.querySelectorAll("*")].find((el) => el.scrollHeight > el.clientHeight + 200 && /(auto|scroll)/.test(getComputedStyle(el).overflowY));
  const showTab = (id) => {
    const pane = state.panes.find((p) => p.tabs.some((t) => t.id === id));
    pane.activeTabId = id;
    state.emit("tabs:changed", { paneId: pane.id, activeTabId: id });
  };
  const otherTabId = () => state.panes.find((p) => p.tabs.some((t) => t.id === nm.tab.id)).tabs.find((t) => t.id !== nm.tab.id).id;
`;
// Export a PNG through the controller, capturing the downloaded Blob (CSP blocks fetching blob: URLs)
const EXPORT = (opts) => `
  let blob = null;
  const origClick = HTMLAnchorElement.prototype.click;
  const origCreate = URL.createObjectURL;
  URL.createObjectURL = (b) => { if (b && b.type === "image/png") blob = b; return origCreate.call(URL, b); };
  HTMLAnchorElement.prototype.click = function () { if (!this.download) origClick.call(this); };
  let res;
  try { res = await nm.exportPNG(${opts}); } finally { HTMLAnchorElement.prototype.click = origClick; URL.createObjectURL = origCreate; }
  const b64 = blob ? await blobToBase64(blob) : "";
`;

const b = await openBrowser();
try {
  await b.goto(BASE);
  await b.evaluate(`${APP_PRELUDE} localStorage.setItem("kip-theme", ${JSON.stringify(THEME)}); document.documentElement.dataset.theme = ${JSON.stringify(THEME)}; return true;`, "theme");
  const nmId = await b.evaluate(`${APP_PRELUDE}
    const NMC = await import(base + "views/nodemap/nodemap_controller.js");
    const orig = NMC.NodeMapController.prototype.commit;
    NMC.NodeMapController.prototype.commit = function (...a) { window.__nm = this; return orig.apply(this, a); };
    state.openTab({ type: "code", title: "main.c", path: "init/main.c", version: "v3.0" });
    state.openTab({ type: "nodemap", title: "NodeMap Canvas", forceNew: true, initialNodes: [
      { type: "tab", x: 40, y: 40, w: 760, h: 520, data: { tabType: "code", hash: ${JSON.stringify(CODE_HASH)} } },
      { type: "note", x: 840, y: 60, w: 260, h: 140, data: { text: "Native note node" } },
    ] });
    const nm = await wait(() => window.__nm && window.__nm.canvas?.nodes.length >= 2 && window.__nm);
    return nm ? nm.tab.id : null;`, "open nodemap");
  check("nodemap opened", nmId, nmId);

  const loaded = await b.evaluate(`${APP_PRELUDE}${NM}
    const ok = await wait(() => frameOf().contentDocument.readyState === "complete" && frameOf().contentDocument.querySelector("#workspace .tab-content-container *"), 30000);
    if (ok) frameOf().contentWindow.__marker = 4242;
    return { ok: Boolean(ok), src: frameOf().src };`, "frame load");
  check("tab iframe loaded, reporting to its node", loaded.ok && /embedNode=/.test(loaded.src), loaded.src);

  const kept = await b.evaluate(`${APP_PRELUDE}${NM}
    showTab(otherTabId()); await sleep(800);
    const parked = nm.rootEl.closest(".tab-keepalive-host").classList.contains("is-parked");
    showTab(nm.tab.id); await sleep(800);
    return { parked, marker: frameOf().contentWindow.__marker };`, "keep-alive");
  check("keep-alive: parked while away, iframe not reloaded", kept.parked && kept.marker === 4242, JSON.stringify(kept));

  const kc = await b.evaluate(`${APP_PRELUDE}${NM}
    const sc = await wait(() => scrollerOf(frameOf()), 20000);
    if (!sc) return { ok: false };
    sc.scrollTop = 600; sc.dispatchEvent(new Event("scroll"));
    // the opening report already gave the node a view state: wait for the one carrying the scroll
    await wait(() => tabNode.data.viewState?.fields?.scrollTop === 600 || tabNode.data.viewState?.scroll, 5000);
    nm.flushSave(); await sleep(500);
    const { localDB } = await import(base + "db.js");
    const M = await import(base + "views/nodemap/canvas_model.js");
    const stored = await M.loadCanvasAsync(localDB, nm.storageKey, null);
    return { ok: true, stored: stored?.canvas?.nodes.find((n) => n.id === tabNode.id)?.data.viewState, marker: frameOf().contentWindow.__marker };`, "keep changes");
  // a code view keeps its scroll in a tab field (scrollTop); other views in scroll memory keys
  check("keep changes: scroll saved with the canvas, no reload", kc.ok && kc.stored && (kc.stored.fields?.scrollTop === 600 || kc.stored.scroll) && kc.marker === 4242, JSON.stringify(kc.stored));

  const png = await b.evaluate(`${APP_PRELUDE}${BLOB_HELPER}${NM}${EXPORT("{ scale: 2 }")}
    return { report: res?.report, w: res?.w, h: res?.h, b64, marker: frameOf().contentWindow.__marker };`, "png");
  if (png.b64) writeFileSync(`${OUT}/nodemap_${THEME}.png`, Buffer.from(png.b64, "base64"));
  check(`png (${THEME}): live tab rasterized, frame survives`, png.b64 && png.report?.dom.length === 1 && png.marker === 4242, JSON.stringify({ report: png.report, w: png.w, h: png.h }));

  const fb = await b.evaluate(`${APP_PRELUDE}${BLOB_HELPER}${NM}
    nm.canvas.settings.renderQuality = "2"; nm.applyRenderQuality();
    const ssBefore = frameOf().dataset.ss;
    const P = await import(base + "views/nodemap/nodemap_png.js");
    const env0 = P.browserPngEnv();
    const env = { ...env0, loadImage: (svg) => (svg.includes("<foreignObject") ? Promise.reject(new Error("refused")) : env0.loadImage(svg)) };
    ${EXPORT("{ scale: 2, env }")}
    const ssAfter = frameOf().dataset.ss;
    nm.canvas.settings.renderQuality = "auto"; nm.applyRenderQuality();
    return { report: res?.report, b64, ssBefore, ssAfter };`, "fallback");
  if (fb.b64) writeFileSync(`${OUT}/nodemap_fallback_${THEME}.png`, Buffer.from(fb.b64, "base64"));
  check("fallback: refused snapshot painted from the DOM; supersampling restored", fb.report?.painted.length === 1 && fb.ssBefore === "2" && fb.ssAfter === "2", JSON.stringify(fb.report));

  const fr = await b.evaluate(`${APP_PRELUDE}${NM}
    nm.renderer.element(tabNode.id).querySelector('[data-act="tab-freeze"]').click();
    await sleep(200);
    const frozen = JSON.stringify([tabNode.data.hash, tabNode.data.viewState]);
    const f = frameOf();
    const sc = scrollerOf(f); sc.scrollTop = 1500; sc.dispatchEvent(new Event("scroll"));
    await sleep(1000);
    const unchanged = JSON.stringify([tabNode.data.hash, tabNode.data.viewState]) === frozen;
    const dirty = Boolean(nm.tabLive.get(tabNode.id)?.dirty);
    showTab(otherTabId()); await sleep(500); showTab(nm.tab.id);
    const f2 = await wait(() => frameOf() !== f && frameOf(), 5000);
    const restored = await wait(() => { const s2 = scrollerOf(f2); return s2 && s2.scrollTop > 0 && s2.scrollTop; }, 20000);
    return { frozen: tabNode.data.frozen, unchanged, dirty, reloaded: Boolean(f2), restored };`, "frozen");
  check("frozen: not saved, dirty, reset on return", fr.frozen && fr.unchanged && fr.dirty && fr.reloaded && fr.restored > 0 && fr.restored < 1500, JSON.stringify(fr));

  const before = await b.evaluate(`${APP_PRELUDE}${NM}
    nm.setTabsFrozen([tabNode.id], false); await sleep(300);
    nm.flushSave(); await sleep(500);
    return { hash: tabNode.data.hash };`, "unfreeze");
  await b.refresh();
  const after = await b.evaluate(`${APP_PRELUDE}
    const frame = await wait(() => document.querySelector("iframe.nm-iframe"), 20000);
    if (!frame) return { ok: false };
    const sc = await wait(() => { const s = [...frame.contentDocument.querySelectorAll("*")].find((el) => el.scrollHeight > el.clientHeight + 200 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)); return s && s.scrollTop > 0 && s; }, 25000);
    return { ok: true, src: frame.src, scrollTop: sc ? sc.scrollTop : 0 };`, "reload");
  check("reload: saved hash and scroll restored", after.ok && after.src.endsWith(`#${before.hash}`) && after.scrollTop > 0, JSON.stringify(after));
  await b.screenshot(`${OUT}/screen_${THEME}.png`);
} catch (err) {
  check("run", false, err.message);
  await b.screenshot(`${OUT}/error_${THEME}.png`).catch(() => {});
} finally {
  await b.close();
  console.log(`${results.filter((r) => r.ok).length}/${results.length} passed`);
  process.exitCode = results.every((r) => r.ok) ? 0 : 1;
}
