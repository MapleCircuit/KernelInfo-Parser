import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveColor, savedTheme, token } from "../js/utils/theme.js";

test("token fallback and var() resolution", () => {
  const read = (n) => ({ "--bg-canvas": " #0a0d12 " }[n] ?? "");
  assert.equal(token("--bg-canvas", "x", read), "#0a0d12");
  assert.equal(token("--missing", "#123456", read), "#123456");
  assert.equal(token("--boom", "f", () => { throw new Error("no DOM"); }), "f");
  assert.equal(resolveColor("var(--bg-canvas)", read), "#0a0d12");
  assert.equal(resolveColor("var(--missing, #abcdef)", read), "#abcdef");
  assert.equal(resolveColor("#ffffff", read), "#ffffff");
  assert.equal(savedTheme({ getItem: () => "hc-light" }), "hc-light");
  assert.equal(savedTheme({ getItem: () => "neon" }), "dark");
});
