/**
 * maintainers_view.js - Subsystems Catalog, Maintainers Roster & CREDITS.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { toast } from "../../components/toast.js";
import { debounce } from "../../utils/debounce.js";

export class MaintainersView {
  constructor() {
    this.currentVersion = "v3.0";
    this.activeTab = "subsystems"; // 'subsystems' | 'developers' | 'credits' | 'patch'
    this.subsystems = [];
    this.developers = [];
    this.selectedSecId = null;
    this.selectedPersonId = null;
    this.currentDevRole = "all";
    this.currentDevSort = "activity";
    this.containerEl = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.currentVersion = tabData.version || state.currentVersion;

    containerEl.innerHTML = `
      <div class="maintainers-view-container">
        <!-- Sidebar Navigation -->
        <div class="maintainers-sidebar">
          <div class="maintainers-nav-tabs">
            <div class="m-nav-tab ${this.activeTab === "subsystems" ? "active" : ""}" data-tab="subsystems">Subsystems</div>
            <div class="m-nav-tab ${this.activeTab === "developers" ? "active" : ""}" data-tab="developers">Developers</div>
            <div class="m-nav-tab ${this.activeTab === "credits" ? "active" : ""}" data-tab="credits">CREDITS</div>
            <div class="m-nav-tab ${this.activeTab === "patch" ? "active" : ""}" data-tab="patch">Patch Reviewer</div>
          </div>
          <div style="padding:8px 12px;border-bottom:1px solid var(--border-color);">
            <input type="text" id="m-search-input" placeholder="Search subsystems, names, emails..." style="width:100%;" />
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
        <div class="maintainers-detail" id="m-detail-container">
          <div style="color:var(--text-muted);padding-top:40px;text-align:center;">
            Select a subsystem or developer to inspect maintainers and biographical records.
          </div>
        </div>
      </div>
    `;

    // Tab switching
    containerEl.querySelectorAll(".m-nav-tab").forEach((tab) => {
      tab.onclick = () => {
        this.activeTab = tab.dataset.tab;
        containerEl.querySelectorAll(".m-nav-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");

        const filterToolbar = containerEl.querySelector("#m-filter-toolbar");
        if (filterToolbar) {
          filterToolbar.style.display = this.activeTab === "developers" ? "flex" : "none";
        }

        const searchInput = containerEl.querySelector("#m-search-input");
        if (this.activeTab === "developers") {
          searchInput.placeholder = "Search developers, names, emails...";
        } else if (this.activeTab === "subsystems") {
          searchInput.placeholder = "Search subsystems, names, emails...";
        } else if (this.activeTab === "credits") {
          searchInput.placeholder = "Search CREDITS entries...";
        } else {
          searchInput.placeholder = "Search...";
        }

        this.loadList(searchInput.value.trim());
      };
    });

    // Developer role filter pills
    containerEl.querySelectorAll(".m-role-pill").forEach((pill) => {
      pill.onclick = () => {
        this.currentDevRole = pill.dataset.role;
        containerEl.querySelectorAll(".m-role-pill").forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
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
        const searchInput = containerEl.querySelector("#m-search-input");
        this.loadList(searchInput.value.trim());
      };
    }

    // Search filter
    const searchInput = containerEl.querySelector("#m-search-input");
    const debouncedSearch = debounce((q) => {
      this.loadList(q);
    }, 250);
    searchInput.oninput = () => {
      debouncedSearch(searchInput.value.trim());
    };

    await this.loadList();
    if (tabData.person) {
      await this.inspectPerson(tabData.person);
    }
  }

  async loadList(query = "") {
    const listContainer = this.containerEl.querySelector("#m-list-container");
    listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);">Searching...</div>`;

    if (this.activeTab === "subsystems") {
      try {
        const res = await api.getMaintainersOverview(this.currentVersion, query);
        this.subsystems = res.sections || [];
        this.renderSubsystemsList();
      } catch (e) {
        listContainer.innerHTML = `<div style="padding:16px;color:var(--accent-red);">Failed: ${e.message}</div>`;
      }
    } else if (this.activeTab === "developers") {
      try {
        const res = await api.getDevelopers(this.currentVersion, {
          query,
          role: this.currentDevRole,
          sort: this.currentDevSort,
        });
        this.developers = res.developers || [];
        this.renderDevelopersList(this.developers);
      } catch (e) {
        listContainer.innerHTML = `<div style="padding:16px;color:var(--accent-red);">Failed: ${e.message}</div>`;
      }
    } else if (this.activeTab === "credits") {
      try {
        const res = await api.getCredits(this.currentVersion, query);
        this.renderCreditsList(res.credits || res.entries || []);
      } catch (e) {
        listContainer.innerHTML = `<div style="padding:16px;color:var(--accent-red);">Failed: ${e.message}</div>`;
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
      card.className = `subsystem-card dev-card ${d.person_id === this.selectedPersonId ? "active" : ""}`;

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

      card.innerHTML = `
        <div class="subsystem-name">
          <span style="font-weight:600;">${this.escapeHtml(d.name || d.email || "Unknown")}</span>
          ${d.subsystems_count > 0 ? `<span class="dev-subsystems-count">${d.subsystems_count} ${d.subsystems_count === 1 ? "subsystem" : "subsystems"}</span>` : ""}
        </div>
        <div class="subsystem-lead" style="display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:4px;">
          <span class="dev-email-text" title="${this.escapeHtml(d.email || "")}">${this.escapeHtml(d.email || "")}</span>
          <div class="dev-badges-group">${roleBadges.join("")}</div>
        </div>
      `;

      card.onclick = () => {
        this.selectedPersonId = d.person_id;
        this.containerEl.querySelectorAll(".subsystem-card").forEach((c) => c.classList.remove("active"));
        card.classList.add("active");
        this.inspectPerson(d.person_id || d.email || d.name);
      };

      card.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "maintainers",
            title: `Person: ${d.name || d.email}`,
            version: this.currentVersion,
            person: d.person_id || d.email,
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
          <span>${sec.name}</span>
          ${sec.status ? `<span class="subsystem-status">${sec.status}</span>` : ""}
        </div>
        <div class="subsystem-lead">
          ${sec.maintainers_count ? `${sec.maintainers_count} Maintainers` : ""}
        </div>
      `;

      card.onclick = () => {
        this.selectedSecId = sec.sec_id;
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
            forceNew: true
          });
        }
      });

      listContainer.appendChild(card);
    });
  }

  async inspectSection(secId) {
    const detailEl = this.containerEl.querySelector("#m-detail-container");
    detailEl.innerHTML = `<div style="color:var(--text-muted);">Loading subsystem details...</div>`;

    try {
      const res = await api.getMaintainerSection(this.currentVersion, secId);
      const sec = res.section || res;

      detailEl.innerHTML = `
        <div class="m-section-header">
          <div class="m-section-title">${sec.name}</div>
          <div style="font-size:12px;color:var(--accent-cyan);margin-top:4px;">Status: ${sec.status || "Maintained"}</div>
        </div>

        <div class="m-grid-2col">
          <!-- Maintainers & Reviewers -->
          <div class="m-card">
            <div class="m-card-title">Maintainers & Reviewers</div>
            <div style="display:flex;flex-direction:column;gap:6px;">
              ${
                Array.isArray(sec.members) && sec.members.length > 0
                  ? sec.members.map((m) => `
                      <div class="person-item" data-id="${m.person_id || ''}" data-email="${m.email || ''}" data-name="${m.name || ''}" style="cursor:pointer;">
                        <div class="person-info">
                          <span class="person-name">${m.name}</span>
                          <span class="person-email">${m.email || ''}</span>
                        </div>

                        <span style="font-size:10px;padding:1px 5px;background:var(--bg-tertiary);border-radius:3px;">
                          ${m.role_name || (m.role_type === 1 ? "Maintainer" : "Reviewer")}
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
              <div><strong style="color:var(--text-muted);">Mailing List:</strong> ${sec.mailing_list || "None"}</div>
              <div><strong style="color:var(--text-muted);">Web Page:</strong> ${sec.web_page ? `<a href="${sec.web_page}" target="_blank" style="color:var(--accent-blue);">${sec.web_page}</a>` : "None"}</div>
              <div><strong style="color:var(--text-muted);">SCM Tree:</strong> ${sec.scm_tree || "None"}</div>
            </div>
          </div>
        </div>

        <!-- Files & Match Rules -->
        <div class="m-card">
          <div class="m-card-title">File Matching Rules (F: / X:)</div>
          <div style="display:flex;flex-direction:column;gap:4px;font-family:var(--font-mono);font-size:11px;">
            ${
              Array.isArray(sec.patterns) && sec.patterns.length > 0
                ? sec.patterns.map((p) => `
                    <div style="display:flex;gap:8px;">
                      <span style="color:var(--accent-orange);font-weight:bold;">${p.pattern_type}:</span>
                      <span>${p.pattern}</span>
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
                    <div class="m-file-link" data-path="${f.fname || f}" style="color:var(--accent-blue);cursor:pointer;padding:2px 0;">
                      📄 ${f.fname || f}
                    </div>
                  `).join("")
                : `<span style="color:var(--text-muted);">No directly matched indexed files recorded</span>`
            }
          </div>
        </div>
      `;

      // Developer click -> Person Profile
      detailEl.querySelectorAll(".person-item").forEach((el) => {
        el.onclick = () => this.inspectPerson(el.dataset.id || el.dataset.email || el.dataset.name);
        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "maintainers",
              title: `Person: ${el.dataset.name || el.dataset.email || "Profile"}`,
              version: this.currentVersion,
              forceNew: true
            });
          }
        });
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
      detailEl.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed: ${e.message}</div>`;
    }
  }

  renderCreditsList(credits) {
    const listContainer = this.containerEl.querySelector("#m-list-container");
    listContainer.innerHTML = "";

    credits.forEach((c) => {
      const card = document.createElement("div");
      card.className = "subsystem-card";
      card.innerHTML = `
        <div class="subsystem-name">
          <span>${c.name}</span>
        </div>
        <div class="subsystem-lead">${c.description || c.email || ""}</div>
      `;

      card.onclick = () => {
        this.containerEl.querySelectorAll(".subsystem-card").forEach((x) => x.classList.remove("active"));
        card.classList.add("active");
        this.inspectPerson(c.person_id || c.email || c.name);
      };

      listContainer.appendChild(card);
    });
  }

  async inspectPerson(emailOrName) {
    const detailEl = this.containerEl.querySelector("#m-detail-container");
    detailEl.innerHTML = `<div style="color:var(--text-muted);">Loading developer profile...</div>`;

    try {
      const p = await api.getPersonProfile(this.currentVersion, emailOrName);
      const stats = p.contribution_stats || {};
      const sections = p.sections || p.subsystems || [];

      detailEl.innerHTML = `
        <div class="m-section-header">
          <div class="m-section-title">${p.name}</div>
          <div style="font-size:12px;color:var(--text-muted);">${p.email || ""}</div>
        </div>

        <div class="m-grid-2col">
          <!-- Biographical / CREDITS -->
          <div class="m-card">
            <div class="m-card-title">CREDITS Record</div>
            <div style="display:flex;flex-direction:column;gap:8px;font-size:12px;">
              <div><strong style="color:var(--text-muted);">Description:</strong> ${p.credits?.description || "Not in CREDITS"}</div>
              ${p.credits?.web_page ? `<div><strong style="color:var(--text-muted);">Web Page:</strong> <a href="${p.credits.web_page}" target="_blank" style="color:var(--accent-blue);">${p.credits.web_page}</a></div>` : ""}
              ${p.credits?.pgp_key ? `<div><strong style="color:var(--text-muted);">PGP Key:</strong> <code style="font-size:11px;color:var(--accent-orange);">${p.credits.pgp_key}</code></div>` : ""}
              <div><strong style="color:var(--text-muted);">Snail Mail:</strong> ${p.credits?.snail_mail ? `<pre style="font-family:inherit;margin-top:2px;white-space:pre-wrap;">${p.credits.snail_mail}</pre>` : "None"}</div>
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
                      <span style="font-weight:500;color:var(--accent-blue);">${s.name}</span>
                      <span style="color:var(--text-muted);">${s.role_name || "Maintainer"}</span>
                    </div>
                  `).join("")
                : `<span style="color:var(--text-muted);">No indexed subsystems</span>`
            }
          </div>
        </div>

        <div style="margin-top:16px;display:flex;gap:8px;">
          <button id="btn-view-person-commits" class="code-btn active" style="font-size:12px;padding:6px 12px;cursor:pointer;">
            View Commits by ${this.escapeHtml(p.name || p.email)}
          </button>
        </div>
      `;

      // Clicking a maintained subsystem opens it
      detailEl.querySelectorAll(".sec-ref-item").forEach((el) => {
        el.onclick = async () => {
          this.activeTab = "subsystems";
          this.containerEl.querySelectorAll(".m-nav-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "subsystems"));
          const filterToolbar = this.containerEl.querySelector("#m-filter-toolbar");
          if (filterToolbar) filterToolbar.style.display = "none";
          this.selectedSecId = parseInt(el.dataset.id, 10);
          await this.loadList();
          this.inspectSection(el.dataset.id);
        };
        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "maintainers",
              title: `Subsystem: ${el.querySelector("span")?.textContent || "Detail"}`,
              version: this.currentVersion,
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
      detailEl.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed to load person profile: ${e.message}</div>`;
    }
  }

  escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
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
        const matchRes = await api.matchMaintainers(this.currentVersion, text);
        resContainer.innerHTML = `
          <div style="display:flex;flex-direction:column;gap:10px;">
            <div><strong>Suggested To:</strong></div>
            ${(matchRes.suggested_to || []).map((p) => `<div>• ${p.name} &lt;${p.email}&gt; (${p.role || "Maintainer"})</div>`).join("")}
            <div style="margin-top:8px;"><strong>Suggested Cc:</strong></div>
            ${(matchRes.suggested_cc || []).map((p) => `<div>• ${p.name || p.email} &lt;${p.email}&gt;</div>`).join("")}
          </div>
        `;
      } catch (e) {
        resContainer.innerHTML = `<div style="color:var(--accent-red);">Failed: ${e.message}</div>`;
      }
    };
  }
}
