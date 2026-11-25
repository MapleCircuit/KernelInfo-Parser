import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};

const { safeHttpUrl } = await import("../js/utils/url_safety.js");
const { formatApiErrorDetail } = await import("../js/api.js");
const { state } = await import("../js/state.js");
const GlobalState = state.constructor;

test("safeHttpUrl permits only explicit http(s) URLs without control characters", () => {
  assert.equal(safeHttpUrl("https://kernel.org/doc"), "https://kernel.org/doc");
  assert.equal(safeHttpUrl("  http://example.com:8080/a?b=1  "), "http://example.com:8080/a?b=1");
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpUrl("data:text/html,<script>alert(1)</script>"), null);
  assert.equal(safeHttpUrl("vbscript:msgbox(1)"), null);
  assert.equal(safeHttpUrl("https://example.com/\nheader"), null);
  assert.equal(safeHttpUrl("/relative/path"), null);
  assert.equal(safeHttpUrl(""), null);
  assert.equal(safeHttpUrl(null), null);
});

test("formatApiErrorDetail formats FastAPI 422 array, object, and string payloads cleanly", () => {
  assert.equal(
    formatApiErrorDetail({
      detail: [
        { loc: ["query", "version"], msg: "String should have at least 1 character", type: "string_too_short" },
        { loc: ["body", "path"], msg: "Invalid path", type: "value_error" },
      ],
    }),
    "version: String should have at least 1 character; path: Invalid path",
  );
  assert.equal(formatApiErrorDetail({ detail: "Not Found" }), "Not Found");
  assert.equal(formatApiErrorDetail({ detail: { message: "Payload too large" } }), "Payload too large");
  assert.equal(formatApiErrorDetail({ error: "Service unavailable" }), "Service unavailable");
  assert.equal(formatApiErrorDetail({}, "Request failed: 422"), "Request failed: 422");
  assert.doesNotMatch(formatApiErrorDetail({ detail: [{ msg: "Bad" }] }), /\[object Object\]/);
});

test("GlobalState.refOf preserves refs containing 'object' while rejecting [object Object]", () => {
  assert.equal(GlobalState.refOf("fix-kobject-uevent"), "fix-kobject-uevent");
  assert.equal(GlobalState.refOf("feature/object_store"), "feature/object_store");
  assert.equal(GlobalState.refOf({ vname: "v6.12" }), "v6.12");
  assert.equal(GlobalState.refOf("[object Object]"), "");
  assert.equal(GlobalState.refOf({}), "");
  assert.equal(GlobalState.refOf(""), "");
});

test("acceptTransferredTab rejects unknown tab types and accepts watch/cve tabs safely", () => {
  assert.equal(state.acceptTransferredTab({ tabData: { id: "bad", type: "evil_type" } }), null);
  const okWatch = state.acceptTransferredTab({ tabData: { id: "watch-1", type: "watch", title: "Watch" } });
  assert.equal(okWatch?.type, "watch");
  const okCve = state.acceptTransferredTab({ tabData: { id: "cve-1", type: "cve", title: "CVEs", version: "v3.0" } });
  assert.equal(okCve?.type, "cve");
  state.cancelPendingSave();
});
