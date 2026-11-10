import { test } from "node:test";
import assert from "node:assert/strict";
import { renamedNoteHtml } from "../js/components/symbol_history_render.js";

test("renamed note", () => {
  assert.equal(renamedNoteHtml({ path: "b.c" }), "");
  assert.equal(renamedNoteHtml(null), "");
  const html = renamedNoteHtml({ path: "new/<b>.c", renamed_from: "old/<a>.c" });
  assert.ok(html.includes("renamed from <code>old/&lt;a&gt;.c</code>"));
  assert.ok(html.includes("old/&lt;a&gt;.c → new/&lt;b&gt;.c"));
  assert.ok(!html.includes("<a>"));
});
