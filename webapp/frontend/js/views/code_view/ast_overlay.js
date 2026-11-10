/**
 * ast_overlay.js - Dual-Layer Syntax Highlighting & AST Disjoint Interval Overlay.
 * Combines client-side multi-language lexical parsing with database spatial map tokens and #ifdef dimming.
 */

import { detectGrammar, DEFAULT_FALLBACK_GRAMMAR } from "./syntax/registry.js";
import { C_KEYWORDS, C_TYPES } from "./syntax/languages/c.js";
import { buildByteToCharMap, byteColToCharIdx } from "../../utils/text_coordinates.js";

const REGEX_CACHE = new Map();
const PUNCT_REGEX = "[^\\s\\w\"'`$@%#;/]+|[^\\s\\w]";

/**
 * Build a language-specific token regular expression with tailored token boundaries.
 * Prevents cross-language collisions (e.g. semicolons as comments in C/Rust, or slashes as comments in Python/Shell).
 *
 * @param {string} grammarId
 * @returns {RegExp}
 */
function getGrammarRegex(grammarId) {
  const cached = REGEX_CACHE.get(grammarId);
  if (cached) {
    return new RegExp(cached.source, "g");
  }

  const parts = [];

  switch (grammarId) {
    case "rust":
      parts.push(
        "/\\*[\\s\\S]*?\\*/",                                                // 1. Block comments
        "//[^\n]*",                                                          // 2. Line / doc comments
        "#!?\\[[^\\]\n]*\\]",                                                // 3. Attributes: #[inline], #![no_std]
        "r###\"[\\s\\S]*?\"###|r##\"[\\s\\S]*?\"##|r#\"[\\s\\S]*?\"#",       // 4. Raw strings: r#"..."#
        "b\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                               // 5. Byte strings: b"..."
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // 6. Double-quoted strings
        "'(?:[^'\\\\]|\\\\.)'",                                              // 7. Character literals: 'c', '\n'
        "'[a-zA-Z_]\\w*\\b",                                                 // 8. Lifetimes: 'a, 'static
        "[a-zA-Z_]\\w*!",                                                    // 9. Macro calls: pr_info!, vec!
        "\\b0x[0-9a-fA-F_]+[uUlLiIfF\\d]*\\b",                              // 10. Hex numbers
        "\\b0b[01_]+[uUlLiIfF\\d]*\\b",                                      // 11. Binary numbers
        "\\b0o[0-7_]+[uUlLiIfF\\d]*\\b",                                     // 12. Octal numbers
        "\\b\\d[\\d_]*(?:\\.[\\d_]*)?(?:[eE][+-]?\\d+)?(?:[a-zA-Z_]\\w*)?\\b",// 13. Decimals / floats
        "[a-zA-Z_]\\w*",                                                     // 14. Identifiers
        PUNCT_REGEX                                                          // 15. Punctuation
      );
      break;

    case "c":
      parts.push(
        "/\\*[\\s\\S]*?\\*/",                                                // Block comments
        "//[^\n]*",                                                          // Line comments
        "#[a-zA-Z_]\\w*",                                                    // Preprocessor directives
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Double-quoted strings
        "'[^'\\\\]*(?:\\\\.[^'\\\\]*)*'",                                    // Char literals
        "\\b0x[0-9a-fA-F]+[uUlL]*\\b",                                       // Hex
        "\\b\\d+[uUlL]*\\b",                                                 // Decimal
        "[a-zA-Z_]\\w*",                                                     // Identifiers
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "python":
      parts.push(
        "#[^\n]*",                                                           // Hash comments
        "\"\"\"[\\s\\S]*?\"\"\"|'''[\\s\\S]*?'''",                           // Triple-quoted docstrings
        "@[a-zA-Z_]\\w*(?:\\.[a-zA-Z_]\\w*)*",                               // Decorators
        "[frbFRB]{0,2}\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                   // Double-quoted strings
        "[frbFRB]{0,2}'[^'\\\\]*(?:\\\\.[^'\\\\]*)*'",                       // Single-quoted strings
        "\\b0x[0-9a-fA-F_]+\\b|\\b0b[01_]+\\b|\\b0o[0-7_]+\\b",             // Hex / bin / oct
        "\\b\\d[\\d_]*(?:\\.[\\d_]*)?(?:[eE][+-]?\\d+)?\\b",                 // Numbers
        "[a-zA-Z_]\\w*",                                                     // Identifiers
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "shell":
      parts.push(
        "#[^\n]*",                                                           // Comments
        "\\$\\{[^}]+\\}|\\$\\([a-zA-Z0-9_.-]+\\)|\\$[a-zA-Z_]\\w*|\\$[@*#?$!0-9-]", // Variables
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Double quotes
        "'[^']*'",                                                           // Single quotes
        "\\b0x[0-9a-fA-F]+\\b|\\b\\d+\\b",                                   // Numbers
        "[a-zA-Z_][\\w-]*",                                                  // Identifiers / words
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "perl":
      parts.push(
        "#[^\n]*",                                                           // Comments
        "[\\$@%][a-zA-Z_]\\w*|\\$#[a-zA-Z_]\\w*|\\$[@*#?$!0-9-]",           // Variables
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Double quotes
        "'[^']*'",                                                           // Single quotes
        "\\b0x[0-9a-fA-F]+\\b|\\b\\d+\\b",                                   // Numbers
        "[a-zA-Z_]\\w*",                                                     // Identifiers
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "assembly":
      parts.push(
        "/\\*[\\s\\S]*?\\*/",                                                // Block comments
        "//[^\n]*",                                                          // C++ comments
        ";[^\n]*",                                                           // Semicolon comments
        "@[^\n]*",                                                           // ARM @ comments
        "#[^\n]*",                                                           // Hash comments
        "\\.[a-zA-Z_]\\w*",                                                  // Directives (.global, etc.)
        "%[a-zA-Z0-9]+",                                                     // Registers (%rax, etc.)
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Strings
        "'[^'\\\\]*(?:\\\\.[^'\\\\]*)*'",                                    // Chars
        "\\b0x[0-9a-fA-F]+\\b|\\b\\d+\\b",                                   // Numbers
        "[a-zA-Z_]\\w*",                                                     // Labels / identifiers
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "makefile":
      parts.push(
        "#[^\n]*",                                                           // Comments
        ":=|\\+=|\\?=|!=",                                                   // Assignment operators
        "\\$\\([a-zA-Z0-9_.-]+\\)|\\$\\{[^}]+\\}|\\$[@<*^?%+]",              // Variables
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Strings
        "'[^']*'",                                                           // Chars
        "\\b\\d+\\b",                                                        // Numbers
        "[a-zA-Z_][\\w-]*",                                                  // Targets / variables
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "kconfig":
      parts.push(
        "#[^\n]*",                                                           // Comments
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Strings
        "'[^']*'",                                                           // Chars
        "\\b0x[0-9a-fA-F]+\\b|\\b\\d+\\b",                                   // Numbers
        "[a-zA-Z0-9_]+",                                                     // Identifiers
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "dts":
      parts.push(
        "/\\*[\\s\\S]*?\\*/",                                                // Block comments
        "//[^\n]*",                                                          // Comments
        "/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]*",                                    // Directives: /dts-v1/, etc.
        "#[a-zA-Z_]\\w*",                                                    // Preproc
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Strings
        "\\b0x[0-9a-fA-F]+\\b|\\b\\d+\\b",                                   // Numbers
        "[a-zA-Z_][\\w-]*",                                                  // Node names / properties
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    case "yaml":
      parts.push(
        "#[^\n]*",                                                           // Comments
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",                                // Strings
        "'[^']*'",                                                           // Chars
        "\\b0x[0-9a-fA-F]+\\b|\\b\\d+(?:\\.\\d+)?\\b",                       // Numbers
        "[a-zA-Z0-9_-]+",                                                    // Keys / values
        PUNCT_REGEX                                                          // Punctuation
      );
      break;

    default:
      // Generic fallback
      parts.push(
        "/\\*[\\s\\S]*?\\*/",
        "//[^\n]*",
        "#[^\n]*",
        "\"[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*\"",
        "'[^'\\\\]*(?:\\\\.[^'\\\\]*)*'",
        "\\b0x[0-9a-fA-F]+\\b|\\b\\d+\\b",
        "[a-zA-Z_]\\w*",
        PUNCT_REGEX
      );
      break;
  }

  const compiled = new RegExp(parts.join("|"), "g");
  REGEX_CACHE.set(grammarId, compiled);
  return new RegExp(compiled.source, "g");
}

export class AstOverlay {
  constructor() {
    this.tokensByLine = new Map(); // lineNo -> list of AST tokens
    this.multiLineCommentLines = new Set(); // lineNo -> is inside multi-line block comment
    this.multiLineDocstringLines = new Set(); // lineNo -> is inside multi-line docstring
    this.ftype = null;
    this.filePath = "";
    this.grammar = DEFAULT_FALLBACK_GRAMMAR;
    this.isCFile = true; // Default true until setFileInfo is called
  }

  setFileInfo(ftype, filePath = "", firstLine = "") {
    this.ftype = ftype;
    this.filePath = filePath || "";
    this.grammar = detectGrammar(ftype, filePath, firstLine);
    this.isCFile = this.grammar.id === "c";
  }

  /**
   * Pre-scan line intervals on file content load to accurately track multi-line
   * block comments (/* ... *\/) and Python docstrings (""" / ''') across virtual scrolling jumps.
   *
   * @param {string[]} rawLines
   */
  indexMultiLineSpans(rawLines) {
    this.multiLineCommentLines.clear();
    this.multiLineDocstringLines.clear();
    if (!Array.isArray(rawLines) || rawLines.length === 0) return;

    let inBlockComment = false;
    let inTripleQuote = null; // '"""' or "'''"

    for (let i = 0; i < rawLines.length; i++) {
      const lineNo = i + 1;
      const line = rawLines[i];

      // 1. Continuation of block comment
      if (inBlockComment) {
        this.multiLineCommentLines.add(lineNo);
        if (line.includes("*/")) {
          inBlockComment = false;
        }
        continue;
      }

      // 2. Continuation of Python triple-quote docstring
      if (inTripleQuote) {
        this.multiLineDocstringLines.add(lineNo);
        if (line.includes(inTripleQuote)) {
          inTripleQuote = null;
        }
        continue;
      }

      // 3. Check for start of block comment (C, Rust, Assembly, DTS)
      if (this.grammar.id === "c" || this.grammar.id === "rust" || this.grammar.id === "assembly" || this.grammar.id === "dts") {
        const openIdx = line.indexOf("/*");
        if (openIdx !== -1) {
          const closeIdx = line.indexOf("*/", openIdx + 2);
          if (closeIdx === -1) {
            inBlockComment = true;
            this.multiLineCommentLines.add(lineNo);
            continue;
          }
        }
      }

      // 4. Check for start of Python triple-quote docstring
      if (this.grammar.id === "python") {
        for (const quote of ['"""', "'''"]) {
          const openIdx = line.indexOf(quote);
          if (openIdx !== -1) {
            const closeIdx = line.indexOf(quote, openIdx + 3);
            if (closeIdx === -1) {
              inTripleQuote = quote;
              this.multiLineDocstringLines.add(lineNo);
              break;
            }
          }
        }
      }
    }
  }

  loadServerTokens(serverTokens) {
    this.tokensByLine.clear();
    if (!Array.isArray(serverTokens)) return;

    const processTok = (line_s, char_s, line_e, char_e, ast_id, type_id) => {
      // Skip pure comments from clickable AST tokens map (C_Comment=2, ASM_Comment=87, Rust_DocComment=142, Rust_Comment=143)
      if (type_id === 2 || type_id === 87 || type_id === 142 || type_id === 143) return;

      for (let l = line_s; l <= line_e; l++) {
        if (!this.tokensByLine.has(l)) {
          this.tokensByLine.set(l, []);
        }
        this.tokensByLine.get(l).push({
          char_s: l === line_s ? char_s : 0,
          char_e: l === line_e ? char_e : 9999,
          ast_id,
          type_id,
          isMultiLine: line_e > line_s
        });
      }
    };

    // Support both nested arrays [[l_s, c_s, l_e, c_e, ast_id, type_id], ...] and flat array
    if (serverTokens.length > 0 && Array.isArray(serverTokens[0])) {
      for (const tok of serverTokens) {
        processTok(tok[0], tok[1], tok[2], tok[3], tok[4], tok[5]);
      }
    } else {
      for (let i = 0; i < serverTokens.length; i += 6) {
        processTok(
          serverTokens[i],
          serverTokens[i + 1],
          serverTokens[i + 2],
          serverTokens[i + 3],
          serverTokens[i + 4],
          serverTokens[i + 5]
        );
      }
    }
  }

  renderLineHtml(lineNo, rawLineText, isDimmed = false) {
    if (!rawLineText) {
      return isDimmed ? `<span class="ast-dimmed">&nbsp;</span>` : "&nbsp;";
    }

    // 1. Direct Include Line Handling for languages with preprocessor includes (C, Assembly, DTS) or Rust mod declarations
    if ((this.grammar.supportsInclude || this.isCFile) && /^\s*#\s*include\b/.test(rawLineText)) {
      return this.renderIncludeLine(lineNo, rawLineText, isDimmed);
    }
    if (this.grammar.supportsInclude && this.grammar.id === "rust" && /^\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+[a-zA-Z_]\w*\s*;/.test(rawLineText)) {
      return this.renderRustModLine(lineNo, rawLineText, isDimmed);
    }

    // 2. Pre-scanned Multi-Line Block Comments & Docstrings
    if (lineNo && this.multiLineCommentLines.has(lineNo)) {
      const h = `<span class="tok-comment">${this.escapeHtml(rawLineText)}</span>`;
      return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
    }
    if (lineNo && this.multiLineDocstringLines.has(lineNo)) {
      const h = `<span class="tok-string">${this.escapeHtml(rawLineText)}</span>`;
      return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
    }

    const astTokens = this.tokensByLine.get(lineNo) || [];

    // Filter out broad multi-line container envelopes (functions, compound statements, preprocessor blocks, Rust impls/mods)
    // that span across multiple lines without providing token-level detail for this specific line.
    const specificTokens = astTokens.filter((t) => {
      const isBroadContainer = t.isMultiLine && (
        t.type_id === 1 || t.type_id === 21 || t.type_id === 69 || t.type_id === 70 || t.type_id === 71 || t.type_id === 72 ||
        t.type_id === 132 || t.type_id === 136 || t.type_id === 140
      );
      return !isBroadContainer;
    });

    // If specific server AST tokens exist for this line, prioritize map information
    if (specificTokens.length > 0) {
      return this.renderWithAstTokens(rawLineText, specificTokens, isDimmed);
    }

    // Otherwise, revert to client-side lexical/semantic parser
    return this.renderLexical(rawLineText, isDimmed, lineNo);
  }

  renderIncludeLine(lineNo, rawLineText, isDimmed) {
    const incMatch = rawLineText.match(/^(\s*#\s*include\s+)([<"][^>"\r\n]+[>"])(.*)$/);
    if (incMatch) {
      const prefix = incMatch[1];
      const headerRaw = incMatch[2];
      const remainder = incMatch[3];
      const cleanHeader = headerRaw.replace(/^[<"']|[>"']$/g, "");

      // Find if server map has an AST token for this include on this line
      const astTokens = lineNo ? (this.tokensByLine.get(lineNo) || []) : [];
      const incTok = astTokens.find((t) => t.type_id === 78) || null;
      const astIdAttr = incTok && incTok.ast_id ? ` data-ast-id="${incTok.ast_id}"` : "";

      const prefixHtml = `<span class="tok-preproc">${this.escapeHtml(prefix)}</span>`;
      const headerHtml = `<span class="tok-include ast-token ast-include" data-name="${this.escapeHtml(cleanHeader)}" data-header="${this.escapeHtml(headerRaw)}"${astIdAttr} data-type-id="78">${this.escapeHtml(headerRaw)}</span>`;
      const remHtml = remainder ? this.tokenizeExpression(remainder) : "";

      const h = `${prefixHtml}${headerHtml}${remHtml}`;
      return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
    }
    return this.renderLexical(rawLineText, isDimmed, lineNo);
  }

  renderRustModLine(lineNo, rawLineText, isDimmed) {
    const modMatch = rawLineText.match(/^(\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+)([a-zA-Z_]\w*)(\s*;.*)$/);
    if (modMatch) {
      const prefix = modMatch[1];
      const modName = modMatch[2];
      const remainder = modMatch[3];
      const headerRaw = `${modName}.rs`;

      const astTokens = lineNo ? (this.tokensByLine.get(lineNo) || []) : [];
      const modTok = astTokens.find((t) => t.type_id === 136) || null;
      const astIdAttr = modTok && modTok.ast_id ? ` data-ast-id="${modTok.ast_id}"` : "";

      const prefixHtml = this.tokenizeExpression(prefix);
      const modHtml = `<span class="tok-include ast-token ast-include" data-name="${this.escapeHtml(modName)}" data-header="${this.escapeHtml(headerRaw)}"${astIdAttr} data-type-id="136">${this.escapeHtml(modName)}</span>`;
      const remHtml = this.tokenizeExpression(remainder);

      const h = `${prefixHtml}${modHtml}${remHtml}`;
      return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
    }
    return this.renderLexical(rawLineText, isDimmed, lineNo);
  }

  renderWithAstTokens(lineText, astTokens, isDimmed) {
    const len = lineText.length;
    if (len === 0) return "&nbsp;";

    // Map 1-based database UTF-8 byte columns to 0-based JS character indices to eliminate alignment drift
    const byteMap = buildByteToCharMap(lineText);

    // Map each character position to the most specific (narrowest) AST token
    const charToken = new Array(len).fill(null);

    // Sort tokens by width descending: widest scopes first, narrowest symbols last (so narrowest wins)
    const sorted = [...astTokens].sort((a, b) => {
      const wA = (a.char_e || len) - (a.char_s || 0);
      const wB = (b.char_e || len) - (b.char_s || 0);
      return wB - wA;
    });

    for (const tok of sorted) {
      const s = byteColToCharIdx(byteMap, tok.char_s || 1);
      const e = tok.char_e != null && tok.char_e < 9999 ? byteColToCharIdx(byteMap, tok.char_e) : len;
      const start = Math.max(0, Math.min(s, len));
      const end = Math.max(start, Math.min(e, len));
      for (let i = start; i < end; i++) {
        charToken[i] = tok;
      }
    }

    // Group adjacent identical tokens into contiguous spans, respecting identifier word boundaries
    const isWordChar = (ch) => ch && /[a-zA-Z0-9_]/.test(ch);
    const spans = [];
    let curStart = 0;
    let curTok = charToken[0];

    for (let i = 1; i < len; i++) {
      if (charToken[i] !== curTok) {
        // If a transition occurs inside an identifier word, defer transition until the word ends
        if (isWordChar(lineText[i - 1]) && isWordChar(lineText[i])) {
          continue;
        }
        spans.push({ start: curStart, end: i, tok: curTok });
        curStart = i;
        curTok = charToken[i];
      }
    }
    spans.push({ start: curStart, end: len, tok: curTok });

    let html = "";
    for (const span of spans) {
      const snippet = lineText.substring(span.start, span.end);
      if (span.tok) {
        html += this.tokenizeSnippet(snippet, span.tok);
      } else {
        html += this.tokenizeExpression(snippet);
      }
    }

    return isDimmed ? `<span class="ast-dimmed">${html}</span>` : html;
  }

  tokenizeSnippet(snippet, tok) {
    // 1. Comments: never clickable (C_Comment=2, ASM_Comment=87, Rust_DocComment=142, Rust_Comment=143)
    if (tok.type_id === 2 || tok.type_id === 87 || tok.type_id === 142 || tok.type_id === 143 ||
        snippet.trim().startsWith("//") || snippet.trim().startsWith("/*")) {
      return `<span class="tok-comment">${this.escapeHtml(snippet)}</span>`;
    }

    // 2. Preprocessor include
    if (tok.type_id === 78) {
      return this.renderIncludeLine(null, snippet, false);
    }

    // 3. Preprocessor defines & macros (C macros: 75, 76; Rust macros: 138, 139)
    if (tok.type_id === 75 || tok.type_id === 76) {
      const defMatch = snippet.match(/^(\s*#?\s*define\s+)([a-zA-Z_]\w*)(.*)$/);
      if (defMatch) {
        const prefix = `<span class="tok-preproc">${this.escapeHtml(defMatch[1])}</span>`;
        const macroName = defMatch[2];
        const remainder = this.tokenizeExpression(defMatch[3]);
        return `${prefix}<span class="tok-macro ast-token" data-name="${this.escapeHtml(macroName)}" data-ast-id="${tok.ast_id}" data-type-id="${tok.type_id}">${this.escapeHtml(macroName)}</span>${remainder}`;
      }
    }

    if (tok.type_id === 138 || tok.type_id === 139) {
      const macroMatch = snippet.match(/^([a-zA-Z_]\w*!?)(.*)$/);
      if (macroMatch) {
        const macroName = macroMatch[1];
        const cleanName = macroName.replace(/!$/, "");
        const remainder = this.tokenizeExpression(macroMatch[2]);
        return `<span class="tok-macro ast-token ast-macro" data-name="${this.escapeHtml(cleanName)}" data-ast-id="${tok.ast_id}" data-type-id="${tok.type_id}">${this.escapeHtml(macroName)}</span>${remainder}`;
      }
    }

    // 4. Internal tokenization for code within the map extent
    const typeClass = this.mapTypeToClass(tok.type_id);
    return this.tokenizeExpression(snippet, tok, typeClass);
  }

  renderLexical(lineText, isDimmed, lineNo = null) {
    const trimmed = lineText.trim();

    // 1. Check if entire line is a comment according to the active grammar
    if (this.grammar.isCommentLine(trimmed)) {
      const h = `<span class="tok-comment">${this.escapeHtml(lineText)}</span>`;
      return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
    }

    // 2. Preprocessor Directives for languages that support cpp (C, Assembly, DTS)
    if (this.grammar.hasPreprocessor && trimmed.startsWith("#")) {
      // #define <MACRO>
      const defMatch = lineText.match(/^(\s*#\s*define\s+)([a-zA-Z_]\w*)(.*)$/);
      if (defMatch) {
        const prefix = `<span class="tok-preproc">${this.escapeHtml(defMatch[1])}</span>`;
        const macroName = defMatch[2];
        const remainder = this.tokenizeExpression(defMatch[3]);
        const h = `${prefix}<span class="tok-macro ast-token" data-name="${this.escapeHtml(macroName)}">${this.escapeHtml(macroName)}</span>${remainder}`;
        return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
      }

      // #include <header.h>
      if (/^\s*#\s*include\b/.test(lineText)) {
        return this.renderIncludeLine(lineNo, lineText, isDimmed);
      }

      // Other preprocessor directives (#if, #ifdef, #ifndef, #else, #elif, #endif, #pragma, #undef)
      const preMatch = lineText.match(/^(\s*#\s*[a-zA-Z_]\w*)(.*)$/);
      if (preMatch) {
        const h = `<span class="tok-preproc">${this.escapeHtml(preMatch[1])}</span>${this.tokenizeExpression(preMatch[2])}`;
        return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
      }
    }

    // 3. General Lexical & Semantic Tokenizer
    const h = this.tokenizeExpression(lineText);
    return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
  }

  /**
   * Tokenize expression text with full multi-language lexical awareness.
   *
   * @param {string} text - Code snippet to tokenize
   * @param {object|null} [tok=null] - Enclosing server AST token if within map extent
   * @param {string} [enclosingTypeClass=""] - CSS class for enclosing AST extent
   * @returns {string} Escaped HTML with token styling and .ast-token interactive attributes
   */
  tokenizeExpression(text, tok = null, enclosingTypeClass = "") {
    const tokenRegex = getGrammarRegex(this.grammar.id);
    let html = "";
    let match;
    let lastIdx = 0;
    let lastKeyword = null;

    const astIdAttr = tok && tok.ast_id ? ` data-ast-id="${tok.ast_id}"` : "";
    const typeIdAttr = tok && tok.type_id ? ` data-type-id="${tok.type_id}"` : "";

    while ((match = tokenRegex.exec(text)) !== null) {
      const idx = match.index;
      const val = match[0];

      if (idx > lastIdx) {
        html += this.escapeHtml(text.substring(lastIdx, idx));
      }

      // 1. Comments
      if (val.startsWith("/*") || val.startsWith("//") ||
          (val.startsWith(";") && this.grammar.id === "assembly") ||
          (val.startsWith("@") && this.grammar.id === "assembly")) {
        html += `<span class="tok-comment">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (val.startsWith("#")) {
        // Rust attributes start with #[ or #!
        if (this.grammar.id === "rust" && (val.startsWith("#[") || val.startsWith("#!["))) {
          html += `<span class="tok-attribute">${this.escapeHtml(val)}</span>`;
        } else if (this.grammar.hasPreprocessor && /^\s*#\s*(include|define|undef|if|ifdef|ifndef|else|elif|endif|pragma)\b/.test(val)) {
          html += `<span class="tok-preproc">${this.escapeHtml(val)}</span>`;
        } else if (this.grammar.id !== "rust") {
          // Standard hash comment in Shell, Python, Perl, Makefile, Kconfig, YAML, etc.
          html += `<span class="tok-comment">${this.escapeHtml(val)}</span>`;
        } else {
          html += `<span class="tok-punct">${this.escapeHtml(val)}</span>`;
        }
        lastKeyword = null;
      } else if (val.startsWith("#[") || val.startsWith("#![")) {
        html += `<span class="tok-attribute">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (val.startsWith("@") && this.grammar.id === "python") {
        html += `<span class="tok-attribute">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (
        ((val.startsWith("$") || val.startsWith("@") || val.startsWith("%")) && (this.grammar.id === "shell" || this.grammar.id === "perl" || this.grammar.id === "makefile")) ||
        (val.startsWith("%") && this.grammar.id === "assembly")
      ) {
        html += `<span class="tok-variable">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (val.startsWith('"""') || val.startsWith("'''")) {
        html += `<span class="tok-string">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (val.startsWith("'") && this.grammar.id === "rust" && /^'[a-zA-Z_]\w*$/.test(val)) {
        html += `<span class="tok-lifetime">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (val.startsWith('"') || val.startsWith("'") || val.startsWith('r"') || val.startsWith('r#') || val.startsWith('b"')) {
        html += `<span class="tok-string">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (val.endsWith("!") && this.grammar.id === "rust" && /^[a-zA-Z_]\w*!$/.test(val)) {
        const macroName = val.slice(0, -1);
        html += `<span class="tok-macro ast-token ast-macro" data-name="${this.escapeHtml(macroName)}"${astIdAttr}${typeIdAttr}>${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (val === ":=" || val === "+=" || val === "?=" || val === "!=") {
        html += `<span class="tok-punct">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (/^(0x[0-9a-fA-F_]+|0b[01_]+|0o[0-7_]+|\d)[\w.]*$/.test(val) && !/^[a-zA-Z_]/.test(val)) {
        html += `<span class="tok-number">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (this.grammar.keywords && this.grammar.keywords.has(val)) {
        html += `<span class="tok-keyword">${this.escapeHtml(val)}</span>`;
        lastKeyword = val;
      } else if (this.grammar.isType(val) || (this.grammar.types && this.grammar.types.has(val))) {
        html += `<span class="tok-type ast-token" data-name="${this.escapeHtml(val)}"${astIdAttr}${typeIdAttr}>${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (/^\.[a-zA-Z_]\w*$/.test(val) && this.grammar.id === "assembly") {
        html += `<span class="tok-preproc">${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else if (/^[a-zA-Z_]\w*$/.test(val)) {
        const isFnCall = text.slice(tokenRegex.lastIndex).trimStart().startsWith("(");
        const isFnDef = lastKeyword === "fn" || lastKeyword === "def" || lastKeyword === "sub" || lastKeyword === "function";
        const isTypeDef = lastKeyword === "struct" || lastKeyword === "enum" || lastKeyword === "union" || lastKeyword === "trait" || lastKeyword === "class";

        let cls = "tok-identifier";
        if (isTypeDef) {
          cls = "tok-type";
        } else if (isFnCall || isFnDef) {
          cls = "tok-function";
        }

        const extraCls = enclosingTypeClass ? ` ${enclosingTypeClass}` : "";
        html += `<span class="${cls} ast-token${extraCls}" data-name="${this.escapeHtml(val)}"${astIdAttr}${typeIdAttr}>${this.escapeHtml(val)}</span>`;
        lastKeyword = null;
      } else {
        html += `<span class="tok-punct">${this.escapeHtml(val)}</span>`;
        // Do not reset lastKeyword on whitespace / punctuation if it could lead to definition
        if (val !== " " && val !== "\t") {
          // Keep keyword if it's colon or namespace separator
          if (val !== ":" && val !== "&" && val !== "*") {
            lastKeyword = null;
          }
        }
      }

      lastIdx = tokenRegex.lastIndex;
    }

    if (lastIdx < text.length) {
      html += this.escapeHtml(text.substring(lastIdx));
    }

    return html;
  }

  mapTypeToClass(typeId) {
    switch (typeId) {
      case 2: return "tok-comment"; // C_Comment
      case 75: case 76: case 77: return "tok-macro ast-macro"; // CPPro_define, CPPro_define_macro, CPPro_undef
      case 78: return "tok-include ast-include"; // CPPro_include
      case 71: case 72: case 69: case 70: return "tok-preproc ast-preproc"; // ifdef, ifndef, else, endif
      case 21: case 5: case 6: return "tok-function ast-func"; // function prototypes & decls
      case 28: case 27: case 26: return "tok-type ast-struct"; // struct, union, enum
      case 10: case 7: case 8: case 36: case 37: case 41: case 42: return "tok-type ast-type"; // types & typedefs
      case 9: return "ast-field"; // struct field
      case 1: return "ast-compound"; // compound

      // ASM AST types (ASTT.ASM_*: 83 - 87)
      case 83: return "tok-macro ast-macro"; // ASM_Macro
      case 84: return "tok-preproc ast-preproc"; // ASM_Directive
      case 85: return "tok-keyword ast-ref"; // ASM_Instruction (mnemonics, registers)
      case 86: return "tok-function ast-func"; // ASM_Label
      case 87: return "tok-comment"; // ASM_Comment

      // Rust AST types (ASTT.Rust_*: 123 - 143)
      case 123: case 125: case 127: case 130: case 131:
        return "tok-type ast-struct"; // Rust_Struct, Rust_Enum, Rust_Union, Rust_Trait, Rust_TraitAlias
      case 133:
        return "tok-type ast-type"; // Rust_Type
      case 128:
        return "tok-function ast-func"; // Rust_Fn
      case 124: case 129:
        return "ast-field"; // Rust_Field, Rust_Param
      case 134: case 135:
        return "tok-variable ast-ref"; // Rust_Const, Rust_Static
      case 138: case 139:
        return "tok-macro ast-macro"; // Rust_MacroDef, Rust_MacroCall
      case 141:
        return "tok-attribute ast-attribute"; // Rust_Attribute
      case 142: case 143:
        return "tok-comment"; // Rust_DocComment, Rust_Comment
      case 132: case 136: case 137: case 140:
        return "ast-compound"; // Rust_Impl, Rust_Mod, Rust_Use, Rust_ForeignMod

      default: return "ast-ref";
    }
  }

  escapeHtml(str) {
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
}
