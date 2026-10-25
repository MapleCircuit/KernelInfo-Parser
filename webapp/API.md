# WebApp Subsystem API & Service Specification

The WebApp subsystem provides a developer introspection, source code exploration, and static analysis platform for the **KernelInfo-Parser** repository. It features a modular FastAPI REST backend and a zero-build vanilla ES-Module IDE frontend.

---

## 1. Architectural Overview & Component Structure

The WebApp is structured into a zero-trust, decoupled layered architecture:

```
webapp/
├── backend/
│   ├── app.py                      # FastAPI application factory & middleware setup
│   ├── config.py                   # Centralized configuration resolver (via core.config)
│   ├── models.py                   # Pydantic request & response data schemas
│   ├── database/
│   │   ├── pool.py                 # Thread-safe MySQL connection pool with backoff
│   │   └── helpers.py              # Encoding safeguards, version metadata lookup
│   ├── security/
│   │   ├── jail.py                 # Canonical filesystem sandboxing & jail validation
│   │   ├── sql.py                  # SQL LIKE wildcard sanitizer
│   │   └── middleware.py           # Security headers (CSP) & sliding-window rate limiter
│   ├── services/
│   │   ├── git_reader.py           # Persistent Git object readers (git cat-file --batch), one per repository
│   │   ├── docs_service.py         # Documentation label index & kernel-doc comment extraction
│   │   ├── tree_resolver.py        # Tri-Path Git tree resolver & arch-aware definition ranking
│   │   ├── arch_layout_service.py  # Build layout of an ARCH at a revision (m_arch_root, else Git)
│   │   ├── filesystem_service.py   # Tree traversal, file maps, include resolver, treemap
│   │   ├── symbol_service.py       # Multi-table symbol search, XRefs, AST containers
│   │   ├── history_service.py      # Release-by-release history of definitions & Kconfig symbols
│   │   ├── kconfig_service.py      # Defconfigs, Menuconfig hierarchy, constraint solver, Kbuild build conditions
│   │   ├── sync_service.py         # Change tokens (db, frontend, parsed, git) & X-KIP-Sync header
│   │   ├── maintainer_service.py   # Subsystems catalog, CREDITS, patch match engine
│   │   ├── git_service.py          # Line-by-line blame, commits log, RFC-2822 format-patch
│   │   ├── pahole_service.py       # Struct memory alignment, padding holes, cachelines
│   │   ├── callgraph_service.py    # Bidirectional function caller & callee graphs
│   │   └── diff_service.py         # Cross-version semantic diffs & Kconfig diffing
│   └── routers/
│       ├── versions.py             # Kernel versions catalog
│       ├── filesystem.py           # Directory trees, source files, include resolution
│       ├── symbols.py              # Symbol definitions, lookups, XRefs, AST inspector
│       ├── kconfig.py              # Kconfig trees, symbol relations, defconfigs
│       ├── maintainers.py          # Subsystems, maintainers, CREDITS, patch reviewer
│       ├── commits.py              # Git commit log, detail, and blame annotations
│       ├── docs.py                 # Documentation labels & kernel-doc
│       └── tools.py                # Pahole layout, callgraph, semantic version diff
├── frontend/
│   ├── index.html                  # Single-page application shell (served with its build's asset URLs)
│   ├── boot.js                     # Loader of the build's modules, recovers from a stale cached shell
│   ├── manifest.json               # Progressive Web App (PWA) manifest
│   ├── sw.js                       # Service Worker: cached shell, per-build assets, API offline fallback
│   ├── vendor/two.min.js           # Vendored vector schematic engine
│   ├── css/                        # Modular Design System tokens & layout
│   └── js/
│       ├── api.js                  # Frontend HTTP client with offline IndexedDB fallback
│       ├── sync.js                 # Server change tokens: cache invalidation, version refresh, new builds
│       ├── db.js                   # Client-side IndexedDB persistence layer
│       ├── state.js                # Reactive global state bus & multi-tab persistence
│       ├── url_sync.js             # Bi-directional URL hash routing
│       ├── components/             # Reusable UI controls (tabs, context menu, toast)
│       ├── utils/                  # Universal clipboard copy and helpers
│       └── views/                  # Domain-specific view controllers
├── main.py                         # Application CLI entrypoint & programmatic delegate exports
├── API.md                          # Authoritative API documentation (this document)
└── README.md                       # Quick-start and development instructions
```

---

## 2. Security & Zero-Trust Sandbox Specifications

### 2.1. Repository Path Jailing (`webapp/backend/security/jail.py`)
All file requests are validated against `LINUX_REPO_DIR` (the root of the tracked kernel git tree).
- `resolve_and_verify_repo_path(relative_path: str) -> Path`: Resolves the canonical path (`resolve()`) and verifies that `resolved_path.is_relative_to(LINUX_REPO_DIR)`.
- Path traversal sequences (`..`, `../`, `//`, null bytes) trigger immediate `HTTP 400 Bad Request`.
- Accessing files outside the repository jail triggers `HTTP_403_FORBIDDEN`.

### 2.2. SQL Wildcard Sanitization (`webapp/backend/security/sql.py`)
- `sanitize_like_query(query: str) -> str`: Escapes user-supplied SQL wildcards (`%` and `_`) using `\%` and `\_` before inserting into parameterized `LIKE %s` queries, preventing regex-based timing denial-of-service.

### 2.3. HTTP Security Headers (`webapp/backend/security/middleware.py`)
Every response includes hardened security headers:
- `Content-Security-Policy`: Restricts scripts, styles, and workers to `'self'` and `'unsafe-inline'` for style attributes.
- `X-Frame-Options`: `DENY` (prevents clickjacking attacks).
- `X-Content-Type-Options`: `nosniff`.
- `Referrer-Policy`: `strict-origin-when-cross-origin`.

### 2.4. Sliding-Window Rate Limiting
- `SlidingWindowRateLimiter`: Enforces an in-memory sliding window cap (default 600 requests / 60 seconds per client IP). Exceeding requests receive `HTTP 429 Too Many Requests` with a `Retry-After` header.

### 2.5. Strict Read-Only Database Invariant
The `webapp/` backend operates exclusively as a read-only presentation and query tier. Under no circumstances may any router, service, middleware, or background task in `webapp/` execute database schema definitions (DDL: `CREATE`, `ALTER`, `DROP`, `TRUNCATE`) or data modification commands (DML: `INSERT`, `UPDATE`, `DELETE`, `REPLACE`).
- All table creations, schema migrations, and initial seed rows (including `m_db_instance`) are the strict and exclusive responsibility of the parser engine (`core/DBLayout.py`, `main.py`).
- Database cursors acquired via `get_db_cursor()` in `webapp/backend/database/pool.py` do not support or execute transaction commits.
- If expected metadata tables or rows are absent, webapp endpoints must return appropriate HTTP error statuses or graceful process-level fallbacks without modifying the underlying database.

### 2.6. Frontend & Service Architectural Invariants
1. **Directory Path Filtering & Polymorphic Navigation**: In database queries on `m_file_blob`, directory records have `ftype == 0` while regular files have `ftype != 0`. All file search, path lookup, and `#include` header resolution queries explicitly filter `ftype != 0` so directories sharing names or paths with header files are never returned as file matches. File viewer endpoints (`get_file`) and frontend code views (`virtual_editor.js`) support polymorphic path navigation: opening directory paths returns child hierarchies (`entries`, `tree`) and renders interactive navigation tables/grids rather than returning 404 errors.
2. **Lexical Comment Prioritization & Directive Matching**: In dual-layer syntax and AST overlays (`webapp/frontend/js/views/code_view/ast_overlay.js`), lexical tokenizers prioritize matching block comments (`/* ... */`) and line comments (`// ...`) *before* general identifier regexes, rendering them as non-clickable `<span class="tok-comment">` elements to prevent comment tokens from leaking into `.ast-token` or triggering symbol context menus. Preprocessor directive matching on snippet-relative coordinate slices allows optional leading hashes (e.g. `^(\s*#?\s*define\s+)`) so directive keywords are styled as preprocessors (`tok-macro`) and never tagged as clickable symbol identifiers.
3. **Flat Coordinate Projection & Defensive Navigation**: Symbol detail backend endpoints (`webapp/backend/services/symbol_service.py:get_symbol_detail`) expose definition coordinate fields as flat scalars (`line: row["line_s"]`, `line_s: row["line_s"]`) at the root of the response payload alongside the nested `definition` record. Frontend jump-to-definition callers (`webapp/frontend/js/views/code_view/context_menu.js`) resolve coordinates defensively across `detail.definition?.line_s || detail.line_s || detail.line || 1` to ensure virtual editors navigate to and highlight the exact symbol definition line.
4. **Maintainer Query Sanitization & Parameter Preference**: Developer identity and maintainer endpoints (`webapp/backend/routers/maintainers.py`, `webapp/backend/services/maintainer_service.py`) support query parameter lookups (`/api/maintainers/person?email=...&version=...`) alongside URL path parameters. Frontend API clients sanitize patch trailers and email strings (stripping `<>`, quotes, and whitespace) and prefer query parameters when identifiers contain special characters (`@`, `<`, `>`, `/`) to prevent URL decoding and route matching failures.
5. **Context Menu Interaction Preservation**: Right-click context menus (`contextmenu`) across webapp workspaces (virtual code editor, file explorer sidebar, nodemap canvas) intercept default browser menus and offer context-adaptive actions (definitions, references, git blame popovers, command palette search, coordinate copying), while strictly preserving primary left-click interactions (inline AST token inspector popups, file navigation, node selection) without collision, event race conditions, or lingering ghost menus.
6. **Structured Cross-Reference Classification**: Symbol cross-reference backend services (`get_symbol_xref`) and frontend inspectors (`showXrefModal`) return and render both primary and multi-declaration definition locations (`definitions: [...]` with file path, line bounds, and AST declaration type) alongside categorized usage references (`references: [...]` with caller function, file, line, and typed badges `Call`, `MemberRef`, `TypeUsage`, `DeclRef`, `MacroExpansion`), providing direct 1-click jump navigation for both definitions and usages.

### 2.7. Tri-Path Tree Resolution & Arch-Aware Definition Ranking (`webapp/backend/services/tree_resolver.py`)
1. **Database Tree Resolution (`TreeResolverService`, module singleton `tree_resolver`)**:
   - **Path A (Single-File Lookup, `<0.2ms`)**: Resolves the exact `blob_id` of a specific `fnid` at any `commit_id` by walking `parent_idx = 0` backwards to the nearest `m_bridge_checkpoint`. If an `m_commit_file` delta intervened along that chain, returns the delta's `blob_id` (or `None` if deleted with `change_type == 'D'`). If untouched, returns the checkpoint's baseline `blob_id`.
   - **Path B (Full-Tree Resolution, `<15ms`, LRU Cached)**: Assembles the entire active `{fnid: blob_id}` repository tree at any `commit_id` by loading baseline snapshot records from `m_bridge_checkpoint` and chronologically applying forward `m_commit_file` deltas along the first-parent DAG. Results are cached in an in-memory `OrderedDict` LRU cache.
   - **Limitation**: Paths A/B only reach a checkpoint when every first-parent ancestor is ingested in `m_commit`/`m_commit_parent`. The parser ingests a bounded commit window, so many intermediate commits have broken chains; Paths A/B are therefore used only as a fallback when the Git repository is unavailable.
2. **Git-Native Tree Resolution (`GitTreeResolver`, module singleton `git_tree_resolver`)**:
   - **Path C (Full-Tree Listing, `~50ms` cold, LRU Cached per commit SHA)**: `list_tree(rev)` resolves any revision (tag, branch, full/short SHA, even commits never ingested into `m_commit`) via `git rev-parse --verify <rev>^{commit}` and returns `{path: blob_sha}` from `git ls-tree -r -z --full-tree`. Submodule entries are skipped. This is the authoritative file list for every revision.
   - `map_blob_hashes(cursor, blob_shas)` joins the Git blob SHAs to `m_file_blob.blob_hash` (`blob_hash` == Git blob SHA-1) and returns `{blob_sha: (blob_id, ftype)}`, preferring typed (`ftype != 0`) rows. Blobs absent from the map were never parsed (unindexed).
   - `blob_paths(cursor, rev)` returns `{blob_id: [paths]}` for the indexed blobs of the revision's tree (sorted paths; one blob may sit at several paths). It is cached per commit SHA (8 entries) for `BLOB_PATHS_TTL` = 120 s, so blobs indexed by a running parse appear without a restart. Symbol services use it to scope `m_symbol_def` (content-addressed by `blob_id`) to any revision; see §4 *Revision scoping of symbol queries*.
3. **Arch-Aware Definition Ranking (`definition_rank`, `best_path`, `definition_arch`)**:
   A symbol defined once per architecture (`struct pt_regs`, `setup_arch`, `thread_struct`...) resolves to the definition of one ARCH value: the `arch` parameter of the symbol endpoints, which the frontend sets to the KConfig engine's ARCH while the engine is on with a resolved configuration, and to `x86_64` otherwise (§5.19). `definition_arch(version, arch)` resolves the ARCH value through `KconfigService.resolve_arch` (unknown values fall back to `x86_64`) and returns it with its `ArchScope` and word size (`arch_bits`: `x86_64` → 64, `i386` → 32, generic `x86` → none). The scope is the ARCH's build layout at the revision (`webapp/backend/services/arch_layout_service.py`): the `m_arch_root` rows the parser stored for the version's head commit, or, for revisions without rows (branches, commits never ingested), the same evaluation of the revision's Makefiles from Git (`parser/arch_layout.py`, `parser/kconfig_ast/KCONFIG_API.md` §6.5), cached per (commit, ARCH); without a layout, the SRCARCH's `own_arch_dirs` are assumed. Definitions are ordered by:
   - prototypes (`SymbolLinkage.PrototypeDecl`) after every definition with a body;
   - path group (`path_arch_group`): the scope's own files (its architecture trees, `ArchLayout.arch_trees()`, and the asm-generic headers its generated wrappers include), then generic code, then the other `include/asm-generic/` and `include/uapi/asm-generic/` headers, then other architectures. An architecture tree is `arch/<a>/` or, in 2.6.x, `include/asm-<a>/` (`path_arch`): `atomic_t` at v2.6.12 resolves to `include/asm-x86_64/atomic.h` for x86_64 (the build links `include/asm` to it), `include/asm-i386/atomic.h` for i386, and ARCH=um's layout also owns `x86_64` through its `include/asm-um/arch` link; m68knommu at v2.6.30 owns `arch/m68k/` (hdr-arch). The same layout trees decide `other_arch` of the build-condition and Kconfig endpoints (`KconfigService._arch_trees`, `is_other_arch_path` in `parser/kbuild_conditions.py`);
   - word size: files of the other word size (`process_32.c` on x86_64, `page_64.h` on i386) after the others;
   - weak/alias definitions (`WeakOrAliasDef`) after strong ones, then path and line.
   A blob present at several paths is reported at its best-ranked path (`best_path`). Choosing between `#ifdef` branches of one file and between files Kbuild does not compile is done by the frontend against the actual configuration (§5.19).
4. **Definitions Reached From the Use Site (`from_file`)**: when the request names the file the symbol is used in, `SymbolService._prefer_reachable()` puts first the definitions the compilation of that file reads for the ARCH, i.e. what the compiler sees. `arch_layout_service.reachable(rev, arch, file)` walks the file's `#include` chain with the ARCH's build layout (`parser/include_graph.py`, `parser/kconfig_ast/KCONFIG_API.md` §6.6): forced `-include` files first, quoted names in the including file's directory, then the `-I` directories in order, through the build's symlinks and generated asm-generic wrappers. `#include` lines count whatever `#if` block holds them (the frontend still drops definitions its configuration compiles out), and directives are read from Git once per blob (cached by blob SHA, 50,000 entries; each walk cached per (commit, ARCH, file), 32 entries; a cold walk of `include/linux/sched.h` at v2.6.12 takes ~0.3 s). Reached definitions go before the path groups of item 3, prototypes stay last and weak definitions after strong ones; definitions are marked `visible`, and `include_chain` lists the files from the use site to the first one. `atomic_t` used in `include/linux/sched.h` at v2.6.12 thus resolves through `include/asm-x86_64/semaphore.h` to `include/asm-x86_64/atomic.h` for x86_64 (through `include/linux/sem.h` → `include/asm-um/atomic.h` → `include/asm-um/arch/atomic.h` = `include/asm-x86_64/atomic.h` for ARCH=um), and never to the unrelated `net/rxrpc/internal.h`. The walk reads the files themselves rather than `m_ast_include`/`m_file_reference`: those rows come from the parser's own clang run, which links `include/asm` to `asm-generic` and skips the includes of inactive `#if` branches, so they describe no particular ARCH.

### 2.8. 3-Level Semantic Cross-Version Diff Engine (`webapp/backend/services/diff_service.py`)
Either side of a comparison may be a release tag, a branch, or an arbitrary commit SHA. Comparison is evaluated in three progressive tiers to optimize latency:
1. **Level 1 (O(1) Content Blob Filter)**: Resolves `{path: blob_sha}` for both revisions via `GitTreeResolver` (Path C), falling back to `{path: blob_id}` from `m_bridge_checkpoint` / `TreeResolverService` only when Git is unavailable on both sides. Unchanged files (equal content keys) are short-circuited in $O(1)$. A deleted path whose exact content reappears under an added path is reported as `renamed`.
2. **Level 2 (AST & Symbol Structural Diff)**: For the first 50 modified files whose blobs are indexed on both sides, compares symbol definition records in `m_symbol_def` by `(name, type_id)` to surface added and removed definitions.
3. **Level 3 (Kconfig Scoped Diff)**: Lists added/removed/modified Kconfig source files (`Kconfig`, `Kconfig.*`) from the Git trees (always complete), and diffs option definitions (`type`, `prompt`, `def_val` per defining file) from `m_kconfig_symbol` via `m_bridge_tag`. A Kconfig file contributes options only if its blob is indexed on every side it exists on; otherwise it is listed in `unindexed_files` and excluded, so unparsed blobs never surface as spurious additions or removals.

### 2.9. Git-Native Database Architecture & Commit Hash Formatting
1. **Zero-VID Native Table Queries & Elimination of View Dependencies**:
   All webapp backend routers and services operate directly on Git-native content-addressable relational tables, completely bypassing legacy SQL compatibility views (`m_v_main`, `m_file`, `m_bridge_file`, `m_bridge_commit_file`):
   - Version catalog and resolution query `m_git_ref` joined with `m_commit`.
   - Filesystem tree and directory hierarchies list the Git tree of the requested revision (`GitTreeResolver`, Path C) and annotate files with `blob_id`/`ftype` from `m_file_blob` via the content-addressed `blob_hash`, so tags, branches, and intermediate commits all return their complete file list. Without a Git repository, they fall back to `m_bridge_checkpoint` and `TreeResolverService`.
   - The code view (`get_file`) resolves a file's indexed blob from `m_bridge_checkpoint`, or—for non-checkpoint commits—from the Git blob SHA returned by `git cat-file`, joined to `m_file_blob.blob_hash`.
   - Cross-version and file diffing use Git trees (Path C) with `m_file_blob` blob-hash annotation, falling back to `m_bridge_checkpoint` and `TreeResolverService.resolve_tree`.
   - Preprocessor `#include` resolution leverages compiler-verified `m_file_reference` (`ref_type=1`) provenance.
   - Backward-compatible JSON field mappings (`vid` = `commit_id`, `fid` = `blob_id`) are preserved so all frontend clients continue functioning seamlessly.
2. **Commit Hash Formatting (`format_commit_hash`)**:
   `m_commit.commit_hash` stores 20-byte binary Git SHA-1 digests (`BINARY(20)`). Backend services (`git_service.py`, `helpers.py`) format binary hashes using `format_commit_hash(val)` / `hash_hex(val)` (`bytes(val).hex()`; a `str` that mysql-connector returned for a hash whose bytes happen to be valid UTF-8 is re-encoded to its 20 bytes first), ensuring 40-character lowercase hexadecimal strings in all JSON API responses. API endpoints support both 40-character hexadecimal queries (using SQL `UNHEX(%s)`), short commit hash prefixes, and legacy integer `commit_id` values.

---

### 2.10. Multi-Repository Request Scoping (`webapp/backend/config.py`, `webapp/backend/security/middleware.py`)
1. **Repository selection**: `RepoContextMiddleware` (pure ASGI, outermost) reads the `X-KI-Repo` header or `?repo=` query parameter and stores the matching configured repository (core config `"repositories"`, see `core/API.md` §7.1.1) in a `ContextVar`. Unknown or missing names fall back to the default repository. Because it is pure ASGI, sync endpoints running in the threadpool inherit the value.
2. **Git access**: Every service resolves the work tree through `get_repo_dir()`; `git_reader` is a `GitReaderPool` with one persistent `git cat-file --batch` process per repository, and `GitTreeResolver.repo_dir` follows the request unless pinned.
3. **Database scoping**: `get_active_repo_id(cursor)` maps the repository to `m_repository.repo_id` (by name, then `git_url`; `-1` when never parsed). Version resolution (`get_version_info`) and the version/branch catalogs only consider that repository's `m_git_ref` rows. Commit ids, blobs and tags are global and need no scoping.
4. **Caches**: Module caches keyed by version names (`_FILE_TOKENS_CACHE`, `_INCLUDE_CACHE`, `_TREEMAP_CACHE`, `_DEFCONFIG_CACHE`, `_TREE_CACHE`, `_MAINTAINER_CACHE`, `_FILE_SUBSYSTEMS_CACHE`, version name/id caches) are `RepoScopedCache` instances that transparently prefix keys with the repository name; the git file-history LRU cache takes the repository path as an argument.
5. **Unparsed repositories** remain browsable: tags absent from `m_git_ref` resolve through `git rev-parse` to `vid = 0`, and trees, files, searches, diffs and treemaps read Git directly.

### 2.11. Client Synchronization & Build-Bound Assets (`webapp/backend/services/sync_service.py`, `webapp/frontend/js/sync.js`)
Clients must follow server changes without the user knowing to reload. The server keeps four change tokens (12-hex digests, cached so most requests cost no database or Git work):
| Token | Covers | Computed from (cache) | Client reaction |
|---|---|---|---|
| `db` | Database rebuilt | `m_db_instance` (30 s) | Every cached API response is dropped |
| `frontend` | Webapp code changed | SHA-256 of the files under `webapp/frontend/` (path + content), recomputed only when a file's size, mtime, ctime or inode changed (checked every 5 s); identical files give the same build across restarts | The page reloads into the new build (see below) |
| `parsed` | Versions of the active repository finished, re-parsed or rolled back | `m_git_ref` + `m_ingest_state` of the repository, unfinished versions excluded (15 s) | Version/branch lists are refetched; cached data of versions whose stamp changed is dropped. On the server, a change also clears the per-version caches (`clear_parse_dependent_caches()`: version-name maps, file tokens, include resolution, maintainers, history facts, blob-path maps) |
| `git` | Tags fetched, branches moved | `git for-each-ref` of the repository (15 s) | Same as `parsed` |
- **Delivery**: `GET /api/sync` returns all four. `SyncHeaderMiddleware` (inside `RepoContextMiddleware`, so tokens are those of the request's repository) adds `X-KIP-Sync: <db>.<parsed>.<git>` to every `/api/` response, so any request reveals a change without polling.
- **When clients check**: `syncManager.start()` runs once when the page opens (replacing the former `api.checkDbSync()`): it compares `db`, `parsed` and `git` with the tokens seen last (`meta/sync_tokens`), then follows the `X-KIP-Sync` headers. The `frontend` token is only compared when the page opens **and** the last frontend check (`localStorage["kip-last-frontend-check"]`) is more than an hour old; there is no polling.
- **Manual check** (`syncManager.checkNow()`, Settings popover **🔄 Check for Updates**, §5.5): requests `GET /api/sync?fresh=true`, so the server recomputes all four tokens instead of serving its cached ones, applies them like a header change, always refetches the version/branch stamps (`apply(…, {force: true})`), and compares the frontend build regardless of the hourly delay (a newer build shows the reload banner). Resolves to `{db, parsed, git, frontend, changed, removed, added, newBuild}`; the popover reports *Up to date* or what changed in a toast. It compares against the server's local Git repository; it does not `git fetch` from a remote.
- **Version stamps**: `/api/versions` and `/api/branches` give each version a `stamp` (`<commit sha[:12]>.<parse completion time>`, `.0` for versions parsed before `m_ingest_state`, `.git` when not parsed; a branch uses its current Git head). `api._vk(version)` puts `"<name>@<stamp>~"` into every version-scoped cache key, so a re-parsed version or a moved branch never serves older cached data; `syncManager.refreshVersions()` deletes the entries of versions whose stamp changed (`localDB.deleteDataWhere`, `isStaleKey`) and emits `versions:updated` (`{changed, removed, added, newlyIndexed}`); `app.js` then rebuilds the selectors, toasts newly indexed versions and re-renders open views of changed versions. A change of cache-key layout (`CACHE_LAYOUT`) clears the data stores once.
- **Build-bound assets**: `GET /` renders `index.html` with `__KIP_BUILD__` replaced by the current build (`<meta name="kip-build">`) and every `"/static/` URL rewritten to `"/b/<build>/` (`render_index()`); relative ES-module imports inherit the prefix. `GET /b/{build}/{path}` serves the current build's files with `Cache-Control: public, max-age=31536000, immutable` and answers **409** for any other build, so a page never mixes modules of two builds. The shell and `/boot.js` are sent with `Cache-Control: private, max-age=3600`, `sw.js` with `no-cache`. Legacy `/static/`, `/js/`, `/css/` mounts stay for direct access.
- **`boot.js`** (classic external script, the CSP forbids inline ones): sets `window.KIP_BUILD` and imports the build's `app.js` (`data-entry`). If the import fails (409: the cached shell is older than the server's build and its modules are not cached), it deletes the `kip-shell`/`kip-assets` caches and reloads once (`sessionStorage["kip-stale-build-reload"]` prevents a loop; a second failure shows an error box). `window.KIP_RELOAD_TO_LATEST()` does the same on request. `app.js` starts on `DOMContentLoaded`, or immediately when the import resolves after it.
- **Service worker** (`sw.js`): `kip-shell` holds `/` (every shell path and query share one entry) and `/boot.js`, cache-first; `kip-assets` holds `/b/<build>/…`, cache-first (the page posts `{type: "prune", build}` on start to drop other builds); `kip-api` holds API responses, network-first with cache fallback (`/api/sync` is never cached). Caches not named `kip-*` (e.g. the former `kernel-ide-v10`) are deleted on activation; there is no hand-maintained pre-cache list.
- **New build announcement** (`syncManager.announceUpdate()`): a bottom banner *"KernelInfo was updated (build …). Reloading in 5 s…"* with **Reload now** and **Later**; the countdown waits while a dialog (`.ki-modal-backdrop`) is open. Reloading drops the cached shell first, so the next page is the new build; tabs and KConfig profiles survive (saved in `localStorage`). **Later** dismisses it until the next hourly check.

## 3. Database Schema & Underlying Tables

| Domain Area | Key Database Tables | Primary Role |
| :--- | :--- | :--- |
| **Versions / Git Refs** | `m_git_ref`, `m_commit` | Version and reference identifiers (`ref_id`, `commit_id`, `ref_name`), chronological author dates, commit SHAs |
| **Filesystem & Blobs** | `m_file_blob`, `m_file_name`, `m_bridge_checkpoint`, `m_file_reference` | File instances, content blobs, paths, file types (0=Dir, 1=C, 2=Kconfig, 3=Rust, 4=Asm, 5=Maintainers, 6=Credits, 7=Raw), and compiler-verified `#include` / Kbuild provenance |
| **AST & Spatial Maps** | `m_ast`, `m_ast_container`, `m_ast_include`, `m_bridge_tag`, `m_bridge_map`, `m_map_ast`, `m_tag`, `m_tag_code` | Relational AST nodes, preprocessor `#include` links, container hierarchies, and coordinate regions (`line_s`, `char_s`, `line_e`, `char_e`) |
| **Symbols & XRefs** | `m_symbol_def`, `m_symbol_ref` (views over `m_tag_def`, `m_tag_ref`, `m_blob_symbol`, `m_symbol_own`, `m_blob_symbol_def`, `m_blob_symbol_ref`; see `core/API.md`) | Definitions and usage cross-references (calls, field access, type usages, macro expansions). `def_id` of a definition stored once per tag is computed (not indexable) and `ref_id` may be NULL, so services select every needed column in the query that finds the rows instead of re-fetching by id. Rows of given target AST ids come from `rows_of_targets_sql()` (`core/SymbolRows.py`) instead of `WHERE ast_id IN (...)` on the views, which takes seconds per target |
| **Kconfig** | `m_kconfig_symbol`, `m_kconfig_tree`, `m_kconfig_relation`, `m_kconfig_kbuild` | Hierarchical menus, architecture-scoped symbol dependencies, and compiled source mappings |
| **Maintainers** | `m_maintainer_section`, `m_maintainer_member`, `m_maintainer_person`, `m_maintainer_pattern`, `m_maintainer_file`, `m_credits_entry` | Subsystem sections, pattern rules, maintainers, reviewers, and historical `CREDITS` biographies |
| **Git & Commits** | `m_commit`, `m_commit_file`, `m_bridge_commit_person`, `m_commit_parent`, `m_git_network`, `m_repository` | Commit metadata, DAG topology, modified files, multi-contributor trailers (Signed-off-by, Acked-by, Reviewed-by) |
| **System & State** | `m_db_instance` | Unique database instance hash (`instance_hash`, `created_at`) used by clients to detect database rebuilds |

---

## 4. REST API Endpoint Catalog

All API endpoints reside under the `/api` prefix. Both canonical version-scoped paths and backward-compatible alias routes are supported.

---

### 4.1. Version Catalog

#### `GET /api/versions`
Versions of the active repository (§2.10): parsed refs from `m_git_ref` (`"indexed": true`) plus every commit-pointing Git tag that was not parsed (`"indexed": false`, `vid: 0`), sorted in Git version order (`versionsort`). Tags pointing at trees (e.g. `v2.6.11`) are skipped.
- **Query Parameters**:
  - `include_rc` (query, bool): If `true`, unparsed pre-release tags (`-rc`, `-pre`, `-alpha`, `-beta`) are included; parsed refs are always listed. Default `true`.
- **Entry fields**: `vid`, `vname`, `commit_hash`, `is_prerelease`, `ref_type` (1 tag, 2 branch, 3 commit), `indexed`, `stamp` (client cache generation, §2.11).
- **Unfinished versions**: a ref whose ingestion is still running or was interrupted (`m_ingest_state.status = 1`, see `core/API.md` §4.1) is not listed as indexed (`helpers.finished_ref_sql()`); its tag appears as unparsed (Git-only) until the run completes. Version-name resolution (`get_version_info`) and the history views (`indexed_versions`) apply the same rule.
- **Response**:
```json
{
  "total": 1,
  "versions": [
    {
      "vid": 1,
      "vname": "v3.0",
      "commit_hash": "02f8c6aee8df311261e6da8ba96bd41704aa9623",
      "is_prerelease": false
    }
  ]
}
```

#### `GET /api/projects`
List the configured repositories (core config `"repositories"`, auto-detected under `workspace.repos_dir`) joined with `m_repository`/`m_project`. Each entry has `repo` (the name sent as `X-KI-Repo`), `name`, `slug`, `path`, `default_branch`, `git_url`, `repo_id`, `indexed` (parsed into the database), `available` (work tree present) and `active`; the envelope adds `active`.
- **Query Parameters**: None
- **Response**:
```json
{
  "total": 1,
  "projects": [
    {
      "project_id": 1,
      "name": "Linux Kernel",
      "repo_path": "/home/scottviger/kernelinfo/linux",
      "default_branch": "master",
      "is_active": true
    }
  ]
}
```

#### `GET /api/branches`
Retrieve the active repository's Git branches: finished indexed refs (`m_git_ref.ref_type = 2` for its `repo_id`) followed by local Git branches. Each entry carries a `stamp` (§2.11) built from the branch's current Git head, so a branch that moved invalidates its cached data.
- **Query Parameters**: None
- **Response**:
```json
{
  "total": 1,
  "branches": [
    {
      "name": "master",
      "commit_hash": "02f8c6aee8df311261e6da8ba96bd41704aa9623",
      "is_default": true
    }
  ]
}
```

#### `GET /api/sync`
Change tokens of the server (§2.11): `{"db", "frontend", "parsed", "git", "repo"}`. Never cached by the service worker.
- **Query Parameters**: `fresh` (bool, default `false`): recompute every token instead of serving the server-side cached values (frontend 5 s, db 30 s, parsed/git 15 s). Used by the manual *Check for Updates*.
```json
{ "db": "4cf36a4c8bf6", "frontend": "65c1f2a0583b", "parsed": "af7532653070", "git": "07886de81433", "repo": "linux" }
```
Every `/api/` response also carries `X-KIP-Sync: <db>.<parsed>.<git>`.

#### `GET /api/db/instance`
Retrieve the unique database instance hash and initialization timestamp, allowing frontend clients to verify whether the active database has been rebuilt.
- **Query Parameters**: None
- **Behavior**:
  - Executes a strictly read-only `SELECT instance_hash, created_at FROM m_db_instance LIMIT 1;`.
  - In compliance with the strict read-only database invariant, never attempts to `CREATE TABLE` or `INSERT` instance records. If `m_db_instance` does not exist or contains no rows, the endpoint logs a warning and returns a stable process-level fallback hash and startup timestamp.
- **Response**:
```json
{
  "instance_hash": "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "created_at": 1727021400
}
```

---

### 4.2. Filesystem & Code View API

#### `GET /api/fs/tree/{version_name}`
#### `GET /api/tree`
Retrieve the directory hierarchy and file entries for a given path in the kernel repository.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string (default `"v3.0"`).
  - `path` (query): Directory path relative to repository root (e.g. `"kernel"`, `""` for root).
- **Behavior**:
  - `version_name` may be a release tag, a branch, or any full/short commit SHA. The listing comes from the Git tree of that revision (complete for every commit); file entries are annotated with `fid` (`blob_id`) and `ftype` from `m_file_blob` by blob hash. Files whose blobs were never parsed have `fid: null` and `ftype: 7` (Raw).
  - Automatically identifies subdirectories and filters out directory records (`ftype == 0`) from the file entries list to prevent duplication.
  - Returns sorted entries: directories first, followed by files alphabetically.
- **Response**:
```json
{
  "version": "v3.0",
  "vid": 1,
  "path": "",
  "entries": [
    { "name": "arch", "type": "dir", "path": "arch" },
    { "name": "kernel", "type": "dir", "path": "kernel" },
    {
      "name": "Makefile",
      "type": "file",
      "fid": 17858,
      "ftype": 7,
      "path": "Makefile",
      "s_stat": "A",
      "e_stat": "M",
      "s_stat_label": "Added",
      "e_stat_label": "Modified"
    }
  ],
  "total_count": 3
}
```

#### `GET /api/fs/file/{version_name}`
#### `GET /api/file`
Retrieve version-specific source file contents, metadata, spatial AST coordinate tokens, version lifecycle history, and incoming cross-file references.
- **Source Retrieval Engine**: Uses a persistent, thread-safe `git cat-file --batch` worker (`GitReader`) to stream exact blob contents directly from Git for the requested revision/tag in sub-millisecond time (~0.1ms small / ~0.2ms big files). Preserves 100% byte fidelity without relying on local working tree checkouts.
- **Graceful Unindexed Fallback**: If a requested file exists in the Git repository but has no indexing row in `m_bridge_checkpoint` (or is a non-C file), the endpoint serves the raw file content with `tokens: []`, empty AST maps, and inferred lifecycle metadata rather than throwing a 404.
- **Directory Detection**: If the path resolves to a Git `tree` or directory record (`ftype == 0`), the endpoint transparently returns the directory hierarchy structure.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string (default `"v3.0"`).
  - `path` (query): Target file path (e.g. `"init/main.c"`). Required.
- **Response**:
```json
{
  "version": "v3.0",
  "vid": 1,
  "fid": 684,
  "path": "init/main.c",
  "content": "/* ... raw source code ... */",
  "total_lines": 890,
  "file_size": 25412,
  "ftype": 1,
  "tokens": [
    [15, 0, 15, 24, 365653, 78],
    [21, 0, 21, 28, 42, 5]
  ],
  "tokens_format": "[line_s, char_s, line_e, char_e, ast_id, type_id]",
  "history": [
    {
      "fid": 684,
      "vid_s": 1,
      "vid_e": 0,
      "vname_s": "v3.0",
      "vname_e": "Active",
      "s_stat": "A",
      "e_stat": "0",
      "s_stat_label": "Added",
      "e_stat_label": "Active"
    }
  ],
  "used_by": {
    "total": 4,
    "breakdown": { "include": 4, "kconfig": 0, "kbuild": 0, "makefile": 0, "documentation": 0 },
    "references": [
      { "fid": 912, "source_file": "kernel/sched.c", "ref_type": "include", "ref_label": "Included By" }
    ]
  }
}
```

- **Directory Path Response** (when target `path` is a directory):
```json
{
  "type": "dir",
  "is_directory": true,
  "version": "v3.0",
  "vid": 1,
  "path": "init",
  "file_info": { "fid": 684, "fnid": 684, "fname": "init", "ftype": "dir" },
  "entries": [
    { "name": "main.c", "type": "file", "fid": 36568, "ftype": 1, "path": "init/main.c", "s_stat_label": "Added" }
  ],
  "tree": [ ... ],
  "content": "[Directory: init]",
  "tokens": [],
  "token_count": 0,
  "subsystems": [],
  "used_by": { "total": 0, "counts": {}, "references": [] }
}
```

#### `GET /api/fs/resolve_include`
#### `GET /api/resolve_include`
Resolve a preprocessor `#include <...>` or `#include "..."` directive to an authoritative git repository file path. Guarantees directory exclusion (`ftype != 0`) to prevent directory false-positives.
- **Parameters**:
  - `version` (query): Kernel version string (default `"v3.0"`).
  - `header` (query): Raw or cleaned include path (e.g. `"<linux/init.h>"`, `"linux/init.h"`, `"#include <linux/init.h>"`). Required.
  - `ast_id` (query): Optional AST Node ID of the include directive to check direct `m_ast_include` links.
  - `current_file` (query): Optional referring source file path (e.g. `"arch/x86/kernel/setup.c"`) to prioritize architecture-specific or relative header matches.
  - `arch` (query, optional): ARCH whose build layout resolves the include (`definition_arch`: unknown values fall back to `x86_64`).
- **Resolution Strategy**:
  0. The compiler's search for the ARCH's build layout (`arch_layout_service.resolve_include`, §2.7 item 4): `"name"` in `current_file`'s directory first, then the `-I` directories, through build-time symlinks and generated wrappers (`<asm/atomic.h>` → `include/asm-x86_64/atomic.h` at v2.6.12 for x86_64, `arch/x86/include/asm/atomic.h` at v3.0); method `build_layout`, with `arch`. Steps 1-5 only run when it finds nothing (e.g. generated headers, `tools/`).
  1. If `current_file` is supplied, queries compiler-verified provenance from `m_file_reference` (`ref_type=1`).
  2. If `ast_id` is supplied, queries `m_ast_include` join `m_file_name`.
  3. Generates prioritized candidate search paths: relative to `current_file` directory, `header`, `include/{header}`, `include/uapi/{header}`, and `arch/{arch}/include/{header}`.
  4. Matches candidates against active non-directory indexed files (`ftype != 0`) in `m_bridge_checkpoint` / `m_file_blob` / `m_file_name`.
  5. Falls back to physical repo jail disk verification.
- **Response**:
```json
{
  "header": "linux/init.h",
  "path": "include/linux/init.h",
  "resolved": true,
  "method": "search_path"
}
```

#### `GET /api/blame`
#### `GET /api/commits/{version_name}/blame/{path:path}`
#### `GET /api/commits/{version_name}/blame?path={path}`
#### `GET /api/version/{version_name}/file/{fid}/blame`
Retrieve line-by-line git blame provenance annotations and commit attributions.
- **Parameters**:
  - `version` (query) / `version_name` (path): Kernel version string (e.g. `"v3.0"`).
  - `path` (query or path): File path in repository (e.g. `"init/main.c"`).
  - `fid` (path): Integer file ID (when querying by file ID).
- **Response**:
```json
{
  "path": "init/main.c",
  "total_lines": 890,
  "lines": [
    {
      "line": 1,
      "commit_hash": "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
      "author_name": "Linus Torvalds",
      "author_email": "torvalds@ppc970.osdl.org",
      "date": 1113690036,
      "subject": "Linux-2.6.12-rc2",
      "author": "Linus Torvalds",
      "summary": "Linux-2.6.12-rc2",
      "final_line": 1
    }
  ],
  "blame": [ ... ]
}
```

#### `GET /api/version/{version_name}/browse/{path:path}`
Polymorphic unified browse endpoint that automatically detects whether the target path is a directory (returning tree hierarchy) or a source file (returning content, tokens, metadata, and lifecycle history).
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `path` (path): Repository path (empty string for repository root).
- **Response**: Returns either directory entries or file content payload based on path classification.
#### `GET /api/fs/treemap/{version_name}`
#### `GET /api/version/{version_name}/treemap`
Directory hierarchy of the revision weighted by bytes and file counts, built from `git ls-tree -r -l` (`GitTreeResolver.list_tree_sizes`, LRU cached). Children are sorted by size, descending.
- **Parameters**:
  - `version_name` (path): Revision.
  - `max_depth` (query): Levels below `path` (1–8, default 3).
  - `path` (query): Subdirectory to zoom into (default `""` for root).
- **Response**:
```json
{
  "version": "v3.0",
  "max_depth": 1,
  "name": "root",
  "path": "",
  "type": "dir",
  "size": 421358238,
  "file_count": 36783,
  "children": [
    { "name": "drivers", "path": "drivers", "type": "dir", "size": 222873639, "file_count": 15270, "children": [] }
  ]
}
```

#### `GET /api/fs/search/{version_name}`
Fuzzy file-path search over the revision's Git tree (used by the Ctrl+K palette).
- **Parameters**: `q` (query), `limit` (query, default 50, max 200).
- **Ranking**: exact basename → basename prefix → basename substring → path substring → subsequence (fewest gaps first).
- **Response**: `{ "version", "query", "total", "files": [{ "path", "name", "score" }] }`

#### `GET /api/fs/raw/{version_name}`
Serve an image blob of the revision (documentation figures). Only `svg`, `png`, `jpg`/`jpeg`, `gif` are served (else 404); a `name.*` path picks the first existing of `.svg/.png/.jpg/.jpeg/.gif` (Sphinx convention). Responses carry `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` so scripts embedded in SVG never run; files over 10 MiB return 413.
- **Parameters**: `path` (query, jailed relative path), `repo` (query, optional repository for `<img>` requests that cannot send headers).

#### `GET /api/fs/compile_commands/{version_name}`
#### `GET /api/version/{version_name}/compile_commands`
Export a Clang JSON Compilation Database (`compile_commands.json`) with one entry per `.c` file of the revision's Git tree, excluding other architectures' `arch/<name>/` subtrees. Commands include the arch/generic include paths, `include/linux/kconfig.h`, `-D__KERNEL__` and `-nostdinc`.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `arch` (query): Target architecture (e.g. `"x86"`, `"arm"`, default `"x86"`).
- **Response**:
```json
[
  {
    "directory": "/kernel",
    "file": "init/main.c",
    "command": "gcc -nostdinc -isystem ... -Iarch/x86/include -Iinclude -c init/main.c -o init/main.o"
  }
]
```

#### `GET /api/fs/compile_commands/{version_name}/export`
#### `GET /api/version/{version_name}/export/compile_commands.json`
Same database as an attachment download (`Content-Disposition: attachment; filename="compile_commands-<version>-<arch>.json"`).

#### `GET /api/version/{version_name}/export/compile_commands`
#### `GET /api/fs/compile_commands/{version_name}/export`
Export compilation commands wrapped in an envelope object (`{"compile_commands": [...], "commands": [...]}`) for client-side download consumers expecting wrapped payloads.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `arch` (query): Target architecture (default `"x86"`).
- **Response**:
```json
{
  "version": "v3.0",
  "arch": "x86",
  "total": 1,
  "compile_commands": [
    {
      "directory": "/kernel",
      "file": "init/main.c",
      "command": "gcc -nostdinc -isystem ... -Iarch/x86/include -Iinclude -c init/main.c -o init/main.o"
    }
  ],
  "commands": [
    {
      "directory": "/kernel",
      "file": "init/main.c",
      "command": "gcc -nostdinc -isystem ... -Iarch/x86/include -Iinclude -c init/main.c -o init/main.o"
    }
  ]
}
```

---

### 4.3. Symbols, Cross-References & AST Inspection API

#### `GET /api/symbols/search`
#### `GET /api/symbols/{version_name}/search`
Search symbol definitions by prefix or substring across functions, variables, structs, enums, enumerator constants (`EnumConstant`), typedefs, and macros.
- **Revision scoping of symbol queries** (also `lookup`, `symbol/{name}`, `xref/{name}`): `version_name` may be a tag, branch, or any commit SHA, including commits that were never ingested into `m_commit`. `SymbolService._scope` builds `{blob_id: [paths]}` from the revision's Git tree (`GitTreeResolver.blob_paths`) and falls back to `m_bridge_checkpoint` only when Git is unavailable. Only definitions whose blob is in that tree are returned, with `file_path` taken from the tree. A symbol is therefore found at a commit whenever its file's exact content was parsed in any version; files whose content was never parsed contribute no symbols. Search scans up to `SEARCH_CANDIDATES` (30000) name-ordered candidates before scoping, then fetches details for the kept `def_id`s. The first query per commit costs ~0.3 s to build the blob map; later queries are cached.
- **Parameters**:
  - `version` / `version_name` (query): Kernel version string (default `"v3.0"`).
  - `q` (query): Search prefix or substring.
  - `limit` (query): Maximum results to return (default 50).
- **Response**:
```json
{
  "total": 1,
  "symbols": [
    {
      "name": "start_kernel",
      "type_id": 5,
      "type_name": "FunctionDecl",
      "fid": 684,
      "file_path": "init/main.c",
      "line_s": 490,
      "line_e": 680
    }
  ]
}
```
> [!NOTE]
> All symbol endpoints (`search`, `detail`, `xref`) map raw AST type names (e.g. `C_enumequal` &rarr; `EnumConstant`, `C_functionproto` &rarr; `FunctionDecl`, `C_struct` &rarr; `StructDecl`, `C_SCtypedef` &rarr; `Typedef`) to readable presentation labels.


#### `GET /api/symbols/lookup`
#### `GET /api/symbols/{version_name}/lookup`
#### `GET /api/version/{version_name}/symbol_lookup`
#### `GET /api/version/{version_name}/lookup`
Fast typeahead symbol name suggestion endpoint.
- **Parameters**:
  - `version` / `version_name` (query or path): Kernel version string (default `"v3.0"`).
  - `q` / `query` (query): Symbol prefix.
  - `limit` (query): Maximum results (default 20).
- **Response**:
```json
["start_kernel", "start_secondary", "start_arm"]
```

#### `GET /api/symbols/{version_name}/detail/{symbol_name}`
#### `GET /api/version/{version_name}/symbol/{symbol_name}`
Retrieve authoritative definition information, source location, and cross-reference overview for a symbol.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `symbol_name` (path): Symbol identifier.
  - `arch` (query, default `x86_64`): ARCH value whose definition wins when the symbol is defined per architecture (§2.7); also the ARCH whose Kconfig model answers `CONFIG_*` / Kconfig symbols.
  - `from_file` (query, optional): file the symbol is used in; the definitions its `#include` chain reaches for `arch` win (§2.7 item 4).
- **Response** also carries `arch`, `from_file`, `include_chain` (files from `from_file` to the chosen definition when it is reached, else `[]`) and `definitions` (every in-tree definition in rank order, at most 50: `[{file, line_s, line_e, type_name, ast_id, linkage, visible}]`, `visible` = reached from `from_file`, `null` without it); `file_path`/`definition` are the first one.
- **Response**:
```json
{
  "name": "start_kernel",
  "found": true,
  "type_id": 5,
  "type_name": "FunctionDecl",
  "fid": 684,
  "file_path": "init/main.c",
  "line": 490,
  "line_s": 490,
  "line_e": 680,
  "total_references": 12
}
```

#### `GET /api/symbols/{version_name}/xref/{symbol_name}`
#### `GET /api/version/{version_name}/xref/{symbol_name}`
Retrieve both definition location(s) and categorized usage references (call sites, member references, type usages, macro expansions) across the kernel codebase.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `symbol_name` (path): Symbol identifier.
  - `limit` (query, optional int): Maximum number of reference records to return (useful for symbols with thousands of references like `kmalloc`).
  - `arch` (query, default `x86_64`): orders `definitions` for that ARCH (§2.7); `definition` is the first.
- **References**: only references in blobs of the revision's tree are returned, ordered by file and line. `file` is the path of the referencing blob in that tree (best-ranked for `arch` when the blob sits at several paths; not `target_fnid`, which is the target's file), `caller` is the name of the AST of the tag the reference sits in (e.g. the enclosing function), `fid` the referencing blob, and `limit` applies after scoping.
- **Response**:
```json
{
  "symbol": "start_kernel",
  "definitions": [
    {
      "file": "init/main.c",
      "line_s": 510,
      "line_e": 680,
      "type_name": "FunctionDecl",
      "ast_id": 42
    }
  ],
  "definition": {
    "file": "init/main.c",
    "line_s": 510,
    "line_e": 680,
    "type_name": "FunctionDecl",
    "ast_id": 42
  },
  "total_definitions": 1,
  "total_references": 2,
  "references": [
    {
      "caller": "x86_64_start_kernel",
      "file": "arch/x86/kernel/head64.c",
      "fid": 138690,
      "line": 95,
      "char_s": 2,
      "tag_id": 2445459,
      "type": "Call"
    },
    {
      "caller": "setup_arch",
      "file": "arch/x86/kernel/setup.c",
      "fid": 138702,
      "line": 842,
      "char_s": 2,
      "tag_id": 2445512,
      "type": "Call"
    }
  ]
}
```

#### `GET /api/symbols/{version_name}/ast/{ast_id}`
#### `GET /api/ast/{ast_id}/tree`
Recursively inspect relational AST node container hierarchies.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `ast_id` (path): Root AST Node ID to expand.
  - `depth` (query): Traversal depth limit (default 3).
- **Response**:
```json
{
  "ast_id": 42,
  "node": {
    "ast_id": 42,
    "name": "start_kernel",
    "type_id": 5,
    "type_name": "FunctionDecl",
    "children": [
      {
        "ast_id": 43,
        "name": "setup_arch",
        "type_id": 21,
        "type_name": "CallExpr",
        "children": []
      }
    ]
  }
}
```

#### `GET /api/symbols/{version_name}/include/{ast_id}`
#### `GET /api/include/{ast_id}`
#### `GET /api/version/{version_name}/include/{ast_id}/symbols`
#### `GET /api/version/{version_name}/include/{ast_id}`
Retrieve imported symbols and target header file path for a preprocessor `CPPro_include` AST node.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `ast_id` (path): AST Node ID of the include directive. If `ast_id <= 0`, falls back to database lookup using `file_path`, `line`, or `header`.
  - `tag_id` (query): Optional tag ID of the include node.
  - `file_path` (query): Path of the source file containing the include.
  - `line` (query): 1-indexed line number of the include in the source file.
  - `header` (query): Include header target (e.g. `<linux/init.h>` or `linux/init.h`).
- **Response**:
```json
{
  "ast_id": 365653,
  "include_text": "<linux/const.h>",
  "header_file": "include/linux/const.h",
  "header_exists": true,
  "total_symbols": 2,
  "imported_symbols": [
    {
      "name": "_AC",
      "type_id": 76,
      "type_name": "MacroDef",
      "def_file": "include/linux/const.h",
      "line_s": 14
    }
  ]
}
```

#### `GET /api/symbols/{version_name}/history`
#### `GET /api/tag/{tag_id}/timeline`
Release-by-release history of one definition (function, struct, union, enum, typedef, macro, global, ASM label, Rust item...) in the active repository (`HistoryService.get_symbol_history`, `webapp/backend/services/history_service.py`).
- **Parameters**: `path` and `line` (query) plus optional `name` (the symbol under the cursor) and `ast_id` select the anchor: the narrowest tag of the file at `version_name` covering `line` that is named `name` (else has `ast_id`, else is a definition in `m_symbol_def`). When `name` is not defined at `path:line` (the cursor is on a use), the definition is located through `m_symbol_def` (the requested file first, then the `arch` query parameter's ranking (§2.7, default `x86_64`), then definitions with a body, then `.c` files). An anchor that is a Kconfig `config`/`menuconfig` entry returns the Kconfig history below. `include_rc` (bool, default `false`): also list `-rc` tags; by default only final releases (and non-tag refs) are listed. The tag route takes `tag_id` and locates its file through `m_bridge_checkpoint`/`m_commit_file`.
- **Identity**: versions are matched by the anchor's identity, `m_ast.name` + `type_id`, never by `ast_id` (a function's `ast_id` hashes signature + body, so it changes with every code change). When a blob holds several matches, a member of the anchor's `m_moved_tag` lineage (transitive, both directions, bounded to 500 tags) wins, then the one closest to the previous version's line; a renamed lineage member is used only when no tag of the same name remains.
- **Algorithm**: the file's blob at every listed ref comes from one `git cat-file --batch-check` (content hash → `m_file_blob`). Per ref: same `tag_id` as the previous ref → `unchanged`; another tag → `modified`, or `cosmetic` when the code is identical once comments and whitespace are removed; first appearance → `introduced` (`added` when it reappears); no match → `removed`/`absent`; blob never parsed → `unindexed`; blob parsed without the definition although a non-indented line of the file still names the symbol → `unparsed` (an indexing gap, the previous state is kept). Code comes from `m_tag_code`; diffs (with context lines) compare against the last present version.
- **Response**:
```json
{
  "kind": "symbol",
  "tag_id": 1352034,
  "ast_id": 2832542,
  "ast_name": "copy_process",
  "type_id": 21,
  "type_name": "C_functionprotodecl",
  "path": "kernel/fork.c",
  "include_rc": false,
  "moved_to": 1404325,
  "moved_from": 1350084,
  "lineage_tag_ids": [1352034, 1267003, 1338314, 1350084, 1404325],
  "total_versions": 5,
  "change_count": 4,
  "snippets": { "982012": "static task_t *copy_process(...", "1142713": "..." },
  "timeline": [
    { "vname": "v2.6.12", "vid": 1860, "is_release": true, "prev_vname": null, "status": "introduced", "path": "kernel/fork.c", "tag_id": 982012, "line_s": 838, "line_e": 1149, "lines_added": 0, "lines_removed": 0, "diff": [], "defs": [{ "path": "kernel/fork.c", "status": "introduced", "tag_id": 982012, "line_s": 838, "line_e": 1149, "diff": [], "lines_added": 0, "lines_removed": 0 }] },
    { "vname": "v2.6.13", "vid": 6034, "is_release": true, "prev_vname": "v2.6.12", "status": "modified", "tag_id": 1142713, "line_s": 843, "line_e": 1162, "lines_added": 17, "lines_removed": 9, "diff": [{ "type": "ctx", "text": "..." }, { "type": "add", "text": "..." }], "defs": ["..."] }
  ]
}
```

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}/history`
#### `GET /api/version/{version_name}/kconfig/symbol/{symbol_name}/history`
Release-by-release history of every definition of `CONFIG_<symbol>` (`CONFIG_` prefix optional) across all Kconfig files (`HistoryService.get_kconfig_history`).
- **Parameters**: `include_rc` (bool, default `false`) as above; `version_name` only picks the reported `path`.
- **Algorithm**: the Kconfig tags of the symbol (`m_ast` name + `Kconfig_Config`/`Kconfig_Menuconfig` → `m_tag` → `m_bridge_tag`) give the blobs defining it and, through `m_bridge_checkpoint`/`m_commit_file`, the candidate paths; one `git cat-file --batch-check` resolves every (ref, path). Each defining file is parsed with `KconfigParser` (cached per blob SHA) to extract the definition's properties, including the dependencies inherited from enclosing `if`/`menu`/`choice` blocks. Definitions are paired per path (in line order) between consecutive refs: `added`, `removed`, `unchanged` (same tag), `modified` (a property changed) or `cosmetic` (text changed, properties identical). The entry status aggregates them (`introduced`, `added`, `modified`, `cosmetic`, `removed`, `unchanged`, `absent`, `unindexed`).
- **Response**: `{ "kind": "kconfig", "name", "ast_name": "CONFIG_<name>", "path", "paths", "include_rc", "total_versions", "change_count", "snippets": {tag_id: code}, "timeline": [{ "vname", "vid", "is_release", "prev_vname", "status", "lines_added", "lines_removed", "defs": [{ "path", "status", "tag_id", "line_s", "line_e", "facts", "changes", "diff", "lines_added", "lines_removed" }] }] }`. `facts` = `{ line_s, line_e, kind: config|menuconfig, type, prompt, defaults[], depends_on[], inherited_depends[], selects[], implies[], ranges[], choice, options[], help }` (conditions rendered as `X if COND`); `changes` = `[{ field, before, after }]` for scalar fields (`kind`, `type`, `prompt`, `choice`, `help`) and `[{ field, added[], removed[], before[], after[] }]` for list fields.

#### `GET /api/history/commits`
Commits behind one change of a history timeline (`HistoryService.get_change_commits`).
- **Parameters**: `path`, `to` (the version where the change is observed), `from` (the previous timeline version; omitted: the whole history up to `to`), `line_s`/`line_e` (the definition's lines at `to`), `limit` (1-500, default 100). Revisions must match `^[A-Za-z0-9][A-Za-z0-9._/+-]*$`.
- **Algorithm**: `git log -L<line_s>,<line_e>:<path> --no-patch from..to` (exact: the commits that touched those lines); without a line range (a removed definition) `git log from..to -- path`. LRU cached (1024 entries) per repository, path, range and lines.
- **Response**: `{ "path", "from", "to", "line_s", "line_e", "total", "commits": [{ "commit_hash", "commit_hash_short", "author_name", "author_email", "author_date", "subject" }] }`.

#### `GET /api/docs/{version_name}/labels`
Index of every `.. _label:` target under `Documentation/` at the revision (`git grep` with context, LRU cached per commit), used to resolve `:ref:` across documents.
- **Response**: `{ "version", "total", "labels": { "<label>": { "path", "line", "title" } } }` (labels lowercased; `title` is the section the label precedes, when one follows).

#### `GET /api/docs/{version_name}/kernel-doc`
kernel-doc comments of a source file, filtered like the `.. kernel-doc::` directive.
- **Parameters**: `path` (query), `export` / `internal` (bool: only / never `EXPORT_SYMBOL*` identifiers), `functions` (space- or comma-separated identifiers), `doc` (a `DOC:` section title), `no_identifiers`.
- **Response**: `{ "version", "path", "total", "exported": [...], "entries": [{ "kind": "function|macro|struct|union|enum|typedef|doc", "name", "brief", "signature", "params": [{ "name", "desc" }], "sections": [{ "title", "body" }], "line" }] }`. Descriptions keep kernel-doc markup (`@param`, `%CONST`, `&struct foo`) for the client to render.

---

### 4.4. Kconfig & Architecture Defconfigs API

All Kconfig endpoints read the Git objects of the requested revision (never the work tree), accept an optional **toolchain profile** (`toolchain` query parameter holding JSON on GET endpoints, `toolchain` body field on POST endpoints; see `GET /toolchain` below) and are scoped by an **ARCH value** (`arch` parameter, default `x86_64`): the value `make ARCH=<arch>` would receive. The top Makefile maps some values onto another source directory (`i386`/`x86_64` -> `x86`, `sparc32`/`sparc64` -> `sparc`, `sh64` -> `sh`, ...); `x86`, `sparc` and `sh` themselves are the generic forms whose bitness is chosen in menuconfig. The model is built by `parser/kconfig_ast/kconfig_model.py` and evaluated by `parser/kconfig_ast/kconfig_eval.py` (server) and `kconfig_engine.js` (browser); both reproduce the kernel's `conf` output byte for byte (see `parser/kconfig_ast/KCONFIG_API.md` section 8). Models are cached per (repository, revision, ARCH, toolchain) in a 6-entry LRU.

#### `GET /api/kconfig/{version_name}/toolchain`
#### `GET /api/version/{version_name}/kconfig/toolchain`
Default toolchain profiles of a revision. Linux 4.18+ Kconfig runs `$(shell,...)` checks against the compiler, linker and assembler while it is parsed; the server answers them from a profile (`parser/kconfig_ast/kconfig_toolchain.py`). Response: `{ "version", "arch", "kernelversion", "macros": true, "gcc": {profile}, "clang": {profile} }` where a profile is `{ "cc", "cc_version", "ld", "ld_version", "assembler", "binutils_version", "cross_compile", "llvm_utils", "pahole_version", "rustc_version", "gcc_plugins", "host_release", "env", "probes" }`. `macros: false` means the revision predates the macro language: only environment variables (`option env=`) matter.
- **Toolchain parameter**: any subset of the profile, e.g. `{"cc": "clang", "cc_version": "18.1.3", "env": {"CC_VERSION_TEXT": "Ubuntu clang version 18.1.3"}, "probes": {"cc-option:-fsanitize=kcfi": "n"}}`. Missing fields take the defaults of that kernel release. `env` overrides any environment variable Kconfig reads; `probes` forces a check (`"y"`/`"n"`) or a command output (text), keyed as reported in the model's `toolchain.probes`.

#### `GET /api/kconfig/{version_name}/tree`
#### `GET /api/kconfig/tree`
The finalized configuration model of one ARCH, in compact transport form, for the client-side engine (`decodeModel()` in `kconfig_engine.js`).
- **Parameters**: `version_name` (path) / `version` (query); `arch` (query, default `x86_64`); `include_nodes` / `include_tree` (bool: also return readable per-entry `nodes` with `tree_id`, `parent_id`, `node_type`, `title`, `prompt`, `symbol_name`, `file_path`, `line_s`, `depends_on_expr`, `depends_on`, `defaults`, `selects`, `selected_by`, `implies`, `implied_by`, `choice`); `include_relations` (bool: `relations` / `reverse_relations` maps).
- **Response** (abridged):
```json
{
  "format": 2, "version": "v3.0", "arch": "x86_64", "srcarch": "x86",
  "env": {"ARCH": "x86_64", "SRCARCH": "x86", "KERNELVERSION": "3.0.0"},
  "mainmenu": "Linux/x86_64 3.0.0 Kernel Configuration",
  "config_header": "Automatically generated make config: don't edit",
  "modules_sym": "MODULES", "root_kconfig": "Kconfig", "total_nodes": 8255,
  "exprs": [["s", "64BIT"], ["c", "x86_64"], ["=", 3, 1], ["&", 0, 2]],
  "menu_fields": ["parent", "kind", "sym", "prompt", "pvis", "vis_if", "dep", "file", "line_s", "line_e", "has_help"],
  "menus": [[-1, 0, null, "Linux/x86_64 3.0.0 Kernel Configuration", -1, -1, -1, 0, 6, 0, 0], [0, 3, "64BIT", "64-bit kernel", 12, -1, -1, 1, 2, 9, 1]],
  "files": ["Kconfig", "arch/x86/Kconfig"],
  "symbols": {"64BIT": {"t": "bool", "p": [12], "d": [[14, -1]], "m": [1]}},
  "warnings": [],
  "semantics": {"write_on_nonzero_default": false, "menu_end_comments": false, "int_hex_zero_default": false, "tristate_choice_mod_hidden": false},
  "toolchain": {
    "macros": false, "summary": "gcc 5.4.0 · GNU ld 2.25", "profile": {...}, "env": {"CC": "gcc", "...": "..."},
    "env_used": [{"name": "ARCH", "value": "x86_64", "overridden": false}],
    "probes": [{"key": "cc-option:-fsanitize=kcfi", "kind": "cc-option", "label": "Compiler accepts -fsanitize=kcfi", "command": "...", "result": "n", "source": "knowledge", "note": "gcc >= never, clang >= 16.0", "locations": [{"file": "arch/Kconfig", "line": 790, "symbol": "CFI_CLANG"}]}],
    "messages": [{"level": "error", "text": "Sorry, this C compiler is not supported.", "file": "scripts/Kconfig.include", "line": 44}]
  }
}
```
  `semantics` lists `conf` behaviours of this revision (detected from its `scripts/kconfig` sources) that both engines follow. `toolchain` reports what the build environment contributed: every environment variable read (`env_used`), every toolchain check with its answer and source (`toolchain` = derived from the profile, `knowledge` = known first gcc/clang versions, `assumed` = no data, assumed to succeed, `override`), the Kconfig entries using it, and `$(error-if)`/`$(warning-if)`/`$(info)` messages.
  Expression nodes: `["s", name]`, `["c", text]`, `["&"|"|", a, b]`, `["!", a]`, `["="|"!="|"<"|"<="|">"|">=", a, b]`; `-1` = no expression. Menu `kind` codes: 0 root, 1 menu, 2 choice, 3 config, 4 menuconfig, 5 comment, 6 if. Symbol keys: `t` type, `p` prompt visibilities, `d` defaults `[value, condition]`, `r` ranges, `s` selects, `i` implies, `rd` reverse dependency, `im` implied-by, `dd` direct dependency, `m` defining menu entries, `ch` choice of a choice value, `ic`/`op`/`mb` choice flags and members, `au` automatic (env/choice), `env`.

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}`
#### `GET /api/version/{version_name}/kconfig/symbol/{symbol_name}`
#### `GET /api/kconfig/symbol` / `GET /api/version/{version_name}/kconfig/symbol` (`name` query)
Arch-scoped details of a symbol (with or without `CONFIG_`). The same name can be declared in different files per architecture (e.g. `64BIT`: `arch/x86/Kconfig` for x86_64, `arch/sparc/Kconfig` for sparc64).
- **Parameters**: `arch` (query, default `x86_64`).
- **Response**: `name`, `arch`, `srcarch`, `defined_for_arch`, `type` (1 bool, 2 tristate, 3 string, 4 hex, 5 int) / `type_name`, `prompt`, `prompts`, `help`, primary `file_path` / `line_s` / `line_e` / `definition`, `declarations` and `definitions` (every definition for this ARCH: `file_path`, `line_s`, `line_e`, `prompt`, `kind`, `depends_on_expr`, `visible_expr`, `help`, `menu_path`), `dir_dep_expr`, `rev_dep_expr`, `implied_expr`, `depends_on` (symbols named by its own `depends on` lines), `selects`, `select_conditions`, `implies`, `selected_by`, `implied_by`, `defaults` (`[{value, cond}]`), `def_val`, `ranges`, `choice`, `choice_members`, `env`, `kconfig_refs` (entries referring to it), `kbuild` (Makefile rules, see usage), `compiled_files` (`[{file_path, target_obj, compile_mode, makefile, line, other_arch}]`, Git scan merged with `m_kconfig_kbuild` rows), plus compatibility fields `vid_s`, `vid_e`, `vname_s`, `vname_e`, `added_version`, `is_active`, `lifecycle_status`.
- When the symbol is not declared for this ARCH: `defined_for_arch: false` and `declarations` lists its declarations in other architectures' Kconfig files (404 if declared nowhere).

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}/usage`
#### `GET /api/version/{version_name}/kconfig/symbol/{symbol_name}/usage`
Where `CONFIG_<symbol>` is used (Git grep of the revision, cached).
- **Parameters**: `arch` (default `x86_64`), `limit` (1-2000, default 400).
- **Response**: `kbuild` (`[{makefile, line, rule, target_obj, path, exists, kind: object|composite|directory, via: rule|condition, compile_mode, other_arch}]`: objects and directories built when the option is enabled, including rules inside `ifeq ($(CONFIG_X),y)` blocks), `source_refs` (`[{file_path, line, text, other_arch}]` in `.c .h .S .lds .dts .dtsi`, `CONFIG_X` and `CONFIG_X_MODULE`), `source_ref_count`, `source_file_count`, `kconfig_refs` (`[{kind: depends_on|select|imply|default|prompt_condition|select_condition|imply_condition, by, file, line}]`). `other_arch` marks paths in an architecture tree (`arch/<a>/`, or `include/asm-<a>/` in 2.6.x) the ARCH's build layout does not use (§2.7).

#### `GET /api/kconfig/{version_name}/build`
Kbuild build condition of source files: under which configuration `make ARCH=<arch>` compiles each one (`KconfigService.build_conditions`, `parser/kbuild_conditions.py`, see `parser/kconfig_ast/KCONFIG_API.md` §6.4). The condition is an expression over `CONFIG_*` symbols that the client evaluates against its own configuration (`build_condition.js`), so one response serves every configuration of the ARCH. Resolvers are cached per (revision, ARCH) (4 entries); the first request of a revision reads its Makefiles and greps it once for `#include "*.c"` sources (~1 s).
- **Parameters**: `path` (query, repeatable, up to 64), `arch` (default `x86_64`).
- **Response**: `{version, arch, srcarch, files: {path: {status, expr, rules, reason}}}`:
  - `status`: `rule` (Kbuild rules name the object; `expr` says when it is built), `other_arch` (in an architecture tree, `arch/<a>/` or 2.6.x `include/asm-<a>/`, that the ARCH's build layout does not use, §2.7: never built or used, `expr` = `"n"`), `no_rule` (the fully parsed Kbuild file of the directory does not mention the object: never built, `expr` = `"n"`), `unknown` (headers, files outside Kbuild, objects built by custom rules, host programs, `#include`d sources, lists computed by `$(shell)`/`$(wildcard)`: `expr` = `"?"`).
  - `expr`: `"y"`/`"m"`/`"n"`/`"?"`, `{"sym": X}`, `{"def": X}`, `{"eq": [a, b]}` (make strings of literals and `{"val": X}`), `{"not": e}`, `{"and": [...]}`, `{"or": [...]}`; three-valued (unknown never yields `"n"` on its own).
  - `rules`: `[{makefile, line, text}]`: the object's rule first, then the directory chain up to the top-level goal list.
```json
{
  "version": "v3.0", "arch": "x86_64", "srcarch": "x86",
  "files": {
    "arch/x86/kernel/tls.c": {
      "status": "rule",
      "expr": { "or": [{ "sym": "X86_32" }, { "sym": "IA32_EMULATION" }] },
      "rules": [{ "makefile": "arch/x86/kernel/Makefile", "line": 58, "text": "obj-$(CONFIG_X86_32) += tls.o" },
                { "makefile": "arch/x86/Kbuild", "line": 10, "text": "obj-y += kernel/" },
                { "makefile": "arch/x86/Makefile", "line": 131, "text": "core-y += arch/x86/" }],
      "reason": ""
    }
  }
}
```

#### `GET /api/version/{version_name}/kconfig/defconfigs`
#### `GET /api/kconfig/{version_name}/defconfigs` / `GET /api/kconfig/defconfigs`
Defconfigs of the ARCH's source directory: `arch/<srcarch>/defconfig` and every file under `arch/<srcarch>/configs/` (nested ones such as powerpc `40x/acadia_defconfig` included). `canonical_default` is what `make ARCH=<arch> defconfig` loads: `KBUILD_DEFCONFIG` from `arch/<srcarch>/Makefile` evaluated with ARCH/SRCARCH known, `CONFIG_*` undefined and `CROSS_COMPILE` set (e.g. `x86` -> `i386_defconfig`, `x86_64` -> `x86_64_defconfig`, `powerpc` -> `ppc64_defconfig`), else `arch/<srcarch>/defconfig`.
- **Response**: `{ "version", "arch", "arch_dir", "total_count", "canonical_default", "defconfigs": [{ "name", "file_path", "fid", "is_canonical" }] }`.

#### `GET /api/version/{version_name}/kconfig/defconfig/content`
#### `GET /api/kconfig/{version_name}/defconfig` / `GET /api/kconfig/defconfig/content`
Raw defconfig text and its literal assignments. Nothing is injected: everything a defconfig leaves out is derived by kconfig when it is loaded.
- **Parameters**: `defconfig` / `file_path` (name or path; empty = the ARCH's default defconfig), `arch`.
- **Response**: `{ "version", "name", "file_path", "arch", "srcarch", "bits", "symbol_count", "values", "content" }`; `bits` is `64BIT` after resolving the defconfig with kconfig (null for architectures without that symbol).

#### `GET /api/kconfig/{version_name}/presets`
#### `GET /api/version/{version_name}/kconfig/presets` / `.../kconfig/env-presets`
Every ARCH value of the revision: one per `arch/*` directory plus the top Makefile's aliases.
- **Response**: `{ "version", "default_arch", "targets": [{ "id", "label", "arch", "srcarch", "kconfig_path", "canonical_defconfig", "bits", "env": {"ARCH", "SRCARCH", "KERNELVERSION"} }], "architectures": (same list), "compilers": [...] }`. `compilers` only matters from Linux 4.18 (`CC_IS_GCC`/`CC_IS_CLANG`), whose `$(shell,...)` macros the model does not evaluate.

#### `GET /api/kconfig/{version_name}/search`
#### `GET /api/version/{version_name}/kconfig/search`
Search symbol names and prompts of the ARCH model (`CONFIG_` prefix optional). Parameters: `q`, `limit` (default 25), `arch`. Response: `{ "version", "arch", "query", "count", "total", "symbols": [{ "name", "type", "type_name", "prompt", "help", "file_path", "line_s" }] }`.

#### `GET /api/kconfig/graph` / `GET /api/kconfig/{version_name}/graph`
#### `GET /api/version/{version_name}/kconfig/graph` / `.../kconfig/graph/{symbol_name}`
Direct relations of one symbol in the ARCH model: its own `depends on` symbols (`depends_on` edges), `selects` / `implies` targets, and selectors (`selects` edges into it). Parameters: `symbol` / `symbol_name`, `arch`, `depth` (kept for compatibility). Response: `{ "version", "arch", "symbol", "nodes": [{ "id", "label", "is_root", "type" }], "edges": [{ "from", "to", "type" }] }`.

#### `POST /api/kconfig/{version_name}/autosolve` / `POST /api/version/{version_name}/kconfig/autosolve`
#### `GET /api/kconfig/{version_name}/autosolve/{symbol}` / `GET /api/version/{version_name}/kconfig/autosolve/{symbol}`
Legal changes (the ones menuconfig would accept, applied in order) that make `target_symbol` reach `value` from `current_values`. The solver satisfies prompt visibility recursively (AND: both sides, OR: cheapest side, `!X`: lowers X), selects prompt-less symbols through a selector, and refuses symbols forced by select, fixed by the architecture or without prompt.
- **Body**: `{ "target_symbol": "KVM_INTEL", "value": "y", "arch": "x86_64", "toolchain": {...}, "current_values": {"KVM": "n"} }` (GET: empty configuration, `arch` query).
- **Response**: `{ "solution_found": true, "toggles_needed": [{ "symbol": "KVM", "from": "n", "set_to": "y", "to": "y" }, { "symbol": "KVM_INTEL", "from": "n", "set_to": "y", "to": "y" }], "total_toggles": 2, "blocked_by": [], "final_value": "y", "target": "KVM_INTEL", "arch": "x86_64" }`.

#### `POST /api/kconfig/{version_name}/validate` / `POST /api/version/{version_name}/kconfig/validate`
Loads `{"symbols": {...}, "arch": ...}` like `conf_read` and reports every value kconfig would not keep. Response: `{ "version", "arch", "forced_symbols": {name: value}, "adjusted_symbols": {name: value}, "details": [{ "symbol", "requested", "value", "reason", "depends_on" }], "conflicts": [{ "symbol", "message", "selected_by", "depends_on" }], "unknown_symbols", "invalid_values" }`. `conflicts` are kconfig's "selects X which has unmet direct dependencies" warnings.

#### `POST /api/kconfig/{version_name}/diff-config` / `POST /api/version/{version_name}/kconfig/diff-config`
Compare `{"active_config": {...}, "custom_config": {...}}`. Response: `{ "version", "matching_symbols", "mismatched_symbols", "differences": [{ "symbol", "val1", "val2" }] }`.

#### `POST /api/kconfig/{version_name}/import` / `POST /api/version/{version_name}/kconfig/import`
Parse `.config` text (`{"content": "...", "arch": optional}`) into `{ "version", "symbol_count", "symbols" }` (strings unescaped). With `arch`, also `unknown_symbols`, `invalid_values` and `warnings` as kconfig reports them.

#### `POST /api/kconfig/{version_name}/export` / `POST /api/version/{version_name}/kconfig/export`
Body `{"symbols": {...}, "arch": optional, "format": "config"|"defconfig"}`. With `arch`, the assignments are resolved by kconfig and the result is exactly what `conf` writes (`format: "defconfig"`: `make savedefconfig` output); response `{ "version", "arch", "content", "symbol_count", "resolved": true, "unknown_symbols", "invalid_values" }`. Without `arch`, the assignments are only formatted (`resolved: false`).

---

### 4.5. Maintainers & CREDITS API

#### `GET /api/maintainers/{version_name}/overview`
#### `GET /api/maintainers`
#### `GET /api/version/{version_name}/maintainers`
Search and list kernel subsystems from the authoritative `MAINTAINERS` file database.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `q` (query): Search filter across subsystem names, maintainer names, and emails.
- **Response**:
```json
{
  "version": "v3.0",
  "total": 1,
  "sections": [
    {
      "sec_id": 1,
      "name": "3C505 NETWORK DRIVER",
      "status": "Maintained",
      "mailing_list": "netdev@vger.kernel.org",
      "scm_tree": "",
      "web_page": "",
      "maintainers": [
        {
          "person_id": 47,
          "name": "Philip Blundell",
          "email": "philb@gnu.org",
          "role_type": 1,
          "role_name": "Maintainer"
        }
      ]
    }
  ]
}
```

#### `GET /api/maintainers/{version_name}/section/{sec_id}`
#### `GET /api/maintainers/section`
#### `GET /api/version/{version_name}/maintainer/section/{sec_id}`
Retrieve comprehensive details for a subsystem section, including member rosters, file matching rules (`F:`, `X:`), and touched files.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `sec_id` (path) / `sec_name` (query): Numeric section ID (`sec_id`) or literal subsystem name.
- **Response**:
```json
{
  "version": "v3.0",
  "sec_id": 1,
  "name": "3C505 NETWORK DRIVER",
  "status": "Maintained",
  "mailing_list": "netdev@vger.kernel.org",
  "scm_tree": "",
  "web_page": "",
  "members": [
    {
      "person_id": 47,
      "name": "Philip Blundell",
      "email": "philb@gnu.org",
      "role_type": 1,
      "role": "Maintainer",
      "role_name": "Maintainer",
      "in_credits": true
    }
  ],
  "patterns": [
    { "pattern_type": 1, "pattern": "drivers/net/3c505*", "priority": 0 }
  ],
  "files": [
    { "fname": "drivers/net/3c505.c" },
    { "fname": "drivers/net/3c505.h" }
  ],
  "file_count": 2
}
```

#### `GET /api/maintainers/{version_name}/person/{person_id_or_email}`
#### `GET /api/maintainers/{version_name}/person`
#### `GET /api/maintainers/person`
#### `GET /api/version/{version_name}/person/{person_id_or_email}`
#### `GET /api/version/{version_name}/person`
Retrieve developer profile, CREDITS biographical data, maintained subsystems, and git contribution statistics. Supports both URI path parameters and query parameters to safely handle email addresses containing special characters.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `person_id_or_email` (path): Developer numeric person ID, email address, or name.
  - `email` (query): Developer email address (e.g. `"torvalds@linux-foundation.org"`).
  - `person_id` / `id` (query): Numeric developer person ID (e.g. `47`).
  - `name` (query): Developer name query.
- **Response**:
```json
{
  "person_id": 47,
  "name": "Philip Blundell",
  "email": "philb@gnu.org",
  "in_credits": true,
  "subsystems": [
    {
      "sec_id": 1,
      "name": "3C505 NETWORK DRIVER",
      "status": "Maintained",
      "role_name": "Maintainer"
    }
  ],
  "credits": {
    "web_page": "http://example.org",
    "pgp_key": "1024D/ABCDEF12",
    "description": "Linux/ARM hacker Device driver hacker",
    "snail_mail": "Cambridge CB5 8EG\nUnited Kingdom"
  },
  "contribution_stats": {
    "authored_commits": 0,
    "signed_off_commits": 0,
    "reviewed_commits": 0
  }
}
```

#### `POST /api/maintainers/{version_name}/match`
#### `POST /api/maintainers/match`
#### `POST /api/version/{version_name}/patch/maintainers`
#### `POST /api/patch/maintainers`
Emulate `scripts/get_maintainer.pl` by evaluating touched paths or raw patch content against subsystem pattern rules.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - **Body** (`application/json`):
    ```json
    {
      "paths": ["drivers/net/3c505.c"],
      "patch": null
    }
    ```
- **Response**:
```json
{
  "matched_sections": [
    {
      "sec_id": 1,
      "name": "3C505 NETWORK DRIVER",
      "matched_file": "drivers/net/3c505.c",
      "maintainers": [
        { "name": "Philip Blundell", "email": "philb@gnu.org", "role": "Maintainer" }
      ]
    }
  ],
  "suggested_cc": [
    "Philip Blundell <philb@gnu.org>",
    "netdev@vger.kernel.org"
  ]
}
```

#### `GET /api/maintainers/{version_name}/credits`
#### `GET /api/credits`
#### `GET /api/version/{version_name}/credits`
Search historical `CREDITS` file entries and biographical details.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `q` (query): Search query string.
- **Response**:
```json
{
  "version": "v3.0",
  "total": 1,
  "credits": [
    {
      "person_id": 47,
      "name": "Philip Blundell",
      "email": "philb@gnu.org",
      "description": "Linux/ARM hacker Device driver hacker",
      "snail_mail": "Cambridge CB5 8EG\nUnited Kingdom"
    }
  ]
}
```

#### `GET /api/maintainers/{version_name}/developers`
#### `GET /api/developers/{version_name}`
#### `GET /api/developers`
Search and list all kernel developers, maintainers, reviewers, and contributors recorded in the kernel persona registry (`m_maintainer_person`).
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `q` / `query` (query): Case-insensitive search on developer name or email.
  - `role` (query): Role filter (`"all"`, `"maintainer"`, `"reviewer"`, `"credits"`). Default `"all"`.
  - `sort` (query): Sort order (`"activity"` for subsystems count descending, or `"alpha"` for alphabetical A-Z). Default `"activity"`.
- **Response**:
```json
{
  "version": "v3.0",
  "total_count": 1171,
  "role_filter": "all",
  "sort": "activity",
  "developers": [
    {
      "person_id": 72,
      "name": "Lennert Buytenhek",
      "email": "kernel@wantstofly.org",
      "subsystems_count": 20,
      "is_maintainer": true,
      "is_reviewer": false,
      "in_credits": true,
      "has_commits": false,
      "primary_role": "Maintainer"
    }
  ]
}
```

---

### 4.6. Git Commits & Patch Management API

#### `GET /api/commits/{version_name}/list`
#### `GET /api/commits`
Retrieve paginated commit history and contributor summaries.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `page` (query): Page number (default 1).
  - `limit` (query): Commits per page (default 50).
  - `q` (query): Filter by commit subject, hash prefix (matched case-insensitively against `HEX(commit_hash)`), or author name.
- **Response**:
```json
{
  "version": "v3.0",
  "page": 1,
  "limit": 50,
  "total": 1250,
  "commits": [
    {
      "commit_id": 1001,
      "commit_hash": "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
      "author_name": "Linus Torvalds",
      "author_email": "torvalds@linux-foundation.org",
      "commit_date": "2005-04-16T15:20:36",
      "subject": "Linux-2.6.12-rc2"
    }
  ]
}
```

#### `GET /api/commits/{version_name}/detail/{commit_id_or_hash}`
#### `GET /api/commit`
#### `GET /api/version/{version_name}/commit/{commit_id_or_hash}`
Retrieve detailed metadata for a commit, including multi-contributor trailers and modified files.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `commit_id_or_hash` (path) / `commit_id` (query): Commit integer ID or 40-character SHA-1 hash.
- **Response**:
```json
{
  "commit_id": 1001,
  "commit_hash": "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
  "author_name": "Linus Torvalds",
  "author_email": "torvalds@linux-foundation.org",
  "commit_date": "2005-04-16T15:20:36",
  "subject": "Linux-2.6.12-rc2",
  "message": "Full commit body message...",
  "trailers": [
    { "key": "Signed-off-by", "value": "Linus Torvalds <torvalds@linux-foundation.org>" }
  ],
  "files_changed": [
    { "path": "Makefile", "status": "M" }
  ]
}
```

#### `GET /api/commits/timeline`
#### `GET /api/commits/{version_name}/timeline`
#### `GET /api/version/{version_name}/timeline`
Retrieve an anchored, bi-directional chronological commit timeline relative to a release tag or version commit. Resolves the version's anchor commit (e.g. `v3.0` -> commit 854 "Linux 3.0") and navigates forward into newer commits (`direction="future"`), backward into older commits (`direction="past"`), or centered around the anchor (`direction="around"`). Uses primary key indexed ordering on `commit_id` for instantaneous (<0.002s) execution.
- **Parameters**:
  - `version_name` (path): Kernel version string, branch, or commit SHA.
  - `limit` (query): Maximum commits to retrieve (default 50).
  - `direction` (query): Seek direction: `"past"` (commits leading up to or before anchor, default), `"future"` (commits after anchor), or `"around"` (centered around anchor commit).
  - `cursor_id` (query): Optional integer commit ID pagination cursor.
- **Response**:
```json
{
  "version": "v3.0",
  "direction": "past",
  "anchor_commit": {
    "commit_id": 854,
    "commit_hash": "02f8c6aee8df311261e6da8ba96bd41704aa9623",
    "subject": "Linux 3.0",
    "author": { "name": "Linus Torvalds", "email": "torvalds@linux-foundation.org" },
    "author_date": 1311299908,
    "author_date_iso": "2011-07-21 18:58:28"
  },
  "total_commits": 50,
  "has_past": true,
  "has_future": true,
  "next_past_cursor": 804,
  "next_future_cursor": 855,
  "timeline": [
    {
      "commit_id": 854,
      "commit_hash": "02f8c6aee8df311261e6da8ba96bd41704aa9623",
      "author": { "name": "Linus Torvalds", "email": "torvalds@linux-foundation.org" },
      "author_date": 1311299908,
      "author_date_iso": "2011-07-21 18:58:28",
      "subject": "Linux 3.0",
      "files_count": 1,
      "contributors": []
    }
  ],
  "top_contributors": [
    { "name": "Linus Torvalds", "email": "torvalds@linux-foundation.org", "commits_count": 18 }
  ]
}
```

#### `GET /api/commits/{version_name}/search`
#### `GET /api/commits/search`
Fast full-text search across commit subjects, messages, author names, and hashes.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `q` (query): Search query string (minimum 2 characters).
  - `limit` (query): Maximum matches to return (default 50).
- **Response**:
```json
{
  "version": "v3.0",
  "query": "ext4",
  "count": 4,
  "commits": [
    {
      "commit_id": 412,
      "commit_hash": "748805f9c4262145b23d578ebc61141c2ec71253",
      "author": { "name": "Theodore Ts'o", "email": "tytso@mit.edu" },
      "subject": "ext4: fix ext4_da_write_end bug",
      "date": "2011-07-02"
    }
  ]
}
```

#### `GET /api/commits/{version_name}/diff/{commit_hash}`
Structured unified diff of one commit against its **first parent** (root commits diff against the empty tree; merges show what the merge brought into the first-parent line). Read straight from Git (`git diff -M -U<context> <parent> <commit>`), so it works for any commit in the repository, parsed or not.
- **Parameters**:
  - `commit_hash` (path): 4–40 hex digits (anything else → `400`; unknown commit → `404`).
  - `context` (query): Context lines per hunk (default 3, 0–20).
- **Limits**: Git output is streamed and cut at 8 MiB; at most 6000 rows are returned. Past the row cap the remaining rows are only counted, so `additions`/`deletions` stay exact and affected files carry `truncated: true` (their remaining hunks are omitted). Rows longer than 400 characters are clipped.
- **Rows**: `[kind, old_line, new_line, text]` with `kind` `"+"`, `"-"` or `" "`; the side a row does not exist on is `null`.
- **Response**:
```json
{
  "commit_hash": "c3b92c8787367a8bb53d57d9789b558f1295cc96",
  "parent_hash": "6a0596583fadd15dca293736114abdea306d3d7c",
  "is_merge": false,
  "context": 0,
  "additions": 1,
  "deletions": 1,
  "truncated": false,
  "files": [
    {
      "path": "Makefile", "old_path": "Makefile", "status": "M",
      "additions": 1, "deletions": 1, "binary": false, "truncated": false,
      "hunks": [
        {
          "old_start": 4, "old_count": 1, "new_start": 4, "new_count": 1, "heading": "SUBLEVEL = 0",
          "lines": [["-", 4, null, "EXTRAVERSION = -rc10"], ["+", null, 4, "EXTRAVERSION ="]]
        }
      ]
    }
  ]
}
```
`status` is `A`, `M`, `D` or `R` (for renames `old_path` differs from `path`).

#### `GET /api/commits/{version_name}/history/{path:path}`
#### `GET /api/commits/{version_name}/history?path={path}`
#### `GET /api/version/{version_name}/file/{path:path}/history`
Retrieve chronological Git commit history touching a specific file up to a given version/tag/commit, along with immediate `last_changed` metadata.
- **In-Memory LRU Caching**: Leverages a 1024-entry thread-safe LRU cache (`_cached_git_file_history`) on `(rev, rel_path, limit)`, providing instantaneous (<0.05ms) sub-millisecond responses on repeated queries without spawning subprocesses.
- **Resilience Guard**: Wrapped with an 8-second execution timeout guard returning `HTTP 504 Gateway Timeout` on prolonged Git operations. Caller services (`FilesystemService`) safely fallback to `None` if unindexed or missing, preventing file viewer crashes.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string, tag, branch, or commit SHA.
  - `path` (path or query): Relative file path within the repository (e.g. `"init/main.c"`).
  - `limit` (query): Maximum commits to retrieve (default 50, maximum 200).
- **Response**:
```json
{
  "version": "v3.0",
  "path": "init/main.c",
  "total_commits": 50,
  "last_changed": {
    "commit_hash": "2f40b79cf44e311f42d2a02b378170c8a3014a40",
    "commit_hash_short": "2f40b79cf4",
    "author_name": "Linus Torvalds",
    "author_email": "torvalds@linux-foundation.org",
    "author_date": 1311299908,
    "author_date_iso": "2011-07-21 18:58:28",
    "subject": "Linux 3.0"
  },
  "commits": [
    {
      "commit_hash": "2f40b79cf44e311f42d2a02b378170c8a3014a40",
      "commit_hash_short": "2f40b79cf4",
      "author_name": "Linus Torvalds",
      "author_email": "torvalds@linux-foundation.org",
      "author_date": 1311299908,
      "author_date_iso": "2011-07-21 18:58:28",
      "subject": "Linux 3.0"
    }
  ]
}
```

#### `POST /api/commits/{version_name}/format_patch`
#### `POST /api/commits/format_patch`
#### `POST /api/version/{version_name}/patch/format`
#### `POST /api/patch/format`
Generate an authentic RFC-2822 standard email formatted patch file (`git format-patch`) from in-browser virtual editor modifications.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - **Body** (`application/json`):
    ```json
    {
      "file_path": "fs/ext4/super.c",
      "original_content": "int a = 1;\nint b = 2;\n",
      "modified_content": "int a = 1;\nint b = 3;\n",
      "commit_subject": "ext4: update b value",
      "commit_body": "Explain the change (optional; defaults to \"Update <file>.\").",
      "author_name": "Kernel Developer",
      "author_email": "dev@kernel.org"
    }
    ```
- **Behavior**: The diffstat (`N +-`, insertions/deletions) is computed from the unified diff; `diff` is empty and no `diff --git` header is emitted when the contents are identical.
- **Response**:
```json
{
  "diff": "--- a/fs/ext4/super.c\n+++ b/fs/ext4/super.c\n@@ -1,2 +1,2 @@\n int a = 1;\n-int b = 2;\n+int b = 3;\n",
  "formatted_patch": "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\nFrom: Kernel Developer <dev@kernel.org>\nSubject: [PATCH] ext4: update b value\n\n--- a/fs/ext4/super.c\n+++ b/fs/ext4/super.c\n@@ -1,2 +1,2 @@\n int a = 1;\n-int b = 2;\n+int b = 3;\n-- \nKernelInfo-Parser\n"
}
```

---

### 4.7. Visual Modeling & Analysis Tools API

#### `GET /api/tools/{version_name}/pahole/{struct_name}`
#### `GET /api/struct/layout`
#### `GET /api/version/{version_name}/struct/layout/{struct_name}`
#### `GET /api/version/{version_name}/pahole/{struct_name}`
Pahole-style memory alignment, member offset calculation, and 64-byte cacheline analysis for C struct and union definitions. Prioritizes struct definitions with AST member containers over forward declarations.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `struct_name` (path or query): C struct identifier (e.g. `"task_struct"`).
- **Response**:
```json
{
  "version": "v3.0",
  "struct_name": "struct task_struct",
  "file_path": "include/linux/sched.h",
  "line_s": 1220,
  "line_e": 1573,
  "total_size": 1304,
  "alignment": 8,
  "padding_bytes": 78,
  "cache_lines_used": 21,
  "members": [
    {
      "name": "state",
      "type": "long",
      "size": 8,
      "alignment": 8,
      "offset": 0,
      "padding_before": 0,
      "cacheline_idx": 0,
      "cacheline_offset": 0,
      "crosses_cacheline": false
    },
    {
      "name": "stack",
      "type": "void *",
      "size": 8,
      "alignment": 8,
      "offset": 8,
      "padding_before": 0,
      "cacheline_idx": 0,
      "cacheline_offset": 8,
      "crosses_cacheline": false
    }
  ],
  "optimization": {
    "current_size": 1304,
    "optimized_size": 1226,
    "bytes_saved": 78,
    "suggested_order": ["state", "stack", "flags", "..."]
  }
}
```

#### `GET /api/tools/{version_name}/callgraph/{function_name}`
#### `GET /api/version/{version_name}/callgraph/{function_name}`
Retrieve bidirectional caller and callee execution flow. Consolidates multiple call sites of the same function with call frequencies and coordinate tracking.
- **Callers** are the calls to the function's AST found in this version's blobs, named after the AST of the tag containing the call. **Callees** are the calls inside the function's lines, each with the files defining the callee in this version (`file_path` is null when none does).
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `function_name` (path): C function identifier.
- **Response**:
```json
{
  "function": "ext4_fill_super",
  "function_name": "ext4_fill_super",
  "version": "v3.0",
  "file": "fs/ext4/super.c",
  "file_path": "fs/ext4/super.c",
  "line_s": 3480,
  "line_e": 3890,
  "caller_count": 1,
  "callee_count": 64,
  "callers": [
    { "name": "ext4_mount", "caller_name": "ext4_mount", "file_path": "fs/ext4/super.c", "line_no": 4830, "call_count": 1, "lines": [4830] }
  ],
  "callees": [
    { "name": "EXT4_SB", "callee_name": "EXT4_SB", "file_path": "fs/ext4/ext4.h", "line_no": 3528, "call_count": 4, "lines": [3528, 3594, 3596, 3706] }
  ]
}
```

#### `GET /api/tools/diff/versions`
#### `GET /api/version/diff`
#### `GET /api/diff/versions/{v1}/{v2}`
Compare file changes between two revisions (release tags, branches, or commit SHAs).
- **Parameters**:
  - `v1` / `version_a` (query or path): Base revision (e.g. `"v3.0"` or `"1052cff317e7"`).
  - `v2` / `version_b` (query or path): Target revision (e.g. `"v3.1"`).
  - `path` (query): Optional directory prefix filter.
- **Behavior**: See §2.8. `source` is `"git"` when both trees came from Git (Path C) and `"db"` for the database fallback. `fid`, `fid_old`, `fid_new` are indexed `blob_id`s, or `0` when the blob was never parsed. `changes` is sorted by path.
- **Response**:
```json
{
  "v1": "v3.0",
  "v2": "v3.1",
  "path": "",
  "source": "git",
  "summary": {
    "added_count": 1065,
    "removed_count": 762,
    "modified_count": 7032,
    "renamed_count": 161,
    "unchanged_count": 28828
  },
  "changes": [
    { "type": "added", "file": "Documentation/ABI/stable/firewire-cdev", "file_path": "Documentation/ABI/stable/firewire-cdev", "fid": 0 },
    { "type": "renamed", "file": "Documentation/DocBook/media/dvb/dvbstb.pdf", "file_path": "Documentation/DocBook/media/dvb/dvbstb.pdf", "old_file": "Documentation/DocBook/dvb/dvbstb.pdf", "fid": 13635 },
    { "type": "modified", "file": "Makefile", "file_path": "Makefile", "fid_old": 15495, "fid_new": 0, "symbols_added": 0, "symbols_removed": 0 }
  ],
  "added": [ { "file": "Documentation/ABI/stable/firewire-cdev", "fid": 0 } ],
  "modified": [ { "file": "Makefile", "fid_old": 15495, "fid_new": 0 } ],
  "renamed": [ { "file": "Documentation/DocBook/media/dvb/dvbstb.pdf", "old_file": "Documentation/DocBook/dvb/dvbstb.pdf", "fid": 13635 } ],
  "deleted": []
}
```

#### `GET /api/tools/diff/file`
#### `GET /api/diff/file`
Unified Git diff of a single file between two revisions.
- **Parameters**:
  - `version_a` (query, required): Base revision.
  - `version_b` (query, required): Target revision.
  - `path` (query, required): Repository-relative file path (validated with `is_safe_rel_path`).
  - `old_path` (query): Base-side path for renamed files.
- **Behavior**: Both revisions are resolved with `git rev-parse --verify` (option-like names are rejected) and diffed with `git diff -M`. Output above 512 KiB is truncated (`truncated: true`).
- **Response**:
```json
{
  "v1": "v3.0",
  "v2": "v3.1",
  "path": "Makefile",
  "old_path": "Makefile",
  "diff": "diff --git a/Makefile b/Makefile\n...",
  "additions": 4,
  "deletions": 3,
  "is_binary": false,
  "truncated": false
}
```

#### `GET /api/tools/{version_name}/diff/kconfig`
#### `GET /api/version/diff/kconfig`
#### `GET /api/diff/kconfig/{v1}/{v2}`
Compare Kconfig source files and option definitions between two revisions (release tags, branches, or commit SHAs).
- **Parameters**:
  - `version_name` (path): Context kernel version string.
  - `v1` (query): Base revision (e.g. `"v3.0"`).
  - `v2` (query): Target revision (e.g. `"v3.1"`).
- **Behavior**: See §2.8 Level 3. `files` is computed from the Git trees and is always complete; `symbols` only covers Kconfig files indexed on every side (`unindexed_files` lists the rest). An option is `changed` when its set of `(file, type, prompt, def_val)` definitions differs.
- **Response**:
```json
{
  "v1": "v3.0",
  "v2": "v3.1",
  "summary": {
    "added": 0,
    "removed": 39,
    "changed": 0,
    "files_added": 12,
    "files_removed": 11,
    "files_modified": 174
  },
  "added": [],
  "removed": ["A2232"],
  "changed": [],
  "symbols": [
    {
      "name": "A2232",
      "change": "removed",
      "before": [ { "file": "drivers/staging/generic_serial/Kconfig", "type": 2, "prompt": "Commodore A2232 serial support (EXPERIMENTAL)", "def_val": "" } ],
      "after": []
    }
  ],
  "files": [ { "file": "drivers/staging/tty/Kconfig", "change": "removed" } ],
  "unindexed_files": ["arch/arm/Kconfig"]
}
```

---

## 5. Frontend Client Architecture & Module Specifications

The frontend is a vanilla ES-Module single-page application requiring zero node/npm build steps.

```
webapp/frontend/js/
├── api.js                 # ApiClient: centralized REST fetch client with offline fallbacks
├── sync.js                # SyncManager: server change tokens, version stamps, new-build reload
├── db.js                  # LocalDatabase: IndexedDB offline cache manager
├── state.js               # StateStore: reactive pub/sub event store with localStorage persistence
├── url_sync.js            # UrlSync: bi-directional hash router
├── app.js                 # Application bootstrap orchestrator
├── components/
│   ├── tabs.js            # Multi-pane split manager (single, horizontal, vertical)
│   ├── context_menu.js    # Context-adaptive menus (CodeView, Explorer, NodeMap, Commits)
│   ├── command_palette.js # Ctrl+K search (files, symbols, Kconfig, commits) & Ctrl+Shift+P commands
│   ├── modal.js           # Shared modal dialog (`onClose` callback, e.g. to resolve a confirmation) + download helper
│   ├── patch_modal.js     # In-browser patch staging (edit → git format-patch)
│   ├── symbol_history_modal.js # Release slider over the history of a definition or Kconfig symbol
│   ├── ref_picker.js      # Git reference picker popover (release tags + searchable commits)
│   └── toast.js           # Ephemeral status toasts
├── utils/
│   ├── clipboard.js       # Universal fallback clipboard copier (navigator + execCommand)
│   ├── debounce.js        # Function debouncer
│   └── definition_picker.js # Go to Definition: the definition the active ARCH/configuration compiles
└── views/
    ├── code_view/
    │   ├── virtual_editor.js     # Virtualized line scroller with minimap
    │   ├── ast_overlay.js        # Disjoint narrowest-interval AST token mapping & syntax colors
    │   ├── fidelity_clipboard.js # 100% copy fidelity raw line buffer extractor
    │   └── blame_view.js         # Interactive git blame annotations
    ├── nodemap/
    │   ├── nodemap_controller.js # Two.js canvas controller, zoom/pan, export
    │   ├── node_renderer.js      # Real-time dragging DOM renderer
    │   ├── edge_router.js        # Cubic bezier spline router
    │   └── node_model.js         # Node and wire data structures
    ├── kconfig/
    │   ├── kconfig_view.js       # Web menuconfig tree, symbol inspector, per-ARCH configurations
    │   ├── kconfig_engine.js     # scripts/kconfig port: values, legal edits, .config/defconfig I/O
    │   ├── kconfig_parser.js     # .config reader & raw (engine-off) writer
    │   ├── menuconfig_tui.js     # mconf-style terminal menuconfig
    │   ├── directive_evaluator.js# #ifdef conditional code dimming (+ ARCH compiler macros)
    │   └── build_condition.js    # Kbuild build-condition evaluation & explanation
    ├── maintainers/maintainers_view.js
    ├── commits/commits_view.js
    ├── diff/diff_view.js         # Compare view: files & Kconfig between any two tags/commits
    ├── treemap/treemap_view.js   # Squarified source treemap (bytes / files)
    └── docs/
        ├── rst_renderer.js       # reStructuredText / legacy plain-text → escaped HTML renderer
        └── doc_page.js           # kernel.org-style documentation page (sidebar, TOC, quick search)
```

### 5.1. `ApiClient` (`webapp/frontend/js/api.js`)
Handles unified HTTP communication, response caching, offline fallback, and network activity tracking:
- **Global Loading Indicator & Anti-Flicker Lifecycle**:
  - Replaces legacy static status bar with a sleek, floating circular loading spinner in the bottom-right viewport (`#global-loading-spinner`).
  - Automatically tracks in-flight `fetch` requests via `this.activeRequests`.
  - Employs a 150ms anti-flicker debounce timer so sub-150ms and IndexedDB cache hits never flash the UI.
  - When network fetch duration exceeds 150ms, fades in the glowing spinning indicator and dispatches `app:loading` event; smoothly fades out when all requests finish.
- `getVersions()`: Fetches release list.
- `getSync({fresh})`: Server change tokens (`GET /api/sync`, never cached; `fresh` adds `?fresh=true`); `onSyncHeader` receives the `X-KIP-Sync` header of every response; `versionStamps` / `_vk(version)` stamp version-scoped cache keys (§2.11).
- `getDirectoryTree(version, path, depth)`: Fetches directory tree.
- `getFileContent(version, path)`: Retrieves raw text and AST tokens.
- `getTreemap(version, maxDepth, path)`: Retrieves hierarchical squarified codebase treemap.
- `getCompileCommands(version, arch)`: Retrieves Clang compilation commands database.
- `resolveInclude(version, header, astId, currentFile, arch)`: Resolves `#include` directive targets for `arch` (default `state.definitionContext().arch`, part of the cache key).
- `getIncludeSymbols(version, astId, options)`: Fetches imported symbols for an include, supporting fallback lookup options (`filePath`, `line`, `header`, `tagId`).
- `getSymbolDetail(version, name, arch, fromFile)` / `getSymbolXref(version, name, limit, arch)`: Symbol inspection; `arch` defaults to `state.definitionContext().arch` and is part of the cache key, as is `fromFile` (the file the symbol is used in, §2.7 item 4).
- `getBuildConditions(version, paths, arch)`: Kbuild build conditions of files (`GET /api/kconfig/{v}/build`).
- `lookupSymbols(version, query, limit)`: Fast typeahead symbol name suggestion list.
- `getSymbolHistory(version, path, line, astId, { name, includeRc })` / `getTagTimeline(tagId, { includeRc })`: Release-by-release history of a definition (sends the definition ARCH).
- `getKconfigHistory(version, name, { includeRc })`: Release-by-release history of a Kconfig symbol with property-level changes.
- `getHistoryCommits({ path, to, from, lineS, lineE })`: Commits behind one history change (`git log -L`).
- `getKconfigGraph(version, arch, symbol, depth)`: Full architecture dependency DAG graph.
- `getKconfigDiff(versionA, versionB)`: Cross-release KConfig symbol definition and default differences.
- `formatPatch(version, payload)`: Generates RFC-2822 standard git format patch from content edits.
- `getMaintainersOverview(version, query)` / `getMaintainerSection(version, secId)`: Subsystems.
- `getDevelopers(version, options)`: Full kernel persona directory with role filtering and activity/alpha sorting.
- `getPersonProfile(version, idOrEmail)`: Developer profiles.

### 5.2. `AstOverlay` (`webapp/frontend/js/views/code_view/ast_overlay.js`)
Implements two-layer hybrid highlighting with map prioritization and client-side fallback:
1. **File-Type Scoped Syntax Parsing (`setFileInfo`)**:
   - Explicitly scopes C keywords (`C_KEYWORDS`), C standard/kernel types (`C_TYPES`), and C preprocessor directives (`#include`, `#define`, `#ifdef`) strictly to C-type files (`ftype === 1` or `.c`/`.h`/`.i` paths).
   - In raw files (Makefiles, documentation, text files) and Kconfig files, prevents C keywords (e.g. `if`, `else`, `return`, `default`) and `#` preprocessors from false-positive C styling, correctly preserving hash comments and plain text tokenization.
2. **Map Prioritization & Narrowest-Interval Disjoint Token Mapping**:
   - Sorts spatial map tokens by width descending (`(char_e - char_s)`).
   - Assigns character intervals to an array `charToken[i]`, ensuring narrow inner tokens (declarators, symbol references, variables) overwrite and take precedence over enclosing multi-line scopes without shadowing.
   - When specific AST tokens exist for a line, prioritizes map information (`renderWithAstTokens`).
3. **Client-Side Lexical & Semantic Fallback (`renderLexical`)**:
   - On lines without AST tokens or lines only covered by broad multi-line container envelopes (`type_id` in 1, 21, 69, 70, 71, 72), automatically reverts to the client-side parser.
   - Accurately tokenizes C/C++ keywords (`.tok-keyword`), types (`.tok-type`), strings (`.tok-string`), numbers (`.tok-number`), operators/punctuation (`.tok-punct`), and comments (`.tok-comment`) when viewing C-type files.
   - Ensures non-symbol tokens (keywords, literals, comments) are strictly non-clickable (no `.ast-token`), while function calls and identifiers receive interactive `.ast-token` badges for context inspection.
4. **Include Directive Tokenization (`renderIncludeLine`)**:
   - Reliably isolates `#include ` directives across all C source and header files (e.g. `init/do_mounts.c`).
   - Renders `#include ` as non-clickable `<span class="tok-preproc">` and the header target (`<linux/...>` or `"header.h"`) as `<span class="tok-include ast-token ast-include">`.
   - Binds server AST ID and type (`data-type-id="78"`) when mapped, while preserving interactive click resolution for all unmapped includes.
   - Slicing coordinate correction: `abs_ce = max(abs_cs, raw_ce)` in `filesystem_service.py` preserves 1-indexed column intervals with closing quotes and angle brackets intact.

### 5.3. `NodeRenderer` & Drag Controller (`webapp/frontend/js/views/nodemap/`)
- Canvas pan and zoom via matrix transformations (`Two.Group`).
- `attachDrag()`: Updates `nodeEl.style.left` and `nodeEl.style.top` in real time during `mousemove` scaled by canvas zoom:
   ```javascript
   const zoom = this.callbacks.getZoom() || 1.0;
   node.x = initialNodeX + (moveEvent.clientX - startX) / zoom;
   node.y = initialNodeY + (moveEvent.clientY - startY) / zoom;
   nodeEl.style.left = `${node.x}px`;
   nodeEl.style.top = `${node.y}px`;
   ```
- Rerenders cubic bezier SVG wires smoothly without lag.

### 5.4. `FidelityClipboard` (`webapp/frontend/js/views/code_view/fidelity_clipboard.js`)
- Intercepts clipboard copy (`Ctrl+C` / `Cmd+C` / right-click copy).
- Slices lines from the clean raw source code array (`rawLines`), ensuring 100% copy fidelity without line numbers, gutter metadata, or HTML formatting artifacts.

### 5.5. `LocalDatabase` & Storage Settings (`webapp/frontend/js/db.js`, `webapp/frontend/js/app.js`)
- **Offline Cache Stores**: Persistent IndexedDB (`KernelInfoIDE_v2`, v3) caching `meta`, `files`, `symbols`, `kconfigs`, `maintainers`, `commits`, `tools`, `tabs_state`, and `nodemap_projects`.
- **Server Synchronization (`syncManager`, `js/sync.js`, §2.11)**: on bootstrap and on every `X-KIP-Sync` header, a changed `db` token purges all data caches (`files`, `symbols`, `kconfigs`, `maintainers`, `commits`, `tools`) and stores the token in `meta/db_instance_hash`; changed `parsed`/`git` tokens refresh the version stamps (`meta/version_stamps`) and drop the cached entries of changed versions.
- **Cache-First Client Policy**:
  - All static analysis and versioned data requests query IndexedDB first.
  - If a cached record exists, it is served immediately without making network calls, ensuring instantaneous loading for heavy structures (such as Kconfig hierarchy trees and source file buffers).
  - Dynamic keyword searches bypass the cache and query the REST API directly.
- **Storage Management API**:
  - `localDB.clearStore(storeName)`: Clears records for a single object store.
  - `localDB.clearDataStores()`: Clears all domain data stores while preserving workspace session tabs and nodemap diagrams.
  - `localDB.deleteDataWhere(predicate)`: Deletes the data-store entries whose key matches (one key-cursor scan per store); returns the count.
  - `localDB.clearAllStores()`: Concurrently clears all configured object stores.
  - `localDB.getStorageEstimate()`: Queries `navigator.storage.estimate()` returning `{ usage, quota }`.
  - `localDB.getStoreCounts()`: Returns a dictionary `{ [storeName]: count }` of stored record counts.
- **Revamped Settings Popover (`#offline-badge`)**:
  - Clicking the top-right Online/Offline badge opens an anchored settings popover dropdown.
  - **Database & Network**: Real-time network status (`navigator.onLine`) alongside the verified DB Instance Hash badge with synchronization indicator (`● Synced`).
  - **🔄 Check for Updates** (`app.checkForUpdates()`, disabled offline): runs `syncManager.checkNow()` (§2.11) and shows *Checking… / Up to date / Updated / Failed* on the button, with a toast listing what changed (new frontend build, database rebuilt, versions re-parsed or moved, new or removed versions/branches).
  - **Cached Data Stores Breakdown**: 6-card metric grid showing counts for Files, Symbols, Kconfig, Maintainers, Commits, and Tools, plus total entries and disk footprint in MB.
  - **Granular Per-Store Clear Chips**: Fast one-click buttons to flush individual stores (`Clear Kconfig`, `Clear Files`, `Clear Symbols`, etc.).
  - **Storage Actions**:
    1. **Clear All Offline Cache**: Flushes all domain data stores (`files`, `symbols`, `kconfigs`, `maintainers`, `commits`, `tools`).
    2. **Reset Tabs & Workspace State**: Clears saved tab state from `localStorage` and `tabs_state`, resetting the active workspace session.
    3. **Full Local Reset & Reload**: Prompts confirmation, wipes IndexedDB, purges `localStorage`, clears Service Worker/browser caches (`window.caches`), and reloads the application.

### 5.6. Context Menu & Cross-References Modal (`webapp/frontend/js/components/context_menu.js`)
- **Dual Mouse Navigation**: Left-click on AST tokens triggers quick inspector popup; right-click opens the contextual action menu (Go to Definition, Find References, Copy Symbol, Expand AST, NodeMap).
- **Go to Definition** (menu items, middle-click default action, documentation `func()` links) goes through `openDefinition()` (`utils/definition_picker.js`, §5.19), so per-architecture symbols open the definition of the active ARCH/configuration. *Open in Kconfig Viewer* (and the Kconfig links of the docs and compare views) open the KConfig tab on the active profile's ARCH (`state.kconfigArch()`, `x86_64` by default).
- **Context-Adaptive Kconfig Navigation**:
  - Automatically identifies Kconfig tokens across both C files and Kconfig files (via spatial AST types `88..118` from `m_map_ast`, `CONFIG_*` prefix matching, or Kconfig file category `ftype === 2`).
  - Replaces "Go to Definition" and "Find References / XRef" with **"Open in Kconfig Viewer"** (`⚙️`), preventing meaningless symbol lookups on configuration symbols.
  - Clicking "Open in Kconfig Viewer" strips any `CONFIG_` prefix, opens/focuses the Kconfig explorer tab, highlights the matching symbol row, and immediately loads dependency constraints and auto-solve options in the inspector.
- **Clean Cross-References (XRef) Modal**:
  - Distinct top **"Definition"** card displaying primary definition file path, line numbers, and readable AST type badge (`FunctionDecl`, `StructDecl`, `EnumConstant`, `CPPro_define_macro`), with direct 1-click jump-to-code navigation.
  - Categorized **"References & Usages"** list showing caller context (enclosing function), file paths in the current revision, line numbers, and readable reference type badges (`Call`, `MemberRef`, `TypeUsage`, `DeclRef`, `MacroExp`).
  - Purely displays indexed cross-references without non-functional search fallback buttons.
- **Include Symbols Inspection**:
  - Clicking or right-clicking `#include` directives provides an **"Inspect Included Symbols"** (`🔍`) action.
  - Queries `api.getIncludeSymbols` with client-provided AST ID, file path, line coordinate, and header query fallback.
  - Displays the floating `IncludeSymbolsPopover` adjacent to the clicked token.

### 5.7. `IncludeSymbolsPopover` (`webapp/frontend/js/components/include_symbols_popover.js`)
- **Anchored Floating Popover**: Clamped dynamically within viewport bounds adjacent to clicked `#include` lines.
- **Live Search Filtering**: Client-side interactive search input (`#include-symbols-search`) with real-time text matching, substring highlighting (`<mark>`), and instant symbol filtering.
- **Category Filter Pills**: Interactive pills with live counts (`All`, `Functions`, `Structs`, `Macros`, `Typedefs`, `Enums`, `Variables`) enabling one-click category filtering.
- **Category Badges & Definition Coordinates**: Color-coded badges for symbol categories (`Func`, `Struct`, `Macro`, `Typedef`, `Enum`, `Var`), definition file paths, and line numbers (`def_file:line_s`).
- **Navigation & Queuing**:
  - Left-click on any symbol row immediately navigates to its definition (`state.openTab`).
  - Middle-click opens the definition in a new tab without dismissing the popover, allowing rapid multi-tab queuing.
  - Top-bar **"📄 Open Header"** action button quickly opens the included header file.

### 5.8. `KconfigView` (`webapp/frontend/js/views/kconfig/kconfig_view.js`) & `KconfigEngine` (`webapp/frontend/js/views/kconfig/kconfig_engine.js`)
- **Engine**: `KconfigEngine` decodes the model of `GET /api/kconfig/{v}/tree` (`decodeModel`) and evaluates it with kconfig's algorithms (twin of `parser/kconfig_ast/kconfig_eval.py`): `value`, `visibility`, `revDep`, `getType` (tristate degrades to bool without `MODULES`), `choiceSelection`, `isChangeable`, `menuVisible` (`menu_is_visible`); legal setters `setTristate` / `setValue` / `toggle` (refuse what kconfig refuses; `refusalReason` explains why), `toggleTarget` (the value `toggle` would set), `previewLowered(name, text)` (what other options lose if the value is set: bool/tristate options whose value drops and other options no longer written, each with the reason (`depends on …`, `no longer selected: …`, `prompt hidden: …`), members of the same choice excluded, the configuration restored afterwards; `null` when kconfig refuses the value; about 150 ms on a v3.0 x86_64 model); `loadConfig` (conf_read report: unknown, invalid, warnings), `loadAssignments`, `setAllNewSymbols`, `writeConfig` (full `.config`, byte-identical to `conf`), `writeDefconfig` (`make savedefconfig`), `configValues`, `exprStr(id, withValues)`, `orTerms`, `menuPath`.
- **Engine on/off** (`state.kconfigEngineActive`, persisted, also gates `#ifdef` dimming): on, every edit goes through the engine so only valid configurations exist, and the published values are the resolved `.config` values. Off, values are edited raw (no constraints, export marked unvalidated); turning it back on resolves the raw values through kconfig and reports how many were adjusted.
- **Architectures**: the dropdown lists `GET /presets` grouped by source directory (ARCH values such as `x86_64`, `i386`, `x86`, `sparc64`). Each `version|ARCH` keeps its own configuration in `state.kconfigProfiles`; switching restores it, or loads the ARCH's default defconfig (`canonical_defconfig`) the first time. The active profile is published as `state.kconfigAssignments` (bare and `CONFIG_` keys) for dimming.
- **Tree (web menuconfig)**: entries from the finalized model in menuconfig order, with implicit submenus. Only entries menuconfig shows are listed (`menuVisible`); **Show All** adds invisible prompted entries (dimmed, tooltip with the failing prompt condition). Markers follow mconf: `[*]`/`[ ]` bool, `<*>`/`<M>`/`< >` tristate, `-*-`/`-M-` forced by select, `(X)`/`( )` choice values, `(value)` int/hex/string, `--->` submenus, `*** comment ***`; choices show their selected member. Clicking the marker changes the value (toggle, choice selection, or a prompt for int/hex/string); refused changes show the reason. With the engine on, lowering a value (y → m/n, m → n, from the tree, the inspector's n/m/y buttons or the TUI's Space/Y/M/N) that other options need asks first: a dialog lists each option that would drop with its current and new value and why, and the change is applied only with **Set to … anyway** (Close, Escape or clicking a listed option, which inspects it, cancels). Auto-solve applies its changes without asking. Search filters symbol names and prompts and shows each match's menu location.
- **Inspector**: value with its origin (explicit, default, forced by select, choice), n/m/y buttons enabled only for legal values (or an input for int/hex/string), evaluated prompt visibility, direct dependencies and "Selected by"/"Implied by" terms with `[=value]` annotations, every definition for the ARCH (file:line buttons opening the code view, prompt, menu location, visibility), defaults with the active one highlighted, ranges, selects/implies, choice group, help text, files built when enabled (other architectures collapsed), directories descended into, **Find usages** (source references grouped by file, Makefile conditions, Kconfig entries referring to it), **Auto-solve** (server solver, changes applied through the engine), and the dependency graph. Symbols not declared for the ARCH list their declarations elsewhere.
- **Toolchain** (Linux 4.18+; button **Toolchain…** or the toolchain summary in the status line): compiler (GCC/Clang) and version, linker (GNU ld/LLD) and version, assembler, binutils version, `CROSS_COMPILE`, LLVM utilities, pahole/rustc versions, GCC plugin headers, build host `uname -r`; a table of every environment variable Kconfig read with an override field (plus *add any variable*); a filterable table of every toolchain check with its result, source and the options using it, each overridable (auto / force y / force n, or text for command outputs); and `$(error-if)` messages (e.g. compiler below the minimum version). *Apply* stores the profile per `version|ARCH` (`state.kconfigToolchains`), rebuilds the model with it and re-resolves the current values through it; *Reset to defaults* clears it. For older revisions the dialog explains that only environment variables matter.
- **Configurations**: **Load Defconfig** (filterable list, default one tagged), **Import .config** (conf_read in the engine; unknown/invalid entries reported), **Export .config** (`<arch>-<version>.config`, the resolved `.config` to place in the kernel tree), **Save defconfig** (minimal `make savedefconfig` file), **Validate** (values kconfig would not keep with reasons, unknown symbols, select-with-unmet-dependency warnings, *Apply kconfig resolution*), **Compare…** (resolved values against a resolved defconfig).
- **TUI** (`menuconfig_tui.js`): mconf-style modal over the same engine: `menu`/`menuconfig`/`choice` open submenus, other children are listed indented; keys Up/Down/PgUp/PgDn/Home/End, Enter, Space, Y/M/N, Esc/Left, `/` search, `?`/H help (help text plus Symbol/Type/Prompt/Defined at/Depends on/Location/Selects/Selected by), Z show all, S save `.config`, D save defconfig.

### 5.9. Universal Middle-Click Tab & Navigation Capture
The frontend implements systematic middle-click capture (`auxclick` with `e.button === 1`) across all interactive UI surfaces:
1. **Force New Tab (`forceNew: true`)**:
   - `state.openTab(tabData, targetPaneId)` accepts `tabData.forceNew`.
   - When set, bypasses duplicate tab matching and always generates a new unique tab instance in the foreground.
2. **CodeView AST Tokens**:
   - Middle-clicking any `.ast-token` in `virtual_editor.js` bypasses the context menu and directly executes the primary contextual action (`contextMenu.executeDefaultAction`) in a new foreground tab:
     - **C/Rust AST Symbols**: Queries symbol definition and jumps to the definition line in a new code tab.
     - **Kconfig Symbols**: Opens the symbol in a new Kconfig tab.
     - **Include Directives**: Resolves the target header and opens it in a new code tab.
3. **Tab Header Close**:
   - Middle-clicking any tab header in the top tab strip immediately closes the tab (`state.closeTab`), conforming to standard IDE and browser tab strip mechanics.
4. **In-Place Navigation Elevation**:
   - Where left-click navigates in-place within the current view, middle-click opens the target in a new tab:
     - **Kconfig Viewer**: Dependency badges (`.kconfig-badge-link`), tree rows (`.kconfig-node-row`), compiled file links (`.compiled-file-link`), and architecture defconfigs (`.def-item`).
     - **File Explorer**: Files in the repository tree open in a new code tab with `forceNew: true`.
     - **Maintainers Viewer**: Subsystem cards, person profile cards, and referenced file links.
     - **Commits Viewer**: Commit history cards and touched file links.
     - **Callgraph Viewer**: Caller and callee function jump links and source file links.
     - **Version Diff Viewer**: Modified and added file rows.
5. **Modal Non-Dismissing Multi-Tab Queuing**:
   - In the **Cross-References (XRef)** and **Included Symbols** modals, middle-clicking definitions or usage links opens the target code tabs with `forceNew: true` while preserving the modal backdrop, allowing users to queue multiple references into separate tabs without re-opening search modals.

### 5.10. Developer Profiles & Kconfig Compiled Source Navigation
1. **Interactive Developer Profile Modal (`showPersonModal`) & Maintainers Sub-Tab**:
   - Clicking developer names across the workspace (Commit author headers, Contributors & Signoffs lists, Commit cards, Git Blame gutters, and Blame context popovers) activates the universal Developer Profile modal (`webapp/frontend/js/components/person_modal.js`).
   - Displays avatar initials, author email, CREDITS biographical data (description, project URL, PGP keys, snail mail), Git stats (authored commits count), and maintained subsystems.
   - **Maintainers "Developers" Directory**:
     - The Maintainers view incorporates a dedicated **"Developers"** directory (`Subsystems` | `Developers` | `CREDITS` | `Patch Reviewer`) listing all 1,170+ personas from `m_maintainer_person`.
     - Supports live name/email search, category filter pills (`All`, `Maintainers`, `Reviewers`, `CREDITS`), and an Activity (subsystems count) vs. A-Z alphabetical sort toggle.
     - Selecting any developer renders their complete profile card in the detail pane with maintained subsystems (clickable to jump into the Subsystems tab) and a direct "View All Commits" action button.
   - Provides 1-click elevation actions:
     - **Open in Maintainers Tab**: Focuses or launches `state.openTab({ type: "maintainers", person: ... })`, inspecting the developer's complete subsystem roster and contribution graph.
     - **View Commits**: Focuses or launches `state.openTab({ type: "commits", query: ... })`, filtering the commit history by author name or email.
2. **Kconfig Compiled Source Files**:
   - `sym.compiled_files` records (`{"file_path": "..."}`) are normalized and displayed with source file icons (`📄 kernel/smp.c`) and associated target object badges (`smp.o`).
   - Clicking any compiled file item (`.compiled-file-link`) immediately opens the target source file in the virtual code editor (`state.openTab({ type: "code", path: filePath })`), supporting both left-click in-place navigation and middle-click multi-tab queuing.

### 5.11. Compare View: Files & Kconfig Between Any Two Revisions (`webapp/frontend/js/views/diff/diff_view.js`)
1. **Activity Bar Entry**: The **Compare** activity-bar button (`data-action="compare"`) opens a single `diff` tab (re-clicking focuses it). The tab persists `versionA`, `versionB`, and `mode` in its tab data, and its title reflects the pair (`Compare v3.0…v3.1`).
2. **Revision Selection**: Base and Target buttons open the shared `refPicker` (§5.13), so either side can be a release tag or any commit. When no base is stored, it defaults to the tag preceding the target (or the latest tag when the target is a commit). A ⇄ button swaps the sides.
3. **Files Mode** (`api.getVersionsDiff`): Status chips (added / modified / deleted / renamed, with unchanged count) act as toggle filters alongside a path filter. Rows render in pages of 500. Clicking a row expands an inline unified diff from `api.getFileDiff`; per-row buttons open the file at the base or target revision, and middle-click opens it in a new tab.
4. **Kconfig Mode** (`api.getKconfigDiff`): Shows option counts and Kconfig file counts, a warning listing unindexed Kconfig files when option coverage is partial, an **Options** list (expand for per-file `type` / `prompt` / `default` before and after, or **Inspect** to open the KConfig view on that symbol), and a **Kconfig Files** list with the same inline diff and open actions as Files mode.

### 5.12. Git Commits Timeline & Bi-Directional Pagination (`webapp/frontend/js/views/commits/commits_view.js`)
- **Clean Full-Width Search Header**: Replaced legacy direction toggle buttons (`◀ Future`, `⊙ Anchor`, `Past ▶`, and `🏷️ v3.0`) with a clean, focused typeahead search bar (`#commit-search-input`).
- **Bi-Directional "Load 50 More Commits" Pagination**:
  - **Top Pagination Button (`#btn-load-more-top`)**: Automatically renders at the top of the commit timeline when commits exist above the current viewport (`has_future`). Seamlessly fetches 50 commits using `direction="future"` and `next_future_cursor`, prepends them in continuous chronological descending order, and preserves exact vertical scroll position (`scrollTop`) without layout jumping.
  - **Bottom Pagination Button (`#btn-load-more-bottom`)**: Automatically renders at the bottom of the timeline when older commits exist (`has_past`). Fetches 50 commits using `direction="past"` and `next_past_cursor` and appends them to the bottom of the list.
  - **Search Pagination**: In commit search mode, the bottom button paginates sequentially through matching commits across pages.
- **Commit Inspection & File Jumping**: Selecting any commit card displays subject, author, UTC date, Git SHA, Lore LKML thread link, structured trailers/signoffs badges, full commit message, and touched file list with middle-click multi-tab queuing.

- **Inline Commit Diff (`#btn-commit-diff`)**: The bottom of a selected commit's detail has a **± Show changes** button. It loads `GET /api/commits/{v}/diff/{hash}` once per commit (IndexedDB `commits` store, key `cdiff_<hash>_<context>`) and renders a compact per-file diff (status badge, `+/−` counts, old/new line-number gutters). Files with more than 120 rows, or after the 25th file, start collapsed; each file body scrolls at 420px.
  - **Jump to the edited lines**: Clicking an added or context row opens the file **at the commit** on that line; clicking a removed row opens the file **at the parent commit** on the old line (deleted files always open at the parent). Hunk headers and the per-file **Open ↗** button jump to the first change. Ctrl/Cmd+click or middle-click opens a new tab. Selecting text in the diff does not navigate.
  - **Change markers in the code view**: The opened tab carries `diffMarks: { kind: "add" | "del", commit, ranges: [[start, end], ...] }`. `VirtualEditor` tints those lines green (added) or red (removed), adds a gutter bar and minimap ticks, and shows a `± <hash>: N lines changed` chip with **↑/↓** (previous/next change block) and **✕** (clear markers). Re-opening an already open file tab replaces its markers.

### 5.13. Header Version Selector & Commit Picker (`webapp/frontend/js/app.js`, `webapp/frontend/js/components/ref_picker.js`)
- **Selectors**: `.version-selector-container` holds Project, Branch, Tag (`#version-select`, releases), RC (`#rc-select`, candidates of the selected release; see §5.18), and Commit (`#commit-picker-btn`) controls. `state.currentVersion` may be a tag, a branch name, or a commit SHA; startup sanitization only resets values that are none of these.
- **Commit Picker**: `#commit-picker-btn` opens `refPicker`, a popover with a search box that lists matching release tags (`api.getVersions`) and commits (`api.getCommits`, matching subject, author, or hash prefix). Arrow keys navigate, Enter selects, and any pasted SHA or ref name not in the lists is offered as a raw revision (resolved server-side via `git rev-parse`). Selecting calls `state.setVersion(ref)`.
- **Display Sync** (`updateVersionSelectorDisplay`, on `version:changed` / `version:synced`): On a tag, the Tag dropdown selects it and the Commit button shows the tag's short SHA. On a commit, the Tag dropdown shows `— none —` and the Commit button shows the short SHA highlighted as detached. The explorer header and tab version badges show the same short form (`shortRef`, 12 hex chars).
- **Cache Keys**: Directory trees are cached under `tree2_*` and diffs under `diff2_*` / `kdiff2_*` / `fdiff_*` so responses cached before Git-native tree resolution are not reused; every version-scoped key embeds the version's stamp (`api._vk`, §2.11). Service Worker caches are described in §2.11.

---

### 5.14. Documentation Viewer (`webapp/frontend/js/views/docs/`, `webapp/frontend/css/docs.css`)
- **Activation**: `isDocumentationPath(path)` selects `*.rst`, `README*`, and `Documentation/` files that are `.txt` or extension-less (excluding `Makefile`/`Kconfig`/`Kbuild`). Such code tabs open in **Rendered** mode (`tabData.docMode = "rendered"`) unless a specific `cursorLine` was requested; the toolbar's **📖 Rendered / </> Source** button toggles back to the virtual editor, and enabling Git Blame switches to source.
- **Layout**: Mirrors https://www.kernel.org/doc/html/latest/ (Sphinx/Alabaster): a left sidebar with *The Linux Kernel* title and the tab's version, **Quick search** (in-page highlight; Enter cycles matches), **Contents** (nested local TOC with scroll-spy), **Navigation** (directory `index.rst`/`00-INDEX` and `Documentation/index.rst`), and **This Page → Show Source**, beside a serif body and a footer. Colors come from the IDE dark tokens.
- **Renderer** (`renderDocument(source, path)`): `.rst` (or `.txt` that uses rST markup) is parsed as reStructuredText: over/underlined sections (levels by first appearance, `¶` permalinks), transitions, bullet/enumerated/field/definition/line lists, `::` and quoted literal blocks, block quotes, grid and simple tables, footnotes, substitutions, hyperlink targets and labels, and directives (`code-block`/`highlight` with C and shell highlighting, admonitions, `toctree`, `contents`, `list-table`/`flat-table`/`csv-table`, `kernel-doc`/`include`/`literalinclude`/`kernel-*` as source-link cards, figures as placeholders, `c:function`-style domain entries, `versionadded`/`deprecated`, `only`, `rubric`, `topic`). Inline markup covers literals, emphasis, roles (`:doc:`, `:ref:`, `:c:func:`/`:c:type:`…, `:manpage:`, `:kbd:`, `:abbr:`, `:menuselection:`…), embedded and named links, bare URLs, `Documentation/…` paths, and kernel automarkup (`func()`, `struct foo`, `CONFIG_FOO`). Legacy plain-text docs keep aligned/indented text as preformatted blocks and promote tab-centered lines to headings. All source text is HTML-escaped.
- **Figures**: `image`/`figure`/`kernel-figure` render inline from `/api/fs/raw` (on a light backing for legibility); unrenderable formats (e.g. `.dot`) fall back to a source link.
- **kernel-doc**: `.. kernel-doc::` blocks are replaced by the extracted API documentation (`/api/docs/{v}/kernel-doc` with the directive's options): Sphinx C-domain entries with highlighted signatures, Parameters/Members, Definition (types), Description/Return/Context sections rendered as reStructuredText after translating kernel-doc markup (`@param` → bold, `%CONST` → literal, `&struct foo` → type link), and a *[source]* link to the comment's line.
- **Cross-document `:ref:`**: unresolved labels are looked up in `/api/docs/{v}/labels` and become links to `<doc>#<label>` showing the target section's title.
- **Links**: `:doc:`/toctree/`Documentation/` links navigate in place (`tabData.docHistory` backs the sidebar **← Back**; Ctrl/middle-click opens a new tab), toctree labels are filled from each target's first title, `func()`/`struct` links jump to the symbol definition via `api.getSymbolDetail`, `CONFIG_*` opens the KConfig view, directive cards open the referenced source, and in-page anchors scroll without touching `location.hash`.

### 5.15. Command Palette (`webapp/frontend/js/components/command_palette.js`)
- **Opening**: `Ctrl/Cmd+K` or `Ctrl/Cmd+P` (search), `Ctrl/Cmd+Shift+P` (commands), or the header **🔍 Search…** button.
- **Modes by prefix**: none — files (`/api/fs/search`), symbols (`api.searchSymbols`) and Kconfig options (`api.searchKconfig`) together; `>` commands; `@` symbols; `#` commits (`api.searchCommits`, selecting one switches the version); `?` Kconfig options; `:` go to line in the active code tab. Results are grouped by section with match highlighting; ↑/↓ navigate, Enter opens, Ctrl+Enter opens in a new tab.
- **Commands** (`KernelIdeApp.paletteCommands()`): compare, select commit, source treemap, export `compile_commands.json` (download via `api.getCompileCommandsDownloadUrl`), KConfig view, toggle the KConfig engine, commits, maintainers, NodeMap, documentation, explorer toggle, plus *Create patch* / *Symbol history* for the active code tab and *Switch project* entries.

### 5.16. `#ifdef` Dimming (`webapp/frontend/js/views/kconfig/directive_evaluator.js`)
- **Gate**: Only while the KConfig constraint engine is enabled (`state.kconfigEngineActive`, toggled in the KConfig view and persisted) **and** a resolved configuration is loaded (≥ 50 symbols, i.e. a defconfig; `state.kconfigConfigActive()`), for preprocessed sources (`PREPROCESSED_RE`: `.c .h .S .lds .dts .dtsi .inc`). A toolbar chip shows the number of compiled-out lines; clicking it (`VirtualEditor.toggleDimSuspended()`) temporarily shows those lines undimmed (chip marked *not dimmed*, dashed) and clicking again restores the dimming. The choice applies to every code tab until toggled back or the page is reloaded (not persisted); the count, the build banner and Go to Definition are unaffected.
- **Semantics**: Three-valued evaluation of `#if/#ifdef/#ifndef/#elif/#else/#endif` (with `\` continuations). Only `CONFIG_*` macros are known, following `autoconf.h`: `CONFIG_FOO` is defined for `y` and every string/int/hex value (even empty), `CONFIG_FOO_MODULE` for `m`, symbols that are `n` or absent are undefined; `IS_BUILTIN` is true for `y` (or an int equal to 1), `IS_ENABLED`/`IS_REACHABLE` for `y`/`m`; `IS_ENABLED/IS_BUILTIN/IS_MODULE/IS_REACHABLE`, `defined()`, `!`, `&&`, `||`, parentheses and numeric comparisons are supported. The compiler-predefined macros of the configuration's ARCH are known as well (`archMacros(arch, assignments)`): `__KERNEL__` is defined; the own architecture's macros are defined (`__x86_64__`/`__amd64__` or `__i386__` by word size, from `CONFIG_64BIT` when set, else from the ARCH value; `__arm__`, `__aarch64__`, `__powerpc__`/`__powerpc64__`, `__sparc__`/`__arch64__`, `__s390__`/`__s390x__`, `__mips__`, ...), every other architecture's macros, the other word size's macros, `__CHECKER__` and `__GENKSYMS__` are undefined, `__LP64__` follows the word size; in `#if` expressions known macros are 1 or 0. Any other macro (header guards, `__ASSEMBLY__`, `MODULE`) is unknown and a branch with an unknown condition is never dimmed, nor are its alternatives. With `partial: true` (used for the x86_64 baseline of §5.19), `CONFIG_*` symbols absent from the assignments are unknown instead of `n`. Directive lines are dimmed only when their enclosing region is compiled out.
- **Live updates**: `VirtualEditor` recomputes on `kconfig:engine:changed`, `kconfig:assignments:updated` and `kconfig:assignment:changed`.
- **Build banner**: with the same gate, `VirtualEditor.refreshBuildBanner()` fetches the open file's Kbuild condition (`api.getBuildConditions`, once per version/ARCH/path) and evaluates it against the configuration (`build_condition.js`, absent symbols are `n`). When it is `n`, a red banner above the code says the file is **not compiled** for the ARCH with the current configuration and why: the failing symbols with their values (e.g. *Needs `CONFIG_E1000E` (currently E1000E=n)*, plus how many alternative rules also fail), or the reason for `other_arch`/`no_rule` (*arch/arm/ is not built for ARCH=x86_64*, *drivers/net/Makefile does not build skeleton.o*). When it is `m`, a blue banner says the file is built as a module. The banner shows the object's Kbuild rule (`makefile:line text`), which opens the Makefile at that line. Unknown conditions (`?`, e.g. headers) show nothing. It is re-evaluated without refetching on every configuration change.

### 5.17. Treemap, Symbol History & Patch Staging
- **Treemap** (`views/treemap/treemap_view.js`, tab type `treemap`): squarified layout of `/api/version/{v}/treemap` two levels deep, Bytes/Files metric toggle, breadcrumb zoom, click a directory to zoom in or a file to open (middle-click: new tab). Opened from the explorer's folder context menu (*Show Treemap of Folder*) or the palette.
- **Symbol & Kconfig history** (`components/symbol_history_modal.js`): `showSymbolHistoryModal(version, path, line, astId, name)` (editor context menu *Symbol History Across Releases* on a symbol, *Symbol History at This Line*, palette *Symbol history at cursor line*) and `showKconfigHistoryModal(version, name)` (context menu *Kconfig History Across Releases* on a `CONFIG_*`/Kconfig token or selection, KConfig view **🕰 History** card).
  - **Slider**: one stop per release where the definition changed (introduced, modified, formatting-only, removed, re-added) plus the first and the newest indexed version; each run of releases without change collapses into a `…` gap (click expands it). ◀/▶ buttons, ←/→ keys and **Play** step through the stops. **All releases** disables collapsing; **Include RCs** refetches with `include_rc=true`. The stop at or before the version the history was opened from is selected first.
  - **Detail**: status, `prev → version` range and line counts; **Diff** (unchanged runs longer than 3 lines folded) or **Code** of each definition. Kconfig stops list only the files whose definition changed (**Show all N definitions** toggles) with a table of the changed properties (`depends on`, `inherited depends`, `select`, `default`, `type`, `prompt`, `help`...). **Commits** loads `api.getHistoryCommits` for that definition (click a hash to open the commit); **Open @ version** opens the file at the definition.
- **Patch staging** (`components/patch_modal.js`): code toolbar **✎ Patch**, editor/explorer context menu, or palette. Edit the file in a textarea (Tab inserts a tab), set subject, description and identity (remembered in `localStorage`), then *Generate patch* → `api.formatPatch`; copy or download `0001-<subject>.patch`.
- **KConfig additions**: **Import .config**, **Validate** and **Compare…** run in the browser engine (see 5.8); the per-symbol **Dependency Graph** uses `api.getKconfigGraph` (own depends-on left, selects/implies right, colored by current value, nodes clickable).

### 5.18. Projects (Multiple Repositories)
- The header **Project** selector lists `/api/projects` (parsed repositories plus *git only* ones). `api.setRepository(name)` adds `X-KI-Repo` to every request, prefixes IndexedDB cache keys with the repository, and `withRepo()` appends `?repo=` to URLs used directly (images, downloads). Switching asks for confirmation, closes the workspace tabs (`state.setRepository`), and selects the repository's latest parsed version (else its default branch).
- The **Tag** dropdown lists final releases grouped into *Parsed* and *Git only (not parsed)*; a release that so far only has candidates appears as `vX.Y (rc only)` and selects its latest candidate. The **RC** dropdown (shown when the selected release has candidates) lists `final` plus that release's `-rc#` tags, newest first, with `(git only)` marking unparsed ones; `KernelIdeApp.buildVersionCatalog()` groups tags by `releaseOf()` (`v3.1-rc4` → `v3.1`) from `/api/versions?include_rc=true`. Catalog requests (`projects`, `branches`, `versions`) are network-first so newly parsed versions appear without clearing the cache.

### 5.19. Definition Context & Picker (`state.definitionContext()`, `webapp/frontend/js/utils/definition_picker.js`)
- **Context**: `state.definitionContext()` returns `{arch, assignments, strict}`: while the KConfig engine is on with a resolved configuration (`state.kconfigConfigActive()`), the ARCH of the active profile (`kconfigProfileKey` = `version|ARCH`) and `state.kconfigAssignments` (`strict`: absent symbols are `n`); otherwise `x86_64` with a baseline (`X86`, `X86_64`, `64BIT` = `y`, `X86_32` = `n`; `strict: false`, every other symbol unknown). `api.getSymbolDetail`/`getSymbolXref`/`getSymbolHistory` send its `arch` (§2.7).
- **Picker**: `resolveDefinition(version, name, fromFile)` takes the backend's ranked `definitions` (with `fromFile`, those the file's `#include` chain reaches come first, §2.7 item 4) and, among those in the first 6 candidate files, returns the first one the context compiles: files whose Kbuild condition evaluates to `n` (`api.getBuildConditions`) are skipped, then definitions on lines `DirectiveEvaluator` compiles out with the context's assignments and `archMacros` (partial when not strict). On x86_64, `struct pt_regs` therefore opens the `__x86_64__` branch of `arch/x86/include/asm/ptrace.h` rather than its `__i386__` one, and `arch/x86/kernel/process_64.c` wins over `process_32.c`. When no candidate is known to be compiled, the backend's first choice is kept. `openDefinition(version, name, {forceNew, fromFile})` opens the result in a code tab (used by the context menu and middle-click, which pass the code view's file as `fromFile`, and by documentation links).

## 6. Programmatic Python Service Delegates (`webapp/main.py`)

For automated testing, scripting, and offline analysis, all core domain service functions are exported directly from [`webapp/main.py`](webapp/main.py):

```python
from webapp.main import (
    # Filesystem & Source Code
    get_tree,
    get_file,
    browse_path,
    get_file_by_id,
    export_compile_commands,
    get_codebase_treemap,
    
    # Symbols, AST & Tags
    search_symbols,
    lookup_symbols,
    get_symbol_detail,
    get_symbol_xref,
    get_ast_container_tree,
    get_tag_by_id,
    get_tag_timeline,
    get_include_symbols,
    
    # Kconfig & Defconfigs
    get_kconfig_defconfigs,
    get_kconfig_defconfig_content,
    get_kconfig_tree,
    get_kconfig_symbol_detail,
    get_kconfig_graph,
    get_kconfig_env_presets,
    export_kconfig_file,
    import_kconfig_file,
    search_kconfig_symbols,
    validate_kconfig_assignments,
    autosolve_kconfig,
    diff_kconfig_configurations,
    
    # Maintainers & CREDITS
    get_maintainers_overview,
    get_maintainer_section_detail,
    get_person_profile,
    get_credits_overview,
    get_developers,
    match_patch_maintainers,
    
    # Git & Blame
    get_blame,
    get_file_blame,
    get_commits,
    get_version_commits,
    get_commit_detail,
    get_commit_timeline,
    generate_formatted_patch,
    
    # Visual Modeling & Diffs
    get_struct_layout,
    get_function_callgraph,
    get_versions_diff,
    get_kconfig_diff,
)
```

---

## 7. Configuration & Execution

### 7.1. Configuration Precedence & Resolution
All database parameters, webapp server bindings, and parser options are resolved through [`core.config`](core/config.py) following strict precedence:
$$\text{CLI Flags} > \text{Environment Variables} > \text{config.json} > \text{Built-in Defaults}$$

Environment variables synchronized via `core.config.sync_environ()` include:
- `DB_HOST` / `MYSQL_HOST` (default `"127.0.0.1"`)
- `DB_PORT` / `MYSQL_TCP_PORT` (default `3306`)
- `DB_USER` / `MYSQL_USER` (default `"root"`)
- `DB_PASS` / `MYSQL_PWD` (default `""`)
- `DB_NAME` / `MYSQL_DATABASE` (default `"main"`)
- `WEBAPP_HOST` (default `"0.0.0.0"`)
- `WEBAPP_PORT` (default `8000`)
- `WEBAPP_RELOAD` (default `true`)

The Git repository served by the webapp (`webapp/backend/config.py:LINUX_REPO_DIR`) is the default entry of the `"repositories"` config list (`core.config.get_default_repo_path()`), auto-detected under `workspace.repos_dir` (default `repos/`).

### 7.2. Starting the Server
Start the application from the repository root:

```bash
# Using webapp entrypoint (with CLI overrides)
python3 webapp/main.py --host 0.0.0.0 --port 8000

# Or via uvicorn directly
uvicorn webapp.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in any modern browser.

---

## 8. Error Handling, Status Codes & Resilience

| HTTP Status Code | Scenario | Response Payload Format |
| :--- | :--- | :--- |
| `200 OK` | Successful execution | `{ ... }` or `[ ... ]` JSON response |
| `400 Bad Request` | Directory traversal attempt (`..`), invalid parameter syntax, missing required fields | `{"detail": "Path traversal not permitted"}` |
| `403 Forbidden` | Access to files outside the repository jail (`LINUX_REPO_DIR`) | `{"detail": "Access to path outside repository jail is forbidden"}` |
| `404 Not Found` | Unknown kernel version, non-existent file/symbol/subsystem/commit | `{"detail": "File 'foo.c' not found for version 'v3.0'"}` |
| `429 Too Many Requests` | Sliding-window rate limit exceeded (>600 requests / 60 seconds per IP) | `{"detail": "Rate limit exceeded. Try again in X seconds."}` |
| `500 Internal Error` | Database connection pool exhaustion or unhandled server exception | `{"detail": "Internal database error"}` |

### 8.1. Client-Side Offline Storage & Fallbacks (`IndexedDB`)
The frontend client automatically falls back to IndexedDB (`KernelInfoDB` v1) whenever network operations fail or the server is temporarily offline:
- **`files` Store**: Cached file contents, AST tokens, directory trees, blame records.
- **`symbols` Store**: Cached symbol search results, definition records, XRef usage graphs.
- **`kconfigs` Store**: Architecture trees, defconfig mappings, dependency graphs.
- **`subsystems` Store**: Maintainer sections, developer profiles, CREDITS catalog.
- **`ui_state` Store**: Active layout tabs, open files, split-screen arrangements, editor cursor offsets.
