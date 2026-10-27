/**
 * commits_view.js - Git Commit Timeline & Diff Viewer.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { debounce } from "../../utils/debounce.js";
import { showPersonModal } from "../../components/person_modal.js";
import { toast } from "../../components/toast.js";
import { requestVersionSwitch } from "../../components/version_switch.js";

const STATUS_COLORS = { A: "var(--accent-green)", D: "var(--accent-red)", R: "#d2a8ff", M: "var(--accent-yellow)" };
// Files whose diff is longer than this start collapsed in the inline commit diff
const AUTO_EXPAND_ROWS = 120;

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Collapse the line numbers of one diff side ("+" -> new, "-" -> old) into [[start, end], ...] blocks. */
function changedRanges(file, kind) {
  const nums = [];
  for (const h of file.hunks) {
    for (const [k, oldNo, newNo] of h.lines) {
      if (k === kind) nums.push(kind === "+" ? newNo : oldNo);
    }
  }
  const ranges = [];
  for (const n of nums) {
    const last = ranges[ranges.length - 1];
    if (last && n === last[1] + 1) last[1] = n;
    else ranges.push([n, n]);
  }
  return ranges;
}

export class CommitsView {
  constructor() {
    this.currentVersion = "v3.0";
    this.commits = [];
    this.selectedCommitId = null;
    this.containerEl = null;
    this.currentPage = 1;
    this.currentQuery = "";
    this.anchorCommit = null;
    this.hasPast = false;
    this.hasFuture = false;
    this.hasMore = false;
    this.nextPastCursor = null;
    this.nextFutureCursor = null;
    this.isLoadingTop = false;
    this.isLoadingBottom = false;
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
          <div class="commits-search-bar" style="display:flex;gap:6px;">
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
      this.loadCommits(q, true);
    }, 250);
    searchInput.oninput = () => {
      debouncedSearch(searchInput.value.trim());
    };

    await this.loadCommits(this.currentQuery, true);
    if (this.selectedCommitId) {
      this.inspectCommit(this.selectedCommitId);
    }
  }

  async loadCommits(query = "", reset = true) {
    const listContainer = this.containerEl.querySelector("#commit-list-container");
    if (reset) {
      this.currentPage = 1;
      this.commits = [];
      this.currentQuery = query;
      this.hasMore = true;
      this.hasPast = false;
      this.hasFuture = false;
      this.nextPastCursor = null;
      this.nextFutureCursor = null;
      listContainer.innerHTML = `<div style="padding:16px;color:var(--text-muted);">Loading commits...</div>`;
    }

    try {
      if (this.currentQuery) {
        const res = await api.getCommits(this.currentVersion, this.currentPage, 50, this.currentQuery);
        const newCommits = res.commits || [];
        this.commits = newCommits;
        this.hasMore = newCommits.length === 50;
      } else {
        const res = await api.getCommitTimeline(this.currentVersion, 50, "past", null);
        this.anchorCommit = res.anchor_commit || null;
        this.commits = res.timeline || [];
        this.hasPast = Boolean(res.has_past);
        this.hasFuture = Boolean(res.has_future);
        this.nextPastCursor = res.next_past_cursor;
        this.nextFutureCursor = res.next_future_cursor;
      }
      this.renderCommitList();
    } catch (e) {
      if (reset) {
        listContainer.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed: ${escapeHtml(e.message)}</div>`;
      }
    }
  }

  async loadMoreTop() {
    if (this.isLoadingTop || !this.hasFuture || this.currentQuery) return;
    this.isLoadingTop = true;
    const topBtn = this.containerEl.querySelector("#btn-load-more-top");
    if (topBtn) {
      topBtn.textContent = "Loading...";
      topBtn.disabled = true;
    }

    const listContainer = this.containerEl.querySelector("#commit-list-container");
    const prevScrollHeight = listContainer.scrollHeight;
    const prevScrollTop = listContainer.scrollTop;

    try {
      const res = await api.getCommitTimeline(this.currentVersion, 50, "future", this.nextFutureCursor);
      const newCommits = [...(res.timeline || [])].reverse();
      this.commits = [...newCommits, ...this.commits];
      this.hasFuture = Boolean(res.has_future);
      this.nextFutureCursor = res.next_future_cursor;
      this.renderCommitList();
      listContainer.scrollTop = prevScrollTop + (listContainer.scrollHeight - prevScrollHeight);
    } catch (e) {
      console.error("Failed to load more commits (top):", e);
      if (topBtn) {
        topBtn.textContent = "Failed to load - Retry";
        topBtn.disabled = false;
      }
    } finally {
      this.isLoadingTop = false;
    }
  }

  async loadMoreBottom() {
    if (this.isLoadingBottom) return;
    this.isLoadingBottom = true;
    const bottomBtn = this.containerEl.querySelector("#btn-load-more-bottom");
    if (bottomBtn) {
      bottomBtn.textContent = "Loading...";
      bottomBtn.disabled = true;
    }

    try {
      if (this.currentQuery) {
        this.currentPage += 1;
        const res = await api.getCommits(this.currentVersion, this.currentPage, 50, this.currentQuery);
        const newCommits = res.commits || [];
        this.commits.push(...newCommits);
        this.hasMore = newCommits.length === 50;
      } else {
        const res = await api.getCommitTimeline(this.currentVersion, 50, "past", this.nextPastCursor);
        const newCommits = res.timeline || [];
        this.commits.push(...newCommits);
        this.hasPast = Boolean(res.has_past);
        this.nextPastCursor = res.next_past_cursor;
      }
      this.renderCommitList();
    } catch (e) {
      console.error("Failed to load more commits (bottom):", e);
      if (bottomBtn) {
        bottomBtn.textContent = "Failed to load - Retry";
        bottomBtn.disabled = false;
      }
    } finally {
      this.isLoadingBottom = false;
    }
  }

  renderCommitList() {
    const listContainer = this.containerEl.querySelector("#commit-list-container");
    listContainer.innerHTML = "";

    // Top pagination button (timeline mode when there are commits above)
    if (!this.currentQuery && this.hasFuture) {
      const topBtn = document.createElement("button");
      topBtn.className = "code-btn";
      topBtn.id = "btn-load-more-top";
      topBtn.style.width = "100%";
      topBtn.style.margin = "0 0 12px 0";
      topBtn.textContent = this.isLoadingTop ? "Loading..." : "Load 50 More Commits";
      topBtn.disabled = this.isLoadingTop;
      topBtn.onclick = () => this.loadMoreTop();
      listContainer.appendChild(topBtn);
    }

    if (this.commits.length === 0) {
      const emptyDiv = document.createElement("div");
      emptyDiv.style.cssText = "padding:16px;color:var(--text-muted);";
      emptyDiv.textContent = this.currentQuery ? "No matching commits found." : "No commits found.";
      listContainer.appendChild(emptyDiv);
      return;
    }

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

    // Bottom pagination button
    const showBottomBtn = (!this.currentQuery && this.hasPast) || (this.currentQuery && this.hasMore);
    if (showBottomBtn) {
      const bottomBtn = document.createElement("button");
      bottomBtn.className = "code-btn";
      bottomBtn.id = "btn-load-more-bottom";
      bottomBtn.style.width = "100%";
      bottomBtn.style.margin = "12px 0 24px 0";
      bottomBtn.textContent = this.isLoadingBottom ? "Loading..." : "Load 50 More Commits";
      bottomBtn.disabled = this.isLoadingBottom;
      bottomBtn.onclick = () => this.loadMoreBottom();
      listContainer.appendChild(bottomBtn);
    }
  }

  async toggleCommitDiff(commitHash, btn, panel) {
    if (!panel.hidden) {
      panel.hidden = true;
      btn.textContent = "± Show changes";
      return;
    }
    panel.hidden = false;
    btn.textContent = "± Hide changes";
    if (panel.dataset.loaded === commitHash) return;
    panel.innerHTML = `<div class="cdiff-status">Loading diff…</div>`;
    try {
      const diff = await api.getCommitDiff(this.currentVersion, commitHash);
      panel.dataset.loaded = commitHash;
      this.renderCommitDiff(panel, diff);
    } catch (e) {
      panel.innerHTML = `<div class="cdiff-status" style="color:var(--accent-red);">Failed to load diff: ${escapeHtml(e.message)}</div>`;
    }
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  renderCommitDiff(panel, diff) {
    const files = diff.files || [];
    const short = (h) => (h || "").slice(0, 8);
    const base = diff.parent_hash ? `vs ${diff.is_merge ? "first parent " : ""}<span class="commit-hash">${short(diff.parent_hash)}</span>` : "root commit";
    let html = `<div class="cdiff-summary">
      <span>${files.length} file${files.length === 1 ? "" : "s"} changed</span>
      <span class="cdiff-add">+${diff.additions}</span><span class="cdiff-del">−${diff.deletions}</span>
      <span class="cdiff-muted">${base}</span>
      ${diff.truncated ? `<span class="cdiff-muted" title="Very large commit: only the first part of the diff is shown">(truncated)</span>` : ""}
      <span class="cdiff-muted cdiff-hint">Click a line to open the file there</span>
    </div>`;

    files.forEach((f, fi) => {
      const rows = f.hunks.reduce((n, h) => n + h.lines.length, 0);
      const pathLabel = f.status === "R" && f.old_path !== f.path
        ? `${escapeHtml(f.old_path)} → ${escapeHtml(f.path)}`
        : escapeHtml(f.path);
      html += `<details class="cdiff-file" data-fi="${fi}" ${rows <= AUTO_EXPAND_ROWS && fi < 25 ? "open" : ""}>
        <summary>
          <span class="cdiff-status-badge" style="color:${STATUS_COLORS[f.status] || "var(--text-secondary)"};">${escapeHtml(f.status)}</span>
          <span class="cdiff-path">${pathLabel}</span>
          <span class="cdiff-add">+${f.additions}</span><span class="cdiff-del">−${f.deletions}</span>
          <button class="code-btn cdiff-open" data-fi="${fi}" title="Open the file at the first change">Open ↗</button>
        </summary>
        <div class="cdiff-body">`;
      if (f.binary) {
        html += `<div class="cdiff-status">Binary file</div>`;
      } else if (!f.hunks.length) {
        html += `<div class="cdiff-status">${f.truncated ? "Diff omitted (commit too large)" : "No content changes (mode or rename only)"}</div>`;
      }
      f.hunks.forEach((h, hi) => {
        html += `<div class="cdiff-hunk" data-fi="${fi}" data-hi="${hi}" title="Open at this change">@@ -${h.old_start},${h.old_count} +${h.new_start},${h.new_count} @@ <span>${escapeHtml(h.heading)}</span></div>`;
        h.lines.forEach(([k, oldNo, newNo, text], li) => {
          const cls = k === "+" ? "add" : k === "-" ? "del" : "ctx";
          html += `<div class="cdiff-row ${cls}" data-fi="${fi}" data-hi="${hi}" data-li="${li}"><span class="cdiff-ln">${oldNo ?? ""}</span><span class="cdiff-ln">${newNo ?? ""}</span><span class="cdiff-sign">${k === " " ? "" : k}</span><span class="cdiff-text">${escapeHtml(text)}</span></div>`;
        });
      });
      if (f.truncated && f.hunks.length) html += `<div class="cdiff-status">… diff truncated</div>`;
      html += `</div></details>`;
    });
    if (!files.length) html += `<div class="cdiff-status">This commit has no file changes against its parent.</div>`;
    panel.innerHTML = html;

    const handle = (e) => {
      const el = e.target.closest(".cdiff-row, .cdiff-hunk, .cdiff-open");
      if (!el || !panel.contains(el)) return;
      if (e.type === "auxclick" && e.button !== 1) return;
      // Let users select diff text without being navigated away
      if (e.type === "click" && el.classList.contains("cdiff-row") && window.getSelection()?.toString()) return;
      e.preventDefault();
      e.stopPropagation();
      const f = files[parseInt(el.dataset.fi, 10)];
      if (!f) return;
      const forceNew = e.type === "auxclick" || e.ctrlKey || e.metaKey;
      if (el.classList.contains("cdiff-row")) {
        const [k, oldNo, newNo] = f.hunks[parseInt(el.dataset.hi, 10)].lines[parseInt(el.dataset.li, 10)];
        if (k === "-") this.openChange(diff, f, "-", oldNo, forceNew);
        else this.openChange(diff, f, "+", newNo, forceNew);
      } else {
        const h = el.classList.contains("cdiff-hunk") ? f.hunks[parseInt(el.dataset.hi, 10)] : f.hunks[0];
        this.openHunk(diff, f, h, forceNew);
      }
    };
    panel.addEventListener("click", handle);
    panel.addEventListener("auxclick", handle);
  }

  /** Open a hunk at its first changed line (on the old side for deletions). */
  openHunk(diff, file, hunk, forceNew) {
    if (!hunk) {
      this.openChange(diff, file, file.status === "D" ? "-" : "+", 1, forceNew);
      return;
    }
    const added = hunk.lines.find(([k]) => k === "+");
    const removed = hunk.lines.find(([k]) => k === "-");
    if (added || !removed) this.openChange(diff, file, "+", added ? added[2] : hunk.new_start, forceNew);
    else this.openChange(diff, file, "-", removed[1], forceNew);
  }

  /** "+"/context lines open the file at the commit; "-" lines open it at the parent with the removed lines marked. */
  openChange(diff, file, side, line, forceNew) {
    const removedSide = side === "-" || file.status === "D";
    const version = removedSide ? diff.parent_hash : diff.commit_hash;
    if (!version) {
      toast.info("This line has no previous version (root commit).");
      return;
    }
    const path = removedSide ? file.old_path : file.path;
    state.openTab({
      type: "code",
      title: path.split("/").pop(),
      path,
      version,
      cursorLine: Math.max(1, line || 1),
      diffMarks: { kind: removedSide ? "del" : "add", commit: diff.commit_hash, ranges: changedRanges(file, removedSide ? "-" : "+") },
      forceNew: Boolean(forceNew),
    });
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
            <div style="margin-top:8px;">
              <button class="code-btn active" id="btn-explore-commit" style="font-size:11px;padding:4px 10px;">
                🔍 Explore Files at this Commit (${c.commit_hash.slice(0, 8)})
              </button>
            </div>
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

        <!-- Inline diff of the commit (loaded on demand) -->
        <div class="commit-diff-section">
          <button class="code-btn" id="btn-commit-diff" title="Show the lines this commit changed; click a line to open the file there">± Show changes</button>
          <div class="commit-diff-panel" id="commit-diff-panel" hidden></div>
        </div>
      `;

      const diffBtn = detailEl.querySelector("#btn-commit-diff");
      const diffPanel = detailEl.querySelector("#commit-diff-panel");
      if (diffBtn && diffPanel && c.commit_hash) {
        diffBtn.onclick = () => this.toggleCommitDiff(c.commit_hash, diffBtn, diffPanel);
      }

      // Author and Contributor profile click handlers
      const authorDetailLink = detailEl.querySelector(".commit-detail-author-link");
      if (authorDetailLink) {
        authorDetailLink.onclick = () => {
          showPersonModal(this.currentVersion, c.author?.email || c.author_email || c.author?.name || c.author_name);
        };
      }

      const exploreCommitBtn = detailEl.querySelector("#btn-explore-commit");
      if (exploreCommitBtn && c.commit_hash) {
        exploreCommitBtn.onclick = async () => {
          // Declining leaves the explored revision (and any KConfig tab) alone; the commit still opens
          await requestVersionSwitch(c.commit_hash);
          toast.success(`Switched exploration target to commit ${c.commit_hash.slice(0, 8)}`);
          state.openTab({
            type: "code",
            title: "Makefile",
            path: "Makefile",
            version: c.commit_hash,
            forceNew: true,
          });
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
      detailEl.innerHTML = `<div style="color:var(--accent-red);padding:16px;">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }
}
