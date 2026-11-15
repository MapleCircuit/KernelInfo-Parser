/**
 * version_folding.test.mjs - Sub-version folding in #version-select and #rc-select across all branches
 * (webapp/API_FRONTEND.md §5.12).
 */
import { FakeElement } from "./helpers/dom_shim.mjs";
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

globalThis.__KIP_SKIP_AUTOSTART__ = true;

const { DEFAULT_TAG_VIEWS, KernelIdeApp } = await import("../js/app.js");
const { state } = await import("../js/state.js");

class SelectFake extends FakeElement {
  constructor(tag = "select") {
    super(tag);
    this._value = "";
  }
  get options() {
    const opts = [];
    for (const child of this.children) {
      if (child.tagName === "OPTGROUP") opts.push(...child.children);
      else opts.push(child);
    }
    return opts;
  }
  get value() {
    const sel = this.options.find((o) => o.selected);
    return sel ? sel.value : this._value;
  }
  set value(v) {
    this._value = v;
    for (const o of this.options) o.selected = o.value === v;
  }
  set innerHTML(val) {
    this._innerHTML = val;
    if (val === "") this.children = [];
  }
  get innerHTML() {
    return this._innerHTML || "";
  }
}

function withVersionSelectDom(fn) {
  const prevCreate = document.createElement;
  const prevGetById = document.getElementById;
  const els = {
    "version-select": new SelectFake("select"),
    "rc-select": new SelectFake("select"),
    "rc-select-label": new FakeElement("label"),
    "branch-picker-btn": new FakeElement("button"),
    "branch-picker-label": new FakeElement("span"),
    "commit-picker-label": new FakeElement("span"),
    "commit-picker-btn": new FakeElement("button"),
    "version-apply-btn": new FakeElement("button"),
    "version-newtab-btn": new FakeElement("button"),
  };
  document.createElement = (tag) => (tag === "select" ? new SelectFake(tag) : new FakeElement(tag));
  document.getElementById = (id) => els[id] || null;
  try {
    return fn(els);
  } finally {
    document.createElement = prevCreate;
    document.getElementById = prevGetById;
  }
}

const SAMPLE_VERSIONS = [
  "0.96a", "0.96a-patch1", "0.96pre",
  "0.99.14", "0.99.14a",
  "1.0", "1.0alpha", "1.0pre1", "1.0.1",
  "pre2.0.1", "pre2.0.2",
  "2.2.0", "2.2.0pre1",
  "2.4.0", "2.4.0-test1", "2.4.9.14", "2.4.9.15",
  "lia64-v2.5.10",
  "v2.6.0", "v2.6.0-test1", "v2.6.0-test11",
  "v2.6.9", "v2.6.9-final", "v2.6.9-rc1",
  "v2.6.16", "v2.6.16.28-rc1", "v2.6.16.62",
  "v3.0", "v3.0.1", "v3.0.101", "v3.0-rc7",
  "v6.6", "v6.6.1", "v6.6.100", "v6.6-rc1",
  "v7.4-rc1", "v7.4-rc2",
  "next-20261109", "next-20261110",
  "assoc-array-fixes", "assoc-array-fixes-v2",
];

test("sub-versions fold under their main version across all branches", () => {
  const app = new KernelIdeApp();
  app.tagViews = structuredClone(DEFAULT_TAG_VIEWS);
  const branches = ["", "master", "stable/linux-6.6.y", "history/2.4-stable", "linux-next/master"];

  for (const branch of branches) {
    state.selectedBranch = branch;
    state.branchTags = null;
    state.branchTagsFor = "";
    app.buildVersionCatalog(SAMPLE_VERSIONS);

    const bases = app.tagVersions.map((t) => t.vname);
    assert.deepEqual(
      bases,
      ["0.96", "0.99", "1.0", "pre2.0", "2.2", "2.4", "lia64-v2.5", "v2.6.0", "v2.6.9", "v2.6.16", "v3.0", "v6.6", "v7.4", "next", "assoc-array-fixes"],
      `unexpected unfolded bases for branch "${branch}"`
    );

    const byBase = new Map(app.tagVersions.map((t) => [t.vname, t]));
    assert.equal(byBase.get("v7.4")?.rcOnly, true);
    assert.equal(byBase.get("2.4")?.rcOnly, false);
    assert.equal(byBase.get("pre2.0")?.rcOnly, false);
    assert.equal(byBase.get("lia64-v2.5")?.rcOnly, false);
    assert.equal(byBase.get("next")?.rcOnly, false);
    assert.equal(byBase.get("0.96")?.rcOnly, false);
  }
});

test("legacy unmigrated DEFAULT_TAG_VIEWS still folds historical and next tags on any branch", () => {
  const app = new KernelIdeApp();
  app.tagViews = [
    {
      name: "— any branch —",
      regex_rules: [
        "^(v2\\.6\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$",
        "^(v(?!2\\.6\\.)\\d+\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$",
      ],
    },
  ];
  state.selectedBranch = "history/2.4-stable";
  app.buildVersionCatalog(SAMPLE_VERSIONS);
  const bases = app.tagVersions.map((t) => t.vname);
  assert.ok(bases.includes("2.4") && !bases.includes("2.4.9.15"));
  assert.ok(bases.includes("v2.6.0") && !bases.includes("v2.6.0-test1"));
  assert.ok(bases.includes("next") && !bases.includes("next-20261110"));
});

test("naturalVersionCompare and Release dropdown ordering handle pre-releases and point releases", () => {
  assert.ok(KernelIdeApp.naturalVersionCompare("2.2.0", "2.2.0pre1") > 0);
  assert.ok(KernelIdeApp.naturalVersionCompare("v3.0.101", "v3.0.2") > 0);
  assert.ok(KernelIdeApp.naturalVersionCompare("0.99.14a", "0.99.14") > 0);

  const app = new KernelIdeApp();
  app.tagViews = structuredClone(DEFAULT_TAG_VIEWS);
  state.selectedBranch = "stable/linux-6.6.y";
  state.branchTags = new Set(["v6.6", "v6.6-rc1", "v6.6.1", "v6.6.100", "v3.0", "v3.0.1", "v2.6.16", "v2.6.16.62"]);
  state.branchTagsFor = "stable/linux-6.6.y";
  state.selectedVersion = "v6.6.100";
  app.buildVersionCatalog(SAMPLE_VERSIONS);

  withVersionSelectDom((els) => {
    app.updateVersionSelectorDisplay();
    const versionOpts = els["version-select"].options.map((o) => o.value);
    assert.deepEqual(versionOpts, ["v2.6.16", "v3.0", "v6.6"]);
    assert.equal(els["version-select"].value, "v6.6");

    const rcOpts = els["rc-select"].options.map((o) => ({ value: o.value, text: o.textContent }));
    assert.deepEqual(rcOpts, [
      { value: "v6.6.100", text: ".100" },
      { value: "v6.6.1", text: ".1" },
      { value: "v6.6", text: "final" },
      { value: "v6.6-rc1", text: "rc1" },
    ]);
    assert.equal(els["rc-select"].hidden, false);
    assert.equal(els["rc-select"].value, "v6.6.100");
  });

  // Scoped branch with non-v tags (history/2.4-stable) folds 2.4.x under 2.4
  state.selectedBranch = "history/2.4-stable";
  state.branchTags = new Set(["2.4.0", "2.4.0-test1", "2.4.9.14", "2.4.9.15"]);
  state.branchTagsFor = "history/2.4-stable";
  state.selectedVersion = "2.4.9.15";
  app.buildVersionCatalog(SAMPLE_VERSIONS);

  withVersionSelectDom((els) => {
    app.updateVersionSelectorDisplay();
    const versionOpts = els["version-select"].options.map((o) => ({ value: o.value, text: o.textContent }));
    assert.deepEqual(versionOpts, [{ value: "2.4", text: "2.4" }]);
    const rcOpts = els["rc-select"].options.map((o) => ({ value: o.value, text: o.textContent }));
    assert.deepEqual(rcOpts, [
      { value: "2.4.9.15", text: ".9.15" },
      { value: "2.4.9.14", text: ".9.14" },
      { value: "2.4.0", text: ".0" },
      { value: "2.4.0-test1", text: ".0-test1" },
    ]);
  });
});
