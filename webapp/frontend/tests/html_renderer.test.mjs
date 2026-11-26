import { test } from "node:test";
import assert from "node:assert/strict";
import { renderHtmlDocument } from "../js/views/docs/html_renderer.js";

test("html_renderer security sanitization", () => {
  // 1. Unquoted javascript: in href
  const unquotedJs = '<p><a href=javascript:alert(1)>Click me</a></p>';
  const res1 = renderHtmlDocument(unquotedJs, "Documentation/sound/test.html");
  assert.ok(!res1.html.includes("javascript:"), "unquoted javascript: href must be stripped");
  assert.ok(res1.html.includes('href="#"'), "unquoted javascript: href must be replaced with #");

  // 2. Slash-separated attributes
  const slashSeparated = '<p><a/href=javascript:alert(2)>Click</a><div/onclick=alert(3)>Div</div></p>';
  const res2 = renderHtmlDocument(slashSeparated, "Documentation/sound/test.html");
  assert.ok(!res2.html.includes("javascript:"), "slash-separated javascript: href must be stripped");
  assert.ok(!res2.html.includes("onclick"), "slash-separated onclick must be stripped");

  // 3. Parametric data URIs
  const parametricDataHref = '<p><a href="data:text/html;charset=utf-8;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">Data Link</a></p>';
  const res3 = renderHtmlDocument(parametricDataHref, "Documentation/sound/test.html");
  assert.ok(!res3.html.includes("data:text/html"), "parametric data: href must be neutralized");
  assert.ok(res3.html.includes('href="#"'), "parametric data: href must be replaced with #");

  const unquotedDataHref = '<p><a href=data:text/html;charset=utf-8,<b>hello</b>>Data Link</a></p>';
  const res4 = renderHtmlDocument(unquotedDataHref, "Documentation/sound/test.html");
  assert.ok(!res4.html.includes("data:text/html"), "unquoted data: href must be neutralized");

  // 4. Executable/script data URI in src
  const dangerousSrc = '<p><img src="data:text/html;charset=utf-8;base64,abc" /><img/src=data:image/svg+xml;utf8,<svg></svg> /></p>';
  const res5 = renderHtmlDocument(dangerousSrc, "Documentation/sound/test.html");
  assert.ok(!res5.html.includes("data:text/html"), "executable data: src must be stripped");
  assert.ok(!res5.html.includes("data:image/svg+xml"), "svg data: src must be stripped");

  // 5. Normal safe links and images still work
  const safeDoc = '<h1>Title</h1><p><a href="guide.html">Safe Guide</a><img src="diagram.png" /></p>';
  const res6 = renderHtmlDocument(safeDoc, "Documentation/sound/test.html");
  assert.equal(res6.title, "Title");
  assert.ok(res6.html.includes('data-doc="Documentation/sound/guide.html"'), "relative doc link transformed");
  assert.ok(res6.html.includes('data-raw="Documentation/sound/diagram.png"'), "relative image src transformed");

  // 6. HTML-entity-encoded schemes and data:// / javascript:// variants
  const entityDoc = '<p><a href="&#106;avascript:alert(1)">EntJS</a><a href="java&#x09;script:alert(2)">TabJS</a><a href="&#100;ata:text/html,x">EntData</a><img src="&#106;avascript:alert(3)" /><img src="data://text/html,x" /></p>';
  const res7 = renderHtmlDocument(entityDoc, "Documentation/sound/test.html");
  assert.ok(!res7.html.includes("javascript:"), "entity-encoded javascript: must be stripped");
  assert.ok(!res7.html.includes("data:text/html"), "entity-encoded data: must be stripped");
  assert.ok(!res7.html.includes("data://"), "data:// image must be stripped");

  // 7. target="_blank" always gets noopener and noreferrer tokens even with existing rel
  const relDoc = '<p><a href="https://kernel.org" target="_blank" rel="external">K</a><a href="mailto:list@kernel.org" rel="author">M</a></p>';
  const res8 = renderHtmlDocument(relDoc, "Documentation/sound/test.html");
  assert.match(res8.html, /rel="external noopener noreferrer"/);
  assert.match(res8.html, /<a\b[^>]*rel="author noopener noreferrer"[^>]*href="mailto:list@kernel\.org"|<a\b[^>]*href="mailto:list@kernel\.org"[^>]*rel="author noopener noreferrer"/);

  // 8. Adjacent-quote event handlers, active SVG/form/video tags, srcset/xlink:href, and nested tag reconstruction
  const bypassDoc = '<div title="x"onclick="alert(1)">ok</div>'
    + '<svg><a xlink:href="javascript:alert(2)">x</a></svg>'
    + '<form action="javascript:alert(3)"><button formaction="javascript:alert(4)">go</button></form>'
    + '<video poster="javascript:alert(5)"></video>'
    + '<img src="diagram.png" srcset="javascript:alert(6) 1x" />'
    + '<scr<script>evil</script>ipt>alert(7)</scr<script></script>ipt>';
  const res9 = renderHtmlDocument(bypassDoc, "Documentation/sound/test.html");
  assert.ok(!res9.html.includes("onclick"), "adjacent-quote onclick must be stripped");
  assert.ok(!res9.html.includes("<svg"), "svg elements must be stripped");
  assert.ok(!res9.html.includes("xlink:href"), "xlink:href must be stripped");
  assert.ok(!res9.html.includes("<form"), "form elements must be stripped");
  assert.ok(!res9.html.includes("formaction"), "formaction must be stripped");
  assert.ok(!res9.html.includes("poster="), "poster must be stripped");
  assert.ok(!res9.html.includes("srcset="), "srcset must be stripped");
  assert.ok(!res9.html.includes("<script"), "nested script reconstruction must be stripped");
});
