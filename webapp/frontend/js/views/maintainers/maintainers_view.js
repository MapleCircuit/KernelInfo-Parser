/**
 * maintainers_view.js - Subsystems Catalog, Maintainers Roster & CREDITS.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { debounce } from "../../utils/debounce.js";
import { escapeHtml } from "../../components/modal.js";
import { COLLAPSE_ICON } from "../../utils/icons.js";

/** Sections of the view's sidebar; the first is the default, left out of the URL (§5.26). */
const SECTIONS = ["subsystems", "developers", "credits", "patch"];
// A query starting with "@" (e.g. "@intel.com") filters by email domain, subdomains included (§4.5)
const SEARCH_PLACEHOLDERS = {
  subsystems: "Search subsystems, names, emails, @domain...",
  developers: "Search developers, names, emails, @domain...",
  credits: "Search CREDITS entries, @domain...",
  patch: "Search...",
};
const EMPTY_DETAIL = `
  <div style="color:var(--text-muted);padding-top:40px;text-align:center;">
    Select a subsystem or developer to inspect maintainers and biographical records.
  </div>`;

/** Key a person is addressed by: the email when known (stable across database rebuilds), else the id or name. */
function personKey(p) {
  return String(p.email || p.person_id || p.name || "");
}

const PATTERN_TAG_MAP = {
  1: "F:",
  2: "X:",
  3: "K:",
  4: "N:",
  5: "D:",
  FILE: "F:",
  EXCLUDE: "X:",
  KEYWORD: "K:",
  REGEX: "N:",
  DOC: "D:",
};

function formatPatternTag(type) {
  const str = String(type ?? "").trim();
  if (PATTERN_TAG_MAP[str]) return PATTERN_TAG_MAP[str];
  if (/^[A-Z]:?$/i.test(str)) return str.endsWith(":") ? str.toUpperCase() : `${str.toUpperCase()}:`;
  return str ? `${str}:` : "";
}

export class MaintainersView {
  constructor() {
    this.tabId = null;
    this.currentVersion = "v3.0";
    this.activeTab = "subsystems"; // 'subsystems' | 'developers' | 'credits' | 'patch'
    this.subsystems = [];
    this.developers = [];
    this.selectedSecId = null;
    this.selectedPerson = null; // personKey() of the inspected developer
    this.currentDevRole = "all";
    this.currentDevSort = "activity";
    this.listCollapsed = false;
    this.listLoaded = false;
    this.containerEl = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id || null;
    this.currentVersion = tabData.version || state.currentVersion;
    // The tab is the whole truth (§5.26): its section, search, developer filters and selection
    this.activeTab = SECTIONS.includes(tabData.section) ? tabData.section : "subsystems";
    this.currentDevRole = ["maintainer", "reviewer", "credits"].includes(tabData.role) ? tabData.role : "all";
    this.currentDevSort = tabData.sort === "alpha" ? "alpha" : "activity";
    this.selectedSecId = tabData.secId ? parseInt(tabData.secId, 10) || null : null;
    this.selectedPerson = this.selectedSecId ? null : (tabData.person ? String(tabData.person) : null);
    this.listCollapsed = Boolean(tabData.listCollapsed);
    this.listLoaded = false;
    const initialQuery = tabData.query || "";

    containerEl.innerHTML = `
      <div class="maintainers-view-container${this.listCollapsed ? " list-collapsed" : ""}">
        <!-- Folded sidebar: a rail on the left that unfolds it -->
        <button type="button" class="maintainers-list-rail" id="btn-maintainers-expand" title="Show maintainers" aria-label="Show maintainers">
          <span class="collapse-btn expand">${COLLAPSE_ICON}</span>
          <span class="maintainers-rail-label">Maintainers</span>
        </button>

        <!-- Sidebar Navigation -->
        <div class="maintainers-sidebar">
          <div class="maintainers-nav-tabs">
            <div class="m-nav-tab ${this.activeTab === "subsystems" ? "active" : ""}" data-tab="subsystems">Subsystems</div>
            <div class="m-nav-tab ${this.activeTab === "developers" ? "active" : ""}" data-tab="developers">Developers</div>
            <div class="m-nav-tab ${this.activeTab === "credits" ? "active" : ""}" data-tab="credits">CREDITS</div>
            <div class="m-nav-tab ${this.activeTab === "patch" ? "active" : ""}" data-tab="patch">Patch Reviewer</div>
          </div>
          <div class="maintainers-search-bar" style="display:flex;gap:6px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--border-color);">
            <input type="text" id="m-search-input" placeholder="${SEARCH_PLACEHOLDERS[this.activeTab]}" value="${escapeHtml(initialQuery)}" style="flex:1;" />
            <button type="button" class="collapse-btn" id="btn-maintainers-collapse" title="Collapse maintainers" aria-label="Collapse maintainers">${COLLAPSE_ICON}</button>
          </div>
          <!-- Developers Filter & Sort Toolbar -->
          <div id="m-filter-toolbar" class="m-filter-toolbar" style="${this.activeTab === "developers" ? "display:flex;" : "display:none;"}">
            <div class="m-role-pills" id="m-role-pills">
              <span class="m-role-pill ${this.currentDevRole === "all" ? "active" : ""}" data-role="all">All</span>
              <span class="m-role-pill ${this.currentDevRole === "maintainer" ? "active" : ""}" data-role="maintainer">Maintainers</span>
              <span class="m-role-pill ${this.currentDevRole === "reviewer" ? "active" : ""}" data-role="reviewer">Reviewers</span>
              <span class="m-role-pill ${this.currentDevRole === "credits" ? "active" : ""}" data-role="credits">CREDITS</span>
            </div>
            <button id="btn-dev-sort" class="btn-dev-sort" title="Toggle Sort (Activity / A-Z)">
              ${this.currentDevSort === "alpha" ? "🔤 A-Z" : "⚡ Activity"}
            </button>
          </div>
          <div class="maintainers-list" id="m-list-container">
            <div style="padding:16px;color:var(--text-muted);">Loading subsystems...</div>
          </div>
        </div>

        <!-- Detail Column -->
        <div class="maintainers-detail" id="m-detail-container">${EMPTY_DETAIL}</div>
      </div>
    `;

    // Tab switching
    containerEl.querySelectorAll(".m-nav-tab").forEach((tab) => {
      tab.onclick = () => {
        if (tab.dataset.tab === this.activeTab) return;
        const leavingPatch = this.activeTab === "patch";
        this.setSection(tab.dataset.tab);
        if (this.activeTab === "patch") {
          // The reviewer replaces the detail column: nothing is inspected any more
          this.setSelection(null, null);
        } else if (leavingPatch) {
          containerEl.querySelector("#m-detail-container").innerHTML = EMPTY_DETAIL;
        }
        this.loadList(containerEl.querySelector("#m-search-input").value.trim());
      };
    });

    // Developer role filter pills
    containerEl.querySelectorAll(".m-role-pill").forEach((pill) => {
      pill.onclick = () => {
        this.currentDevRole = pill.dataset.role;
        containerEl.querySelectorAll(".m-role-pill").forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
        this.persistTabState({ role: this.currentDevRole === "all" ? undefined : this.currentDevRole });
        const searchInput = containerEl.querySelector("#m-search-input");
        this.loadList(searchInput.value.trim());
      };
    });

    // Developer sort toggle
    const sortBtn = containerEl.querySelector("#btn-dev-sort");
    if (sortBtn) {
      sortBtn.onclick = () => {
        this.currentDevSort = this.currentDevSort === "activity" ? "alpha" : "activity";
        sortBtn.textContent = this.currentDevSort === "alpha" ? "🔤 A-Z" : "⚡ Activity";
        this.persistTabState({ sort: this.currentDevSort === "alpha" ? "alpha" : undefined });
        const searchInput = containerEl.querySelector("#m-search-input");
        this.loadList(searchInput.value.trim());
      };
    }

    // Search filter
    const searchInput = containerEl.querySelector("#m-search-input");
    const debouncedSearch = debounce((q) => {
      this.persistTabState({ query: q });
      this.loadList(q);
    }, 250);
    searchInput.oninput = () => {
      debouncedSearch(searchInput.value.trim());
    };

    containerEl.querySelector("#btn-maintainers-expand").onclick = () => this.setListCollapsed(false);
    containerEl.querySelector("#btn-maintainers-collapse").onclick = () => this.setListCollapsed(true);

    // The selection is requested beside the list, so neither waits for the other
    const detail = this.activeTab === "patch" ? null
      : this.selectedSecId ? this.inspectSection(this.selectedSecId)
      : this.selectedPerson ? this.inspectPerson(this.selectedPerson)
      : null;
    const list = this.listCollapsed ? null : this.loadList(initialQuery);
    await Promise.all([list, detail]);
  }

  /** Fold the sidebar to a rail on the left, or unfold it (loading it the first time). */
  async setListCollapsed(collapsed) {
    this.listCollapsed = collapsed;
    this.containerEl.querySelector(".maintainers-view-container")?.classList.toggle("list-collapsed", collapsed);
    this.persistTabState({ listCollapsed: collapsed || undefined });
    if (collapsed) return;
    this.containerEl.querySelector("#m-search-input")?.focus();
    if (this.listLoaded) return;
    await this.loadList(this.containerEl.querySelector("#m-search-input")?.value?.trim() || "");
    this.containerEl.querySelector(".subsystem-card.active")?.scrollIntoView({ block: "nearest" });
  }

  /**
   * Record on this tab (and so in its URL, §5.26) what this view is showing. A view whose
   * element was replaced by a re-render is stale: its state is nobody's address any more.
   */
  persistTabState(patch) {
    if (!this.containerEl || !this.containerEl.isConnected) return null;
    return state.updateTabState(this.tabId, patch);
  }

  /** Switch the sidebar section (nav tab, filter toolbar, search placeholder) and record it. */
  setSection(section) {
    this.activeTab = section;
    this.containerEl.querySelectorAll(".m-nav-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === section));
    const filterToolbar = this.containerEl.querySelector("#m-filter-toolbar");
    if (filterToolbar) filterToolbar.style.display = section === "developers" ? "flex" : "none";
    const searchInput = this.containerEl.querySelector("#m-search-input");
    if (searchInput) searchInput.placeholder = SEARCH_PLACEHOLDERS[section];
    this.persistTabState({ section: section === "subsystems" ? undefined : section });
  }

  /** The inspected subsystem or developer (one at a time), recorded on the tab. */
  setSelection(secId, person) {
    this.selectedSecId = secId ? parseInt(secId, 10) : null;
    this.selectedPerson = this.selectedSecId ? null : (person ? String(person) : null);
    this.persistTabState({ secId: this.selectedSecId || undefined, person: this.selectedPerson || undefined });
  }

  async loadList(query = "") {
    this.listLoaded = true;
    const listContainer = this.containerEl.querySelector("#m-list-container");
    listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);">Searching...</div>`;

    if (this.activeTab === "subsystems") {
      try {
        const res = await this.api.getMaintainersOverview(this.currentVersion, query);
        this.subsystems = res.sections || [];
        this.renderSubsystemsList();
        if (!this.subsystems.length) {
          listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);">${
            res.indexed === false ? "The MAINTAINERS file of this revision has not been parsed." : "No matching subsystem."
          }</div>`;
        }
      } catch (e) {
        listContainer.innerHTML = `<div style="padding:16px;color:var(--accent-red);">Failed: ${escapeHtml(e.message)}</div>`;
      }
    } else if (this.activeTab === "developers") {
      try {
        const res = await this.api.getDevelopers(this.currentVersion, {
          query,
          role: this.currentDevRole,
          sort: this.currentDevSort,
        });
        this.developers = res.developers || [];
        this.renderDevelopersList(this.developers);
      } catch (e) {
        listContainer.innerHTML = `<div style="padding:16px;color:var(--accent-red);">Failed: ${escapeHtml(e.message)}</div>`;
      }
    } else if (this.activeTab === "credits") {
      try {
        const res = await this.api.getCredits(this.currentVersion, query);
        this.renderCreditsList(res.credits || res.entries || []);
      } catch (e) {
        listContainer.innerHTML = `<div style="padding:16px;color:var(--accent-red);">Failed: ${escapeHtml(e.message)}</div>`;
      }
    } else if (this.activeTab === "patch") {
      this.renderPatchReviewer();
    }
  }

  renderDevelopersList(developers) {
    const listContainer = this.containerEl.querySelector("#m-list-container");
    listContainer.innerHTML = "";

    if (!developers || developers.length === 0) {
      listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);text-align:center;">No developers matching the criteria.</div>`;
      return;
    }

    developers.forEach((d) => {
      const card = document.createElement("div");
      card.className = `subsystem-card dev-card ${personKey(d) === this.selectedPerson ? "active" : ""}`;

      const roleBadges = [];
      if (d.is_maintainer) {
        roleBadges.push(`<span class="m-role-badge m-role-badge-m">Maintainer</span>`);
      }
      if (d.is_reviewer) {
        roleBadges.push(`<span class="m-role-badge m-role-badge-r">Reviewer</span>`);
      }
      if (d.in_credits) {
        roleBadges.push(`<span class="m-role-badge m-role-badge-c">CREDITS</span>`);
      }

      // Under an @domain search, show the address that matched instead of the primary one
      const matched = d.matched_emails || [];
      const shownEmail = matched[0] || d.email || "";
      const emailTitle = matched.length ? matched.join(", ") : shownEmail;
      const moreMatched = matched.length > 1 ? ` +${matched.length - 1}` : "";

      card.innerHTML = `
        <div class="subsystem-name">
          <span style="font-weight:600;">${escapeHtml(d.name || d.email || "Unknown")}</span>
          ${d.subsystems_count > 0 ? `<span class="dev-subsystems-count">${d.subsystems_count} ${d.subsystems_count === 1 ? "subsystem" : "subsystems"}</span>` : ""}
        </div>
        <div class="subsystem-lead" style="display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:4px;">
          <span class="dev-email-text" title="${escapeHtml(emailTitle)}">${escapeHtml(shownEmail)}${moreMatched}</span>
          <div class="dev-badges-group">${roleBadges.join("")}</div>
        </div>
      `;

      card.onclick = () => {
        this.containerEl.querySelectorAll(".subsystem-card").forEach((c) => c.classList.remove("active"));
        card.classList.add("active");
        this.inspectPerson(personKey(d));
      };

      card.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "maintainers",
            title: `Person: ${d.name || d.email}`,
            version: this.currentVersion,
            person: personKey(d),
            forceNew: true
          });
        }
      });

      listContainer.appendChild(card);
    });
  }

  renderSubsystemsList() {
    const listContainer = this.containerEl.querySelector("#m-list-container");
    listContainer.innerHTML = "";

    this.subsystems.forEach((sec) => {
      const card = document.createElement("div");
      card.className = `subsystem-card ${sec.sec_id === this.selectedSecId ? "active" : ""}`;
      card.innerHTML = `
        <div class="subsystem-name">
          <span>${escapeHtml(sec.name)}</span>
          ${sec.status ? `<span class="subsystem-status">${escapeHtml(sec.status)}</span>` : ""}
        </div>
        <div class="subsystem-lead">
          ${sec.maintainers_count ? `${escapeHtml(sec.maintainers_count)} Maintainers` : ""}
        </div>
      `;

      card.onclick = () => {
        this.containerEl.querySelectorAll(".subsystem-card").forEach((c) => c.classList.remove("active"));
        card.classList.add("active");
        this.inspectSection(sec.sec_id);
      };

      card.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "maintainers",
            title: `Subsystem: ${sec.name}`,
            version: this.currentVersion,
            secId: sec.sec_id,
            forceNew: true
          });
        }
      });

      listContainer.appendChild(card);
    });
  }

  async inspectSection(secId) {
    this.setSelection(secId, null);
    const detailEl = this.containerEl.querySelector("#m-detail-container");
    detailEl.innerHTML = `<div style="color:var(--text-muted);">Loading subsystem details...</div>`;

    try {
      const res = await this.api.getMaintainerSection(this.currentVersion, secId);
      const sec = res.section || res;

      detailEl.innerHTML = `
        <div class="m-section-header">
          <div class="m-section-title">${escapeHtml(sec.name)}</div>
          <div style="font-size:12px;color:var(--accent-cyan);margin-top:4px;">Status: ${escapeHtml(sec.status || "Maintained")}</div>
        </div>

        <div class="m-grid-2col">
          <!-- Maintainers & Reviewers -->
          <div class="m-card">
            <div class="m-card-title">Maintainers & Reviewers</div>
            <div style="display:flex;flex-direction:column;gap:6px;">
              ${
                Array.isArray(sec.members) && sec.members.length > 0
                  ? sec.members.map((m) => `
                      <div class="person-item" data-id="${escapeHtml(m.person_id || '')}" data-email="${escapeHtml(m.email || '')}" data-name="${escapeHtml(m.name || '')}" style="cursor:pointer;">
                        <div class="person-info">
                          <span class="person-name">${escapeHtml(m.name)}</span>
                          <span class="person-email">${escapeHtml(m.email || '')}</span>
                        </div>

                        <span style="font-size:10px;padding:1px 5px;background:var(--bg-tertiary);border-radius:3px;">
                          ${escapeHtml(m.role_name || (m.role_type === 1 ? "Maintainer" : "Reviewer"))}
                        </span>
                      </div>
                    `).join("")
                  : `<span style="color:var(--text-muted);">None listed</span>`
              }
            </div>
          </div>

          <!-- Section Metadata -->
          <div class="m-card">
            <div class="m-card-title">Subsystem Metadata</div>
            <div style="display:flex;flex-direction:column;gap:8px;font-size:12px;">
              <div><strong style="color:var(--text-muted);">Mailing List:</strong> ${escapeHtml(sec.mailing_list || "None")}</div>
              <div><strong style="color:var(--text-muted);">Web Page:</strong> ${sec.web_page ? `<a href="${escapeHtml(sec.web_page)}" target="_blank" style="color:var(--accent-blue);">${escapeHtml(sec.web_page)}</a>` : "None"}</div>
              <div><strong style="color:var(--text-muted);">SCM Tree:</strong> ${escapeHtml(sec.scm_tree || "None")}</div>
              ${sec.patchwork ? `<div><strong style="color:var(--text-muted);">Patchwork:</strong> <a href="${escapeHtml(sec.patchwork)}" target="_blank" style="color:var(--accent-blue);">${escapeHtml(sec.patchwork)}</a></div>` : ""}
              ${sec.bug_url ? `<div><strong style="color:var(--text-muted);">Bug Tracker:</strong> <a href="${escapeHtml(sec.bug_url)}" target="_blank" style="color:var(--accent-blue);">${escapeHtml(sec.bug_url)}</a></div>` : ""}
              ${sec.chat_channel ? `<div><strong style="color:var(--text-muted);">Chat Channel:</strong> ${escapeHtml(sec.chat_channel)}</div>` : ""}
              ${sec.profile_doc ? `<div><strong style="color:var(--text-muted);">Profile Doc:</strong> <span class="m-profile-doc-link" data-path="${escapeHtml(sec.profile_doc)}" style="color:var(--accent-blue);cursor:pointer;">${escapeHtml(sec.profile_doc)}</span></div>` : ""}
            </div>
          </div>
        </div>

        <!-- Files & Match Rules -->
        <div class="m-card">
          <div class="m-card-title">File Matching Rules (F: / X: / N: / K: / D:)</div>
          <div style="display:flex;flex-direction:column;gap:4px;font-family:var(--font-mono);font-size:11px;">
            ${
              Array.isArray(sec.patterns) && sec.patterns.length > 0
                ? sec.patterns.map((p) => `
                    <div style="display:flex;gap:8px;">
                      <span style="color:var(--accent-orange);font-weight:bold;">${escapeHtml(formatPatternTag(p.pattern_type || p.pat_type || p.type))}</span>
                      <span>${escapeHtml(p.pattern)}</span>
                    </div>
                  `).join("")
                : `<span style="color:var(--text-muted);">No explicit file rules</span>`
            }
          </div>
        </div>

        <!-- Matched Repository Files Roster -->
        <div class="m-card" style="margin-top:16px;">
          <div class="m-card-title">Matched Repository Files (${Array.isArray(sec.files) ? sec.files.length : 0})</div>
          <div style="max-height:220px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;font-family:var(--font-mono);font-size:11px;">
            ${
              Array.isArray(sec.files) && sec.files.length > 0
                ? sec.files.map((f) => `
                    <div class="m-file-link" data-path="${escapeHtml(f.fname || f)}" style="color:var(--accent-blue);cursor:pointer;padding:2px 0;">
                      📄 ${escapeHtml(f.fname || f)}
                    </div>
                  `).join("")
                : `<span style="color:var(--text-muted);">No directly matched indexed files recorded</span>`
            }
          </div>
        </div>
      `;

      // Developer click -> Person Profile
      detailEl.querySelectorAll(".person-item").forEach((el) => {
        el.onclick = () => this.inspectPerson(el.dataset.email || el.dataset.id || el.dataset.name);
        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "maintainers",
              title: `Person: ${el.dataset.name || el.dataset.email || "Profile"}`,
              version: this.currentVersion,
              person: el.dataset.email || el.dataset.id || el.dataset.name,
              forceNew: true
            });
          }
        });
      });

      // Documentation click -> Open in Code View
      detailEl.querySelectorAll(".m-profile-doc-link").forEach((link) => {
        link.onclick = () => {
          state.openTab({
            type: "code",
            title: link.dataset.path.split("/").pop(),
            path: link.dataset.path,
            version: this.currentVersion,
          });
        };
      });

      // File click -> Open in Code View
      detailEl.querySelectorAll(".m-file-link").forEach((link) => {
        link.onclick = () => {
          state.openTab({
            type: "code",
            title: link.dataset.path.split("/").pop(),
            path: link.dataset.path,
            version: this.currentVersion
          });
        };
        link.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "code",
              title: link.dataset.path.split("/").pop(),
              path: link.dataset.path,
              version: this.currentVersion,
              forceNew: true
            });
          }
        });
      });

    } catch (e) {
      detailEl.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  renderCreditsList(credits) {
    const listContainer = this.containerEl.querySelector("#m-list-container");
    listContainer.innerHTML = "";

    credits.forEach((c) => {
      const card = document.createElement("div");
      card.className = `subsystem-card ${personKey(c) === this.selectedPerson ? "active" : ""}`;
      card.innerHTML = `
        <div class="subsystem-name">
          <span>${escapeHtml(c.name)}</span>
        </div>
        <div class="subsystem-lead">${escapeHtml(c.description || c.email || "")}</div>
      `;

      card.onclick = () => {
        this.containerEl.querySelectorAll(".subsystem-card").forEach((x) => x.classList.remove("active"));
        card.classList.add("active");
        this.inspectPerson(personKey(c));
      };

      listContainer.appendChild(card);
    });
  }

  async inspectPerson(emailOrName) {
    this.setSelection(null, emailOrName);
    const detailEl = this.containerEl.querySelector("#m-detail-container");
    detailEl.innerHTML = `<div style="color:var(--text-muted);">Loading developer profile...</div>`;

    try {
      const p = await this.api.getPersonProfile(this.currentVersion, emailOrName);
      const stats = p.contribution_stats || {};
      const sections = p.sections || p.subsystems || [];

      detailEl.innerHTML = `
        <div class="m-section-header">
          <div class="m-section-title">${escapeHtml(p.name)}</div>
          <div style="font-size:12px;color:var(--text-muted);">${escapeHtml(p.email || "")}</div>
        </div>

        <div class="m-grid-2col">
          <!-- Biographical / CREDITS -->
          <div class="m-card">
            <div class="m-card-title">CREDITS Record</div>
            <div style="display:flex;flex-direction:column;gap:8px;font-size:12px;">
              <div><strong style="color:var(--text-muted);">Description:</strong> ${escapeHtml(p.credits?.description || "Not in CREDITS")}</div>
              ${p.credits?.web_page ? `<div><strong style="color:var(--text-muted);">Web Page:</strong> <a href="${escapeHtml(p.credits.web_page)}" target="_blank" style="color:var(--accent-blue);">${escapeHtml(p.credits.web_page)}</a></div>` : ""}
              ${p.credits?.pgp_key ? `<div><strong style="color:var(--text-muted);">PGP Key:</strong> <code style="font-size:11px;color:var(--accent-orange);">${escapeHtml(p.credits.pgp_key)}</code></div>` : ""}
              <div><strong style="color:var(--text-muted);">Snail Mail:</strong> ${p.credits?.snail_mail ? `<pre style="font-family:inherit;margin-top:2px;white-space:pre-wrap;">${escapeHtml(p.credits.snail_mail)}</pre>` : "None"}</div>
            </div>
          </div>

          <!-- Git Contribution Stats -->
          <div class="m-card">
            <div class="m-card-title">Git Contribution Stats</div>
            <div style="display:flex;flex-direction:column;gap:6px;font-size:12px;">
              <div><strong style="color:var(--text-muted);">Authored Commits:</strong> ${stats.authored_commits || 0}</div>
              <div><strong style="color:var(--text-muted);">Signed-off Commits:</strong> ${stats.signed_off_commits || 0}</div>
              <div><strong style="color:var(--text-muted);">Reviewed Commits:</strong> ${stats.reviewed_commits || 0}</div>
            </div>
          </div>
        </div>

        <!-- Maintained Subsystems -->
        <div class="m-card" style="margin-top:16px;">
          <div class="m-card-title">Maintained Subsystems (${sections.length})</div>
          <div style="display:flex;flex-direction:column;gap:6px;font-size:12px;">
            ${
              sections.length > 0
                ? sections.map((s) => `
                    <div class="sec-ref-item" data-id="${s.sec_id}" style="cursor:pointer;padding:4px 8px;border-radius:3px;background:var(--bg-tertiary);display:flex;justify-content:space-between;">
                      <span style="font-weight:500;color:var(--accent-blue);">${escapeHtml(s.name)}</span>
                      <span style="color:var(--text-muted);">${escapeHtml(s.role_name || "Maintainer")}</span>
                    </div>
                  `).join("")
                : `<span style="color:var(--text-muted);">No indexed subsystems</span>`
            }
          </div>
        </div>

        <div style="margin-top:16px;display:flex;gap:8px;">
          <button id="btn-view-person-commits" class="code-btn active" style="font-size:12px;padding:6px 12px;cursor:pointer;">
            View Commits by ${escapeHtml(p.name || p.email)}
          </button>
        </div>
      `;

      // Clicking a maintained subsystem opens it
      detailEl.querySelectorAll(".sec-ref-item").forEach((el) => {
        el.onclick = async () => {
          this.setSection("subsystems");
          this.selectedSecId = parseInt(el.dataset.id, 10);
          // The subsystems list is shown whole, so the search box no longer applies
          const searchInput = this.containerEl.querySelector("#m-search-input");
          if (searchInput) searchInput.value = "";
          this.persistTabState({ query: undefined });
          this.inspectSection(el.dataset.id);
          await this.loadList();
        };
        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "maintainers",
              title: `Subsystem: ${el.querySelector("span")?.textContent || "Detail"}`,
              version: this.currentVersion,
              secId: parseInt(el.dataset.id, 10),
              forceNew: true
            });
          }
        });
      });

      const commitsBtn = detailEl.querySelector("#btn-view-person-commits");
      if (commitsBtn) {
        commitsBtn.onclick = () => {
          state.openTab({
            type: "commits",
            title: `Commits: ${p.name || p.email}`,
            version: this.currentVersion,
            query: p.name || p.email,
          });
        };
      }
    } catch (e) {
      detailEl.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed to load person profile: ${escapeHtml(e.message)}</div>`;
    }
  }

  renderPatchReviewer() {
    const detailEl = this.containerEl.querySelector("#m-detail-container");
    const listContainer = this.containerEl.querySelector("#m-list-container");
    listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);">Paste patch on the right to analyze recipients.</div>`;

    detailEl.innerHTML = `
      <div class="patch-analyzer-box">
        <div style="font-weight:600;font-size:14px;color:var(--accent-orange);">Patch Reviewer (get_maintainer.pl Simulator)</div>
        <textarea class="patch-input-textarea" id="patch-text-input" placeholder="Paste git format-patch or unified diff here..."></textarea>
        <button class="code-btn active" id="btn-run-match" style="align-self:flex-start;">Analyze Recipients</button>
        <div id="patch-match-results" style="margin-top:12px;"></div>
      </div>
    `;

    detailEl.querySelector("#btn-run-match").onclick = async () => {
      const text = detailEl.querySelector("#patch-text-input").value.trim();
      const resContainer = detailEl.querySelector("#patch-match-results");
      resContainer.innerHTML = `<div style="color:var(--text-muted);">Analyzing patch...</div>`;

      try {
        const matchRes = await this.api.matchMaintainers(this.currentVersion, text);
        const toList = (matchRes.suggested_to || []).map((p) => {
          const str = typeof p === "string" ? p : `${p.name || p.email} <${p.email || ""}> (${p.role || "Maintainer"})`;
          return `<div>• ${escapeHtml(str)}</div>`;
        }).join("");
        const ccList = (matchRes.suggested_cc || []).map((p) => {
          const str = typeof p === "string" ? p : `${p.name || p.email} <${p.email || ""}>`;
          return `<div>• ${escapeHtml(str)}</div>`;
        }).join("");

        resContainer.innerHTML = `
          <div style="display:flex;flex-direction:column;gap:10px;">
            <div><strong>Suggested To:</strong></div>
            ${toList || "<div style='color:var(--text-muted);'>None</div>"}
            <div style="margin-top:8px;"><strong>Suggested Cc:</strong></div>
            ${ccList || "<div style='color:var(--text-muted);'>None</div>"}
            ${matchRes.skipped_files_count ? `<div style="margin-top:8px;color:var(--accent-yellow);">${escapeHtml(matchRes.skipped_files_count)} more file(s) of the patch were not matched (per-patch file limit).</div>` : ""}
          </div>
        `;
      } catch (e) {
        resContainer.innerHTML = `<div style="color:var(--accent-red);">Failed: ${escapeHtml(e.message)}</div>`;
      }
    };
  }
}
