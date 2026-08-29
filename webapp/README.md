# KernelInfo-Parser Developer Web Application

The KernelInfo-Parser Web Application is a high-performance introspection and analysis platform for the Linux Kernel AST parser project. It allows developers to browse kernel source code, explore relational Abstract Syntax Trees (AST), view token spatial coordinates, inspect and edit Kconfig hierarchies with live constraint validation, interact with an authentic Terminal Menuconfig (TUI) interface, inspect subsystem maintainer and reviewer rosters, browse credited kernel contributors, and explore git commit timelines and blame annotations.

## Documentation Reference
For an exhaustive, deep architectural and feature specification of both the **FastAPI Backend Server** and the **Single-Page Application Client**, see:
👉 **[`WEBAPP_SYSTEMS_AND_FEATURES.md`](file:///home/scottviger/dev/KernelInfo-Parser/webapp/WEBAPP_SYSTEMS_AND_FEATURES.md)**

## Architecture Overview
- **Backend API Server**: [`webapp/main.py`](file:///home/scottviger/dev/KernelInfo-Parser/webapp/main.py) (FastAPI, MySQL connection pooling, Git integration, in-memory caching).
- **Frontend SPA Client**: [`webapp/webapp.html`](file:///home/scottviger/dev/KernelInfo-Parser/webapp/webapp.html) (Vanilla HTML5 / CSS3 / ES2022 JavaScript, responsive dark theme, zero runtime npm dependencies).

## Running the Web Application
Start the FastAPI server from the repository root:
```bash
uvicorn webapp.main:app --host 0.0.0.0 --port 8000 --reload
```
Open your browser at `http://localhost:8000/app` or `http://localhost:8000/webapp`.
