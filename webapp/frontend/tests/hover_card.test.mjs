import { test } from "node:test";
import assert from "node:assert/strict";
import { HoverIntent, hoverSettings, kindLabel } from "../js/views/code_view/hover_model.js";

function clock() {
  let now = 0;
  const timers = new Map();
  let id = 0;
  return {
    setTimer: (fn, ms) => { timers.set(++id, { fn, at: now + ms }); return id; },
    clearTimer: (t) => timers.delete(t),
    advance(ms) {
      now += ms;
      for (const [k, t] of [...timers].sort((a, b) => a[1].at - b[1].at)) if (t.at <= now && timers.has(k)) { timers.delete(k); t.fn(); }
    },
  };
}

test("hover intent: rest, move, leave grace, card, suppression", () => {
  const c = clock();
  const log = [];
  const hi = new HoverIntent({ delay: 450, grace: 150, onShow: (t) => log.push(`show ${t}`), onHide: (t) => log.push(`hide ${t}`), setTimer: c.setTimer, clearTimer: c.clearTimer });
  hi.rest("a"); c.advance(300); hi.rest("a"); c.advance(300);
  assert.deepEqual(log, [], "moving restarts the delay");
  c.advance(200);
  assert.deepEqual(log, ["show a"]);
  hi.leave(); c.advance(100); hi.enterCard(); c.advance(500);
  assert.deepEqual(log, ["show a"], "moving into the card within the grace keeps it");
  hi.leaveCard(); c.advance(150);
  assert.deepEqual(log, ["show a", "hide a"]);
  hi.suppress(true); hi.rest("b"); c.advance(1000);
  assert.deepEqual(log, ["show a", "hide a"], "nothing shows while suppressed (menu, inspector, selection)");
  hi.suppress(false); hi.rest("b"); c.advance(450); hi.rest("c"); c.advance(450);
  assert.deepEqual(log.slice(2), ["show b", "hide b", "show c"]);
  hi.hide();
  assert.equal(hi.shown, null);
});

test("settings and labels", () => {
  const store = new Map([["kip-hover-cards", "0"], ["kip-hover-delay", "5000"]]);
  assert.deepEqual(hoverSettings({ getItem: (k) => store.get(k) ?? null }), { enabled: false, delay: 450 });
  assert.equal(kindLabel("C_functionprotodecl"), "function");
  assert.equal(kindLabel("CPPro_define_macro"), "macro");
});
