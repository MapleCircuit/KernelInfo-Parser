/**
 * tabs.js - Multi-tab and split-pane view manager.
 * Renders tab strips, pane splitters, and delegates active tab rendering to domain views.
 */
import { state } from "../state.js";

export class TabsComponent {
  constructor(containerEl, viewRenderers) {
    this.container = containerEl;
    this.viewRenderers = viewRenderers; // Map of tab type -> view instance

    state.subscribe("layout:changed", () => this.render());
    state.subscribe("tabs:changed", () => this.render());
    state.subscribe("pane:activated", () => this.highlightActivePane());
  }

  render() {
    this.container.innerHTML = "";
    this.container.className = `split-container split-${state.splitMode}`;

    state.panes.forEach((pane, idx) => {
      if (idx > 0) {
        const divider = document.createElement("div");
        divider.className = "pane-divider";
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

      const tabList = document.createElement("div");
      tabList.className = "tab-list";

      pane.tabs.forEach((tab) => {
        const tabEl = document.createElement("div");
        tabEl.className = `tab ${tab.id === pane.activeTabId ? "active" : ""}`;
        tabEl.title = tab.path || tab.title;

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
        verSpan.textContent = tab.version || state.currentVersion;

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
      splitHBtn.title = "Split Editor Right";
      splitHBtn.innerHTML = `<svg viewBox="0 0 16 16"><path d="M0 2a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V2zm7.5 1v10h6.5a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1h-6.5zm-1 0H2a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h4.5V3z"/></svg>`;
      splitHBtn.onclick = (e) => {
        e.stopPropagation();
        state.setSplitMode(state.splitMode === "horizontal" ? "single" : "horizontal");
      };

      const splitVBtn = document.createElement("button");
      splitVBtn.className = "tabstrip-btn";
      splitVBtn.title = "Split Editor Down";
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

      const activeTab = pane.tabs.find((t) => t.id === pane.activeTabId);
      if (activeTab) {
        this.renderTabContent(contentEl, activeTab);
      } else {
        contentEl.innerHTML = `
          <div style="display:flex;height:100%;align-items:center;justify-content:center;flex-direction:column;color:var(--text-muted);gap:12px;">
            <p>No tab open in this pane.</p>
            <p style="font-size:11px;">Select a file from the sidebar or press <kbd style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;border:1px solid var(--border-color);">Ctrl+K</kbd> to search.</p>
          </div>
        `;
      }

      paneEl.appendChild(contentEl);
      this.container.appendChild(paneEl);
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

  renderTabContent(containerEl, tab) {
    containerEl.innerHTML = "";
    const renderer = this.viewRenderers[tab.type];
    if (renderer && typeof renderer.render === "function") {
      renderer.render(containerEl, tab);
    } else {
      containerEl.innerHTML = `<div style="padding:20px;">Unknown view type: ${tab.type}</div>`;
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
      default: return "[?]";
    }
  }
}
