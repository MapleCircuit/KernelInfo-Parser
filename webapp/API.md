# WebApp Subsystem API & Service Specification

The WebApp subsystem provides a developer introspection and analysis platform for KernelInfo-Parser, comprising a FastAPI REST backend server and a responsive single-page application (SPA) client.

## Authoritative Documentation
For exhaustive architectural specifications, endpoint schemas, state machines, and rendering algorithms, refer to:
- [`WEBAPP_SYSTEMS_AND_FEATURES.md`](webapp/WEBAPP_SYSTEMS_AND_FEATURES.md)
- [`README.md`](webapp/README.md)

---

## Core Architecture & Components

| Component | File | Primary Responsibility |
| :--- | :--- | :--- |
| **FastAPI API Server** | [`main.py`](webapp/main.py) | REST API endpoints, MySQL connection pooling, git integration, defconfig parsing, and in-memory caches. |
| **SPA Client Application** | [`webapp.html`](webapp/webapp.html) | Vanilla HTML5/CSS3/ES2022 interactive web UI, dark theme, IndexedDB caching, canvas DAG engine, terminal TUI menuconfig, Pahole memory viewer, and squarified treemaps. |


---

## REST API Endpoint Catalog

### 1. Version & Filesystem Service
- `GET /api/versions`: List all release versions in `m_v_main`.
- `GET /api/tree?version={v}&path={p}`: Directory hierarchy traversal and file listing.
- `GET /api/file?version={v}&path={p}`: File content, AST token mapping, `#if` scopes, and container hierarchies.
- `GET /api/blame?version={v}&path={p}`: Git blame line annotations and commit references.
- `GET /api/file/history?path={p}`: Cross-version lifecycle status (`Added`, `Modified`, `Deleted`).

### 2. Kconfig & Architecture Defconfigs
- `GET /api/kconfig/tree?version={v}&arch={a}`: Hierarchical Menuconfig tree scoped by architecture.
- `GET /api/kconfig/symbol?version={v}&name={sym}`: Symbol details, dependency relations, and reverse dependencies.
- `GET /api/kconfig/defconfigs?version={v}&arch={a}`: Discovered architecture defconfig files.
- `GET /api/kconfig/defconfig/content?version={v}&defconfig={name}`: Parsed defconfig key-value pairs.
- `POST /api/kconfig/solve`: 20-pass constraint resolution and symbol auto-selection.
- `POST /api/kconfig/export`: Generate `.config` file content from active configuration.

### 3. Maintainers & Credits Directory
- `GET /api/maintainers?version={v}&q={query}`: Subsystem catalog and maintainer search.
- `GET /api/maintainers/section?version={v}&sec_id={id}`: Subsystem section details, patterns, and member rosters.
- `GET /api/maintainers/person?version={v}&person_id={id}`: Developer profile, maintained sections, and git commits.
- `POST /api/maintainers/match`: Match file paths or patches against subsystem pattern rules (`get_maintainer.pl` emulation).
- `GET /api/credits?version={v}&q={query}`: Historical `CREDITS` file entries and biographical details.

### 4. Git Commits & Patch Management
- `GET /api/commits?version={v}&page={p}&q={query}`: Paginated commit log and top contributors.
- `GET /api/commit?version={v}&commit_id={id}`: Full commit details, multi-contributor trailers, and modified files/tags.
- `POST /api/patch/format`: Generate RFC-2822 compliant `git format-patch` emails with automated maintainer CC lists.

### 5. Semantic Analysis, Symbols & Visual Modeling Tools
- `GET /api/dag/data?version={v}&symbol={sym}`: Dependency Directed Acyclic Graph (DAG) for canvas visualization.
- `GET /api/treemap/data?version={v}&path={p}`: Squarified directory treemap with Lines-of-Code metrics.
- `GET /api/struct/layout?version={v}&name={struct_name}`: Pahole-style memory alignment and padding analysis.
- `GET /api/symbols/search?version_name={v}&q={prefix}&limit={n}`: Fast prefix search across defined symbols using `m_symbol_def`.
- `GET /api/symbols/lookup?version_name={v}&q={prefix}&limit={n}`: Fast typeahead symbol name suggestions.
- `GET /api/version/{version_name}/symbol/{symbol_name}`: Authoritative definition, declarations, and usages across the codebase.
- `GET /api/version/{version_name}/xref/{symbol_name}`: Cross-reference definitions and categorized usage references (`calls`, `member_refs`, `type_usages`, `declarations`) powered by `m_symbol_def` and `m_symbol_ref`.
- `GET /api/version/{version_name}/callgraph/{function_name}`: Bidirectional function caller/callee flow using `m_symbol_def` and `m_symbol_ref`.
- `GET /api/ast/{ast_id}/tree?version_name={v}&depth={d}`: Recursive `m_ast_container` hierarchy traversal with `tag_id` annotation for tagged nodes (`None` for untagged statement/expression nodes).
- `GET /api/tag/{tag_id}`: Direct tag metadata and spatial AST coordinate map inspection.
- `GET /api/tag/{tag_id}/timeline`: Cross-version code evolution history, timeline snapshots, diffs, and commit contexts.
