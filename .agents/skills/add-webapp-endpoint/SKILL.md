---
name: add-webapp-endpoint
description: >-
  Checklist and workflow for adding or modifying a FastAPI endpoint, backend service,
  or frontend view in webapp/. Activate when implementing or changing webapp features.
---

# Add or Modify a WebApp Endpoint / View

## 1. Targeted Spec Slice Lookup
Never read `webapp/API*.md` from line 1. Read a section with `python3 .agents/scripts/spec_section.py <spec> <section>`:
- **Core Architecture, Security & Caching**: `webapp/API.md` (§1, §2, §2.7, §2.20)
- **REST API Endpoints**: `webapp/API_ENDPOINTS.md` (§4.1, §4.2, §4.3, §4.4, §4.5, §4.6, §4.7, §4.8, §4.9, §4.10, §4.11, §4.12, §4.13)
- **Frontend Views & UI Components**: `webapp/API_FRONTEND.md` (§5.0, §5.1, §5.6, §5.8, §5.11, §5.12, §5.13, §5.20, §5.21, §5.22, §5.26, §5.29, §5.30, §5.31, §5.32, §5.33, §5.34, §5.35, §5.36, §5.37, §5.38)

## 2. Subsystem Fast Lookup & Test Map

| Feature Area | Spec Reference | Backend Router / Service | Frontend View / Component | Backend Unit Test | Frontend Test |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Versions & Repos** | `webapp/API.md` §2.10, `webapp/API_ENDPOINTS.md` §4.1 | `routers/versions.py`, `catalog_cache.py` | `ref_picker.js`, `version_switch.js` | `tests/test_webapp_existing_data.py` | — |
| **Filesystem & Code** | `webapp/API_ENDPOINTS.md` §4.2, `webapp/API_FRONTEND.md` §5.21 | `routers/filesystem.py`, `filesystem_service.py` | `code_view/virtual_editor.js`, `ast_overlay.js` | `tests/test_webapp_hover.py` | `hover_card.test.mjs` |
| **Grep / Search** | `webapp/API.md` §2.1, `webapp/API_FRONTEND.md` §5.34 | `services/grep_service.py` | `search/search_view.js` | `tests/test_webapp_grep.py` | `grep_format.test.mjs` |
| **Symbols & Refs** | `webapp/API_ENDPOINTS.md` §4.3, `webapp/API_FRONTEND.md` §5.6 | `routers/symbols.py`, `symbol_service.py` | `components/context_menu.js` | `tests/test_webapp_file_references.py` | `file_references.test.mjs` |
| **Kconfig Engine** | `webapp/API_ENDPOINTS.md` §4.4, `webapp/API_FRONTEND.md` §5.8 | `routers/kconfig.py`, `kconfig_service.py` | `kconfig/kconfig_view.js`, `kconfig_engine.js` | `tests/test_webapp_defconfig.py` | `kconfig_workflows.test.mjs` |
| **Maintainers** | `webapp/API_ENDPOINTS.md` §4.5 | `maintainer_service.py` | `views/sections/` | `tests/test_webapp_maintainer.py` | `sections.test.mjs` |
| **Commits & DAG** | `webapp/API_ENDPOINTS.md` §4.6, `webapp/API_FRONTEND.md` §5.12 | `routers/commits.py`, `git_service.py` | `views/commits/commits_view.js` | `tests/test_webapp_commit_query.py` | `commit_query.test.mjs` |
| **Diff / Compare** | `webapp/API.md` §2.8, `webapp/API_FRONTEND.md` §5.11 | `services/diff_service.py` | `views/diff/diff_view.js` | `tests/test_webapp_advanced_features.py` | `api_diff.test.mjs` |
| **Mailing List** | `webapp/API_ENDPOINTS.md` §4.8, `webapp/API_FRONTEND.md` §5.22 | `routers/mail.py`, `mailing_list_service.py`| `views/mail/mail_view.js` | `tests/test_webapp_mail.py` | `merged_render.test.mjs` |
| **Kernel API** | `webapp/API_ENDPOINTS.md` §4.9, `webapp/API_FRONTEND.md` §5.29 | `routers/kapi.py`, `kapi_service.py` | `views/kernel_api/kernel_api_view.js` | `tests/test_webapp_kapi.py` | — |
| **Insights** | `webapp/API_ENDPOINTS.md` §4.10, `webapp/API_FRONTEND.md` §5.30 | `routers/insights.py`, `insights_service.py`| `views/insights/` | `tests/test_webapp_insights.py` | — |
| **CVE Records** | `webapp/API_ENDPOINTS.md` §4.11 | `routers/cve.py`, `cve_service.py` | `views/sections/` | `tests/test_webapp_cve.py` | — |
| **Watch List** | `webapp/API_ENDPOINTS.md` §4.12, `webapp/API_FRONTEND.md` §5.36 | `routers/watch.py`, `watch_service.py` | `views/watch/` | `tests/test_webapp_watch.py` | `watch.test.mjs` |
| **Man Pages** | `webapp/API.md` §2.20, `webapp/API_ENDPOINTS.md` §4.13, `webapp/API_FRONTEND.md` §5.38 | `routers/man.py`, `man_service.py` | `views/man/man_view.js`, `man_render.js` | `tests/test_webapp_man.py` | `man_render.test.mjs` |
| **Patch Series** | `webapp/API_FRONTEND.md` §5.33 | `services/patch_series.py` | `views/patch/patch_series_view.js` | `tests/test_webapp_patch_series.py` | `patch_series.test.mjs` |
| **Checkpatch** | `webapp/API.md` §2.18 | `services/checkpatch_service.py` | `components/checkpatch_panel.js` | `tests/test_webapp_checkpatch.py` | — |

## 3. Step-by-Step Implementation Workflow
Enforce the invariants loaded from `webapp/AGENTS.md` (read-only DB, git/path/SQL sanitization, HTML escaping, confirmation gate):
1. **Backend Service & Router**:
   - Create or update service in `webapp/backend/services/` (read-only DB via `with get_reader() as reader:` and query specs, cached with `RepoScopedCache`).
   - Add or update router in `webapp/backend/routers/`, export in `routers/__init__.py`, and register in `webapp/backend/app.py`.
2. **Frontend View & Tab**:
   - Create or update view in `webapp/frontend/js/views/` (escape dynamic values with `escapeHtml()`).
   - Register view factory in `webapp/frontend/js/app.js` (`app.viewRenderers`), deep-link hash in `url_sync.js`, and per-tab state in `state.js`.
3. **Targeted Verification**:
   - Backend: `python3 -m unittest tests/test_webapp_<feature>.py`
   - Frontend: `node --test webapp/frontend/tests/<feature>.test.mjs`
4. **Documentation Sync**:
   - Update `webapp/API.md` (architecture/services), `webapp/API_ENDPOINTS.md` (REST endpoints), or `webapp/API_FRONTEND.md` (frontend views) in declarative present-tense.
5. **Final Gate**:
   - Run `python3 main.py -u` once.
