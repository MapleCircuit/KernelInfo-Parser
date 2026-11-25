import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};

const tmplMod = await import("../js/views/docs/tmpl_renderer.js");
const { renderDocument } = await import("../js/views/docs/rst_renderer.js");
const { DocPage } = await import("../js/views/docs/doc_page.js");

test("tmpl_renderer validates ulink schemes, escapes fallback text/linkend, and drops renderTmplFragment", () => {
  assert.equal("renderTmplFragment" in tmplMod, false, "unused renderTmplFragment export removed");
  const xml = `
    <book><bookinfo><title>Test</title></bookinfo>
      <chapter><title>Ch</title>
        <para>
          <ulink url="javascript:alert(&quot;xss&quot;)"></ulink>
          <ulink url="https://kernel.org/doc">Kernel</ulink>
          <link linkend="sec&lt;1&gt;"></link>
        </para>
      </chapter>
    </book>`;
  const res = tmplMod.renderTmplDocument(xml, "Documentation/DocBook/test.tmpl");
  assert.ok(!res.html.includes('href="javascript:'), "javascript: ulink neutralized");
  assert.ok(res.html.includes('href="#" target="_blank" rel="noopener noreferrer"'), "unsafe ulink falls back to # with rel");
  assert.ok(res.html.includes('href="https://kernel.org/doc" target="_blank" rel="noopener noreferrer"'), "https ulink allowed");
  assert.ok(!res.html.includes("#sec<1>"), "raw angle brackets not left unescaped in href");
  assert.ok(res.html.includes('href="#sec&amp;lt;1&amp;gt;"'), "linkend escaped");
});

test("rst_renderer precompiled inline regex handles reentrant substitutions", () => {
  const rst = `Title\n=====\n\n.. |foo| replace:: **bar**\n\nHello |foo| world \`link <https://kernel.org>\`_.\n`;
  const res = renderDocument(rst, "Documentation/test.rst");
  assert.match(res.html, /<strong>bar<\/strong>/);
  assert.match(res.html, /world/);
  assert.match(res.html, /href="https:\/\/kernel\.org"/);
});

test("DocPage handles malformed URI anchors and disconnected renderRelated", async () => {
  let clickHandler = null;
  let scrolled = null;
  const relatedBox = { isConnected: false, innerHTML: "" };
  const page = new DocPage({
    path: "Documentation/test.rst",
    version: "v3.0",
    onNavigate() {},
    onShowSource() {},
    canGoBack: false,
    onBack() {},
    api: {
      getDirectoryTree: async () => ({ entries: [{ name: "index.rst" }] }),
    },
  });
  page.root = {
    isConnected: false,
    addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
    contains: () => true,
    querySelector: (sel) => (sel === ".kdoc-related" ? relatedBox : null),
  };
  page.scrollToId = (id) => { scrolled = id; };
  page.bindLinks();

  // Malformed percent-encoding must not throw
  await clickHandler({
    target: {
      closest: () => ({
        classList: { contains: () => false },
        dataset: {},
        getAttribute: () => "#%E0%A4%A",
      }),
    },
    preventDefault() {},
  });
  assert.equal(scrolled, "%E0%A4%A");

  // Disconnected box after await must not write innerHTML
  await page.renderRelated();
  assert.equal(relatedBox.innerHTML, "");
});
