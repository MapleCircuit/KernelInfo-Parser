/**
 * callgraph_view.js - Bidirectional Callgraph Explorer.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";
import { escapeHtml } from "../../components/modal.js";

export class CallgraphView {
  constructor() {
    this.currentVersion = "v3.0";
    this.functionName = "schedule";
    this.containerEl = null;
    this.tabId = null;
    this._seq = 0;
    this._pathSeq = 0;
    this._suggestTimer = null;
  }

  dispose() {
    this._seq++;
    this._pathSeq++;
    clearTimeout(this._suggestTimer);
    this._suggestTimer = null;
    this.containerEl = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.tabId = tabData.id;
    this.currentVersion = tabData.version || state.currentVersion;
    if (tabData.functionName) {
      this.functionName = tabData.functionName.replace(/\(\)$/, "").trim();
    }

    containerEl.innerHTML = `
      <div class="tools-view-container">
        <div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border-color);padding-bottom:12px;">
          <div>
            <div style="font-size:var(--fs-18, 18px);font-weight:600;color:var(--accent-purple);">
              Callgraph: <span class="callgraph-header-name" style="font-family:var(--font-mono);">${escapeHtml(this.functionName)}()</span>
            </div>
            <div style="font-size:var(--fs-12, 12px);color:var(--text-muted);margin-top:4px;">
              Bidirectional caller/callee cross-reference explorer.
            </div>
          </div>
          <div style="display:flex;gap:8px;">
            <input type="text" id="callgraph-input" value="${escapeHtml(this.functionName)}" placeholder="Function name..." />
            <button type="button" class="code-btn active" id="callgraph-btn">Explore</button>
            <button type="button" class="code-btn" id="callgraph-btn-nodemap" title="Open interactive DAG in NodeMap">🗺️ Open in NodeMap</button>
            <button type="button" class="code-btn" id="callgraph-btn-path" title="Shortest call chains from this function to another (N2)">⇢ Find path to…</button>
            <button type="button" class="code-btn" id="callgraph-btn-ops" title="Indirect callers: the ops tables (struct initializers) that hold this function in a function-pointer field (N1)">🧩 Indirect callers (ops tables)</button>
          </div>
        </div>

        <div id="callgraph-path" class="callpath-panel" hidden>
          <div class="callpath-form">
            <span>Path from <code class="callpath-from"></code> to</span>
            <input type="text" id="callpath-to" placeholder="target function" spellcheck="false" list="callpath-suggest" />
            <datalist id="callpath-suggest"></datalist>
            <label class="kconfig-muted">depth <input type="number" id="callpath-depth" min="1" max="10" value="6" /></label>
            <input type="text" id="callpath-exclude" placeholder="skip: printk, kmalloc" spellcheck="false" title="Functions not to go through (hubs)" />
            <label class="kconfig-muted" title="Also follow calls through struct members (x->ops->read(...)) to the functions ops tables assign to that field. Matched by field name: approximate, and it widens the search"><input type="checkbox" id="callpath-indirect" /> through ops tables</label>
            <button class="code-btn active" id="callpath-go" type="button">Find</button>
            <button class="code-btn" id="callpath-close" type="button" title="Close" aria-label="Close">✕</button>
          </div>
          <div class="callpath-status kconfig-muted" aria-live="polite"></div>
          <div class="callpath-results"></div>
        </div>
        <div id="callgraph-results" style="flex:1;display:flex;gap:20px;">
          <div style="color:var(--text-muted);">Loading callgraph...</div>
        </div>
      </div>
    `;

    containerEl.querySelector("#callgraph-btn-ops").onclick = async () => {
      const { showOpsModal } = await import("../code_view/ops_modal.js");
      if (!this.containerEl?.isConnected) return;
      showOpsModal({ version: this.currentVersion, name: this.functionName });
    };
    this.bindCallPath(tabData.pathTo || "");
    const btn = containerEl.querySelector("#callgraph-btn");
    const input = containerEl.querySelector("#callgraph-input");

    const triggerSearch = () => {
      this.functionName = input.value.trim() || "schedule";
      this.loadCallgraph();
    };

    btn.onclick = triggerSearch;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        triggerSearch();
      }
    });

    await this.loadCallgraph();
  }

  /** Call path finder (N2): `GET /webapi/tools/{v}/callpath`, shortest chains with the call site of every hop. */
  bindCallPath(initialTo) {
    const c = this.containerEl;
    const panel = c.querySelector("#callgraph-path");
    const toInput = c.querySelector("#callpath-to");
    const status = c.querySelector(".callpath-status");
    const results = c.querySelector(".callpath-results");
    const open = (show) => {
      panel.hidden = !show;
      c.querySelector(".callpath-from").textContent = `${this.functionName}()`;
      if (show) toInput.focus();
    };
    c.querySelector("#callgraph-btn-path").onclick = () => open(panel.hidden);
    c.querySelector("#callpath-close").onclick = () => {
      open(false);
      state.updateTabState(this.tabId, { pathTo: "" });
    };
    toInput.addEventListener("input", () => {
      clearTimeout(this._suggestTimer);
      const q = toInput.value.trim();
      if (q.length < 3) return;
      const seq = this._seq;
      this._suggestTimer = setTimeout(async () => {
        try {
          const res = await this.api.searchSymbols(this.currentVersion, q, 15);
          if (seq !== this._seq || !this.containerEl?.isConnected) return;
          c.querySelector("#callpath-suggest").innerHTML = (res.symbols || []).map((s) => `<option value="${escapeHtml(s.name || s.symbol_name || "")}">`).join("");
        } catch {
          // suggestions are optional
        }
      }, 250);
    });
    const run = async () => {
      const seq = ++this._pathSeq;
      const to = toInput.value.trim();
      if (!/^[A-Za-z_]\w*$/.test(to)) {
        status.textContent = "Type a function name";
        return;
      }
      state.updateTabState(this.tabId, { pathTo: to });
      const params = new URLSearchParams({ from: this.functionName, to, max_depth: c.querySelector("#callpath-depth").value || "6", k: "5" });
      const exclude = c.querySelector("#callpath-exclude").value.split(/[\s,]+/).filter(Boolean).join(",");
      if (exclude) params.set("exclude", exclude);
      const indirect = c.querySelector("#callpath-indirect").checked;
      if (indirect) params.set("indirect", "1");
      status.textContent = `Searching call chains ${this.functionName} → ${to}…`;
      results.innerHTML = "";
      let res;
      try {
        res = await this.api.requestJob(`/webapi/tools/${encodeURIComponent(this.currentVersion)}/callpath?${params}`, {
          // 202 while the revision's source index is assembled (the first search of a revision, §2.19)
          timeoutMs: 600000, onProgress: (p) => { if (seq === this._pathSeq && this.containerEl?.isConnected) status.textContent = `Indexing the sources of ${this.currentVersion}: ${p?.note || "…"} ${p?.total ? `${p.done ?? 0}/${p.total}` : ""}`; },
        });
      } catch (err) {
        if (seq !== this._pathSeq || !this.containerEl?.isConnected) return;
        status.textContent = `Search failed: ${err.message}`;
        return;
      }
      if (seq !== this._pathSeq || !this.containerEl?.isConnected) return;
      const why = res.timed_out ? " (time limit reached)" : res.budget_exhausted ? " (expansion budget reached)" : "";
      const missing = !res.found?.from ? ` · ${this.functionName} is not defined or called in this revision` : !res.found?.to ? ` · ${to} is not defined or called in this revision` : "";
      status.textContent = res.paths.length
        ? `${res.paths.length} shortest chain${res.paths.length === 1 ? "" : "s"} of ${res.paths[0].length - 1} call${res.paths[0].length === 2 ? "" : "s"} · ${res.explored} functions expanded · ${res.elapsed_ms} ms · ARCH ${res.arch}${indirect ? " · hops through ops tables are dashed (matched by field name)" : " · calls through function pointers are not followed"}`
        : `No call chain within depth ${res.depth}${why} · ${res.explored} functions expanded${missing}`;
      results.innerHTML = res.paths.map((p) => `<div class="callpath-row">${p.map((h, i) => {
        const prev = p[i - 1];
        const arrow = !i ? "" : prev?.via
          ? `<span class="callpath-arrow callpath-indirect" title="Through a function pointer: ${escapeHtml(prev.via)}">⇢ <small>${escapeHtml(prev.via)}</small></span>`
          : '<span class="callpath-arrow">→</span>';
        return `${arrow}<span class="callpath-hop${prev?.via ? " via" : ""}">
          <a href="#" data-fn="${escapeHtml(h.function)}" title="Open the call graph of ${escapeHtml(h.function)}${h.file ? ` (static in ${escapeHtml(h.file)})` : ""}">${escapeHtml(h.function)}</a>${h.file ? `<span class="callpath-site" title="A static function or macro of this file">${escapeHtml(h.file.split("/").pop())}</span>` : ""}${h.path ? `<a href="#" class="callpath-site" data-path="${escapeHtml(h.path)}" data-line="${h.line}" title="The call in ${escapeHtml(h.function)}">${escapeHtml(h.path.split("/").pop())}:${h.line}</a>` : ""}</span>`;
      }).join("")}</div>`).join("");
    };
    c.querySelector("#callpath-go").onclick = run;
    toInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") run();
    });
    results.addEventListener("click", (e) => {
      const a = e.target.closest("a");
      if (!a) return;
      e.preventDefault();
      const forceNew = e.ctrlKey || e.metaKey;
      if (a.dataset.path) {
        state.openTab({ type: "code", title: a.dataset.path.split("/").pop(), path: a.dataset.path, cursorLine: Number(a.dataset.line), version: this.currentVersion, forceNew });
      } else if (a.dataset.fn) {
        state.openTab({ type: "callgraph", title: `Callgraph: ${a.dataset.fn}`, functionName: a.dataset.fn, version: this.currentVersion, forceNew: true });
      }
    });
    if (initialTo) {
      toInput.value = initialTo;
      open(true);
      run();
    }
  }

  async loadCallgraph() {
    const seq = ++this._seq;
    const headerNameEl = this.containerEl.querySelector(".callgraph-header-name");
    if (headerNameEl) {
      headerNameEl.textContent = `${this.functionName}()`;
    }
    const inputEl = this.containerEl.querySelector("#callgraph-input");
    if (inputEl) {
      inputEl.value = this.functionName;
    }

    const tab = (this.tabId ? state.findTab(this.tabId) : null) || state.getActiveTab();
    if (tab && tab.type === "callgraph") {
      state.updateTabState(tab.id, {
        functionName: this.functionName,
        title: `Callgraph: ${this.functionName}`
      });
    }

    const resContainer = this.containerEl.querySelector("#callgraph-results");
    try {
      const res = await this.api.getCallgraph(this.currentVersion, this.functionName);
      if (seq !== this._seq || !this.containerEl?.isConnected) return;
      const callers = res.callers || [];
      const callees = res.callees || [];

      // Wire Open in NodeMap button
      const nmBtn = this.containerEl.querySelector("#callgraph-btn-nodemap");
      if (nmBtn) {
        nmBtn.onclick = () => {
          const fn = (name, id, x, y, color) => ({ id, type: "symbol", x, y, color, data: { name, kind: "func" } });
          const initialNodes = [fn(this.functionName, "target-root", 420, 180, "purple")];
          const initialEdges = [];
          callers.forEach((c, idx) => {
            const cId = `caller-${idx}`;
            initialNodes.push(fn(c.name || c.caller_name || c.caller || `caller_${idx}`, cId, 80, 80 + idx * 140, "purple"));
            initialEdges.push({ from: cId, to: "target-root" });
          });
          callees.forEach((c, idx) => {
            const cId = `callee-${idx}`;
            initialNodes.push(fn(c.name || c.callee_name || c.callee || `callee_${idx}`, cId, 760, 80 + idx * 140, "blue"));
            initialEdges.push({ from: "target-root", to: cId });
          });

          state.openTab({
            type: "nodemap",
            title: `Callgraph: ${this.functionName}`,
            version: this.currentVersion,
            initialNodes,
            initialEdges
          });
        };
      }

      resContainer.innerHTML = `
        <!-- Callers (Functions calling this) -->
        <div class="callgraph-col">
          <div class="callgraph-col-header">
            <span>Incoming Callers (${callers.length})</span>
          </div>
          <div class="callgraph-node-list">
            ${
              callers.length > 0
                ? callers.map((c) => {
                    const funcName = c.name || c.caller_name || c.caller || "";
                    const filePath = c.file_path || c.file_name || c.file || "";
                    const safeFunc = escapeHtml(funcName);
                    const safeFile = escapeHtml(filePath);
                    return `
                    <div class="callgraph-node-item" style="display:flex;justify-content:space-between;align-items:center;">
                      <div class="callgraph-jump" data-func="${safeFunc}" style="cursor:pointer;flex:1;">
                        <span style="font-weight:500;">${safeFunc}()</span>
                        ${(c.call_count && c.call_count > 1) ? `<span style="font-size:var(--fs-10, 10px);padding:1px 5px;border-radius:10px;background:var(--bg-tertiary);color:var(--accent-orange);margin-left:6px;font-weight:600;" title="${c.call_count} calls">×${c.call_count}</span>` : ""}
                        <span class="callgraph-node-file">${safeFile}</span>
                      </div>
                      ${filePath ? `<span class="callgraph-src-link" data-path="${safeFile}" data-line="${c.line_no || c.line_s || 1}" style="color:var(--accent-blue);font-size:var(--fs-10, 10px);cursor:pointer;" title="Open in Code View">[📄 src]</span>` : ""}
                    </div>
                  `;
                  }).join("")
                : `<div style="color:var(--text-muted);padding:12px;">No incoming callers found.</div>`
            }
          </div>
        </div>

        <!-- Callees (Functions called by this) -->
        <div class="callgraph-col">
          <div class="callgraph-col-header">
            <span>Outgoing Callees (${callees.length})</span>
          </div>
          <div class="callgraph-node-list">
            ${
              callees.length > 0
                ? callees.map((c) => {
                    const funcName = c.name || c.callee_name || c.callee || "";
                    const filePath = c.file_path || c.file_name || c.file || "";
                    const safeFunc = escapeHtml(funcName);
                    const safeFile = escapeHtml(filePath);
                    return `
                    <div class="callgraph-node-item" style="display:flex;justify-content:space-between;align-items:center;">
                      <div class="callgraph-jump" data-func="${safeFunc}" style="cursor:pointer;flex:1;">
                        <span style="font-weight:500;">${safeFunc}()</span>
                        ${(c.call_count && c.call_count > 1) ? `<span style="font-size:var(--fs-10, 10px);padding:1px 5px;border-radius:10px;background:var(--bg-tertiary);color:var(--accent-blue);margin-left:6px;font-weight:600;" title="${c.call_count} calls">×${c.call_count}</span>` : ""}
                        <span class="callgraph-node-file">${safeFile}</span>
                      </div>
                      ${filePath ? `<span class="callgraph-src-link" data-path="${safeFile}" data-line="${c.line_no || c.line || 1}" style="color:var(--accent-blue);font-size:var(--fs-10, 10px);cursor:pointer;" title="Open in Code View">[📄 src]</span>` : ""}
                    </div>
                  `;
                  }).join("")
                : `<div style="color:var(--text-muted);padding:12px;">No outgoing callees found.</div>`
            }
          </div>
        </div>
      `;

      // Drilldown on click
      resContainer.querySelectorAll(".callgraph-jump").forEach((el) => {
        el.onclick = () => {
          this.functionName = el.dataset.func;
          this.loadCallgraph();
        };
        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "callgraph",
              title: `Callgraph: ${el.dataset.func}`,
              functionName: el.dataset.func,
              version: this.currentVersion,
              forceNew: true
            });
          }
        });
      });

      // Jump to Code View on [src] click
      resContainer.querySelectorAll(".callgraph-src-link").forEach((el) => {
        el.onclick = (e) => {
          e.stopPropagation();
          state.openTab({
            type: "code",
            title: el.dataset.path.split("/").pop(),
            path: el.dataset.path,
            cursorLine: parseInt(el.dataset.line || "1", 10),
            version: this.currentVersion
          });
        };
        el.addEventListener("auxclick", (e) => {
          if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            state.openTab({
              type: "code",
              title: el.dataset.path.split("/").pop(),
              path: el.dataset.path,
              cursorLine: parseInt(el.dataset.line || "1", 10),
              version: this.currentVersion,
              forceNew: true
            });
          }
        });
      });
    } catch (e) {
      if (seq !== this._seq || !this.containerEl?.isConnected) return;
      resContainer.innerHTML = `<div style="color:var(--accent-red);">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }
}
