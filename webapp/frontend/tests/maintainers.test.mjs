import { test } from "node:test";
import assert from "node:assert/strict";

test("maintainers pattern tags canonical representation", () => {
  const PATTERN_TAG_MAP = {
    1: "F:",
    2: "X:",
    3: "K:",
    4: "N:",
    5: "D:",
    FILE: "F:",
    EXCLUDE: "X:",
    KEYWORD: "K:",
    REGEX: "N:",
    DOC: "D:",
  };

  function formatPatternTag(type) {
    const str = String(type ?? "").trim();
    if (PATTERN_TAG_MAP[str]) return PATTERN_TAG_MAP[str];
    if (/^[A-Z]:?$/i.test(str)) return str.endsWith(":") ? str.toUpperCase() : `${str.toUpperCase()}:`;
    return str ? `${str}:` : "";
  }

  assert.equal(formatPatternTag(1), "F:");
  assert.equal(formatPatternTag(2), "X:");
  assert.equal(formatPatternTag(3), "K:");
  assert.equal(formatPatternTag(4), "N:");
  assert.equal(formatPatternTag(5), "D:");
  assert.equal(formatPatternTag("FILE"), "F:");
  assert.equal(formatPatternTag("EXCLUDE"), "X:");
  assert.equal(formatPatternTag("KEYWORD"), "K:");
  assert.equal(formatPatternTag("REGEX"), "N:");
  assert.equal(formatPatternTag("F"), "F:");
  assert.equal(formatPatternTag("F:"), "F:");
});
