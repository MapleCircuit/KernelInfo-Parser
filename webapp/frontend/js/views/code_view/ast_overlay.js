/**
 * ast_overlay.js - Dual-Layer Syntax Highlighting & AST Disjoint Interval Overlay.
 * Combines client-side lexical parsing with database spatial map tokens and #ifdef dimming.
 */

const C_KEYWORDS = new Set([
  "auto", "break", "case", "char", "const", "continue", "default", "do",
  "double", "else", "enum", "extern", "float", "for", "goto", "if",
  "inline", "int", "long", "register", "restrict", "return", "short",
  "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
  "unsigned", "void", "volatile", "while", "_Bool", "_Complex", "_Imaginary",
  "__init", "__exit", "__user", "__kernel", "__iomem", "__percpu", "__rcu",
  "asmlinkage", "FASTCALL", "__attribute__", "__inline__", "__inline",
  "__asm__", "__volatile__", "likely", "unlikely", "NULL"
]);

const C_TYPES = new Set([
  "size_t", "ssize_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
  "int8_t", "int16_t", "int32_t", "int64_t", "u8", "u16", "u32", "u64",
  "s8", "s16", "s32", "s64", "__u8", "__u16", "__u32", "__u64",
  "__s8", "__s16", "__s32", "__s64", "__be16", "__be32", "__be64",
  "__le16", "__le32", "__le64", "gfp_t", "phys_addr_t", "resource_size_t",
  "dma_addr_t", "loff_t", "atomic_t", "atomic64_t", "spinlock_t",
  "mutex", "rwsem", "list_head", "hlist_node", "bool"
]);

export class AstOverlay {
  constructor() {
    this.tokensByLine = new Map(); // lineNo -> list of AST tokens
    this.ftype = null;
    this.filePath = "";
    this.isCFile = true; // Default true until setFileInfo is called
  }

  setFileInfo(ftype, filePath = "") {
    this.ftype = ftype;
    this.filePath = filePath || "";
    this.isCFile = (ftype === 1) || Boolean(filePath && /\.(c|h|i)$/i.test(filePath));
  }

  loadServerTokens(serverTokens) {
    this.tokensByLine.clear();
    if (!Array.isArray(serverTokens)) return;

    // Support both nested arrays [[l_s, c_s, l_e, c_e, ast_id, type_id], ...] and flat array
    if (serverTokens.length > 0 && Array.isArray(serverTokens[0])) {
      for (const tok of serverTokens) {
        const [line_s, char_s, line_e, char_e, ast_id, type_id] = tok;
        // Skip pure comments from clickable AST tokens map
        if (type_id === 2) continue;

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
      }
    } else {
      for (let i = 0; i < serverTokens.length; i += 6) {
        const line_s = serverTokens[i];
        const char_s = serverTokens[i + 1];
        const line_e = serverTokens[i + 2];
        const char_e = serverTokens[i + 3];
        const ast_id = serverTokens[i + 4];
        const type_id = serverTokens[i + 5];
        if (type_id === 2) continue;

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
      }
    }
  }

  renderLineHtml(lineNo, rawLineText, isDimmed = false) {
    if (!rawLineText) {
      return isDimmed ? `<span class="ast-dimmed">&nbsp;</span>` : "&nbsp;";
    }

    // 1. Direct Include Line Handling:
    // If in a C file and line is an #include directive, ALWAYS format the include line accurately:
    // '#include ' as non-clickable preproc, and the header as clickable .ast-include.
    // If the map has a token for this line (type_id 78), attach the server's ast_id.
    if (this.isCFile && /^\s*#\s*include\b/.test(rawLineText)) {
      return this.renderIncludeLine(lineNo, rawLineText, isDimmed);
    }

    const astTokens = this.tokensByLine.get(lineNo) || [];

    // Filter out broad multi-line container envelopes (functions, compound statements, preprocessor blocks)
    // that span across multiple lines without providing token-level detail for this specific line.
    const specificTokens = astTokens.filter((t) => {
      const isBroadContainer = t.isMultiLine && (t.type_id === 1 || t.type_id === 21 || t.type_id === 69 || t.type_id === 70 || t.type_id === 71 || t.type_id === 72);
      return !isBroadContainer;
    });

    // If specific server AST tokens exist for this line, prioritize map information
    if (specificTokens.length > 0) {
      return this.renderWithAstTokens(rawLineText, specificTokens, isDimmed);
    }

    // Otherwise, revert to client-side lexical/semantic parser
    return this.renderLexical(rawLineText, isDimmed);
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
    return this.renderLexical(rawLineText, isDimmed);
  }

  renderWithAstTokens(lineText, astTokens, isDimmed) {
    const len = lineText.length;
    if (len === 0) return "&nbsp;";

    // Map each character position to the most specific (narrowest) AST token
    const charToken = new Array(len).fill(null);

    // Sort tokens by width descending: widest scopes first, narrowest symbols last (so narrowest wins)
    const sorted = [...astTokens].sort((a, b) => {
      const wA = (a.char_e || len) - (a.char_s || 0);
      const wB = (b.char_e || len) - (b.char_s || 0);
      return wB - wA;
    });

    for (const tok of sorted) {
      const s = Math.max(0, Math.min(tok.char_s || 0, len));
      const e = Math.max(0, Math.min(tok.char_e || len, len));
      for (let i = s; i < e; i++) {
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
    // 1. Comments: never clickable
    if (tok.type_id === 2 || snippet.trim().startsWith("//") || snippet.trim().startsWith("/*")) {
      return `<span class="tok-comment">${this.escapeHtml(snippet)}</span>`;
    }

    // 2. Preprocessor include: Only the <header> or "header" is clickable, NEVER the #include keyword!
    if (tok.type_id === 78) {
      return this.renderIncludeLine(null, snippet, false);
    }

    // 3. Preprocessor defines & macros
    if (tok.type_id === 75 || tok.type_id === 76) {
      const defMatch = snippet.match(/^(\s*#?\s*define\s+)([a-zA-Z_]\w*)(.*)$/);
      if (defMatch) {
        const prefix = `<span class="tok-preproc">${this.escapeHtml(defMatch[1])}</span>`;
        const macroName = defMatch[2];
        const remainder = this.tokenizeExpression(defMatch[3]);
        return `${prefix}<span class="tok-macro ast-token" data-name="${this.escapeHtml(macroName)}" data-ast-id="${tok.ast_id}" data-type-id="${tok.type_id}">${this.escapeHtml(macroName)}</span>${remainder}`;
      }
    }

    // 4. Internal tokenization for code within the map extent
    const typeClass = this.mapTypeToClass(tok.type_id);
    const tokenRegex = /(\/\/[^\n]*|\/\*[\s\S]*?\*\/|".*?"|'.*?'|[a-zA-Z_]\w*|\b0x[0-9a-fA-F]+[uUlL]*\b|\b\d+[uUlL]*\b|[^\s\w]+)/g;
    let html = "";
    let match;
    let lastIdx = 0;

    while ((match = tokenRegex.exec(snippet)) !== null) {
      const idx = match.index;
      const val = match[0];

      if (idx > lastIdx) {
        html += this.escapeHtml(snippet.substring(lastIdx, idx));
      }

      if (val.startsWith("//") || val.startsWith("/*") || (!this.isCFile && val.startsWith("#"))) {
        html += `<span class="tok-comment">${this.escapeHtml(val)}</span>`;
      } else if (val.startsWith('"') || val.startsWith("'")) {
        html += `<span class="tok-string">${this.escapeHtml(val)}</span>`;
      } else if (/^(0x[0-9a-fA-F]+|\d+)[uUlL]*$/.test(val)) {
        html += `<span class="tok-number">${this.escapeHtml(val)}</span>`;
      } else if (this.isCFile && C_KEYWORDS.has(val)) {
        html += `<span class="tok-keyword">${this.escapeHtml(val)}</span>`;
      } else if (this.isCFile && (C_TYPES.has(val) || val.endsWith("_t"))) {
        html += `<span class="tok-type ast-token" data-name="${this.escapeHtml(val)}" data-ast-id="${tok.ast_id}" data-type-id="${tok.type_id}">${this.escapeHtml(val)}</span>`;
      } else if (/^[a-zA-Z_]\w*$/.test(val)) {
        const isFnCall = snippet.slice(tokenRegex.lastIndex).trimStart().startsWith("(");
        const cls = isFnCall ? "tok-function" : "tok-identifier";
        html += `<span class="${cls} ast-token ${typeClass}" data-name="${this.escapeHtml(val)}" data-ast-id="${tok.ast_id}" data-type-id="${tok.type_id}">${this.escapeHtml(val)}</span>`;
      } else {
        html += `<span class="tok-punct">${this.escapeHtml(val)}</span>`;
      }

      lastIdx = tokenRegex.lastIndex;
    }

    if (lastIdx < snippet.length) {
      html += this.escapeHtml(snippet.substring(lastIdx));
    }

    return html;
  }

  isClickableType(typeId) {
    if (!typeId || typeId === 2 || typeId === 69 || typeId === 70) return false;
    return true;
  }

  renderLexical(lineText, isDimmed) {
    const trimmed = lineText.trim();

    if (this.isCFile) {
      // 1. Comments: C++ style, C block starts, or multi-line comment continuations (lines starting with '*')
      if (trimmed.startsWith("//") || trimmed.startsWith("/*") || (trimmed.startsWith("*") && !trimmed.startsWith("*="))) {
        const h = `<span class="tok-comment">${this.escapeHtml(lineText)}</span>`;
        return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
      }

      // 2. Preprocessor Directives
      if (trimmed.startsWith("#")) {
        // Handle #define <MACRO>
        const defMatch = lineText.match(/^(\s*#\s*define\s+)([a-zA-Z_]\w*)(.*)$/);
        if (defMatch) {
          const prefix = `<span class="tok-preproc">${this.escapeHtml(defMatch[1])}</span>`;
          const macroName = defMatch[2];
          const remainder = this.tokenizeExpression(defMatch[3]);
          const h = `${prefix}<span class="tok-macro ast-token" data-name="${this.escapeHtml(macroName)}">${this.escapeHtml(macroName)}</span>${remainder}`;
          return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
        }

        // Handle #include <header.h> or "header.h"
        if (/^\s*#\s*include\b/.test(lineText)) {
          return this.renderIncludeLine(null, lineText, isDimmed);
        }

        // Other preprocessor directives (#if, #ifdef, #ifndef, #else, #endif, #pragma, #undef)
        const preMatch = lineText.match(/^(\s*#\s*[a-zA-Z_]\w*)(.*)$/);
        if (preMatch) {
          const h = `<span class="tok-preproc">${this.escapeHtml(preMatch[1])}</span>${this.tokenizeExpression(preMatch[2])}`;
          return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
        }
      }
    } else {
      // Non-C files: '#' is standard comment syntax (Makefiles, Kconfig, shell, etc.)
      if (trimmed.startsWith("#") || trimmed.startsWith("//") || trimmed.startsWith("/*")) {
        const h = `<span class="tok-comment">${this.escapeHtml(lineText)}</span>`;
        return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
      }
    }

    // 3. General Lexical & Semantic Tokenizer
    const h = this.tokenizeExpression(lineText);
    return isDimmed ? `<span class="ast-dimmed">${h}</span>` : h;
  }

  tokenizeExpression(text) {
    const tokenRegex = /(\/\/[^\n]*|\/\*[\s\S]*?\*\/|".*?"|'.*?'|[a-zA-Z_]\w*|\b0x[0-9a-fA-F]+[uUlL]*\b|\b\d+[uUlL]*\b|[^\s\w]+)/g;
    let html = "";
    let match;
    let lastIdx = 0;

    while ((match = tokenRegex.exec(text)) !== null) {
      const idx = match.index;
      const val = match[0];

      if (idx > lastIdx) {
        html += this.escapeHtml(text.substring(lastIdx, idx));
      }

      if (val.startsWith("//") || val.startsWith("/*") || (!this.isCFile && val.startsWith("#"))) {
        html += `<span class="tok-comment">${this.escapeHtml(val)}</span>`;
      } else if (val.startsWith('"') || val.startsWith("'")) {
        html += `<span class="tok-string">${this.escapeHtml(val)}</span>`;
      } else if (/^(0x[0-9a-fA-F]+|\d+)[uUlL]*$/.test(val)) {
        html += `<span class="tok-number">${this.escapeHtml(val)}</span>`;
      } else if (this.isCFile && C_KEYWORDS.has(val)) {
        html += `<span class="tok-keyword">${this.escapeHtml(val)}</span>`;
      } else if (this.isCFile && (C_TYPES.has(val) || val.endsWith("_t") || val.endsWith("_type"))) {
        html += `<span class="tok-type ast-token" data-name="${this.escapeHtml(val)}">${this.escapeHtml(val)}</span>`;
      } else if (/^[a-zA-Z_]\w*$/.test(val)) {
        const isFnCall = text.slice(tokenRegex.lastIndex).trimStart().startsWith("(");
        const cls = isFnCall ? "tok-function" : "tok-identifier";
        html += `<span class="${cls} ast-token" data-name="${this.escapeHtml(val)}">${this.escapeHtml(val)}</span>`;
      } else {
        html += `<span class="tok-punct">${this.escapeHtml(val)}</span>`;
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
