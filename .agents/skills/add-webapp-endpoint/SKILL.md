---
name: add-webapp-endpoint
description: >-
  Checklist and workflow for adding or modifying a FastAPI endpoint, backend service,
  or frontend view in webapp/. Activate when implementing or changing webapp features.
---

# Add or Modify a WebApp Endpoint / View

## 1. Targeted Spec Slice Lookup
Never read `webapp/API*.md` from line 1. Jump directly to the relevant slice or run `python3 .agents/scripts/spec_section.py <spec> <section>`:
- **Core Architecture, Security & Caching**: `webapp/API.md` (§1 `[L24-L126]`, §2 Security `[L127-L342]`, §2B Services `[L343-L927]`, §2.20 Man Pages `[L882-L927]`)
- **REST API Endpoints**: `webapp/API_ENDPOINTS.md` (§4.1 `[L32-L217]`, §4.2 `[L218-L511]`, §4.3 Search/XRef `[L512-L683]` / AST/History `[L684-L892]`, §4.4 `[L893-L1123]`, §4.5 `[L1124-L1400]`, §4.6 Commits/Diff `[L1401-L1640]` / Blame/Patches `[L1641-L1819]`, §4.7 Tools `[L1820-L2167]`, §4.8 LKML `[L2168-L2483]`, §4.9 `[L2484-L2576]`, §4.10 `[L2577-L2676]`, §4.11 `[L2677-L2727]`, §4.12 `[L2728-L2746]`, §4.13 Man Pages `[L2747-L2789]`)
- **Frontend Views & UI Components**: `webapp/API_FRONTEND.md` (§5.0 `[L55-L144]`, §5.1 `[L145-L238]`, §5.6 `[L423-L495]`, §5.8 `[L511-L708]`, §5.11 `[L793-L866]`, §5.12 `[L867-L965]`, §5.13 `[L966-L1090]`, §5.20 `[L1421-L1442]`, §5.21 `[L1443-L1599]`, §5.22 `[L1600-L1724]`, §5.26 `[L1986-L2104]`, §5.29–5.37 `[L2164-L2456]`, §5.38 Man Pages `[L2417-L2469]`)

## 2. Subsystem Fast Lookup & Test Map

| Feature Area | Spec Reference | Backend Router / Service | Frontend View / Component | Backend Unit Test | Frontend Test |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Versions & Repos** | `API.md` §2.10 `[L488-L536]`, `API_ENDPOINTS.md` §4.1 `[L32-L217]` | `routers/versions.py`, `catalog_cache.py` | `ref_picker.js`, `version_switch.js` | `tests/test_webapp_existing_data.py` | — |
| **Filesystem & Code** | `API_ENDPOINTS.md` §4.2 `[L218-L511]`, `API_FRONTEND.md` §5.21 `[L1443-L1599]` | `routers/filesystem.py`, `filesystem_service.py` | `code_view/virtual_editor.js`, `ast_overlay.js` | `tests/test_webapp_hover.py` | `hover_card.test.mjs` |
| **Grep / Search** | `API.md` §2.1 `[L125-L339]`, `API_FRONTEND.md` §5.34 `[L2364-L2386]` | `services/grep_service.py` | `search/search_view.js` | `tests/test_webapp_grep.py` | `grep_format.test.mjs` |
| **Symbols & Refs** | `API_ENDPOINTS.md` §4.3 `[L512-L892]`, `API_FRONTEND.md` §5.6 `[L423-L495]` | `routers/symbols.py`, `symbol_service.py` | `components/context_menu.js` | `tests/test_webapp_file_references.py` | `file_references.test.mjs` |
| **Kconfig Engine** | `API_ENDPOINTS.md` §4.4 `[L893-L1123]`, `API_FRONTEND.md` §5.8 `[L511-L708]` | `routers/kconfig.py`, `kconfig_service.py` | `kconfig/kconfig_view.js`, `kconfig_engine.js` | `tests/test_webapp_defconfig.py` | `kconfig_workflows.test.mjs` |
| **Maintainers** | `API_ENDPOINTS.md` §4.5 `[L1124-L1400]` | `maintainer_service.py` | `views/sections/` | `tests/test_webapp_maintainer.py` | `sections.test.mjs` |
| **Commits & DAG** | `API_ENDPOINTS.md` §4.6 `[L1401-L1819]`, `API_FRONTEND.md` §5.12 `[L867-L965]` | `routers/commits.py`, `git_service.py` | `views/commits/commits_view.js` | `tests/test_webapp_commit_query.py` | `commit_query.test.mjs` |
| **Diff / Compare** | `API.md` §2.8 `[L422-L454]`, `API_FRONTEND.md` §5.11 `[L793-L866]` | `services/diff_service.py` | `views/diff/diff_view.js` | `tests/test_webapp_advanced_features.py` | `api_diff.test.mjs` |
| **Mailing List** | `API_ENDPOINTS.md` §4.8 `[L2168-L2483]`, `API_FRONTEND.md` §5.22 `[L1600-L1724]` | `routers/mail.py`, `mailing_list_service.py`| `views/mail/mail_view.js` | `tests/test_webapp_mail.py` | `merged_render.test.mjs` |
| **Kernel API** | `API_ENDPOINTS.md` §4.9 `[L2484-L2576]`, `API_FRONTEND.md` §5.29 `[L2164-L2221]` | `routers/kapi.py`, `kapi_service.py` | `views/kernel_api/kernel_api_view.js` | `tests/test_webapp_kapi.py` | — |
| **Insights** | `API_ENDPOINTS.md` §4.10 `[L2577-L2676]`, `API_FRONTEND.md` §5.30 `[L2222-L2263]`| `routers/insights.py`, `insights_service.py`| `views/insights/` | `tests/test_webapp_insights.py` | — |
| **CVE Records** | `API_ENDPOINTS.md` §4.11 `[L2677-L2727]` | `routers/cve.py`, `cve_service.py` | `views/sections/` | `tests/test_webapp_cve.py` | — |
| **Watch List** | `API_ENDPOINTS.md` §4.12 `[L2728-L2746]`, `API_FRONTEND.md` §5.36 `[L2417-L2439]`| `routers/watch.py`, `watch_service.py` | `views/watch/` | `tests/test_webapp_watch.py` | `watch.test.mjs` |
| **Man Pages** | `API.md` §2.20 `[L882-L927]`, `API_ENDPOINTS.md` §4.13 `[L2747-L2789]`, `API_FRONTEND.md` §5.38 `[L2457-L2510]` | `routers/man.py`, `man_service.py` | `views/man/man_view.js`, `man_render.js` | `tests/test_webapp_man.py` | `man_render.test.mjs` |
| **Patch Series** | `API_FRONTEND.md` §5.33 `[L2344-L2363]` | `services/patch_series.py` | `views/patch/patch_series_view.js` | `tests/test_webapp_patch_series.py` | `patch_series.test.mjs` |
| **Checkpatch** | `API.md` §2.18 `[L814-L831]` | `services/checkpatch_service.py` | `components/checkpatch_panel.js` | `tests/test_webapp_checkpatch.py` | — |

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
