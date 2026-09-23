/**
 * callgraph_view.js - Bidirectional Callgraph Explorer.
 */
import { api } from "../../api.js";
import { state } from "../../state.js";

export class CallgraphView {
  constructor() {
    this.currentVersion = "v3.0";
    this.functionName = "schedule";
    this.containerEl = null;
  }

  async render(containerEl, tabData) {
    this.containerEl = containerEl;
    this.currentVersion = tabData.version || state.currentVersion;
    if (tabData.functionName) {
      this.functionName = tabData.functionName;
    }

    containerEl.innerHTML = `
      <div class="tools-view-container">
        <div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border-color);padding-bottom:12px;">
          <div>
            <div style="font-size:18px;font-weight:600;color:var(--accent-purple);">
              Callgraph: <span style="font-family:var(--font-mono);">${this.functionName}()</span>
            </div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
              Bidirectional caller/callee cross-reference explorer.
            </div>
          </div>
          <div style="display:flex;gap:8px;">
            <input type="text" id="callgraph-input" value="${this.functionName}" placeholder="Function name..." />
            <button class="code-btn active" id="callgraph-btn">Explore</button>
            <button class="code-btn" id="callgraph-btn-nodemap" title="Open interactive DAG in NodeMap">🗺️ Open in NodeMap</button>
          </div>
        </div>

        <div id="callgraph-results" style="flex:1;display:flex;gap:20px;">
          <div style="color:var(--text-muted);">Loading callgraph...</div>
        </div>
      </div>
    `;

    const btn = containerEl.querySelector("#callgraph-btn");
    const input = containerEl.querySelector("#callgraph-input");

    btn.onclick = () => {
      this.functionName = input.value.trim() || "schedule";
      this.loadCallgraph();
    };

    await this.loadCallgraph();
  }

  async loadCallgraph() {
    const resContainer = this.containerEl.querySelector("#callgraph-results");
    try {
      const res = await api.getCallgraph(this.currentVersion, this.functionName);
      const callers = res.callers || [];
      const callees = res.callees || [];

      // Wire Open in NodeMap button
      const nmBtn = this.containerEl.querySelector("#callgraph-btn-nodemap");
      if (nmBtn) {
        nmBtn.onclick = () => {
          const initialNodes = [
            {
              id: "target-root",
              title: `${this.functionName}()`,
              type: "func",
              x: 420,
              y: 180,
              color: "#bc8cff"
            }
          ];
          const initialEdges = [];

          callers.forEach((c, idx) => {
            const cId = `caller-${idx}`;
            const cName = c.name || c.caller_name || c.caller || `caller_${idx}`;
            initialNodes.push({
              id: cId,
              title: `${cName}()`,
              type: "func",
              x: 80,
              y: 80 + idx * 120,
              color: "#a371f7"
            });
            initialEdges.push({
              fromNodeId: cId,
              toNodeId: "target-root"
            });
          });

          callees.forEach((c, idx) => {
            const cId = `callee-${idx}`;
            const cName = c.name || c.callee_name || c.callee || `callee_${idx}`;
            initialNodes.push({
              id: cId,
              title: `${cName}()`,
              type: "func",
              x: 760,
              y: 80 + idx * 120,
              color: "#58a6ff"
            });
            initialEdges.push({
              fromNodeId: "target-root",
              toNodeId: cId
            });
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
                    return `
                    <div class="callgraph-node-item" style="display:flex;justify-content:space-between;align-items:center;">
                      <div class="callgraph-jump" data-func="${funcName}" style="cursor:pointer;flex:1;">
                        <span style="font-weight:500;">${funcName}()</span>
                        ${(c.call_count && c.call_count > 1) ? `<span style="font-size:10px;padding:1px 5px;border-radius:10px;background:var(--bg-tertiary);color:var(--accent-orange);margin-left:6px;font-weight:600;" title="${c.call_count} calls">×${c.call_count}</span>` : ""}
                        <span class="callgraph-node-file">${filePath}</span>
                      </div>
                      ${filePath ? `<span class="callgraph-src-link" data-path="${filePath}" data-line="${c.line_no || c.line_s || 1}" style="color:var(--accent-blue);font-size:10px;cursor:pointer;" title="Open in Code View">[📄 src]</span>` : ""}
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
                    return `
                    <div class="callgraph-node-item" style="display:flex;justify-content:space-between;align-items:center;">
                      <div class="callgraph-jump" data-func="${funcName}" style="cursor:pointer;flex:1;">
                        <span style="font-weight:500;">${funcName}()</span>
                        ${(c.call_count && c.call_count > 1) ? `<span style="font-size:10px;padding:1px 5px;border-radius:10px;background:var(--bg-tertiary);color:var(--accent-blue);margin-left:6px;font-weight:600;" title="${c.call_count} calls">×${c.call_count}</span>` : ""}
                        <span class="callgraph-node-file">${filePath}</span>
                      </div>
                      ${filePath ? `<span class="callgraph-src-link" data-path="${filePath}" data-line="${c.line_no || c.line || 1}" style="color:var(--accent-blue);font-size:10px;cursor:pointer;" title="Open in Code View">[📄 src]</span>` : ""}
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
      resContainer.innerHTML = `<div style="color:var(--accent-red);">Failed: ${e.message}</div>`;
    }
  }
}
