import { test } from "node:test";
import assert from "node:assert/strict";
import { AstOverlay } from "../js/views/code_view/ast_overlay.js";
import { rustGrammar } from "../js/views/code_view/syntax/languages/rust.js";
import { buildByteToCharMap, byteColToCharIdx } from "../js/utils/text_coordinates.js";

test("text_coordinates: UTF-8 multi-byte to JS char mapping", () => {
  // ASCII line: 1 byte per char
  const lineAscii = "int x = 42;";
  const mapAscii = buildByteToCharMap(lineAscii);
  assert.equal(byteColToCharIdx(mapAscii, 1), 0);
  assert.equal(byteColToCharIdx(mapAscii, 5), 4);

  // Line with 3-byte Cyrillic / Chinese characters: "/* Привет */ int y;"
  // "П" is 2 bytes (D0 9F), "р" is 2 bytes, etc.
  const lineUtf8 = "/* Привет */ int y;";
  const mapUtf8 = buildByteToCharMap(lineUtf8);
  // 'int' starts after "/* Привет */ "
  // JS char index of 'i' in 'int':
  const charIdxI = lineUtf8.indexOf("int");
  // Calculate byte offset of 'int'
  const byteOffsetI = Buffer.byteLength(lineUtf8.slice(0, charIdxI), "utf-8") + 1; // 1-based
  assert.equal(byteColToCharIdx(mapUtf8, byteOffsetI), charIdxI);
});

test("ast_overlay: ASM tokens map to explicit classes and comments are excluded", () => {
  const overlay = new AstOverlay();
  assert.equal(overlay.mapTypeToClass(83), "tok-macro ast-macro");
  assert.equal(overlay.mapTypeToClass(84), "tok-preproc ast-preproc");
  assert.equal(overlay.mapTypeToClass(85), "tok-keyword ast-ref");
  assert.equal(overlay.mapTypeToClass(86), "tok-function ast-func");
  assert.equal(overlay.mapTypeToClass(87), "tok-comment");

  // Load server tokens including ASM_Comment (87)
  overlay.loadServerTokens([
    [1, 1, 1, 10, 101, 87], // ASM_Comment: must be skipped
    [2, 1, 2, 5, 102, 85],   // ASM_Instruction: must be kept
  ]);
  assert.equal(overlay.tokensByLine.has(1), false, "ASM_Comment should not be stored in tokensByLine");
  assert.equal(overlay.tokensByLine.has(2), true, "ASM_Instruction should be stored in tokensByLine");
});

test("rust grammar and mod navigation", () => {
  assert.equal(rustGrammar.supportsInclude, true, "rustGrammar must support include");
  const overlay = new AstOverlay();
  overlay.setFileInfo(3, "rust/kernel/lib.rs", "mod sub;");
  const html = overlay.renderLineHtml(1, "mod sub;");
  assert.ok(html.includes("ast-include"), "mod sub; must be rendered as an include token");
  assert.ok(html.includes('data-name="sub"'), "data-name must be 'sub'");
  assert.ok(html.includes('data-header="sub.rs"'), "data-header must be 'sub.rs'");
});
