/**
 * commits_view.js - Git Commit Timeline & Diff Viewer.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { debounce } from "../../utils/debounce.js";
import { showPersonModal } from "../../components/person_modal.js";

export class CommitsView {
  constructor() {
    this.currentVersion = "v3.0";
    this.commits = [];
    this.selectedCommitId = null;
    this.containerEl = null;
    this.currentPage = 1;
    this.currentQuery = "";
    this.hasMore = true;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.currentVersion = tabData.version || state.currentVersion;
    if (tabData.commitId) {
      this.selectedCommitId = tabData.commitId;
    }
    if (tabData.query) {
      this.currentQuery = tabData.query;
    }

    containerEl.innerHTML = `
      <div class="commits-view-container">
        <!-- Commits List Column -->
        <div class="commits-list-col">
          <div class="commits-search-bar">
            <input type="text" id="commit-search-input" placeholder="Search commit subject, author, hash..." style="flex:1;" />
          </div>
          <div class="commits-list" id="commit-list-container">
            <div style="padding:16px;color:var(--text-muted);">Loading commits...</div>
          </div>
        </div>

        <!-- Commit Detail Column -->
        <div class="commit-detail-col" id="commit-detail-container">
          <div style="color:var(--text-muted);padding-top:40px;text-align:center;">
            Select a commit from the timeline to view message, trailers, touched files, and diff.
          </div>
        </div>
      </div>
    `;

    const searchInput = containerEl.querySelector("#commit-search-input");
    if (this.currentQuery && searchInput) {
      searchInput.value = this.currentQuery;
    }

    const debouncedSearch = debounce((q) => {
      this.loadCommits(q, false);
    }, 250);
    searchInput.oninput = () => {
      debouncedSearch(searchInput.value.trim());
    };

    await this.loadCommits(this.currentQuery, false);
    if (this.selectedCommitId) {
      this.inspectCommit(this.selectedCommitId);
    }
  }

  async loadCommits(query = "", append = false) {
    const listContainer = this.containerEl.querySelector("#commit-list-container");
    if (!append) {
      this.currentPage = 1;
      this.commits = [];
      this.currentQuery = query;
      this.hasMore = true;
      listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);">Loading commits...</div>`;
    }

    try {
      const res = await api.getCommits(this.currentVersion, this.currentPage, 50, this.currentQuery);
      const newCommits = res.commits || [];
      if (append) {
        this.commits.push(...newCommits);
      } else {
        this.commits = newCommits;
      }
      this.hasMore = newCommits.length === 50;
      this.renderCommitList();
    } catch (e) {
      if (!append) {
        listContainer.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed: ${e.message}</div>`;
      }
    }
  }

  renderCommitList() {
    const listContainer = this.containerEl.querySelector("#commit-list-container");
    listContainer.innerHTML = "";

    this.commits.forEach((c) => {
      const card = document.createElement("div");
      card.className = `commit-card ${c.commit_id === this.selectedCommitId || c.commit_hash === this.selectedCommitId ? "active" : ""}`;

      const dateStr = c.author_date ? new Date(c.author_date * 1000).toISOString().split("T")[0] : "";
      const hashShort = (c.commit_hash || "0000000").substring(0, 7);
      const authorName = c.author?.name || c.author_name || "Unknown";
      const authorIdentifier = c.author?.email || c.author_email || authorName;

      card.innerHTML = `
        <div class="commit-subject">${c.subject || "No subject"}</div>
        <div class="commit-meta">
          <span class="commit-author-link" style="color:var(--text-secondary);cursor:pointer;transition:color 0.15s;" title="View developer profile: ${authorName}">${authorName}</span>
          <div style="display:flex;gap:6px;align-items:center;">
            <span>${dateStr}</span>
            <span class="commit-hash">${hashShort}</span>
          </div>
        </div>
      `;

      const authorLink = card.querySelector(".commit-author-link");
      if (authorLink) {
        authorLink.onmouseenter = () => { authorLink.style.color = "var(--accent-blue)"; };
        authorLink.onmouseleave = () => { authorLink.style.color = "var(--text-secondary)"; };
        authorLink.onclick = (e) => {
          e.stopPropagation();
          showPersonModal(this.currentVersion, authorIdentifier);
        };
      }

      card.onclick = () => {
        this.selectedCommitId = c.commit_id || c.commit_hash;
        this.containerEl.querySelectorAll(".commit-card").forEach((x) => x.classList.remove("active"));
        card.classList.add("active");
        this.inspectCommit(this.selectedCommitId);
      };

      card.addEventListener("auxclick", (e) => {
        if (e.button === 1) {
          e.preventDefault();
          e.stopPropagation();
          state.openTab({
            type: "commits",
            title: `Commit ${hashShort}`,
            commitId: c.commit_hash || c.commit_id,
            version: this.currentVersion,
            forceNew: true
          });
        }
      });

      listContainer.appendChild(card);
    });

    if (this.hasMore) {
      const moreBtn = document.createElement("button");
      moreBtn.className = "code-btn";
      moreBtn.style.width = "100%";
      moreBtn.style.margin = "12px 0 24px 0";
      moreBtn.textContent = "Load More Commits (50)";
      moreBtn.onclick = async () => {
        moreBtn.textContent = "Loading...";
        this.currentPage += 1;
        await this.loadCommits(this.currentQuery, true);
      };
      listContainer.appendChild(moreBtn);
    }
  }

  async inspectCommit(commitIdOrHash) {
    const detailEl = this.containerEl.querySelector("#commit-detail-container");
    detailEl.innerHTML = `<div style="color:var(--text-muted);">Loading commit details...</div>`;

    try {
      const c = await api.getCommitDetail(this.currentVersion, commitIdOrHash);
      const dateStr = c.author_date ? new Date(c.author_date * 1000).toUTCString() : "";

      detailEl.innerHTML = `
        <div class="commit-header">
          <div class="commit-detail-title">${c.subject || "Commit"}</div>
          <div class="commit-detail-meta">
            <div><strong>Author:</strong> <span class="commit-detail-author-link" style="color:var(--accent-blue);cursor:pointer;text-decoration:underline;" title="Click to view developer profile">${c.author?.name || c.author_name} &lt;${c.author?.email || c.author_email}&gt;</span></div>
            <div><strong>Date:</strong> ${dateStr}</div>
            <div><strong>Hash:</strong> <span class="commit-hash">${c.commit_hash}</span></div>
            ${c.lore_url ? `<div><a href="${c.lore_url}" target="_blank" class="lore-link">Lore.kernel.org LKML Thread ↗</a></div>` : ""}
          </div>
        </div>

        <!-- Contributors & Signoffs (Trailers) -->
        ${(() => {
          const contribList = Array.isArray(c.contributors) && c.contributors.length > 0 ? c.contributors : (Array.isArray(c.trailers) ? c.trailers : []);
          if (!contribList.length) return "";
          return `
            <div class="m-card" style="margin-bottom:16px;">
              <div class="m-card-title">Contributors &amp; Signoffs (${contribList.length})</div>
              <div style="display:flex;flex-direction:column;gap:6px;font-size:11px;">
                ${contribList.map((contrib) => {
                  const roles = Array.isArray(contrib.roles) ? contrib.roles : [contrib.role_name || (contrib.role_type === 4 ? "Signed-off-by" : (contrib.role_type === 5 ? "Reviewed-by" : (contrib.role_type === 6 ? "Acked-by" : (contrib.role_type === 8 ? "Reported-by" : "Contributor"))))];
                  const badgesHtml = roles.map((role) => {
                    let color = "var(--accent-blue)";
                    if (role.toLowerCase().includes("signed-off")) color = "var(--accent-green)";
                    else if (role.toLowerCase().includes("reported") || role.toLowerCase().includes("suggested")) color = "var(--accent-orange)";
                    else if (role.toLowerCase().includes("co-developed")) color = "#d2a8ff";
                    return `<span style="color:${color};font-weight:bold;font-size:10px;background:rgba(255,255,255,0.05);padding:2px 6px;border-radius:3px;border:1px solid rgba(255,255,255,0.1);">${role}</span>`;
                  }).join(" ");

                  const contribId = contrib.email || contrib.name;
                  return `
                    <div class="commit-contributor-item" data-person="${contribId}" style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:var(--bg-tertiary);border-radius:4px;cursor:pointer;transition:background 0.15s;" title="Click to view developer profile">
                      <span><strong style="color:var(--accent-blue);">${contrib.name}</strong> &lt;${contrib.email || ""}&gt;</span>
                      <div style="display:flex;gap:4px;align-items:center;">${badgesHtml}</div>
                    </div>
                  `;
                }).join("")}
              </div>
            </div>
          `;
        })()}

        <!-- Full Commit Message -->
        <div class="m-card">
          <div class="m-card-title">Commit Message</div>
          <pre style="font-family:var(--font-mono);font-size:12px;white-space:pre-wrap;color:var(--text-primary);line-height:1.4;">${c.message || c.subject}</pre>
        </div>

        <!-- Modified Files -->
        <div class="m-card">
          <div class="m-card-title">Modified Files (${c.files?.length || 0})</div>
          <div style="display:flex;flex-direction:column;gap:6px;font-family:var(--font-mono);font-size:11px;">
            ${
              Array.isArray(c.files) && c.files.length > 0
                ? c.files.map((f) => `
                    <div style="display:flex;gap:8px;align-items:center;">
                      <span style="font-weight:bold;color:${f.change_type === 'M' ? 'var(--accent-yellow)' : 'var(--accent-green)'};">[${f.change_type}]</span>
                      <span class="commit-file-link" data-path="${f.file}" style="color:var(--accent-blue);cursor:pointer;">${f.file}</span>
                    </div>
                  `).join("")
                : `<span style="color:var(--text-muted);">No modified files recorded</span>`
            }
          </div>
        </div>
      `;

      // Author and Contributor profile click handlers
      const authorDetailLink = detailEl.querySelector(".commit-detail-author-link");
      if (authorDetailLink) {
        authorDetailLink.onclick = () => {
          showPersonModal(this.currentVersion, c.author?.email || c.author_email || c.author?.name || c.author_name);
        };
      }

      detailEl.querySelectorAll(".commit-contributor-item").forEach((item) => {
        item.onmouseenter = () => { item.style.background = "rgba(255,255,255,0.08)"; };
        item.onmouseleave = () => { item.style.background = "var(--bg-tertiary)"; };
        item.onclick = () => {
          showPersonModal(this.currentVersion, item.dataset.person);
        };
      });

      // Jump to file
      detailEl.querySelectorAll(".commit-file-link").forEach((link) => {
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
}
