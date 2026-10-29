/**
 * session_manager.js - Multi-Window Session & Cross-Tab Coordination Manager.
 * Handles session persistence, window-session binding via sessionStorage,
 * cross-window heartbeat presence, cloned-sessionStorage detection,
 * and real-time cross-tab communication (BroadcastChannel).
 */
import { localDB } from "./db.js";

const SESSIONS_INDEX_KEY = "kip_sessions_index";
const SESSION_STORAGE_PIN_KEY = "kip_active_session_id";
const SESSION_STORAGE_WIN_KEY = "kip_window_id";
const SHARED_PROFILES_KEY = "kip_shared_kconfig_profiles";
const SHARED_TOOLCHAINS_KEY = "kip_shared_kconfig_toolchains";
const BROADCAST_CHANNEL_NAME = "kip_multi_tab";
// Presence refresh, well inside the 30s window `getActiveWindowsList` prunes with
const HEARTBEAT_INTERVAL_MS = 10000;

export class SessionManager {
  constructor() {
    this.channel = typeof window !== "undefined" && "BroadcastChannel" in window
      ? new BroadcastChannel(BROADCAST_CHANNEL_NAME)
      : null;

    this.windowId = null;
    this.heartbeatTimer = null;
    this.activeSessionId = null;
    this.activeSessionName = "Default Session";
    // Remote engines running in other windows: tabId -> { sessionId, sessionName, tabId, version, arch, configName, assignments }
    this.remoteEngines = new Map();
    // Active windows in other browser tabs: winId -> { winId, sessionId, sessionName, lastSeen }
    this.activeWindows = new Map();
    // Tab dragged in another window, while that drag lasts: { tabId, sourceWinId, ..., at }
    this.incomingTabDrag = null;
    this.subscribers = new Map();

    this.getWindowId();
    this.initChannelListeners();
    this.initWindowLifecycle();
  }

  getWindowId() {
    if (!this.windowId) {
      if (typeof window !== "undefined") {
        if (window.name && window.name.startsWith("kip_win_")) {
          this.windowId = window.name;
        } else {
          this.windowId = `kip_win_${Date.now().toString(36)}_${Math.random().toString(36).substring(2, 7)}`;
          try {
            window.name = this.windowId;
          } catch (e) {}
        }
      } else {
        this.windowId = "kip_win_mock";
      }
    }
    return this.windowId;
  }

  initWindowLifecycle() {
    if (typeof window === "undefined") return;

    window.addEventListener("beforeunload", () => {
      if (this.channel && this.activeSessionId) {
        this.channel.postMessage({
          type: "window:unloading",
          winId: this.getWindowId(),
          sessionId: this.activeSessionId
        });
      }
    });

    // Announce this window and ask for active peers
    setTimeout(() => {
      this.broadcastWindowHeartbeat();
      this.requestActiveWindows();
    }, 100);

    // Presence is read with a 30s cut-off (`getActiveWindowsList`), so it has to be refreshed:
    // without this a window that has been open for half a minute looks gone to the others and the
    // Session Picker stops reporting which sessions are in use elsewhere.
    this.heartbeatTimer = setInterval(() => this.broadcastWindowHeartbeat(), HEARTBEAT_INTERVAL_MS);
  }

  subscribe(event, callback) {
    if (!this.subscribers.has(event)) {
      this.subscribers.set(event, new Set());
    }
    this.subscribers.get(event).add(callback);
    return () => this.subscribers.get(event).delete(callback);
  }

  emit(event, data) {
    if (this.subscribers.has(event)) {
      this.subscribers.get(event).forEach((cb) => {
        try {
          cb(data);
        } catch (e) {
          console.error(`Error in sessionManager subscriber for ${event}:`, e);
        }
      });
    }
  }

  initChannelListeners() {
    if (!this.channel) return;

    this.channel.onmessage = (event) => {
      const msg = event.data;
      if (!msg || !msg.type) return;

      switch (msg.type) {
        case "kconfig:engine:announced": {
          if (msg.sessionId !== this.activeSessionId) {
            this.remoteEngines.set(msg.tabId, {
              sessionId: msg.sessionId,
              sessionName: msg.sessionName,
              winId: msg.winId,
              tabId: msg.tabId,
              title: msg.title || `KConfig (${msg.arch})`,
              version: msg.version,
              arch: msg.arch,
              configName: msg.configName || "",
              assignments: msg.assignments || {},
              isRemote: true
            });
            this.emit("kconfig:remote_engines:updated", this.getRemoteEngineList());
          }
          break;
        }

        case "kconfig:assignments:broadcast": {
          if (msg.sessionId === this.activeSessionId) break;
          const eng = this.remoteEngines.get(msg.tabId);
          if (!eng) {
            // Its announcement never reached this window (it was opened later, or the engine was
            // enabled before this window had a session): ask for it so the engine registers here
            // and the next edit arrives as an update.
            this.requestRemoteEngines();
            break;
          }
          eng.assignments = msg.assignments || {};
          // The tab may have moved to another ARCH, revision or configuration name since it announced
          if (msg.title) eng.title = msg.title;
          if (msg.version) eng.version = msg.version;
          if (msg.arch) eng.arch = msg.arch;
          if (typeof msg.configName === "string") eng.configName = msg.configName;
          if (msg.profileKey) eng.profileKey = msg.profileKey;
          this.emit("kconfig:remote_assignments:updated", { tabId: msg.tabId, assignments: eng.assignments });
          break;
        }

        case "kconfig:engine:revoked": {
          // Only the window that announced an engine may retire it. Engines are keyed by tab id,
          // and a tab transferred to another window (§5.24.4) runs there under the same id: the
          // window it left revokes that id just after the new one announced it, and dropping the
          // entry then would lose an engine that is still running.
          const registered = this.remoteEngines.get(msg.tabId);
          if (registered && (!msg.winId || !registered.winId || registered.winId === msg.winId)) {
            this.remoteEngines.delete(msg.tabId);
            this.emit("kconfig:remote_engines:updated", this.getRemoteEngineList());
          }
          break;
        }

        case "kconfig:request_engines": {
          // Another window asked who is running an engine. StateStore will answer.
          this.emit("kconfig:engines:requested", msg);
          break;
        }

        case "session:index:updated": {
          this.emit("session:index:updated", msg);
          break;
        }

        case "session:renamed": {
          if (msg.id === this.activeSessionId) {
            this.activeSessionName = msg.name;
          }
          this.emit("session:renamed", msg);
          break;
        }

        case "session:takeover": {
          if (msg.winId === this.getWindowId()) break;
          if (msg.sessionId && msg.sessionId === this.activeSessionId) {
            // Another window just took this session: sign out of it before anything else is
            // written to it, so the two windows can never save over each other.
            const sid = this.activeSessionId;
            this.unpinSessionFromWindow();
            this.emit("session:taken_over", { sessionId: sid, byWinId: msg.winId });
          }
          break;
        }

        case "session:closed": {
          if (msg.sessionId === this.activeSessionId) {
            this.emit("session:closed_remotely", msg.sessionId);
          }
          if (this.dropRemoteEnginesOf((eng) => eng.sessionId === msg.sessionId)) {
            this.emit("kconfig:remote_engines:updated", this.getRemoteEngineList());
          }
          break;
        }

        case "tab:drag:start": {
          // A tab is being dragged in another window: this window may become its drop target even
          // when the browser hides the drag payload from `dragover` (§5.24.4).
          if (msg.winId === this.getWindowId()) break;
          this.incomingTabDrag = { ...(msg.payload || {}), at: Date.now() };
          this.emit("tab:drag:started", this.incomingTabDrag);
          break;
        }

        case "tab:drag:end": {
          if (msg.winId === this.getWindowId()) break;
          if (this.incomingTabDrag && this.incomingTabDrag.tabId === msg.tabId) {
            this.incomingTabDrag = null;
          }
          this.emit("tab:drag:ended", msg);
          break;
        }

        case "tab:push": {
          // A tab handed straight to this window's session from another one (§5.24.5), without a
          // drag: it is taken here exactly as a cross-window drop, and the window it came from
          // closes its copy once this one answers `tab:transfer:accepted`.
          if (msg.winId === this.getWindowId()) break;
          if (!msg.targetSessionId || msg.targetSessionId !== this.activeSessionId) break;
          this.emit("tab:push:received", msg.payload || null);
          break;
        }

        case "tab:transfer:accepted": {
          // Another window took the tab: it is gone from the drag, wherever it came from
          this.incomingTabDrag = null;
          if (msg.sourceWinId !== this.getWindowId()) break;
          this.emit("tab:transfer:accepted", msg);
          break;
        }

        case "window:heartbeat": {
          if (msg.winId !== this.getWindowId() && msg.sessionId) {
            this.activeWindows.set(msg.winId, {
              winId: msg.winId,
              sessionId: msg.sessionId,
              sessionName: msg.sessionName,
              lastSeen: Date.now()
            });
            this.emit("window:active_changed", this.getActiveWindowsList());
          }
          break;
        }

        case "window:request_active": {
          if (msg.winId !== this.getWindowId() && this.activeSessionId) {
            this.broadcastWindowHeartbeat();
          }
          break;
        }

        case "window:unloading": {
          if (this.activeWindows.has(msg.winId)) {
            this.activeWindows.delete(msg.winId);
            this.emit("window:active_changed", this.getActiveWindowsList());
          }
          // The engines that window was running are gone with it: a code tab following one of them
          // must fall back instead of dimming against a configuration nobody resolves any more.
          if (this.dropRemoteEnginesOf((eng) => (eng.winId ? eng.winId === msg.winId : eng.sessionId === msg.sessionId))) {
            this.emit("kconfig:remote_engines:updated", this.getRemoteEngineList());
          }
          break;
        }
      }
    };
  }

  broadcastWindowHeartbeat() {
    if (!this.channel || !this.activeSessionId) return;
    this.channel.postMessage({
      type: "window:heartbeat",
      winId: this.getWindowId(),
      sessionId: this.activeSessionId,
      sessionName: this.activeSessionName
    });
  }

  requestActiveWindows() {
    if (!this.channel) return;
    this.channel.postMessage({
      type: "window:request_active",
      winId: this.getWindowId()
    });
  }

  getActiveWindowsList() {
    const now = Date.now();
    const list = [];
    for (const [wId, w] of this.activeWindows.entries()) {
      if (now - w.lastSeen < 30000) {
        list.push(w);
      } else {
        this.activeWindows.delete(wId);
      }
    }
    return list;
  }

  isSessionActiveInOtherWindow(sessionId) {
    if (!sessionId) return false;
    const now = Date.now();
    for (const [wId, w] of this.activeWindows.entries()) {
      if (w.sessionId === sessionId && w.winId !== this.getWindowId()) {
        if (now - w.lastSeen < 30000) return true;
      }
    }
    return false;
  }

  // ------------------------------------------------------------------ Session Identification
  /**
   * Determine session for the current browser window on startup:
   * 1. Query parameter: ?session=<id>
   * 2. Window sessionStorage: sessionStorage[kip_active_session_id]
   * Detects and breaks cloned sessionStorage across tabs so new tabs do not overlap.
   */
  resolveStartupSessionId() {
    try {
      const urlParams = new URLSearchParams(window.location.search);
      const urlSession = urlParams.get("session");
      if (urlSession) {
        // Clean URL without triggering navigation
        urlParams.delete("session");
        const cleanQuery = urlParams.toString() ? `?${urlParams.toString()}` : "";
        window.history.replaceState({}, document.title, window.location.pathname + cleanQuery + window.location.hash);
        if (this.hasSession(urlSession)) {
          this.pinSessionToWindow(urlSession);
          return urlSession;
        }
      }

      const winId = this.getWindowId();
      const storedWinId = sessionStorage.getItem(SESSION_STORAGE_WIN_KEY);
      const pinned = sessionStorage.getItem(SESSION_STORAGE_PIN_KEY);

      // Detect if this tab cloned sessionStorage from another tab
      if (storedWinId && storedWinId !== winId) {
        // Cloned from another tab! Unpin to prevent overlap and prompt user
        sessionStorage.removeItem(SESSION_STORAGE_PIN_KEY);
        sessionStorage.setItem(SESSION_STORAGE_WIN_KEY, winId);
        return null;
      }

      // Rehydrate if this is genuinely the same window (e.g. F5 reload)
      if (pinned && this.hasSession(pinned)) {
        sessionStorage.setItem(SESSION_STORAGE_WIN_KEY, winId);
        return pinned;
      }
    } catch (e) {
      console.warn("[SessionManager] Startup session resolution error:", e);
    }
    return null;
  }

  pinSessionToWindow(sessionId, sessionName = null) {
    this.activeSessionId = sessionId;
    if (sessionName) {
      this.activeSessionName = sessionName;
    } else {
      const meta = this.getSessionMeta(sessionId);
      if (meta?.name) this.activeSessionName = meta.name;
    }
    try {
      sessionStorage.setItem(SESSION_STORAGE_PIN_KEY, sessionId);
      sessionStorage.setItem(SESSION_STORAGE_WIN_KEY, this.getWindowId());
    } catch (e) {}
    this.broadcastWindowHeartbeat();
    this.claimSessionExclusively(sessionId);
  }

  /**
   * A session is open in exactly one window at a time. Pinning it here claims it: any other window
   * holding it signs out of that session on the spot (`session:taken_over` → Session Picker), so two
   * windows can never write over each other's workspace. The claim is unconditional rather than
   * conditional on presence, so it holds even when the other window's heartbeat has not been seen.
   */
  claimSessionExclusively(sessionId) {
    if (!this.channel || !sessionId) return;
    this.channel.postMessage({
      type: "session:takeover",
      sessionId,
      winId: this.getWindowId(),
      sessionName: this.activeSessionName
    });
  }

  unpinSessionFromWindow() {
    const oldSid = this.activeSessionId;
    this.activeSessionId = null;
    try {
      sessionStorage.removeItem(SESSION_STORAGE_PIN_KEY);
    } catch (e) {}
    if (this.channel && oldSid) {
      this.channel.postMessage({
        type: "window:unloading",
        winId: this.getWindowId(),
        sessionId: oldSid
      });
    }
  }

  // ------------------------------------------------------------------ Session Storage & Index
  hasSession(sessionId) {
    if (!sessionId) return false;
    const index = this.getSessionsIndex();
    return index.some((s) => s.id === sessionId);
  }

  getSessionMeta(sessionId) {
    if (!sessionId) return null;
    const index = this.getSessionsIndex();
    return index.find((s) => s.id === sessionId) || null;
  }

  getSessionsIndex() {
    try {
      const raw = localStorage.getItem(SESSIONS_INDEX_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) return parsed;
      }
    } catch (e) {
      console.warn("[SessionManager] Failed to read sessions index:", e);
    }

    // Auto-migrate legacy kernel_ide_state if index is empty
    return this.migrateLegacySession();
  }

  saveSessionsIndex(index) {
    try {
      localStorage.setItem(SESSIONS_INDEX_KEY, JSON.stringify(index));
      this.channel?.postMessage({ type: "session:index:updated" });
    } catch (e) {
      console.warn("[SessionManager] Failed to save sessions index:", e);
    }
  }

  migrateLegacySession() {
    try {
      const legacyRaw = localStorage.getItem("kernel_ide_state");
      if (legacyRaw) {
        const legacyState = JSON.parse(legacyRaw);
        const legacyId = `sess_${Date.now().toString(36)}_${Math.random().toString(36).substring(2, 8)}`;
        const meta = {
          id: legacyId,
          name: "Default Session",
          createdAt: Date.now(),
          updatedAt: Date.now(),
          repo: legacyState.repo || "",
          version: legacyState.version || "v3.0",
          tabCount: (legacyState.panes?.[0]?.tabs?.length) || 1,
          previewTabs: (legacyState.panes?.[0]?.tabs || []).map((t) => ({ type: t.type, title: t.title }))
        };

        const stateObj = {
          ...legacyState,
          id: legacyId,
          name: "Default Session"
        };
        localStorage.setItem(`kip_session_${legacyId}`, JSON.stringify(stateObj));
        localDB.set("tabs_state", `session_${legacyId}`, stateObj).catch(() => {});

        const initialIndex = [meta];
        localStorage.setItem(SESSIONS_INDEX_KEY, JSON.stringify(initialIndex));
        return initialIndex;
      }
    } catch (e) {
      console.warn("[SessionManager] Migration of legacy state failed:", e);
    }
    return [];
  }

  // ------------------------------------------------------------------ CRUD
  createSession({
    name = "",
    repo = "",
    version = "v3.0",
    splitMode = "single",
    panes = null,
    pin = false,
    // A session created around tabs that already exist (drag-out, duplication) keeps the engines
    // those tabs were running: a KConfig tab arrives resolving its configuration, not switched off.
    kconfigEngineTabIds = [],
    kconfigEngineProfiles = {}
  } = {}) {
    const timestampPart = Date.now().toString(36);
    const randomPart = Math.random().toString(36).substring(2, 8);
    const id = `sess_${timestampPart}_${randomPart}`;
    const sessionName = name.trim() || `Session ${this.getSessionsIndex().length + 1}`;

    const defaultPanes = panes || [
      {
        id: "pane-1",
        activeTabId: "tab-default",
        tabs: [
          {
            id: `tab-${Math.random().toString(36).substring(2, 9)}`,
            type: "code",
            title: "sched.c",
            version: version || "v3.0",
            path: "kernel/sched.c",
            cursorLine: 1,
            scrollLine: 1
          }
        ]
      }
    ];

    const sessionState = {
      id,
      name: sessionName,
      createdAt: Date.now(),
      updatedAt: Date.now(),
      repo,
      version,
      selectedVersion: version,
      splitMode,
      activePaneId: "pane-1",
      panes: defaultPanes,
      kconfigEngineTabIds: [...kconfigEngineTabIds],
      kconfigEngineProfiles: { ...kconfigEngineProfiles }
    };

    // Save record
    localStorage.setItem(`kip_session_${id}`, JSON.stringify(sessionState));
    localDB.set("tabs_state", `session_${id}`, sessionState).catch(() => {});

    // Update index
    const index = this.getSessionsIndex();
    index.unshift({
      id,
      name: sessionName,
      createdAt: sessionState.createdAt,
      updatedAt: sessionState.updatedAt,
      repo,
      version,
      tabCount: defaultPanes.reduce((sum, p) => sum + (p.tabs?.length || 0), 0),
      previewTabs: defaultPanes.flatMap((p) => p.tabs || []).map((t) => ({ type: t.type, title: t.title }))
    });
    this.saveSessionsIndex(index);

    if (pin) {
      this.pinSessionToWindow(id, sessionName);
    }
    return sessionState;
  }

  /**
   * Copy of a session's panes with fresh tab ids, plus the old id -> new id map. A duplicate is an
   * independent session: were its tabs to keep their ids, the copy of a KConfig tab would announce
   * its engine under the id the original announces (engines are keyed by tab id across windows,
   * §5.23.5) and a code tab of the copy would follow the original's engine.
   */
  static remapPaneTabIds(panes) {
    const copy = JSON.parse(JSON.stringify(panes || []));
    const idMap = {};
    for (const pane of copy) {
      for (const tab of pane.tabs || []) {
        const fresh = `tab-${Math.random().toString(36).substring(2, 9)}`;
        idMap[tab.id] = fresh;
        tab.id = fresh;
      }
    }
    for (const pane of copy) {
      if (pane.activeTabId && idMap[pane.activeTabId]) pane.activeTabId = idMap[pane.activeTabId];
      for (const tab of pane.tabs || []) {
        // A code tab following a KConfig tab of this session follows its copy; one following an
        // engine of another session (no entry in the map) keeps following that one.
        if (tab.kconfigEngineTabId && idMap[tab.kconfigEngineTabId]) {
          tab.kconfigEngineTabId = idMap[tab.kconfigEngineTabId];
        }
      }
    }
    return { panes: copy, idMap };
  }

  duplicateSession(sessionId, newName = null) {
    // Read, never `loadSession`: that pins the original to this window and would take it over
    const orig = this.readSession(sessionId);
    if (!orig) return null;

    const dupName = newName || `${orig.name || "Session"} (Copy)`;
    const { panes, idMap } = SessionManager.remapPaneTabIds(orig.panes);

    // The copy runs the same engines as the original, under the copied tabs' ids
    const engineTabIds = (orig.kconfigEngineTabIds || []).map((id) => idMap[id]).filter(Boolean);
    const engineProfiles = {};
    for (const [oldId, descriptor] of Object.entries(orig.kconfigEngineProfiles || {})) {
      if (idMap[oldId]) engineProfiles[idMap[oldId]] = { ...descriptor };
    }

    const newSession = this.createSession({
      name: dupName,
      repo: orig.repo,
      version: orig.version,
      splitMode: orig.splitMode,
      panes,
      pin: false,
      kconfigEngineTabIds: engineTabIds,
      kconfigEngineProfiles: engineProfiles
    });
    return newSession;
  }

  /** A session's stored record, without pinning it to this window (so without claiming it). */
  readSession(sessionId) {
    if (!sessionId) return null;
    try {
      const raw = localStorage.getItem(`kip_session_${sessionId}`);
      if (raw) return JSON.parse(raw);
    } catch (e) {
      console.warn(`[SessionManager] Failed to read session ${sessionId}:`, e);
    }
    return null;
  }

  loadSession(sessionId) {
    if (!sessionId) return null;
    try {
      const raw = localStorage.getItem(`kip_session_${sessionId}`);
      if (raw) {
        const parsed = JSON.parse(raw);
        this.pinSessionToWindow(sessionId, parsed.name || "Session");
        return parsed;
      }
    } catch (e) {
      console.warn(`[SessionManager] Failed to load session ${sessionId} from localStorage:`, e);
    }
    return null;
  }

  async loadSessionFromDB(sessionId) {
    const memoryLoaded = this.loadSession(sessionId);
    if (memoryLoaded) return memoryLoaded;

    try {
      const dbLoaded = await localDB.get("tabs_state", `session_${sessionId}`);
      if (dbLoaded) {
        this.pinSessionToWindow(sessionId, dbLoaded.name || "Session");
        localStorage.setItem(`kip_session_${sessionId}`, JSON.stringify(dbLoaded));
        return dbLoaded;
      }
    } catch (e) {
      console.warn(`[SessionManager] Failed to load session ${sessionId} from IndexedDB:`, e);
    }
    return null;
  }

  saveActiveSession(stateObj) {
    if (!this.activeSessionId) return;
    const id = this.activeSessionId;

    const sessionRecord = {
      id,
      name: this.activeSessionName || stateObj.name || "Session",
      updatedAt: Date.now(),
      repo: stateObj.repo || "",
      version: stateObj.version || "v3.0",
      selectedVersion: stateObj.selectedVersion || stateObj.version || "v3.0",
      splitMode: stateObj.splitMode || "single",
      activePaneId: stateObj.activePaneId || "pane-1",
      panes: stateObj.panes || [],
      kconfigEngineTabIds: stateObj.kconfigEngineTabIds || [],
      kconfigEngineProfiles: stateObj.kconfigEngineProfiles || {}
    };

    try {
      localStorage.setItem(`kip_session_${id}`, JSON.stringify(sessionRecord));
      localDB.set("tabs_state", `session_${id}`, sessionRecord).catch(() => {});

      // Update index entry
      const index = this.getSessionsIndex();
      const idx = index.findIndex((s) => s.id === id);
      const allTabs = sessionRecord.panes.flatMap((p) => p.tabs || []);
      const meta = {
        id,
        name: this.activeSessionName,
        createdAt: idx >= 0 ? index[idx].createdAt : Date.now(),
        updatedAt: sessionRecord.updatedAt,
        repo: sessionRecord.repo,
        version: sessionRecord.version,
        tabCount: allTabs.length,
        previewTabs: allTabs.slice(0, 5).map((t) => ({ type: t.type, title: t.title }))
      };

      if (idx >= 0) {
        index[idx] = meta;
      } else {
        index.unshift(meta);
      }
      this.saveSessionsIndex(index);
    } catch (e) {
      console.warn(`[SessionManager] Failed to save active session ${id}:`, e);
    }
  }

  renameSession(sessionId, newName) {
    const trimmed = (newName || "").trim();
    if (!sessionId || !trimmed) return false;

    if (sessionId === this.activeSessionId) {
      this.activeSessionName = trimmed;
    }

    try {
      const raw = localStorage.getItem(`kip_session_${sessionId}`);
      if (raw) {
        const session = JSON.parse(raw);
        session.name = trimmed;
        session.updatedAt = Date.now();
        localStorage.setItem(`kip_session_${sessionId}`, JSON.stringify(session));
        localDB.set("tabs_state", `session_${sessionId}`, session).catch(() => {});
      }

      const index = this.getSessionsIndex();
      const item = index.find((s) => s.id === sessionId);
      if (item) {
        item.name = trimmed;
        item.updatedAt = Date.now();
        this.saveSessionsIndex(index);
      }
      this.channel?.postMessage({ type: "session:renamed", id: sessionId, name: trimmed });
      this.emit("session:renamed", { id: sessionId, name: trimmed });
      return true;
    } catch (e) {
      console.warn(`[SessionManager] Failed to rename session ${sessionId}:`, e);
      return false;
    }
  }

  deleteSession(sessionId) {
    if (!sessionId) return false;

    try {
      localStorage.removeItem(`kip_session_${sessionId}`);
      localDB.delete("tabs_state", `session_${sessionId}`).catch(() => {});

      const index = this.getSessionsIndex().filter((s) => s.id !== sessionId);
      this.saveSessionsIndex(index);

      if (sessionId === this.activeSessionId) {
        this.unpinSessionFromWindow();
      }

      this.channel?.postMessage({ type: "session:closed", sessionId });
      this.emit("session:deleted", sessionId);
      return true;
    } catch (e) {
      console.warn(`[SessionManager] Failed to delete session ${sessionId}:`, e);
      return false;
    }
  }

  // ------------------------------------------------------------------ Shared KConfig Storage
  getSharedKconfigProfiles() {
    try {
      const raw = localStorage.getItem(SHARED_PROFILES_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) {}
    return {};
  }

  saveSharedKconfigProfiles(profiles) {
    try {
      localStorage.setItem(SHARED_PROFILES_KEY, JSON.stringify(profiles || {}));
    } catch (e) {}
  }

  getSharedKconfigToolchains() {
    try {
      const raw = localStorage.getItem(SHARED_TOOLCHAINS_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) {}
    return {};
  }

  saveSharedKconfigToolchains(toolchains) {
    try {
      localStorage.setItem(SHARED_TOOLCHAINS_KEY, JSON.stringify(toolchains || {}));
    } catch (e) {}
  }

  // ------------------------------------------------------------------ Remote Engine Registry
  getRemoteEngineList() {
    return Array.from(this.remoteEngines.values());
  }

  /** Forget every remote engine matching `predicate`; true when any was registered. */
  dropRemoteEnginesOf(predicate) {
    let dropped = false;
    for (const [tabId, eng] of this.remoteEngines.entries()) {
      if (predicate(eng)) {
        this.remoteEngines.delete(tabId);
        dropped = true;
      }
    }
    return dropped;
  }

  broadcastEngineAnnounce(engineData) {
    if (!this.channel || !this.activeSessionId) return;
    this.channel.postMessage({
      type: "kconfig:engine:announced",
      sessionId: this.activeSessionId,
      sessionName: this.activeSessionName,
      winId: this.getWindowId(),
      ...engineData
    });
  }

  broadcastEngineAssignments(tabId, assignments, meta = {}) {
    if (!this.channel || !this.activeSessionId) return;
    this.channel.postMessage({
      type: "kconfig:assignments:broadcast",
      sessionId: this.activeSessionId,
      tabId,
      assignments,
      ...meta
    });
  }

  broadcastEngineRevoke(tabId) {
    if (!this.channel || !this.activeSessionId) return;
    this.channel.postMessage({
      type: "kconfig:engine:revoked",
      sessionId: this.activeSessionId,
      winId: this.getWindowId(),
      tabId
    });
  }

  requestRemoteEngines() {
    if (!this.channel) return;
    this.channel.postMessage({
      type: "kconfig:request_engines",
      sessionId: this.activeSessionId
    });
  }

  // ------------------------------------------------------------------ Cross-Window Tab Transfers
  /** Announce a tab drag so every other window can take the tab as a drop (§5.24.4). */
  broadcastTabDragStart(payload) {
    if (!this.channel || !payload) return;
    this.channel.postMessage({
      type: "tab:drag:start",
      winId: this.getWindowId(),
      payload
    });
  }

  /** The drag ended here: no window is a drop target for that tab any more. */
  broadcastTabDragEnd(tabId) {
    if (!this.channel) return;
    this.channel.postMessage({
      type: "tab:drag:end",
      winId: this.getWindowId(),
      tabId
    });
  }

  /** Tell the window the tab came from that this window took it, so it closes its copy. */
  broadcastTabTransferAccepted({ tabId, sourceWinId, sourcePaneId, sourceSessionId }) {
    if (!this.channel) return;
    this.channel.postMessage({
      type: "tab:transfer:accepted",
      tabId,
      sourceWinId,
      sourcePaneId,
      sourceSessionId,
      winId: this.getWindowId(),
      sessionId: this.activeSessionId,
      sessionName: this.activeSessionName
    });
  }

  /**
   * Hand a tab to the window holding `targetSessionId` (§5.24.5). Whether any window is listening
   * is not known here — the caller writes the tab into that session's stored record when no
   * `tab:transfer:accepted` comes back, so a stale presence entry cannot lose it.
   */
  broadcastTabPush(payload, targetSessionId) {
    if (!this.channel || !payload || !targetSessionId) return false;
    this.channel.postMessage({
      type: "tab:push",
      winId: this.getWindowId(),
      sessionId: this.activeSessionId,
      targetSessionId,
      payload
    });
    return true;
  }

  /**
   * Write a tab into the stored record of a session no window is holding (§5.24.5): it is appended
   * to that session's active pane as its active tab and is there the next time the session is
   * opened. A KConfig tab keeps the engine it was running — only the descriptor is stored, its
   * values being shared across sessions under `profileKey` (§5.8). Returns the stored tab, or null.
   */
  pushTabToStoredSession(sessionId, payload) {
    const session = this.readSession(sessionId);
    const tabData = payload?.tabData;
    if (!session || !tabData) return null;

    const panes = Array.isArray(session.panes) && session.panes.length > 0
      ? session.panes
      : [{ id: "pane-1", activeTabId: null, tabs: [] }];
    const pane = panes.find((p) => p.id === session.activePaneId) || panes[0];
    if (!Array.isArray(pane.tabs)) pane.tabs = [];

    const tab = { ...tabData };
    // Tab ids are unique per session, not globally: a duplicate of the source session may be stored
    // here, and two tabs of one session sharing an id would announce one engine under both.
    const taken = new Set(panes.flatMap((p) => (p.tabs || []).map((t) => t.id)));
    if (taken.has(tab.id)) tab.id = `tab-${Math.random().toString(36).substring(2, 9)}`;

    pane.tabs.push(tab);
    pane.activeTabId = tab.id;
    session.panes = panes;
    session.activePaneId = pane.id;
    session.updatedAt = Date.now();

    const kc = payload.kconfig || null;
    if (tab.type === "kconfig" && kc?.engineActive) {
      session.kconfigEngineTabIds = [
        ...(session.kconfigEngineTabIds || []).filter((id) => id !== tab.id),
        tab.id
      ];
      session.kconfigEngineProfiles = {
        ...(session.kconfigEngineProfiles || {}),
        [tab.id]: {
          version: kc.version || tab.version,
          arch: kc.arch || tab.arch,
          configName: kc.configName || tab.configName || "",
          profileKey: kc.profileKey || ""
        }
      };
    }

    try {
      localStorage.setItem(`kip_session_${sessionId}`, JSON.stringify(session));
      localDB.set("tabs_state", `session_${sessionId}`, session).catch(() => {});

      const index = this.getSessionsIndex();
      const meta = index.find((s) => s.id === sessionId);
      if (meta) {
        const allTabs = panes.flatMap((p) => p.tabs || []);
        meta.updatedAt = session.updatedAt;
        meta.tabCount = allTabs.length;
        meta.previewTabs = allTabs.slice(0, 5).map((t) => ({ type: t.type, title: t.title }));
        this.saveSessionsIndex(index);
      }
    } catch (e) {
      console.warn(`[SessionManager] Failed to push a tab into session ${sessionId}:`, e);
      return null;
    }
    return tab;
  }

  /** The tab being dragged in another window right now, or null (stale announcements expire). */
  getIncomingTabDrag(maxAgeMs = 120000) {
    if (!this.incomingTabDrag) return null;
    if (Date.now() - this.incomingTabDrag.at > maxAgeMs) {
      this.incomingTabDrag = null;
      return null;
    }
    return this.incomingTabDrag;
  }
}

export const sessionManager = new SessionManager();
