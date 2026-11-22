/**
 * nodemap_pickers.test.mjs - NodeMap field pickers (`webapp/frontend/js/views/nodemap/nodemap_pickers.js`):
 * suggestion routing per mode, stale-response dropping and keyboard selection of the autocomplete,
 * File Explorer directory / search browsing and initial directory, and the revision popover options.
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => {
  const t = realSetTimeout(...args);
  t?.unref?.();
  return t;
};
const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (...args) => {
  const t = realSetInterval(...args);
  t?.unref?.();
  return t;
};

const P = await import("../js/views/nodemap/nodemap_pickers.js");

function client() {
  const calls = [];
  return {
    calls,
    async searchSymbols(v, q, limit) {
      calls.push(["symbols", v, q, limit]);
      return {
        symbols: [
          { name: `${q}_fn`, type_name: "C_function", file_path: "kernel/fork.c", line_s: 10 },
          { name: `${q}_struct`, type_name: "C_struct", file_path: "include/linux/sched.h", line_s: 700 },
          { name: `${q}_union`, type_name: "C_union", file_path: "include/linux/u.h", line_s: 3 },
        ],
      };
    },
    async searchKconfig(v, q, limit, arch) {
      calls.push(["kconfig", v, q, limit, arch]);
      return { symbols: [{ name: "CONFIG_SMP", type: "bool", prompt: "Symmetric multi-processing", file_path: "arch/x86/Kconfig", line_s: 5 }] };
    },
    async getDirectoryTree(v, path) {
      calls.push(["tree", v, path]);
      if (path === "missing") throw new Error("Path not found");
      const entries = path === ""
        ? [{ name: "include", type: "dir", path: "include" }, { name: "Makefile", type: "file", path: "Makefile" }]
        : [{ name: "sched.h", type: "file", path: `${path}/sched.h` }];
      return { version: v, path, entries };
    },
    async searchFiles(v, q, limit) {
      calls.push(["files", v, q, limit]);
      return { files: [{ path: "include/linux/sched.h", name: "sched.h", score: 1 }] };
    },
  };
}

test("suggestions route by mode: symbols, struct/union first, Kconfig with ARCH", async () => {
  const c = client();
  const sym = await P.fetchSuggestions("symbol", c, "v3.0", "task");
  assert.equal(sym.length, 3);
  assert.deepEqual(sym[0], { name: "task_fn", kind: "C_function", path: "kernel/fork.c", line: 10, detail: "" });
  const st = await P.fetchSuggestions("struct", c, "v3.0", "task");
  assert.deepEqual(st.map((r) => r.name), ["task_struct", "task_union"], "struct mode keeps struct / union definitions");
  assert.equal(P.structKindOf(st[1].kind), "union");
  const kc = await P.fetchSuggestions("kconfig", c, "v6.6", "SMP", { arch: "arm64" });
  assert.deepEqual(kc, [{ name: "SMP", kind: "bool", path: "arch/x86/Kconfig", line: 5, detail: "Symmetric multi-processing" }]);
  assert.deepEqual(c.calls.at(-1), ["kconfig", "v6.6", "SMP", 25, "arm64"]);
  assert.deepEqual(await P.fetchSuggestions("symbol", c, "v3.0", "  "), [], "empty query: no request");

  // Unparsed revision: no struct in the index, the other definitions stay (free text still works)
  const fnOnly = { searchSymbols: async () => ({ symbols: [{ name: "foo", type_name: "C_function", file_path: "a.c" }] }) };
  assert.equal((await P.fetchSuggestions("struct", fnOnly, "v9", "foo")).length, 1);
  assert.deepEqual(await P.fetchSuggestions("symbol", { searchSymbols: async () => ({ symbols: [] }) }, "v9", "foo"), []);
});

test("autocomplete drops out-of-order responses and moves with the arrow keys", async () => {
  const pending = [];
  const m = new P.AutocompleteModel((q) => new Promise((resolve) => pending.push({ q, resolve })));
  const first = m.run("ta");
  const second = m.run("task");
  pending[1].resolve([{ name: "task_struct" }, { name: "task_work" }]);
  assert.deepEqual((await second).map((r) => r.name), ["task_struct", "task_work"]);
  pending[0].resolve([{ name: "tasklet" }]);
  assert.equal(await first, null, "the older request resolved last: dropped");
  assert.equal(m.items.length, 2, "its rows never replace the newer ones");
  assert.equal(m.current().name, "task_struct", "the first row is highlighted");
  m.move(1);
  assert.equal(m.current().name, "task_work");
  m.move(1);
  assert.equal(m.current().name, "task_struct", "ArrowDown wraps");
  m.move(-1);
  assert.equal(m.current().name, "task_work", "ArrowUp wraps back");
  m.cancel();
  assert.equal(m.current(), null);
  const failing = new P.AutocompleteModel(async () => { throw new Error("404"); });
  assert.deepEqual(await failing.run("x"), [], "a failed lookup shows no rows");
});

test("autocomplete keyboard: arrows move, Enter picks without submitting, Escape closes the list only", async () => {
  const { FakeElement } = await import("./helpers/dom_shim.mjs");
  const tick = () => new Promise((r) => realSetTimeout(r, 5));
  const input = new FakeElement("input");
  input.isConnected = true;
  input.value = "";
  const picked = [];
  const queries = [];
  const m = P.attachAutocomplete(input, {
    delay: 0,
    fetch: async (q) => { queries.push(q); return [{ name: "schedule", kind: "C_function" }, { name: "schedule_timeout", kind: "C_function" }]; },
    onPick: (r) => picked.push(r.name),
  });
  assert.equal(P.attachAutocomplete(input, {}), m, "attaching twice keeps one dropdown");
  const key = (k) => {
    const ev = { type: "keydown", key: k, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
    input.dispatchEvent(ev);
    return ev;
  };
  assert.equal(key("Enter").prevented, false, "no list: Enter submits the form as usual");
  input.value = "sched";
  input.dispatchEvent({ type: "input" });
  await tick();
  assert.deepEqual(queries, ["sched"]);
  assert.equal(key("ArrowDown").prevented, true);
  const enter = key("Enter");
  assert.equal(enter.prevented, true, "Enter picks instead of submitting");
  assert.equal(enter.stopped, true);
  assert.deepEqual(picked, ["schedule_timeout"]);
  assert.equal(input.value, "schedule_timeout");
  assert.equal(key("Enter").prevented, false, "the list closed after the pick");

  input.dispatchEvent({ type: "input" });
  await tick();
  const esc = key("Escape");
  assert.equal(esc.stopped, true, "Escape does not reach the dialog");
  assert.equal(key("Enter").prevented, false, "Escape closed the list");
  assert.deepEqual(picked, ["schedule_timeout"]);
});

test("file explorer: initial directory, directory browsing, search mode, stale responses", async () => {
  assert.equal(P.initialDirOf("include/linux/sched.h"), "include/linux");
  assert.equal(P.initialDirOf("Makefile"), "");
  assert.equal(P.initialDirOf("drivers/"), "drivers");
  assert.equal(P.initialDirOf(""), "");

  const c = client();
  const fx = new P.FileExplorerModel({ client: c, version: "v3.0", dir: "include/linux" });
  assert.deepEqual(fx.crumbs(), [{ label: "root", dir: "" }, { label: "include", dir: "include" }, { label: "linux", dir: "include/linux" }]);
  assert.equal(fx.parentDir(), "include");
  await fx.open("");
  assert.deepEqual(fx.rows.map((r) => [r.type, r.path]), [["dir", "include"], ["file", "Makefile"]]);
  await fx.open("include");
  assert.deepEqual(fx.rows.map((r) => r.path), ["include/sched.h"]);
  await fx.search("sched");
  assert.equal(fx.filter, "sched");
  assert.deepEqual(fx.rows, [{ type: "file", name: "sched.h", path: "include/linux/sched.h" }]);
  assert.deepEqual(c.calls.at(-1), ["files", "v3.0", "sched", 100]);
  await fx.search("");
  assert.equal(fx.filter, "", "clearing the filter goes back to the directory");
  assert.deepEqual(c.calls.at(-1), ["tree", "v3.0", "include"]);
  await fx.open("missing");
  assert.match(fx.error, /not found/);

  // A slow listing that a newer one overtook is dropped
  let release;
  const slow = new P.FileExplorerModel({ client: { getDirectoryTree: (v, p) => (p === "a" ? new Promise((r) => { release = r; }) : Promise.resolve({ entries: [{ name: "b.c", type: "file", path: "b/b.c" }] })) }, version: "v3.0" });
  const a = slow.open("a");
  await slow.open("b");
  release({ entries: [{ name: "a.c", type: "file", path: "a/a.c" }] });
  assert.equal(await a, false);
  assert.deepEqual(slow.rows.map((r) => r.path), ["b/b.c"]);
});

test("file explorer modal: scoped to the revision, opens in the parent directory, a file pick closes it", async () => {
  const c = client();
  let made = null;
  const openModalFn = (cfg) => {
    made = { cfg, closed: false, body: { innerHTML: "", querySelector: () => null }, footer: { querySelector: () => null }, close() { this.closed = true; } };
    return made;
  };
  const picked = [];
  const fx = P.openFileExplorer({ client: c, version: "v6.6", currentPath: "include/linux/sched.h", openModalFn, onSelect: (p) => picked.push(p) });
  await fx.ready;
  assert.match(made.cfg.subtitle, /v6\.6/);
  assert.deepEqual(c.calls[0], ["tree", "v6.6", "include/linux"], "starts in the input's directory");
  fx.activate({ type: "up", path: "include" });
  await new Promise((r) => realSetTimeout(r, 0));
  assert.equal(fx.model.dir, "include", ".. goes up");
  fx.activate({ type: "dir", path: "include/linux" });
  await new Promise((r) => realSetTimeout(r, 0));
  assert.equal(fx.model.dir, "include/linux");
  fx.activate({ type: "file", path: "include/linux/sched.h" });
  assert.deepEqual(picked, ["include/linux/sched.h"]);
  assert.equal(made.closed, true);

  // A remembered directory that is gone falls back to the root
  const fx2 = P.openFileExplorer({ client: c, version: "v6.6", currentPath: "missing/x.c", openModalFn, onSelect() {} });
  await fx2.ready;
  assert.equal(fx2.model.dir, "");
  assert.equal(fx2.model.error, "");
});

test("revision popover: unscoped refPicker, current ref, picked ref forwarded", () => {
  const calls = [];
  const picker = { open: (anchor, opts) => calls.push({ anchor, opts }) };
  const got = [];
  const anchor = {};
  const cl = { getVersionsCached() {}, getCommits() {} };
  P.openVersionPicker(anchor, { current: " v3.0 ", client: cl, picker, onSelect: (r) => got.push(r) });
  assert.equal(calls[0].anchor, anchor);
  assert.equal(calls[0].opts.current, "v3.0");
  assert.equal(calls[0].opts.scoped, false);
  assert.equal(calls[0].opts.client, cl);
  calls[0].opts.onSelect("v6.6");
  calls[0].opts.onSelect("");
  assert.deepEqual(got, ["v6.6"], "an empty pick is ignored");
  assert.match(P.pickButtonHtml("file", "d-path", { insp: true }), /data-insp="pick" data-pick="file" data-for="d-path"/);
  assert.equal(P.pickButtonHtml("nope", "x"), "");
});
