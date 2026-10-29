/**
 * tabs.js - Multi-tab and split-pane view manager.
 * Renders tab strips, pane splitters, handles tab drag-and-drop (in-app, between the windows of
 * two sessions, & drag-out), the tab right-click menu (elevate into a browser tab of its own, push
 * to another session), and delegates active tab rendering to domain views.
 */
import { state } from "../state.js";
import { sessionManager } from "../session_manager.js";
import { shortRef } from "./ref_picker.js";
import { escapeHtml } from "./modal.js";
import { contextMenu } from "./context_menu.js";
import { toast } from "./toast.js";

// Dragged tab in the drag event's data transfer; the text form is what survives browsers that
// hand a custom type only to the window the drag started in.
const TAB_DRAG_MIME = "application/kip-tab";
const TAB_DRAG_TEXT_PREFIX = "kip-tab:";
// A drop on another window of this browser also lands outside this one: elevating the tab into a
// new window waits this long for that window to say it took it (§5.24.4).
const TRANSFER_GRACE_MS = 250;
// A tab pushed to another session is offered to the window holding it first; when no window has
// taken it this long after, it is written into that session's stored record instead (§5.24.5).
const PUSH_ACK_GRACE_MS = 400;

export class TabsComponent {
  constructor(containerEl, viewRenderers) {
    this.container = containerEl;
    // Map of tab type -> view instance shared by every tab of that type, or a factory for types
    // whose tabs each need their own instance (kconfig: one constraint engine per tab)
    this.viewRenderers = viewRenderers;
    this.perTabViews = new Map(); // tab id -> view instance of a factory type

    this.draggedTabContext = null; // { tabId, sourcePaneId, tabData }
    this.dropIndicator = null;
    // Tabs another window took over while they were being dragged out of this one
    this.transferredAwayTabIds = new Set();
    // Tabs pushed to another session and not yet acknowledged: tab id -> { sessionId, sessionName }
    this.pendingPushes = new Map();

    // A tab dragged into another window's session is closed here: the window that took it owns it
    // now, with its contents, its position and (for a KConfig tab) the engine it was running.
    sessionManager.subscribe("tab:transfer:accepted", (msg) => {
      this.transferredAwayTabIds.add(msg.tabId);
      // A push that was taken live needs no stored-record fallback any more (§5.24.5)
      this.pendingPushes.delete(msg.tabId);
      const tab = state.findTab(msg.tabId);
      if (!tab) return;
      const pane = state.panes.find((p) => p.tabs.some((t) => t.id === msg.tabId));
      const title = tab.title;
      state.closeTab(msg.tabId, pane ? pane.id : null);
      toast.info(`'${title}' moved to session '${msg.sessionName || "another window"}'`);
    });

    // A tab pushed to the session this window holds, from another window's tab menu (§5.24.5)
    sessionManager.subscribe("tab:push:received", (payload) => this.acceptPushedTab(payload));

    state.subscribe("layout:changed", () => this.render());
    state.subscribe("tabs:changed", () => {
      this.pruneTabViews();
      this.render();
    });
    state.subscribe("pane:activated", () => this.highlightActivePane());
  }

  render() {
    this.container.innerHTML = "";
    this.container.className = `split-container split-${state.splitMode}`;

    state.panes.forEach((pane, idx) => {
      if (idx > 0) {
        const divider = document.createElement("div");
        divider.className = "pane-divider";
        this.attachDividerResize(divider, state.splitMode, idx);
        this.container.appendChild(divider);
      }

      const paneEl = document.createElement("div");
      paneEl.className = "pane";
      paneEl.dataset.paneId = pane.id;
      if (pane.id === state.activePaneId) {
        paneEl.classList.add("active-pane");
      }

      paneEl.addEventListener("click", () => {
        state.setActivePane(pane.id);
      });

      // 1. Tabstrip
      const tabstrip = document.createElement("div");
      tabstrip.className = "tabstrip";
      this.attachTabstripDropTarget(tabstrip, pane.id);

      const tabList = document.createElement("div");
      tabList.className = "tab-list";

      pane.tabs.forEach((tab, tabIdx) => {
        const tabEl = document.createElement("div");
        tabEl.className = `tab ${tab.id === pane.activeTabId ? "active" : ""}`;
        tabEl.dataset.tabId = tab.id;
        tabEl.dataset.paneId = pane.id;
        tabEl.dataset.tabIndex = String(tabIdx);
        tabEl.title = tab.path || tab.title;

        // Make tab draggable
        this.attachTabDraggable(tabEl, tab, pane.id);
        this.attachTabContextMenu(tabEl, tab, pane.id);

        // Icon
        const iconSpan = document.createElement("span");
        iconSpan.className = `tab-icon ${tab.type}`;
        iconSpan.textContent = this.getTabTypeBadge(tab.type);

        // Title
        const titleSpan = document.createElement("span");
        titleSpan.className = "tab-title";
        titleSpan.textContent = tab.title;

        // Version badge if different from global
        const verSpan = document.createElement("span");
        verSpan.className = "tab-version";
        verSpan.textContent = shortRef(tab.version || state.currentVersion);

        // Close button
        const closeBtn = document.createElement("button");
        closeBtn.className = "tab-close";
        closeBtn.innerHTML = "&times;";
        closeBtn.title = "Close tab";
        closeBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          state.closeTab(tab.id, pane.id);
        });

        tabEl.appendChild(iconSpan);
        tabEl.appendChild(titleSpan);
        tabEl.appendChild(verSpan);
        tabEl.appendChild(closeBtn);

        tabEl.addEventListener("click", () => {
          pane.activeTabId = tab.id;
          state.setActivePane(pane.id);
          if (tab.version && tab.version !== state.currentVersion) {
            state.syncVersionFromTab(tab.version);
          }
          state.emit("tabs:changed", { paneId: pane.id, activeTabId: tab.id });
          state.saveState();
        });

        tabEl.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.closeTab(tab.id, pane.id);
          }
        });

        tabList.appendChild(tabEl);
      });

      tabstrip.appendChild(tabList);

      // Tabstrip actions (Split buttons)
      const actions = document.createElement("div");
      actions.className = "tabstrip-actions";

      const splitHBtn = document.createElement("button");
      splitHBtn.className = "tabstrip-btn";
      splitHBtn.title = state.splitMode === "horizontal" ? "Close Split View" : "Split Editor Right (Side by Side)";
      splitHBtn.innerHTML = `<svg viewBox="0 0 16 16"><path d="M0 2a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V2zm7.5 1v10h6.5a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1h-6.5zm-1 0H2a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h4.5V3z"/></svg>`;
      splitHBtn.onclick = (e) => {
        e.stopPropagation();
        state.setSplitMode(state.splitMode === "horizontal" ? "single" : "horizontal");
      };

      const splitVBtn = document.createElement("button");
      splitVBtn.className = "tabstrip-btn";
      splitVBtn.title = state.splitMode === "vertical" ? "Close Split View" : "Split Editor Down (Stacked)";
      splitVBtn.innerHTML = `<svg viewBox="0 0 16 16"><path d="M0 2a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V2zm1 6.5h14V14a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V8.5zm0-1V3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v4.5H1z"/></svg>`;
      splitVBtn.onclick = (e) => {
        e.stopPropagation();
        state.setSplitMode(state.splitMode === "vertical" ? "single" : "vertical");
      };

      actions.appendChild(splitHBtn);
      actions.appendChild(splitVBtn);
      tabstrip.appendChild(actions);

      paneEl.appendChild(tabstrip);

      // 2. Content Container
      const contentEl = document.createElement("div");
      contentEl.className = "tab-content-container";
      contentEl.dataset.paneId = pane.id;
      this.attachContentDropTarget(contentEl, pane.id, paneEl);

      const activeTab = pane.tabs.find((t) => t.id === pane.activeTabId);
      if (activeTab) {
        this.renderTabContent(contentEl, activeTab);
      } else {
        contentEl.innerHTML = `
          <div style="display:flex;height:100%;align-items:center;justify-content:center;flex-direction:column;color:var(--text-muted);gap:12px;">
            <p>No tab open in this pane.</p>
            <p style="font-size:11px;">Select a file from the sidebar, drop a tab here, or press <kbd style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;border:1px solid var(--border-color);">Ctrl+K</kbd> to search.</p>
          </div>
        `;
      }

      paneEl.appendChild(contentEl);
      this.container.appendChild(paneEl);
    });
  }

  // ------------------------------------------------------------------ Tab Drag & Drop
  attachTabDraggable(tabEl, tab, paneId) {
    tabEl.draggable = true;

    tabEl.addEventListener("dragstart", (e) => {
      // The payload carries where the tab comes from and what has to travel with it, so another
      // window's session can take it exactly as it was (state.tabTransferPayload)
      const payload = state.tabTransferPayload(tab, paneId);
      this.draggedTabContext = {
        tabId: tab.id,
        sourcePaneId: paneId,
        tabData: { ...tab },
        payload
      };

      e.dataTransfer.effectAllowed = "move";
      const serialized = JSON.stringify(payload);
      e.dataTransfer.setData(TAB_DRAG_MIME, serialized);
      e.dataTransfer.setData("text/plain", `${TAB_DRAG_TEXT_PREFIX}${serialized}`);
      // Announced as well, because some browsers only let the window a drag started in read a
      // custom type: the other windows then take the tab from this announcement instead.
      sessionManager.broadcastTabDragStart(payload);

      // Visual ghost
      setTimeout(() => {
        tabEl.classList.add("tab-dragging");
      }, 0);
    });

    tabEl.addEventListener("dragend", (e) => {
      tabEl.classList.remove("tab-dragging");
      this.removeDropIndicator();
      document.querySelectorAll(".pane-drop-target").forEach((el) => el.classList.remove("pane-drop-target"));

      const context = this.draggedTabContext;
      this.draggedTabContext = null;
      sessionManager.broadcastTabDragEnd(tab.id);
      if (!context) return;

      // Check if dropped outside the browser window to spawn new window
      const isOutside =
        e.clientX < 0 ||
        e.clientX > window.innerWidth ||
        e.clientY < 0 ||
        e.clientY > window.innerHeight ||
        (e.screenX !== 0 && (e.screenX < window.screenX || e.screenX > window.screenX + window.outerWidth));

      if (!isOutside) return;

      // Another window of this browser is outside this one too. It says so over the channel, which
      // may arrive just after `dragend`, so elevation waits for that word: a tab handed to another
      // session must never also be elevated into a window of its own.
      const elevate = () => {
        if (this.transferredAwayTabIds.delete(tab.id)) return;
        if (!state.findTab(tab.id)) return; // it was closed or taken meanwhile
        this.elevateTabToNewWindow(context.tabData, context.sourcePaneId);
      };
      if (this.transferredAwayTabIds.has(tab.id)) {
        this.transferredAwayTabIds.delete(tab.id);
        return;
      }
      window.setTimeout(elevate, TRANSFER_GRACE_MS);
    });
  }

  /**
   * The tab a drop carries: this window's own drag, the drop event's data transfer (a drag from
   * another window of this browser), or the drag another window announced (§5.24.4).
   */
  dragPayloadFrom(e) {
    for (const [type, prefix] of [[TAB_DRAG_MIME, ""], ["text/plain", TAB_DRAG_TEXT_PREFIX]]) {
      try {
        const raw = e.dataTransfer?.getData(type) || "";
        if (raw.startsWith(prefix) && raw.length > prefix.length) {
          const parsed = JSON.parse(raw.slice(prefix.length));
          if (parsed && parsed.tabData) return parsed;
        }
      } catch (err) {
        // Not our payload (another application's drag), or the browser withheld it
      }
    }
    // Nothing readable in the drag: fall back to the announcement, unless this drop is plainly
    // something else (files dragged in from the desktop), which must not take someone's tab.
    const types = Array.from(e.dataTransfer?.types || []);
    if (types.includes("Files")) return null;
    return sessionManager.getIncomingTabDrag();
  }

  /**
   * Take a tab dropped here from another window's session: it is inserted at the drop position and
   * the window it came from closes it, so the tab moves rather than being copied.
   */
  acceptForeignTabDrop(e, targetPaneId, targetIndex = null) {
    const payload = this.dragPayloadFrom(e);
    if (!payload || !payload.tabData) return false;
    // This window's own drag is handled by `moveTabToPane`, which keeps the tab's view alive
    if (payload.sourceWinId === sessionManager.getWindowId()) return false;
    // Nothing is written while no session is pinned here (§5.23.5), so the tab would be lost:
    // the window it came from closes it only once this one has taken it.
    if (!state.currentSessionId) {
      toast.info("Choose a session in this window before moving a tab into it");
      return false;
    }

    const tab = state.acceptTransferredTab(payload, targetPaneId, targetIndex);
    if (!tab) return false;

    sessionManager.incomingTabDrag = null;
    sessionManager.broadcastTabTransferAccepted({
      tabId: payload.tabId,
      sourceWinId: payload.sourceWinId,
      sourcePaneId: payload.sourcePaneId,
      sourceSessionId: payload.sourceSessionId
    });
    toast.success(`'${tab.title}' moved here from session '${payload.sourceSessionName || "another window"}'`);
    return true;
  }

  attachTabstripDropTarget(tabstrip, targetPaneId) {
    tabstrip.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";

      const tabList = tabstrip.querySelector(".tab-list");
      if (!tabList) return;

      const tabs = Array.from(tabList.querySelectorAll(".tab:not(.tab-dragging)"));
      const mouseX = e.clientX;

      let targetTab = null;
      let insertAfter = false;

      for (const t of tabs) {
        const rect = t.getBoundingClientRect();
        const midX = rect.left + rect.width / 2;
        if (mouseX < midX) {
          targetTab = t;
          insertAfter = false;
          break;
        } else if (mouseX <= rect.right) {
          targetTab = t;
          insertAfter = true;
          break;
        }
      }

      this.showDropIndicator(tabList, targetTab, insertAfter);
    });

    tabstrip.addEventListener("dragleave", (e) => {
      if (!tabstrip.contains(e.relatedTarget)) {
        this.removeDropIndicator();
      }
    });

    tabstrip.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();

      const dropInfo = this.dropIndicatorContext;
      this.removeDropIndicator();
      const targetIndex = dropInfo ? dropInfo.targetIndex : null;

      if (!this.draggedTabContext) {
        this.acceptForeignTabDrop(e, targetPaneId, targetIndex);
        return;
      }
      const { tabId, sourcePaneId, tabData } = this.draggedTabContext;
      this.moveTabToPane(tabId, sourcePaneId, targetPaneId, targetIndex, tabData);
    });
  }

  attachContentDropTarget(contentEl, targetPaneId, paneEl) {
    contentEl.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      paneEl.classList.add("pane-drop-target");
    });

    contentEl.addEventListener("dragleave", (e) => {
      if (!contentEl.contains(e.relatedTarget)) {
        paneEl.classList.remove("pane-drop-target");
      }
    });

    contentEl.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      paneEl.classList.remove("pane-drop-target");
      this.removeDropIndicator();

      if (!this.draggedTabContext) {
        this.acceptForeignTabDrop(e, targetPaneId, null);
        return;
      }
      const { tabId, sourcePaneId, tabData } = this.draggedTabContext;
      this.moveTabToPane(tabId, sourcePaneId, targetPaneId, null, tabData);
    });
  }

  showDropIndicator(tabList, targetTab, insertAfter) {
    this.removeDropIndicator();

    const indicator = document.createElement("div");
    indicator.className = "tab-drop-indicator";
    this.dropIndicator = indicator;

    if (!targetTab) {
      tabList.appendChild(indicator);
      this.dropIndicatorContext = { targetIndex: tabList.querySelectorAll(".tab").length };
    } else {
      const idx = parseInt(targetTab.dataset.tabIndex, 10);
      if (insertAfter) {
        targetTab.after(indicator);
        this.dropIndicatorContext = { targetIndex: idx + 1 };
      } else {
        targetTab.before(indicator);
        this.dropIndicatorContext = { targetIndex: idx };
      }
    }
  }

  removeDropIndicator() {
    if (this.dropIndicator) {
      this.dropIndicator.remove();
      this.dropIndicator = null;
    }
    this.dropIndicatorContext = null;
  }

  moveTabToPane(tabId, sourcePaneId, targetPaneId, targetIndex = null, tabData = null) {
    const sourcePane = state.getPane(sourcePaneId);
    const targetPane = state.getPane(targetPaneId);
    if (!sourcePane || !targetPane) return;

    const sourceIdx = sourcePane.tabs.findIndex((t) => t.id === tabId);
    if (sourceIdx === -1 && !tabData) return;

    const movingTab = sourceIdx >= 0 ? sourcePane.tabs.splice(sourceIdx, 1)[0] : { ...tabData };

    if (sourcePane.id === targetPane.id) {
      // Reordering within the same pane
      let destIdx = targetIndex !== null ? targetIndex : targetPane.tabs.length;
      if (sourceIdx < destIdx) destIdx -= 1;
      destIdx = Math.max(0, Math.min(destIdx, targetPane.tabs.length));
      targetPane.tabs.splice(destIdx, 0, movingTab);
      targetPane.activeTabId = movingTab.id;
    } else {
      // Moving across panes
      if (sourcePane.activeTabId === tabId) {
        sourcePane.activeTabId = sourcePane.tabs.length > 0 ? sourcePane.tabs[Math.max(0, sourceIdx - 1)].id : null;
      }
      let destIdx = targetIndex !== null ? targetIndex : targetPane.tabs.length;
      destIdx = Math.max(0, Math.min(destIdx, targetPane.tabs.length));
      targetPane.tabs.splice(destIdx, 0, movingTab);
      targetPane.activeTabId = movingTab.id;
      state.setActivePane(targetPane.id);
    }

    state.emit("tabs:changed", { paneId: targetPane.id, activeTabId: targetPane.activeTabId });
    state.saveState();
  }

  /**
   * Move a tab into a browser tab of its own (§5.24.3): a new session is created holding it, this
   * window closes it, and the new browser tab opens pinned to that session. Reached by dragging the
   * tab out of the window and by **Open on New Browser Tab** in its right-click menu (§5.24.5).
   */
  elevateTabToNewWindow(tab, sourcePaneId) {
    if (!tab) return;

    // A KConfig tab elevated into its own window arrives there still running its engine: the new
    // session is created holding it, and rehydrates its values from the shared configuration under
    // `profileKey` (§5.8). Without this the engine would be switched off on the way out.
    const engineHeld = state.isKconfigEngineOwner(tab.id);
    const engine = state.kconfigEngines[tab.id];
    const engineProfiles = engineHeld && engine
      ? {
          [tab.id]: {
            version: engine.version,
            arch: engine.arch,
            configName: engine.configName,
            profileKey: engine.profileKey
          }
        }
      : {};

    // Create a new session for the dragged out tab (pin = false so source window keeps its own session)
    const newSession = sessionManager.createSession({
      name: `${tab.title} Window`,
      version: tab.version || state.currentVersion,
      repo: state.currentRepo,
      splitMode: "single",
      panes: [
        {
          id: "pane-1",
          activeTabId: tab.id,
          tabs: [{ ...tab }]
        }
      ],
      pin: false,
      kconfigEngineTabIds: engineHeld ? [tab.id] : [],
      kconfigEngineProfiles: engineProfiles
    });

    // Close in current window
    state.closeTab(tab.id, sourcePaneId);

    // Open new browser window with the new session pinned
    window.open(`/?session=${encodeURIComponent(newSession.id)}`, "_blank");
  }

  // ------------------------------------------------------------------ Tab Context Menu
  attachTabContextMenu(tabEl, tab, paneId) {
    tabEl.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      state.setActivePane(paneId);
      contextMenu.showTabMenu(e.clientX, e.clientY, {
        tab,
        onOpenInNewWindow: () => this.elevateTabToNewWindow({ ...tab }, paneId),
        onPushToSession: (session) => this.pushTabToSession(tab, paneId, session)
      });
    });
  }

  /**
   * Hand a tab to another session (§5.24.5). The window holding that session takes it live, exactly
   * as it would a tab dropped into it; when no window holds it — or its presence entry was stale —
   * the tab is written into that session's stored record instead. Either way the tab moves: it is
   * closed here only once it has been taken somewhere, so it can never be lost between the two.
   */
  pushTabToSession(tab, sourcePaneId, session) {
    if (!tab || !session?.id) return;
    if (session.id === state.currentSessionId) return;

    const payload = state.tabTransferPayload(tab, sourcePaneId);
    if (!payload) return;

    // A KConfig tab's values travel through the shared configuration store rather than in the
    // payload (§5.8), so what this window holds has to be written before the tab leaves it.
    state.saveState();

    const sessionName = session.name || session.id;
    this.pendingPushes.set(tab.id, { sessionId: session.id, sessionName });
    sessionManager.broadcastTabPush(payload, session.id);

    window.setTimeout(() => {
      // The window holding that session took it: `tab:transfer:accepted` closed the tab and
      // dropped this entry, and writing the stored record too would duplicate the tab.
      if (!this.pendingPushes.delete(tab.id)) return;
      if (!state.findTab(tab.id)) return; // closed or moved elsewhere meanwhile

      const stored = sessionManager.pushTabToStoredSession(session.id, payload);
      if (!stored) {
        toast.error(`'${tab.title}' could not be pushed to session '${sessionName}'`);
        return;
      }
      state.closeTab(tab.id, sourcePaneId);
      toast.success(`'${tab.title}' pushed to session '${sessionName}' (opens there with it)`);
    }, PUSH_ACK_GRACE_MS);
  }

  /** Take a tab another window pushed to the session this window holds (§5.24.5). */
  acceptPushedTab(payload) {
    if (!payload || !payload.tabData) return;
    // Nothing is written while no session is pinned here (§5.23.5), so the tab would be lost: the
    // window it came from keeps it and writes it into the stored record instead.
    if (!state.currentSessionId) return;

    const pane = state.getActivePane() || state.panes[0];
    const tab = state.acceptTransferredTab(payload, pane ? pane.id : null, null);
    if (!tab) return;

    sessionManager.broadcastTabTransferAccepted({
      tabId: payload.tabId,
      sourceWinId: payload.sourceWinId,
      sourcePaneId: payload.sourcePaneId,
      sourceSessionId: payload.sourceSessionId
    });
    toast.success(`'${tab.title}' pushed here from session '${payload.sourceSessionName || "another window"}'`);
  }

  // ------------------------------------------------------------------ Resizable Divider
  attachDividerResize(dividerEl, splitMode, dividerIndex) {
    dividerEl.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const isHorizontal = splitMode === "horizontal";
      const panes = Array.from(this.container.querySelectorAll(".pane"));
      const p1 = panes[dividerIndex - 1];
      const p2 = panes[dividerIndex];
      if (!p1 || !p2) return;

      const containerRect = this.container.getBoundingClientRect();
      dividerEl.classList.add("dragging");
      document.body.style.cursor = isHorizontal ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";

      const onPointerMove = (moveEvent) => {
        if (isHorizontal) {
          const offsetX = moveEvent.clientX - containerRect.left;
          const ratio = Math.max(0.12, Math.min(0.88, offsetX / containerRect.width));
          p1.style.flex = `${(ratio * 100).toFixed(2)} 1 0%`;
          p2.style.flex = `${((1 - ratio) * 100).toFixed(2)} 1 0%`;
        } else {
          const offsetY = moveEvent.clientY - containerRect.top;
          const ratio = Math.max(0.12, Math.min(0.88, offsetY / containerRect.height));
          p1.style.flex = `${(ratio * 100).toFixed(2)} 1 0%`;
          p2.style.flex = `${((1 - ratio) * 100).toFixed(2)} 1 0%`;
        }
      };

      const onPointerUp = () => {
        dividerEl.classList.remove("dragging");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        window.removeEventListener("pointermove", onPointerMove);
        window.removeEventListener("pointerup", onPointerUp);
      };

      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    });
  }

  highlightActivePane() {
    this.container.querySelectorAll(".pane").forEach((p) => {
      if (p.dataset.paneId === state.activePaneId) {
        p.classList.add("active-pane");
      } else {
        p.classList.remove("active-pane");
      }
    });
  }

  /** View rendering a tab: the type's shared instance, or this tab's own one for factory types. */
  rendererFor(tab) {
    const entry = this.viewRenderers[tab.type];
    if (typeof entry !== "function") return entry;
    let view = this.perTabViews.get(tab.id);
    if (!view) {
      view = entry();
      this.perTabViews.set(tab.id, view);
    }
    return view;
  }

  /** Release the views of tabs that were closed. */
  pruneTabViews() {
    const open = new Set(state.panes.flatMap((p) => p.tabs.map((t) => t.id)));
    for (const [tabId, view] of [...this.perTabViews]) {
      if (open.has(tabId)) continue;
      if (typeof view.dispose === "function") view.dispose();
      this.perTabViews.delete(tabId);
    }
  }

  renderTabContent(containerEl, tab) {
    containerEl.innerHTML = "";
    const renderer = this.rendererFor(tab);
    if (renderer && typeof renderer.render === "function") {
      renderer.render(containerEl, tab);
    } else {
      containerEl.innerHTML = `<div style="padding:20px;">Unknown view type: ${escapeHtml(tab.type)}</div>`;
    }
  }

  getTabTypeBadge(type) {
    switch (type) {
      case "code": return "[C]";
      case "kconfig": return "[K]";
      case "nodemap": return "[M]";
      case "maintainers": return "[S]";
      case "commits": return "[G]";
      case "pahole": return "[P]";
      case "callgraph": return "[X]";
      case "diff": return "[D]";
      case "treemap": return "[T]";
      case "explorer": return "[E]";
      case "mail": return "[L]";
      default: return "[?]";
    }
  }
}
