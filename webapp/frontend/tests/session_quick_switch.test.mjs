/**
 * session_quick_switch.test.mjs - Quick-switch session selection, direct adoption, and session lifecycle bugs.
 */
import "./helpers/dom_shim.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Module singletons start intervals/timers: keep them unreffed so the test process exits cleanly.
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

// Stub document.getElementById("#toast-container") minimally for toast.info/warning calls.
const appendedToasts = [];
const toastContainer = {
  appendChild(el) {
    appendedToasts.push(el);
    return el;
  }
};
const origGetById = globalThis.document.getElementById;
globalThis.document.getElementById = (id) => (id === "toast-container" ? toastContainer : origGetById(id));

const { sessionManager } = await import("../js/session_manager.js");
const { state } = await import("../js/state.js");
const {
  MAX_QUICK_SWITCH_SESSIONS,
  getQuickSwitchSessions,
  openWorkspaceSession,
  openWorkspaceSessionInNewTab
} = await import("../js/components/session_modal.js");

function resetSessions(currentId = null) {
  localStorage.clear();
  state.cancelPendingSave();
  sessionManager.activeSessionId = currentId;
  state.currentSessionId = currentId;
  state.currentSessionName = "";
  appendedToasts.length = 0;
}

test("getQuickSwitchSessions excludes current, sorts by updatedAt desc, and caps at limit", () => {
  resetSessions("s-current");
  const index = [
    { id: "s-current", name: "Current", updatedAt: 9999 },
    ...Array.from({ length: 12 }, (_, i) => ({
      id: `s-${i}`,
      name: `Session ${i}`,
      createdAt: 100 + i,
      updatedAt: 1000 + i * 10
    }))
  ];
  sessionManager.saveSessionsIndex(index);

  const res = getQuickSwitchSessions("s-current");
  assert.equal(MAX_QUICK_SWITCH_SESSIONS, 8);
  assert.equal(res.sessions.length, 8);
  assert.equal(res.totalOther, 12);
  assert.equal(res.hiddenCount, 4);
  assert.deepEqual(
    res.sessions.map((s) => s.id),
    ["s-11", "s-10", "s-9", "s-8", "s-7", "s-6", "s-5", "s-4"]
  );
  assert.ok(!res.sessions.some((s) => s.id === "s-current"));

  const customLimit = getQuickSwitchSessions("s-current", 3);
  assert.equal(customLimit.sessions.length, 3);
  assert.equal(customLimit.hiddenCount, 9);
});

test("getQuickSwitchSessions returns empty when no other sessions exist", () => {
  resetSessions("only-one");
  sessionManager.saveSessionsIndex([{ id: "only-one", name: "Only", updatedAt: 500 }]);
  const res = getQuickSwitchSessions("only-one");
  assert.deepEqual(res, { sessions: [], totalOther: 0, hiddenCount: 0 });
});

test("openWorkspaceSession adopts a saved session directly and warns on takeover or missing record", () => {
  resetSessions();
  const s1 = sessionManager.createSession({ name: "First", version: "v3.0", pin: true });
  const s2 = sessionManager.createSession({ name: "Second", version: "v4.0", pin: false });
  state.adoptSession(s1);
  assert.equal(state.currentSessionId, s1.id);

  // Switch directly to s2 without modal
  let selected = null;
  const ok = openWorkspaceSession(s2.id, { onSelect: (id) => { selected = id; } });
  assert.equal(ok, true);
  assert.equal(selected, s2.id);
  assert.equal(state.currentSessionId, s2.id);
  assert.equal(state.currentSessionName, "Second");
  assert.equal(appendedToasts.length, 0);

  // Simulate s1 open in another window and switch back -> takeover toast fires
  sessionManager.saveActiveWindowsRegistry({
    other_win: { winId: "other_win", sessionId: s1.id, sessionName: "First", lastSeen: Date.now() }
  });
  const okTakeover = openWorkspaceSession(s1.id);
  assert.equal(okTakeover, true);
  assert.equal(state.currentSessionId, s1.id);
  assert.equal(appendedToasts.length, 1);

  // Missing session record returns false and warns
  const okMissing = openWorkspaceSession("sess_missing");
  assert.equal(okMissing, false);
  assert.equal(appendedToasts.length, 2);
});

test("createSession sets matching activeTabId and duplicateSession preserves branch and activePaneId", () => {
  resetSessions();
  const created = sessionManager.createSession({ name: "Fresh", version: "v5.0" });
  state.adoptSession(created);
  assert.ok(state.getActiveTab(), "newly created session must have an active tab");
  assert.equal(created.panes[0].activeTabId, created.panes[0].tabs[0].id);

  const multiPane = sessionManager.createSession({
    name: "Multi",
    version: "v6.0",
    selectedBranch: "stable/linux-6.1.y",
    activePaneId: "pane-2",
    panes: [
      { id: "pane-1", activeTabId: "t1", tabs: [{ id: "t1", type: "code", title: "a.c", path: "a.c", version: "v6.0" }] },
      { id: "pane-2", activeTabId: "t2", tabs: [{ id: "t2", type: "code", title: "b.c", path: "b.c", version: "v6.0" }] }
    ]
  });
  const dup = sessionManager.duplicateSession(multiPane.id);
  assert.equal(dup.selectedBranch, "stable/linux-6.1.y");
  assert.equal(dup.activePaneId, "pane-2");
});

test("remote session closure unpins window and clears active session state", () => {
  resetSessions();
  const s = sessionManager.createSession({ name: "Closing", version: "v3.0" });
  state.adoptSession(s);
  state.saveState(); // schedule pending coalesced save

  let closedEventSid = null;
  const unsub = state.subscribe("session:closed", (sid) => { closedEventSid = sid; });
  try {
    sessionManager.channel.onmessage({ data: { type: "session:closed", sessionId: s.id } });
    assert.equal(closedEventSid, s.id);
    assert.equal(sessionManager.activeSessionId, null);
    assert.equal(state.currentSessionId, null);
    assert.equal(state.currentSessionName, "");
    assert.equal(state._saveTimer, null);
  } finally {
    unsub();
  }
});

test("openWorkspaceSessionInNewTab opens ?session=<id> in a new browser tab and handles missing or blocked popups", () => {
  resetSessions();
  const s1 = sessionManager.createSession({ name: "Current", version: "v3.0", pin: true });
  const s2 = sessionManager.createSession({ name: "Target", version: "v4.0", pin: false });
  state.adoptSession(s1);

  const opened = [];
  const origOpen = globalThis.window.open;
  globalThis.window.open = (url, target) => {
    opened.push({ url, target });
    return {};
  };
  try {
    assert.equal(openWorkspaceSessionInNewTab(s2.id), true);
    assert.deepEqual(opened, [{ url: `/?session=${encodeURIComponent(s2.id)}`, target: "_blank" }]);
    assert.equal(state.currentSessionId, s1.id, "current window stays on its own session");

    assert.equal(openWorkspaceSessionInNewTab("sess_nonexistent"), false);
    assert.equal(opened.length, 1);
    assert.equal(appendedToasts.length, 1);

    globalThis.window.open = () => null;
    assert.equal(openWorkspaceSessionInNewTab(s2.id), false);
    assert.equal(appendedToasts.length, 2);
  } finally {
    globalThis.window.open = origOpen;
  }
});
