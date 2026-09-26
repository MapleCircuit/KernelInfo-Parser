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
│   │   ├── git_reader.py           # Persistent Git object reader (git cat-file --batch)
│   │   ├── filesystem_service.py   # Tree traversal, file maps, include resolver, treemap
│   │   ├── symbol_service.py       # Multi-table symbol search, XRefs, AST containers
│   │   ├── kconfig_service.py      # Defconfigs, Menuconfig hierarchy, constraint solver
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
│       └── tools.py                # Pahole layout, callgraph, semantic version diff
├── frontend/
│   ├── index.html                  # Semantic single-page application shell
│   ├── manifest.json               # Progressive Web App (PWA) manifest
│   ├── sw.js                       # Service Worker with Cache-First offline caching
│   ├── vendor/two.min.js           # Vendored vector schematic engine
│   ├── css/                        # Modular Design System tokens & layout
│   └── js/
│       ├── api.js                  # Frontend HTTP client with offline IndexedDB fallback
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
1. **Directory Path Filtering & Polymorphic Navigation**: In database queries on `m_file`, directory records have `ftype == 0` while regular files have `ftype != 0`. All file search, path lookup, and `#include` header resolution queries must explicitly filter `m_file.ftype != 0` so directories sharing names or paths with header files are never returned as file matches. File viewer endpoints (`get_file`) and frontend code views (`virtual_editor.js`) support polymorphic path navigation: opening directory paths returns child hierarchies (`entries`, `tree`) and renders interactive navigation tables/grids rather than returning 404 errors.
2. **Lexical Comment Prioritization & Directive Matching**: In dual-layer syntax and AST overlays (`webapp/frontend/js/views/code_view/ast_overlay.js`), lexical tokenizers prioritize matching block comments (`/* ... */`) and line comments (`// ...`) *before* general identifier regexes, rendering them as non-clickable `<span class="tok-comment">` elements to prevent comment tokens from leaking into `.ast-token` or triggering symbol context menus. Preprocessor directive matching on snippet-relative coordinate slices allows optional leading hashes (e.g. `^(\s*#?\s*define\s+)`) so directive keywords are styled as preprocessors (`tok-macro`) and never tagged as clickable symbol identifiers.
3. **Flat Coordinate Projection & Defensive Navigation**: Symbol detail backend endpoints (`webapp/backend/services/symbol_service.py:get_symbol_detail`) expose definition coordinate fields as flat scalars (`line: row["line_s"]`, `line_s: row["line_s"]`) at the root of the response payload alongside the nested `definition` record. Frontend jump-to-definition callers (`webapp/frontend/js/views/code_view/context_menu.js`) resolve coordinates defensively across `detail.definition?.line_s || detail.line_s || detail.line || 1` to ensure virtual editors navigate to and highlight the exact symbol definition line.
4. **Maintainer Query Sanitization & Parameter Preference**: Developer identity and maintainer endpoints (`webapp/backend/routers/maintainers.py`, `webapp/backend/services/maintainer_service.py`) support query parameter lookups (`/api/maintainers/person?email=...&version=...`) alongside URL path parameters. Frontend API clients sanitize patch trailers and email strings (stripping `<>`, quotes, and whitespace) and prefer query parameters when identifiers contain special characters (`@`, `<`, `>`, `/`) to prevent URL decoding and route matching failures.
5. **Context Menu Interaction Preservation**: Right-click context menus (`contextmenu`) across webapp workspaces (virtual code editor, file explorer sidebar, nodemap canvas) intercept default browser menus and offer context-adaptive actions (definitions, references, git blame popovers, command palette search, coordinate copying), while strictly preserving primary left-click interactions (inline AST token inspector popups, file navigation, node selection) without collision, event race conditions, or lingering ghost menus.
6. **Structured Cross-Reference Classification**: Symbol cross-reference backend services (`get_symbol_xref`) and frontend inspectors (`showXrefModal`) return and render both primary and multi-declaration definition locations (`definitions: [...]` with file path, line bounds, and AST declaration type) alongside categorized usage references (`references: [...]` with caller function, file, line, and typed badges `Call`, `MemberRef`, `TypeUsage`, `DeclRef`, `MacroExpansion`), providing direct 1-click jump navigation for both definitions and usages.
7. **Offline Status & Granular Storage Reset Controls**: Web application connection status badges (`#offline-badge`) anchor a settings popover detailing real-time network connectivity (`navigator.onLine`) and local storage breakdowns (IndexedDB cached file/symbol counts, active tabs), backed by granular reset controls: (1) clearing offline cache stores (`localDB.clearStore`), (2) resetting workspace tab sessions (`localStorage` and `tabs_state`), and (3) a confirmation-guarded full reset that wipes all IndexedDB stores (`localDB.clearAllStores`), clears `localStorage`, purges browser caches (`window.caches`), and reloads the application.

---

## 3. Database Schema & Underlying Tables

| Domain Area | Key Database Tables | Primary Role |
| :--- | :--- | :--- |
| **Versions** | `m_v_main` | Version identifiers (`vid`, `vname`) |
| **Filesystem** | `m_file`, `m_file_name`, `m_bridge_file` | File instances, paths, file types (0=Dir, 1=C, 2=Kconfig, 3=Rust, 4=Asm, 5=Maintainers, 6=Credits, 7=Raw), and change status |
| **AST & Spatial Maps** | `m_ast`, `m_ast_container`, `m_ast_include`, `m_bridge_tag`, `m_bridge_map`, `m_map_ast` | Relational AST nodes, preprocessor `#include` links, container hierarchies, and coordinate regions (`line_s`, `char_s`, `line_e`, `char_e`) |
| **Symbols & XRefs** | `m_symbol_def`, `m_symbol_ref` | Definitions and usage cross-references (calls, field access, type usages) |
| **Kconfig** | `m_kconfig_tree`, `m_kconfig_relation`, `m_kconfig_kbuild` | Hierarchical menus, architecture-scoped symbol dependencies, and compiled source mappings |
| **Maintainers** | `m_maintainer_section`, `m_maintainer_member`, `m_maintainer_person`, `m_maintainer_pattern`, `m_maintainer_file`, `m_credits_entry` | Subsystem sections, pattern rules, maintainers, reviewers, and historical `CREDITS` biographies |
| **Git & Commits** | `m_commit`, `m_bridge_commit_file`, `m_commit_trailer` | Commit metadata, modified files, multi-contributor trailers (Signed-off-by, Acked-by, Reviewed-by) |
| **System & State** | `m_db_instance` | Unique database instance hash (`instance_hash`, `created_at`) used by clients to detect database rebuilds |

---

## 4. REST API Endpoint Catalog

All API endpoints reside under the `/api` prefix. Both canonical version-scoped paths and backward-compatible alias routes are supported.

---

### 4.1. Version Catalog

#### `GET /api/versions`
Retrieve all available kernel versions indexed in the database.
- **Query Parameters**: None
- **Response**:
```json
{
  "total": 1,
  "versions": [
    { "vid": 1, "vname": "v3.0" }
  ]
}
```

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
- **Graceful Unindexed Fallback**: If a requested file exists in the Git repository but has no indexing row in `m_bridge_file` (or is a non-C file), the endpoint serves the raw file content with `tokens: []`, empty AST maps, and inferred lifecycle metadata rather than throwing a 404.
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
Resolve a preprocessor `#include <...>` or `#include "..."` directive to an authoritative git repository file path. Guarantees directory exclusion (`m_file.ftype != 0`) to prevent directory false-positives.
- **Parameters**:
  - `version` (query): Kernel version string (default `"v3.0"`).
  - `header` (query): Raw or cleaned include path (e.g. `"<linux/init.h>"`, `"linux/init.h"`, `"#include <linux/init.h>"`). Required.
  - `ast_id` (query): Optional AST Node ID of the include directive to check direct `m_ast_include` links.
  - `current_file` (query): Optional referring source file path (e.g. `"arch/x86/kernel/setup.c"`) to prioritize architecture-specific or relative header matches.
- **Resolution Strategy**:
  1. If `ast_id` is supplied, queries `m_ast_include` join `m_file_name`.
  2. Generates candidate search paths: relative to `current_file` directory, `header`, `include/{header}`, `include/uapi/{header}`, and `arch/{arch}/include/{header}`.
  3. Matches candidates against active non-directory indexed files (`m_file.ftype != 0`) in `m_bridge_file` / `m_file_name`.
  4. Falls back to physical repo jail disk verification.
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
Retrieve line-by-line git blame provenance annotations.
- **Parameters**:
  - `version` (query) / `version_name` (path): Kernel version string.
  - `path` (query or path): File path in repository.
- **Response**:
```json
{
  "path": "init/main.c",
  "lines": [
    {
      "line": 1,
      "commit_hash": "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
      "author": "Linus Torvalds",
      "date": "2005-04-16",
      "summary": "Linux-2.6.12-rc2"
    }
  ]
}
```

#### `GET /api/version/{version_name}/browse/{path:path}`
Polymorphic unified browse endpoint that automatically detects whether the target path is a directory (returning tree hierarchy) or a source file (returning content, tokens, metadata, and lifecycle history).
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `path` (path): Repository path (empty string for repository root).
- **Response**: Returns either directory entries or file content payload based on path classification.

#### `GET /api/version/{version_name}/references/{fid}`
Retrieve all incoming cross-file references targeting a specific file ID (`fid`), including `#include` references, Kconfig relations, and Kbuild Makefile linkages.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `fid` (path): Integer file ID.
  - `ref_type` (query): Optional filter (`"include"`, `"kconfig"`, `"kbuild"`, `"makefile"`, `"documentation"`).
- **Response**:
```json
{
  "fid": 684,
  "path": "include/linux/init.h",
  "total": 4,
  "breakdown": { "include": 4, "kconfig": 0, "kbuild": 0, "makefile": 0, "documentation": 0 },
  "references": [
    { "fid": 912, "source_file": "kernel/sched.c", "ref_type": "include", "ref_label": "Included By" }
  ]
}
```

#### `GET /api/fs/treemap/{version_name}`
#### `GET /api/version/{version_name}/treemap`
Generate a hierarchical squarified treemap data structure representing file and directory size distribution across the codebase for visual disk/complexity analytics.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `max_depth` (query): Tree depth limit (default 3).
  - `path` (query): Subdirectory prefix filter (default `""` for root).
- **Response**:
```json
{
  "name": "root",
  "path": "",
  "file_count": 35412,
  "children": [
    {
      "name": "drivers",
      "path": "drivers",
      "file_count": 18230,
      "children": []
    },
    {
      "name": "arch",
      "path": "arch",
      "file_count": 8940,
      "children": []
    }
  ]
}
```

#### `GET /api/fs/compile_commands/{version_name}`
#### `GET /api/version/{version_name}/compile_commands`
Export a Clang JSON Compilation Database (`compile_commands.json`) containing compilation command lines and include flags for all indexed C source files in the specified architecture.
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

---

### 4.3. Symbols, Cross-References & AST Inspection API

#### `GET /api/symbols/search`
#### `GET /api/symbols/{version_name}/search`
Search symbol definitions by prefix or substring across functions, variables, structs, enums, enumerator constants (`EnumConstant`), typedefs, and macros.
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
Fast typeahead symbol name suggestion endpoint.
- **Parameters**:
  - `version` / `version_name` (query): Kernel version string (default `"v3.0"`).
  - `q` (query): Symbol prefix.
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
      "line": 95,
      "type": "Call",
      "ref_type": 1,
      "ast_id": 1054
    },
    {
      "caller": "setup_arch",
      "file": "arch/x86/kernel/setup.c",
      "line": 842,
      "type": "Call",
      "ref_type": 1,
      "ast_id": 1289
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

#### `GET /api/tag/{tag_id}/timeline`
Retrieve cross-version code evolution history and diff tracking for a persistent tag.
- **Parameters**:
  - `tag_id` (path): Unique tag ID.
- **Response**:
```json
{
  "tag_id": 105,
  "total_versions": 1,
  "timeline": [
    {
      "vid": 1,
      "vname": "v3.0",
      "status": "Active",
      "code_snippet": "asmlinkage void __init start_kernel(void)"
    }
  ]
}
```

---

### 4.4. Kconfig & Architecture Defconfigs API

#### `GET /api/kconfig/{version_name}/tree`
#### `GET /api/kconfig/tree`
Retrieve the authentic scoped Menuconfig tree for an architecture. Performs authentic recursive source-inclusion traversal starting from `arch/<arch>/Kconfig`, inlining `source` and `rsource` declarations into enclosing menus. Attaches full boolean dependency expressions (`depends_on_expr`), default expressions with conditions (`defaults`), mutual-exclusion choice metadata (`choice`), and symbol types (`type`, `type_name`). Employs in-memory LRU tree caching (`_TREE_CACHE`) for zero-overhead retrieval (<0.001s latency).
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string (default `"v3.0"`).
  - `arch` (query): Target CPU architecture (e.g. `"x86"`, `"arm"`, `"arm64"`, `"mips"`, `"powerpc"`, `"sparc"`).
  - `include_tree` (query, bool): If `true`, returns the recursively assembled hierarchical tree under root categories (key `"tree"`). Default `false`.
  - `include_relations` (query, bool): If `true`, includes global `relations` and `reverse_relations` maps. Default `false`.
- **Response**:
```json
{
  "version": "v3.0",
  "arch": "x86",
  "total_nodes": 8564,
  "total_count": 8564,
  "nodes": [
    {
      "tree_id": 1,
      "parent_id": 0,
      "node_type": 1,
      "title": "General setup",
      "symbol_name": null,
      "depends_on": [],
      "depends_on_expr": null,
      "defaults": [],
      "selects": [],
      "implies": [],
      "choice": null
    },
    {
      "tree_id": 105,
      "parent_id": 1,
      "node_type": 3,
      "title": "64-bit kernel",
      "symbol_name": "64BIT",
      "type": 1,
      "type_name": "bool",
      "depends_on": ["!UML"],
      "depends_on_expr": "!UML",
      "defaults": [
        { "value": "y", "expr": null }
      ],
      "selects": [],
      "implies": [],
      "choice": null
    }
  ]
}
```

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}`
#### `GET /api/kconfig/symbol`
Retrieve comprehensive metadata for a Kconfig symbol, including definitions, prompt, dependencies, reverse selects, and compiled C files.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `symbol_name` (path or query): Kconfig symbol name without `CONFIG_` prefix (e.g. `"SMP"`).
- **Response**:
```json
{
  "name": "SMP",
  "type": "bool",
  "prompt": "Symmetric multi-processing support",
  "depends_on": "X86_LOCAL_APIC",
  "selects": ["USE_GENERIC_SMP_HELPERS"],
  "selected_by": ["MAXSMP"],
  "compiled_files": ["kernel/smp.c", "arch/x86/kernel/smp.c"]
}
```

#### `GET /api/kconfig/{version_name}/defconfigs`
#### `GET /api/kconfig/defconfigs`
List discovered defconfig files for an architecture.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `arch` (query): Target architecture (default `"x86"`).
- **Response**:
```json
{
  "version": "v3.0",
  "arch": "x86",
  "defconfigs": [
    { "name": "x86_64_defconfig", "path": "arch/x86/configs/x86_64_defconfig" },
    { "name": "i386_defconfig", "path": "arch/x86/configs/i386_defconfig" }
  ]
}
```

#### `GET /api/kconfig/{version_name}/defconfig`
#### `GET /api/kconfig/defconfig/content`
Retrieve parsed key-value assignments and raw content from an authentic defconfig file.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `defconfig` / `file_path` (query): Defconfig identifier or relative path.
  - `arch` (query): Target architecture.
- **Response**:
```json
{
  "version": "v3.0",
  "name": "x86_64_defconfig",
  "file_path": "arch/x86/configs/x86_64_defconfig",
  "arch": "x86",
  "bits": 64,
  "symbol_count": 892,
  "values": {
    "64BIT": "y",
    "SMP": "y",
    "NR_CPUS": "64"
  },
  "content": "#\n# Automatically generated make config: don't edit\n# Linux Kernel Configuration\nCONFIG_64BIT=y\nCONFIG_SMP=y\nCONFIG_NR_CPUS=64\n..."
}
```

##### Menuconfig TUI & Constraint Engine
- **Fixed Viewport Modal & Window Layout**: The curses terminal modal (`.menuconfig-tui-modal`) is styled with `position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: 9999; overflow: hidden;`. The inner flex body (`.tui-body`) uses `align-items: flex-start; padding: 24px 20px; box-sizing: border-box; overflow: hidden; min-height: 0;` to anchor the dialog window (`.tui-window`) at a stable, fixed distance from the top border of the window, independent of item count. The dialog window (`height: calc(100vh - 80px); max-height: calc(100vh - 80px);`) confines option scrolling strictly inside `.tui-menu-list` (`flex: 1; min-height: 0; overflow-y: auto;`), ensuring header titles, instructions, and footer action buttons remain anchored and visible without window jumping or downward scroll drift.
- **Authentic Hierarchical Navigation**: Flat tree nodes (`m_kconfig_tree`) are indexed by `parent_id`. The main menu displays authentic top-level categories (`parent_id === 0`). Submenu indicators (`--->`) are dynamically determined from child node counts (`nodesByParent.get(node.tree_id)`).
- **Navigation Controls**: `<Enter>` or `< Select >` navigates into submenus; `<Space>` toggles symbol configuration state (`[*]`/`[ ]`); `<Esc>` or `< Exit >` navigates up the menu hierarchy or exits cleanly back to the IDE view; `<S>` exports or saves configuration; mouse click selects an item and double-click enters/toggles.
- **Bidirectional Key Synchronization**: Client state (`state.kconfigAssignments`) and constraint solver (`KconfigEngine.propagate`) maintain synchronized entries for both bare symbol keys (`"64BIT"`) and `CONFIG_` prefixed keys (`"CONFIG_64BIT"`), with `KconfigParser.serialize` deduplicating output.
- **Kconfig Choice & Help Block Parsing**: `KconfigLexer` emits clean line-delimited `HELP_TEXT` tokens, and `KconfigParser` (`_parse_choice`, `_parse_menu`) explicitly parses and binds `help_text`. Choice blocks containing multi-line help documentation cleanly encapsulate child choices without line/token swallowing, preventing prompt overwriting (e.g. choice prompt being overwritten by the last choice's prompt such as `"LZO"`) or swallowing subsequent top-level symbols into pseudo-choice folders.


#### `GET /api/kconfig/graph`
#### `GET /api/kconfig/{version_name}/graph`
Retrieve the full architecture dependency graph for client-side constraint evaluation, dependency tree traversal, and visual DAG rendering.
- **Parameters**:
  - `version` / `version_name` (query or path): Kernel version string.
  - `arch` (query): Target architecture.
  - `symbol` (query): Optional root symbol filter.
  - `depth` (query): Max dependency expansion depth (default 2).
- **Response**:
```json
{
  "nodes": [
    { "id": "EXT4_FS", "label": "The Extended 4 (ext4) filesystem", "type": "tristate", "value": "y", "is_root": true }
  ],
  "edges": [
    { "source": "EXT4_FS", "target": "BLOCK", "type": "depends_on" }
  ]
}
```

#### `GET /api/kconfig/{version_name}/autosolve/{symbol}`
#### `GET /api/version/{version_name}/kconfig/autosolve/{symbol}`
#### `POST /api/kconfig/{version_name}/autosolve`
#### `POST /api/version/{version_name}/kconfig/autosolve`
Evaluate prerequisite dependency trees for blocked or unmet Kconfig symbols and compute the minimal set of required symbol toggles to satisfy all constraints. Supports both single-symbol GET queries and customized configuration POST payloads.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `symbol` (path, for GET): Target symbol identifier (e.g. `"SMP"`).
  - **Body** (`application/json`, for POST):
    ```json
    {
      "target_symbol": "EXT4_FS",
      "current_values": { "BLOCK": "n" }
    }
    ```
- **Response**:
```json
{
  "solution_found": true,
  "target_symbol": "EXT4_FS",
  "toggles_needed": [
    { "symbol": "BLOCK", "from": "n", "to": "y", "reason": "prerequisite" }
  ],
  "total_toggles": 1
}
```

#### `POST /api/kconfig/{version_name}/diff`
#### `POST /api/version/{version_name}/kconfig/diff`
Compare two configuration assignment sets (active vs custom/saved) to highlight modified, added, and conflicting symbols.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - **Body** (`application/json`):
    ```json
    {
      "active_config": { "CONFIG_EXT4_FS": "y", "CONFIG_BTRFS_FS": "n" },
      "custom_config": { "CONFIG_EXT4_FS": "y", "CONFIG_BTRFS_FS": "y" }
    }
    ```
- **Response**:
```json
{
  "matching_symbols": 1,
  "mismatched_symbols": 1,
  "differences": [
    { "symbol": "CONFIG_BTRFS_FS", "active": "n", "custom": "y" }
  ]
}
```

#### `GET /api/kconfig/{version_name}/presets`
#### `GET /api/version/{version_name}/kconfig/presets`
Retrieve discovered CPU architectures, canonical defconfigs, architectural symbol bindings, and compiler presets.
- **Parameters**:
  - `version_name` (path): Kernel version string.
- **Response**:
```json
{
  "version": "v3.0",
  "targets": [
    {
      "id": "x86_64",
      "label": "x86_64 (64-bit x86)",
      "arch": "x86",
      "srcarch": "x86",
      "kconfig_path": "arch/x86/Kconfig",
      "canonical_defconfig": "x86_64_defconfig",
      "bits": 64,
      "symbols": {
        "64BIT": "y",
        "X86_64": "y",
        "X86": "y",
        "X86_32": "n",
        "ARCH": "x86",
        "SRCARCH": "x86"
      }
    },
    {
      "id": "arm",
      "label": "ARM (32-bit ARM)",
      "arch": "arm",
      "srcarch": "arm",
      "kconfig_path": "arch/arm/Kconfig",
      "canonical_defconfig": "versatile_defconfig",
      "bits": 32,
      "symbols": {
        "ARM": "y",
        "ARCH": "arm",
        "SRCARCH": "arm",
        "64BIT": "n"
      }
    }
  ],
  "compilers": [
    { "id": "gcc", "label": "GNU Compiler Collection (gcc)", "symbols": { "CC_IS_GCC": "y" } },
    { "id": "clang", "label": "LLVM Clang (clang)", "symbols": { "CC_IS_CLANG": "y" } }
  ]
}
```

---

### 4.5. Maintainers & CREDITS API

#### `GET /api/maintainers/{version_name}/overview`
#### `GET /api/maintainers`
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

---

### 4.6. Git Commits & Patch Management API

#### `GET /api/commits/{version_name}/list`
#### `GET /api/commits`
Retrieve paginated commit history and contributor summaries.
- **Parameters**:
  - `version_name` (path) / `version` (query): Kernel version string.
  - `page` (query): Page number (default 1).
  - `limit` (query): Commits per page (default 50).
  - `q` (query): Filter by commit message, hash, or author.
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

#### `GET /api/commits/{version_name}/timeline`
Retrieve a chronological commit timeline along with author leaderboards and trailer summary metrics.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `limit` (query): Maximum commits to summarize (default 100).
- **Response**:
```json
{
  "version": "v3.0",
  "total_commits": 100,
  "timeline": [
    {
      "commit_hash": "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
      "author": "Linus Torvalds",
      "date": "2005-04-16",
      "subject": "Linux-2.6.12-rc2"
    }
  ],
  "top_authors": [
    { "author": "Linus Torvalds", "count": 45 }
  ]
}
```

#### `POST /api/commits/{version_name}/format_patch`
#### `POST /api/commits/format_patch`
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
      "author_name": "Kernel Developer",
      "author_email": "dev@kernel.org"
    }
    ```
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
Compare file changes across two releases.
- **Parameters**:
  - `v1` / `version_a` (query): Base kernel version (e.g. `"v3.0"`).
  - `v2` / `version_b` (query): Target kernel version (e.g. `"v3.1"`).
  - `path` (query): Optional directory prefix filter.
- **Response**:
```json
{
  "version_a": "v3.0",
  "version_b": "v3.1",
  "summary": {
    "added_count": 1284,
    "removed_count": 961,
    "modified_count": 7033,
    "unchanged_count": 31053
  },
  "changes": [
    { "type": "added", "file": "Documentation/ABI/stable/firewire-cdev", "file_path": "Documentation/ABI/stable/firewire-cdev", "fid": 41156 },
    { "type": "modified", "file": "Makefile", "file_path": "Makefile", "fid": 17858 }
  ],
  "added": [ { "file": "Documentation/ABI/stable/firewire-cdev", "fid": 41156 } ],
  "modified": [ { "file": "Makefile", "fid": 17858 } ],
  "deleted": []
}
```

#### `GET /api/tools/{version_name}/diff/kconfig`
#### `GET /api/version/diff/kconfig`
Compare KConfig symbol definitions and default values across two kernel releases.
- **Parameters**:
  - `version_name` (path): Context kernel version string.
  - `v1` (query): Base kernel version (e.g. `"v3.0"`).
  - `v2` (query): Target kernel version (e.g. `"v3.1"`).
- **Response**:
```json
{
  "v1": "v3.0",
  "v2": "v3.1",
  "summary": {
    "added": 12,
    "removed": 2,
    "changed": 5
  },
  "added": ["NEW_DRIVER_OPTION"],
  "removed": ["DEPRECATED_FEATURE"],
  "changed": ["MAXSMP"]
}
```

---

## 5. Frontend Client Architecture & Module Specifications

The frontend is a vanilla ES-Module single-page application requiring zero node/npm build steps.

```
webapp/frontend/js/
├── api.js                 # ApiClient: centralized REST fetch client with offline fallbacks
├── db.js                  # LocalDatabase: IndexedDB offline cache manager
├── state.js               # StateStore: reactive pub/sub event store with localStorage persistence
├── url_sync.js            # UrlSync: bi-directional hash router
├── app.js                 # Application bootstrap orchestrator
├── components/
│   ├── tabs.js            # Multi-pane split manager (single, horizontal, vertical)
│   ├── context_menu.js    # Context-adaptive menus (CodeView, Explorer, NodeMap, Commits)
│   └── toast.js           # Ephemeral status toasts
├── utils/
│   └── clipboard.js       # Universal fallback clipboard copier (navigator + execCommand)
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
    │   ├── kconfig_view.js       # Interactive Kconfig hierarchy explorer, search & dependency inspector
    │   ├── kconfig_engine.js     # 20-pass constraint propagation engine
    │   ├── kconfig_parser.js     # .config deserializer & serializer
    │   ├── menuconfig_tui.js     # Curses-style terminal TUI
    │   └── directive_evaluator.js# #ifdef conditional code dimming
    └── maintainers/maintainers_view.js
```

### 5.1. `ApiClient` (`webapp/frontend/js/api.js`)
Handles unified HTTP communication, response caching, and offline fallback:
- `getVersions()`: Fetches release list.
- `getDirectoryTree(version, path, depth)`: Fetches directory tree.
- `getFileContent(version, path)`: Retrieves raw text and AST tokens.
- `resolveInclude(version, header, astId, currentFile)`: Resolves `#include` directive targets.
- `getIncludeSymbols(version, astId, options)`: Fetches imported symbols for an include, supporting fallback lookup options (`filePath`, `line`, `header`, `tagId`).
- `getSymbolDetail(version, name)` / `getSymbolXref(version, name)`: Symbol inspection.
- `getMaintainersOverview(version, query)` / `getMaintainerSection(version, secId)`: Subsystems.
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
- **Database Instance Synchronization (`api.checkDbSync()`)**:
  - Automatically queries `GET /api/db/instance` upon frontend bootstrap.
  - Compares the remote `instance_hash` against `localDB.get("meta", "db_instance_hash")`.
  - When a mismatch occurs (indicating the database was wiped and rebuilt), automatically purges all data caches (`files`, `symbols`, `kconfigs`, `maintainers`, `commits`, `tools`) and writes the new instance hash.
- **Cache-First Client Policy**:
  - All static analysis and versioned data requests query IndexedDB first.
  - If a cached record exists, it is served immediately without making network calls, ensuring instantaneous loading for heavy structures (such as Kconfig hierarchy trees and source file buffers).
  - Dynamic keyword searches bypass the cache and query the REST API directly.
- **Storage Management API**:
  - `localDB.clearStore(storeName)`: Clears records for a single object store.
  - `localDB.clearDataStores()`: Clears all domain data stores while preserving workspace session tabs and nodemap diagrams.
  - `localDB.clearAllStores()`: Concurrently clears all configured object stores.
  - `localDB.getStorageEstimate()`: Queries `navigator.storage.estimate()` returning `{ usage, quota }`.
  - `localDB.getStoreCounts()`: Returns a dictionary `{ [storeName]: count }` of stored record counts.
- **Revamped Settings Popover (`#offline-badge`)**:
  - Clicking the top-right Online/Offline badge opens an anchored settings popover dropdown.
  - **Database & Network**: Real-time network status (`navigator.onLine`) alongside the verified DB Instance Hash badge with synchronization indicator (`● Synced`).
  - **Cached Data Stores Breakdown**: 6-card metric grid showing counts for Files, Symbols, Kconfig, Maintainers, Commits, and Tools, plus total entries and disk footprint in MB.
  - **Granular Per-Store Clear Chips**: Fast one-click buttons to flush individual stores (`Clear Kconfig`, `Clear Files`, `Clear Symbols`, etc.).
  - **Storage Actions**:
    1. **Clear All Offline Cache**: Flushes all domain data stores (`files`, `symbols`, `kconfigs`, `maintainers`, `commits`, `tools`).
    2. **Reset Tabs & Workspace State**: Clears saved tab state from `localStorage` and `tabs_state`, resetting the active workspace session.
    3. **Full Local Reset & Reload**: Prompts confirmation, wipes IndexedDB, purges `localStorage`, clears Service Worker/browser caches (`window.caches`), and reloads the application.

### 5.6. Context Menu & Cross-References Modal (`webapp/frontend/js/components/context_menu.js`)
- **Dual Mouse Navigation**: Left-click on AST tokens triggers quick inspector popup; right-click opens the contextual action menu (Go to Definition, Find References, Copy Symbol, Expand AST, NodeMap).
- **Context-Adaptive Kconfig Navigation**:
  - Automatically identifies Kconfig tokens across both C files and Kconfig files (via spatial AST types `88..118` from `m_map_ast`, `CONFIG_*` prefix matching, or Kconfig file category `ftype === 2`).
  - Replaces "Go to Definition" and "Find References / XRef" with **"Open in Kconfig Viewer"** (`⚙️`), preventing meaningless symbol lookups on configuration symbols.
  - Clicking "Open in Kconfig Viewer" strips any `CONFIG_` prefix, opens/focuses the Kconfig explorer tab, highlights the matching symbol row, and immediately loads dependency constraints and auto-solve options in the inspector.
- **Clean Cross-References (XRef) Modal**:
  - Distinct top **"Definition"** card displaying primary definition file path, line numbers, and readable AST type badge (`FunctionDecl`, `StructDecl`, `EnumConstant`, `CPPro_define_macro`), with direct 1-click jump-to-code navigation.
  - Categorized **"References & Usages"** list showing caller context, file paths, line numbers, and readable reference type badges (`Call`, `MemberRef`, `TypeUsage`, `DeclRef`, `MacroExp`).
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
- **Active Constraint Engine**: Active by default with live 3-valued boolean logic (`n=0, m=1, y=2`) and recursive default propagation fixpoint loop.
- **Authentic Scoped Trees**: Inlines `source`/`rsource` statements within their authentic enclosing menus starting from `arch/<arch>/Kconfig`, eliminating cross-architecture menu pollution (e.g. S/390 menus appearing under x86).
- **Architecture Switching Flow**:
  - Switching the target architecture dropdown checks if custom configuration selections exist and presents a confirmation modal before clearing old selections.
  - Automatically fetches architecture presets (`getKconfigPresets`), sets target architectural symbols (`ARCH`, `SRCARCH`, `64BIT`, `X86_64`, `ARM`, etc.), loads the architecture's canonical defconfig (`x86_64_defconfig`, `versatile_defconfig`, etc.), and executes recursive default propagation.
- **Mutual-Exclusion Choice Solver**:
  - Detects choice blocks (`choice.id`) and enforces single-member selection.
  - Renders choice members as radio options `( )` and `(*)`. Selecting any member automatically demotes competing choice members to `n` and triggers recursive propagation.
- **Type-Aware Cycling & TUI Navigation**:
  - Tristate symbols cycle across `[ ]` &rarr; `<M>` &rarr; `[*]`.
  - Boolean symbols toggle between `[ ]` and `[*]`.
  - Terminal Curses Menuconfig TUI (`menuconfig_tui.js`) supports spacebar tristate cycling and choice radio selection.
- **Evaluated Dependency Badges & Failing Breakdown**:
  - Evaluates direct dependencies in real time against the active configuration, rendering status badges: `✓ <expr> (y)` (`dep-met`), `~ <expr> (m)` (`dep-mod`), and `✗ <expr> (n)` (`dep-unmet`).
  - Nodes with unmet dependencies are visually dimmed (`.kconfig-node-disabled`) with hover tooltips detailing failing clauses.
  - Evaluates default conditions (`default <val> if <expr>`) in the inspector to show which defaults are actively triggered.
- **Clickable In-Place Dependency Navigation**:
  - Clicking any dependency badge cleans expressions (stripping negation `!`, prefix `CONFIG_`, and compound logic bounds), searches and highlights the target symbol in the active tree, updates tab title/state in place, and loads the target symbol's inspection panel without opening redundant duplicate tabs.
- **Persistent Folder Expansion & Scroll Preservation**:
  - The explorer tree tracks open folders via a persistent set of node IDs (`this.expandedNodeIds`). Selecting a kconfig symbol, toggling values, selecting choice members, solving prerequisites, or loading defconfigs re-renders the tree while preserving all opened folders, child hierarchy expansions, active row highlights, and the exact scroll offset (`scrollTop`). Clicking dependency badges expands all ancestor menus (`expandAncestors`) to reveal the target symbol in its authentic hierarchical place without collapsing any previously opened folders.

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
1. **Interactive Developer Profile Modal (`showPersonModal`)**:
   - Clicking developer names across the workspace (Commit author headers, Contributors & Signoffs lists, Commit cards, Git Blame gutters, and Blame context popovers) activates the universal Developer Profile modal (`webapp/frontend/js/components/person_modal.js`).
   - Displays avatar initials, author email, CREDITS biographical data (description, project URL, PGP keys, snail mail), Git stats (authored commits count), and maintained subsystems.
   - Provides 1-click elevation actions:
     - **Open in Maintainers Tab**: Focuses or launches `state.openTab({ type: "maintainers", person: ... })`, inspecting the developer's complete subsystem roster and contribution graph.
     - **View Commits**: Focuses or launches `state.openTab({ type: "commits", query: ... })`, filtering the commit history by author name or email.
2. **Kconfig Compiled Source Files**:
   - `sym.compiled_files` records (`{"file_path": "..."}`) are normalized and displayed with source file icons (`📄 kernel/smp.c`) and associated target object badges (`smp.o`).
   - Clicking any compiled file item (`.compiled-file-link`) immediately opens the target source file in the virtual code editor (`state.openTab({ type: "code", path: filePath })`), supporting both left-click in-place navigation and middle-click multi-tab queuing.

---

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
