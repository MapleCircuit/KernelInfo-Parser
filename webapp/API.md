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
│   ├── models.py                   # Pydantic request schema of the patch formatter
│   ├── database/
│   │   ├── pool.py                 # Thread-safe database connection pool (no per-cursor reset) with backoff
│   │   └── helpers.py              # Encoding safeguards, version metadata lookup
│   ├── security/
│   │   ├── jail.py                 # Canonical filesystem sandboxing & jail validation
│   │   ├── git.py                  # Git revision & object-spec validation (argument injection)
│   │   ├── sql.py                  # SQL LIKE wildcard sanitizer & identifier validation
│   │   └── middleware.py           # Security headers (CSP), sliding-window rate limiter, repository selection (pure ASGI)
│   ├── services/
│   │   ├── git_reader.py           # Persistent Git object readers (git cat-file --batch), up to 4 per repository
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
│   │   ├── diff_service.py         # Cross-version semantic diffs & Kconfig diffing
│   │   └── mailing_list_service.py # LKML public-inbox archive, threads, patches & LRU caches
│   └── routers/
│       ├── versions.py             # Kernel versions catalog
│       ├── filesystem.py           # Directory trees, source files, include resolution
│       ├── symbols.py              # Symbol definitions, lookups, XRefs, AST inspector
│       ├── kconfig.py              # Kconfig trees, symbol relations, defconfigs
│       ├── maintainers.py          # Subsystems, maintainers, CREDITS, patch reviewer
│       ├── commits.py              # Git commit log, detail, and blame annotations
│       ├── mail.py                 # Mailing list threads, messages, patch diffs, and search
│       ├── docs.py                 # Documentation labels & kernel-doc
│       └── tools.py                # Pahole layout, callgraph, semantic version diff
├── frontend/
│   ├── index.html                  # Single-page application shell (served with its build's asset URLs)
│   ├── boot.js                     # Loader of the build's modules, recovers from a stale cached shell
│   ├── manifest.json               # Progressive Web App (PWA) manifest
│   ├── sw.js                       # Service Worker: cached shell, per-build assets, API offline fallback
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
├── main.py                         # Server entrypoint (`app`, CLI: -c, --host, --port, --reload)
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

### 2.1.1. Git Revision & Object-Spec Validation (`webapp/backend/security/git.py`)
Every endpoint names a revision (a version name, a branch or a commit SHA) and the services pass it
to `git` as a **positional argument** (`git blame <rev> -- <path>`). Git reads an argument starting
with `-` as an option, so an unvalidated revision becomes one: `--contents=<file>` makes `git blame`
print any file, and `--output=<file>` makes `git log` write one. A revision is therefore validated
before it can reach a command line:
- `safe_rev(rev) -> str`: returns the revision, or raises `HTTP 400 Bad Request`. Accepts only
  `core.globalstuff.is_safe_git_rev()`: a name of at most 255 characters starting with `[A-Za-z0-9]`
  and otherwise limited to `[A-Za-z0-9._/+-]`, which covers tags (`v2.6.32-rc1`), branches
  (`origin/master`, `feature/x`) and full or short SHAs, and excludes options, whitespace, newlines
  and every character git gives a meaning in a revision expression (`^`, `~`, `:`, `{`, `}`).
- `is_safe_rev(rev) -> bool`: the non-raising form, for services that fall back instead of failing
  (`TreeResolverService.resolve_commit_sha()` returns `None`, so callers answer 404 as before).
- `<rev>:<path>` specs for `git cat-file --batch` are checked with `core.globalstuff.is_safe_git_spec()`.
  The stream reads one request per line: a newline or NUL inside a spec injects further requests and
  desynchronizes every later answer on that long-lived stream, so such specs are never written to it:
  `GitReader` (`git_reader.py`) answers `missing`, and `history_service.batch_blob_shas()` — which
  matches answers to specs by position — leaves the offending spec out and reports `None` for it.

Validation points (every revision a service passes to git goes through one of them):
`GitService.get_blame()` / `get_file_history()` (`safe_rev`), `_cached_git_file_history()`
(`is_safe_rev`), `KconfigService._rev()` (the single funnel for that service's `git grep` /
`ls-tree` calls), `GitTreeResolver.resolve_commit_sha()` (Path C, and therefore the filesystem,
diff, docs and history services), `HistoryService.get_change_commits()`, and
`database/helpers.get_version_info()`, whose `git rev-parse` fallback is skipped for a name that
would be read as an option (the unresolved name is still returned, so callers answer 404).
`GitService.get_commit_diff()` keeps its stricter 4–40 hex check (`_HEX_REF`).

### 2.1.2. Identifier Validation for Git Pattern Searches (`webapp/backend/security/sql.py`)
A caller-supplied name also reaches `git grep` as a **pattern**, where metacharacters match far more
than the name itself: `CONFIG_.*` finds every `CONFIG_` reference in the tree (44,485 lines at v3.0,
3.0 MiB, versus 406 for a real symbol), and the whole result would be read, parsed and cached under
a name the caller chose. `KconfigService._safe_symbol()` therefore strips the `CONFIG_` prefix and
rejects anything `is_valid_identifier()` does not accept (`^[A-Za-z0-9_.~+-]+$`) with
`HTTP 400 Bad Request`; it is the gate for `get_symbol_usage()` and `kbuild_usage()`. Patterns built
from an accepted name are still escaped (`re.escape`, as `_other_arch_definitions()` does), the
literal searches pass `-F` so git reads the name as text rather than a basic regex, and a symbol
with more than `_USAGE_REF_LIMIT` (20,000) references is reported `source_refs_truncated: true`
(the KConfig view shows its count as `20000+`) instead of being cached whole. `git grep -E` itself uses a DFA, so a pattern cannot make it
backtrack; the control here is over how much it can match and retain.

### 2.2. SQL Wildcard Sanitization (`webapp/backend/security/sql.py`)
- `escape_like(value: str) -> str`: Escapes the SQL `LIKE` wildcards (`%`, `_`) and the escape character itself, leaving length untouched. For values that must keep their full length, such as a path prefix (`filesystem_service.get_tree()`), where an unescaped `%` turns an anchored prefix match into a full scan.
- `sanitize_like_query(query: str) -> str`: Truncates a search query to 100 characters, then escapes it with `escape_like()`. Used by the symbol, maintainer and commit searches. Commit search matters most: `q=%` previously made all three of its `LIKE` clauses match every row of `m_commit` (95,949 rows of the reference database, against 70 for the escaped, literal `%`).
- `is_valid_identifier(name: str) -> bool`: `^[A-Za-z0-9_.~+-]+$`, the gate described in §2.1.2.

### 2.3. HTTP Security Headers (`webapp/backend/security/middleware.py`)
Every response includes hardened security headers:
- `Content-Security-Policy`: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'`. Scripts and workers are restricted to `'self'`, with `'unsafe-inline'` only for style attributes. `script-src` carries **no** `'unsafe-eval'`: no frontend module evaluates code from a string (no `eval`, no `new Function`), so allowing it would only widen what an injected string could do. A route may set a stricter policy of its own (`GET /api/fs/raw/...` sandboxes raw SVG); the middleware only fills in the default.
- `X-Frame-Options`: `DENY` (prevents clickjacking attacks).
- `X-Content-Type-Options`: `nosniff`.
- `Referrer-Policy`: `strict-origin-when-cross-origin`.

### 2.3.1. CORS (`webapp/backend/app.py`)
The API carries no cookie or header authentication, so any origin may read it (`allow_origins=["*"]`,
methods `GET`, `POST`, `OPTIONS`) but **never with credentials** (`allow_credentials=False`). The two
combined are what Starlette turns into a reflected `Access-Control-Allow-Origin` plus
`Access-Control-Allow-Credentials: true`, which would let any page in a visitor's browser read this
API as that visitor; with credentials off, the literal `*` is returned instead. Because the frontend
is served from the same origin, it still reads `X-KIP-Sync` and every other response header.

### 2.4. Sliding-Window Rate Limiting
- `SlidingWindowRateLimiter`: Enforces an in-memory sliding window cap (default 600 requests / 60 seconds per client IP) on `/api/` requests; the shell, `/boot.js` and the build's assets are not limited, whatever their extension, while an API path ending in `.js` or `.json` is. Exceeding requests receive `HTTP 429 Too Many Requests` with a `Retry-After` header. Behind a reverse proxy every client shares the proxy's address.
- The security headers, the rate limiter, `RepoContextMiddleware` and `SyncHeaderMiddleware` are pure ASGI middleware (`BaseHTTPMiddleware` would run each request through an extra task and memory stream).

### 2.5. Strict Read-Only Database Invariant
The `webapp/` backend operates exclusively as a read-only presentation and query tier. Under no circumstances may any router, service, middleware, or background task in `webapp/` execute database schema definitions (DDL: `CREATE`, `ALTER`, `DROP`, `TRUNCATE`) or data modification commands (DML: `INSERT`, `UPDATE`, `DELETE`, `REPLACE`).
- All table creations, schema migrations, and initial seed rows (including `m_db_instance`) are the strict and exclusive responsibility of the parser engine (`core/DBLayout.py`, `main.py`).
- Database cursors acquired via `get_db_cursor()` in `webapp/backend/database/pool.py` do not support or execute transaction commits.
- Connection Pool: When `ssh_tunnel` is inactive and a local MariaDB instance is configured, `get_db_pool()` connects via Unix domain socket (`/run/mysqld/mysqld.sock`), automatically falling back to TCP (`127.0.0.1:3306`) if the socket is inaccessible. When `ssh_tunnel` is enabled in configuration (`database.ssh_tunnel.enabled: true`), `get_db_pool()` and FastAPI application startup (`webapp/backend/app.py:lifespan`) automatically initialize and verify the SSH tunnel forward (`core/ssh_tunnel.py`), reroute pool connection parameters to the forwarded port (`127.0.0.1:<local_port>`), and gracefully tear down the tunnel and connection pool on server shutdown.
- Resilient Connection Management: `get_db_pool()` creates the pool (and the SSH tunnel before it) once, under a lock, whatever the threads asking, and checks it with one connection. The pool (`ConnectionPool`, up to `POOL_SIZE` = 32 connections, opened on demand) replaces mysql-connector's `MySQLConnectionPool`, which pinged every connection it lent (under a process-wide lock) and reset its session when given back, on top of the ping `cursor()` itself makes: three round trips per cursor (3–6 ms over the SSH tunnel, now ~0.7 ms). The webapp never changes session state (read only, autocommit, no session variables, temporary tables or locks), so a connection goes back as it is. `get_pooled_connection()` waits up to `POOL_WAIT_S` (10 s) for a connection of a saturated pool, since connections come back as other requests finish (the threadpool has 40 workers). `get_db_cursor()` reconnects a connection that `cursor()` finds dropped while idle (server restart, tunnel drop), and closes instead of reusing one that failed during the request or kept a result it could not drain. If connecting fails, the pool resets (at most 3 times) and automatically re-verifies the SSH tunnel.
- If expected metadata tables or rows are absent, webapp endpoints must return appropriate HTTP error statuses or graceful process-level fallbacks without modifying the underlying database.

### 2.6. Frontend & Service Architectural Invariants
1. **Directory Path Filtering & Polymorphic Navigation**: In database queries on `m_file_blob`, directory records have `ftype == 0` while regular files have `ftype != 0`. All file search, path lookup, and `#include` header resolution queries explicitly filter `ftype != 0` so directories sharing names or paths with header files are never returned as file matches. File viewer endpoints (`get_file`) and frontend code views (`virtual_editor.js`) support polymorphic path navigation: opening directory paths returns child hierarchies (`entries`, `tree`) and renders interactive navigation tables/grids rather than returning 404 errors.
2. **Lexical Comment Prioritization & Directive Matching**: In dual-layer syntax and AST overlays (`webapp/frontend/js/views/code_view/ast_overlay.js`), lexical tokenizers prioritize matching block comments (`/* ... */`) and line comments (`// ...`) *before* general identifier regexes, rendering them as non-clickable `<span class="tok-comment">` elements to prevent comment tokens from leaking into `.ast-token` or triggering symbol context menus. Preprocessor directive matching on snippet-relative coordinate slices allows optional leading hashes (e.g. `^(\s*#?\s*define\s+)`) so directive keywords are styled as preprocessors (`tok-macro`) and never tagged as clickable symbol identifiers.
3. **Flat Coordinate Projection & Defensive Navigation**: Symbol detail backend endpoints (`webapp/backend/services/symbol_service.py:get_symbol_detail`) expose definition coordinate fields as flat scalars (`line: row["line_s"]`, `line_s: row["line_s"]`) at the root of the response payload alongside the nested `definition` record. Frontend jump-to-definition callers (`webapp/frontend/js/views/code_view/context_menu.js`) resolve coordinates defensively across `detail.definition?.line_s || detail.line_s || detail.line || 1` to ensure virtual editors navigate to and highlight the exact symbol definition line.
4. **Maintainer Query Sanitization & Parameter Preference**: Developer identity and maintainer endpoints (`webapp/backend/routers/maintainers.py`, `webapp/backend/services/maintainer_service.py`) support query parameter lookups (`/api/maintainers/{version}/person?email=...`) alongside URL path parameters. Frontend API clients sanitize patch trailers and email strings (stripping `<>`, quotes, and whitespace) and prefer query parameters when identifiers contain special characters (`@`, `<`, `>`, `/`) to prevent URL decoding and route matching failures.
5. **Context Menu Interaction Preservation**: Right-click context menus (`contextmenu`) across webapp workspaces (virtual code editor, file explorer sidebar, nodemap canvas) intercept default browser menus and offer context-adaptive actions (definitions, references, git blame popovers, command palette search, coordinate copying), while strictly preserving primary left-click interactions (inline AST token inspector popups, file navigation, node selection) without collision, event race conditions, or lingering ghost menus.
6. **Structured Cross-Reference Classification**: Symbol cross-reference backend services (`get_symbol_xref`) and frontend inspectors (`showXrefModal`) return and render both primary and multi-declaration definition locations (`definitions: [...]` with file path, line bounds, and AST declaration type) alongside usage references (`references: [...]` with caller function, file, line, and typed badges `Decl`, `TypeUsage`, `Call`, `MemberRef`, `DeclRef`, `MacroExp`; one list, grouped by role), providing direct 1-click jump navigation for both definitions and usages. Every location is a location of the requested revision: definitions come only from blobs of its Git tree, and the ASTs whose references are looked up are only those the symbol's own name identifies, never the shared AST of a marker macro such as `EXPORT_SYMBOL` (see §4.3).
7. **Escaping of Interpolated HTML**: every value interpolated into an `innerHTML` / `insertAdjacentHTML` template passes through `escapeHtml()` (exported by `webapp/frontend/js/components/modal.js`), including error text (`e.message`), repository data (git author names, file paths) and state values (the KConfig `arch`). Only values that are *already* rendered HTML are interpolated raw, and those are built from escaped parts (`doc_page.js` navigation links, the `virtual_editor.js` build-condition `why` / `ruleHtml` fragments). The CSP (§2.3) forbids inline scripts, so this is the layer that keeps repository content from entering the DOM as markup.

### 2.7. Tri-Path Tree Resolution & Arch-Aware Definition Ranking (`webapp/backend/services/tree_resolver.py`)
1. **Database Tree Resolution (`TreeResolverService`, module singleton `tree_resolver`)**:
   - **Path B (Full-Tree Resolution, `<15ms`, LRU Cached)**: Assembles the entire active `{fnid: blob_id}` repository tree at any `commit_id` by loading baseline snapshot records from `m_bridge_checkpoint` and chronologically applying forward `m_commit_file` deltas along the first-parent DAG. Results are cached in an in-memory `OrderedDict` LRU cache.
   - **Limitation**: Path B only reaches a checkpoint when every first-parent ancestor is ingested in `m_commit`/`m_commit_parent`. The parser ingests a bounded commit window, so many intermediate commits have broken chains; Path B is therefore used only as a fallback when the Git repository is unavailable.
2. **Git-Native Tree Resolution (`GitTreeResolver`, module singleton `git_tree_resolver`)**:
   - **Path C (Full-Tree Listing, `~50ms` cold, LRU Cached per commit SHA)**: `list_tree(rev)` resolves any revision (tag, branch, full/short SHA, even commits never ingested into `m_commit`) via `git rev-parse --verify <rev>^{commit}` and returns `{path: blob_sha}` from `git ls-tree -r -z --full-tree`. Submodule entries are skipped. This is the authoritative file list for every revision.
   - `list_dir(rev, directory)` returns `(subdirectory names, {path: blob_sha} of the direct files)` from a per-commit directory index built in one pass over `list_tree` (`DERIVED_CACHE_SIZE` = 4 commits), so expanding a directory no longer scans the whole tree. `search_index(rev)` is the per-commit list of `(lowercased path, lowercased basename, path)` that the file palette scans (`GET /api/fs/search`).
   - `resolve_commit_sha(rev)` answers are cached (4096 entries per repository): a full SHA that names a commit permanently, any other name for `REV_SHA_TTL` (15 s), and all names are dropped when the `git` sync token changes (`clear_revision_caches`). Every tree, blob-map and build-layout lookup resolves its revision first, so without the cache one include resolution spawned three `git rev-parse` processes.
   - `map_blob_hashes(cursor, blob_shas)` joins the Git blob SHAs to `m_file_blob.blob_hash` (`blob_hash` == Git blob SHA-1) and returns `{blob_sha: (blob_id, ftype)}`, preferring typed (`ftype != 0`) rows. Blobs absent from the map were never parsed (unindexed).
   - `blob_paths(cursor, rev)` returns `{blob_id: [paths]}` for the indexed blobs of the revision's tree (sorted paths; one blob may sit at several paths). It is cached per commit SHA (8 entries): for `BLOB_PATHS_TTL` = 120 s, so blobs indexed by a running parse appear without a restart, except for the head commit of a ref whose ingestion finished, whose blobs are all indexed and whose map is kept until the parse state changes (`clear_revision_caches`). Symbol services use it to scope `m_symbol_def` (content-addressed by `blob_id`) to any revision; see §4 *Revision scoping of symbol queries*.
3. **Arch-Aware Definition Ranking (`definition_rank`, `best_path`, `definition_arch`)**:
   A symbol defined once per architecture (`struct pt_regs`, `setup_arch`, `thread_struct`...) resolves to the definition of one ARCH value: the `arch` parameter of the symbol endpoints, which the frontend sets to the KConfig engine's ARCH while the engine is on with a resolved configuration, and to `x86_64` otherwise (§5.19). `definition_arch(version, arch)` resolves the ARCH value through `KconfigService.resolve_arch` (unknown values fall back to `x86_64`) and returns it with its `ArchScope` and word size (`arch_bits`: `x86_64` → 64, `i386` → 32, generic `x86` → none). The scope is the ARCH's build layout at the revision (`webapp/backend/services/arch_layout_service.py`): the `m_arch_root` rows the parser stored for the version's head commit, or, for revisions without rows (branches, commits never ingested), the same evaluation of the revision's Makefiles from Git (`parser/arch_layout.py`, `parser/kconfig_ast/KCONFIG_API.md` §6.5), cached per (commit, ARCH); without a layout, the SRCARCH's `own_arch_dirs` are assumed. Definitions are ordered by:
   - prototypes (`SymbolLinkage.PrototypeDecl`) after every definition with a body;
   - path group (`path_arch_group`): the scope's own files (its architecture trees, `ArchLayout.arch_trees()`, and the asm-generic headers its generated wrappers include), then generic code, then the other `include/asm-generic/` and `include/uapi/asm-generic/` headers, then other architectures. An architecture tree is `arch/<a>/` or, in 2.6.x, `include/asm-<a>/` (`path_arch`): `atomic_t` at v2.6.12 resolves to `include/asm-x86_64/atomic.h` for x86_64 (the build links `include/asm` to it), `include/asm-i386/atomic.h` for i386, and ARCH=um's layout also owns `x86_64` through its `include/asm-um/arch` link; m68knommu at v2.6.30 owns `arch/m68k/` (hdr-arch). The same layout trees decide `other_arch` of the build-condition and Kconfig endpoints (`KconfigService._arch_trees`, `is_other_arch_path` in `parser/kbuild_conditions.py`);
   - word size: files of the other word size (`process_32.c` on x86_64, `page_64.h` on i386) after the others;
   - weak/alias definitions (`WeakOrAliasDef`) after strong ones, then path and line.
   A blob present at several paths is reported at its best-ranked path (`best_path`). Choosing between `#ifdef` branches of one file and between files Kbuild does not compile is done by the frontend against the actual configuration (§5.19).
4. **Definitions Reached From the Use Site (`from_file`)**: when the request names the file the symbol is used in, `SymbolService._prefer_reachable()` puts first the definitions the compilation of that file reads for the ARCH, i.e. what the compiler sees. `arch_layout_service.reachable(rev, arch, file)` walks the file's `#include` chain with the ARCH's build layout (`parser/include_graph.py`, `parser/kconfig_ast/KCONFIG_API.md` §6.6): forced `-include` files first, quoted names in the including file's directory, then the `-I` directories in order, through the build's symlinks and generated asm-generic wrappers. `#include` lines count whatever `#if` block holds them (the frontend still drops definitions its configuration compiles out), and directives are read from Git once per blob (cached by blob SHA, 50,000 entries; each walk cached per (commit, ARCH, file), 32 entries; a cold walk of `include/linux/sched.h` at v2.6.12 takes ~0.3 s). Reached definitions go before the path groups of item 3, prototypes stay last and weak definitions after strong ones; definitions are marked `visible`. `atomic_t` used in `include/linux/sched.h` at v2.6.12 thus resolves through `include/asm-x86_64/semaphore.h` to `include/asm-x86_64/atomic.h` for x86_64 (through `include/linux/sem.h` → `include/asm-um/atomic.h` → `include/asm-um/arch/atomic.h` = `include/asm-x86_64/atomic.h` for ARCH=um), and never to the unrelated `net/rxrpc/internal.h`. The walk reads the files themselves rather than `m_ast_include`/`m_file_reference`: those rows come from the parser's own clang run, which links `include/asm` to `asm-generic` and skips the includes of inactive `#if` branches, so they describe no particular ARCH.

### 2.8. 3-Level Semantic Cross-Version Diff Engine (`webapp/backend/services/diff_service.py`)
Either side of a comparison may be a release tag, a branch, or an arbitrary commit SHA. Comparison is evaluated in three progressive tiers to optimize latency:
1. **Level 1 (O(1) Content Blob Filter)**: Resolves `{path: blob_sha}` for both revisions via `GitTreeResolver` (Path C), falling back to `{path: blob_id}` from `m_bridge_checkpoint` / `TreeResolverService` only when Git is unavailable on both sides. Unchanged files (equal content keys) are short-circuited in $O(1)$. **Renames** pair a deleted with an added path (`DiffService._pair_renames`): with Git trees, `git diff-tree -r -z --name-status --diff-filter=R -M50% -l1000` reports each pair with its content similarity (`R087` → 87); then a deleted path whose exact content reappears under a still unpaired added path is paired at 100 (this is the only rule without Git). Git compares contents only while `added` and `deleted` (after exact and same-basename pairs) each stay within the limit and their product within its square; beyond that it pairs only identical files and files that kept their basename and asks for a higher limit, which the response reports as `renames.exhaustive: false` / `renames.limit_needed`, shown in the Compare view's rename note (v3.0→v3.1: every pair compared, 391 renames of which 161 identical, ~0.5 s; v3.0→v4.0: 2,967 renames without the exhaustive pass, `limit_needed` 19,771). The exhaustive pass for such ranges is the separate rename search (`GET /api/tools/diff/renames`), run on request and scoped to a directory.
2. **Level 2 (AST & Symbol Structural Diff)**: For the first 50 modified source files (`.c`, `.h`, `.S`, `.rs`; paths sort documentation and dotfiles first) whose blobs are indexed on both sides, compares symbol definition records in `m_symbol_def` by `(name, type_id)` (one query for all of them) to surface added and removed definitions.
3. **Level 3 (Kconfig Scoped Diff)**: Lists added/removed/modified Kconfig source files (`Kconfig`, `Kconfig.*`) from the Git trees, then parses only the files whose blob differs (from Git, cached per blob SHA) and compares each symbol's definitions there: properties (type, prompt, defaults, `depends on` including the dependencies inherited from enclosing `if`/`menu`/`choice` blocks, selects, implies, ranges, choice, options, `transitional`, help) per defining file. A definition that only moved lines is unchanged. A symbol is added/removed only when no Kconfig file of the other revision defines it (files with the same blob on both sides are checked with one `git grep`). Without Git trees (revisions Git cannot resolve), the indexed versions' Kconfig tags are compared instead (name -> set of definition text hashes, `DiffService._fetch_kconfig_symbols`).

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
2. **Git access**: Every service resolves the work tree through `get_repo_dir()`; `git_reader` is a `GitReaderPool` with up to 4 persistent `git cat-file --batch` processes per repository, and `GitTreeResolver.repo_dir` follows the request unless pinned.
3. **Database scoping**: `get_active_repo_id(cursor)` maps the repository to `m_repository.repo_id` (by name, then `git_url`; `-1` when never parsed). Version resolution (`get_version_info`) and the version/branch catalogs only consider that repository's `m_git_ref` rows. Commit ids, blobs and tags are global and need no scoping.
4. **Caches**: Module caches keyed by version names (`_INCLUDE_CACHE`, `_TREEMAP_CACHE`, `_MAINTAINER_CACHE`, `_FILE_SUBSYSTEMS_CACHE`, `_CREDITS_KEYS_CACHE`, the Kconfig `_MODELS` / `_LISTING_CACHE` / `_PATHS_CACHE` / `_USAGE_CACHE` / `_BUILD_RESOLVERS`, version name/id caches) or by content (`_FILE_TOKENS_CACHE` per blob, `_BLOB_SECTIONS_CACHE` / `_BLOB_CREDITS_CACHE` per MAINTAINERS / CREDITS blob, `_ARCH_INFO_CACHE` per commit and ARCH) are `RepoScopedCache` instances that transparently prefix keys with the repository name; the git file-history LRU cache takes the repository path as an argument. A cache keyed by a version name would keep answering for the commit a branch pointed at, so all of them are cleared when the `parsed` **or** the `git` sync token changes (§2.11).
   A `RepoScopedCache` takes an optional `maxsize` and then evicts the least recently used entry. Every cache whose key carries a request parameter is bounded, because otherwise each distinct value a caller sends adds an entry that is never released: `_INCLUDE_CACHE` (4096; keyed by header and referring file), `_FILE_TOKENS_CACHE` (512), `_TREEMAP_CACHE` (64; each entry is a whole subtree), `_FILE_SUBSYSTEMS_CACHE` (4096; keyed by file path), `_MAINTAINER_CACHE` (16; one MAINTAINERS model per version, and any commit SHA is a version), `_CREDITS_KEYS_CACHE` (16), the version name/id maps (`_NAME_TO_VID_CACHE`, `_VID_TO_NAME_CACHE`, 50,000 each; a separate `_PRELOADED` flag records that the repository's refs were loaded), `_USAGE_CACHE` (256; keyed by Kconfig symbol), `_LISTING_CACHE` (64; keyed by revision), `_ARCH_INFO_CACHE` (256) and `_PATHS_CACHE` (4, separate because one entry is a revision's whole file list: 36,783 paths / ~4.5 MiB at v3.0, more in later releases). A failed or timed-out `git` call of the Kconfig service is never cached as an empty answer. Caches keyed only by what the repository itself defines stay unbounded, and the callers that evict by their own policy (`_MODELS`, `_BUILD_RESOLVERS`) reach through `dict.pop` and are unaffected. Because a bounded cache can evict between a membership test and a read, its callers read once into a local (`.get()`) rather than testing and then indexing.
5. **Unparsed repositories** remain browsable: tags absent from `m_git_ref` resolve through `git rev-parse` to `vid = 0`, and trees, files, searches, diffs and treemaps read Git directly.

### 2.11. Client Synchronization & Build-Bound Assets (`webapp/backend/services/sync_service.py`, `webapp/frontend/js/sync.js`)
Clients must follow server changes without the user knowing to reload. The server keeps four change tokens (12-hex digests, cached so most requests cost no database or Git work):
| Token | Covers | Computed from (cache) | Client reaction |
|---|---|---|---|
| `db` | Database rebuilt | `m_db_instance` (30 s) | Every cached API response is dropped |
| `frontend` | Webapp code changed | SHA-256 of the files under `webapp/frontend/` (path + content), recomputed only when a file's size, mtime, ctime or inode changed (checked every 5 s); identical files give the same build across restarts | The page reloads into the new build (see below) |
| `parsed` | Versions of the active repository finished, re-parsed or rolled back | `m_git_ref` + `m_ingest_state` of the repository, unfinished versions excluded (15 s) | Version/branch lists are refetched; cached data of versions whose stamp changed is dropped. On the server, a change also clears the per-version caches (`clear_parse_dependent_caches()`: version-name maps, file tokens, include resolution, maintainers, history facts, blob-path maps) |
| `git` | Tags fetched, branches moved | `git for-each-ref` of the repository (15 s) | Same as `parsed`, server side included: caches keyed by a revision name (a branch's file tokens, Kconfig model, maintainers, name → commit) are cleared, and the revision → SHA and blob-map caches of `GitTreeResolver` (`clear_revision_caches`; tree listings are content-addressed and kept) |
- **Delivery**: `GET /api/sync` returns all four. `SyncHeaderMiddleware` (inside `RepoContextMiddleware`, so tokens are those of the request's repository) adds `X-KIP-Sync: <db>.<parsed>.<git>` to every `/api/` response, so any request reveals a change without polling. While the three tokens are cached the header is read in place (`cached_header_value`); only a refresh goes to the threadpool, and one refresh per token runs at a time (`_Ttl`: requests arriving meanwhile wait for its answer instead of each running `git for-each-ref` or the database query).
- **When clients check**: `syncManager.start()` runs once when the page opens (replacing the former `api.checkDbSync()`): it compares `db`, `parsed` and `git` with the tokens seen last (`meta/sync_tokens`), then follows the `X-KIP-Sync` headers. The `frontend` token is only compared when the page opens **and** the last frontend check (`localStorage["kip-last-frontend-check"]`) is more than an hour old; there is no polling.
- **Manual check** (`syncManager.checkNow()`, Settings popover **🔄 Check for Updates**, §5.5): requests `GET /api/sync?fresh=true`, so the server recomputes all four tokens instead of serving its cached ones, applies them like a header change, always refetches the version/branch stamps (`apply(…, {force: true})`), and compares the frontend build regardless of the hourly delay (a newer build shows the reload banner). Resolves to `{db, parsed, git, frontend, changed, removed, added, newBuild}`; the popover reports *Up to date* or what changed in a toast. It compares against the server's local Git repository; it does not `git fetch` from a remote.
- **Version stamps**: `/api/versions` and `/api/branches` give each version a `stamp` (`<commit sha[:12]>.<parse completion time>`, `.0` for versions parsed before `m_ingest_state`, `.git` when not parsed; a branch uses its current Git head). `api._vk(version)` puts `"<name>@<stamp>~"` into every version-scoped cache key, so a re-parsed version or a moved branch never serves older cached data; `syncManager.refreshVersions()` deletes the entries of versions whose stamp changed (`localDB.deleteDataWhere`, `isStaleKey`) and emits `versions:updated` (`{changed, removed, added, newlyIndexed}`); `app.js` then rebuilds the selectors, toasts newly indexed versions and re-renders open views of changed versions. A change of cache-key layout (`CACHE_LAYOUT`) clears the data stores once.
- **Build-bound assets**: `GET /` renders `index.html` with `__KIP_BUILD__` replaced by the current build (`<meta name="kip-build">`) and every `"/static/` URL rewritten to `"/b/<build>/` (`render_index()`); relative ES-module imports inherit the prefix. `GET /b/{build}/{path}` serves the current build's files with `Cache-Control: public, max-age=31536000, immutable` and answers **409** for any other build, so a page never mixes modules of two builds. The shell and `/boot.js` are sent with `Cache-Control: private, max-age=3600`, `sw.js` with `no-cache`. Legacy `/static/`, `/js/`, `/css/` mounts stay for direct access.
- **`boot.js`** (classic external script, the CSP forbids inline ones): sets `window.KIP_BUILD` and imports the build's `app.js` (`data-entry`). If the import fails (409: the cached shell is older than the server's build and its modules are not cached), it deletes the `kip-shell`/`kip-assets` caches and reloads once (`sessionStorage["kip-stale-build-reload"]` prevents a loop; a second failure shows an error box). `window.KIP_RELOAD_TO_LATEST()` does the same on request. `app.js` starts on `DOMContentLoaded`, or immediately when the import resolves after it.
- **Service worker** (`sw.js`): `kip-shell` holds `/` (every shell path and query share one entry) and `/boot.js`, cache-first; `kip-assets` holds `/b/<build>/…`, cache-first (the page posts `{type: "prune", build}` on start to drop other builds); `kip-api` holds API responses, network-first with cache fallback (`/api/sync` is never cached, so it always reaches the server), bounded to `API_CACHE_LIMIT` (300) entries, oldest dropped first. A reply the worker serves from `kip-api` after the request failed, and its synthesized `503`, carry `X-KIP-Offline: 1`, so the page does not mistake a cached reply for a live one (§5.1 *Server reachability*). Caches not named `kip-*` (e.g. the former `kernel-ide-v10`) are deleted on activation; there is no hand-maintained pre-cache list.
- **New build announcement** (`syncManager.announceUpdate()`): a bottom banner *"KernelInfo was updated (build …). Reloading in 5 s…"* with **Reload now** and **Later**; the countdown waits while a dialog (`.ki-modal-backdrop`) is open. Reloading drops the cached shell first, so the next page is the new build; tabs and KConfig profiles survive (saved in `localStorage`). **Later** dismisses it until the next hourly check.

### 2.12. Startup Catalog Warm-Up (`webapp/backend/app.py:lifespan`, `webapp/backend/routers/versions.py`)
A restarted server has empty in-memory caches, and a client after **Full Local Reset** (§5.5) has an empty IndexedDB, so the first page load would otherwise pay for every catalog itself — above all the branch-tags walk (`GET /api/branch/tags`, ~18 s for `master` of Linux).
- **What runs**: after the database pool is pre-warmed, `lifespan` starts `start_prewarm()`, a daemon thread (`kip-prewarm`) that does not delay startup. For every configured repository whose work tree exists it selects that repository (`set_active_repository`), reads `get_versions(include_rc=True)` and `get_branches()` (warming the database pool, Git packs and the OS page cache) and walks the tags of the branch marked `is_default`, filling `_BRANCH_TAGS_CACHE`. It logs `Pre-warmed <repo> in <s> s (default branch <name>: <n> tags)` at INFO; a failure is logged as a warning and never stops the server.
- **Shared walks**: `branch_tags(head)` keeps one in-flight walk per `(repository, head)` (`_BRANCH_TAGS_INFLIGHT`), so a request arriving while the warm-up still walks `master` waits for that walk instead of starting a second `git for-each-ref --merged` (it is answered when the warm-up finishes, not 18 s after it arrived). The walk uses the resolved head, so a branch moving during it cannot mix two positions. A failed or timed-out walk (180 s) is not cached.
- **Shutdown**: `cancel_branch_walks()` terminates walks still running, so a `--reload` restart does not leave Git processes behind.
- Only the default branch is warmed; another branch is walked on its first request and then cached as before.

## 3. Database Schema & Underlying Tables

| Domain Area | Key Database Tables | Primary Role |
| :--- | :--- | :--- |
| **Versions / Git Refs** | `m_git_ref`, `m_commit` | Version and reference identifiers (`ref_id`, `commit_id`, `ref_name`), chronological author dates, commit SHAs |
| **Filesystem & Blobs** | `m_file_blob`, `m_file_name`, `m_bridge_checkpoint`, `m_file_reference` | File instances, content blobs, paths, file types (0=Dir, 1=C, 2=Kconfig, 3=Rust, 4=Asm, 5=Maintainers, 6=Credits, 7=Raw), and compiler-verified `#include` / Kbuild provenance |
| **AST & Spatial Maps** | `m_ast`, `m_ast_container`, `m_ast_include`, `m_bridge_tag`, `m_map_ast`, `m_tag` | Relational AST nodes, preprocessor `#include` links, container hierarchies, and coordinate regions (`line_s`, `char_s`, `line_e`, `char_e`); `m_map_ast.map_id` is the tag id, so the file token maps join `m_map_ast.map_id = m_bridge_tag.tag_id` (`m_bridge_map` is no longer written) |
| **Symbols & XRefs** | `m_symbol_def`, `m_symbol_ref` (views over `m_tag_def`, `m_tag_ref`, `m_blob_symbol`, `m_symbol_own`, `m_blob_symbol_def`, `m_blob_symbol_ref`; see `core/API.md`) | Definitions and usage cross-references (calls, field access, type usages, macro expansions). `def_id` of a definition stored once per tag is computed (not indexable) and `ref_id` may be NULL, so services select every needed column in the query that finds the rows instead of re-fetching by id. Rows of given target AST ids come from `rows_of_targets_sql()` (`core/SymbolRows.py`) instead of `WHERE ast_id IN (...)` on the views, which takes seconds per target |
| **Kconfig** | `m_ast` (types `Kconfig_*`), `m_tag`, `m_bridge_tag` | Kconfig entries as tags (where each definition is, per blob and version). Menus, dependencies and values come from the semantic model built from Git per revision and ARCH (§4.4); there are no Kconfig tables |
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
List the configured repositories (core config `"repositories"`, auto-detected under `workspace.repos_dir`) joined with `m_repository`/`m_project`. Each entry has `repo` (the name sent as `X-KI-Repo`), `name`, `slug`, `path`, `default_branch`, `git_url`, `repo_id`, `indexed` (parsed into the database), `available` (work tree present), `tag_views` (configured regex tag layouts), and `active`; the envelope adds `active`.
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
      "tag_views": [
        {
          "name": "— any branch —",
          "regex_rules": [
            "^(v2\\.6\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$",
            "^(v(?!2\\.6\\.)\\d+\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$"
          ]
        }
      ],
      "is_active": true
    }
  ]
}
```

#### `GET /api/branches`
Retrieve the active repository's Git branches: finished indexed refs (`m_git_ref.ref_type = 2` for its `repo_id`) followed by local and remote-tracking Git branches (e.g. `stable/linux-6.6.y`) discovered via `git for-each-ref` and filtered through the repository's `branch_filter`, alongside `tag_views` (configured regex tag grouping layouts). Unindexed branches remain immediately browsable in the webapp directly through Git. Each entry carries a `stamp` (§2.11) built from the branch's current Git head, so a branch that moved invalidates its cached data.
- **Query Parameters**: None
- **Response**:
```json
{
  "total": 2,
  "tag_views": [
    {
      "name": "— any branch —",
      "regex_rules": [
        "^(v2\\.6\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$",
        "^(v(?!2\\.6\\.)\\d+\\.\\d+)((?:-(?:rc|pre|alpha|beta)\\d*|\\.\\d+(?:-(?:rc|pre|alpha|beta)\\d*)?).*)$"
      ]
    }
  ],
  "branches": [
    {
      "name": "master",
      "commit_hash": "02f8c6aee8df311261e6da8ba96bd41704aa9623",
      "is_default": true
    },
    {
      "name": "stable/linux-6.6.y",
      "commit_hash": "5dba9ddd98cbc7ad319d687887981a0ea0062c75",
      "is_default": false
    }
  ]
}
```

#### `GET /api/branch/tags`
Tag names reachable from one branch: the releases that belong to its history (`git for-each-ref --merged=<branch> refs/tags`). This is what scopes the header's Tag and RC lists (§5.13) — `v2.6.16.62` was tagged on `stable/linux-2.6.16.y` and is not reachable from `master`, so it is offered for that branch only.
- **Query Parameters**: `branch` (string, required): a branch name (any revision resolves). Validated like every revision (§2.1); `400` when it is not a plain ref, `404` when it does not resolve, `503` when the Git walk failed or timed out (not cached; the client then keeps every tag listed).
- **Cost**: Git walks the whole history of the branch, so the first answer costs ~0.3 s for a stable branch and ~18 s for `master` of Linux (1.5 M commits; ~6 s with a Git commit-graph written in the repository). The answer is cached per `(repository, branch head)` in the server (32 entries, never stale: a branch that moved has another head) and in the client under the branch's stamp (§2.11), so it is computed once per branch position. The default branch of each repository is walked when the server starts (§2.12), and concurrent requests for one head share a single walk. The frontend requests it with a long timeout and off the critical path: the dropdowns render complete and narrow when it arrives.
- **Response**:
```json
{
  "branch": "stable/linux-2.6.16.y",
  "head": "6b0daf99dd2392b024bdca05530e4e761bc3cdae",
  "total": 138,
  "tags": ["v2.6.12", "v2.6.13", "v2.6.16.1", "v2.6.16.62"]
}
```

#### `GET /api/sync`
Change tokens of the server (§2.11): `{"db", "frontend", "parsed", "git", "repo"}`. Never cached by the service worker.
- **Query Parameters**: `fresh` (bool, default `false`): recompute every token instead of serving the server-side cached values (frontend 5 s, db 30 s, parsed/git 15 s). Used by the manual *Check for Updates*.
```json
{ "db": "4cf36a4c8bf6", "frontend": "65c1f2a0583b", "parsed": "af7532653070", "git": "07886de81433", "repo": "linux" }
```
Every `/api/` response also carries `X-KIP-Sync: <db>.<parsed>.<git>`.

---

### 4.2. Filesystem & Code View API

#### `GET /api/fs/tree/{version_name}`
Retrieve the directory entries of a path in the kernel repository.
- **Parameters**:
  - `version_name` (path): Revision: a release tag, a branch, or any full/short commit SHA.
  - `path` (query): Directory path relative to repository root (e.g. `"kernel"`, `""` for root).
- **Behavior**:
  - The listing comes from the Git tree of that revision (complete for every commit); file entries are annotated with `fid` (`blob_id`) and `ftype` from `m_file_blob` by blob hash. Files whose blobs were never parsed have `fid: null` and `ftype: 7` (Raw).
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
      "s_stat": "0",
      "e_stat": "0",
      "s_stat_label": "Added"
    }
  ]
}
```

#### `GET /api/fs/file/{version_name}`
Content of a file at a revision, with the AST token map of its indexed blob and the commit that last changed it.
- **Source Retrieval Engine**: Uses persistent `git cat-file --batch` workers (`GitReader`) to stream exact blob contents directly from Git for the requested revision in sub-millisecond time (~0.1ms small / ~0.2ms big files). Preserves 100% byte fidelity without relying on local working tree checkouts. One stream answers one read at a time, so `git_reader` (`GitReaderPool`) keeps up to `READERS_PER_REPO` (4) of them per repository, spawned only when every existing one is busy: a file view no longer waits behind a history timeline (hundreds of blobs), a Kconfig model build or an include walk.
- **Indexed blob**: found by content: the Git blob SHA of the file is `m_file_blob.blob_hash`, so the tokens always belong to the text served, at a tag, at a branch that moved since it was parsed, or at a commit never ingested. Tokens are cached per `blob_id` (`_FILE_TOKENS_CACHE`), so revisions sharing a file content share them. A file whose blob was never parsed is served with `fid: null`, `ftype: null` and `tokens: []`.
- **Directory Detection**: If the path resolves to a Git `tree`, the endpoint returns the directory listing.
- **Binary / oversized files**: a NUL in the first KiB answers `is_binary: true`, more than 50 MiB `is_oversized: true`, both without `content` and with a `message` the code view shows in place of the file.
- **Parameters**:
  - `version_name` (path): Revision.
  - `path` (query): Target file path (e.g. `"init/main.c"`). Required.
- **Response**:
```json
{
  "type": "file",
  "is_binary": false,
  "version": "v3.0",
  "vid": 1,
  "path": "init/main.c",
  "ftype": 1,
  "file_info": {
    "fid": 684,
    "fname": "init/main.c",
    "ftype": 1,
    "last_changed": { "commit_hash": "…", "author_name": "…", "author_date": 1310000000, "subject": "…" }
  },
  "content": "/* ... raw source code ... */",
  "tokens": [
    [15, 0, 15, 24, 365653, 78],
    [21, 0, 21, 28, 42, 5]
  ]
}
```
`tokens` rows are `[line_s, char_s, line_e, char_e, ast_id, type_id]`. `ftype` at the root is what the code view's syntax scoping reads (`AstOverlay.setFileInfo`, §5.2).

- **Directory Path Response** (when target `path` is a directory):
```json
{
  "type": "dir",
  "is_directory": true,
  "version": "v3.0",
  "vid": 1,
  "path": "init",
  "ftype": 0,
  "file_info": { "fid": null, "fname": "init", "ftype": "dir" },
  "entries": [
    { "name": "main.c", "type": "file", "fid": 36568, "ftype": 1, "path": "init/main.c", "s_stat_label": "Added" }
  ],
  "tree": [ ... ],
  "content": "[Directory: init]",
  "tokens": []
}
```

#### `GET /api/fs/resolve_include`
Resolve a preprocessor `#include <...>` or `#include "..."` directive to an authoritative git repository file path. Guarantees directory exclusion (`ftype != 0`) to prevent directory false-positives.
- **Parameters**:
  - `version` (query): Kernel version string (default `"v3.0"`).
  - `header` (query): Raw or cleaned include path (e.g. `"<linux/init.h>"`, `"linux/init.h"`, `"#include <linux/init.h>"`). Required.
  - `ast_id` (query): Optional AST Node ID of the include directive to check direct `m_ast_include` links.
  - `current_file` (query): Optional referring source file path (e.g. `"arch/x86/kernel/setup.c"`) to prioritize architecture-specific or relative header matches.
  - `arch` (query, optional): ARCH whose build layout resolves the include (`definition_arch`: unknown values fall back to `x86_64`).
- **Jailing**: `header` and `current_file` become candidate repository paths, so both are rejected with `HTTP 400 Bad Request` unless `is_safe_rel_path()` accepts them (a traversing header such as `../../../../etc/passwd` is never resolved).
- **Resolution Strategy**:
  0. The compiler's search for the ARCH's build layout (`arch_layout_service.resolve_include`, §2.7 item 4): `"name"` in `current_file`'s directory first, then the `-I` directories, through build-time symlinks and generated wrappers (`<asm/atomic.h>` → `include/asm-x86_64/atomic.h` at v2.6.12 for x86_64, `arch/x86/include/asm/atomic.h` at v3.0); method `build_layout`, with `arch`. Steps 1-5 only run when it finds nothing (e.g. generated headers, `tools/`).
  1. If `current_file` is supplied, queries compiler-verified provenance from `m_file_reference` (`ref_type=1`, the rows of `current_file`'s blob at that path: `source_blob_id`/`source_fnid` joined to the revision's `m_bridge_checkpoint`).
  2. If `ast_id` is supplied, queries `m_ast_include` join `m_file_name`.
  3. Generates prioritized candidate search paths: relative to `current_file` directory, `header`, `include/{header}`, `include/uapi/{header}`, and `arch/{arch}/include/{header}`.
  4. Matches candidates against active non-directory indexed files (`ftype != 0`) in `m_bridge_checkpoint` / `m_file_blob` / `m_file_name`.
  5. Falls back to the candidates present in the revision's Git tree (method `git_tree`); the work tree on disk is never consulted, since it may hold another revision.
- **Response**:
```json
{
  "header": "linux/init.h",
  "path": "include/linux/init.h",
  "resolved": true,
  "method": "search_path"
}
```

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
- **Ranking**: exact basename → basename prefix → basename substring → path substring → subsequence (fewest gaps first, queries of 2+ characters). Each keystroke scans the revision's search index (`GitTreeResolver.search_index`, lowercased once per commit) and keeps only the best `limit` matches (`heapq.nsmallest`); `total` counts every match.
- **Response**: `{ "version", "query", "total", "files": [{ "path", "name", "score" }] }`

#### `GET /api/fs/raw/{version_name}`
Serve an image blob of the revision (documentation figures). Only `svg`, `png`, `jpg`/`jpeg`, `gif` are served (else 404); a `name.*` path picks the first existing of `.svg/.png/.jpg/.jpeg/.gif` (Sphinx convention). Responses carry `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` so scripts embedded in SVG never run; files over 50 MiB return 413.
- **Parameters**: `path` (query, jailed relative path), `repo` (query, optional repository for `<img>` requests that cannot send headers).

#### `GET /api/fs/compile_commands/{version_name}/export`
Download a Clang JSON Compilation Database (`compile_commands.json`) with one entry per `.c` file of the revision's Git tree, excluding other architectures' `arch/<name>/` subtrees, as an attachment (`Content-Disposition: attachment; filename="compile_commands-<version>-<arch>.json"`, both names reduced to file-name characters). Commands include the arch/generic include paths, `include/linux/kconfig.h`, `-D__KERNEL__` and `-nostdinc`.
- **Parameters**:
  - `version_name` (path): Revision.
  - `arch` (query): Target architecture (e.g. `"x86"`, `"arm"`, default `"x86"`).
- **Body**:
```json
[
  {
    "directory": "/kernel",
    "file": "init/main.c",
    "command": "clang -Iarch/x86/include ... -D__KERNEL__ -nostdinc -c init/main.c -o init/main.o"
  }
]
```

---

### 4.3. Symbols, Cross-References & AST Inspection API

#### `GET /api/symbols/{version_name}/search`
Search symbol definitions by name prefix across functions, variables, structs, enums, enumerator constants (`EnumConstant`), typedefs, and macros.
- **Revision scoping of symbol queries** (also `detail/{name}`, `xref/{name}`, `include/{ast_id}`, the callgraph and Pahole, §4.7): `version_name` may be a tag, branch, or any commit SHA, including commits that were never ingested into `m_commit`. `SymbolService._scope` builds `{blob_id: [paths]}` from the revision's Git tree (`GitTreeResolver.blob_paths`) and falls back to `m_bridge_checkpoint` only when Git is unavailable. Only definitions whose blob is in that tree are returned, with `file_path` taken from the tree. A symbol is therefore found at a commit whenever its file's exact content was parsed in any version; files whose content was never parsed contribute no symbols. The first query per commit costs ~0.3 s to build the blob map; later queries are cached.
- **Ranking and cost**: the exact name first, then shorter names, then by name; definitions of one name by path and line. Ordering the `m_symbol_def` view itself sorted every prefix match of every indexed version (328 s for `s`, 4.7 s for `sched`), so the search ranks distinct names first: `_name_page` reads `NAME_PAGE` (400) names at a time from the name indexes of `m_tag_def` and `m_blob_symbol_def` (`GROUP BY name ORDER BY LENGTH(name), name`, continuing after the last key) and merges them. Their definitions are then read in batches of names (`FIRST_NAME_BATCH` = 8, doubling up to 128, at most `SEARCH_CANDIDATES` rows each) and scoped to the revision until `limit` are kept or `MAX_NAME_PAGES` (8) pages were read. Warm, `s` answers in ~0.7 s and `sched` in ~1.1 s.
- **Parameters**:
  - `version_name` (path): Revision.
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


#### `GET /api/symbols/{version_name}/detail/{symbol_name}`
The definition of a symbol in the revision's tree (what Go to Definition opens). References are not part of it: `xref/{name}` computes them, only for the views that list them.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `symbol_name` (path): Symbol identifier.
  - `arch` (query, default `x86_64`): ARCH value whose definition wins when the symbol is defined per architecture (§2.7); also the ARCH whose Kconfig model answers `CONFIG_*` / Kconfig symbols.
  - `from_file` (query, optional): file the symbol is used in; the definitions its `#include` chain reaches for `arch` win (§2.7 item 4).
- **Response** also carries `arch`, `from_file` and `definitions` (every in-tree definition in rank order, at most 50: `[{file, line_s, line_e, type_name, ast_id, linkage, visible}]`, `visible` = reached from `from_file`, `null` without it); `file_path`/`definition` are the first one.
- **Kconfig symbols** (`CONFIG_*`, or a name no C definition has): answered from the ARCH's Kconfig model with `is_kconfig: true`, `type_name: "kconfig"` and the model's detail under `kconfig`.
- **Response**:
```json
{
  "version": "v3.0",
  "name": "start_kernel",
  "type_name": "FunctionDecl",
  "file_path": "init/main.c",
  "line": 490,
  "line_s": 490,
  "definition": { "def_id": 1, "ast_id": 42, "tag_id": 7, "type_id": 21, "type_name": "FunctionDecl", "fid": 684, "file": "init/main.c", "line_s": 490, "line_e": 680 },
  "definitions": [{ "file": "init/main.c", "line_s": 490, "line_e": 680, "type_name": "FunctionDecl", "ast_id": 42, "linkage": 0, "visible": null }],
  "arch": "x86_64",
  "from_file": null
}
```

#### `GET /api/symbols/{version_name}/xref/{symbol_name}`
Retrieve both definition location(s) and usage references (declarations, type usages, call sites, member references, declaration references, macro expansions, told apart by each reference's `type` badge) across the kernel codebase.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `symbol_name` (path): Symbol identifier.
  - `limit` (query, optional int, 1-5000): Maximum number of reference records to return (useful for symbols with thousands of references like `kmalloc`). Always capped at `MAX_XREF_REFS` (5000), including when no limit is given.
  - `arch` (query, default `x86_64`): orders `definitions` for that ARCH (§2.7); `definition` is the first.
- **Definitions**: only definitions whose blob is in the revision's tree, so every `file` is a path of this revision. When the index knows the symbol but holds no definition of it in this tree — the revision's copy of the defining file was never parsed, which is common at a commit between two ingested releases — `definitions` is `[]`, `definition` is `null` and `definition_unindexed` is `true` (the references still resolve, see below).
- **References**: only references in blobs of the revision's tree are returned, grouped by role (`Decl`, `TypeUsage`, `Call`, `MemberRef`, `DeclRef` — which also takes unknown roles as `Ref` —, `MacroExp`) and ordered by file and line within a role. `file` is the path of the referencing blob in that tree (best-ranked for `arch` when the blob sits at several paths; not `target_fnid`, which is the target's file), `caller` is the name of the AST of the tag the reference sits in (e.g. the enclosing function), `fid` the referencing blob, and `limit` applies after scoping.
- **Reference targets**: a reference resolves to the AST of the definition its blob was parsed against, so the targets are the ASTs of the in-tree definitions, or — without any — every indexed AST of the symbol (at most `MAX_XREF_TARGETS` (64); one per content of the defining file), which is what keeps the usages of a revision with no in-tree definition. An AST is a target only when `m_ast.name` is the symbol's name: a marker definition (`EXPORT_SYMBOL(sym)`, an initcall) records the symbol's name against the AST of the *macro*, which is content-addressed and therefore one shared node for every symbol marked that way (180k `C_ExportSymbolDef` rows share 4 ASTs in a 2.6.x index), so using it would return the references of all of them. Such a definition is still listed in `definitions` (it is a real source location).
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
  "definition_unindexed": false,
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
Recursively inspect relational AST node container hierarchies.
- **Parameters**:
  - `version_name` (path): Revision (AST nodes are content addressed, so it does not change the answer and the service ignores it).
  - `ast_id` (path): Root AST Node ID to expand.
  - `depth` (query): Traversal depth limit (0-12, default 3).
- **Size bound**: AST nodes are content-addressed, so one subtree is referenced by many parents: subtrees are built once per (node, remaining depth) and reused, but a reused subtree still serializes at every position it occupies, so the body grows multiplicatively with depth. `MAX_AST_TREE_NODES` (20,000 serialized nodes) bounds it at any depth: each node built counts one, each reuse of a built subtree counts its whole size, and the first child that does not fit cuts the remaining children of its parent, which then carries `truncated: true`. Each node lists its children once, in `m_ast_container` priority order. A former `containers` list repeated every child next to its priority, read by no client; it doubled the body at every level and its positions were counted only after each subtree was finished, so the bound did not hold (397,000 positions, 55 MB at depth 12 under `copy_process`; now 4,155 positions, 0.45 MB).
- **Cost**: the tree is fetched level by level (`m_ast_container` and `m_ast` rows of a whole level per query, `AST_TREE_CHUNK` = 1000 ids), not with one query per node: depth 3 below `copy_process` takes ~40 ms instead of ~250 ms (105 queries).
- **Response**: the root node itself. Children with `type_id` > 20 (inner statements and expressions) carry `tag_id: null`.
```json
{
  "ast_id": 42,
  "tag_id": 1234,
  "name": "start_kernel",
  "type_id": 5,
  "type_name": "FunctionDecl",
  "children": [
    {
      "ast_id": 43,
      "tag_id": null,
      "name": "setup_arch",
      "type_id": 21,
      "type_name": "CallExpr",
      "children": []
    }
  ]
}
```

#### `GET /api/symbols/{version_name}/include/{ast_id}`
Retrieve imported symbols and target header file path for a preprocessor `CPPro_include` AST node.
- **Parameters**:
  - `version_name` (path): Revision.
  - `ast_id` (path): AST Node ID of the include directive. If `ast_id <= 0`, it is found from `tag_id`, else in the blob `file_path` has at this revision (the include tag covering `line`, else the one naming `header`).
  - `tag_id` (query): Optional tag ID of the include node.
  - `file_path` (query): Path of the source file containing the include.
  - `line` (query): 1-indexed line number of the include in the source file.
  - `header` (query): Include header target (e.g. `<linux/init.h>` or `linux/init.h`).
- **Definitions**: each symbol's definition is taken from the revision's tree (`SymbolService._scope`): in the header itself first, then in its directory.
- **Response**:
```json
{
  "ast_id": 365653,
  "include_text": "<linux/const.h>",
  "header_file": "include/linux/const.h",
  "symbols": [
    {
      "priority": 1,
      "ast_id": 5512,
      "name": "_AC",
      "type_id": 76,
      "type_name": "CPPro_define_macro",
      "category": "Macro",
      "def_file": "include/linux/const.h",
      "def_line_s": 14
    }
  ]
}
```

#### `GET /api/symbols/{version_name}/history`
Release-by-release history of one definition (function, struct, union, enum, typedef, macro, global, ASM label, Rust item...) in the active repository (`HistoryService.get_symbol_history`, `webapp/backend/services/history_service.py`).
- **Parameters**: `path` and `line` (query) plus optional `name` (the symbol under the cursor) and `ast_id` select the anchor: the narrowest tag of the file at `version_name` covering `line` that is named `name` (else has `ast_id`, else is a definition in `m_symbol_def`). When `name` is not defined at `path:line` (the cursor is on a use), the definition is located through `m_symbol_def` (the requested file first, then the `arch` query parameter's ranking (§2.7, default `x86_64`), then definitions with a body, then `.c` files). An anchor that is a Kconfig `config`/`menuconfig` entry returns the Kconfig history below. `include_rc` (bool, default `false`): also list pre-release tags (`-rc`, `-pre`, `-alpha`, `-beta`, `RC_RE`); by default only final releases are listed. `branch` (optional): the branch the timeline follows, see **Branch scope**.
- **Branch scope** (`history_scope()`): a timeline follows **one branch**: the indexed tags reachable from it (the `GET /api/branch/tags` walk, cached per branch head) and the branch's own ref when it is indexed; tags of other branches and bare-commit refs are left out, so stable point releases (`v2.6.32.71`) never interleave with mainline releases on `master`, and a stable branch lists the mainline releases up to its fork followed by its own point releases. Without `branch` it is chosen for `version_name`: the version itself when it names a branch, the default branch when the version is one of its tags (or is not a tag, e.g. a commit), else the branches whose name carries the version's release number (`stable/linux-2.6.32.y` for `v2.6.32.71`; at most 3 walked), the first one reaching it; the default branch when none does. `400` for a branch that is not a plain ref, `404` unknown, `503` when Git could not walk it. The response carries `branch` (the branch followed) and `rc_count` (pre-release tags of that branch, listed or not).
- **Identity**: versions are matched by the anchor's identity, `m_ast.name` + `type_id`, never by `ast_id` (a function's `ast_id` hashes signature + body, so it changes with every code change). When a blob holds several matches, a member of the anchor's `m_moved_tag` lineage (transitive, both directions, bounded to 500 tags) wins, then the one closest to the previous version's line; a renamed lineage member is used only when no tag of the same name remains.
- **Algorithm**: the file's blob at every listed ref comes from one `git cat-file --batch-check` (content hash → `m_file_blob`). Per ref: same `tag_id` as the previous ref or identical code → `unchanged`; another tag → `modified`, or `cosmetic` when the code is identical once comments and whitespace are removed; first appearance → `introduced` (`added` when it reappears); no match → `removed`/`absent`; blob never parsed → `unindexed`; blob parsed without the definition although a non-indented line of the file still names the symbol → `unparsed` (an indexing gap, the previous state is kept). Code comes from on-demand Git blob slicing via `m_bridge_tag` coordinates (cached in memory); diffs (with context lines) compare against the last present version.
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
  "branch": "master",
  "rc_count": 203,
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
Release-by-release history of every definition of `CONFIG_<symbol>` (`CONFIG_` prefix optional) across all Kconfig files (`HistoryService.get_kconfig_history`).
- **Parameters**: `include_rc` (bool, default `false`) and `branch` as above (the branch scope applies the same way); `version_name` picks the reported `path` and, without `branch`, the branch followed.
- **Algorithm**: the Kconfig tags of the symbol (`m_ast` name + `Kconfig_Config`/`Kconfig_Menuconfig` → `m_tag` → `m_bridge_tag`) give the blobs defining it and, through `m_bridge_checkpoint`/`m_commit_file`, the candidate paths; one `git cat-file --batch-check` resolves every (ref, path). Each defining file is parsed with `KconfigParser` (cached per blob SHA) to extract the definition's properties, including the dependencies inherited from enclosing `if`/`menu`/`choice` blocks. Definitions are paired per path (in line order) between consecutive refs: `added`, `removed`, `unchanged` (same tag or identical code), `modified` (a property changed) or `cosmetic` (text changed, properties identical). The entry status aggregates them (`introduced`, `added`, `modified`, `cosmetic`, `removed`, `unchanged`, `absent`, `unindexed`).
- **Response**: `{ "kind": "kconfig", "name", "ast_name": "CONFIG_<name>", "path", "paths", "include_rc", "branch", "rc_count", "total_versions", "change_count", "snippets": {tag_id: code}, "timeline": [{ "vname", "vid", "is_release", "prev_vname", "status", "lines_added", "lines_removed", "defs": [{ "path", "status", "tag_id", "line_s", "line_e", "facts", "changes", "diff", "lines_added", "lines_removed" }] }] }`. `facts` = `{ line_s, line_e, kind: config|menuconfig, type, prompt, defaults[], depends_on[], inherited_depends[], selects[], implies[], ranges[], choice, options[], help }` (conditions rendered as `X if COND`); `changes` = `[{ field, before, after }]` for scalar fields (`kind`, `type`, `prompt`, `choice`, `help`) and `[{ field, added[], removed[], before[], after[] }]` for list fields.

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

All Kconfig endpoints read the Git objects of the requested revision (never the work tree), accept an optional **toolchain profile** (`toolchain` query parameter holding JSON on GET endpoints, `toolchain` body field of the auto-solve POST; see `GET /toolchain` below) and are scoped by an **ARCH value** (`arch` parameter, default `x86_64`): the value `make ARCH=<arch>` would receive. The top Makefile maps some values onto another source directory (`i386`/`x86_64` -> `x86`, `sparc32`/`sparc64` -> `sparc`, `sh64` -> `sh`, ...); `x86`, `sparc` and `sh` themselves are the generic forms whose bitness is chosen in menuconfig. The model is built by `parser/kconfig_ast/kconfig_model.py` and evaluated by `parser/kconfig_ast/kconfig_eval.py` (server) and `kconfig_engine.js` (browser); both reproduce the kernel's `conf` output byte for byte (see `parser/kconfig_ast/KCONFIG_API.md` section 8). Models are cached per (repository, revision, ARCH, toolchain) in a 6-entry LRU, and persist across restarts as gzipped JSON under `~/.cache/kernelinfo/kconfig-models/` (`KERNELINFO_CACHE_DIR` overrides the base directory; 32 files, least recently used evicted, ~4 MB each for a modern tree), keyed by commit SHA, ARCH, toolchain profile and a hash of the model-building code, so a code change or a moved branch never reuses a stale model (a cold build of a modern tree takes ~2-3 s, a cached one ~0.3 s). A build costs about a second of CPU and tens of MB, and the toolchain profile is part of the key, so builds are bounded: each key has its own lock (concurrent requests for one model wait for that single build instead of duplicating it, and never block requests for other models), at most two models are built at once across all keys, and the cache lock is never held while a model is built.

#### `GET /api/kconfig/{version_name}/toolchain`
Default toolchain profiles of a revision. Linux 4.18+ Kconfig runs `$(shell,...)` checks against the compiler, linker and assembler while it is parsed; the server answers them from a profile (`parser/kconfig_ast/kconfig_toolchain.py`). Response: `{ "version", "arch", "kernelversion", "macros": true, "gcc": {profile}, "clang": {profile} }` where a profile is `{ "cc", "cc_version", "ld", "ld_version", "assembler", "binutils_version", "cross_compile", "llvm_utils", "pahole_version", "rustc_version", "gcc_plugins", "host_release", "env", "probes" }`. `macros: false` means the revision predates the macro language: only environment variables (`option env=`) matter.
- **Toolchain parameter**: validated before use — unknown keys are ignored, each value is coerced to its field's type, `env`/`probes` must be objects of at most 128 string entries (each value at most 512 characters), and a wrong shape (`{"env": "text"}`) answers `HTTP 400 Bad Request` rather than failing inside the model build. It is any subset of the profile, e.g. `{"cc": "clang", "cc_version": "18.1.3", "env": {"CC_VERSION_TEXT": "Ubuntu clang version 18.1.3"}, "probes": {"cc-option:-fsanitize=kcfi": "n"}}`. Missing fields take the defaults of that kernel release. `env` overrides any environment variable Kconfig reads; `probes` forces a check (`"y"`/`"n"`) or a command output (text), keyed as reported in the model's `toolchain.probes`.

#### `GET /api/kconfig/{version_name}/tree`
The finalized configuration model of one ARCH, in compact transport form, for the client-side engine (`decodeModel()` in `kconfig_engine.js`).
- **Parameters**: `version_name` (path); `arch` (query, default `x86_64`); `toolchain` (query, JSON).
- **Response** (abridged):
```json
{
  "format": 2, "version": "v3.0", "arch": "x86_64", "srcarch": "x86",
  "env": {"ARCH": "x86_64", "SRCARCH": "x86", "KERNELVERSION": "3.0.0"},
  "mainmenu": "Linux/x86_64 3.0.0 Kernel Configuration",
  "config_header": "Automatically generated make config: don't edit",
  "modules_sym": "MODULES", "root_kconfig": "Kconfig",
  "exprs": [["s", "64BIT"], ["c", "x86_64"], ["=", 3, 1], ["&", 0, 2]],
  "menu_fields": ["parent", "kind", "sym", "prompt", "pvis", "vis_if", "dep", "file", "line_s", "line_e", "has_help"],
  "menus": [[-1, 0, null, "Linux/x86_64 3.0.0 Kernel Configuration", -1, -1, -1, 0, 6, 0, 0], [0, 3, "64BIT", "64-bit kernel", 12, -1, -1, 1, 2, 9, 1]],
  "files": ["Kconfig", "arch/x86/Kconfig"],
  "symbols": {"64BIT": {"t": "bool", "p": [12], "d": [[14, -1]], "m": [1]}},
  "warnings": [],
  "semantics": {"write_on_nonzero_default": false, "menu_end_comments": false, "int_hex_zero_default": false, "tristate_choice_mod_hidden": false},
  "toolchain": {
    "macros": false, "summary": "gcc 5.4.0 · GNU ld 2.25", "profile": {...}, "env": {"CC": "gcc", "...": "..."},
    "env_used": [{"name": "ARCH", "value": "x86_64"}],
    "probes": [{"key": "cc-option:-fsanitize=kcfi", "kind": "cc-option", "label": "Compiler accepts -fsanitize=kcfi", "command": "...", "result": "n", "source": "knowledge", "note": "gcc >= never, clang >= 16.0", "locations": [{"file": "arch/Kconfig", "line": 790, "symbol": "CFI_CLANG"}]}],
    "messages": [{"level": "error", "text": "Sorry, this C compiler is not supported.", "file": "scripts/Kconfig.include", "line": 44}]
  }
}
```
  `semantics` lists `conf` behaviours of this revision (detected from its `scripts/kconfig` sources) that both engines follow. `toolchain` reports what the build environment contributed: every environment variable read (`env_used`), every toolchain check with its answer and source (`toolchain` = derived from the profile, `knowledge` = known first gcc/clang versions, `assumed` = no data, assumed to succeed, `override`), the Kconfig entries using it, and `$(error-if)`/`$(warning-if)`/`$(info)` messages.
  Expression nodes: `["s", name]`, `["c", text]`, `["&"|"|", a, b]`, `["!", a]`, `["="|"!="|"<"|"<="|">"|">=", a, b]`; `-1` = no expression. Menu `kind` codes: 0 root, 1 menu, 2 choice, 3 config, 4 menuconfig, 5 comment, 6 if. Symbol keys: `t` type, `p` prompt visibilities, `d` defaults `[value, condition]`, `r` ranges, `s` selects, `i` implies, `rd` reverse dependency, `im` implied-by, `dd` direct dependency, `m` defining menu entries, `ch` choice of a choice value, `ic`/`op`/`mb` choice flags and members, `au` automatic (env/choice), `env`.

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}`
Arch-scoped details of a symbol (with or without `CONFIG_`). The same name can be declared in different files per architecture (e.g. `64BIT`: `arch/x86/Kconfig` for x86_64, `arch/sparc/Kconfig` for sparc64).
- **Parameters**: `arch` (query, default `x86_64`).
- **Response**: `name`, `arch`, `srcarch`, `defined_for_arch`, `type` (1 bool, 2 tristate, 3 string, 4 hex, 5 int) / `type_name`, `prompt`, `prompts`, `help`, primary `file_path` / `line_s` / `line_e` / `definition`, `declarations` and `definitions` (every definition for this ARCH: `file_path`, `line_s`, `line_e`, `prompt`, `kind`, `help`), `depends_on` (symbols named by its own `depends on` lines), `selects`, `implies`, `defaults` (`[{value, cond}]`), `def_val`, `ranges`, `choice`, `env`, `kconfig_refs` (entries referring to it), `kbuild` (Makefile rules, see usage), `compiled_files` (`[{file_path, target_obj, compile_mode, makefile, line, other_arch}]`: the existing files among the `kbuild` rules of this revision). Reverse dependencies and evaluated expressions are not part of it: the KConfig view reads them from its own engine (§5.8).
- When the symbol is not declared for this ARCH: `defined_for_arch: false` and `declarations` lists its declarations in other architectures' Kconfig files (404 if declared nowhere).

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}/usage`
Where `CONFIG_<symbol>` is used (Git grep of the revision, cached; a `git grep` that fails or times out answers 503 and is not cached).
- **Parameters**: `arch` (default `x86_64`), `limit` (1-2000, default 400). `symbol_name` must be an identifier (§2.1.2) or the request is rejected with 400.
- **Response**: `kbuild` (`[{makefile, line, rule, target_obj, path, exists, kind: object|composite|directory, via: rule|condition, compile_mode, other_arch}]`: objects and directories built when the option is enabled, including rules inside `ifeq ($(CONFIG_X),y)` blocks), `source_refs` (`[{file_path, line, text, other_arch}]` in `.c .h .S .lds .dts .dtsi`, `CONFIG_X` and `CONFIG_X_MODULE`), `source_ref_count`, `source_refs_truncated` (the symbol has more than 20,000 references, so the list was cut before being cached; the view shows the count with a `+`), `source_file_count`, `kconfig_refs` (`[{kind: depends_on|select|imply|default|prompt_condition|select_condition|imply_condition, by, file, line}]`). `other_arch` marks paths in an architecture tree (`arch/<a>/`, or `include/asm-<a>/` in 2.6.x) the ARCH's build layout does not use (§2.7).

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

#### `GET /api/kconfig/{version_name}/defconfigs`
Defconfigs of the ARCH's source directory: `arch/<srcarch>/defconfig` and every file under `arch/<srcarch>/configs/` (nested ones such as powerpc `40x/acadia_defconfig` included). The one flagged `is_canonical` is what `make ARCH=<arch> defconfig` loads: `KBUILD_DEFCONFIG` from `arch/<srcarch>/Makefile` evaluated with ARCH/SRCARCH known, `CONFIG_*` undefined and `CROSS_COMPILE` set (e.g. `x86` -> `i386_defconfig`, `x86_64` -> `x86_64_defconfig`, `powerpc` -> `ppc64_defconfig`), else `arch/<srcarch>/defconfig`.
- **Response**: `{ "version", "arch", "defconfigs": [{ "name", "file_path", "fid", "is_canonical" }] }`.

#### `GET /api/kconfig/{version_name}/defconfig`
Raw defconfig text and its literal assignments. Nothing is injected: everything a defconfig leaves out is derived by kconfig when it is loaded.
- **Parameters**: `file_path` (name or path; empty = the ARCH's default defconfig), `arch`, `toolchain`.
- **Response**: `{ "version", "name", "file_path", "arch", "srcarch", "bits", "values", "content" }`; `bits` is `64BIT` after resolving the defconfig with kconfig (null for architectures without that symbol).

#### `GET /api/kconfig/{version_name}/presets`
Every ARCH value of the revision: one per `arch/*` directory plus the top Makefile's aliases.
- **Response**: `{ "version", "default_arch", "targets": [{ "id", "label", "arch", "srcarch", "bits", "env": {"ARCH", "SRCARCH", "KERNELVERSION"} }] }`. The default defconfig of each ARCH is the one `GET /defconfigs` flags.

#### `GET /api/kconfig/{version_name}/search`
Search symbol names and prompts of the ARCH model (`CONFIG_` prefix optional). Parameters: `q`, `limit` (1-500, default 25), `arch`. Response: `{ "version", "arch", "query", "count", "total", "symbols": [{ "name", "type", "type_name", "prompt", "help", "file_path", "line_s" }] }`.

#### `GET /api/kconfig/{version_name}/graph`
Direct relations of one symbol in the ARCH model: its own `depends on` symbols (`depends_on` edges), `selects` / `implies` targets, and selectors (`selects` edges into it). Parameters: `symbol`, `arch`, `toolchain`. Response: `{ "version", "arch", "symbol", "edges": [{ "from", "to", "type" }] }`.

#### `POST /api/kconfig/{version_name}/autosolve`
Legal changes (the ones menuconfig would accept, applied in order) that make `target_symbol` reach `value` from `current_values`. The solver satisfies prompt visibility recursively (AND: both sides, OR: cheapest side, `!X`: lowers X), selects prompt-less symbols through a selector, and refuses symbols forced by select, fixed by the architecture or without prompt.
- **Body**: `{ "target_symbol": "KVM_INTEL", "value": "y", "arch": "x86_64", "toolchain": {...}, "current_values": {"KVM": "n"} }`.
- **Response**: `{ "solution_found": true, "toggles_needed": [{ "symbol": "KVM", "from": "n", "set_to": "y", "to": "y" }, { "symbol": "KVM_INTEL", "from": "n", "set_to": "y", "to": "y" }], "total_toggles": 2, "blocked_by": [], "final_value": "y", "target": "KVM_INTEL", "arch": "x86_64" }`.

Validation, import, export and configuration comparison run in the browser on the same model (`kconfig_engine.js`, §5.8); the server only builds the model and answers the auto-solver.

---

### 4.5. Maintainers & CREDITS API

**Version scoping** (`MaintainerService._version_scope`): sections and CREDITS entries are content addressed. A section unchanged since an earlier MAINTAINERS keeps the row stored with that earlier blob (`m_maintainer_section.blob_id`), so the sections of a version are the rows whose `ast_id` is one of its MAINTAINERS blob's tags (`m_bridge_tag` → `m_tag.ast_id`), one per section name (the newest row when several share it); CREDITS entries likewise through `m_credits_entry.ast_id`. v3.0 lists 925 sections and 507 CREDITS entries, exactly its files' contents. A version whose MAINTAINERS (or CREDITS) blob was never parsed lists none of them (`indexed: false` in the overview) rather than every section ever parsed. The id lists are cached per blob (`_BLOB_SECTIONS_CACHE`, `_BLOB_CREDITS_CACHE`, 64 entries each); the subsystem matcher per version (`_MAINTAINER_CACHE`, 16 entries).

#### `GET /api/maintainers/{version_name}/overview`
Search and list the subsystems of the version's `MAINTAINERS` file.
- **Parameters**:
  - `version_name` (path): Revision.
  - `q` (query): Search filter across subsystem names, maintainer names, and emails.
- **Response**:
```json
{
  "version": "v3.0",
  "indexed": true,
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
Retrieve comprehensive details for a subsystem section, including member rosters, file matching rules (`F:`, `X:`), and touched files.
- **Parameters**:
  - `version_name` (path): Revision.
  - `sec_id` (path): Numeric section ID, or a subsystem name (looked up among the version's sections).
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
#### `GET /api/maintainers/{version_name}/person?email=...`
Retrieve developer profile, CREDITS biographical data, maintained subsystems, and git contribution statistics. An email address goes in the `email` query parameter (its `@`, `<`, `/` do not survive a path segment); a numeric id or a name in the path.
Subsystems, the CREDITS entry, recent commits and contribution stats are aggregated over every alias persona of the developer (same identity rule as the Developers directory, see **Persona identity merging** below), so a profile always matches its directory card. Contribution stats count distinct commits per role. An email/name lookup prefers a named persona over a name-less one.
- **Parameters**:
  - `version_name` (path): Revision (its MAINTAINERS and CREDITS files scope `subsystems` and `credits`).
  - `person_id_or_email` (path): Developer numeric person ID, email address, or name.
  - `email` (query): Developer email address (e.g. `"torvalds@linux-foundation.org"`).
- **Response**:
```json
{
  "person_id": 47,
  "name": "Philip Blundell",
  "email": "philb@gnu.org",
  "emails": ["philb@gnu.org"],
  "alias_person_ids": [47],
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
Emulate `scripts/get_maintainer.pl` by evaluating touched paths or raw patch content against the subsystem pattern rules of the version's MAINTAINERS. The `in_credits` flags come from the version's CREDITS, read once per version (`_CREDITS_KEYS_CACHE`).
Every file is matched against every MAINTAINERS pattern, so at most `MAX_PATCH_FILES` (200) files of one patch are resolved; the rest are counted in `skipped_files_count` (the patch reviewer says how many were left out) and not matched (2,000 files took ~14 s before this cap, and the cost was paid even when nothing matched).
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - **Body** (`application/json`):
    ```json
    {
      "touched_files": ["drivers/net/3c505.c"],
      "patch": "diff --git a/... (optional unified diff)"
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
Search the entries of the version's `CREDITS` file and their biographical details.
Returns one entry per developer: several rows of one entry and the same person under other alias personas collapse into the newest entry. Its addresses are unioned into `emails`, empty bio fields are filled from older revisions, and `email` falls back to the merged list when the newest revision lost it. Names are cleaned (see **Persona identity merging**).
- **Parameters**:
  - `version_name` (path): Revision.
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
      "emails": ["philb@gnu.org"],
      "alias_person_ids": [47],
      "description": "Linux/ARM hacker Device driver hacker",
      "snail_mail": "Cambridge CB5 8EG\nUnited Kingdom"
    }
  ]
}
```

#### `GET /api/maintainers/{version_name}/developers`
Search and list all kernel developers, maintainers, reviewers, and contributors recorded in the kernel persona registry (`m_maintainer_person`).

**Persona identity merging**: the registry holds one row per (name, email) alias, so one developer appears under many `person_id`s. Rows are merged into one entry per identity (`MaintainerService._identity_index`, cached per repository until the registry grows):
- Identity = name folded for case/whitespace, with surrounding quotes and a leaked `From:` prefix stripped (`_clean_name`, also the displayed name).
- A name-less persona joins the named identity that owns its address; when several do (typo'd name, nickname), the one using it on the most personas, then the largest, wins.
- Named identities are never merged with each other by email: transitive email links chain distinct people together (shared/mistyped addresses), so `"Greg KH"` stays separate from `"Greg Kroah-Hartman"`.
- The merged entry uses the alias with a name and email, most subsystems, CREDITS, commits as `person_id`/`email`; flags are OR-ed over aliases, `subsystems_count` is the distinct subsystem names across aliases. The search and `role` filter apply to the merged identity (a match on any alias returns the whole identity); sorting happens after merging.
- **Parameters**:
  - `version_name` (path): Revision (`subsystems_count`, `is_maintainer`, `is_reviewer` and `in_credits` count only its MAINTAINERS and CREDITS).
  - `q` (query): Case-insensitive search on developer name or email.
  - `role` (query): Role filter (`"all"`, `"maintainer"`, `"reviewer"`, `"credits"`). Default `"all"`.
  - `sort` (query): Sort order (`"activity"` for subsystems count descending, or `"alpha"` for alphabetical A-Z). Default `"activity"`.
- **Response**:
```json
{
  "version": "v3.0",
  "sort": "activity",
  "developers": [
    {
      "person_id": 72,
      "name": "Lennert Buytenhek",
      "email": "kernel@wantstofly.org",
      "emails": ["kernel@wantstofly.org", "buytenh@wantstofly.org", "buytenh@marvell.com"],
      "alias_person_ids": [67, 1135, 5334, 5338, 7184],
      "subsystems_count": 24,
      "is_maintainer": true,
      "is_reviewer": false,
      "in_credits": true,
      "has_commits": false
    }
  ]
}
```

---

### 4.6. Git Commits & Patch Management API

#### `GET /api/commits/{version_name}/list`
Search the indexed commits of the request's repository, newest first (the Commits view's search; its unfiltered list is the timeline below).
- **Repository scoping**: commit ids are global, so the query is restricted to the repository's Git network (`m_commit.network_id` = `m_repository.network_id`, `helpers.get_active_network_id`): other repositories of the database never appear. A repository that was never parsed answers an empty list.
- **Current-date filtering & Git fallback**: By default, commits with future dates (`author_date > now`) are excluded to hide erroneous timestamps. When `q` is provided, future-dated commits can be matched if searched for. If `q` names an unparsed commit hash or prefix not in `m_commit`, results fall back to querying the Git CLI directly.
- **Parameters**:
  - `version_name` (path): Revision (must resolve, else 404).
  - `page` (query): Page number (1 or more, default 1).
  - `limit` (query): Commits per page (1-500, default 50). Bound again in the service, which also clamps the derived `OFFSET` (`MAX_COMMIT_PAGE`, `MAX_COMMIT_OFFSET`), since both are bound straight into SQL.
  - `q` (query): Filter by commit subject, hash prefix (matched via indexed binary hash range `helpers.commit_prefix_range`), or author name. Wildcards are escaped (§2.2), so `%` and `_` match literally. Commits are traversed using primary key order (`c.commit_id DESC`), preventing full-table filesort timeouts.
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
Retrieve detailed metadata for a commit, including multi-contributor trailers and modified files.
- **Unparsed commits & Git fallback**: When a commit hash or reference is not found in `m_commit`, the service falls back to inspecting the Git repository directly (`git log`, `git show`, `git diff-tree`), parsing commit headers, trailers, and modified files in memory without database writes.
- **Modified files**: `files` comes from `m_commit_file` (`files_source: "db"`). `git log -p` emits no diff for a merge, so the parser stages **no file rows for merge commits** (`parser/git_ast/GIT_API.md` §2); for any commit whose staged list is empty the service resolves it live with `git diff -M --name-status <first parent> <commit>` and reports `files_source: "git"` — the same comparison `.../diff/{hash}` renders, so the *Modified Files* count can no longer disagree with **± Show changes**. A commit that genuinely changes nothing keeps an empty list and `files_source: "none"`. `is_merge` is set from the commit's parent count in `m_commit_parent`, which also gives the first parent of that diff (Git is asked, `rev-list --parents`, only for a commit without parent rows: a root, or one whose parents were not ingested), and a list longer than 5000 entries is cut with `files_truncated: true`.
- **Parameters**:
  - `version_name` (path): Revision (not used: commit ids and hashes are global).
  - `commit_id_or_hash` (path): Commit integer ID, 40-character SHA-1 hash, or a short hex prefix (4-39 digits). A prefix is a range over the binary hash in which every digit counts (`helpers.commit_prefix_range`: an odd length bounds its last digit, so the 7-digit abbreviations Git prints resolve exactly); a prefix two commits share answers `HTTP 400` (ambiguous), as does anything that is not hex. The same range rule resolves short SHAs used as a version name (`get_version_info`, where an ambiguous prefix resolves to nothing).
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
  "files": [
    { "change_type": "M", "file": "Makefile" }
  ],
  "files_source": "db",
  "files_truncated": false,
  "is_merge": false,
  "linked_emails": [
    {
      "message_id": "20210515120000.1234-1-dev@kernel.org",
      "thread_id": 98765,
      "relation_type": 1,
      "match_tier": 1,
      "match_tier_name": "Trailer",
      "patch_version": 1,
      "patch_idx": 1,
      "patch_total": 1,
      "lore_url": "https://lore.kernelinfo.org/all/20210515120000.1234-1-dev@kernel.org/"
    }
  ],
  "lore_url": "https://lore.kernelinfo.org/all/20210515120000.1234-1-dev@kernel.org/"
}
```

#### `POST /api/commits/{version_name}/resolve_mail/{commit_id_or_hash}`
Execute on-demand live resolution of a Git commit against the Public-Inbox / LKML archive without pre-indexed database writes.
- **Parameters**:
  - `version_name` (path): Revision (not used: commit ids and hashes are global).
  - `commit_id_or_hash` (path): Commit integer ID or hex hash.
- **Read-Only Safety**: Strictly adheres to the zero-write backend invariant; queries the commit features, extracts `git patch-id` and blob hashes, invokes `MailingListClient.resolve_commits()`, and synthesizes linked email records dynamically in memory without executing any `INSERT` or `UPDATE` SQL queries. For unparsed Git-only commits, non-numeric `commit_id` references are safely sanitized to integer `0` in `CommitFeatures` before querying the mailing list daemon.
- **Limits**: the commit's patch is read with the 8 MiB cap of `.../diff/{hash}` (a larger one gets no patch id, since a truncated patch would hash differently), and every Git step has a 30 s timeout.
- **Response**:
```json
{
  "resolved": true,
  "cached": false,
  "resolved_count": 1,
  "linked_emails": [
    {
      "message_id": "20210515120000.1234-1-dev@kernel.org",
      "thread_id": 98765,
      "relation_type": 1,
      "match_tier": 2,
      "match_tier_name": "Live Resolved",
      "patch_version": 1,
      "patch_idx": 1,
      "patch_total": 1,
      "lore_url": "https://lore.kernelinfo.org/all/20210515120000.1234-1-dev@kernel.org/"
    }
  ]
}
```

#### `GET /api/commits/{version_name}/timeline`
Retrieve an anchored, bi-directional chronological commit timeline relative to a release tag or version commit. Resolves the version's anchor commit (e.g. `v3.0` -> commit 854 "Linux 3.0") and navigates forward into newer commits (`direction="future"`), backward into older commits (`direction="past"`), or centered around the anchor (`direction="around"`). Uses primary key indexed ordering on `commit_id` for instantaneous (<0.002s) execution. Every query stays inside the repository's Git network (see the list above), so two repositories of one database never interleave. Future-dated commits (`author_date > now`) are excluded from default timelines to omit corrupt timestamps. If `version_name` names an unparsed revision or branch outside `m_commit`, the timeline reads commits directly from Git (skipping any with timestamps beyond the current date).
- **Parameters**:
  - `version_name` (path): Kernel version string, branch, or commit SHA.
  - `limit` (query): Maximum commits to retrieve (1-250, default 100; the same bound in every direction).
  - `direction` (query): Seek direction: `"past"` (commits leading up to or before anchor, default), `"future"` (commits after anchor), or `"around"` (centered around anchor commit).
  - `cursor_id` (query): Optional integer commit ID pagination cursor.
- **Response**:
```json
{
  "version": "v3.0",
  "direction": "past",
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
      "files_count": 1
    }
  ]
}
```

#### `GET /api/commits/{version_name}/search`
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

#### `GET /api/commits/{version_name}/blame/{path:path}`
Line-by-line `git blame` of a file at a revision (`git blame --porcelain`: each commit's author and summary are sent by Git once, on its first line, and attached to every line of it). Output lines are split strictly by newline (`\n`) rather than universal line breaks (`splitlines()`), ensuring in-file control characters (e.g. form feeds `\f` / `\x0c`) preserve exact line alignment.
- **Parameters**:
  - `version_name` (path): Revision (validated by `safe_rev`, §2.1.1).
  - `path` (path): Jailed repository path (e.g. `"init/main.c"`); traversal answers 400, a failed blame 404, a blame running over 12 s 504.
- **Response**:
```json
{
  "version": "v3.0",
  "vid": 1,
  "path": "init/main.c",
  "lines": [
    {
      "line": 1,
      "commit_hash": "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
      "author_name": "Linus Torvalds",
      "author_email": "torvalds@ppc970.osdl.org",
      "date": 1113690036,
      "subject": "Linux-2.6.12-rc2"
    }
  ]
}
```

#### `GET /api/commits/{version_name}/history/{path:path}`
Retrieve chronological Git commit history touching a specific file up to a given version/tag/commit, along with immediate `last_changed` metadata.
- **In-Memory LRU Caching**: Leverages a 1024-entry thread-safe LRU cache (`_cached_git_file_history`) on `(rev, rel_path, limit)`, providing instantaneous (<0.05ms) sub-millisecond responses on repeated queries without spawning subprocesses.
- **Resilience Guard**: Wrapped with an 8-second execution timeout guard returning `HTTP 504 Gateway Timeout` on prolonged Git operations. Caller services (`FilesystemService`) safely fallback to `None` if unindexed or missing, preventing file viewer crashes.
- **Parameters**:
  - `version_name` (path): Kernel version string, tag, branch, or commit SHA.
  - `path` (path): Relative file path within the repository (e.g. `"init/main.c"`).
  - `limit` (query): Maximum commits to retrieve (default 50, 1-200).
- **Response**:
```json
{
  "version": "v3.0",
  "path": "init/main.c",
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

#### `POST /api/version/{version_name}/patch/format`
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
Pahole-style member offsets, alignment holes and 64-byte cachelines of a C struct or union, verified via compiler AST attributes and source extraction. `404` when the revision defines no struct or union of that name.
- **Definition**: the `C_structdecl` / `C_uniondecl` definitions of the name in the revision's tree (`SymbolService._scope`), ranked: definitions with members before forward declarations, then kernel headers before trees that carry their own copy for host programs (`tools/`, `scripts/`, `samples/`, `Documentation/`), then `.h` files, then `definition_rank` (§2.7). `struct list_head` at v3.0 is `include/linux/types.h:222`, 16 bytes.
- **Sizes & Offsets**: member sizes and bit offsets are read from compiler AST attributes (`m_ast_attribute`: `size_bytes`, `offset_bits`, `bitfield_width`), which are extracted directly by Libclang during AST parsing. For members where compiler attributes are absent, size falls back to the AST category (`_EXACT_SIZES` and `_GUESSED_SIZES`). Member declarator types and array subscripts (e.g. `unsigned char [16]` for `id[16]`) are extracted directly from the struct definition in Git source. `is_estimate` is true only when any member lacks compiler attributes and uses category estimation (`struct adb_ids` is exact 20 bytes; `is_estimate: false`).
- **Parameters**:
  - `version_name` (path): Revision.
  - `struct_name` (path): C struct identifier (e.g. `"task_struct"`; a `struct ` / `union ` prefix is stripped).
- **Response**:
```json
{
  "version": "v3.0",
  "struct_name": "struct list_head",
  "file_path": "include/linux/types.h",
  "line_s": 222,
  "line_e": 224,
  "is_estimate": true,
  "estimated_members": 2,
  "total_size": 16,
  "alignment": 8,
  "padding_bytes": 0,
  "holes_count": 0,
  "cache_lines_used": 1,
  "members": [
    {
      "name": "next",
      "type": "struct",
      "size": 8,
      "alignment": 8,
      "estimated": true,
      "offset": 0,
      "padding_before": 0,
      "cacheline_idx": 0,
      "cacheline_offset": 0,
      "crosses_cacheline": false
    }
  ],
  "optimization": {
    "current_size": 16,
    "optimized_size": 16,
    "bytes_saved": 0,
    "suggested_order": ["next", "prev"]
  }
}
```

#### `GET /api/tools/{version_name}/callgraph/{function_name}`
Retrieve bidirectional caller and callee execution flow. Consolidates multiple call sites of the same function with call frequencies and coordinate tracking.
- **Function**: the in-tree definitions of the name that are functions (`C_functionproto`/`C_functionprotodecl`, not macros), scoped to the revision's tree like the symbol endpoints; the one shown (`file_path`, lines, callees) has a body (not a prototype, and an `ExternalDef` before a header inline stub), ranked by `definition_rank` (§2.7).
- **Callers** are the calls to any in-tree declaration that is the function itself (a call binds to the declaration visible where it is made, usually a header prototype; at most 8 ASTs, `m_ast.name` must be the function's name), found in this revision's blobs and named after the AST of the tag containing the call. **Callees** are the calls inside the function's lines, each with the files defining the callee in this revision (`file_path` is null when none does). At most `MAX_CALL_SITES` (2000) call sites are kept per direction (`truncated: true` beyond). Reference lookup goes through every indexed blob holding the targets (`rows_of_targets_sql`), so a widely used function takes seconds (`schedule` at v3.0: 542 callers, ~25 s); the frontend allows 60 s.
- **Parameters**:
  - `version_name` (path): Revision.
  - `function_name` (path): C function identifier.
- **Response**:
```json
{
  "function": "copy_process",
  "version": "v3.0",
  "file_path": "kernel/fork.c",
  "line_s": 1049,
  "line_e": 1447,
  "truncated": false,
  "callers": [
    { "name": "do_fork", "file_path": "kernel/fork.c", "line_no": 1499, "call_count": 1, "lines": [1499] }
  ],
  "callees": [
    { "name": "security_task_create", "file_path": "security/security.c", "line_no": 1063, "call_count": 1, "lines": [1063] }
  ]
}
```

#### `GET /api/tools/diff/versions`
Compare file changes between two revisions (release tags, branches, or commit SHAs).
- **Parameters**:
  - `version_a` (query, required): Base revision (e.g. `"v3.0"` or `"1052cff317e7"`).
  - `version_b` (query, required): Target revision (e.g. `"v3.1"`).
- **Behavior**: See §2.8. The whole trees are compared; scoping to a directory is what the rename search below offers. `source` is `"git"` when both trees came from Git (Path C) and `"db"` for the database fallback. `fid`, `fid_old`, `fid_new` are indexed `blob_id`s, or `0` when the blob was never parsed. `changes` holds one row per change, sorted by path (8,790 rows at v3.0→v3.1, ~1.2 MiB of JSON); `symbols_added` / `symbols_removed` are set for the first 50 modified source files (§2.8 Level 2). Renamed entries carry `similarity` (percent, 100 = identical content). `renames` describes the rename pairing: `similarity` (the threshold, 50), `source` (`"git"` for Git's detection, `"exact"` when only identical content was paired), `exhaustive` (every deleted/added pair was compared) and `limit_needed` (the rename limit Git asked for when it was not, else 0).
- **Response**:
```json
{
  "v1": "v3.0",
  "v2": "v3.1",
  "source": "git",
  "summary": {
    "added_count": 1065,
    "removed_count": 762,
    "modified_count": 7032,
    "renamed_count": 391,
    "unchanged_count": 28828
  },
  "changes": [
    { "type": "added", "file_path": "Documentation/ABI/stable/firewire-cdev", "fid": 0 },
    { "type": "renamed", "file_path": "Documentation/DocBook/media/dvb/dvbstb.pdf", "old_file": "Documentation/DocBook/dvb/dvbstb.pdf", "similarity": 100, "fid": 13635 },
    { "type": "modified", "file_path": "arch/alpha/include/asm/atomic.h", "fid_old": 15495, "fid_new": 663012, "symbols_added": 1, "symbols_removed": 2 }
  ],
  "renames": { "similarity": 50, "exhaustive": true, "limit_needed": 0, "source": "git" }
}
```

#### `GET /api/tools/diff/unchanged`
Paths present at both revisions with the same content: the files behind `summary.unchanged_count` of the revision diff, which does not list them (28,828 paths, ~1 MiB of JSON at v3.0→v3.1).
- **Parameters**: `version_a`, `version_b` (query, required).
- **Behavior**: `DiffService.get_unchanged_files()` reads both trees exactly like the revision diff (`_fetch_trees`: Git trees, or the database for both sides) and returns the sorted paths whose content key is equal on both sides (~0.7 s cold at v3.0→v3.1, then served from the tree LRU cache).
- **Response**:
```json
{ "v1": "v3.0", "v2": "v3.1", "source": "git", "count": 28828, "files": ["COPYING", "Documentation/.gitignore"] }
```

#### `GET /api/tools/diff/renames`
Exhaustive rename search: compares every deleted with every added file between two revisions (Git trees only), optionally under one directory.
- **Parameters**:
  - `version_a` (query, required): Base revision.
  - `version_b` (query, required): Target revision.
  - `path` (query): Directory (or file) the search is limited to on both sides (validated with `is_safe_rel_path`, passed as a `:(literal)` pathspec); empty = whole tree.
  - `similarity` (query, 1–100, default 50): Minimum content similarity in percent.
- **Behavior**: `DiffService.search_renames()` counts the added and deleted paths under `path` and the candidate pairs (`added × deleted` after identical copies, which Git pairs without comparing). Above `MAX_RENAME_SEARCH_PAIRS` (60,000,000) nothing runs and `reason` is `"too_many"`, so the caller narrows `path`. Otherwise `git diff-tree -M<similarity>% -l<max(added, deleted)+1>` compares every pair (110 s timeout; `reason: "failed"` when Git fails). `complete` is true when every pair was compared. Only pairs of a path deleted and a path added under `path` are returned, so a rename whose other end lies outside `path` is not found by a scoped search. Timings: v3.0→v3.1 whole tree (811,530 pairs) ~0.9 s; v3.0→v4.0 `drivers/net` (1,857,397 pairs, 804 renames at 30%) ~2.6 s; v3.0→v4.0 whole tree is 202 million pairs (~60 s for Git), so it is refused. Low thresholds pair unrelated files that share boilerplate (license headers).
- **Response**:
```json
{
  "v1": "v3.0",
  "v2": "v3.1",
  "path": "Documentation",
  "similarity": 40,
  "candidates": { "added": 213, "deleted": 168, "pairs": 9796 },
  "max_pairs": 60000000,
  "complete": true,
  "reason": "",
  "renames": [
    { "old_file": "Documentation/DocBook/dvb/.gitignore", "file": "Documentation/DocBook/media/dvb/.gitignore", "similarity": 100, "fid": 422662 }
  ]
}
```

#### `GET /api/tools/diff/file`
Unified Git diff of a single file between two revisions.
- **Parameters**:
  - `version_a` (query, required): Base revision.
  - `version_b` (query, required): Target revision.
  - `path` (query, required): Repository-relative file path (validated with `is_safe_rel_path`).
  - `old_path` (query): Base-side path for renamed files.
  - `similarity` (query, 1–100, default 50): Rename threshold of the diff, so a pair found below 50% (rename search) shows as one rename diff instead of a deletion and an addition.
- **Behavior**: Both revisions are resolved with `git rev-parse --verify` (option-like names are rejected) and diffed with `git diff -M<similarity>%`; both paths are passed as `:(literal)` pathspecs. Output above 512 KiB is truncated (`truncated: true`).
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

#### `GET /api/tools/diff/blobs`
Changed lines between **any two files at any two revisions** (the code view's **⇄ Diff**, §5.21): the same path across releases, a renamed file, or two unrelated files.
- **Parameters**:
  - `version_a`, `path_a` (query, required): Base revision and repository-relative path.
  - `version_b`, `path_b` (query, required): Target revision and path.
- **Behavior**: `DiffService.get_blob_diff()` validates both paths (`is_safe_rel_path`; else `400`), resolves both revisions (`resolve_commit_sha`; unknown or option-like → `404`) and looks each path up with `git ls-tree -z <sha> -- :(literal)<path>` (absent → `404`, a directory → `400`). Equal blob SHAs answer `identical: true` with no hunks. Otherwise `git diff --no-color --no-ext-diff -U0 <blob_a> <blob_b>` runs on the two object SHAs (nothing user-supplied reaches the command line); stdout is streamed and cut at 8 MiB, at most 200,000 rows are kept and rows are **not** clipped (`parse_unified_diff(..., max_chars=None)`), so `truncated: true` means the hunks stop early. Only changed lines are returned: the client rebuilds the full file from the side it already shows (§5.21). `binary` is set when Git reports binary content.
- **Rows**: `[kind, old_line, new_line, text]` as in the commit diff (`"+"` / `"-"`; the other side is `null`). Text keeps a trailing `\r` of CRLF files.
- **Response**:
```json
{
  "v1": "v3.1-rc10", "v2": "v3.1", "path_a": "Makefile", "path_b": "Makefile",
  "blob_a": "…", "blob_b": "…", "identical": false, "binary": false,
  "additions": 1, "deletions": 1, "truncated": false,
  "hunks": [
    { "old_start": 4, "old_count": 1, "new_start": 4, "new_count": 1,
      "lines": [["-", 4, null, "EXTRAVERSION = -rc10"], ["+", null, 4, "EXTRAVERSION ="]] }
  ]
}
```

#### `GET /api/diff/kconfig/{v1}/{v2}`
Compare Kconfig source files and option definitions between two revisions (release tags, branches, or commit SHAs).
- **Parameters**:
  - `v1` (path): Base revision (e.g. `"v3.0"`).
  - `v2` (path): Target revision (e.g. `"v3.1"`).
- **Behavior**: See §2.8 Level 3. Both `files` and `symbols` come from Git and are complete; `unindexed_files` is always empty (kept for compatibility). Each definition in `before`/`after` carries `file`, `line`, `type` (1 bool, 2 tristate, 3 string, 4 hex, 5 int), `prompt` (text without its condition), `def_val` (first default) and `facts` (the property record of the symbol history, `GET /api/kconfig/{version_name}/symbol/{symbol_name}/history`); a symbol with one definition on each side also gets `changes` (`[{field, before, after}]` / `[{field, added, removed, before, after}]`). Measured: v3.0 -> v3.1 takes ~2 s (174 modified files), v6.17 -> v6.18 ~3 s.
- **Response**:
```json
{
  "v1": "v3.0",
  "v2": "v3.1",
  "summary": {
    "added": 221,
    "removed": 110,
    "changed": 343,
    "files_added": 12,
    "files_removed": 11,
    "files_modified": 174
  },
  "added": ["..."],
  "removed": ["A2232", "..."],
  "changed": ["ACPI_APEI_GHES", "..."],
  "symbols": [
    {
      "name": "ACPI_APEI_GHES",
      "change": "changed",
      "before": [ { "file": "drivers/acpi/apei/Kconfig", "line": 13, "type": 1, "prompt": "APEI Generic Hardware Error Source", "def_val": "", "facts": { "kind": "config", "type": "bool", "...": "..." } } ],
      "after": [ { "file": "drivers/acpi/apei/Kconfig", "line": 13, "type": 1, "prompt": "APEI Generic Hardware Error Source", "def_val": "", "facts": { "...": "..." } } ],
      "changes": [ { "field": "selects", "added": ["IRQ_WORK"], "removed": [], "before": ["..."], "after": ["..."] } ]
    }
  ],
  "files": [ { "file": "drivers/staging/tty/Kconfig", "change": "removed" } ],
  "unindexed_files": []
}
```

---

### 4.8. Linux Kernel Mailing List (LKML) & Public-Inbox API

The Mailing List subsystem connects the web application to the public-inbox / LKML archive (via direct storage read or the lightweight remote micro-daemon over SSH tunnel), providing thread inspection, unified patch diff parsing, full-text search, and offline IndexedDB fallback.

#### `GET /api/mail/status`
Check daemon status, reachability, active port, and the base URL of the self-hosted public-inbox mirror (`lore.kernelinfo.org`, a clone of `lore.kernel.org`).
- **Response**:
```json
{
  "enabled": true,
  "available": true,
  "port": 8192,
  "lore_url": "https://lore.kernelinfo.org/all/",
  "reason": null
}
```

#### `GET /api/mail/message`
Retrieve full email details including RFC-822 headers, body text, raw payload, and pre-parsed interactive patch diffs.
- **Parameters**:
  - `mid` (query): RFC-822 Message-ID (e.g. `"<20210515120000.1234-1-dev@kernel.org>"` or `"20210515120000.1234-1-dev@kernel.org"`). Automatically supports URL decoding, angle-bracket removal, space-to-plus recovery (`+` converted to space in query strings), and direct `xref3` binary OID fallback.
  The service also accepts a sequential article number in `mid`.
- **In-Memory LRU Cache**: 512 entries cached in-memory in `MailingListService._email_cache`. Stale or error entries are automatically bypassed.
- **Client Cache**: `api.getMailMessage` operates `networkFirst: true`. IndexedDB never caches failed or empty email responses.
- **Response**:
```json
{
  "available": true,
  "num": 421890,
  "message_id": "20210515120000.1234-1-dev@kernel.org",
  "subject": "[PATCH v2 1/3] fs/ext4: fix use-after-free in ext4_ext_remove_space",
  "from_name": "Developer Name",
  "from_email": "dev@kernel.org",
  "author": "Developer Name <dev@kernel.org>",
  "date": 1621080000,
  "date_iso": "2021-05-15 12:00:00",
  "tid": 87654,
  "headers": {
    "from": "Developer Name <dev@kernel.org>",
    "to": "linux-ext4@vger.kernel.org",
    "subject": "[PATCH v2 1/3] fs/ext4: fix use-after-free in ext4_ext_remove_space",
    "date": "Sat, 15 May 2021 12:00:00 +0000",
    "message-id": "<20210515120000.1234-1-dev@kernel.org>"
  },
  "body": "Commit message body text...\n\nSigned-off-by: Developer Name <dev@kernel.org>\n---\n...",
  "raw": "From: Developer Name <dev@kernel.org>\nTo: linux-ext4@vger.kernel.org\n...",
  "is_patch": true,
  "is_cover": false,
  "is_pull": false,
  "patch_version": 2,
  "patch_idx": 1,
  "patch_total": 3,
  "diff": {
    "additions": 14,
    "deletions": 3,
    "files": [
      {
        "path": "fs/ext4/extents.c",
        "old_path": "fs/ext4/extents.c",
        "status": "M",
        "additions": 14,
        "deletions": 3,
        "binary": false,
        "hunks": [
          {
            "old_start": 2840,
            "old_count": 7,
            "new_start": 2840,
            "new_count": 18,
            "heading": "ext4_ext_remove_space",
            "lines": [
              [" ", 2840, 2840, " static int ext4_ext_remove_space(struct inode *inode)"],
              ["-", 2841, null, "     ext4_free_extents(inode);"],
              ["+", null, 2841, "     if (ext4_valid_extent(inode))"],
              ["+", null, 2842, "         ext4_free_extents(inode);"]
            ]
          }
        ]
      }
    ]
  },
  "lore_url": "https://lore.kernelinfo.org/all/20210515120000.1234-1-dev@kernel.org/"
}
```

#### `GET /api/mail/thread`
Retrieve a discussion thread's messages ordered chronologically by sequential number or date.
- **Parameters**:
  - `tid` (query): Thread integer identifier.
  - `mid` (query): Optional Message-ID hint used to resolve `tid` automatically if `tid` is omitted or 0.
- **Bounded page**: a thread is unbounded in principle (the reference archive's largest holds 12,342 messages, each carrying a compressed overview blob), so the archive store caps the messages unpacked per request at `MAX_THREAD_MESSAGES` (1000); the endpoint answers that first page. `total` is the thread's real message count from an indexed count, and `truncated` states whether messages beyond the returned page remain — so a client must read `total` as the thread size and `messages` as a page of it, not as the whole thread.
- **In-Memory LRU Cache**: 128 entries cached in `MailingListService._thread_cache`, each reused for `LIVE_CACHE_TTL` (60 s) only: the archive is fetched continuously and a thread gains replies.
- **Response**:
```json
{
  "available": true,
  "tid": 87654,
  "subject": "[PATCH v2 0/3] fs/ext4: extent tree cleanup & fix",
  "total": 4,
  "limit": 1000,
  "offset": 0,
  "truncated": false,
  "participants": [
    "Developer Name <dev@kernel.org>",
    "Theodore Ts'o <tytso@mit.edu>"
  ],
  "messages": [
    {
      "num": 421889,
      "message_id": "20210515115900.1234-0-dev@kernel.org",
      "tid": 87654,
      "subject": "[PATCH v2 0/3] fs/ext4: extent tree cleanup & fix",
      "author": "Developer Name <dev@kernel.org>",
      "date": 1621079940,
      "date_iso": "2021-05-15 11:59:00",
      "is_patch": false,
      "is_cover": true,
      "is_pull": false,
      "patch_version": 2,
      "patch_idx": 0,
      "patch_total": 3
    },
    {
      "num": 421890,
      "message_id": "20210515120000.1234-1-dev@kernel.org",
      "tid": 87654,
      "subject": "[PATCH v2 1/3] fs/ext4: fix use-after-free in ext4_ext_remove_space",
      "author": "Developer Name <dev@kernel.org>",
      "date": 1621080000,
      "date_iso": "2021-05-15 12:00:00",
      "is_patch": true,
      "is_cover": false,
      "is_pull": false,
      "patch_version": 2,
      "patch_idx": 1,
      "patch_total": 3
    }
  ],
  "lore_url": "https://lore.kernelinfo.org/all/t/87654"
}
```

#### `GET /api/mail/versions`
List every revision (`v1`, `v2`, ...) of the patch series a thread belongs to. Revisions are normally posted as separate threads sharing one base subject, so the series is reconstructed by normalizing the requested thread's subject (`normalize_series_subject`: reply prefixes and leading `[PATCH v3 2/5]` tags removed) and matching that base subject against the archive search index. At most `MAX_VERSION_THREADS` (8) sibling threads are expanded.
- **Parameters**:
  - `tid` (query): Thread integer identifier.
  - `mid` (query): Optional Message-ID, used both to resolve `tid` and to pin `current_version` when a single thread carries messages of more than one revision.
- **Grouping**: every message of each thread is folded into the entry of its own revision (`patch_version`); a reply carrying no patch tag inherits the revision of the earliest series message in its thread. `total` counts all messages of the revision, `patches` only the patches, cover letters and pull requests.
- **In-Memory LRU Cache**: keyed by `tid:mid`, bounded by the thread cache size (128 entries) in `MailingListService._version_cache`, reused for `LIVE_CACHE_TTL` (60 s): a new revision of the series is a new thread.
- **Response**:
```json
{
  "available": true,
  "tid": 87654,
  "base_subject": "fs/ext4: extent tree cleanup & fix",
  "current_version": 2,
  "total_versions": 2,
  "versions": [
    {
      "version": 1,
      "tid": 87001,
      "subject": "[PATCH 0/3] fs/ext4: extent tree cleanup & fix",
      "root_message_id": "20210501120000.99-0-dev@kernel.org",
      "cover_message_id": "20210501120000.99-0-dev@kernel.org",
      "author": "Developer Name <dev@kernel.org>",
      "timestamp": 1619870400,
      "date_iso": "2021-05-01 12:00:00",
      "total": 6,
      "patches": 4,
      "is_current": false
    },
    {
      "version": 2,
      "tid": 87654,
      "subject": "[PATCH v2 0/3] fs/ext4: extent tree cleanup & fix",
      "root_message_id": "20210515115900.1234-0-dev@kernel.org",
      "cover_message_id": "20210515115900.1234-0-dev@kernel.org",
      "author": "Developer Name <dev@kernel.org>",
      "timestamp": 1621079940,
      "date_iso": "2021-05-15 11:59:00",
      "total": 4,
      "patches": 4,
      "is_current": true
    }
  ],
  "lore_url": "https://lore.kernelinfo.org/all/?q=fs/ext4%3A%20extent%20tree%20cleanup%20%26%20fix"
}
```

#### `GET /api/mail/search`
Full-text search across public-inbox mail archive with Boolean and term prefixes.
- **Parameters**:
  - `q` (query): Search terms or prefixes (e.g. `s:ext4`, `f:torvalds`, `m:20210515`).
  - `page` (query): 1-indexed page number (default 1).
  - `limit` (query): Items per page (1-100, default 50).
  - `filter` (query): Filtering preset: `"all"`, `"patch"`, `"cover"`, `"pull"`.
- **Future-Dated Messages**: an empty `q` is the chronological browse listing (`ORDER BY ds DESC`), where a message carrying a `Date:` header in the future — a skewed sender clock or a forged header — would sort ahead of every real email. Those rows are therefore excluded from the empty-query listing by the archive store (`PublicInboxLocalStore.search_emails`, `ds <= now + FUTURE_DATE_SKEW_SECONDS`, a one-hour tolerance), and `MailingListService._drop_future_dated` re-applies the same horizon to the returned page so the invariant also holds against a micro-daemon predating it (lowering `total` by what the page dropped). **A non-empty `q` is never filtered**: a search that matches a future-dated message still returns it, and the filter presets apply unchanged in both cases.
- **Reported `total`**: where the Xapian bindings are available, `total` is Xapian's estimated match count (`get_matches_estimated()`) for the whole archive. Otherwise it is a lower bound — the offset plus the returned page, plus one when more remain — and the response always carries `has_more`; the frontend roster also treats a page that adds no new message as the end of the results (§5.22).
- **Paging cost is flat**: `limit` and `offset` are clamped by the archive store (`MAX_SEARCH_LIMIT` 200, `MAX_SEARCH_OFFSET` 5,000,000) and the offset is applied by Xapian or by SQL `LIMIT ? OFFSET ?` rather than by slicing a fetched list, so a deep page costs what the first one does. Materializing `limit + offset` records previously cost 3.1GB and 43s at offset 2,000,000 on a 22M-message archive — enough to have the archive daemon OOM-killed — against 33MB and 0.16s now. Without Xapian, the subject-substring fallback streams a bounded scan (`MAX_SEARCH_SCAN` 20,000 rows) and keeps only the requested page.
- **In-Memory LRU Cache**: 64 entries cached in `MailingListService._search_cache`, reused for `LIVE_CACHE_TTL` (60 s), so the browse listing shows new mail within a minute. The client asks network-first and keeps only the browse listing (no query) in IndexedDB for offline use (§5.1).
- **Lore links**: `lore_url` of a search carries the query URL-encoded (`quote_plus`), as does a commit's fallback link built from its subject.
- **Response**:
```json
{
  "available": true,
  "query": "ext4",
  "filter_type": "patch",
  "page": 1,
  "limit": 50,
  "offset": 0,
  "total": 142,
  "has_more": true,
  "results": [
    {
      "num": 421890,
      "message_id": "20210515120000.1234-1-dev@kernel.org",
      "tid": 87654,
      "subject": "[PATCH v2 1/3] fs/ext4: fix use-after-free in ext4_ext_remove_space",
      "author": "Developer Name <dev@kernel.org>",
      "date": 1621080000,
      "date_iso": "2021-05-15 12:00:00",
      "is_patch": true,
      "is_cover": false,
      "is_pull": false,
      "patch_version": 2,
      "patch_idx": 1,
      "patch_total": 3
    }
  ]
}
```

---

## 5. Frontend Client Architecture & Module Specifications

The frontend is a vanilla ES-Module single-page application requiring zero node/npm build steps.

### 5.0. Interaction Invariant: No Unrequested Change Without Confirmation
**Any action that would change more than what the user directly asked for must be reported and confirmed before it is applied.** This holds for every view, and for the configuration state in particular (`state.kconfigProfiles`, `state.kconfigEngines`, the KConfig engines): the user's click is a mandate for the change they made, never for its side effects. A new interaction that can alter values, options or state the user did not name is implemented as *compute the full consequence first (on a throwaway engine or a dry run), show it (what changes, from which value to which, and why), apply it only on explicit confirmation, and leave everything untouched on Close/Escape*. Confirmation is skipped only when the consequence is empty — nothing else changes — and the dialog must then not appear at all. Existing instances of the pattern: lowering a value that other options need (§5.8, `previewLowered`), enabling the constraint engine over edited values (§5.8, `resolutionDiff`), applying a toolchain profile that resolves values differently (§5.8, `reloadModel`), applying a revision to a KConfig tab (§5.13, `requestVersionSwitch`: its values are carried to the target revision, resolved against that model, and the configuration saved for the revision it leaves is dropped), *Apply kconfig resolution* in **Validate** (§5.8, the change the user asked for, itemized before it is applied). Auto-solve is the documented exception: its changes *are* the requested action and are listed afterwards. Undo/redo in a KConfig tab (§5.8, `Ctrl+Z`) is not an instance either: it puts back exactly the state the tab had before (or after) the change being undone, which is the requested action itself. Toasts report what happened; they never replace a confirmation.

```
webapp/frontend/js/
├── api.js                 # ApiClient: centralized REST fetch client with offline fallbacks
├── sync.js                # SyncManager: server change tokens, version stamps, new-build reload
├── db.js                  # LocalDatabase: IndexedDB offline cache manager
├── state.js               # StateStore: tabs, selected vs. explored revision, KConfig engines, persistence
├── url_sync.js            # UrlSync: bi-directional hash router
├── app.js                 # Application bootstrap orchestrator
├── components/
│   ├── tabs.js            # Multi-pane split manager (single, horizontal, vertical); per-tab view instances
│   ├── context_menu.js    # Context-adaptive menus (CodeView, Explorer, NodeMap, Commits)
│   ├── command_palette.js # Ctrl+K search (files, symbols, Kconfig, commits) & Ctrl+Shift+P commands
│   ├── modal.js           # Shared modal dialog (`onClose` callback, e.g. to resolve a confirmation) + download helper
│   ├── patch_modal.js     # In-browser patch staging (edit → git format-patch)
│   ├── symbol_history_modal.js # Release slider over the history of a definition or Kconfig symbol
│   ├── ref_picker.js      # Git reference picker popover (release tags + searchable commits)
│   ├── version_switch.js  # Apply / New Tab: guarded revision switch of the active tab
│   ├── scroll_memory.js   # Per-tab scroll positions of every scrollable element, kept in the session
│   └── toast.js           # Ephemeral status toasts (success / error / info / warning); #toast-container is always the topmost layer (z-index 2147483647), above every modal/backdrop
├── utils/
│   ├── clipboard.js       # Universal fallback clipboard copier (navigator + execCommand)
│   ├── debounce.js        # Function debouncer
│   ├── definition_picker.js # Go to Definition: the definition the active ARCH/configuration compiles
│   └── icons.js           # Shared inline SVG icons (COLLAPSE_ICON: default icon of every collapse control)
└── views/
    ├── code_view/
    │   ├── virtual_editor.js     # Virtualized line scroller with minimap (one per code tab)
    │   ├── ast_overlay.js        # Disjoint narrowest-interval AST token mapping & syntax colors
    │   ├── syntax/               # Modular language syntax grammars & detection registry
    │   │   ├── registry.js       # Grammar detector (ftype, extension, filename, shebang)
    │   │   └── languages/        # Declarative language lexers (c, rust, python, shell, perl, asm, etc.)
    │   ├── fidelity_clipboard.js # 100% copy fidelity raw line buffer extractor
    │   ├── file_diff.js          # "⇄ Diff": full-file diff rows, side-by-side pairing, file/revision picker
    │   └── blame_view.js         # Interactive git blame annotations
    ├── nodemap/
    │   ├── nodemap_controller.js # Schematic canvas controller (DOM nodes, SVG wires), zoom/pan, export
    │   ├── node_renderer.js      # Real-time dragging DOM renderer
    │   ├── edge_router.js        # Cubic bezier spline router
    │   └── node_model.js         # Node and wire data structures
    ├── kconfig/
    │   ├── kconfig_view.js       # Web menuconfig tree, symbol inspector, per-ARCH configurations
    │   ├── kconfig_engine.js     # scripts/kconfig port: values, legal edits, .config/defconfig I/O
    │   ├── kconfig_parser.js     # .config reader & raw (engine-off) writer
    │   ├── menuconfig_tui.js     # mconf-style terminal menuconfig
    │   ├── kconfig_history.js    # Undo/redo of a KConfig tab's configuration (diffs between states)
    │   ├── directive_evaluator.js# #ifdef conditional code dimming (+ ARCH compiler macros)
    │   └── build_condition.js    # Kbuild build-condition evaluation & explanation
    ├── explorer/explorer_view.js  # Full-tab repository explorer (tab type `explorer`)
    ├── maintainers/maintainers_view.js
    ├── commits/commits_view.js
    ├── diff/diff_view.js         # Compare view: files & Kconfig between any two tags/commits
    ├── treemap/treemap_view.js   # Squarified source treemap (bytes / files)
    └── docs/
        ├── md_renderer.js        # GitHub Flavored Markdown (GFM) renderer (alerts, tables, task lists, code blocks)
        ├── rst_renderer.js       # reStructuredText / legacy plain-text → escaped HTML renderer
        └── doc_page.js           # kernel.org-style documentation page (sidebar, TOC, quick search)
```

### 5.1. `ApiClient` (`webapp/frontend/js/api.js`)
Handles unified HTTP communication, response caching, offline fallback, and network activity tracking:
- **Global Loading Indicator & Anti-Flicker Lifecycle**:
  - Replaces legacy static status bar with a sleek, floating circular loading spinner in the bottom-right viewport (`#global-loading-spinner`).
  - Automatically tracks in-flight `fetch` requests via `this.activeRequests`.
  - Employs a 150ms anti-flicker debounce timer so sub-150ms and IndexedDB cache hits never flash the UI.
  - When network fetch duration exceeds 150ms, fades in the glowing spinning indicator; smoothly fades out when all requests finish.
- **Server reachability (`isOnline`, `serverUp`, `isConnected`)**: `navigator.onLine` only reports the browser's link state and stays `true` while the backend is down, so the client tracks the backend separately. `serverUp` is set from every exchange: any HTTP status proves the server answered (a `500` still counts), while a transport failure, the request timeout (10 s, raised per request with `timeoutMs` for the few endpoints that compute — `getBranchTags()` asks for 180 s, commit endpoints ask for 45 s), or a reply carrying `X-KIP-Offline` (served from the Service Worker cache, §2.11) means it did not. `getSync()` marks the same way. A change dispatches `app:server-status` (`{up}`) on `document`; `isConnected` is `isOnline && serverUp` (`isOnline` follows the browser's `online`/`offline` events); the settings panel's connection status reads `isConnected` and `isOnline`.
- **Heartbeat (`startHeartbeat()`, `stopHeartbeat()`, `pingServer()`)**: `pingServer()` requests `GET /api/sync` (cheap, server-cached, never intercepted by the worker) with a 5 s timeout and records reachability without throwing or touching the loading spinner. The heartbeat polls every **30 s** while the server answers and every **5 s** while it does not, skips hidden tabs, and re-paces itself on each state change. A probe also runs when the tab becomes visible and on the browser's `online` event. `app.js` starts it from `setupNetworkStatus()`.
- `getVersions()`: Fetches release list (always from the network, `networkFirst`) and keeps the answer in memory per repository.
- `getVersionsCached()`: That in-memory catalog — refreshed by every `getVersions()` call (startup, each sync refresh, §2.11) — fetched only when this page has none yet. Used where a lookup must not wait on `/api/versions`: the **⇄ Diff** picker's default revision (`previousTag`), the ref picker's tag list (§5.13) and the Compare view's default base.
- `getSync({fresh})`: Server change tokens (`GET /api/sync`, never cached; `fresh` adds `?fresh=true`); `onSyncHeader` receives the `X-KIP-Sync` header of every response; `versionStamps` / `_vk(version)` stamp version-scoped cache keys (§2.11).
- `getDirectoryTree(version, path)`: Direct children of a directory.
- `getFileContent(version, path)`: Retrieves raw text and AST tokens.
- `getTreemap(version, maxDepth, path)`: Retrieves hierarchical squarified codebase treemap.
- `getCompileCommandsDownloadUrl(version, arch)`: URL of the `compile_commands.json` download (`GET /api/fs/compile_commands/{v}/export`).
- `resolveInclude(version, header, astId, currentFile, arch)`: Resolves `#include` directive targets for `arch` (default `state.definitionContext().arch`, part of the cache key).
- `getIncludeSymbols(version, astId, options)`: Fetches imported symbols for an include, supporting fallback lookup options (`filePath`, `line`, `header`, `tagId`).
- `getSymbolDetail(version, name, arch, fromFile)` / `getSymbolXref(version, name, limit, arch)`: Symbol inspection; `arch` defaults to `state.definitionContext().arch` and is part of the cache key, as is `fromFile` (the file the symbol is used in, §2.7 item 4).
- `getBuildConditions(version, paths, arch)`: Kbuild build conditions of files (`GET /api/kconfig/{v}/build`).
- `searchSymbols(version, query, limit)`: Symbol definitions by prefix (Ctrl+K palette).
- `getSymbolHistory(version, path, line, astId, { name, includeRc, branch })`: Release-by-release history of a definition on one branch (sends the definition ARCH; long timeout, since the first history of a branch walks its tags).
- `getKconfigHistory(version, name, { includeRc, branch })`: Release-by-release history of a Kconfig symbol on one branch, with property-level changes.
- `getHistoryCommits({ path, to, from, lineS, lineE })`: Commits behind one history change (`git log -L`).
- `getKconfigGraph(version, arch, symbol, toolchain)`: Direct relations (edges) of one Kconfig symbol.
- `getKconfigDiff(versionA, versionB)`: Cross-release KConfig symbol definition and default differences.
- `formatPatch(version, payload)`: Generates RFC-2822 standard git format patch from content edits.
- `getMaintainersOverview(version, query)` / `getMaintainerSection(version, secId)`: Subsystems.
- `getDevelopers(version, options)`: Full kernel persona directory with role filtering and activity/alpha sorting.
- `getPersonProfile(version, idOrEmail)`: Developer profiles.
- `getCallgraph(version, functionName)`: Callers and callees (60 s timeout: the callers of a widely used function take seconds, §4.7).
- `searchMailingList(query, { page, limit, filterType })`: Mailing list search, always network-first (the archive grows while it is fetched); only the browse listing (empty query) is kept in IndexedDB for offline use, never the queries typed in the palette.
- `_sanitizeVer(v)`: falls back to `v3.0` for an empty value or a stringified object (`"[object …"`) only; any other name, a branch containing "object" included, is sent as is.

### 5.2. `AstOverlay` (`webapp/frontend/js/views/code_view/ast_overlay.js`)
Implements two-layer hybrid highlighting with map prioritization, multi-language grammar dispatch, and client-side fallback:
1. **Multi-Language Detection & Grammar Dispatch (`setFileInfo`, `syntax/registry.js`)**:
   - Dispatches language grammars from `webapp/frontend/js/views/code_view/syntax/registry.js` using a 4-tier detection cascade:
     1. Database `ftype` identifiers (`ftype === 1` for C, `2` for Kconfig, `3` for Rust, `4` for Assembly).
     2. Exact filename matching (e.g. `Makefile`, `Kbuild`, `Kconfig`).
     3. Canonical file extension mapping (`.rs`, `.py`, `.sh`/`.bash`, `.pl`/`.pm`, `.S`/`.s`, `.mk`, `.dts`/`.dtsi`, `.yaml`/`.yml`, `.c`/`.h`/`.i`).
     4. First-line shebang inspection (`#!/bin/sh`, `#!/usr/bin/env python`, `#!/usr/bin/perl`) for extensionless kernel scripts.
   - Modular grammar definitions (`webapp/frontend/js/views/code_view/syntax/languages/`) encapsulate keywords, standard/kernel types, comments, string delimiters, and directive rules for each language.
2. **Dedicated Token Classes & Visual Fidelity (`codeview.css`)**:
   - Reuses core styling tokens (`.tok-keyword`, `.tok-type`, `.tok-string`, `.tok-number`, `.tok-comment`, `.tok-preproc`, `.tok-macro`, `.tok-include`, `.tok-punct`, `.tok-function`, `.tok-identifier`).
   - Introduces dedicated language styling classes:
     - `.tok-variable` (`#ffa657`): Shell and Perl variables (`$VAR`, `${VAR}`, `$@`, `%hash`, `@arr`), Makefile expansions (`$(CC)`, `$<`), and Assembly registers (`%rax`).
     - `.tok-attribute` (`#7ee787`): Rust outer/inner attributes (`#[inline]`, `#![no_std]`) and Python decorators (`@classmethod`).
     - `.tok-lifetime` (`#ffa657` italic): Rust lifetime parameters (`'a`, `'static`).
3. **Map Prioritization & Narrowest-Interval Disjoint Token Mapping**:
   - Sorts spatial map tokens by width descending (`(char_e - char_s)`).
   - Assigns character intervals to an array `charToken[i]`, ensuring narrow inner tokens (declarators, symbol references, variables) overwrite and take precedence over enclosing multi-line scopes without shadowing.
   - When specific AST tokens exist for a line, prioritizes map information (`renderWithAstTokens`).
   - Maps Rust AST node types (ASTT values 123–143) to corresponding CSS classes (`Rust_Struct`, `Rust_Enum`, `Rust_Union`, `Rust_Trait` -> `tok-type ast-struct`; `Rust_Fn` -> `tok-function ast-func`; `Rust_MacroDef`, `Rust_MacroCall` -> `tok-macro ast-macro`; `Rust_Attribute` -> `tok-attribute ast-attribute`; comments -> `tok-comment`).
   - Filters broad multi-line container envelopes across C and Rust (C containers 1, 21, 69–72; Rust `Rust_Impl` 132, `Rust_Mod` 136, `Rust_ForeignMod` 140) to prevent enclosing blocks from suppressing line-level tokenization.
4. **Client-Side Lexical Fallback & Multi-Line Interval Scanning (`indexMultiLineSpans`, `renderLexical`)**:
   - On lines without server AST tokens or unparsed files, falls back to the client-side language lexer.
   - Lightweight $O(N)$ single-pass scan (`indexMultiLineSpans`) on file load indexes multi-line block comments (`/* ... */`) and Python triple-quoted docstrings (`"""..."""` / `'''...'''`), ensuring correct styling across virtual scrolling jumps without line-prefix heuristics.
   - Identifiers, types, and function calls across all supported languages receive interactive `.ast-token` badges with `data-name="..."`, enabling full right-click context menu capabilities (Go to Definition, Find References, Symbol Palette search).
5. **Include Directive Tokenization (`renderIncludeLine`)**:
   - Reliably isolates `#include ` directives across C, Assembly, and DeviceTree source files.
   - Renders `#include ` as non-clickable `<span class="tok-preproc">` and the header target (`<linux/...>` or `"header.h"`) as `<span class="tok-include ast-token ast-include">`.
   - Binds server AST ID and type (`data-type-id="78"`) when mapped, while preserving interactive click resolution for all unmapped includes.

### 5.3. `NodeRenderer` & Drag Controller (`webapp/frontend/js/views/nodemap/`)
- Canvas pan and zoom via CSS transforms of the node layer; wires are SVG paths (`edge_router.js`).
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
- **Offline Cache Stores**: Persistent IndexedDB (`KernelInfoIDE_v2`, v5) caching `meta`, `files`, `symbols`, `kconfigs`, `maintainers`, `commits`, `tools`, `mail`, `tabs_state`, and `nodemap_projects`. Each data store has an `updated_at` index (v5): once a day, when the page opens, `syncManager.pruneOldEntries()` drops the entries written more than 30 days ago (`localDB.pruneOlderThan`, a key-only index scan), which is what bounds the offline cache. A change of `CACHE_LAYOUT` (`sync.js`, now 3: several response shapes changed) clears the data stores once.
- **Upgrades with other windows open**: an open connection closes itself on `versionchange`, so a newer page's upgrade is not blocked by an older window; a page whose open is still blocked after 3 s (`BLOCKED_OPEN_TIMEOUT_MS`) runs uncached instead of waiting (every cached `api.request()` would otherwise wait on `localDB.ready` forever).
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
- **Connection Badge (`#offline-badge`, `app.setupNetworkStatus()`)**: three states, driven by the browser link state **and** by whether the backend actually answers (`api.serverUp`, §5.1), refreshed on `online`/`offline` and on `app:server-status`:
  | State | Badge | Class | Meaning |
  |---|---|---|---|
  | Connected | `● Online` | `.offline-badge` | Link up and the server answered |
  | Backend down | `● Server Unreachable` | `.offline-badge.unreachable` (amber) | Link up, but requests and the heartbeat get no reply; cached data is shown |
  | Link down | `● Offline (Cached)` | `.offline-badge.offline` (red) | `navigator.onLine` is false |
  Each transition toasts once (a warning when the server drops, a success when it comes back); re-renders of the same state are silent, and an open settings popover is rebuilt so it reports the same state.
- **Revamped Settings Popover (`#offline-badge`)**:
  - Clicking the top-right connection badge opens an anchored settings popover dropdown.
  - **Database & Network**: Real-time connection status (the badge's three states: *Online (Connected)* / *Server Unreachable* / *Offline (Cached)*) alongside the verified DB Instance Hash badge with synchronization indicator (`● Synced`).
  - **🔄 Check for Updates** (`app.checkForUpdates()`, disabled unless connected — the button reads *Offline* or *Unreachable*): runs `syncManager.checkNow()` (§2.11) and shows *Checking… / Up to date / Updated / Failed* on the button, with a toast listing what changed (new frontend build, database rebuilt, versions re-parsed or moved, new or removed versions/branches).
  - **Typography & Font Sizes**:
    - **Code View Font Size**: Slider in pixels (10px to 26px, default 12px) dynamically setting `--code-font-size` and computing proportional row height `--code-row-height = Math.max(16, Math.round(fontSize * 1.667))` in `virtual_editor.js`, diffs, and code views.
    - **UI Text Scale**: Relative percentage slider (80% to 160%, default 100%) adjusting `--ui-font-scale` so discrete font sizes (`8px` through `24px`) across tabs, mailing lists, commits, maintainers, kconfig, sidebars, tree rows, badges, and modals scale proportionately without breaking layouts.
    - **Reset to Defaults**: Restores 12px code view and 100% UI text scale reliably across multiple uses.
    - **Persistence**: Saved in `localStorage` (`kip-code-font-size`, `kip-ui-font-scale`) and restored automatically on application initialization.
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
  - Only definition cards with a path of the current revision are rendered (a definition outside its tree can be neither shown nor jumped to), and `definition_unindexed` replaces the empty-state line with *"Defined in other indexed revisions, but this revision's copy of the file was never parsed — the usages below still apply"* (common on a commit between two ingested releases).
- **Commit Hash Menu** (`showCommitHashMenu`): right-clicking any commit hash (`.copy-hash`, §5.12) opens **Copy Hash** (the full 40-character SHA, the same action as left-click), **View Commit** and **Set as Work Commit**. *View Commit* calls the `onView(hash)` callback first — a view that already lists commits inspects it in place — and opens a `Commit <short>` tab when that returns false (a parent commit outside the loaded range). *Set as Work Commit* only moves the header selection (`state.setSelectedVersion`, §5.13), so no tab changes revision until **Apply** or **New Tab** is pressed; the item is disabled and reads *Already the Work Commit* when the hash is the current selection.
- **Tabstrip Tab Menu** (`showTabMenu`, `showTabPushMenu`): right-clicking a tab offers **Open on New Browser Tab** and **Push to Another Session…**, the second opening a list of every other saved session marked `🟢 open` (held by another window) or `💤 saved`. The menu only lays the actions out — `TabsComponent` performs them, since it owns tab transfers (§5.24.5).
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
- **Engine**: `KconfigEngine` decodes the model of `GET /api/kconfig/{v}/tree` (`decodeModel`) and evaluates it with kconfig's algorithms (twin of `parser/kconfig_ast/kconfig_eval.py`): `value`, `visibility`, `revDep`, `getType` (tristate degrades to bool without `MODULES`), `choiceSelection`, `isChangeable`, `menuVisible` (`menu_is_visible`); legal setters `setTristate` / `setValue` (refuse what kconfig refuses; `refusalReason` explains why), `toggleTarget` (the value `sym_toggle_tristate_value` would set: n -> m -> y -> n within range), `previewLowered(name, text)` (what other options lose if the value is set: bool/tristate options whose value drops and other options no longer written, each with the reason (`depends on …`, `no longer selected: …`, `prompt hidden: …`), members of the same choice excluded, the configuration restored afterwards; `null` when kconfig refuses the value; about 150 ms on a v3.0 x86_64 model); `loadConfig` (conf_read report: unknown, invalid, warnings), `loadAssignments`, `setAllNewSymbols`, `writeConfig` (full `.config`, byte-identical to `conf`), `writeDefconfig` (`make savedefconfig`), `configValues`, `exprStr(id, withValues)`, `orTerms`, `menuPath`.
- **Engine on/off** (also gates `#ifdef` dimming): on, every edit goes through the engine so only valid configurations exist, and the published values are the resolved `.config` values. Off, values are edited raw (no constraints, export marked unvalidated); turning it back on resolves the raw values through kconfig and reports how many were adjusted. Switching it off keeps every value: the tab's configuration stays in its `version|ARCH` profile and only stops being constrained.
- **Enabling it over edited values** (`resolutionDiff()`): the engine only holds configurations kconfig accepts, so enabling it loads the tab's values through `conf_read` semantics, which can change values the user never touched (raw edits kconfig refuses, an imported or hand-edited `.config`, symbols a `select` forces or whose prompt is invisible). The resolution is therefore computed first on a throwaway `KconfigEngine` built from the same model (the live engine and the values stay untouched) and shown before anything is applied: a dialog lists every option with its current value, the value kconfig resolves it to and why (*forced by select: …*, *prompt not visible: …*, *limited by dependencies or range*) plus the symbols unknown for the ARCH that would be dropped, and the engine is enabled only with **Enable engine and apply these changes** (Close or Escape leaves the engine off and the values exactly as they are, §5.0). When the resolution changes nothing, no dialog appears and the engine turns on directly. The same diff backs **Validate** and its *Apply kconfig resolution*.
- **One engine per tab, several at once** (`state.kconfigEngineTabIds` + `state.kconfigEngines`, persisted): the engine is **off by default** (a new KConfig tab opens on **⚡ Enable Engine**) and belongs to the tab that enables it. Each KConfig tab has its own view instance, model and values (`TabsComponent` builds one `KconfigView` per tab and `dispose()`s it when the tab closes), and **any number of tabs may run their engine at the same time**: each one resolves its own configuration and publishes it under its own tab id (`state.kconfigEngines[tabId] = {version, arch, configName, profileKey, assignments}`), so two ARCHes or two revisions can be resolved side by side. Enabling an engine therefore asks nothing of the other tabs — only the resolution of *this* tab's values is confirmed (above, §5.0). `state.setKconfigEngineEnabled(tabId, enabled)` switches one on or off and emits `kconfig:engine:changed` `{tabId, active, engineTabIds}`; `state.disableAllKconfigEngines()` (palette *Disable every KConfig engine*) switches all of them off. A `KconfigView` follows that event only for its own tab. `state.requestKconfigEngine(tabId)` / `takeKconfigEngineRequest(tabId)` (event `kconfig:engine:requested`, not persisted) let the command palette ask a tab — including one in the background, which claims the request when it renders — to enable its engine through that same confirmation instead of switching it on behind the view's back. Closing a tab, switching project or restoring a session whose tab is gone switches that engine off; no configuration is lost in any of these cases (`state.kconfigProfiles` keeps one per `version|ARCH`). `state.kconfigEngineActive` is derived (any engine running **in this window**), `state.isKconfigEngineOwner(tabId)` asks about one tab of this window, `state.hasEngine(tabId)` asks whether that engine runs anywhere (this window or another session, §5.23) and is what decides whether a code tab may follow it (§5.16), and `state.kconfigEngineList()` describes the running engines (tab id, title, revision, ARCH, name, symbol count, whether the configuration is resolved) for the code view's engine selector (§5.16). Only the engine descriptors are persisted: each engine's values are rehydrated from `kconfigProfiles[profileKey]` on load.
- **Cross-Session Kconfig Engine Broadcasting** (`SessionManager`, `BroadcastChannel("kip_multi_tab")`): running engines are usable across multiple browser tabs and windows. When an engine runs or updates in Session A, it announces its presence (`kconfig:engine:announced`) and broadcasts live assignments (`kconfig:assignments:broadcast`). Windows running Session B or Session C discover remote engines via `kconfig:request_engines`, automatically merging them into `state.kconfigEngineList()`. Code tabs in Session B can follow an engine running in Session A for `#ifdef` dimming, build condition evaluation, and definition ranking without needing to duplicate the Kconfig tab: such an engine is picked and kept exactly like a local one (`state.hasEngine`, §5.16), every edit in Session A's KConfig tab re-dims the code tabs following it in Session B, and it is dropped again when Session A's window or session goes away (§5.23). Saved configuration profiles and toolchain profiles are shared by every session and window: they live once, in `localStorage["kip_shared_kconfig_profiles"]` / `["kip_shared_kconfig_toolchains"]`, and each change writes only its own key (`sessionManager.updateSharedKconfigProfile` / `updateSharedKconfigToolchain` re-read the map first), so a window never erases configurations another window saved since it loaded. Session records do not carry them; a record saved before carries a copy, which `adoptSession()` moves into the shared store for the keys it lacks.

- **Deep links**: the tab's ARCH, configuration name, tree filter and inspected symbol are kept in the URL hash (`#tab=kconfig&arch=…&cfg=…&q=…&sym=…`, §5.26), so a copied link reopens the same search and the same symbol; switching ARCH drops the symbol, which belonged to the ARCH being left.
- **Architectures**: the dropdown lists `GET /presets` grouped by source directory (ARCH values such as `x86_64`, `i386`, `x86`, `sparc64`). Each `version|ARCH` keeps its own configuration in `state.kconfigProfiles`; switching restores it, or loads the ARCH's default defconfig (`canonical_defconfig`) the first time.
- **Several configurations of one ARCH and revision** (`state.kconfigProfiles` keys built by `kconfigProfileKey(version, arch, configName)`): a tab's configuration is saved under `version|ARCH` while it is the only one, and under `version|ARCH|name` once it is named, so copies are independent and can be compared against each other. **⧉ Duplicate** copies the current values (and the toolchain profile) into the first free name — `v2`, `v3`, … (`state.nextKconfigConfigName`, the unnamed one counting as v1) — and opens them in a new tab, unconstrained whoever holds the engine. **✎ `<name>`** renames the configuration (empty = unnamed, `|` is not allowed, a name another tab of the same `version|ARCH` uses is refused): the saved values and toolchain profile move to the new key (`state.moveKconfigProfile`) and the tab strip, status line and URL hash (`cfg=`) follow. A revision or ARCH switch that would land a tab on another tab's configuration gives it the next free name instead (`state.ensureUniqueKconfigConfigName`), so two tabs never share one configuration. The tab title is `KConfig (<arch>)`, plus ` · <name>`. A tab running its engine publishes its configuration under its own tab id (`state.setKconfigAssignments(tabId, profileKey, values, {version, arch, configName})`, bare and `CONFIG_` keys), and the code tabs following that engine dim with it; a tab whose engine is off only saves its values in its profile (`state.saveKconfigProfileValues`), so it never speaks for another tab's engine.
- **Tree (web menuconfig)**: entries from the finalized model in menuconfig order, with implicit submenus. Only entries menuconfig shows are listed (`menuVisible`); invisible prompted entries are added (dimmed, tooltip with the failing prompt condition) by the TUI's **Z** key, and automatically when an inspected symbol would otherwise be unreachable (`view.showUnmet`; the tab has no *Show All* button). Markers follow mconf: `[*]`/`[ ]` bool, `<*>`/`<M>`/`< >` tristate, `-*-`/`-M-` forced by select, `(X)`/`( )` choice values, `(value)` int/hex/string, `--->` submenus, `*** comment ***`; choices show their selected member. Clicking the marker changes the value (toggle, choice selection, or a prompt for int/hex/string); refused changes show the reason. With the engine on, lowering a value (y → m/n, m → n, from the tree, the inspector's n/m/y buttons or the TUI's Space/Y/M/N) that other options need asks first: a dialog lists each option that would drop with its current and new value and why, and the change is applied only with **Set to … anyway** (Close, Escape or clicking a listed option, which inspects it, cancels). Auto-solve applies its changes without asking. Search filters symbol names and prompts and shows each match's menu location.
- **Inspector**: value with its origin (explicit, default, forced by select, choice), n/m/y buttons enabled only for legal values (or an input for int/hex/string), evaluated prompt visibility, direct dependencies and "Selected by"/"Implied by" terms with `[=value]` annotations, every definition for the ARCH (file:line buttons opening the code view, prompt, menu location, visibility), defaults with the active one highlighted, ranges, selects/implies, choice group, help text, files built when enabled (other architectures collapsed), directories descended into, **Find usages** (source references grouped by file, Makefile conditions, Kconfig entries referring to it), **Auto-solve** (server solver, changes applied through the engine), and the dependency graph. Symbols not declared for the ARCH list their declarations elsewhere.
- **Toolchain** (Linux 4.18+; button **Toolchain…** or the toolchain summary in the status line): compiler (GCC/Clang) and version, linker (GNU ld/LLD) and version, assembler, binutils version, `CROSS_COMPILE`, LLVM utilities, pahole/rustc versions, GCC plugin headers, build host `uname -r`; a table of every environment variable Kconfig read with an override field (plus *add any variable*); a filterable table of every toolchain check with its result, source and the options using it, each overridable (auto / force y / force n, or text for command outputs); and `$(error-if)` messages (e.g. compiler below the minimum version). *Apply* stores the profile per `version|ARCH` (`state.kconfigToolchains`) and rebuilds the model with it; *Reset to defaults* clears it. Because another model resolves dependencies, defaults and ranges differently, `reloadModel()` resolves the current values on a throwaway engine first and, when any would change, lists them (option, now, after) and keeps the new model only once confirmed (§5.0) — declining, like a failed rebuild, restores the previous profile and leaves the model and every value untouched. With the engine off no value changes, so only the model is rebuilt. For older revisions the dialog explains that only environment variables matter.
- **Configurations**: **Load Defconfig** (filterable list, default one tagged), **Import .config** (conf_read in the engine; unknown/invalid entries reported), **Export .config** (`<arch>-<version>.config`, the resolved `.config` to place in the kernel tree), **Validate** (values kconfig would not keep with reasons, unknown symbols, select-with-unmet-dependency warnings, *Apply kconfig resolution*), **Compare…** (beside **⚡ Enable Engine** in the toolbar; `showCompareModal()`: one window with a **Compare with** selector — a defconfig of this ARCH, resolved on a throwaway engine, or any other open KConfig tab — including a named copy of the same ARCH and revision — whose configuration is taken from `state.kconfigProfiles` (`state.kconfigProfileKeyOf(tab)`) exactly as that tab holds it, resolved while its engine is on and raw while it is off. The table lists only the options whose values differ (`Option | Current | <side>`, `—` for an option one side does not write) with the matching/different counts (the first 2000 rows; comparing two architectures differs in thousands of options), and clicking a row inspects that option. A tab on another ARCH or revision is compared all the same, with a note naming the difference; a tab that has never loaded a configuration says so).
- **Undo / redo** (`kconfig_history.js`, `KconfigHistory`): `Ctrl+Z` / `Cmd+Z` reverts the last change applied to the tab's configuration and `Ctrl+Shift+Z` / `Ctrl+Y` re-applies it, while the tab is the active one, no dialog (or the TUI) is open and the focus is not in a text field (those keep their own text undo). A change is whatever reaches `commit()` — a value set from the tree, the inspector or the TUI (including the options a confirmed lowering drops with it), Load Defconfig, Import .config, Auto-solve, *Apply kconfig resolution*, enabling the engine over edited values — plus switching the engine off. A state is the tab's values, the engine's own state (`KconfigEngine.captureState()`: user values, choice values and defaults, choice member priority) and whether the tab runs its engine; only the **differences** between consecutive states are kept in memory (`[key, before, after]` per changed entry, 200 entries per tab, nothing persisted), plus the current state. Undo restores that state exactly (`restoreState()`, no re-resolution through kconfig, and the engine is switched back on or off as it was), so the restored configuration is the one the tab had, byte for byte. A new change after an undo drops the redo entries. The history survives re-renders of the tab (a tab switch, a layout change: the view puts back the state the history ends on when the saved values are unchanged) and starts over on another model: an ARCH or revision switch, a toolchain profile applied (`reloadModel`), or values changed from elsewhere while the tab was hidden. A toast names what was undone or redone.
- **TUI** (`menuconfig_tui.js`): mconf-style modal over the same engine: `menu`/`menuconfig`/`choice` open submenus, other children are listed indented; keys Up/Down/PgUp/PgDn/Home/End, Enter, Space, Y/M/N, Esc/Left, `/` search, `?`/H help (help text plus Symbol/Type/Prompt/Defined at/Depends on/Location/Selects/Selected by), Z show all, S save `.config`, D save defconfig (`writeDefconfig`, the only place a minimal defconfig is downloaded from).

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
     - **File Explorer**: Files in the sidebar tree open in a new code tab with `forceNew: true`; in the full-tab explorer (§5.20) a file opens in a new code tab and a directory in a second explorer tab, both at that explorer's revision.
     - **Code View Breadcrumbs**: a path segment opens the repository explorer of that directory in a new tab.
     - **Maintainers Viewer**: Subsystem cards, person profile cards, and referenced file links.
     - **Commits Viewer**: Commit history cards and touched file links.
     - **Callgraph Viewer**: Caller and callee function jump links and source file links.
     - **Pahole Struct Layout**: Nested struct/union drilldown links (`.struct-drilldown`), definition source file links (`#pahole-open-src`), Analyze button (`#pahole-search-btn`), and NodeMap elevation (`#pahole-btn-nodemap`).
     - **Version Diff Viewer**: Modified and added file rows.
5. **Modal Non-Dismissing Multi-Tab Queuing**:
   - In the **Cross-References (XRef)** and **Included Symbols** modals, middle-clicking definitions or usage links opens the target code tabs with `forceNew: true` while preserving the modal backdrop, allowing users to queue multiple references into separate tabs without re-opening search modals.

### 5.10. Developer Profiles & Kconfig Compiled Source Navigation
1. **Interactive Developer Profile Modal (`showPersonModal`) & Maintainers Sub-Tab**:
   - Clicking developer names across the workspace (Commit author headers, Contributors & Signoffs lists, Commit cards, Blame gutters, and Blame context popovers) activates the universal Developer Profile modal (`webapp/frontend/js/components/person_modal.js`).
   - Displays avatar initials, author email, CREDITS biographical data (description, project URL, PGP keys, snail mail), Git stats (authored commits count), and maintained subsystems.
   - **Maintainers "Developers" Directory**:
     - The Maintainers view incorporates a dedicated **"Developers"** directory (`Subsystems` | `Developers` | `CREDITS` | `Patch Reviewer`) listing every developer from `m_maintainer_person`, one card per merged identity (alias personas collapsed, §4.5 `/developers`).
     - Supports live name/email search, category filter pills (`All`, `Maintainers`, `Reviewers`, `CREDITS`), and an Activity (subsystems count) vs. A-Z alphabetical sort toggle.
     - Selecting any developer renders their complete profile card in the detail pane with maintained subsystems (clickable to jump into the Subsystems tab) and a direct "View All Commits" action button.
     - **Deep links**: the sidebar section, the search, the developer role pill and sort, and the inspected subsystem or developer are recorded on the tab (`section`, `query`, `role`, `sort`, `secId` / `person`, via `persistTabState`) and written to the URL hash (`#tab=maintainers&view=<developers|credits|patch>&sec=<sec_id>&person=<email>&q=…&role=…&sort=alpha`, §5.26). One selection is held at a time: inspecting a subsystem drops `person` and vice versa, and the Patch Reviewer clears both. A developer is addressed by email when known (stable across database rebuilds), else by `person_id` or name. `MaintainersView.render` rebuilds everything from the tab, requesting the selection beside the list.
   - Provides 1-click elevation actions:
     - **Open in Maintainers Tab**: Focuses or launches `state.openTab({ type: "maintainers", person: ... })`, inspecting the developer's complete subsystem roster and contribution graph.
     - **View Commits**: Focuses or launches `state.openTab({ type: "commits", query: ... })`, filtering the commit history by author name or email.
2. **Kconfig Compiled Source Files**:
   - `sym.compiled_files` records (`{"file_path": "..."}`) are normalized and displayed with source file icons (`📄 kernel/smp.c`) and associated target object badges (`smp.o`).
   - Clicking any compiled file item (`.compiled-file-link`) immediately opens the target source file in the virtual code editor (`state.openTab({ type: "code", path: filePath })`), supporting both left-click in-place navigation and middle-click multi-tab queuing.

### 5.11. Compare View: Files & Kconfig Between Any Two Revisions (`webapp/frontend/js/views/diff/diff_view.js`)
1. **Activity Bar Entry**: The **Compare** activity-bar button (`data-action="compare"`) opens a single `diff` tab (re-clicking focuses it). The tab persists `versionA`, `versionB`, and `mode` in its tab data, and its title reflects the pair (`Compare v3.0…v3.1`).
2. **Revision Selection**: Base and Target buttons open the shared `refPicker` (§5.13), so either side can be a release tag or any commit. When no base is stored, it defaults to the tag preceding the target (or the latest tag when the target is a commit). A ⇄ button swaps the sides.
3. **Files Mode** (`api.getVersionsDiff`, 45 s timeout): Status chips (added / modified / deleted / renamed / unchanged) act as toggle filters alongside a path filter (which matches the path and, for renames, the old path). With no chip selected every change is listed; unchanged files are listed only while their chip is selected. They are fetched the first time it is (`api.getUnchangedFiles`, `GET /api/tools/diff/unchanged`, once per comparison; *Loading unchanged files…* meanwhile, and reselecting the chip retries a failed fetch), merged in path order with the other selected statuses, and shown with a `U` badge; clicking an unchanged row opens the file at the target revision instead of expanding a diff. Rows render in pages of 500. A rename row shows `old → new` and, below 100%, its similarity. Clicking a row expands an inline unified diff from `api.getFileDiff` (renames pass their similarity, capped at 50); per-row buttons open the file at the base or target revision, and middle-click opens it in a new tab.
   - **⬇ Download (N)** (`downloadFileRows`): saves every row matching the chips and the path filter, all pages (N is that count; the button is disabled while unchanged files load or when nothing matches), as *Paths (.txt)* (one path per line: the target path, the base path for deleted files), *Table (.tsv)* (`status` A/M/D/R/U, `similarity` for renames, `path`, `old_path`) or *JSON (.json)* (`base`, `target`, `generated`, `filters` with the statuses, path filter and last successful rename search, `renames` of the response, `count`, `files` with `status`, `path` and, for renames, `old_path` and `similarity`). No format carries the symbol counts of modified files. Rows include the pairing of a rename search already applied. The file is named `compare_<base>..<target>[_<statuses>][_<filter>].<ext>` and written with `downloadText` (`components/modal.js`).
   - **Rename search** (shown while the **renamed** chip is active, `renderRenameSearch`): a note says how renames were paired (`renames` of the response: every pair compared, or only identical / same-basename files because there were too many candidates, or identical content only without Git trees), and a form takes a similarity threshold (default 50%) and a directory (suggestions: the directories holding the most unpaired added/deleted files). **Search renames** calls `api.searchRenames` (`GET /api/tools/diff/renames`, 120 s timeout, cached per revision pair, threshold and directory). A refused search (`too_many`) states the deleted × added counts and asks for a directory. A completed search replaces the pairing inside the directory (`applyRenameSearch`): renames with both ends there go back to added + deleted and the search result pairs them again (so a higher threshold drops pairs and a lower one adds them), renames crossing the directory boundary are kept, and the chip counts are recomputed; the status line reports the number of renames, the change against before, the pairs compared and the time. Each successful search is recorded on the tab (`renameSearches`, `[{sim, dir}]`, and so in the URL as `rn`, §5.26) and re-applied in order whenever the diff loads again (a mode switch, a re-render, a reopened link; the searches are cached, so this is fast). A search supersedes the recorded ones in its directory or below it, since it re-pairs that whole directory; changing or swapping a revision clears them.
   - **Deep links and per-tab state**: each Compare tab has its own `DiffView` (`app.viewRenderers.diff` is a factory), and the revisions, mode, chips (Files and Kconfig), path filter, Kconfig filter and rename searches are recorded on the tab with `state.updateTabState` (`persistTabState`, skipped for a view whose element was replaced; the render itself records before the pane attaches it) and written to the URL hash (§5.26). A deep link into an existing Compare tab merges those fields into it (`state.openTab`).
4. **Kconfig Mode** (`api.getKconfigDiff`): Status chips **added / modified / deleted** (each shows the option count and the Kconfig file count) act as toggle filters on both lists, like the Files chips: an option `added`/`changed`/`removed` and a Kconfig file `added`/`modified`/`removed` map onto them (`KCONFIG_STATUS_OF`); with no chip selected everything is listed. A text filter matches option names (with or without `CONFIG_`) and file paths. Below: a warning listing unindexed Kconfig files when option coverage is partial, an **Options** list (the first 2000 matches; expand for per-file `type` / `prompt` / `default` before and after, or **Inspect** to open the KConfig view on that symbol), and a **Kconfig Files** list with the same inline diff and open actions as Files mode.
   - **⬇ Download (N)** (`downloadKconfigRows`): saves every option and Kconfig file matching the chips and the filter (N is their sum; not capped at 2000; disabled when nothing matches), as *Options (.txt)* (one `CONFIG_<name>` per line), *Kconfig files (.txt)* (one path per line), *Table (.tsv)* (`kind` option/file, `status` A/M/D, `name` — `CONFIG_<name>` or the path —, `files` defining the option on either side, `changed_fields` of a single-definition change) or *JSON (.json)* (`base`, `target`, `generated`, `filters` with the statuses and the filter text, `summary` of the response, `count` `{options, files}`, `options` with `name`, `status`, `files` and the response's `before`, `after`, `changes`, and `files` with `status` and `path`). The file is named `compare-kconfig_<base>..<target>[_<statuses>][_<filter>]` (`_options.txt` / `_files.txt` / `.tsv` / `.json`).

### 5.12. Git Commits Timeline & Bi-Directional Pagination (`webapp/frontend/js/views/commits/commits_view.js`)
- **Clean Full-Width Search Header**: Replaced legacy direction toggle buttons (`◀ Future`, `⊙ Anchor`, `Past ▶`, and `🏷️ v3.0`) with a clean, focused typeahead search bar (`#commit-search-input`).
- **Bi-Directional "Load 50 More Commits" Pagination**:
  - **Top Pagination Button (`#btn-load-more-top`)**: Automatically renders at the top of the commit timeline when commits exist above the current viewport (`has_future`). Seamlessly fetches 50 commits using `direction="future"` and `next_future_cursor`, prepends them in continuous chronological descending order, and preserves exact vertical scroll position (`scrollTop`) without layout jumping.
  - **Bottom Pagination Button (`#btn-load-more-bottom`)**: Automatically renders at the bottom of the timeline when older commits exist (`has_past`). Fetches 50 commits using `direction="past"` and `next_past_cursor` and appends them to the bottom of the list.
  - **Repeated loads**: each button is rebuilt by `renderCommitList()` from `isLoadingTop` / `isLoadingBottom`, so those flags are cleared **before** re-rendering — a button rendered while they still said "loading" stayed disabled and kept reading *Loading…*, which capped the timeline at one extra page per direction. Commits already in the list are filtered out by `commit_id`/`commit_hash` when a page is merged, so a cursor that overlaps the previous page cannot duplicate rows.
  - **Search Pagination**: In commit search mode, the bottom button paginates sequentially through matching commits across pages.
- **Deep links**: the search and the inspected commit are kept in the URL hash (`#tab=commits&q=…&cid=<hash>`, §5.26, always the commit hash so the link survives a database rebuild).
- **One view per tab**: each commits tab has its own `CommitsView` (`app.viewRenderers.commits` is a factory), and `render()` takes the search, the selected commit and the folded list from the tab alone (`tab.query`, `tab.commitId`, `tab.listCollapsed`; absent means none). The view used to be one shared instance that only overwrote what a tab carried, so a tab without a search showed the search typed in the last commits tab rendered.
- **Collapsible list**: the collapse button at the end of the search bar (`#btn-commits-collapse`, the default collapse icon `COLLAPSE_ICON` of the sidebar, §5.27) folds the list column into a 28 px rail on the left (`.commits-view-container.list-collapsed`); clicking the rail (`#btn-commits-expand`, the mirrored icon and *Commits*) unfolds it and focuses the search. The state is kept on the tab (`listCollapsed`) and in the URL (`lc=1`, §5.26). **A folded list is never requested**: the timeline or search is loaded the first time the list is unfolded (`listLoaded`), then the selected card is scrolled into view. Tabs opened for one commit from elsewhere open folded (`listCollapsed: true`): a blame gutter, *View Commit* of the hash menu (§5.6) when it opens a tab, the commit popover's *Open commit tab*, the file and symbol history modals, and a commit card middle-clicked into a tab of its own. Reopening an existing tab with `listCollapsed` given (`state.openTab`) applies it; searches by author (person modal, Subsystems view) still open unfolded.
- **Selected commit first**: when the tab opens with a commit selected (deep link, restored tab, *View Commit*), `render()` requests its detail **before** the timeline or search and runs both in parallel (`Promise.all`), so the detail pane never waits for the list. `api.getCommitDetail` is sent with fetch `priority: "high"` (ignored by browsers without the Fetch Priority API). Each pane renders only its latest request (`_detailToken`, `_listToken`): a commit clicked, or a search typed, while an earlier one is still loading is never overwritten by the late answer.
- **Commit Inspection & File Jumping**: Selecting any commit card displays subject, author, UTC date, Git SHA, Lore LKML thread link, structured trailers/signoffs badges, full commit message, and touched file list with middle-click multi-tab queuing.
- **Modified Files agrees with the diff**: the card renders the detail endpoint's `files`; when that list was resolved from Git rather than staged (merge commits, see the detail endpoint above) the title carries a muted *resolved from Git (merge, vs first parent)* note, and `files_truncated` appends *… list truncated*. The badge color of every status (`A`/`D`/`R`/`M`) comes from `STATUS_COLORS`, and paths are HTML-escaped.
- **Copy a hash with one click** (`hashHtml` / `attachHashCopy`): every `.commit-hash` in the view — the short hash on a commit card, the full hash in the detail header, and the parent hash in the diff summary — is a `.copy-hash` button (`role="button"`, keyboard `Enter`/`Space`) that copies the **full 40-character SHA** through `utils/clipboard.js` and flashes green on success. One delegated listener per view container, bound in the **capture** phase so copying a card's hash does not also select or re-open that commit.
- **Right-click a hash**: the same delegated listener opens `contextMenu.showCommitHashMenu` (§5.6) with **Copy Hash**, **View Commit** and **Set as Work Commit**. *View Commit* goes through `selectLoadedCommit(hash)`, which selects and inspects the card already in the timeline (matched by `data-hash`, scrolled into view) and returns false for a hash outside the loaded range so the menu opens a tab of its own for it.

- **Inline Commit Diff (`#btn-commit-diff`)**: The bottom of a selected commit's detail has a **± Show changes** button. It loads `GET /api/commits/{v}/diff/{hash}` once per commit (IndexedDB `commits` store, key `cdiff_<hash>_<context>`) and renders a compact per-file diff (status badge, `+/−` counts, old/new line-number gutters). Files with more than 120 rows, or after the 25th file, start collapsed; each file body scrolls at 420px.
  - **Jump to the edited lines**: Clicking an added or context row opens the file **at the commit** on that line; clicking a removed row opens the file **at the parent commit** on the old line (deleted files always open at the parent). Hunk headers and the per-file **Open ↗** button jump to the first change. Ctrl/Cmd+click or middle-click opens a new tab. Selecting text in the diff does not navigate.
  - **Automatic Header Version Synchronization**: Clicking **Open ↗**, an edited line, or *Explore Files at this Commit* switches the workspace to the target commit revision, synchronizing both `state.currentVersion` and `state.selectedVersion` (emitting `version:synced`) so the top navigation bar immediately tracks and reflects the commit version without requiring the user to navigate out and back into the tab.
  - **Change markers in the code view**: The opened tab carries `diffMarks: { kind: "add" | "del", commit, ranges: [[start, end], ...] }`. `VirtualEditor` tints those lines green (added) or red (removed), adds a gutter bar and minimap ticks, and shows a `± <hash>: N lines changed` chip with **↑/↓** (previous/next change block) and **✕** (clear markers). Re-opening an already open file tab replaces its markers.

### 5.13. Header Version Selector, Apply & New Tab (`webapp/frontend/js/app.js`, `webapp/frontend/js/components/ref_picker.js`, `webapp/frontend/js/components/version_switch.js`)
- **Two revisions, not one**: `state.selectedVersion` is what the header's controls point at, `state.currentVersion` is the revision of the active tab (what links opened from it follow, `state.activeVersion()`). Both are persisted, with the branch the lists are scoped to (`state.selectedBranch`, event `branch:selected`). **Selecting a revision changes nothing**: the Tag, Release, Branch and Commit controls only move the selection (`state.setSelectedVersion`, event `version:selected`), and nothing reloads until **Apply** or **New Tab** is pressed.
- **Selectors**: `.version-selector-container` holds Project, Branch (`#branch-select`, the scope or custom tag view of the lists beside it), Tag (`#version-select`, releases), Release (`#rc-select`, sub-versions and candidates of the selected release; see §5.18), Commit (`#commit-picker-btn`), **Apply** (`#version-apply-btn`) and **New Tab** (`#version-newtab-btn`). A selection may be a tag, a branch name or a commit SHA; startup sanitization only resets values that are none of these (for both the explored and the selected revision), and it goes by the **whole** catalog, so a revision the branch scope excludes is never reset.
- **Branch Selector & Tag Layout Views** (`tag_views` in repository config; `getActiveTagView()`, `parseTagVersion()`, `renderBranchSelector()`, `selectBranch()`, `loadBranchScope()`, `state.setBranchTags()`, `state.tagOnSelectedBranch()`, `releaseOnBranch()`):
  - **Custom Tag Views (Top of Dropdown)**: Populated from `tag_views` in repository config (defaulting to `— any branch —`). When active, ordered regex rules partition tags into base releases (Tag selector) and sub-versions (Release selector):
    1. For Linux 2.6.x, `^(v2\.6\.\d+)((?:-(?:rc|pre|alpha|beta)\d*|\.\d+(?:-(?:rc|pre|alpha|beta)\d*)?).*)$` maps `v2.6.<sublevel>` (e.g. `v2.6.16`) as the base release, while stable point releases (`.1`..`.62`) and candidates (`-rc1`..`-rc6`, `.28-rc1`) go to the Release dropdown.
    2. For Linux 3.x+, `^(v(?!2\.6\.)\d+\.\d+)((?:-(?:rc|pre|alpha|beta)\d*|\.\d+(?:-(?:rc|pre|alpha|beta)\d*)?).*)$` maps `v<major>.<minor>` (e.g. `v3.0`) as the base release, with point releases (`.1`..`.101`) and candidates (`-rc1`..`-rc7`) in the Release dropdown.
    3. Other projects can configure custom `tag_views` with arbitrary regex patterns.
  - **Real Git Branches (True to Git)**: Selecting a real Git branch scopes offered tags to those reachable from it (`GET /api/branch/tags`, e.g. `stable/linux-2.6.16.y`), preserving standard Git pre-release grouping where stable point releases remain standalone tags and only `-rc`/`-pre` pre-releases move to the Release dropdown. Selecting a branch also sets its head as the selected revision.
  - **Sub-version / Release Dropdown Sorting & Default**: Items in the Release dropdown are sorted descending by version (latest stable point releases first, e.g. `.101`, `.2`, `.1`, then `final`, followed by release candidates newest to oldest, `-rc7`, `-rc1`). When a base release is selected in the Tag dropdown, it defaults to the `final` tag if present, or to the latest candidate if rc-only.
- **Apply** (`requestVersionSwitch(target)`): moves **the active tab** to the selected revision (`state.setVersion`, which also updates `state.currentVersion` and drops the tab's saved scroll position) and nothing else — every other tab stays where it is. The tab menu's **Apply Current Tag** does the same for the right-clicked tab, and **Get Tab Tag** copies a tab's revision back into the header (§5.24.5). Pressing it when the tab already shows that revision, or with no tab open at all, says so (the latter opens an explorer instead). The button is highlighted (`.pending`) while the selection differs from the active tab's revision, and its tooltip names both revisions.
- **New Tab** (`openVersionInNewTab(version)`): opens a full-tab **repository explorer** (§5.20) on the selected revision, which is how a second revision is explored side by side without touching the tabs already open.
- **Links follow their tab**: `state.openTab()` defaults a new tab's revision to the active tab's (`state.activeVersion()`), never to the header selection, and every view passes its own revision explicitly (code view, explorer, blame gutters, include popover, XRef modal, NodeMap). A commit chosen in the palette's `#` mode, *Explore Files at this Commit* in the commits view and *Explore at commit* in the file history modal therefore no longer repoint an open tab: they move the header selection and open that commit in a tab of its own.
- **Guarded switch for KConfig tabs** (§5.0): applying a revision to a KConfig tab also **carries that tab's configuration to the target revision and drops the configuration saved for the revision it leaves** (`kconfigCarryPlan` / `applyKconfigCarryPlan`: the values move to `version|ARCH[|name]` of the target, the toolchain profile moves with them, and the tab takes the next free name when the target already has a configuration under its own). A dialog states how many values move, that nothing is kept for the revision being left, that options the target revision does not define are dropped when the model loads them, and — when the tab runs its engine — that the carried values are resolved against the target's model and the code tabs following that engine follow the result. **Close** leaves the tab, its configuration and the explored revision untouched. After the switch the view reports what the resolution dropped (`activateArch`). Restoring a revision from the URL hash only moves the selection and never prompts.
- **Commit Picker**: `#commit-picker-btn` opens `refPicker` with `scoped: true`, a popover with a search box that lists matching release tags (`api.getVersionsCached`, the catalog already loaded in this page, **narrowed to the selected branch** — its section reads *Release Tags on `<branch>`* and its placeholder names the branch) and commits (`api.getCommits`, matching subject, author, or hash prefix). The Compare view opens the same picker **unscoped** (`scoped` defaults to false), since comparing two branches means reaching the tags of both; a tag outside the scope is also still selectable in the header by pasting its name (the *Revision* entry). Arrow keys navigate, Enter selects, and any pasted SHA or ref name not in the lists is offered as a raw revision (resolved server-side via `git rev-parse`). Selecting one sets the header selection.
- **Display Sync** (`updateVersionSelectorDisplay`, on `version:changed` / `version:synced` / `version:selected` / `tabs:changed`): On a tag, the Tag dropdown selects it and the Commit button shows the tag's short SHA. On a commit, the Tag dropdown shows `— none —` and the Commit button shows the short SHA highlighted as detached. The Branch dropdown shows the **scope** or active tag view (`state.selectedBranch`), not the explored revision, so picking a tag from a scoped list leaves the branch where it is. The sidebar explorer header and the tab version badges show the active tab's revision in the same short form (`shortRef`, 12 hex chars).
- **Cache Keys**: Directory trees are cached under `tree2_*` and diffs under `diff3_*` / `kdiff2_*` / `fdiff_*` / `bdiff_*` / `renames_*` / `unchanged_*` so responses cached before Git-native tree resolution (and, for `diff3_`, before similarity-based rename detection) are not reused; every version-scoped key embeds the version's stamp (`api._vk`, §2.11), the branch scope included (`branch_tags_<branch>@<stamp>~`, so a branch that moved is walked again and an unchanged one is read from IndexedDB). Service Worker caches are described in §2.11.

---

### 5.14. Documentation Viewer (`webapp/frontend/js/views/docs/`, `webapp/frontend/css/docs.css`)
- **Activation**: `isDocumentationPath(path)` selects `*.rst`, `*.md`, `*.markdown`, `README*` (including root or directory `README.md`), and `Documentation/` files that are `.txt` or extension-less (excluding `Makefile`/`Kconfig`/`Kbuild`). Such code tabs open in **Rendered** mode (`tabData.docMode = "rendered"`) unless a specific `cursorLine` was requested; the toolbar's **📖 Rendered / </> Source** button toggles back to the virtual editor, and enabling **Blame** switches to source.
- **Layout**: Mirrors https://www.kernel.org/doc/html/latest/ (Sphinx/Alabaster): a left sidebar with *The Linux Kernel* title and the tab's version, **Quick search** (in-page highlight; Enter cycles matches), **Contents** (nested local TOC with scroll-spy), **Navigation** (directory `index.rst`/`00-INDEX` and `Documentation/index.rst`), and **This Page → Show Source**, beside a serif body and a footer. Colors come from the IDE dark tokens.
- **Unified Renderer Dispatch** (`renderDocument(source, path)`): Inspects the target file path and source contents. If `isMarkdownPath(path)` (or unadorned source containing markdown headings/fences/task-lists via `looksLikeMarkdown`), delegates to `renderMarkdownDocument(source, path)` (`webapp/frontend/js/views/docs/md_renderer.js`). Otherwise, renders via `RstRenderer` (`rst_renderer.js`).
- **Markdown Renderer (`webapp/frontend/js/views/docs/md_renderer.js`)**:
  - **GFM Headings & Permalinks**: Parses ATX (`#`–`######`) and Setext (`===` / `---`) headings, generating clean slugs (`slugify`), anchor IDs, and clickable `¶` permalinks (`.headerlink`). Extracts local TOC hierarchy (`h1`–`h4`) for `DocPage` sidebar navigation and scroll-spy.
  - **Fenced Code Blocks & Multi-Language Highlighting**: Code fences (```` ``` ```` or `~~~`) with language specifiers are highlighted via `highlightCode(code, lang)` using `AstOverlay` and language grammars (`syntax/languages/`), supporting Rust, C, Python, Shell, Perl, Assembly, Makefile, Kconfig, DTS, YAML, JSON, and fallback text.
  - **GitHub Alert Callouts**: Blockquotes starting with `> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`, `> [!WARNING]`, or `> [!CAUTION]` render as styled `.admonition` callout boxes with categorical title badges and SVG icons matching Sphinx documentation themes.
  - **GFM Tables**: Pipe tables with cell alignments (`:---`, `:---:`, `---:`), headers, and responsive borders styled under `.docutils`.
  - **Task Lists**: List items with `[ ]` or `[x]` / `[X]` checkboxes render as `.task-list` and `.task-list-item` with non-editable styled checkboxes (`docs.css`).
  - **Inline Formatting & Kernel Automarkup**: Strikethrough (`~~text~~` → `<del>`), bold (`**` / `__`), italic (`*` / `_`), inline code, links with relative document path resolution (`resolveRelativePath`), images, bare URLs, keyboard shortcuts (`<kbd>`), and kernel automarkup (`func()` → symbol link, `struct foo` → struct link, `CONFIG_*` → Kconfig view link). Supports nested linked images and badges (`[![alt](img)](url)`) with transparent inline styling (`.kdoc-badge`), reference-style link definitions (`[label]: url "title"`), full/collapsed reference links (`[text][ref]`), shortcut reference links (`[ref]`), and clean omission of HTML comments (`<!-- ... -->`).
- **reStructuredText Renderer (`webapp/frontend/js/views/docs/rst_renderer.js`)**: `.rst` (or `.txt` that uses rST markup) is parsed as reStructuredText: over/underlined sections (levels by first appearance, `¶` permalinks), transitions, bullet/enumerated/field/definition/line lists, `::` and quoted literal blocks, block quotes, grid and simple tables, footnotes, substitutions, hyperlink targets and labels, and directives (`code-block`/`highlight` with C and shell highlighting, admonitions, `toctree`, `contents`, `list-table`/`flat-table`/`csv-table`, `kernel-doc`/`include`/`literalinclude`/`kernel-*` as source-link cards, figures as placeholders, `c:function`-style domain entries, `versionadded`/`deprecated`, `only`, `rubric`, `topic`). Inline markup covers literals, emphasis, roles (`:doc:`, `:ref:`, `:c:func:`/`:c:type:`…, `:manpage:`, `:kbd:`, `:abbr:`, `:menuselection:`…), embedded and named links, bare URLs, `Documentation/…` paths, and kernel automarkup (`func()`, `struct foo`, `CONFIG_FOO`). Legacy plain-text docs keep aligned/indented text as preformatted blocks and promote tab-centered lines to headings. All source text is HTML-escaped.
- **Figures**: `image`/`figure`/`kernel-figure` render inline from `/api/fs/raw` (on a light backing for legibility); unrenderable formats (e.g. `.dot`) fall back to a source link.
- **kernel-doc**: `.. kernel-doc::` blocks are replaced by the extracted API documentation (`/api/docs/{v}/kernel-doc` with the directive's options): Sphinx C-domain entries with highlighted signatures, Parameters/Members, Definition (types), Description/Return/Context sections rendered as reStructuredText after translating kernel-doc markup (`@param` → bold, `%CONST` → literal, `&struct foo` → type link), and a *[source]* link to the comment's line.
- **Cross-document `:ref:`**: unresolved labels are looked up in `/api/docs/{v}/labels` and become links to `<doc>#<label>` showing the target section's title.
- **Links**: `:doc:`/toctree/`Documentation/` links navigate in place (`tabData.docHistory` backs the sidebar **← Back**; Ctrl/middle-click opens a new tab), toctree labels are filled from each target's first title, `func()`/`struct` links jump to the symbol definition via `api.getSymbolDetail`, `CONFIG_*` opens the KConfig view, directive cards open the referenced source, and in-page anchors scroll without touching `location.hash`.

### 5.15. Command Palette (`webapp/frontend/js/components/command_palette.js`)
- **Opening**: `Ctrl/Cmd+K` or `Ctrl/Cmd+P` (search), `Ctrl/Cmd+Shift+P` (commands), or the header **🔍 Search…** button.
- **Modes by prefix**: none — files (`/api/fs/search`), symbols (`api.searchSymbols`) and Kconfig options (`api.searchKconfig`) together; `>` commands; `@` symbols; `#` commits (`api.searchCommits`, selecting one selects it in the header and opens its tree in a repository explorer tab); `?` Kconfig options; `:` go to line in the active code tab. Results are grouped by section with match highlighting; ↑/↓ navigate, Enter opens, Ctrl+Enter opens in a new tab. Enter always acts on the text currently typed: if the rendered results were computed for an earlier input (the 160 ms debounced search is pending or still in flight), it searches the current text immediately and opens that search's top result, so a quick Enter never runs a stale row such as the *Suggested commands* shown for an empty query (`resultsFor` / `pendingEnter`).
- **Commands** (`KernelIdeApp.paletteCommands()`): compare, select commit, source treemap, export `compile_commands.json` (download via `api.getCompileCommandsDownloadUrl`), KConfig view, *Enable KConfig engine in a tab* (asks the active KConfig tab, else the first one not already running its engine, to enable it — `state.requestKconfigEngine(tabId)` shows that tab and its view confirms the resolution as usual (§5.0), so the palette never changes values on its own; with no such tab it says so), *Disable every KConfig engine* (`state.disableAllKconfigEngines()`, configurations kept), *Open repository explorer tab* (§5.20), commits, maintainers, NodeMap, documentation, explorer toggle, plus *Create patch* / *Symbol history* for the active code tab and *Switch project* entries.

### 5.16. `#ifdef` Dimming (`webapp/frontend/js/views/kconfig/directive_evaluator.js`)
- **Which engine a file follows** (`tab.kconfigEngineTabId`, persisted): several KConfig tabs may run an engine (§5.8), so each code tab follows exactly one of them, chosen with the toolbar's **⚙ `<arch>` ▾** button (`#btn-kconfig-engine`). The popover lists *No engine* plus every running engine with its ARCH, revision, symbol count and whether its configuration is resolved; a revision other than the tab's is marked as such. A tab nobody set an engine on follows the **first engine whose revision is the tab's own**, and **no engine** when none of them is on that revision — dimming and the build banner are then simply off until one is picked (`resolveEngineSelection`, `state.defaultEngineTabIdFor`). A choice made by hand (`kconfigEngineUserPicked`) outlives new engines appearing and is only re-defaulted when the engine it names stops running.
- **An engine of another session is picked like a local one**: the popover lists the engines running in other browser windows as well (§5.23), and **every** check of "can this tab follow it?" goes through `state.hasEngine(engineTabId)`, which is true for an engine of this window (`isKconfigEngineOwner`) *and* for one in `sessionManager.remoteEngines`. Both the choice (`setEngineSelection`) and its re-resolution on every engine change (`resolveEngineSelection`) use it, so picking a remote engine sticks: it survives switching away from the tab, re-rendering it, *No engine* being chosen in between, and the announcements of other engines — it is only re-defaulted when that engine actually stops running (its tab closed, its engine switched off, or its window closed, §5.23).
- **Gate**: Only while the engine this tab follows is running (§5.8) **and** has a resolved configuration (≥ 50 symbols, i.e. a defconfig; `state.kconfigConfigActive(engineTabId)`), for preprocessed sources (`PREPROCESSED_RE`: `.c .h .S .lds .dts .dtsi .inc`). A toolbar chip shows the number of compiled-out lines; clicking it (`VirtualEditor.toggleDimSuspended()`) temporarily shows those lines undimmed (chip marked *not dimmed*, dashed) and clicking again restores the dimming. The choice applies to every code tab until toggled back or the page is reloaded (not persisted); the count, the build banner and Go to Definition are unaffected.
- **Semantics**: Three-valued evaluation of `#if/#ifdef/#ifndef/#elif/#else/#endif` (with `\` continuations). Only `CONFIG_*` macros are known, following `autoconf.h`: `CONFIG_FOO` is defined for `y` and every string/int/hex value (even empty), `CONFIG_FOO_MODULE` for `m`, symbols that are `n` or absent are undefined; `IS_BUILTIN` is true for `y` (or an int equal to 1), `IS_ENABLED`/`IS_REACHABLE` for `y`/`m`; `IS_ENABLED/IS_BUILTIN/IS_MODULE/IS_REACHABLE`, `defined()`, `!`, `&&`, `||`, parentheses and numeric comparisons are supported. The compiler-predefined macros of the configuration's ARCH are known as well (`archMacros(arch, assignments)`): `__KERNEL__` is defined; the own architecture's macros are defined (`__x86_64__`/`__amd64__` or `__i386__` by word size, from `CONFIG_64BIT` when set, else from the ARCH value; `__arm__`, `__aarch64__`, `__powerpc__`/`__powerpc64__`, `__sparc__`/`__arch64__`, `__s390__`/`__s390x__`, `__mips__`, ...), every other architecture's macros, the other word size's macros, `__CHECKER__` and `__GENKSYMS__` are undefined, `__LP64__` follows the word size; in `#if` expressions known macros are 1 or 0. Any other macro (header guards, `__ASSEMBLY__`, `MODULE`) is unknown and a branch with an unknown condition is never dimmed, nor are its alternatives. With `partial: true` (used for the x86_64 baseline of §5.19), `CONFIG_*` symbols absent from the assignments are unknown instead of `n`. Directive lines are dimmed only when their enclosing region is compiled out.
- **Live updates**: every code tab has its own `VirtualEditor` (`TabsComponent` builds one per tab and `dispose()`s it — unsubscribing it — when the tab closes), and each one recomputes its dimmed lines, its `#ifdef` chip count and its build banner on `kconfig:engine:changed` and on `kconfig:assignments:updated` **for the engine it follows** (both events carry the `tabId` of the engine, so an edit in a KConfig tab no code tab follows costs nothing): enabling or disabling an engine, loading or importing a configuration, a single value edit and a toolchain rebuild reach the editors following it. `kconfig:engine:changed` also re-resolves each editor's engine selection, so a code tab whose engine was switched off falls back to the default and one on a revision that just got its first engine picks it up. Both events reach a code tab following an engine of **another session** exactly the same way: an edit in that window's KConfig tab arrives as `kconfig:assignments:broadcast`, updates the remote engine's entry (values *and* descriptor) and is re-emitted locally as `kconfig:assignments:updated` under the same engine tab id, so the dimming, the chip count and the build banner of every code tab following it refresh on each edit without that tab being touched (§5.23). A tab only does that work while it is on screen (`isLive()`: its lines are still in the document); showing a tab again re-renders it, which reloads the file and recomputes both from the configuration in force at that moment, so a tab edited behind the scenes is never stale when it comes back. The *not dimmed* choice is shared by every code tab (`code:dim-suspended`), so toggling the chip in one updates the others on screen.
- **Build banner**: with the same gate, `VirtualEditor.refreshBuildBanner()` fetches the open file's Kbuild condition (`api.getBuildConditions`, once per version/ARCH/path) and evaluates it against the configuration (`build_condition.js`, absent symbols are `n`). When it is `n`, a red banner above the code says the file is **not compiled** for the ARCH with the current configuration and why: the failing symbols with their values (e.g. *Needs `CONFIG_E1000E` (currently E1000E=n)*, plus how many alternative rules also fail), or the reason for `other_arch`/`no_rule` (*arch/arm/ is not built for ARCH=x86_64*, *drivers/net/Makefile does not build skeleton.o*). When it is `m`, a blue banner says the file is built as a module. The banner shows the object's Kbuild rule (`makefile:line text`), which opens the Makefile at that line. Unknown conditions (`?`, e.g. headers) show nothing. It is re-evaluated without refetching on every configuration change.

### 5.17. Treemap, Symbol History & Patch Staging
- **Treemap** (`views/treemap/treemap_view.js`, tab type `treemap`): squarified layout of `/api/version/{v}/treemap` two levels deep, Bytes/Files metric toggle, breadcrumb zoom, click a directory to zoom in or a file to open (middle-click: new tab). Opened from the explorer's folder context menu (*Show Treemap of Folder*) or the palette.
- **Symbol & Kconfig history** (`components/symbol_history_modal.js`): `showSymbolHistoryModal(version, path, line, astId, name)` (editor context menu *Symbol History Across Releases* on a symbol, *Symbol History at This Line*, palette *Symbol history at cursor line*) and `showKconfigHistoryModal(version, name)` (context menu *Kconfig History Across Releases* on a `CONFIG_*`/Kconfig token or selection, KConfig view **🕰 History** card).
  - **Branch**: the timeline follows one branch (*Branch scope* of `GET /api/symbols/{version_name}/history`). The modal opens on the header's branch (`state.selectedBranch`, a real branch rather than a tag layout view) when the version is one of its tags (`state.tagOnSelectedBranch`) or is that branch; otherwise the server picks the branch holding the version (the default branch, or e.g. `stable/linux-2.6.32.y` for `v2.6.32.71`). The **Branch** selector of the controls (`GET /api/branches`) refetches the timeline on another branch, keeping the selected release when it is on it; it changes nothing outside the modal (the header's scope stays as it is).
  - **Slider**: one stop per release where the definition changed (introduced, modified, formatting-only, removed, re-added) plus the first and the newest indexed version; each run of releases without change collapses into a `…` gap (click expands it). ◀/▶ buttons, ←/→ keys and **Play** step through the stops. **All releases** disables collapsing; **Include RCs** refetches with `include_rc=true` and shows how many pre-release tags the branch has (`rc_count`); it is disabled when the branch has none. Without it no pre-release tag (`-rc`, `-pre`, ...) is listed. The stop at or before the version the history was opened from is selected first.
  - **Detail**: status, `prev → version` range and line counts; **Diff** (unchanged runs longer than 3 lines folded) or **Code** of each definition. Kconfig stops list only the files whose definition changed (**Show all N definitions** toggles) with a table of the changed properties (`depends on`, `inherited depends`, `select`, `default`, `type`, `prompt`, `help`...). **Commits** loads `api.getHistoryCommits` for that definition (click a hash to open the commit); **Open @ version** opens the file at the definition.
- **Patch staging** (`components/patch_modal.js`): code toolbar **✎ Patch**, editor/explorer context menu, or palette. Edit the file in a textarea (Tab inserts a tab), set subject, description and identity (remembered in `localStorage`), then *Generate patch* → `api.formatPatch`; copy or download `0001-<subject>.patch`.
- **KConfig additions**: **Import .config**, **Validate** and **Compare…** run in the browser engine (see 5.8); the per-symbol **Dependency Graph** uses `api.getKconfigGraph` (own depends-on left, selects/implies right, colored by current value, nodes clickable).

### 5.18. Projects (Multiple Repositories)
- The header **Project** selector lists `/api/projects` (parsed repositories plus *git only* ones). `api.setRepository(name)` adds `X-KI-Repo` to every request, prefixes IndexedDB cache keys with the repository, and `withRepo()` appends `?repo=` to URLs used directly (images, downloads). Switching asks for confirmation, closes the workspace tabs (`state.setRepository`), and selects the repository's latest parsed version (else its default branch).
- The **Tag** dropdown lists base releases (in custom tag views, or on real Git branches without a branch scope) grouped into *Parsed* and *Git only (not parsed)*; a release that so far only has candidates appears as `vX.Y (rc only)` and selects its latest candidate. The **Release** dropdown (shown when the selected release has sub-versions or candidates) lists stable point releases descending (newest first), `final` (base release), plus release candidates (`-rc#`), newest first, with `(git only)` marking unparsed ones; `KernelIdeApp.buildVersionCatalog()` groups tags by active `tag_views` regex rules (e.g. `releaseOf()` for `v3.0.1` → `v3.0`, `v2.6.16.62` → `v2.6.16`) from `/api/versions?include_rc=true`. Catalog requests (`projects`, `branches`, `versions`) are network-first so newly parsed versions appear without clearing the cache.

### 5.19. Definition Context & Picker (`state.definitionContext()`, `webapp/frontend/js/utils/definition_picker.js`)
- **Context**: `state.definitionContext(engineTabId?)` returns `{arch, assignments, strict, engineTabId}`: for a running engine with a resolved configuration (`state.kconfigConfigActive(engineTabId)`), that engine's ARCH and resolved values (`strict`: absent symbols are `n`); otherwise `x86_64` with a baseline (`X86`, `X86_64`, `64BIT` = `y`, `X86_32` = `n`; `strict: false`, every other symbol unknown). Called without an argument it uses the engine the **active tab** follows (`state.activeEngineTabId()`: a KConfig tab's own engine, or a code tab's selected one, §5.16), which is what the context menu, the definition picker and `api.getSymbolDetail`/`getSymbolXref`/`getSymbolHistory` go by (they send its `arch`, §2.7); `VirtualEditor` passes its own tab's engine explicitly, so a file dims and reports its build condition against the engine it follows even while another tab is active. `state.kconfigArch(engineTabId?)` is the same resolution for the ARCH alone, except that a tab which cannot follow an engine (Compare, documentation, NodeMap) falls back to any running engine (`state.fallbackEngineTabId()`), so opening a Kconfig symbol from there lands on the ARCH being configured.
- **Picker**: `resolveDefinition(version, name, fromFile)` takes the backend's ranked `definitions` (with `fromFile`, those the file's `#include` chain reaches come first, §2.7 item 4) and, among those in the first 6 candidate files, returns the first one the context compiles: files whose Kbuild condition evaluates to `n` (`api.getBuildConditions`) are skipped, then definitions on lines `DirectiveEvaluator` compiles out with the context's assignments and `archMacros` (partial when not strict). On x86_64, `struct pt_regs` therefore opens the `__x86_64__` branch of `arch/x86/include/asm/ptrace.h` rather than its `__i386__` one, and `arch/x86/kernel/process_64.c` wins over `process_32.c`. When no candidate is known to be compiled, the backend's first choice is kept. `openDefinition(version, name, {forceNew, fromFile})` opens the result in a code tab (used by the context menu and middle-click, which pass the code view's file as `fromFile`, and by documentation links).

### 5.20. Repository Explorer Tab (`webapp/frontend/js/views/explorer/explorer_view.js`, `webapp/frontend/css/explorer.css`)
- **Opening**: clicking any segment of the path in a code view's breadcrumbs (`.breadcrumb-dir`, including the leading revision, which opens the root) opens the directory in a full tab instead of revealing it in the sidebar; middle-click opens it in a second explorer tab. The header's **New Tab** button (§5.13) opens the root of the selected revision, the palette has *Open repository explorer tab*, and a commit chosen in the palette, in the commits view (*Explore Files at this Commit*) or in the file history modal opens its tree here.
- **Tab data** (`type: "explorer"`): `path` (the directory, `""` = root), `version` and `filter`; the listing's position is kept by §5.28 (`scrollPositions`), and moving to another directory clears it (`clearScrollPositions`). Two explorer tabs of the same directory and revision are the same tab (`state.openTab` matching), and the tab title is the directory's name. The URL hash carries it as `tab=explorer&dir=<path>&v=<ref>`.
- **Listing** (`GET /api/fs/tree/{v}?path=…`): directories first, then files (server-side order), each row with an extension icon, its name and either *directory* or the file's start-state label (`s_stat_label`). A filter box narrows the directory, **↑ Up** goes to the parent, **Reveal** reveals the directory in the sidebar explorer (`explorer:reveal`), **Treemap** opens its treemap (§5.17), and the status bar states how many entries are listed, how many are directories, and the path and revision they come from.
- **Navigation stays in the revision**: a directory opens in the same tab (middle-click: a new explorer tab) and a file opens in a code tab **at the explorer's revision** (middle-click: a new one), so browsing a second revision never repoints the tabs of the first. Right-clicking a row opens the shared explorer context menu (`contextMenu.showFileTreeMenu`) with that revision.

### 5.21. Code View Toolbar, Saved Position & Minimap (`webapp/frontend/js/views/code_view/virtual_editor.js`)
- **Toolbar**: breadcrumbs (every segment opens the repository explorer, §5.20) then the `#ifdef` chip (§5.16), the diff-marks chip (§5.12), the last-changed chip, **⚙ `<engine>` ▾** (which KConfig engine this file follows, §5.16), **📜 History** (`showFileHistoryModal`), **⇄ Diff** (below), **Blame** (`BlameView`, whose author links and commit jumps use the revision the blame was loaded for, and whose commit hashes and gutter indicator borders are color-differentiated by author to make change transitions immediately evident), **Copy File** and **✎ Patch**.
- **⇄ Diff: the file against any file at any revision** (`webapp/frontend/js/views/code_view/file_diff.js`, `VirtualEditor.enterFileDiff`):
  - **Picker** (`showDiffPicker`): a **Revision** button opening the shared `refPicker` (§5.13, unscoped; default: the tag preceding the tab's revision, `previousTag`, the rule the Compare view uses for its base, read from `api.getVersionsCached()` so opening the picker sends no `/api/versions` request), a **File** box (default: the same path; suggestions from `api.searchFiles` *at the picked revision*), **Quick picks** (*This file @ previous tag*, and every other open code tab's file at its revision) and *Picked file is the target* (swap). **Show diff** / Enter applies; Cancel, Escape or a click outside changes nothing.
  - **Direction**: by default the picked file is the **base** and the tab's own file the **target**; swapped, the tab is the base.
  - **Full diff in place**: `api.getBlobDiff` (`GET /api/tools/diff/blobs`, cached as `bdiff_*`) returns only the changed lines; `buildDiffRows` rebuilds every line of both files from those hunks and the text the tab already shows (rows `{k, o, n, t, s}`, `s` = the tab's own line), so a 10k-line file costs only its changes on the wire. The viewport stays virtualized over the diff rows, and the minimap draws them with a green / red bar per added / removed row.
  - **Layouts**: **unified** (one column: old and new line numbers, `+`/`−` sign) or **side-by-side** (`pairDiffRows`: base left, target right, the removed and added lines of a block paired row by row, filler rows hatched); toggled with ☰/◫ in the chip and kept on the tab (`diffLayout`). The minimap marks a changed pair amber.
  - **Chip** `⇄ <base> → <target> +A −D n/N ☰|◫ ↑ ↓ ⇆ ✕`: sides read `v2.6.39` for the same path or `v3.0:kernel/sched_fair.c` for another file; ↑/↓ jump between change blocks (`jumpToDiffHunk`; the counter reads `–/N` until the first jump, then `n/N`, in a fixed width of `2·digits(N)+1` characters so the chip never shifts while navigating), ⇆ swaps base and target, ✕ returns to the plain file at the line the diff was showing. Identical files show *identical*, a diff past the server's limits *⚠ truncated* (its rows stop after the last loaded change); binary files and errors (missing path, directory, unknown revision) are reported by a toast and leave the tab as it was.
  - **What still works**: rows that are lines of the tab's own file keep syntax highlighting, AST tokens, `#ifdef` dimming, the context menu and middle-click, with their real line number (`data-line-no`); the other file's lines are plain text. Blame is disabled and the commit diff-marks chip hidden while a diff is shown (both annotate the plain file). Copying a multi-line selection copies what is shown instead of the raw buffer (`FidelityClipboard` gets no lines in diff mode).
  - **State**: the tab records `diffWith: {version, path, swap}` and `diffLayout` (`state.updateTabState`, replace-only: the URL changes without a new history entry), so the diff survives tab switches, re-renders and reloads, and the hash carries it (`dv`, `dp` when the path differs, `ds=1`, `dm=split`; §5.26). A code deep link without `dv` addresses the plain file, so a reused tab drops its diff (`state.openTab`); a link that changes the diff drops the tab's saved scroll position. Entering, swapping or leaving a diff keeps the reader at the same line of the own file. No other state changes, so §5.0 needs no confirmation.
- **Saved position**: the viewport's `scrollTop` is kept on the tab (`tab.scrollTop`, written on scroll and persisted debounced), so selecting the tab again — which reloads and re-renders the file — comes back exactly where it was left. Opening an already open file *at a line* (`cursorLine`, e.g. from a definition, an XRef or a commit diff) drops that saved position (`state.openTab`), so navigation still wins over it; applying another revision to the tab drops it too. The viewport is marked `data-scroll-restore="off"`, so the generic tab scroll memory (§5.28) leaves it to this logic, and its saves go through the same debounced `scheduleScrollSave()`; every other scrollable part of a code tab (the rendered documentation viewport and its sidebar) is kept by §5.28.
- **Minimap to scale**: `minimapMetrics()` maps the document (`lines × 20px`) onto at most one pixel per line, so the drawing is exactly as deep as the file is long and the track below the last line is shaded rather than pretending to be content. The slider's height is the share of the document the viewport shows and its travel is `mapHeight − sliderHeight`, so at the bottom of the file its lower edge meets the last line instead of running past it (it is also looked up inside the tab's own container, so split panes no longer share one slider). Clicking or dragging the minimap scrolls to that position, and a `ResizeObserver` on the editor body redraws it when the pane, the window or the sidebar changes its size.

### 5.22. Mailing List & LKML Discussions Tab (`webapp/frontend/js/views/mail/mail_view.js`, `webapp/frontend/css/mail.css`)
1. **Activity Bar & Navigation**:
   - The Activity Bar features a dedicated **Mailing List** envelope icon (`data-action="mail"`) opening or focusing a singleton `mail` tab (`[L]` badge in tab strip).
   - Bi-directional URL synchronization via `#tab=mail&tid=<threadId>&mid=<messageId>&q=<query>&f=<filter>&mode=<search|tree>`: the query, the filter chip, the left-column mode, the open thread and the focused message are all in the hash, so copying the URL reopens that same search with that same email open (§5.26).
   - Command palette mode `"m"` (`m <search query>`) searches LKML discussions directly from any tab.
2. **Integrated Two-Column Architecture**:
   - **Left Column (two modes, `.mail-list-switcher`)**: a segmented switcher (`🔍 Search` / `🌲 Thread Tree`) swaps the two panes (`#mail-search-pane`, `#mail-tree-pane`; the inactive one carries `hidden`). The mode is kept on the tab (`tab.listMode`) and in the URL (`&mode=tree`), so a reopened or reloaded tab comes back in the mode it was left in. The offline banner stays above both panes.
     - **Search mode** (`#mail-search-pane`):
       - Real-time debounced typeahead search input (`s:`, `f:`, `m:` prefix support).
       - Filter chips (`All`, `Patches`, `Cover Letters`, `Pull Requests`) with categorical badge highlights.
       - Scrollable results roster with pagination (`Load More (N of Total)`): each click raises `currentPage` and merges the next page into `results` (deduplicated by `message_id`/`num`). The control is enabled whenever no request is in flight — `isLoadingSearch` is cleared *before* `renderResultsList` draws it, so the button never renders permanently stuck on `Loading...` — a click while loading is ignored, and a page that adds no new message retires the control (`totalResults` clamped to the rows actually held, `currentPage` rolled back), since the archive's `total` is a lower bound (§4.8).
       - With no query typed, the roster is the chronological browse listing and **excludes messages dated in the future** (§4.8); typing a query that matches them brings them back.
       - Middle-click multi-tab opening: middle-clicking any search result opens the thread in a new standalone tab.
     - **Thread Tree mode** (`#mail-tree-pane`, replaces the former mini timeline outline):
       - **Revision picker** (`.mail-version-chips`): one chip per revision of the series (`v1`, `v2`, ... each with its message count), from `GET /api/mail/versions` (§4.8). The chip of the revision on screen is active; choosing another loads that revision's thread (`inspectThread` on its `tid`, focused on its cover letter) and redraws the tree, so a multi-version patch series is navigated without going back to search. A single-revision series states so instead of showing one chip; nothing is shown when no thread is open.
       - **Reply tree** (`buildThreadTree`): the hierarchy is derived from each message's `References` chain — the closest reference that is present in the thread and is not already a descendant becomes the parent (cycle-safe), and a patch with no resolvable parent attaches to the series cover letter. Roots are ordered chronologically, as are the replies under each node.
       - **Expandable nodes**: a node with replies carries a twisty (`▸`/`▾`) and a `↳ N` count of the messages below it; collapsed replies are revealed by expanding it. Root rows and the branch holding the focused message open by default, and an explicit expand or collapse is remembered for that thread (`treeOpen`). Each row shows its patch badge (`0/N`, `i/N`, `PULL`, `RE`), the subject with its reply prefixes and bracket tag stripped, the author, the date and the bracket tag itself.
       - **Selection**: clicking a row expands and scrolls to that email card in the conversation column (`focusMessageCard`) and records it as the tab's `mid`; middle-clicking opens that message in a new standalone mail tab. When one thread carries several revisions, the tree shows only the branches of the selected revision.
   - **Right Column (Conversation & Patch Viewer)**:
     - Top thread summary card showing root subject, message count, participant count, and external `lore.kernelinfo.org ↗` link.
     - **Thread & Card Scroll Containment**: Enforces strict flexbox containment (`min-height: 0; min-width: 0; flex: 1 1 0%`) on `.tab-content-container`, `.mail-detail-col`, `.mail-thread-wrapper`, and `.mail-messages-container` with `flex-shrink: 0` on individual email cards (`.mail-card`). This guarantees that multi-thousand-line emails, large patch series, and raw RFC-822 payloads scroll vertically in their entirety without clipping.
     - **Collapsible Message Cards**: Accordion-style email cards with author avatar/name, email, timestamp, and status badges.
3. **Beautified & Raw Dual-Mode Viewing**:
   - Segmented toggle (`Beautified` / `Raw`) on every message card:
     - **Beautified Mode**: Formatted in monospace (`font-family: var(--font-mono)`) with `white-space: pre-wrap; word-break: break-word` to preserve indentation of kernel code snippets, ASCII diagrams, and stack traces; indented and tinted quote blocks (`> ...`), highlighted trailer lines (`Signed-off-by:`, `Acked-by:`, `Reviewed-by:`), collapsible RFC-822 header drawer, and structured interactive diff viewer.
     - **Raw Mode**: Monospace view of the unparsed RFC-822 / git-am formatted message with horizontal scrolling for long lines.
     - **Instant Cached Re-rendering**: Cached message details in `loadedMessageDetails` are immediately rendered whenever an email card opens or mode toggles, avoiding infinite loading spinners when switching modes or expanding cards.
   - Action controls: `📋 Copy` RFC-822 text to clipboard, `⬇ Download` `.eml` / `.patch` file, and direct `Lore ↗` permalink.
   - **Archive host**: every outbound permalink points at the self-hosted public-inbox mirror `https://lore.kernelinfo.org/all/` (a clone of `lore.kernel.org`), defined once per side as `LORE_BASE_URL` in `webapp/backend/services/mailing_list_service.py` (reused by `webapp/backend/services/git_service.py` for commit linkage) and in `webapp/frontend/js/views/mail/mail_view.js`. `Link:` trailers inside upstream commit messages are untouched: they are historical kernel data pointing at `lore.kernel.org`.
4. **Interactive Patch Viewer**:
   - Unified diff hunks are parsed into structured file cards showing path, old/new line numbers, additions (`+N`), and deletions (`−N`).
   - File cards feature a 1-click **Open ↗** button that opens the modified file directly in the Virtual Code Editor at the modified line.
   - Clicking individual hunk diff rows navigates to the exact line in the code view.
5. **Caching & Offline Resilience**:
   - IndexedDB store (`localDB.mail` under DB version 4) acts as both an active read-through cache (preventing repetitive network and SSH tunnel requests for immutable emails) and an offline fallback when network connectivity is lost. Search results, threads and revision lists (`versions`) are cached; failed or empty mail payloads never are.
   - Automatically displays an inline warning banner with a **Retry** button when the daemon or SSH tunnel is offline, smoothly rendering cached conversations.
6. **Commit View Integration**:
   - Commit detail cards display linked mailing list discussions (`Patch`, `Cover Letter`, `Pull Request`) with tier tags (`Trailer`, `Patch-ID`, `BlobHash`, `MetadataFallback`).
   - Clicking any linked email opens the Mailing List tab focused on that specific thread and message.
   - Incorporates an on-demand **Auto-resolve from LKML** action for unlinked commits, resolving against the micro-daemon over SSH tunnel dynamically without modifying MySQL tables.

### 5.23. Multi-Window Workspace Sessions & Cross-Tab Coordination (`webapp/frontend/js/session_manager.js`, `webapp/frontend/js/components/session_modal.js`)
1. **Window-to-Session Isolation & Cloned-Storage Protection**:
   - `sessionStorage` pins each individual browser window/tab to an active workspace session (`sessionStorage["kip_active_session_id"]`), while full session documents are stored under `localStorage["kip_session_<id>"]` and indexed in `localStorage["kip_sessions_index"]`. A session document holds the panes, tabs and engine descriptors only (KConfig configurations are shared, §5.8). `state.saveState()` coalesces the saves of a burst (`SAVE_STATE_DELAY_MS`, 300 ms: every tab switch, tab-state update and scroll pause asks for one) and `state.flushState()` writes a pending one now: on `pagehide`, when the page is hidden, before another session is adopted or the current one duplicated, and before a tab is transferred. Full Local Reset suspends persistence (`state.suspendPersistence()`) so nothing is written back after the wipe.
   - **Cloned-Storage Protection**: Modern browsers duplicate `sessionStorage` when a tab is duplicated, opened via `target="_blank"`, or created with `window.open`. To prevent new tabs from silently inheriting and overlapping with the parent tab's session, `SessionManager` assigns a unique Window ID (`window.name = "kip_win_<id>"` matched against `sessionStorage["kip_window_id"]`). If a mismatch is detected on startup, the cloned session pin is cleared and the Session Picker modal is presented.
   - **Refresh (F5) Seamlessness**: When a window reloads, its `window.name` matches its `sessionStorage["kip_window_id"]`, seamlessly rehydrating its pinned session without prompting.
2. **Distinct Session IDs & Duplication**:
   - Every session receives a distinct, human-readable base-36 timestamped identifier (`sess_<time36>_<rand>`, e.g. `sess_m2k8x3w_9a2f1c`).
   - Sessions can be cloned via `sessionManager.duplicateSession(sessionId)`, allowing users to branch off an existing workspace's open tabs and split panes into a new independent session without state overlap. The copy is read with `sessionManager.readSession(sessionId)` (not `loadSession`, which pins — and therefore claims — the original), and its tabs get **fresh ids** (`SessionManager.remapPaneTabIds`): tab ids identify engines across windows (§5.23.5), so a copy keeping them would announce its engine under the original's id and its code tabs would follow the original's engine. `pane.activeTabId` and every `tab.kconfigEngineTabId` pointing at a copied tab are remapped with them, while one following an engine of another session is left alone.
   - **The engines come with the copy**: `kconfigEngineTabIds` and `kconfigEngineProfiles` are carried over under the remapped ids, so duplicating a session whose KConfig tab resolves a configuration gives a copy that resolves it too, instead of one whose engine is off. `createSession({kconfigEngineTabIds, kconfigEngineProfiles})` takes them for every session built around tabs that already exist (duplication, drag-out elevation, §5.24.3); the values themselves are not copied — they are shared under `profileKey` (§5.8) and rehydrated by `adoptSession`.
3. **Session Picker Modal (`showSessionPickerModal`)**:
   - When an unassigned window is opened and saved sessions exist in the registry, the modal is presented (`allowClose: false` on startup, `allowClose: true` on manual switch).
   - Displays session cards with active window badges, timestamps, revisions, repository badges, explicit session IDs, and tab preview chips (`type` + `title`).
   - Real-time cross-tab presence tracking tags sessions currently open in other tabs with an `"Open in another tab · Open takes it over"` warning badge (its tooltip points at **Duplicate** for working on a copy instead).
   - **Exclusive ownership — taking a session over**: a session is open in **one window at a time**, so **Open** on a session already in use elsewhere simply takes it: no confirmation is asked and nothing overlaps. Pinning the session broadcasts the claim (`session:takeover`, below), the window that held it is signed out of it on the spot — it stops writing to that session, the picker opens there with `allowClose: false` and a toast says the session continues in another window — and this window gets a toast naming the session it took over. Nothing is lost: the session is the same record either way, and **Duplicate** branches it into an independent session instead (§5.23.2).
   - Actions to **Open**, **Duplicate**, **Delete** (with confirmation), or create a **➕ New Session**.
4. **Header Session Controls**:
   - The top header navigation features a dedicated `.session-selector-container` with `#session-picker-btn`.
   - Displays the current session's human-readable name with a caret dropdown and tooltip with session name and ID.
   - Dropdown menu header displays full session ID (`ID: sess_...`) with click-to-select styling.
   - Dropdown menu actions:
     - **🗂 Switch Session…**: Opens the Session Picker modal.
     - **➕ New Session…**: Prompts for a session name, provisions a clean session with distinct ID, and switches to it.
     - **⧉ Duplicate Session…**: Clones the current session and tabs into a new session instance.
     - **✏️ Rename Session…**: Updates the session's name locally and broadcasts the change across tabs.
     - **🗑 Close Session**: Prompts confirmation, deletes the session from `localStorage`, unpins the window, broadcasts closure remotely, and re-opens the Session Picker modal.
5. **Cross-Tab Real-Time Coordination (`BroadcastChannel("kip_multi_tab")`)**:
   - Synchronizes running Kconfig engines, live constraint assignments, active window presence, and session lifecycle events across windows without polling.
   - Messages: `kconfig:request_engines`, `kconfig:engine:announced`, `kconfig:engine:revoked`, `kconfig:assignments:broadcast`, `session:renamed`, `session:closed`, `session:takeover`, `window:heartbeat`, `window:request_active`, `window:unloading`, the tab-transfer trio `tab:drag:start`, `tab:drag:end`, `tab:transfer:accepted` (§5.24.4), and the directed `tab:push` (§5.24.5).
   - **Exclusive session ownership** (`session:takeover`): `sessionManager.pinSessionToWindow()` always ends in `claimSessionExclusively(sessionId)`, so *every* way of entering a session (picker **Open**, `?session=<id>`, a new or duplicated session, F5 rehydration) claims it. The claim is unconditional rather than conditional on presence, so it holds even when the other window's heartbeat has not been seen. A window receiving a claim for the session it holds calls `unpinSessionFromWindow()` (which also posts `window:unloading`, retiring its presence and the engines it was announcing) and emits `session:taken_over`; `GlobalState` clears `currentSessionId`/`currentSessionName` — stopping every further write to a session this window no longer owns, since a save only calls `saveActiveSession()` while a session is pinned and the pending (coalesced) save is dropped rather than written (`unpinSessionFromWindow({flush: false})`, as for a deleted session) — and re-emits `session:taken_over`, on which `KernelIdeApp` re-labels the header and opens the Session Picker with `allowClose: false`. A window ignores its own claim (`winId`).
   - **Presence heartbeat**: `broadcastWindowHeartbeat()` runs on startup, on every pin **and every 10s** (`HEARTBEAT_INTERVAL_MS`), because `getActiveWindowsList()` prunes entries older than 30s; without the interval a window open for half a minute looked gone to the others and the picker stopped reporting which sessions are in use elsewhere.
   - **Remote engine registry** (`sessionManager.remoteEngines`, keyed by the engine's tab id): `kconfig:engine:announced` carries the announcing window's `winId` alongside its session, so an engine is dropped again when its window goes away (`window:unloading` → `dropRemoteEnginesOf`, matched on `winId`) or when its session is closed elsewhere (`session:closed`). **Only the window that announced an engine may retire it**: `kconfig:engine:revoked` carries the revoking `winId` and a registry entry is dropped only when it matches, because a tab transferred to another window (§5.24.4) runs its engine there under the same tab id — the window it left revokes that id just after the new one announced it, and an unscoped revoke would drop an engine that is still running. **Switching session also retires that window's engines** (`adoptSession` revokes every engine of the session being left before adopting the next one), so no code tab keeps dimming against a configuration nobody resolves any more. Dropping one emits `kconfig:remote_engines:updated`, which `GlobalState` re-emits as `kconfig:engine:changed` with `remote: true` (below) so every code tab following that engine falls back instead of dimming against a configuration nobody resolves any more.
   - **Live assignments**: `kconfig:assignments:broadcast` carries the engine's full descriptor (`title`, `version`, `arch`, `configName`, `profileKey`) with the values, not just the values, because the KConfig tab may have moved to another ARCH, revision or configuration name since it announced itself; the receiving window updates its registry entry from it, so a code tab in another session re-dims against the right ARCH on every edit. A broadcast for an engine this window never heard announced (its window was opened later, or the engine was enabled before this window had a session) answers with `kconfig:request_engines` instead of being dropped, so the engine registers and the next edit arrives as an update.
   - **`remote: true` on `kconfig:engine:changed`**: an engine of *another* session appearing or going away changes what the code tabs here can follow, but no KConfig tab of this window gained or lost its own engine, so `KconfigView` ignores those events (it would otherwise re-run its engine-resolution confirmation, §5.0, on every announce from another window) while `VirtualEditor` acts on them like any other engine change.

### 5.24. Universal Tab Drag-and-Drop, Pane & Cross-Session Transfers, Drag-Out Elevation & Tab Menu (`webapp/frontend/js/components/tabs.js`, `webapp/frontend/css/tabs.css`)
1. **Intra-Pane Tab Reordering & Scroll Preservation**:
   - Tabs carry `draggable="true"` and register `dragstart`, `dragend`, `dragover`, and `dragleave` event handlers.
   - While dragging over a tab strip, mouse coordinate tracking calculates the midpoint of each tab to position an animated visual drop indicator line (`.tab-drop-indicator`).
   - Dropping within the same pane reorders `pane.tabs` in `state.panes` and emits `tabs:changed`.
   - **Tabstrip Horizontal Scroll Preservation**: Switching or clicking tabs maintains the `.tabstrip` and `.tab-list` horizontal scroll position (`scrollLeft`) across DOM renders per pane, restored both synchronously and via `requestAnimationFrame` to prevent the tabstrip from resetting its horizontal scroll offset to 0.
2. **Inter-Pane Tab Transfers**:
   - Tabs can be dragged and dropped between different split panes (e.g. from `pane-1` to `pane-2` or vice-versa).
   - Dropping onto the target pane's tab strip inserts the tab at the hovered index; dropping onto the pane's content container appends it to the target pane.
   - Transfer preserves tab metadata, cursor line, scroll position, and view state. If the tab was running a Kconfig engine, engine ownership transfers with it.
3. **Drag-Out Elevation to New Browser Window**:
   - Dragging a tab outside the browser window boundaries (detected on `dragend` from `clientX`/`clientY`/`screenX` leaving the window) elevates the tab into a new browser window.
   - Creates a new workspace session pre-populated with the dragged tab, closes the tab in the source window, and launches the new browser window via `window.open("/?session=<newSessionId>", "_blank")`. Both the drag-out and the **Open on New Browser Tab** menu entry (§5.24.5) go through the same `elevateTabToNewWindow(tab, sourcePaneId)`.
   - **The engine travels with the tab**: a KConfig tab holding an engine is elevated *still holding it* — the new session is created with `kconfigEngineTabIds: [tab.id]` and that engine's descriptor (`version`, `arch`, `configName`, `profileKey`), and the new window's `adoptSession` rehydrates its values from the shared configuration under `profileKey` (§5.8). A configuration being resolved is therefore never switched off on the way into its own window.
   - **Elevation waits for a possible transfer** (`TRANSFER_GRACE_MS`, 250ms): another window of the same browser is "outside" this one too, so a drop that landed in another session (below) would otherwise *also* spawn a window. `dragend` defers elevation, and the `tab:transfer:accepted` message — which may arrive just after it — cancels it; a tab that is gone from `state` by then is not elevated either.

4. **Cross-Session Transfers Between Windows** (`state.tabTransferPayload`, `state.acceptTransferredTab`):
   - A tab is dragged **from the window of one session straight into the window of another** and dropped on its tab strip (at the hovered index) or on a pane's content area. Since a session is open in one window at a time (§5.23.3), two windows are what it takes to see both sides; the tab **moves**, it is not copied.
   - `dragstart` puts `state.tabTransferPayload(tab, paneId)` — the tab, `sourceWinId`/`sourcePaneId`/`sourceSessionId`/`sourceSessionName`, and for a KConfig tab `{engineActive, profileKey, version, arch, configName}` — into the drag as `application/kip-tab` **and** as `text/plain` prefixed `kip-tab:`, and announces it with `tab:drag:start`. `dragend` posts `tab:drag:end`. The announcement is the fallback for browsers that hand a custom data type only to the window the drag started in; `dragPayloadFrom` prefers the drag's own data, ignores drops carrying `Files`, and expires an announcement after 120s (`sessionManager.getIncomingTabDrag`).
   - A window with **no session pinned** (its Session Picker is up) refuses the drop with a toast instead of taking the tab: it writes nothing while unpinned (§5.23.5), so the tab would be lost. The source window closes its copy only once another window has said it took it.
   - The receiving window recognizes a foreign drag by `sourceWinId` (its own goes through `moveTabToPane`), inserts the tab with `state.acceptTransferredTab(payload, paneId, index)` and posts `tab:transfer:accepted`; the source window closes its copy of that tab on that message and both windows toast the move. The tab keeps its contents, cursor line and scroll position, gets a fresh id only if that id is already open here (a duplicate of the source session), and its revision becomes the window's through `syncVersionFromTab`.
   - **A KConfig tab arrives still running its engine**: `acceptTransferredTab` registers it in `kconfigEngineTabIds`/`kconfigEngines` under the descriptor it came with, re-reads that `profileKey` from the shared configuration and toolchain stores (this window may hold an older copy in memory), announces it to the other windows and emits `kconfig:engine:changed`, so the code tabs of the receiving session can follow it at once (§5.16). The values themselves never travel in the drag: they are shared across sessions under `profileKey` (§5.8).

5. **Tab Right-Click Menu: Revision Exchange, Elevate & Push to Another Session** (`attachTabContextMenu`, `applyCurrentTagToTab`, `getTabTag`, `pushTabToSession`, `acceptPushedTab`, `contextMenu.showTabMenu`):
   - Right-clicking a tab opens the universal context menu (§5.6) on that tab — whichever pane it sits in, and whether or not it is the active tab (only the pane is activated) — with these actions:
     - **⤓ Apply Current Tag (`<ref>`)**: moves *this* tab to the revision selected in the header (`state.selectedVersion`) — the header's **Apply**, aimed at the right-clicked tab rather than the active one. It goes through `requestVersionSwitch(target, tab)`, so a KConfig tab gets the same carry-over confirmation (§5.0) and nothing changes when it is dismissed. `state.setVersion(version, tab)` moves the explored revision (`currentVersion`, `version:changed`) only when that tab is the active one; a background tab is moved on its own (`tabs:changed`) and loads in the new revision when it is next shown.
     - **⤒ Get Tab Tag (`<ref>`)**: selects this tab's revision in the header (`state.setSelectedVersion`, `version:selected`). Like every header pick it only moves the selection: no tab is switched.
     - Both entries are disabled (`already on it` / `already selected`) when the tab's revision already equals the header selection.
     - **🗗 Open on New Browser Tab** (`moves it`): the same elevation as dragging the tab out of the window (§5.24.3) — a new session is created holding the tab, with the engine it was running, this window closes it, and the new browser tab opens pinned to that session.
     - **↪ Push to Another Session…**: a second menu lists every other saved session, each marked `🟢 open` when a window is holding it (`isSessionActiveInOtherWindow`) or `💤 saved` when none is; the entry is disabled when no other session exists. Choosing one moves the tab there. The current session is never listed.
   - **Live push**: `sessionManager.broadcastTabPush(payload, targetSessionId)` posts `tab:push` carrying exactly the drag payload (`state.tabTransferPayload`). The window whose `activeSessionId` matches emits `tab:push:received`, and `acceptPushedTab` takes the tab with `state.acceptTransferredTab` into its active pane and answers `tab:transfer:accepted` — so the push lands exactly as a cross-window drop (§5.24.4) does, down to the KConfig engine arriving still running and the source window closing its copy on the acknowledgement. A window with no session pinned takes nothing (§5.23.5).
   - **Offline push**: no window answers within `PUSH_ACK_GRACE_MS` (400ms) — because no window holds that session, or its presence entry was stale — so `sessionManager.pushTabToStoredSession(sessionId, payload)` appends the tab to the stored record's active pane as its active tab (fresh id if that record already holds the id), carries the KConfig engine descriptor into `kconfigEngineTabIds`/`kconfigEngineProfiles`, refreshes the session index (`tabCount`, `previewTabs`) and only then is the tab closed here. The session shows it the next time it is opened. Presence is therefore never trusted to decide where a tab goes: the live path is tried first and the stored write is the fallback, so a tab is closed here only once something has taken it.
   - The session is written (`state.flushState()`) before the payload leaves; a KConfig tab's values travel through the shared configuration store under `profileKey` (§5.8), written whenever they change, rather than in the payload itself.

### 5.25. Multi-Pane Split Editor Layout & Resizable Dividers (`webapp/frontend/css/layout.css`, `webapp/frontend/js/components/tabs.js`)
1. **Split Button Specificity & Layout Fix**:
   - Fixed CSS specificity issue where `#workspace { flex-direction: column; }` (ID specificity 0,1,0,0) overrode `.split-horizontal` (class specificity 0,0,2,0), which caused "Split Editor Down" to render in a horizontal row.
   - Scoped rules `#workspace.split-horizontal { flex-direction: row; }` and `#workspace.split-vertical { flex-direction: column; }` enforce proper orientation for "Split Editor Right" (side-by-side columns) and "Split Editor Down" (stacked rows).
2. **Cloned Tab Initialization & Focus**:
   - Transitioning from `single` to split mode (`split-horizontal` or `split-vertical`) clones the currently active tab into `pane-2` and immediately activates `pane-2` with focus, matching professional IDE mechanics.
   - Returning to `single` mode merges any open tabs from `pane-2` back into `pane-1` without closing them.
3. **Active Pane Indication**:
   - The active pane is styled with `.active-pane`, providing a subtle top accent bar and active tabstrip background.
4. **Draggable Divider Resizing**:
   - Split panes are separated by `.pane-divider` with dedicated horizontal (`row-resize`) or vertical (`col-resize`) handles.
   - Dragging the divider computes flex-basis proportions dynamically for smooth, real-time pane resizing.

### 5.26. Deep Links: the URL Hash as the Address of What a Tab Shows (`webapp/frontend/js/url_sync.js`, `state.updateTabState`)
The hash is not merely "which kind of tab is open": it carries the state inside the active tab that a reader would otherwise have to reproduce by hand, so **copying the URL reopens the same search and the same target**.

1. **Parameters per tab type** (`v` = revision and `tab` = type are always written; `q` is the search typed in that tab's own filter box):

| Tab | Hash |
| --- | --- |
| `code` | `#v=<rev>&tab=code&file=<path>&line=<n>&dv=<rev>&dp=<path>&ds=1&dm=split` (`dv`…`dm`: the **⇄ Diff** shown, §5.21) |
| `explorer` | `#v=<rev>&tab=explorer&dir=<path>` |
| `kconfig` | `#v=<rev>&tab=kconfig&arch=<ARCH>&cfg=<name>&sym=<SYMBOL>&q=<filter>` |
| `mail` | `#v=<rev>&tab=mail&tid=<threadId>&mid=<Message-ID>&q=<query>&f=<patch\|cover\|pull>&mode=tree` |
| `commits` | `#v=<rev>&tab=commits&cid=<40-hex hash>&q=<query>&lc=1` (`lc`: commit list folded, §5.12) |
| `diff` (Compare) | `#v=<rev>&tab=diff&a=<base>&b=<target>&st=<chips>&q=<filter>&rn=<similarity>:<dir>…` (Files) or `…&mode=kconfig&st=<chips>&q=<filter>` (Kconfig) |
| `maintainers` | `#v=<rev>&tab=maintainers&view=<developers\|credits\|patch>&sec=<sec_id>&person=<email\|id\|name>&q=<query>&role=<maintainer\|reviewer\|credits>&sort=alpha` (`sec` and `person` are exclusive; §5.10) |
| `nodemap` | `#v=<rev>&tab=nodemap` |
| `pahole` | `#v=<rev>&tab=pahole&struct=<struct_name>` (Pahole struct layout, cachelines & padding holes) |
| `callgraph` | `#v=<rev>&tab=callgraph&fn=<func_name>` (Bidirectional caller/callee callgraph) |

   Empty, default and absent values are left out (`f=all`, `mode=search`, `line=1`, `view=subsystems`, `role=all`, the Activity sort, an unnamed configuration, no selection), so the hash stays as short as what it actually addresses. `sym` is the bare symbol name (no `CONFIG_` prefix), `cid` is always the **commit hash**, never the integer `commit_id`, so a shared link survives a database rebuild, `st` lists the selected Compare chips (`added,modified,deleted,renamed,unchanged` in Files mode, `added,modified,deleted` in Kconfig mode, in that order), `rn` is repeated once per applied rename search in the order they were applied (empty directory = whole tree), and `mid` is the message's `Message-ID` (percent-encoded, compared normalized — unbracketed and case-insensitive).

2. **Reading a hash** (`applyHash`, from `init` on load and from `handleHashChange` on every `hashchange`): the revision is selected in the header and the tab is opened with those fields. An already open tab of the same identity is reused rather than duplicated (`state.openTab`: a KConfig tab is identified by revision + ARCH + configuration name, a mailing list tab by its thread and message, a commits tab by its title) and the deep link's view state — `query`, `symbol`, `commitId`, `listCollapsed`, `listMode`, `filter`, `threadId`, `messageId` — is merged into it, so the tab comes back on that search, that selection and that mode and not merely on the right kind of tab. Views restore it from their `tabData`: `MailView` re-runs the query with the filter chip and opens the thread focused on `mid`; `KconfigView` refills the filter box, applies the flat match list and inspects `sym` — a link carrying both keeps the filter instead of clearing it to reveal the symbol in the tree; `CommitsView` refills the search box, inspects `cid` and folds the list when `lc=1`; `DiffView` restores the revisions (a link without `a`/`b` keeps the tab's own), mode, chips and the filter of that mode, and re-runs the `rn` rename searches in order once the diff has loaded (each is cached, §5.11); `MaintainersView` restores the section, search, role pill and sort and inspects `sec` or `person` (a Maintainers link always carries all of `section`, `secId`, `person`, `role`, `sort`, empty when absent, so a reused tab drops a selection or filter the link does not have). Reopening a tab clears its saved scroll positions only when a navigation field actually changes; an empty value (`""`, `0`) counts as the same as an absent one, so re-applying the hash on reload keeps them (§5.28).

3. **Writing it & Browser History Navigation** (`updateHashFromState`, `history.pushState` / `history.replaceState`, `handlePopState`): the hash follows the **active** tab. Tab and revision changes announce themselves as `tabs:changed` / `version:changed`; a navigation *inside* a tab is announced as **`tab:state`** by `state.updateTabState(tabId, patch)`, which merges the patch into the tab, persists the session and emits — it is deliberately **not** `tabs:changed`, since that redraws the pane and would discard the very view that reported the change. A patch entry that is empty (`""`, `0`, `null`, `undefined`) deletes that key, and a patch that changes nothing emits nothing.
   - **Selective History Stack**: `url_sync.js` distinguishes discrete navigation hops from ephemeral in-tab filtering. Discrete events — switching active tabs, opening new tabs, or changing primary navigation keys (such as Pahole struct drilldowns `structName`, Callgraph function jumps `functionName`, Explorer directory hops `path`, commit selections `commitId`, mailing list thread/message selections `threadId`/`messageId`, or Kconfig symbol selections `symbol`) — capture a workspace snapshot and push a new browser history entry (`history.pushState(snapshot, "", newHash)`). Ephemeral in-tab micro-adjustments (typing into search/filter inputs, editor cursor line movements, scroll positions) and initial tab hydration (such as Compare resolving default base/target revisions upon initial render, marked via `{ replace: true }`) update the URL and snapshot in place via `history.replaceState()`, preventing redundant intermediate history states.
   - **Back / Forward Rewinding & Redaction** (`popstate`): Clicking the browser's Back or Forward arrow emits `popstate`, delivering the target history entry's workspace snapshot. `restoreSnapshot(targetSnapshot)` reconciles current state against the snapshot:
     1. *Tab Redaction*: Any tab open in the live workspace that was created after the target snapshot was captured is closed (`state.closeTab`), redacting newly opened tabs when navigating backwards.
     2. *Tab Reopening*: Any tab present in the target snapshot that was subsequently closed is reopened in its recorded pane.
     3. *In-Tab Rewinding*: Tabs whose navigation targets changed (e.g. drilling down into nested struct definitions in Pahole or navigating folders in Explorer) have their parameters rewound to the target snapshot's values and re-render.
     4. *Active Tab & Pane Focus*: Active pane and tab selection are restored, and `tabs:changed` renders the restored workspace state.
   Views record their own state through `persistTabState(patch)`, which ignores the call when the view's element is no longer in the document: a view replaced by a re-render is stale and its state is nobody's address any more. The recorded moments are: the KConfig filter box, the inspected symbol (tree row, flat match, badge, graph node), the ARCH switch (which also drops `sym`, a symbol of the ARCH being left) and the configuration name; the mailing list query, filter chip, left-column mode, opened thread and focused message; and the commits query, inspected commit and folded list; the Compare revisions, mode, chips, both filters and each successful rename search (the tab keeps `versionA`, `versionB`, `mode`, `statuses`, `query`, `kconfigStatuses`, `kconfigQuery`, `renameSearches`); and the Maintainers section, search, role pill, sort and inspected subsystem or developer.
4. **A link opened while no session is pinned** (`urlSync.defer()` / `urlSync.init()`): a link opened in a new browser tab arrives with its address in the hash, but that window has no session yet, so the startup Session Picker (§5.23) comes up first. Adopting a session **replaces the panes** with that session's saved tabs and then rewrites the hash from the active one, so applying the address before that choice would open the tab into panes that are about to be discarded — the browser tab would appear never to open at all. `app.init` therefore **holds** the address (`defer()`) instead of calling `urlSync.init()` at boot: while held, `hashchange` only updates what is pending and `updateHashFromState` writes nothing, so neither the picker nor the adoption can clobber it. The hold is released as soon as a session is adopted — on `session:adopted`, whichever path adopted it, and from the picker's `onSelect` — and the held address is then applied **on top of** the adopted session's tabs, so the tab the link asked for opens inside that session. When a session is already pinned to the window no picker appears and `urlSync.init()` runs normally at boot.
5. **What is deliberately not addressed**: scroll positions (kept per tab in the session instead, §5.28), expanded tree nodes, open/collapsed email cards, the engine state of a KConfig tab and the pagination page reached — a deep link addresses *what* is shown, not how far the reader had scrolled through it. Only the active tab is in the hash; the whole workspace (every pane, tab and configuration) is restored from the session instead (§5.23).

### 5.27. Explorer Sidebar Collapse (`webapp/frontend/index.html`, `webapp/frontend/css/layout.css`, `webapp/frontend/js/app.js`)
The left **Repository Explorer** sidebar collapses to zero width so the workspace can take the whole window.

1. **Controls**: a chevron button at the top right of the sidebar header (`#sidebar-collapse`, inside `.sidebar-header`) collapses it. Its icon (chevron against a bar) is the **default icon of every collapse control** in the frontend: `COLLAPSE_ICON` in `webapp/frontend/js/utils/icons.js`, inside a `.collapse-btn` (`webapp/frontend/css/layout.css`), mirrored by `.collapse-btn.expand` for the control that unfolds the panel again (the commits list uses both, §5.12); the Activity Bar's Explorer button (`data-action="explorer"`) and the palette command *Toggle file explorer* toggle it, and are the way back once the sidebar — and with it its own button — is hidden.
2. **Single owner of the state** (`app.setSidebarCollapsed(collapsed)` / `app.toggleSidebar()`): every control routes through these, so the `#sidebar.collapsed` class, the Explorer button's `active` state, the collapse button's `aria-expanded` and the stored preference never disagree. `explorer:reveal` (§5.20) expands through the same call before revealing a directory.
3. **Animation**: `#sidebar` transitions `width` (`--transition-norm`) between `--sidebar-width` and `0`; collapsed, it also drops `min-width` and its border, and its header and content are `visibility: hidden` so nothing inside can stretch it back open mid-transition. The code view's `ResizeObserver` (§5.21) redraws the minimap at the new pane width.
4. **Persistence**: the collapsed state is a per-browser UI preference in `localStorage["kernel_ide_sidebar_collapsed"]`, applied on boot (`setupSidebarCollapse`), deliberately **not** part of a workspace session (§5.23) — it describes the chrome, not what the session is showing. Blocked or unavailable storage is caught: the sidebar still toggles, it is just not remembered.

### 5.28. Tab Scroll Memory (`webapp/frontend/js/components/scroll_memory.js`)
Every tab comes back where it was scrolled — after selecting another tab, after any re-render of the panes, and after closing the browser and reopening the session.

1. **Universal, no opt-in**: `TabsComponent.render()` calls `attachScrollMemory(contentEl, tab)` on each pane's content container right after the active tab's view is rendered into it. A capture-phase `scroll` listener records the position of **any** scrolled element inside it (lists, detail columns, inspectors, timelines, the container itself), so a view added later is covered without code of its own.
2. **Storage**: `tab.scrollPositions` maps an element key to `[scrollTop, scrollLeft]`. It lives on the tab, so it is persisted with the session's panes (`state.saveState`, §5.23) and travels with a tab dragged or pushed to another session (§5.24). Writes are debounced (`scheduleScrollSave()`, 400 ms) and a pending write is flushed on `pagehide` and when the page becomes hidden, so closing the window does not lose the last scroll. A position of `0,0` removes its key; at most 24 keys are kept per tab (least recently scrolled dropped first).
3. **Element keys**: the nearest ancestor `id` (inside the container) followed by `tag.firstClass:n` steps down to the element, `n` counting siblings with the same tag and first class (`""` alone is the container). Keys therefore find the same element in a fresh render of the same view.
4. **Restoration**: views fill their content asynchronously, so each saved position is applied as soon as its element exists **and** is tall/wide enough to take it, re-checked on DOM mutations and every 250 ms. Restoration also re-applies a position when the view replaces the element right after (cached data first, then the server's answer) and ends when every position is applied and the DOM has settled for 1.5 s, after 15 s (any position not yet reachable is then applied clamped), or **as soon as the reader interacts** with the tab (`wheel`, `pointerdown`, `keydown`, `touchstart`). While it runs, scroll events on the saved elements are not recorded: they come from the view loading (an emptied container clamped to 0) or from the restoration itself.
5. **When positions are dropped**: applying another revision to the tab (`state.setVersion`); reopening an existing tab onto something else through `state.openTab` (a different `cursorLine`, `symbol`, `threadId`, `messageId`, `query`, `listMode`, `filter`, `commitId` or Compare parameters); an explorer tab navigating to another directory (`clearScrollPositions`). In-tab selections (`state.updateTabState`) keep them: the reader is scrolling that very view.
6. **Opt-out**: a subtree marked `data-scroll-restore="off"` keeps its own position and is ignored here — the code view's virtualized line viewport (`tab.scrollTop`, §5.21), whose saved position must yield to a requested line.
7. **Not addressed**: canvas pan/zoom (Node Map, treemap) is not scrolling and is not kept; deep links still carry no scroll position (§5.26).

## 6. Using the Services from Python

`webapp/main.py` is only the server's entrypoint (it exports `app`). Scripts and tests use the service classes directly, one module per area under `webapp/backend/services/` (`FilesystemService`, `SymbolService`, `KconfigService`, `MaintainerService`, `GitService`, `PaholeService`, `CallgraphService`, `DiffService`, `history_service`, `mailing_list_service`), or the router functions under `webapp/backend/routers/`, whose parameters carry plain defaults (`Annotated[..., Query()]`) so they can be called without FastAPI:

```python
from webapp.backend.services.symbol_service import SymbolService
from webapp.backend.services.kconfig_service import KconfigService

detail = SymbolService().get_symbol_detail("v3.0", "copy_process")
model = KconfigService().get_tree("v3.0", "x86_64")
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
# Using webapp entrypoint (with CLI overrides; -c selects another config.json)
python3 webapp/main.py -c config.json --host 0.0.0.0 --port 8000 --no-reload

# Or via uvicorn directly (CONFIG_FILE selects another config.json)
uvicorn webapp.main:app --host 0.0.0.0 --port 8000 --reload
```

`-c` is passed on as the `CONFIG_FILE` environment variable before anything reads the configuration, so it also applies to the process uvicorn's reloader starts. Options left unset fall back to the `webapp` section of the configuration (`host`, `port`, `reload`).

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
The frontend client automatically falls back to IndexedDB (`KernelInfoDB` v4) whenever network operations fail or the server is temporarily offline:
- **`files` Store**: Cached file contents, AST tokens, directory trees, blame records.
- **`symbols` Store**: Cached symbol search results, definition records, XRef usage graphs.
- **`kconfigs` Store**: Architecture trees, defconfig mappings, dependency graphs.
- **`subsystems` Store**: Maintainer sections, developer profiles, CREDITS catalog.
- **`commits` Store**: Commit listings, commit details, structured diffs.
- **`mail` Store**: Public-inbox email messages, parsed patch diffs, thread message rosters, search result sets. Functions as an active read-through cache preventing duplicate network/SSH requests for identical emails, as well as an offline store.
- **`ui_state` Store**: Active layout tabs, open files, split-screen arrangements, editor cursor offsets.
