import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MAN_STATUSES,
  filterManEntries,
  manDiffBadges,
  manDiffMarkdown,
  manDiffSections,
  manDiffTsv,
} from "../js/views/diff/man_diff.js";

const ENTRIES = [
  {
    name: "newcall",
    section: "2",
    status: "added",
    path: "man/man2/newcall.2",
    path_a: null,
    path_b: "man/man2/newcall.2",
    summary: "new system call",
    so: null,
  },
  {
    name: "read",
    section: "2",
    status: "modified",
    path: "man/man2/read.2",
    path_a: "man2/read.2",
    path_b: "man/man2/read.2",
    summary: "read bytes from fd",
    summary_a: "read from fd",
    summary_b: "read bytes from fd",
    summary_changed: true,
    so_changed: false,
    fn_added: ["read2"],
    fn_removed: [],
    aliases_added: ["read2"],
    aliases_removed: [],
  },
  {
    name: "pwrite",
    section: "2",
    status: "modified",
    path: "man/man2/pwrite.2",
    path_a: "man/man2/pwrite.2",
    path_b: "man/man2/pwrite.2",
    summary: "read or write at offset",
    so: null,
    so_a: "man/man2/pread.2",
    so_b: null,
    so_changed: true,
    summary_changed: false,
    fn_added: [],
    fn_removed: [],
    aliases_added: [],
    aliases_removed: [],
  },
  {
    name: "old_fmt",
    section: "5",
    status: "deleted",
    path: "man5/old_fmt.5",
    path_a: "man5/old_fmt.5",
    path_b: null,
    summary: "obsolete format",
    so: "man5/new_fmt.5",
  },
];

test("manDiffSections sorts by sectionRank and counts entries", () => {
  assert.deepEqual(manDiffSections(ENTRIES), [
    { section: "2", title: "System calls", count: 3 },
    { section: "5", title: "File formats and configuration files", count: 1 },
  ]);
});

test("filterManEntries filters by statuses, section and multi-word query", () => {
  assert.equal(MAN_STATUSES.length, 3);
  assert.equal(filterManEntries(ENTRIES, new Set(), "", "").length, 4);
  assert.equal(filterManEntries(ENTRIES, new Set(["added", "deleted"]), "", "").length, 2);
  assert.equal(filterManEntries(ENTRIES, new Set(), "5", "").length, 1);
  assert.equal(filterManEntries(ENTRIES, new Set(), "", "read2").length, 1);
  assert.equal(filterManEntries(ENTRIES, new Set(), "", "read(2) bytes").length, 1);
  assert.equal(filterManEntries(ENTRIES, new Set(), "", "obsolete format").length, 1);
});

test("manDiffBadges formats function, alias, so, summary and move badges", () => {
  assert.deepEqual(
    manDiffBadges(ENTRIES[1]).map((b) => [b.kind, b.text]),
    [["fn", "+1 fn"], ["alias", "+1 alias"], ["summary", "summary"], ["moved", "man2/read.2 →"]],
  );
  assert.deepEqual(
    manDiffBadges(ENTRIES[2]).map((b) => [b.kind, b.text]),
    [["so", "alias → page"]],
  );
  assert.deepEqual(
    manDiffBadges(ENTRIES[3]).map((b) => [b.kind, b.text]),
    [["so", "→ man5/new_fmt.5"]],
  );
});

test("manDiffMarkdown and manDiffTsv render release notes and TSV table", () => {
  const res = {
    a: "man-pages-6.9",
    b: "man-pages-6.10",
    summary: { added: 1, modified: 2, deleted: 1, unchanged: 10 },
  };
  const md = manDiffMarkdown(res, ENTRIES);
  assert.ok(md.startsWith("# Man pages changes man-pages-6.9 → man-pages-6.10"));
  assert.ok(md.includes("## Added (1)"));
  assert.ok(md.includes("- `newcall(2)` — new system call"));
  assert.ok(md.includes("## Modified (2)"));
  assert.ok(md.includes("## Deleted (1)"));

  const tsv = manDiffTsv(ENTRIES).trim().split("\n");
  assert.equal(tsv[0], "status\tname\tsection\tpath\told_path\tsummary\tchanges");
  assert.equal(tsv[1], "A\tnewcall\t2\tman/man2/newcall.2\t\tnew system call\t");
  assert.equal(tsv[2].split("\t")[4], "man2/read.2");
});
