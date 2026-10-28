/**
 * mail_view.js - Linux Kernel Mailing List (LKML) & Public-Inbox Thread Viewer.
 *
 * Provides an integrated two-column interface for searching, browsing, beautifying,
 * and navigating LKML patch series and email discussion threads with direct code editor jumps.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { debounce } from "../../utils/debounce.js";
import { escapeHtml } from "../../components/modal.js";
import { toast } from "../../components/toast.js";
import { copyToClipboard } from "../../utils/clipboard.js";

const STATUS_COLORS = {
  A: "var(--accent-green)",
  D: "var(--accent-red)",
  R: "#d2a8ff",
  M: "var(--accent-yellow)",
};

export class MailView {
  constructor() {
    this.containerEl = null;
    this.currentQuery = "";
    this.currentFilter = "all";
    this.currentPage = 1;
    this.totalResults = 0;
    this.results = [];

    this.activeThreadId = 0;
    this.activeMessageId = "";
    this.activeThreadData = null;
    this.loadedMessageDetails = new Map(); // mid -> message detail object
    this.rawModeMap = new Map(); // mid -> boolean (true if in raw view)

    this.isLoadingSearch = false;
    this.isLoadingThread = false;
    this.daemonStatus = null;
    this._checkingDaemon = false;
  }

  async render(containerEl, tabData = {}) {
    this.containerEl = containerEl;
    if (tabData.query !== undefined) this.currentQuery = tabData.query;
    if (tabData.threadId) this.activeThreadId = parseInt(tabData.threadId, 10);
    if (tabData.messageId) this.activeMessageId = String(tabData.messageId).trim();

    containerEl.innerHTML = `
      <div class="mail-view-container">
        <!-- Left Column: Search & Results List -->
        <div class="mail-list-col">
          <div class="mail-search-bar">
            <div style="display:flex;gap:6px;align-items:center;">
              <span style="font-size:14px;">✉</span>
              <input type="text" id="mail-search-input" class="mail-search-input" placeholder="Search LKML subject, author, s:, f:, m:..." />
              <button id="mail-search-clear-btn" class="mail-clear-btn" title="Clear search" style="display:none;">&times;</button>
            </div>
            <!-- Quick Filter Chips -->
            <div class="mail-filter-chips">
              <button class="mail-chip ${this.currentFilter === 'all' ? 'active' : ''}" data-filter="all">All</button>
              <button class="mail-chip ${this.currentFilter === 'patch' ? 'active' : ''}" data-filter="patch">Patches</button>
              <button class="mail-chip ${this.currentFilter === 'cover' ? 'active' : ''}" data-filter="cover">Cover Letters</button>
              <button class="mail-chip ${this.currentFilter === 'pull' ? 'active' : ''}" data-filter="pull">Pull Requests</button>
            </div>
          </div>

          <div id="mail-status-banner" class="mail-status-banner" style="display:none;"></div>

          <div class="mail-results-list" id="mail-results-container">
            <div style="padding:16px;color:var(--text-muted);text-align:center;">
              Loading mailing list...
            </div>
          </div>
        </div>

        <!-- Right Column: Thread Conversation & Patch Viewer -->
        <div class="mail-detail-col" id="mail-detail-container">
          <div class="mail-empty-state">
            <span style="font-size:28px;">✉</span>
            <p>Select a thread from the list or search the Linux Kernel Mailing List to view conversations, patches, and replies.</p>
          </div>
        </div>
      </div>
    `;

    // 1. Wire search inputs and filter chips
    const searchInput = containerEl.querySelector("#mail-search-input");
    const clearBtn = containerEl.querySelector("#mail-search-clear-btn");
    if (this.currentQuery && searchInput) {
      searchInput.value = this.currentQuery;
      if (clearBtn) clearBtn.style.display = "block";
    }

    const debouncedSearch = debounce((q) => {
      this.currentQuery = q;
      if (clearBtn) clearBtn.style.display = q ? "block" : "none";
      this.loadSearch(true);
    }, 250);

    searchInput.oninput = () => {
      debouncedSearch(searchInput.value.trim());
    };

    if (clearBtn) {
      clearBtn.onclick = () => {
        searchInput.value = "";
        clearBtn.style.display = "none";
        this.currentQuery = "";
        this.loadSearch(true);
      };
    }

    containerEl.querySelectorAll(".mail-chip").forEach((chip) => {
      chip.onclick = () => {
        containerEl.querySelectorAll(".mail-chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        this.currentFilter = chip.dataset.filter;
        this.loadSearch(true);
      };
    });

    // 2. Initial checks & search in parallel
    this.checkDaemonStatus();

    // 3. If initial thread or message was requested, inspect it immediately
    if (this.activeThreadId > 0 || this.activeMessageId) {
      this.inspectThread(this.activeThreadId, this.activeMessageId);
    }
    this.loadSearch(true);
  }

  async checkDaemonStatus() {
    if (this._checkingDaemon || this.daemonStatus !== null) return;
    this._checkingDaemon = true;
    try {
      const st = await api.getMailStatus();
      this.daemonStatus = st;
      const banner = this.containerEl?.querySelector("#mail-status-banner");
      if (!banner) return;

      if (!st.available) {
        banner.style.display = "flex";
        banner.innerHTML = `
          <div style="display:flex;align-items:center;gap:8px;font-size:11px;color:var(--accent-yellow);flex:1;">
            <span>⚠️</span>
            <span>Mailing list backend is offline (${escapeHtml(st.reason || 'Not reachable')}). Showing cached threads.</span>
          </div>
          <button class="code-btn" id="mail-retry-status-btn" style="font-size:10px;padding:2px 8px;">Retry</button>
        `;
        const retryBtn = banner.querySelector("#mail-retry-status-btn");
        if (retryBtn) {
          retryBtn.onclick = () => {
            banner.style.display = "none";
            this.daemonStatus = null;
            this.checkDaemonStatus();
            this.loadSearch(true);
          };
        }
      } else {
        banner.style.display = "none";
      }
    } catch {
      // Ignore network errors in status check
    } finally {
      this._checkingDaemon = false;
    }
  }

  async loadSearch(reset = true) {
    const listContainer = this.containerEl.querySelector("#mail-results-container");
    if (!listContainer) return;

    if (reset) {
      this.currentPage = 1;
      this.results = [];
      listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);text-align:center;">Searching...</div>`;
    }

    this.isLoadingSearch = true;
    try {
      const data = await api.searchMailingList(this.currentQuery, {
        page: this.currentPage,
        limit: 50,
        filterType: this.currentFilter,
      });

      this.totalResults = data.total || 0;
      const newItems = data.results || [];
      if (reset) {
        this.results = newItems;
      } else {
        const known = new Set(this.results.map((r) => r.message_id || r.num));
        for (const item of newItems) {
          if (!known.has(item.message_id || item.num)) {
            this.results.push(item);
          }
        }
      }
      this.renderResultsList();
    } catch (e) {
      if (reset) {
        listContainer.innerHTML = `
          <div style="padding:16px;color:var(--accent-red);font-size:12px;">
            Search failed: ${escapeHtml(e.message)}
          </div>
        `;
      }
    } finally {
      this.isLoadingSearch = false;
    }
  }

  renderResultsList() {
    const listContainer = this.containerEl.querySelector("#mail-results-container");
    if (!listContainer) return;
    listContainer.innerHTML = "";

    if (!this.results.length) {
      listContainer.innerHTML = `
        <div style="padding:24px 16px;text-align:center;color:var(--text-muted);font-size:12px;">
          ${this.currentQuery ? `No emails matching "${escapeHtml(this.currentQuery)}"` : 'No emails found.'}
        </div>
      `;
      return;
    }

    this.results.forEach((item) => {
      const card = document.createElement("div");
      const isSelected = (item.tid && item.tid === this.activeThreadId) || (item.message_id && item.message_id === this.activeMessageId);
      card.className = `mail-result-card ${isSelected ? 'active' : ''}`;

      const dateStr = item.date_iso ? item.date_iso.split(" ")[0] : "";
      const authorShort = (item.author || "Unknown").split("<")[0].trim();

      let badgesHtml = "";
      if (item.is_pull) {
        badgesHtml += `<span class="mail-badge pull">GIT PULL</span>`;
      } else if (item.is_cover) {
        badgesHtml += `<span class="mail-badge cover">Cover [0/${item.patch_total || 'N'}]</span>`;
      } else if (item.is_patch) {
        const vStr = item.patch_version > 1 ? ` v${item.patch_version}` : "";
        const idxStr = item.patch_idx ? ` ${item.patch_idx}/${item.patch_total || '1'}` : "";
        badgesHtml += `<span class="mail-badge patch">PATCH${vStr}${idxStr}</span>`;
      }

      card.innerHTML = `
        <div class="mail-result-subject">${escapeHtml(item.subject || 'No Subject')}</div>
        <div class="mail-result-meta">
          <span class="mail-result-author" title="${escapeHtml(item.author)}">${escapeHtml(authorShort)}</span>
          <div style="display:flex;gap:6px;align-items:center;">
            ${badgesHtml}
            <span class="mail-result-date">${dateStr}</span>
          </div>
        </div>
      `;

      card.onclick = () => {
        this.containerEl.querySelectorAll(".mail-result-card").forEach((c) => c.classList.remove("active"));
        card.classList.add("active");
        this.inspectThread(item.tid, item.message_id);
      };

      card.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "mail",
            title: `LKML: ${(item.subject || "Thread").slice(0, 32)}`,
            threadId: item.tid,
            messageId: item.message_id,
            forceNew: true,
          });
        }
      });

      listContainer.appendChild(card);
    });

    // Pagination button
    if (this.results.length < this.totalResults) {
      const moreBtn = document.createElement("button");
      moreBtn.className = "code-btn";
      moreBtn.style.width = "100%";
      moreBtn.style.margin = "12px 0 24px 0";
      moreBtn.textContent = this.isLoadingSearch ? "Loading..." : `Load More (${this.results.length} of ${this.totalResults})`;
      moreBtn.disabled = this.isLoadingSearch;
      moreBtn.onclick = () => {
        this.currentPage++;
        this.loadSearch(false);
      };
      listContainer.appendChild(moreBtn);
    }
  }

  async inspectThread(tid, targetMid = "") {
    if (this.isLoadingThread) return;
    this.isLoadingThread = true;
    this.activeThreadId = tid || 0;
    this.activeMessageId = targetMid || "";
    const detailEl = this.containerEl?.querySelector("#mail-detail-container");
    if (!detailEl) {
      this.isLoadingThread = false;
      return;
    }

    detailEl.innerHTML = `<div style="padding:32px;color:var(--text-muted);text-align:center;">Loading thread discussion...</div>`;

    try {
      const threadData = await api.getMailThread(tid, targetMid);
      this.activeThreadData = threadData;
      this.activeThreadId = threadData.tid || tid;

      // Update active tab title in state & DOM directly without re-rendering tab content
      const title = threadData.subject ? `LKML: ${threadData.subject.slice(0, 30)}` : "LKML Thread";
      const activeTab = state.getActiveTab();
      if (activeTab && activeTab.type === "mail") {
        activeTab.title = title;
        activeTab.threadId = this.activeThreadId;
        activeTab.messageId = this.activeMessageId;
        state.saveState();
        const tabEl = document.querySelector(`.tab[data-tab-id="${activeTab.id}"] .tab-title`);
        if (tabEl) tabEl.textContent = title;
      }

      this.renderThreadView(detailEl, threadData, targetMid);
    } catch (e) {
      detailEl.innerHTML = `
        <div style="padding:32px;color:var(--accent-red);text-align:center;">
          Failed to load thread: ${escapeHtml(e.message)}
        </div>
      `;
    } finally {
      this.isLoadingThread = false;
    }
  }

  renderThreadView(container, threadData, targetMid = "") {
    const messages = threadData.messages || [];
    const rootSubj = threadData.subject || "Discussion Thread";
    const participants = threadData.participants || [];

    const cleanTargetMid = decodeURIComponent(targetMid || "").replace(/^<|>$/g, "").trim().toLowerCase();

    // Find the intended target message (or default to the first message)
    let targetIdx = -1;
    if (cleanTargetMid) {
      targetIdx = messages.findIndex((m) => {
        const mMid = decodeURIComponent(m.message_id || "").replace(/^<|>$/g, "").trim().toLowerCase();
        return mMid === cleanTargetMid || String(m.num) === cleanTargetMid;
      });
    }
    if (targetIdx === -1) {
      targetIdx = 0;
    }

    // Build timeline outline items
    let outlineHtml = "";
    messages.forEach((m, idx) => {
      let label = `Msg ${idx + 1}`;
      if (m.is_cover) label = `[0/${m.patch_total || 'N'}]`;
      else if (m.is_patch) label = `[${m.patch_idx || idx + 1}/${m.patch_total || '1'}]`;
      else if (m.is_pull) label = `[PULL]`;

      const isTarget = (idx === targetIdx);
      outlineHtml += `
        <button class="mail-outline-btn ${isTarget ? 'active' : ''}" data-mid="${escapeHtml(m.message_id || m.num)}" title="${escapeHtml(m.subject)}">
          ${escapeHtml(label)}
        </button>
      `;
    });

    container.innerHTML = `
      <div class="mail-thread-wrapper">
        <!-- Thread Header Bar -->
        <div class="mail-thread-header">
          <div class="mail-thread-title">${escapeHtml(rootSubj)}</div>
          <div class="mail-thread-meta">
            <span><strong>${messages.length}</strong> message${messages.length === 1 ? '' : 's'}</span>
            <span>·</span>
            <span><strong>${participants.length}</strong> contributor${participants.length === 1 ? '' : 's'}</span>
            ${threadData.lore_url ? `<span>·</span><a href="${threadData.lore_url}" target="_blank" class="lore-link">Lore.kernel.org ↗</a>` : ''}
          </div>
          <!-- Mini Timeline Outline -->
          ${messages.length > 1 ? `<div class="mail-thread-outline">${outlineHtml}</div>` : ''}
        </div>

        <!-- Messages Conversation Cards -->
        <div class="mail-messages-container" id="mail-cards-list"></div>
      </div>
    `;

    // Wire outline jump clicks
    container.querySelectorAll(".mail-outline-btn").forEach((btn) => {
      btn.onclick = () => {
        container.querySelectorAll(".mail-outline-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const targetId = btn.dataset.mid;
        const targetCard = Array.from(container.querySelectorAll(".mail-card")).find(
          (c) => c.dataset.mid === targetId
        );
        if (targetCard) {
          // Auto-expand card if collapsed
          const bodyEl = targetCard.querySelector(".mail-card-body");
          if (bodyEl && bodyEl.style.display === "none") {
            const toggle = targetCard.querySelector(".mail-card-toggle");
            if (toggle) toggle.click();
          }
          targetCard.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      };
    });

    const cardsContainer = container.querySelector("#mail-cards-list");
    let targetCardEl = null;
    messages.forEach((m, idx) => {
      const isTarget = (idx === targetIdx);
      const cardEl = this.createMessageCard(m, isTarget);
      cardsContainer.appendChild(cardEl);
      if (isTarget) {
        targetCardEl = cardEl;
        this.fetchAndRenderMessageDetail(m.message_id || m.num, cardEl);
      }
    });

    if (targetCardEl && targetIdx > 0) {
      setTimeout(() => {
        targetCardEl.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 50);
    }
  }

  createMessageCard(msgSummary, startExpanded = false) {
    const card = document.createElement("div");
    card.className = `mail-card ${startExpanded ? 'expanded' : ''}`;
    card.dataset.mid = msgSummary.message_id || msgSummary.num;

    const authorShort = (msgSummary.author || "Unknown").split("<")[0].trim();
    const authorEmail = (msgSummary.author || "").match(/<([^>]+)>/)?.[1] || "";
    const dateStr = msgSummary.date_iso || "";

    let badgeHtml = "";
    if (msgSummary.is_pull) {
      badgeHtml = `<span class="mail-badge pull">GIT PULL</span>`;
    } else if (msgSummary.is_cover) {
      badgeHtml = `<span class="mail-badge cover">Cover Letter [0/${msgSummary.patch_total || 'N'}]</span>`;
    } else if (msgSummary.is_patch) {
      const vStr = msgSummary.patch_version > 1 ? ` v${msgSummary.patch_version}` : "";
      const idxStr = msgSummary.patch_idx ? ` ${msgSummary.patch_idx}/${msgSummary.patch_total || '1'}` : "";
      badgeHtml = `<span class="mail-badge patch">PATCH${vStr}${idxStr}</span>`;
    }

    card.innerHTML = `
      <div class="mail-card-header">
        <div class="mail-card-header-left">
          <button class="mail-card-toggle" title="Toggle message body">
            ${startExpanded ? '▼' : '▶'}
          </button>
          <div style="display:flex;flex-direction:column;gap:2px;">
            <div style="display:flex;gap:8px;align-items:center;">
              <strong class="mail-author-name">${escapeHtml(authorShort)}</strong>
              ${authorEmail ? `<span class="mail-author-email">&lt;${escapeHtml(authorEmail)}&gt;</span>` : ''}
              ${badgeHtml}
            </div>
            <div class="mail-card-subject">${escapeHtml(msgSummary.subject || '')}</div>
          </div>
        </div>

        <div class="mail-card-header-right">
          <span class="mail-card-date">${dateStr}</span>
          <div class="mail-card-actions">
            <!-- View Mode Toggle -->
            <div class="mail-view-mode-toggle" title="Toggle Beautified / Raw RFC-822 view">
              <button class="mail-mode-btn active" data-mode="beautified">Beautified</button>
              <button class="mail-mode-btn" data-mode="raw">Raw</button>
            </div>
            <button class="code-btn mail-action-btn" data-act="copy" title="Copy RFC-822 payload to clipboard">📋 Copy</button>
            <button class="code-btn mail-action-btn" data-act="download" title="Download message as .eml / .patch">⬇ Download</button>
            <a href="https://lore.kernel.org/all/${encodeURIComponent(msgSummary.message_id)}/" target="_blank" class="lore-link" title="Open on lore.kernel.org">Lore ↗</a>
          </div>
        </div>
      </div>

      <div class="mail-card-body" style="display:${startExpanded ? 'block' : 'none'};">
        <div class="mail-card-loading">Loading message content...</div>
      </div>
    `;

    // 1. Toggle collapse on header click
    const headerEl = card.querySelector(".mail-card-header");
    const bodyEl = card.querySelector(".mail-card-body");
    const toggleBtn = card.querySelector(".mail-card-toggle");

    const toggleOpen = () => {
      const isCurrentlyOpen = bodyEl.style.display !== "none";
      if (isCurrentlyOpen) {
        bodyEl.style.display = "none";
        toggleBtn.textContent = "▶";
        card.classList.remove("expanded");
      } else {
        bodyEl.style.display = "block";
        toggleBtn.textContent = "▼";
        card.classList.add("expanded");
        const mid = msgSummary.message_id || msgSummary.num;
        if (!this.loadedMessageDetails.has(mid)) {
          this.fetchAndRenderMessageDetail(mid, card);
        }
      }
    };

    toggleBtn.onclick = (e) => {
      e.stopPropagation();
      toggleOpen();
    };

    headerEl.onclick = (e) => {
      // Avoid collapsing if user clicked buttons inside header
      if (e.target.closest("button, a, input")) return;
      toggleOpen();
    };

    // 2. Wire action buttons
    const modeBtns = card.querySelectorAll(".mail-mode-btn");
    modeBtns.forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        modeBtns.forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        const mode = b.dataset.mode;
        const mid = msgSummary.message_id || msgSummary.num;
        this.rawModeMap.set(mid, mode === "raw");
        const detail = this.loadedMessageDetails.get(mid);
        if (detail) {
          this.renderCardContent(bodyEl, detail, mode === "raw");
        }
      };
    });

    const copyBtn = card.querySelector('[data-act="copy"]');
    if (copyBtn) {
      copyBtn.onclick = async (e) => {
        e.stopPropagation();
        const mid = msgSummary.message_id || msgSummary.num;
        let detail = this.loadedMessageDetails.get(mid);
        if (!detail) {
          detail = await api.getMailMessage(mid);
        }
        const textToCopy = detail?.raw || detail?.body || "";
        if (textToCopy) {
          copyToClipboard(textToCopy);
          toast.success("Copied email RFC-822 to clipboard");
        } else {
          toast.error("No raw content available to copy");
        }
      };
    }

    const downloadBtn = card.querySelector('[data-act="download"]');
    if (downloadBtn) {
      downloadBtn.onclick = async (e) => {
        e.stopPropagation();
        const mid = msgSummary.message_id || msgSummary.num;
        let detail = this.loadedMessageDetails.get(mid);
        if (!detail) {
          detail = await api.getMailMessage(mid);
        }
        const content = detail?.raw || detail?.body || "";
        const filename = `${(detail?.subject || mid).replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 40)}.eml`;
        const blob = new Blob([content], { type: "message/rfc822" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
      };
    }

    return card;
  }

  async fetchAndRenderMessageDetail(mid, cardEl) {
    const bodyEl = cardEl.querySelector(".mail-card-body");
    if (!bodyEl) return;

    try {
      const detail = await api.getMailMessage(mid);
      this.loadedMessageDetails.set(mid, detail);
      const isRaw = this.rawModeMap.get(mid) || false;
      this.renderCardContent(bodyEl, detail, isRaw);
    } catch (e) {
      bodyEl.innerHTML = `
        <div style="padding:16px;color:var(--accent-red);font-size:12px;">
          Failed to load message: ${escapeHtml(e.message)}
        </div>
      `;
    }
  }

  renderCardContent(bodyEl, detail, isRaw) {
    bodyEl.innerHTML = "";
    if (!detail || detail.available === false || detail.error) {
      bodyEl.innerHTML = `
        <div class="mail-empty-message-error" style="padding:14px;background:rgba(255,200,0,0.08);border:1px solid rgba(255,200,0,0.25);border-radius:6px;margin:8px 0;">
          <div style="font-weight:600;color:var(--accent-yellow);margin-bottom:6px;display:flex;align-items:center;gap:6px;">
            <span>⚠️</span>
            <span>Content currently unavailable from local archive</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);margin-bottom:10px;">
            ${escapeHtml(detail?.error || "The message content could not be located in the local public-inbox mirror.")}
          </div>
          <div style="display:flex;gap:10px;align-items:center;">
            ${detail?.lore_url ? `<a href="${detail.lore_url}" target="_blank" class="lore-link" style="font-size:12px;">View on Lore.kernel.org ↗</a>` : ""}
            <button class="code-btn mail-retry-msg-btn" style="font-size:11px;padding:3px 10px;">Retry</button>
          </div>
        </div>
      `;
      const retryBtn = bodyEl.querySelector(".mail-retry-msg-btn");
      if (retryBtn) {
        retryBtn.onclick = (e) => {
          e.stopPropagation();
          bodyEl.innerHTML = `<div class="mail-card-loading">Retrying message content...</div>`;
          const parentCard = bodyEl.closest(".mail-card");
          const mid = parentCard?.dataset?.mid || detail?.message_id || detail?.num;
          if (parentCard && mid) {
            this.fetchAndRenderMessageDetail(mid, parentCard);
          }
        };
      }
      return;
    }

    if (isRaw) {
      const rawText = detail.raw || detail.body || "(No raw content)";
      bodyEl.innerHTML = `
        <div class="mail-raw-container">
          <pre class="mail-raw-pre"><code>${escapeHtml(rawText)}</code></pre>
        </div>
      `;
      return;
    }

    // Beautified View
    const headers = detail.headers || {};
    let headersSummaryHtml = "";
    if (Object.keys(headers).length > 0) {
      const headerKeys = ["from", "to", "cc", "date", "message-id", "in-reply-to"];
      const rows = headerKeys
        .filter((k) => headers[k])
        .map((k) => `<div><strong style="color:var(--text-secondary);">${escapeHtml(k.toUpperCase())}:</strong> ${escapeHtml(headers[k])}</div>`)
        .join("");

      if (rows) {
        headersSummaryHtml = `
          <details class="mail-headers-details">
            <summary class="mail-headers-summary">RFC-822 Headers (${Object.keys(headers).length})</summary>
            <div class="mail-headers-table">${rows}</div>
          </details>
        `;
      }
    }

    // Parse and beautify body lines (quotes, indentation, commentary)
    const bodyText = detail.body || "";
    const beautifiedBodyHtml = bodyText
      ? this.formatEmailBodyText(bodyText)
      : `<div style="padding:12px;color:var(--text-muted);font-style:italic;">(No text body available for this email)</div>`;

    // If message contains structured diff hunks, render the interactive patch viewer
    let diffHtml = "";
    if (detail.is_patch && detail.diff) {
      diffHtml = this.renderPatchDiff(detail.diff);
    }

    bodyEl.innerHTML = `
      ${headersSummaryHtml}
      <div class="mail-body-content">${beautifiedBodyHtml}</div>
      ${diffHtml}
    `;

    // Wire "Open in Code Editor" and diff hunk clicks
    if (detail.is_patch && detail.diff) {
      this.wireDiffInteractions(bodyEl, detail.diff);
    }
  }

  formatEmailBodyText(text) {
    if (!text) return "";
    const lines = text.split("\n");
    let result = "";
    let inQuote = false;
    let quoteBlock = [];

    const flushQuote = () => {
      if (quoteBlock.length > 0) {
        result += `<blockquote class="mail-quote-block">${quoteBlock.map(escapeHtml).join("<br/>")}</blockquote>`;
        quoteBlock = [];
      }
      inQuote = false;
    };

    for (const line of lines) {
      if (line.startsWith(">")) {
        inQuote = true;
        quoteBlock.push(line);
      } else {
        if (inQuote) flushQuote();
        if (line.trim().startsWith("Signed-off-by:") || line.trim().startsWith("Acked-by:") || line.trim().startsWith("Reviewed-by:") || line.trim().startsWith("Reported-by:")) {
          result += `<div class="mail-trailer-line">${escapeHtml(line)}</div>`;
        } else {
          result += `<div class="mail-text-line">${escapeHtml(line)}</div>`;
        }
      }
    }
    if (inQuote) flushQuote();

    return result;
  }

  renderPatchDiff(diff) {
    const files = diff.files || [];
    let html = `
      <div class="mail-patch-section">
        <div class="cdiff-summary">
          <span>${files.length} file${files.length === 1 ? '' : 's'} modified in patch</span>
          <span class="cdiff-add">+${diff.additions || 0}</span>
          <span class="cdiff-del">−${diff.deletions || 0}</span>
          <span class="cdiff-muted cdiff-hint">Click Open ↗ to view file in editor</span>
        </div>
    `;

    files.forEach((f, fi) => {
      const pathLabel = f.status === "R" && f.old_path !== f.path
        ? `${escapeHtml(f.old_path)} → ${escapeHtml(f.path)}`
        : escapeHtml(f.path);

      html += `
        <details class="cdiff-file" data-fi="${fi}" open>
          <summary>
            <span class="cdiff-status-badge" style="color:${STATUS_COLORS[f.status] || 'var(--text-secondary)'};">${escapeHtml(f.status || 'M')}</span>
            <span class="cdiff-path">${pathLabel}</span>
            <span class="cdiff-add">+${f.additions || 0}</span>
            <span class="cdiff-del">−${f.deletions || 0}</span>
            <button class="code-btn cdiff-open" data-fi="${fi}" title="Open this file in Code Editor">Open ↗</button>
          </summary>
          <div class="cdiff-body">
      `;

      if (f.binary) {
        html += `<div class="cdiff-status">Binary file</div>`;
      } else if (!f.hunks || !f.hunks.length) {
        html += `<div class="cdiff-status">No content changes</div>`;
      } else {
        f.hunks.forEach((h, hi) => {
          html += `<div class="cdiff-hunk" data-fi="${fi}" data-hi="${hi}">@@ -${h.old_start},${h.old_count} +${h.new_start},${h.new_count} @@ <span>${escapeHtml(h.heading || '')}</span></div>`;
          (h.lines || []).forEach(([k, oldNo, newNo, lineText], li) => {
            const cls = k === "+" ? "add" : k === "-" ? "del" : "ctx";
            html += `
              <div class="cdiff-row ${cls}" data-fi="${fi}" data-hi="${hi}" data-li="${li}">
                <span class="cdiff-ln">${oldNo ?? ""}</span>
                <span class="cdiff-ln">${newNo ?? ""}</span>
                <span class="cdiff-sign">${k === " " ? "" : k}</span>
                <span class="cdiff-text">${escapeHtml(lineText)}</span>
              </div>
            `;
          });
        });
      }

      html += `</div></details>`;
    });

    html += `</div>`;
    return html;
  }

  wireDiffInteractions(bodyEl, diff) {
    const files = diff.files || [];
    bodyEl.querySelectorAll(".cdiff-open").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const fi = parseInt(btn.dataset.fi, 10);
        const f = files[fi];
        if (f) {
          state.openTab({
            type: "code",
            title: f.path.split("/").pop(),
            path: f.path,
            cursorLine: f.hunks?.[0]?.new_start || 1,
            version: state.currentVersion,
            forceNew: true,
          });
        }
      };
    });

    bodyEl.querySelectorAll(".cdiff-row").forEach((row) => {
      row.onclick = (e) => {
        if (window.getSelection()?.toString()) return;
        const fi = parseInt(row.dataset.fi, 10);
        const f = files[fi];
        if (!f) return;
        const hi = parseInt(row.dataset.hi, 10);
        const li = parseInt(row.dataset.li, 10);
        const line = f.hunks?.[hi]?.lines?.[li];
        const lineNum = line ? (line[2] || line[1] || 1) : 1;
        state.openTab({
          type: "code",
          title: f.path.split("/").pop(),
          path: f.path,
          cursorLine: lineNum,
          version: state.currentVersion,
          forceNew: false,
        });
      };
    });
  }
}
