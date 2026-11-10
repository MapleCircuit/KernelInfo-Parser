import { test } from "node:test";
import assert from "node:assert/strict";
import {
  centeredScrollTop, codeHash, externalLink, fenceLanguage, formatLineSpec, markdownSnippet, parseLineSpec, pinnedSha,
} from "../js/views/code_view/permalink.js";

test("line specs round trip, reversed ranges normalized", () => {
  assert.deepEqual(parseLineSpec("12"), { start: 12, end: 12 });
  assert.deepEqual(parseLineSpec("12-30"), { start: 12, end: 30 });
  assert.deepEqual(parseLineSpec("30-12"), { start: 12, end: 30 });
  assert.equal(parseLineSpec("0"), null);
  assert.equal(parseLineSpec("a-b"), null);
  assert.equal(formatLineSpec(12), "12");
  assert.equal(formatLineSpec(12, 30), "12-30");
  assert.equal(formatLineSpec(30, 12), "12-30");
});

test("permalinks pin the commit", () => {
  assert.equal(pinnedSha("v3.0", "02f8c6aee8df.1700000000"), "02f8c6aee8df");
  assert.equal(pinnedSha("02f8c6aee8df3cdc935e9bdd4f2d020306035dbe", ""), "02f8c6aee8df3cdc935e9bdd4f2d020306035dbe");
  assert.equal(pinnedSha("master", "abcdef012345.git"), "abcdef012345");
  assert.equal(pinnedSha("v3.0", undefined), "");
  assert.equal(codeHash({ rev: "02f8c6aee8df", path: "init/main.c", start: 490, end: 500 }),
    "#v=02f8c6aee8df&tab=code&file=init%2Fmain.c&line=490-500");
  assert.equal(codeHash({ rev: "v3.0", path: "a.c", start: 1, end: 1 }), "#v=v3.0&tab=code&file=a.c");
  assert.equal(codeHash({ rev: "v3.0", path: "a.c", start: 5, diffWith: { version: "v2.6.39", path: "b.c", swap: true }, diffLayout: "split" }),
    "#v=v3.0&tab=code&file=a.c&line=5&dv=v2.6.39&dp=b.c&ds=1&dm=split");
});

test("markdown snippets", () => {
  const md = markdownSnippet({ path: "init/main.c", start: 2, end: 3, revLabel: "v3.0", sha: "02f8c6aee8df", link: "http://x/#y",
    lines: ["int a;", "int b;"], lang: fenceLanguage("c") });
  assert.equal(md, "[`init/main.c:2-3` @ v3.0 (02f8c6aee8df)](http://x/#y)\n```c\nint a;\nint b;\n```\n");
  const tricky = markdownSnippet({ path: "a`b.md", start: 1, end: 1, revLabel: "v1", sha: "", link: "l", lines: ["```", "x"], lang: "" });
  assert.ok(tricky.startsWith("[`a\\`b.md:1` @ v1](l)\n````\n"));
  assert.ok(tricky.endsWith("\n````\n"));
  assert.equal(fenceLanguage("makefile"), "make");
  assert.equal(fenceLanguage("unknown"), "");
});

test("external viewers", () => {
  const sha = "02f8c6aee8df3cdc935e9bdd4f2d020306035dbe";
  assert.equal(externalLink("https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git", "init/main.c", sha, 490, 500),
    `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/init/main.c?id=${sha}#n490`);
  assert.equal(externalLink("https://github.com/torvalds/linux", "init/main.c", sha, 490, 500),
    `https://github.com/torvalds/linux/blob/${sha}/init/main.c#L490-L500`);
  assert.equal(externalLink("git@github.com:torvalds/linux.git", "a b.c", sha, 3),
    `https://github.com/torvalds/linux/blob/${sha}/a%20b.c#L3`);
  assert.equal(externalLink("https://example.org/linux.git", "a.c", sha, 1), "");
  assert.equal(externalLink("", "a.c", sha, 1), "");
});

test("range centering", () => {
  assert.equal(centeredScrollTop(1, 1, 20, 600), 0);
  // middle of lines 490-500 (line 495: 494 rows above, half a row) minus half the viewport
  assert.equal(centeredScrollTop(490, 500, 20, 600), 494 * 20 + 10 - 300);
});
