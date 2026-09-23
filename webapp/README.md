# KernelInfo-Parser Developer Web Application

The **KernelInfo-Parser Web Application** is a high-performance introspection, code exploration, and static analysis platform for the Linux Kernel AST parser project. It allows developers to browse kernel source code, explore relational Abstract Syntax Trees (AST), view token spatial coordinates, inspect and edit Kconfig hierarchies with live constraint validation, interact with an authentic Terminal Menuconfig (TUI) interface, inspect subsystem maintainer and reviewer rosters, browse credited kernel contributors, and explore git commit timelines and blame annotations.

---

## Documentation References

- **Authoritative REST API & Architecture Specification**:  
  👉 **[`webapp/API.md`](webapp/API.md)** — Complete endpoint catalog, request/response models, zero-trust security sandbox, database schema mappings, frontend module architecture, and Python service delegate exports.

---

## Modular Architecture Overview

The web application is built on a decoupled, zero-trust layered architecture:

- **Backend (`webapp/backend/`)**:
  - Built with **FastAPI** and Python 3.10+.
  - **Database Connection Pool**: Thread-safe MySQL pool (`webapp/backend/database/pool.py`) with automatic backoff and reconnection.
  - **Zero-Trust Security**: Canonical repository jailing (`webapp/backend/security/jail.py`), parameterized SQL wildcard escaping (`webapp/backend/security/sql.py`), strict Content-Security-Policy headers, and sliding-window rate limiting.
  - **Domain Services**: Encapsulated service layer (`webapp/backend/services/`) for Filesystem, Symbols & AST, Kconfig, Maintainers, Git Blame/Commits, Memory Layout (Pahole), Callgraphs, and Semantic Diffs.
  - **Modular Routers**: Version-scoped REST controllers (`webapp/backend/routers/`).
- **Frontend (`webapp/frontend/`)**:
  - **Zero-Build Vanilla ES-Modules**: Native browser ES6 modules requiring no Webpack, Vite, or npm build steps.
  - **Code View & AST Overlay**: Virtualized editor (`virtual_editor.js`) with narrowest-interval disjoint token mapping, interactive in-editor folder browsing, internal snippet lexical coloring (`ast_overlay.js`), and 100% copy fidelity buffer extraction (`fidelity_clipboard.js`).
  - **Interactive NodeMap**: Hardware-accelerated canvas vector schematic view using Two.js, cubic bezier edge routing, topological DAG auto-layout, and real-time zoom-aware node dragging.
  - **Universal Right-Click Context Menus**: Context-adaptive right-click menu system across Code Editor (Pahole layout, Callgraphs, Line Blame popover with commit diff jump, Command Palette search, coordinate copying), File Explorer (file and folder actions), and NodeMap (AST inspect, center, remove, clear canvas).
  - **Kconfig Menuconfig & Solver**: 20-pass constraint propagation engine, terminal TUI emulation, defconfig modal picker, and 1-click prerequisite auto-solver.
  - **Maintainers & CREDITS Hub**: Subsystem catalog, patch reviewer, and developer biographical profiles with git contribution stats.
  - **Offline Storage Tier**: IndexedDB cache (`webapp/frontend/js/db.js`) supporting complete offline fallback.

---

## Running the Web Application

Start the web application server from the repository root:

```bash
# Direct execution via CLI entrypoint
python3 webapp/main.py --host 0.0.0.0 --port 8000

# Or via uvicorn with live reload
uvicorn webapp.main:app --host 0.0.0.0 --port 8000 --reload
```

Open your browser at `http://localhost:8000`.

---

## Running Tests

Run the web application test suites:

```bash
python3 -m unittest tests/test_webapp_maintainer.py tests/test_webapp_advanced_features.py
```

To run all repository tests and verify parser regressions (Rule 8):

```bash
python3 main.py -u
```
