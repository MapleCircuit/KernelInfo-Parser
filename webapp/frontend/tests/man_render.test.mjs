/**
 * man_render.test.mjs - Man page renderers (web / terminal), the man tab model and its deep link
 * (webapp/API_FRONTEND.md §5.38, §5.26).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { manHeader, renderManTerm, renderManWeb, safeUrl } from "../js/views/man/man_render.js";
import {
  compareTags, groupIndex, isManPagePath, linuxApiTab, linuxSymbolTab, man7Url, manTabFields, manTabTitle, newestTag, pageOfPath, parseManRef, sectionTitle,
} from "../js/views/man/man_model.js";
import { hashRepo, tabRoute } from "../js/tab_routes.js";
import "../js/routes.js";

const DOC = {
  format: 1,
  th: { title: "READ", section: "2", date: "2024-05-02", source: "Linux man-pages 6.9", manual: "" },
  so: null,
  sections: [
    { id: "name", title: "NAME", blocks: [{ t: "p", c: [{ x: "read — read from a file descriptor" }] }] },
    { id: "synopsis", title: "SYNOPSIS", blocks: [{ t: "pre", ex: false, lines: [[{ x: "#include <unistd.h>", f: "B" }], [{ x: "ssize_t read(int " , f: "B" }, { x: "fd", f: "I" }, { x: ");", f: "B" }]] }] },
    {
      id: "description", title: "DESCRIPTION", blocks: [
        { t: "p", c: [{ x: "read", f: "B", ref: ["read", "2"] }, { x: "(2) attempts to read up to " }, { x: "count", f: "I" }, { x: " bytes from file descriptor " }, { x: "fd", f: "I" }, { x: " into the buffer starting at " }, { x: "buf", f: "I" }, { x: ". On files that support seeking, the read operation commences at the file offset." }] },
        { t: "ss", id: "description-details", title: "Details <x>" },
        { t: "tp", tags: [[{ x: "EAGAIN", f: "B" }]], body: [{ t: "p", c: [{ x: "The file descriptor refers to a file other than a socket." }] }] },
        { t: "tp", tags: [[{ x: "•" }]], body: [{ t: "p", c: [{ x: "bullet item" }] }] },
        { t: "rs", body: [{ t: "p", c: [{ x: "indented" }, { br: 1 }, { x: "next line" }] }] },
        { t: "pre", ex: true, lines: [[{ x: "int main(void) { return 0; }" }]] },
        { t: "table", align: ["l", "r"], box: true, header: 1, rows: [[[{ x: "Name", f: "B" }], [{ x: "Value", f: "B" }]], [[{ x: "A<b>" }], [{ x: "1" }]], [[{ x: "LONGER" }], [{ x: "22" }]]] },
        { t: "p", c: [{ x: "Home: " }, { x: "man7", url: "https://man7.org/" }, { x: " bad: " }, { x: "click", url: "javascript:alert(1)" }, { x: " mail: " }, { x: "", url: "mailto:a@b.org" }] },
      ],
    },
    { id: "see-also", title: "SEE ALSO", blocks: [{ t: "p", c: [{ x: "close", f: "B", ref: ["close", "2"] }, { x: "(2), " }, { x: "x\"><img src=y onerror=z>", f: "B", ref: ["x\"><img src=y onerror=z>", "3"] }, { x: "(3)" }] }] },
  ],
};

test("header parts follow .TH, the manual defaults by section", () => {
  assert.deepEqual(manHeader(DOC), { tag: "READ(2)", manual: "System Calls Manual", source: "Linux man-pages 6.9", date: "2024-05-02", title: "READ", section: "2" });
  assert.equal(manHeader({ th: { title: "ls", section: "1", manual: "My Manual" } }).manual, "My Manual");
  assert.equal(manHeader(null, { name: "intro", section: "1" }).tag, "INTRO(1)");
});

test("web mode: title bar, TOC, headings, tables, refs", () => {
  const html = renderManWeb(DOC, { idPrefix: "t-" });
  assert.match(html, /<span class="man-web-tag">READ\(2\)<\/span><span class="man-web-manual">System Calls Manual<\/span>/);
  assert.match(html, /data-man-anchor="t-see-also">SEE ALSO<\/a>/);
  assert.match(html, /<h2 id="t-description">DESCRIPTION<\/h2>/);
  assert.match(html, /<h3 class="man-ss" id="t-description-details">Details &lt;x&gt;<\/h3>/);
  assert.match(html, /<a href="#" class="man-ref" data-man-ref="read" data-man-sec="2"[^>]*><b>read<\/b><\/a>\(2\)/);
  assert.match(html, /<dl class="man-tp"><dt><b>EAGAIN<\/b><\/dt><dd><p>/);
  assert.match(html, /<pre class="man-pre man-ex">int main\(void\)/);
  assert.match(html, /<pre class="man-pre"><b>#include &lt;unistd.h&gt;<\/b>\n<b>ssize_t read\(int <\/b><i>fd<\/i>/);
  assert.match(html, /<table class="man-tbl man-box"><thead><tr><th class="man-al-left"><b>Name<\/b><\/th><th class="man-al-right">/);
  assert.match(html, /<td class="man-al-left">A&lt;b&gt;<\/td>/);
  assert.match(html, /indented<br>next line/);
  assert.match(html, /<footer class="man-web-foot"><span>Linux man-pages 6\.9<\/span><span>2024-05-02<\/span>/);
});

test("everything is escaped and URLs are sanitised", () => {
  for (const html of [renderManWeb(DOC), renderManTerm(DOC)]) {
    assert.ok(!html.includes("<img"), "markup in text is escaped");
    assert.ok(!html.includes("javascript:"), "javascript: URLs are dropped");
    assert.match(html, /data-man-ref="x&quot;&gt;&lt;img src=y onerror=z&gt;"/);
    assert.match(html, /<a href="https:\/\/man7\.org\/" class="man-url" target="_blank" rel="noopener noreferrer">/);
    assert.match(html, /href="mailto:a@b\.org"/);
  }
  assert.equal(safeUrl("https://x.org/a?b=1"), "https://x.org/a?b=1");
  assert.equal(safeUrl("HTTP://X.ORG"), "HTTP://X.ORG");
  assert.equal(safeUrl("mailto:me@x.org"), "mailto:me@x.org");
  assert.equal(safeUrl("javascript:alert(1)"), "");
  assert.equal(safeUrl("data:text/html,x"), "");
  assert.equal(safeUrl("https://x.org/\"onmouseover=1"), "");
  assert.equal(renderManWeb({ sections: [] }).includes("This page has no content."), true);
  assert.doesNotThrow(() => renderManWeb({ sections: [{ id: "x", title: "X", blocks: [null, { t: "zz" }, { t: "p" }, { t: "table" }] }] }));
  assert.doesNotThrow(() => renderManTerm({ sections: [{ id: "x", title: "X", blocks: [null, { t: "zz" }, { t: "p" }, { t: "table" }] }] }));
});

const strip = (html) => html.replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, "\"").replace(/&#39;/g, "'").replace(/&amp;/g, "&");

test("terminal mode: header/footer lines, 7-column indent, 80-column justified fill", () => {
  const html = renderManTerm(DOC);
  assert.match(html, /^<pre class="man-term" style="--man-cols:80">/);
  const lines = strip(html).split("\n");
  assert.equal(lines[0].length, 80);
  assert.ok(lines[0].startsWith("READ(2)") && lines[0].endsWith("READ(2)") && lines[0].includes("System Calls Manual"));
  const last = lines[lines.length - 1];
  assert.ok(last.startsWith("Linux man-pages 6.9") && last.endsWith("READ(2)") && last.includes("2024-05-02") && last.length === 80);
  assert.ok(lines.includes("NAME"));
  assert.ok(lines.includes("       read — read from a file descriptor"));
  // The DESCRIPTION paragraph wraps: every line but its last is justified to 80 columns
  const start = lines.findIndex((l) => /^ {7}read\(2\) +attempts/.test(l));
  assert.ok(start > 0);
  assert.equal(lines[start].length, 80);
  assert.ok(lines[start + 1].startsWith("       ") && lines[start + 1].length <= 80);
  // Subsection at 3, tag at 7 then body at 14; a short tag shares its line with the body
  assert.ok(lines.includes("   Details <x>"));
  const eagain = lines.indexOf("       EAGAIN");
  assert.ok(eagain > 0);
  assert.ok(lines[eagain + 1].startsWith("              The file descriptor"));
  assert.ok(lines.includes("       •      bullet item"));
  // .RS indents, a forced break ends the line
  assert.ok(lines.includes("              indented"));
  assert.ok(lines.includes("              next line"));
  // Boxed table with a header rule, right-aligned numbers
  assert.ok(lines.some((l) => l.startsWith("       ┌")));
  assert.ok(lines.includes("       │ A<b>   │     1 │"));
  assert.ok(lines.includes("       │ LONGER │    22 │"));
  assert.ok(lines.some((l) => l.startsWith("       ├")));
  // .UR in a terminal: text then <url>
  assert.ok(strip(html).includes("man7 <https://man7.org/>"));
  // Bold is a bold span, italic an underline span; refs stay links
  assert.match(html, /<a href="#" class="man-ref" data-man-ref="read" data-man-sec="2"><span class="mt-b">read<\/span><\/a>\(2\)/);
  assert.match(html, /<span class="mt-u">count<\/span>/);
  assert.equal(strip(renderManTerm(DOC, { width: 60 })).split("\n")[0].length, 60);
});

test("page paths, refs and section titles", () => {
  assert.ok(isManPagePath("man2/read.2"));
  assert.ok(isManPagePath("man/man2const/EPOLL_CTL_ADD.2const"));
  assert.ok(isManPagePath("man/man3type/size_t.3type"));
  assert.ok(!isManPagePath("Documentation/man2/read.2"));
  assert.ok(!isManPagePath("man2/README"));
  assert.ok(!isManPagePath("man/man2/sub/read.2"));
  assert.deepEqual(pageOfPath("man/man2/read.2"), { name: "read", section: "2" });
  assert.deepEqual(pageOfPath("man3/printf.3"), { name: "printf", section: "3" });
  assert.equal(pageOfPath("man2/read.3"), null);
  assert.deepEqual(parseManRef("epoll_ctl(2)"), { name: "epoll_ctl", section: "2" });
  assert.deepEqual(parseManRef(" size_t (3type) "), { name: "size_t", section: "3type" });
  assert.equal(parseManRef("read"), null);
  assert.equal(man7Url("size_t", "3type"), "https://man7.org/linux/man-pages/man3/size_t.3type.html");
  assert.equal(sectionTitle("2"), "System calls");
  assert.equal(sectionTitle("3const"), "Library functions — constants");
  assert.equal(manTabTitle({ manPage: "read", manSec: "2" }), "man read(2)");
  assert.equal(manTabTitle({ manPath: "man/man7/epoll.7" }), "man epoll(7)");
  assert.equal(manTabTitle({}), "Man pages");
});

test("index groups follow section order and the filter", () => {
  const pages = [
    { name: "ls", section: "1", summary: "list directory contents" },
    { name: "write", section: "2", summary: "write to a file descriptor" },
    { name: "read", section: "2", summary: "read from a file descriptor" },
    { name: "size_t", section: "3type", summary: "count of bytes" },
    { name: "printf", section: "3", summary: "formatted output" },
  ];
  assert.deepEqual(groupIndex(pages).map((g) => g.section), ["2", "3", "3type", "1"]);
  assert.deepEqual(groupIndex(pages)[0].pages.map((p) => p.name), ["read", "write"]);
  assert.deepEqual(groupIndex(pages, "file DESCRIPTOR").map((g) => g.pages.map((p) => p.name)), [["read", "write"]]);
  assert.deepEqual(groupIndex(pages, "nothing-matches"), []);
});

test("tags compare naturally", () => {
  assert.ok(compareTags("man-pages-6.10", "man-pages-6.9.1") > 0);
  assert.ok(compareTags("man-pages-6.9.1", "man-pages-6.9") > 0);
  assert.ok(compareTags("man-pages-2.00", "man-pages-5.13") < 0);
  assert.equal(newestTag(["man-pages-5.13", "man-pages-6.20", "man-pages-6.9.1", "man-pages-2.00"]), "man-pages-6.20");
  assert.equal(newestTag([]), "");
});

test("back-links open tabs of the linux repository", () => {
  assert.deepEqual(linuxApiTab({ repo: "linux", version: "v6.9", name: "read" }), {
    type: "api", title: "Kernel API · syscalls", view: "syscalls", arch: "x86_64", sel: "read", query: "", filter: "", repo: "linux", version: "v6.9",
  });
  const s = linuxSymbolTab({ repo: "linux", version: "v6.9", symbol: "ssize_t" });
  assert.equal(s.type, "search");
  assert.equal(s.query, "ssize_t");
  assert.equal(s.repo, "linux");
  assert.ok(s.word && s.caseSensitive && !s.regex);
  // A man tab's repository and tag are the tab's own repo / version
  const f = manTabFields({ repo: "man-pages", version: "man-pages-6.9", name: "read", section: "2", kernel: "v6.9", kernelRepo: "linux" });
  assert.equal(f.repo, "man-pages");
  assert.equal(f.version, "man-pages-6.9");
  assert.equal(f.manRepo, undefined);
  const p = new URLSearchParams("repo=linux&v=v6.9&tab=search");
  assert.equal(hashRepo(p), "linux");
  assert.equal(hashRepo(new URLSearchParams("repo=man-pages&v=x")), "man-pages");
  assert.equal(hashRepo(new URLSearchParams("repo=../../etc")), "");
  assert.equal(hashRepo(new URLSearchParams("v=v3.0")), "");
});

test("man route: hash round trip", () => {
  const route = tabRoute("man");
  assert.ok(route);
  assert.equal(route.badge, "[H]");
  const hash = "page=read&sec=2&mode=term&kv=v6.9&krepo=linux";
  const tab = route.fromHash(new URLSearchParams(hash));
  assert.deepEqual(tab, {
    title: "man read(2)", manPage: "read", manSec: "2", manMode: "term",
    kernelVersion: "v6.9", kernelRepo: "linux", manPath: "", manQuery: "",
  });
  const out = new URLSearchParams();
  route.toHash(tab, out);
  assert.equal(out.toString(), hash);
  // An address written with mrepo/mv names the man-pages repository and tag as the tab's own
  const legacy = route.fromHash(new URLSearchParams("page=read&sec=2&mv=man-pages-6.9&mrepo=man-pages"));
  assert.equal(legacy.repo, "man-pages");
  assert.equal(legacy.version, "man-pages-6.9");
  assert.equal(route.fromHash(new URLSearchParams("page=x&mrepo=../etc")).repo, undefined);
  // An unknown mode is dropped; a path-only link is titled from the path
  const byPath = route.fromHash(new URLSearchParams("path=man%2Fman7%2Fepoll.7&mode=bogus"));
  assert.equal(byPath.manMode, "");
  assert.equal(byPath.manPath, "man/man7/epoll.7");
  assert.equal(byPath.title, "man epoll");
  // The page and tag are navigation, the mode and the index filter are not
  const nav = route.navKey(tab);
  assert.equal(route.navKey({ ...tab, manMode: "web", manQuery: "x" }), nav);
  assert.notEqual(route.navKey({ ...tab, manPage: "write" }), nav);
  for (const k of ["manPage", "manSec", "manMode", "kernelVersion", "kernelRepo", "manPath"]) assert.ok(route.keys.includes(k), k);
  assert.ok(!route.keys.includes("manRepo") && !route.keys.includes("manVersion"));
});
