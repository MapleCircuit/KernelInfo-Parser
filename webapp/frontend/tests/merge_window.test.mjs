import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};

const { insightsSections } = await import("../js/views/insights/insights_view.js");
const { isGenericRef, mergeRowTag, repoNameFromUrl } = await import("../js/views/insights/release_sections.js");

const mwSection = () => insightsSections().find((s) => s.id === "merge-window");

test("generic refs fall back to the git repository name", () => {
  for (const ref of ["for-linus", "for_linus", "misc", "linux-next", "fixes-for-linus", "master", "main", "HEAD", "urgent", "for-v7.3-rc3", "2026-02-01-fixes"]) {
    assert.equal(isGenericRef(ref), true, ref);
  }
  for (const ref of ["edac_urgent_for_v7.3_rc3", "net-3.1", "kbuild-fixes", "akpm", "staging-linus"]) {
    assert.equal(isGenericRef(ref), false, ref);
  }
  assert.equal(repoNameFromUrl("git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6"), "kbuild-2.6");
  assert.equal(repoNameFromUrl("git://git.kernel.org/pub/scm/linux/kernel/git/davem/net-next.git/"), "net-next");
  assert.equal(repoNameFromUrl("git://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"), "torvalds/linux");
  assert.equal(mergeRowTag({
    ref: "for-linus",
    source_url: "git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
  }), "kbuild-2.6");
  assert.equal(mergeRowTag({
    ref: "edac_urgent_for_v7.3_rc3",
    source_url: "git://git.kernel.org/pub/scm/linux/kernel/git/ras/ras",
  }), "edac_urgent_for_v7.3_rc3");
});

test("merge-window rowHtml shows merge tag or git repo name and toggles sender via ctx.showSender", () => {
  const sec = mwSection();
  const edacPull = {
    day: 4.2,
    sender: "Borislav Petkov",
    ref: "edac_urgent_for_v7.3_rc3",
    subject: "Merge tag 'edac_urgent_for_v7.3_rc3'",
    section: { sec_id: 12, name: "EDAC" },
    commits: 3,
  };
  const hiddenSender = sec.rowHtml(edacPull, { showSender: false });
  assert.ok(!hiddenSender.includes("Borislav Petkov"));
  assert.ok(hiddenSender.includes("edac_urgent_for_v7.3_rc3"));
  assert.ok(hiddenSender.includes("EDAC"));

  const shownSender = sec.rowHtml(edacPull, { showSender: true });
  assert.ok(shownSender.includes("Borislav Petkov"));
  assert.ok(shownSender.includes("edac_urgent_for_v7.3_rc3"));
  assert.ok(shownSender.includes("EDAC"));

  const genericRef = sec.rowHtml({
    day: 6.0,
    sender: "Michal Marek",
    ref: "for-linus",
    source_url: "git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
    subject: "Merge branch 'for-linus' of git://git.kernel.org/pub/scm/linux/kernel/git/mmarek/kbuild-2.6",
    section: null,
    commits: 8,
  });
  assert.ok(!genericRef.includes("Michal Marek"));
  assert.ok(genericRef.includes("kbuild-2.6"));
  assert.ok(!genericRef.includes("for-linus<"));

  const withoutRef = sec.rowHtml({
    day: 5,
    sender: "Linus Torvalds",
    ref: "",
    subject: "Merge branch 'misc-fixes'",
    section: null,
    commits: 1,
  }, { showSender: true });
  assert.ok(withoutRef.includes("Linus Torvalds"));
  assert.ok(withoutRef.includes("Merge branch 'misc-fixes'"));
});

test("merge-window afterDetail highlights selected pull in sv-chart", async () => {
  const sec = mwSection();
  const pulls = [
    { merge: "a".repeat(40), parents: ["1".repeat(40), "2".repeat(40)], day: 2.5, after_rc1: false, commits: 10, sender: "A", ref: "a", subject: "Merge A", section: null },
    { merge: "b".repeat(40), parents: ["1".repeat(40), "3".repeat(40)], day: 18.4, after_rc1: true, commits: 25, sender: "B", ref: "b", subject: "Merge B", section: { sec_id: null, name: "EDAC" } },
  ];
  const fakeApi = {
    async getInsights() {
      return {
        release: "v7.3",
        prev: "v7.2",
        rc1: "v7.3-rc1",
        rc1_day: 14.0,
        pulls,
        totals: { pulls: 2, commits: 35, before_rc1: 1, by_day: [{ day: 2, commits: 10 }, { day: 18, commits: 25 }] },
      };
    },
  };
  const wrapEl = { innerHTML: "" };
  const fakeView = {
    repo: () => "linux",
    containerEl: { querySelector: (sel) => (sel === ".sv-chart-wrap" ? wrapEl : null) },
  };
  const listRes = await sec.fetchList({ release: "v7.3", api: fakeApi, view: fakeView });
  assert.ok(listRes.summaryHtml.includes('class="sv-chart-wrap"'));
  assert.ok(!listRes.summaryHtml.includes("sv-bar-active"));

  sec.afterDetail(null, pulls[1], { release: "v7.3", view: fakeView });
  assert.ok(wrapEl.innerHTML.includes('data-x="18" class="sv-bar-active"'));
  assert.ok(wrapEl.innerHTML.includes("d18.4"));

  const detailHtml = sec.detailHtml(pulls[1], { release: "v7.3", view: fakeView });
  assert.ok(detailHtml.includes("<dt>Subsystem</dt><dd>EDAC</dd>"));
  assert.ok(!detailHtml.includes("data-section"));
});
