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

test("makefile grammar tokenizes .o object paths outside comments and strings", () => {
  for (const path of ["kernel/Makefile", "drivers/net/Kbuild", "Makefile.build", "scripts/Kbuild.mk"]) {
    const overlay = new AstOverlay();
    overlay.setFileInfo(null, path, "");
    const line = "obj-$(CONFIG_FOO) += foo.o foo-bar.o 8250_core.o sub/foo-bar.o ../common/foo.o # ignored.o";
    const html = overlay.renderLineHtml(1, line);
    for (const obj of ["foo.o", "foo-bar.o", "8250_core.o", "sub/foo-bar.o", "../common/foo.o"]) {
      assert.ok(
        html.includes(`class="tok-include ast-token ast-object" data-name="${obj}" data-object="${obj}">${obj}</span>`),
        `${path} must render ${obj} as interactive ast-object span; got: ${html}`
      );
    }
    assert.ok(!html.includes('data-object="ignored.o"'), `${path} must ignore .o in inline comment`);
  }

  const overlay = new AstOverlay();
  overlay.setFileInfo(null, "kernel/Makefile", "");
  assert.ok(!overlay.renderLineHtml(1, "# comment with foo.o").includes("ast-object"), "comment line ignored");
  assert.ok(!overlay.renderLineHtml(1, 'MSG := "quoted.o"').includes("ast-object"), "quoted string ignored");
  assert.ok(!overlay.renderLineHtml(1, "%.o: %.c").includes("ast-object"), "pattern rule %.o ignored");
  assert.ok(!overlay.renderLineHtml(1, "out := foo.out").includes("ast-object"), "non-.o suffix ignored");
});

test("makefile grammar tokenizes CONFIG_* symbols inside $() / ${} and bare identifiers outside comments", () => {
  for (const path of ["kernel/Makefile", "drivers/net/Kbuild", "Makefile.build", "scripts/Kbuild.mk"]) {
    const overlay = new AstOverlay();
    overlay.setFileInfo(null, path, "");

    const objHtml = overlay.renderLineHtml(1, "obj-$(CONFIG_FOO) += foo.o");
    assert.ok(
      objHtml.includes('<span class="tok-variable">$(</span><span class="tok-variable ast-token" data-name="CONFIG_FOO">CONFIG_FOO</span><span class="tok-variable">)</span>'),
      `${path} must render $(CONFIG_FOO) with inner interactive ast-token; got: ${objHtml}`
    );

    const braceHtml = overlay.renderLineHtml(2, "obj-${CONFIG_64BIT} += bar.o");
    assert.ok(
      braceHtml.includes('<span class="tok-variable">${</span><span class="tok-variable ast-token" data-name="CONFIG_64BIT">CONFIG_64BIT</span><span class="tok-variable">}</span>'),
      `${path} must render \${CONFIG_64BIT} with inner interactive ast-token; got: ${braceHtml}`
    );

    const ifeqHtml = overlay.renderLineHtml(3, "ifeq ($(CONFIG_BAR),y)");
    assert.ok(
      ifeqHtml.includes('<span class="tok-variable">$(</span><span class="tok-variable ast-token" data-name="CONFIG_BAR">CONFIG_BAR</span><span class="tok-variable">)</span>'),
      `${path} must render ifeq ($(CONFIG_BAR),y) with inner ast-token; got: ${ifeqHtml}`
    );

    const ifdefHtml = overlay.renderLineHtml(4, "ifdef CONFIG_SMP");
    assert.ok(
      ifdefHtml.includes('<span class="tok-identifier ast-token" data-name="CONFIG_SMP">CONFIG_SMP</span>'),
      `${path} must render bare CONFIG_SMP as ast-token; got: ${ifdefHtml}`
    );
  }

  const overlay = new AstOverlay();
  overlay.setFileInfo(null, "drivers/net/Makefile", "");

  const digitsHtml = overlay.renderLineHtml(1, "obj-$(CONFIG_8139TOO) += 8139too.o");
  assert.ok(
    digitsHtml.includes('<span class="tok-variable ast-token" data-name="CONFIG_8139TOO">CONFIG_8139TOO</span>'),
    `CONFIG_8139TOO must match [A-Za-z0-9_]+; got: ${digitsHtml}`
  );

  // Non-config variables and empty CONFIG_ are not marked as CONFIG_* ast-tokens
  const nonConfig = overlay.renderLineHtml(2, "ccflags-y := -I$(srctree)/include -I${src} $(CONFIG_)");
  assert.ok(!nonConfig.includes('data-name="srctree"'), "non-config $(srctree) must not emit ast-token");
  assert.ok(!nonConfig.includes('data-name="src"'), "non-config ${src} must not emit ast-token");
  assert.ok(!nonConfig.includes('data-name="CONFIG_"'), "empty $(CONFIG_) must not emit ast-token");

  // Mismatched delimiters must not split as $(CONFIG_*) / ${CONFIG_*} variable wrapper
  const mismatched = overlay.renderLineHtml(3, "x := ${CONFIG_BAD) + ${OTHER}");
  assert.ok(!mismatched.includes('data-name="CONFIG_BAD"'), "mismatched ${CONFIG_BAD) must not match");

  // Comments (full line and inline) must be ignored
  const commentLine = overlay.renderLineHtml(4, "# obj-$(CONFIG_COMMENTED) += foo.o");
  assert.ok(!commentLine.includes("ast-token"), "comment line must not emit ast-token");
  assert.ok(!commentLine.includes("data-name="), "comment line must not emit data-name");

  const inlineComment = overlay.renderLineHtml(5, "obj-$(CONFIG_REAL) += a.o # obj-$(CONFIG_COMMENTED) ifdef CONFIG_IGNORED");
  assert.ok(inlineComment.includes('data-name="CONFIG_REAL"'), "active CONFIG_REAL before # must emit ast-token");
  assert.ok(!inlineComment.includes('data-name="CONFIG_COMMENTED"'), "inline comment $(CONFIG_COMMENTED) must be ignored");
  assert.ok(!inlineComment.includes('data-name="CONFIG_IGNORED"'), "inline comment CONFIG_IGNORED must be ignored");
});
