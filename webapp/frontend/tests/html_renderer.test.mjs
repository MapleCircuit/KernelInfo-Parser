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
});
