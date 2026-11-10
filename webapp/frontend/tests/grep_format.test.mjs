import { test } from "node:test";
import assert from "node:assert/strict";
import { grepParams, grepSummary, grepToText, groupByFile, highlightRanges, parsePathFilters } from "../js/utils/grep_format.js";
import { tabRoute } from "../js/tab_routes.js";
import "../js/routes.js";

test("ranges are applied to the raw text, each segment escaped", () => {
  assert.equal(highlightRanges("a<b>c", [[1, 4]]), 'a<mark class="grep-hit">&lt;b&gt;</mark>c');
  assert.equal(highlightRanges("x & y & z", [[2, 3], [6, 7]]), 'x <mark class="grep-hit">&amp;</mark> y <mark class="grep-hit">&amp;</mark> z');
  assert.equal(highlightRanges("abc", [[5, 9], [2, 1]]), "abc");
  assert.equal(highlightRanges("abcdef", [[0, 3], [2, 5]]), '<mark class="grep-hit">abc</mark>def');
  assert.equal(highlightRanges('"q"', []), "&quot;q&quot;");
});

test("grouping, export and summaries", () => {
  const res = { total: 3, files: 2, matches: [
    { path: "a.c", line: 1, text: "x" }, { path: "a.c", line: 9, text: "y" }, { path: "b.c", line: 2, text: "z" },
  ] };
  assert.deepEqual(groupByFile(res.matches).map((g) => [g.path, g.matches.length]), [["a.c", 2], ["b.c", 1]]);
  assert.equal(grepToText(res), "a.c:1:x\na.c:9:y\nb.c:2:z\n");
  assert.equal(grepSummary(res), "3 matches in 2 files");
  assert.match(grepSummary({ ...res, truncated: true }), /truncated/);
  assert.match(grepSummary({ ...res, timed_out: true }), /partial/);
});

test("request parameters", () => {
  assert.equal(grepParams({ q: "a b", regex: true, caseSensitive: true, word: true, paths: ["fs/", "!*.h"], limit: 500 }).toString(),
    "q=a+b&mode=regex&case=1&word=1&path=fs%2F&path=%21*.h&limit=500");
  assert.equal(grepParams({ q: "x" }).toString(), "q=x");
  assert.deepEqual(parsePathFilters(" drivers/net/, *.c  !*.h"), ["drivers/net/", "*.c", "!*.h"]);
});

test("search tab hash round trip", async () => {
  const route = tabRoute("search");
  assert.ok(route, "search route registered");
  const tab = { query: "rq->lock", regex: true, caseSensitive: false, word: true, paths: ["kernel/", "!*.h"], limit: 0 };
  const params = new URLSearchParams();
  route.toHash(tab, params);
  assert.equal(params.toString(), "q=rq-%3Elock&re=1&w=1&p=kernel%2F&p=%21*.h");
  const back = route.fromHash(params);
  assert.equal(back.query, "rq->lock");
  assert.equal(back.regex, true);
  assert.equal(back.caseSensitive, false);
  assert.deepEqual(back.paths, ["kernel/", "!*.h"]);
  assert.notEqual(route.navKey(tab), route.navKey({ ...tab, query: "other" }));
});
