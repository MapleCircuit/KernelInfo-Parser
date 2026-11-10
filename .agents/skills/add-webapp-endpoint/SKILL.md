---
name: add-webapp-endpoint
description: >-
  Checklist and workflow for adding or modifying a FastAPI endpoint, backend service,
  or frontend view in webapp/. Activate when implementing or changing webapp features.
---

# Add or Modify a WebApp Endpoint / View

## 1. Targeted Spec Slice Lookup
Never read `webapp/API*.md` from line 1. Jump directly to the relevant slice or run `python3 .agents/scripts/spec_section.py <spec> <section>`:
- **Core Architecture, Security & Caching**: `webapp/API.md` (§1 `[L24-L124]`, §2 Security `[L125-L339]`, §2B Services `[L340-L865]`)
- **REST API Endpoints**: `webapp/API_ENDPOINTS.md` (§4.1 `[L31-L214]`, §4.2 `[L215-L508]`, §4.3 Search/XRef `[L509-L680]` / AST/History `[L681-L889]`, §4.4 `[L890-L1120]`, §4.5 `[L1121-L1397]`, §4.6 Commits/Diff `[L1398-L1637]` / Blame/Patches `[L1638-L1816]`, §4.7 Tools `[L1817-L2164]`, §4.8 LKML `[L2165-L2480]`, §4.9 `[L2481-L2573]`, §4.10 `[L2574-L2673]`, §4.11 `[L2674-L2724]`, §4.12 `[L2725-L2741]`)
- **Frontend Views & UI Components**: `webapp/API_FRONTEND.md` (§5.0 `[L54-L143]`, §5.1 `[L144-L225]`, §5.6 `[L407-L479]`, §5.8 `[L495-L692]`, §5.11 `[L777-L850]`, §5.12 `[L851-L949]`, §5.13 `[L950-L1071]`, §5.20 `[L1375-L1396]`, §5.21 `[L1397-L1553]`, §5.22 `[L1554-L1678]`, §5.26 `[L1939-L2049]`, §5.29–5.37 `[L2109-L2395]`)

## 2. Subsystem Fast Lookup & Test Map

| Feature Area | Spec Reference | Backend Router / Service | Frontend View / Component | Backend Unit Test | Frontend Test |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Versions & Repos** | `API.md` §2.10 `[L485-L521]`, `API_ENDPOINTS.md` §4.1 `[L31-L214]` | `routers/versions.py`, `catalog_cache.py` | `ref_picker.js`, `version_switch.js` | `tests/test_webapp_existing_data.py` | — |
| **Filesystem & Code** | `API_ENDPOINTS.md` §4.2 `[L215-L508]`, `API_FRONTEND.md` §5.21 `[L1397-L1553]` | `routers/filesystem.py`, `filesystem_service.py` | `code_view/virtual_editor.js`, `ast_overlay.js` | `tests/test_webapp_hover.py` | `hover_card.test.mjs` |
| **Grep / Search** | `API.md` §2.1 `[L125-L339]`, `API_FRONTEND.md` §5.34 `[L2304-L2326]` | `services/grep_service.py` | `search/search_view.js` | `tests/test_webapp_grep.py` | `grep_format.test.mjs` |
| **Symbols & Refs** | `API_ENDPOINTS.md` §4.3 `[L509-L889]`, `API_FRONTEND.md` §5.6 `[L407-L479]` | `routers/symbols.py`, `symbol_service.py` | `components/context_menu.js` | `tests/test_webapp_file_references.py` | `file_references.test.mjs` |
| **Kconfig Engine** | `API_ENDPOINTS.md` §4.4 `[L890-L1120]`, `API_FRONTEND.md` §5.8 `[L495-L692]` | `routers/kconfig.py`, `kconfig_service.py` | `kconfig/kconfig_view.js`, `kconfig_engine.js` | `tests/test_webapp_defconfig.py` | `kconfig_workflows.test.mjs` |
| **Maintainers** | `API_ENDPOINTS.md` §4.5 `[L1121-L1397]` | `maintainer_service.py` | `views/sections/` | `tests/test_webapp_maintainer.py` | `sections.test.mjs` |
| **Commits & DAG** | `API_ENDPOINTS.md` §4.6 `[L1398-L1816]`, `API_FRONTEND.md` §5.12 `[L851-L949]` | `routers/commits.py`, `git_service.py` | `views/commits/commits_view.js` | `tests/test_webapp_commit_query.py` | `commit_query.test.mjs` |
| **Diff / Compare** | `API.md` §2.8 `[L422-L454]`, `API_FRONTEND.md` §5.11 `[L777-L850]` | `services/diff_service.py` | `views/diff/diff_view.js` | `tests/test_webapp_advanced_features.py` | `api_diff.test.mjs` |
| **Mailing List** | `API_ENDPOINTS.md` §4.8 `[L2165-L2480]`, `API_FRONTEND.md` §5.22 `[L1554-L1678]` | `routers/mail.py`, `mailing_list_service.py`| `views/mail/mail_view.js` | `tests/test_webapp_mail.py` | `merged_render.test.mjs` |
| **Kernel API** | `API_ENDPOINTS.md` §4.9 `[L2481-L2573]`, `API_FRONTEND.md` §5.29 `[L2109-L2161]` | `routers/kapi.py`, `kapi_service.py` | `views/kernel_api/kernel_api_view.js` | `tests/test_webapp_kapi.py` | — |
| **Insights** | `API_ENDPOINTS.md` §4.10 `[L2574-L2673]`, `API_FRONTEND.md` §5.30 `[L2162-L2203]`| `routers/insights.py`, `insights_service.py`| `views/insights/` | `tests/test_webapp_insights.py` | — |
| **CVE Records** | `API_ENDPOINTS.md` §4.11 `[L2674-L2724]` | `routers/cve.py`, `cve_service.py` | `views/sections/` | `tests/test_webapp_cve.py` | — |
| **Watch List** | `API_ENDPOINTS.md` §4.12 `[L2725-L2741]`, `API_FRONTEND.md` §5.36 `[L2357-L2379]`| `routers/watch.py`, `watch_service.py` | `views/watch/` | `tests/test_webapp_watch.py` | `watch.test.mjs` |
| **Patch Series** | `API_FRONTEND.md` §5.33 `[L2284-L2303]` | `services/patch_series.py` | `views/patch/patch_series_view.js` | `tests/test_webapp_patch_series.py` | `patch_series.test.mjs` |
| **Checkpatch** | `API.md` §2.18 `[L798-L815]` | `services/checkpatch_service.py` | `components/checkpatch_panel.js` | `tests/test_webapp_checkpatch.py` | — |

## 3. Step-by-Step Implementation Workflow
Enforce the invariants loaded from `webapp/AGENTS.md` (read-only DB, git/path/SQL sanitization, HTML escaping, confirmation gate):
1. **Backend Service & Router**:
   - Create or update service in `webapp/backend/services/` (read-only DB via `with get_db_cursor() as cursor:`, cached with `RepoScopedCache`).
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
