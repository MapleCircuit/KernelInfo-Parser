import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ShortcutRegistry, chordFromEvent, findConflicts, formatChord, isTextTarget, mergeOverrides,
  normalizeChord, parseChord, scopeChain,
} from "../js/utils/shortcuts.js";

function memoryStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m };
}

test("chord parsing and normalization", () => {
  assert.equal(normalizeChord("mod+shift+p"), "Mod+Shift+P");
  assert.equal(normalizeChord("Shift+Mod+P"), "Mod+Shift+P");
  assert.equal(normalizeChord("?"), "?");
  assert.equal(normalizeChord("Shift+?"), "?");
  assert.equal(normalizeChord("f3"), "F3");
  assert.equal(normalizeChord("Shift+F12"), "Shift+F12");
  assert.equal(normalizeChord("Ctrl++"), "Ctrl++");
  assert.equal(normalizeChord("esc"), "Escape");
  assert.equal(normalizeChord("Space"), "Space");
  assert.equal(normalizeChord(""), "");
  assert.equal(normalizeChord("Mod+"), "");
  assert.equal(parseChord("Mod+Shift+P").key, "P");
});

test("events become chords per platform", () => {
  assert.equal(chordFromEvent({ key: "k", ctrlKey: true }, false), "Mod+K");
  assert.equal(chordFromEvent({ key: "k", metaKey: true }, true), "Mod+K");
  assert.equal(chordFromEvent({ key: "k", ctrlKey: true }, true), "Ctrl+K");
  assert.equal(chordFromEvent({ key: "P", ctrlKey: true, shiftKey: true }, false), "Mod+Shift+P");
  assert.equal(chordFromEvent({ key: "?", shiftKey: true }, false), "?");
  assert.equal(chordFromEvent({ key: "F12", shiftKey: true }, false), "Shift+F12");
  assert.equal(chordFromEvent({ key: "Shift", shiftKey: true }, false), "");
  assert.equal(chordFromEvent({ key: " " }, false), "Space");
});

test("labels per platform", () => {
  assert.equal(formatChord("Mod+Shift+P", false), "Ctrl+Shift+P");
  assert.equal(formatChord("Mod+Shift+P", true), "⌘⇧P");
  assert.equal(formatChord("ArrowUp", false), "↑");
});

test("scope resolution", () => {
  assert.deepEqual(scopeChain({ tabType: "code" }), ["code", "global"]);
  assert.deepEqual(scopeChain({ tabType: "code", modal: true }), ["modal", "global"]);
  assert.deepEqual(scopeChain({}), ["global"]);
  assert.equal(isTextTarget({ tagName: "INPUT", type: "text" }), true);
  assert.equal(isTextTarget({ tagName: "INPUT", type: "checkbox" }), false);
  assert.equal(isTextTarget({ tagName: "DIV", isContentEditable: true }), true);
  assert.equal(isTextTarget({ tagName: "DIV" }), false);
});

test("conflicts and overrides", () => {
  const defs = [
    { id: "a", keys: ["Mod+K"], scope: "global" },
    { id: "b", keys: ["Mod+J"], scope: "global" },
    { id: "c", keys: ["Mod+K"], scope: "code" },
    { id: "d", keys: ["Mod+K"], scope: "global", docOnly: true },
  ];
  assert.deepEqual(findConflicts(defs, mergeOverrides(defs)), []);
  const merged = mergeOverrides(defs, { b: ["mod+k"] });
  assert.deepEqual(merged.get("b"), ["Mod+K"]);
  assert.deepEqual(findConflicts(defs, merged), [{ scope: "global", chord: "Mod+K", ids: ["a", "b"] }]);
});

test("registry dispatch, narrow scope first, inputs skipped", () => {
  const reg = new ShortcutRegistry({ storage: memoryStorage(), mac: false, eventTarget: { dispatchEvent() {} } });
  const calls = [];
  reg.register({ id: "g", keys: ["Mod+K"], scope: "global", handler: () => calls.push("g") });
  reg.register({ id: "c", keys: ["Mod+K"], scope: "code", handler: () => calls.push("c") });
  reg.register({ id: "w", keys: ["F3"], scope: "code", when: (ctx) => ctx.ok, handler: () => calls.push("w") });
  reg.register({ id: "doc", keys: ["F4"], scope: "global", docOnly: true });
  reg.setContextProvider(() => ({ tabType: "code", ok: false }));
  const ev = (key, extra = {}) => ({ key, ctrlKey: false, preventDefault() { this.prevented = true; }, stopPropagation() {}, target: { tagName: "DIV" }, ...extra });
  const e1 = ev("k", { ctrlKey: true });
  assert.equal(reg.dispatch(e1), true);
  assert.equal(e1.prevented, true);
  assert.deepEqual(calls, ["c"]);
  reg.setContextProvider(() => ({ tabType: "kconfig" }));
  reg.dispatch(ev("k", { ctrlKey: true }));
  assert.deepEqual(calls, ["c", "g"]);
  assert.equal(reg.dispatch(ev("F3")), false);
  assert.equal(reg.dispatch(ev("F4")), false);
  assert.equal(reg.dispatch(ev("k", { ctrlKey: true, target: { tagName: "INPUT", type: "text" } })), false);
});

test("rebinding persists, refuses conflicts, resets", () => {
  const storage = memoryStorage();
  const events = [];
  const reg = new ShortcutRegistry({ storage, mac: false, eventTarget: { dispatchEvent: (e) => events.push(e.type) } });
  reg.register({ id: "palette.open", keys: ["Mod+K", "Mod+P"], scope: "global", handler: () => {} });
  reg.register({ id: "other", keys: ["Mod+J"], scope: "global", handler: () => {} });
  const refused = reg.setOverride("palette.open", ["Mod+J"]);
  assert.equal(refused.ok, false);
  assert.equal(refused.conflicts[0].chord, "Mod+J");
  assert.equal(reg.setOverride("other", ["Mod+L"]).ok, true);
  assert.equal(reg.setOverride("palette.open", ["Mod+J"]).ok, true);
  assert.deepEqual(reg.keysFor("palette.open"), ["Mod+J"]);
  assert.equal(reg.label("palette.open"), "Ctrl+J");
  const reloaded = new ShortcutRegistry({ storage, mac: false, eventTarget: { dispatchEvent() {} } });
  reloaded.register({ id: "palette.open", keys: ["Mod+K", "Mod+P"], scope: "global", handler: () => {} });
  assert.deepEqual(reloaded.keysFor("palette.open"), ["Mod+J"]);
  reg.resetAll();
  assert.deepEqual(reg.keysFor("palette.open"), ["Mod+K", "Mod+P"]);
  assert.equal(storage.getItem("kip-shortcuts"), null);
  assert.ok(events.length >= 3);
  assert.equal(reg.list().find((d) => d.id === "palette.open").customized, false);
});

test("overlay grouping, search and chord capture", async () => {
  const { groupShortcuts, captureStep } = await import("../js/utils/shortcuts.js");
  const list = [
    { id: "palette.open", scope: "global", description: "Search files", keys: ["Mod+K"], docOnly: false },
    { id: "kconfig.undo", scope: "kconfig", description: "Undo", keys: ["Mod+Z"], docOnly: false },
    { id: "tui.move", scope: "kconfig-tui", description: "Move", keys: ["ArrowUp"], docOnly: true },
    { id: "x.custom", scope: "weird", description: "Other", keys: [], docOnly: false },
  ];
  const groups = groupShortcuts(list, "", false);
  assert.deepEqual(groups.map((g) => g.title), ["Global", "KConfig", "KConfig TUI", "weird"]);
  assert.deepEqual(groups[0].entries[0].labels, ["Ctrl+K"]);
  assert.deepEqual(groupShortcuts(list, "undo").map((g) => g.scope), ["kconfig"]);
  assert.deepEqual(groupShortcuts(list, "ctrl+k").map((g) => g.scope), ["global"]);
  assert.deepEqual(groupShortcuts(list, "⌘K", true).map((g) => g.scope), ["global"]);
  assert.deepEqual(captureStep({ key: "Control", ctrlKey: true }), { state: "waiting" });
  assert.deepEqual(captureStep({ key: "Escape" }), { state: "cancelled" });
  assert.deepEqual(captureStep({ key: "j", ctrlKey: true }, false), { state: "captured", chord: "Mod+J" });
});

test("suspended registry dispatches nothing", () => {
  const reg = new ShortcutRegistry({ storage: memoryStorage(), mac: false, eventTarget: { dispatchEvent() {} } });
  let n = 0;
  reg.register({ id: "a", keys: ["Mod+K"], scope: "global", handler: () => n++ });
  const ev = { key: "k", ctrlKey: true, preventDefault() {}, stopPropagation() {}, target: { tagName: "DIV" } };
  reg.suspend(true);
  assert.equal(reg.dispatch(ev), false);
  reg.suspend(false);
  assert.equal(reg.dispatch(ev), true);
  assert.equal(n, 1);
});
