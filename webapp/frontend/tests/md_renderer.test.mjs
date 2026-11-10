/**
 * md_renderer.test.mjs - Markdown documentation renderer (moved from scratch/test_md_renderer.mjs).
 */

import assert from "node:assert";
import { test } from "node:test";
import { renderMarkdownDocument, isMarkdownPath, extractMarkdownTitle } from "../js/views/docs/md_renderer.js";
import { renderDocument, isDocumentationPath } from "../js/views/docs/rst_renderer.js";

// Body kept unindented: the Markdown samples are template literals whose indentation is significant
test("markdown renderer", () => {
// 1. Path & Documentation Detection

assert.strictEqual(isMarkdownPath("README.md"), true);
assert.strictEqual(isMarkdownPath("webapp/API.md"), true);
assert.strictEqual(isMarkdownPath("Documentation/kernel-doc.markdown"), true);
assert.strictEqual(isMarkdownPath("kernel/sched/core.c"), false);
assert.strictEqual(isMarkdownPath("init/Kconfig"), false);

assert.strictEqual(isDocumentationPath("webapp/API.md"), true);
assert.strictEqual(isDocumentationPath("README.md"), true);
assert.strictEqual(isDocumentationPath("Documentation/admin-guide/index.rst"), true);
assert.strictEqual(isDocumentationPath("init/main.c"), false);


// 2. Headings, TOC, and Permalinks

const sampleMd = `
# Project Overview

Introduction to the kernel platform.

## Architecture & Design

Details of the architecture.

### Subsystem Components

Modular components.

## Another Section
### Subsystem Components
`;

const res = renderMarkdownDocument(sampleMd, "docs/overview.md");
assert.strictEqual(res.title, "Project Overview");
assert.strictEqual(res.toc.length, 5);
assert.strictEqual(res.toc[0].level, 1);
assert.strictEqual(res.toc[0].id, "project-overview");
assert.strictEqual(res.toc[1].level, 2);
assert.strictEqual(res.toc[1].id, "architecture-design");
assert.strictEqual(res.toc[2].level, 3);
assert.strictEqual(res.toc[2].id, "subsystem-components");
// Test duplicate slug deduplication
assert.strictEqual(res.toc[4].id, "subsystem-components-1");

// Verify ¶ permalinks
assert(res.html.includes('<h1 id="project-overview">Project Overview<a class="kdoc-headerlink" href="#project-overview" title="Permalink to this headline">¶</a></h1>'));
assert(res.html.includes('<h2 id="architecture-design">Architecture &amp; Design<a class="kdoc-headerlink" href="#architecture-design" title="Permalink to this headline">¶</a></h2>'));


// 3. Fenced Code Blocks with Multi-Language Syntax Highlighting

const codeBlockMd = `
Here is Rust code:

\`\`\`rust
pub fn spin_lock(lock: &SpinLock) -> Result<LockGuard> {
    lock.acquire(); // inline comment
}
\`\`\`

Here is Python code:

\`\`\`python
@staticmethod
def load_config(path: str) -> bool:
    # load file
    return True
\`\`\`
`;

const codeRes = renderMarkdownDocument(codeBlockMd, "test.md");
// Rust code highlighting checks
assert(codeRes.html.includes('class="highlight-rust notranslate kdoc-code-block"'), "Rust highlight container");
assert(codeRes.html.includes('<span class="tok-keyword">pub</span>'), "Rust pub keyword highlighted");
assert(codeRes.html.includes('<span class="tok-keyword">fn</span>'), "Rust fn keyword highlighted");
assert(codeRes.html.includes('class="tok-function ast-token" data-name="spin_lock"'), "Rust function name");
assert(codeRes.html.includes('class="tok-type ast-token" data-name="SpinLock"'), "Rust SpinLock type");
assert(codeRes.html.includes('<span class="tok-comment">// inline comment</span>'), "Rust comment");

// Python code highlighting checks
assert(codeRes.html.includes('class="highlight-python notranslate kdoc-code-block"'), "Python highlight container");
assert(codeRes.html.includes('<span class="tok-attribute">@staticmethod</span>'), "Python @staticmethod decorator");
assert(codeRes.html.includes('<span class="tok-keyword">def</span>'), "Python def keyword");
assert(codeRes.html.includes('<span class="tok-type ast-token" data-name="True"'), "Python True constant");
assert(codeRes.html.includes('# load file</span>'), "Python comment");


// 4. GitHub-Style Callouts / Alerts (> [!NOTE], > [!WARNING], etc.)

const alertMd = `
> [!NOTE]
> This is a helpful note for developers.

> [!WARNING]
> Critical warning: ensure locks are acquired in order.

> [!TIP]
> Use the command palette for faster navigation.

> Regular blockquote with some wisdom.
`;

const alertRes = renderMarkdownDocument(alertMd, "alert.md");
assert(alertRes.html.includes('<div class="admonition note"><p class="admonition-title">Note</p><p>This is a helpful note for developers.</p></div>'), "Note alert rendered");
assert(alertRes.html.includes('<div class="admonition warning"><p class="admonition-title">Warning</p><p>Critical warning: ensure locks are acquired in order.</p></div>'), "Warning alert rendered");
assert(alertRes.html.includes('<div class="admonition tip"><p class="admonition-title">Tip</p><p>Use the command palette for faster navigation.</p></div>'), "Tip alert rendered");
assert(alertRes.html.includes('<blockquote><p>Regular blockquote with some wisdom.</p></blockquote>'), "Regular blockquote rendered");


// 5. GFM Tables with Alignment

const tableMd = `
| Symbol | Kind | Description |
| :--- | :---: | ---: |
| \`schedule()\` | Core | Task scheduler |
| \`do_fork()\` | Process | Clone process |
`;

const tableRes = renderMarkdownDocument(tableMd, "table.md");
assert(tableRes.html.includes('<table class="docutils">'), "Table element generated");
assert(tableRes.html.includes('<thead><tr><th style="text-align:left;">Symbol</th><th style="text-align:center;">Kind</th><th style="text-align:right;">Description</th></tr></thead>'), "Table headers with alignments");
assert(tableRes.html.includes('<td style="text-align:left;"><code class="docutils literal notranslate">schedule()</code></td>'), "Table cells with alignment and code");


// 6. Task Lists

const taskMd = `
- [x] Implement multi-language lexer
- [x] Add Rust AST support
- [ ] Write integration benchmarks
`;

const taskRes = renderMarkdownDocument(taskMd, "tasks.md");
assert(taskRes.html.includes('<ul class="task-list">'), "Task list element");
assert(taskRes.html.includes('<li class="task-list-item"><input type="checkbox" disabled checked="" /> <span>Implement multi-language lexer</span></li>'), "Checked task item");
assert(taskRes.html.includes('<li class="task-list-item"><input type="checkbox" disabled /> <span>Write integration benchmarks</span></li>'), "Unchecked task item");


// 7. Inline Formatting & Kernel Automarkup

const inlineMd = `
Check **bold text**, *italic text*, ***bold italic***, ~~struck out~~, and \`inline code\`.

Kernel symbols: CONFIG_SMP, schedule(), and struct task_struct.

Links: [Developer Guide](guide.md) and [External Site](https://kernel.org).
`;

const inlineRes = renderMarkdownDocument(inlineMd, "doc/intro.md");
assert(inlineRes.html.includes('<strong>bold text</strong>'), "Bold text");
assert(inlineRes.html.includes('<em>italic text</em>'), "Italic text");
assert(inlineRes.html.includes('<strong><em>bold italic</em></strong>'), "Bold italic text");
assert(inlineRes.html.includes('<del>struck out</del>'), "Strikethrough text");
assert(inlineRes.html.includes('<code class="docutils literal notranslate">inline code</code>'), "Inline code");

// Automarkup
assert(inlineRes.html.includes('<a class="reference internal kdoc-kconfig" data-kconfig="SMP" href="#"><code class="xref kconfig">CONFIG_SMP</code></a>'), "CONFIG_SMP automarkup link");
assert(inlineRes.html.includes('<a class="reference internal kdoc-symbol" data-symbol="schedule" href="#"><code>schedule()</code></a>'), "schedule() automarkup link");
assert(inlineRes.html.includes('<a class="reference internal kdoc-symbol" data-symbol="task_struct" href="#"><code>struct task_struct</code></a>'), "struct task_struct automarkup link");

// In-repo doc link vs external link
assert(inlineRes.html.includes('<a class="reference internal" data-doc="doc/guide.md" href="#">Developer Guide</a>'), "Internal doc relative navigation link");
assert(inlineRes.html.includes('<a class="reference external" href="https://kernel.org" target="_blank" rel="noopener noreferrer">External Site</a>'), "External web link");


// 8. Unified renderDocument dispatch

const unifiedMd = renderDocument("# Hello Markdown\n\nContent here.", "README.md");
assert.strictEqual(unifiedMd.title, "Hello Markdown");
assert(unifiedMd.html.includes('<h1 id="hello-markdown">Hello Markdown'), "Markdown rendered via renderDocument");

const unifiedRst = renderDocument("Sample Title\n============\n\nrST text.", "doc.rst");
assert.strictEqual(unifiedRst.title, "Sample Title");
assert(unifiedRst.html.includes('<h1 id="sample-title">Sample Title'), "rST rendered via renderDocument");


// 9. Linked Images & Badges

const badgeMd = `
<!-- This is an HTML comment that should be omitted -->
[![Crates.io](https://img.shields.io/crates/v/pin-init.svg)](https://crates.io/crates/pin-init)
![License](https://img.shields.io/crates/l/pin-init)
`;
const badgeRes = renderMarkdownDocument(badgeMd, "rust/pin-init/README.md");
assert(!badgeRes.html.includes("HTML comment"), "HTML comments are omitted from rendered HTML");
assert(badgeRes.html.includes('<a class="reference external" href="https://crates.io/crates/pin-init" target="_blank" rel="noopener noreferrer"><img class="kdoc-img kdoc-badge" src="https://img.shields.io/crates/v/pin-init.svg" alt="Crates.io" /></a>'), "Linked badge image rendered correctly without broken syntax");
assert(badgeRes.html.includes('<img class="kdoc-img kdoc-badge" src="https://img.shields.io/crates/l/pin-init" alt="License" />'), "Standalone badge image rendered with badge class");


// 10. Reference-style Links & Definitions

const refMd = `
[Pinning][pinning] is Rust's way of ensuring data does not move.
This crate was originally named [\`pinned-init\`].
Visit [Rust-for-Linux] for more details.

[\`pinned-init\`]: https://crates.io/crates/pinned-init
[pinning]: https://doc.rust-lang.org/std/pin/index.html
[Rust-for-Linux]: https://rust-for-linux.com/
`;
const refRes = renderMarkdownDocument(refMd, "rust/pin-init/README.md");
assert(!refRes.html.includes("https://crates.io/crates/pinned-init]:"), "Reference definitions omitted from visible paragraphs");
assert(refRes.html.includes('<a class="reference external" href="https://doc.rust-lang.org/std/pin/index.html" target="_blank" rel="noopener noreferrer">Pinning</a>'), "Full reference link [Pinning][pinning] resolved");
assert(refRes.html.includes('<a class="reference external" href="https://crates.io/crates/pinned-init" target="_blank" rel="noopener noreferrer"><code class="docutils literal notranslate">pinned-init</code></a>'), "Shortcut reference link [`pinned-init`] resolved");
assert(refRes.html.includes('<a class="reference external" href="https://rust-for-linux.com/" target="_blank" rel="noopener noreferrer">Rust-for-Linux</a>'), "Shortcut reference link [Rust-for-Linux] resolved");
});

test("indented block-like lines never stall the parser", async () => {
  const { renderMarkdownFragment } = await import("../js/views/docs/md_renderer.js");
  for (const src of ["  # heading", "    # deep", "  | a | b |", "x\n    # y\nz"]) {
    assert.equal(typeof renderMarkdownFragment(src), "string", src);
  }
  assert.match(renderMarkdownFragment("  # heading"), /<h1/);
});
