# KernelInfo-Parser Developer Web Application: Full Architecture, Systems & Features Specification

This document provides an exhaustive, authoritative technical specification of all features, subsystems, REST endpoints, UI controllers, rendering algorithms, state machines, and caching architectures within the **KernelInfo-Parser Developer Web Application** (comprising the **FastAPI Backend Server** in [`webapp/main.py`](webapp/main.py) and the **Single-Page Application Client** in [`webapp/webapp.html`](webapp/webapp.html)).

---

## 1. Executive Summary & Architectural Topology

The KernelInfo-Parser Web Application is a high-performance developer introspection and analysis platform designed to explore Linux kernel source code, relational Abstract Syntax Trees (AST), spatial coordinate mappings, hierarchical configuration menus (Kconfig), subsystem maintainers, credits, git commit timelines, code blame annotations, memory layout structures, function call flows, codebase treemaps, and cross-release evolution tracking across kernel release versions (e.g. `v3.0`).

```
+---------------------------------------------------------------------------------------------------------------+
|                                                CLIENT BROWSER                                                 |
|           Single-Page Application (Vanilla HTML5 / CSS3 / ES2022 JavaScript - Zero Dependencies)              |
|                                                                                                               |
|  +---------------------------+  +---------------------------+  +-------------------------+  +-------------------------------+ |
|  | Explorer & AST            |  | Kconfig Web GUI           |  | Terminal TUI            |  | Subsystems & Maintainers Hub  | |
|  | - File Tree Sidebar       |  | - Dual Mode (Drill/Full)  |  | - Keyboard Nav          |  | - Subsystem Roster Grid       | |
|  | - Token Resolution        |  | - 20-Pass Solver          |  | - ANSI Dialogs          |  | - Maintainers & Reviewers     | |
|  | - Line Linking/Hash       |  | - Target Profile          |  | - Hotkeys (Y/N/M/?)     |  | - Pattern Matching Engine     | |
|  | - Semantic AST Highlighting |  | - Call Graph Inspector    |  | - Defconfig Selector    |  | - Matching Files Table        | |
|  | - Git Blame Annotations   |  | - Memory Hole Packing     |  | - Search & Autocomplete |  | - Contributor Bios & Rosters  | |
|  | - #if Conditional Folding |  | - Import / Export         |  | - Symbol Auto-Solve     |  | - Direct Maintainer Matching  | |
|  +---------------------------+  +---------------------------+  +-------------------------+  +-------------------------------+ |
|  +---------------------+  +---------------------+  +---------------------+  +-------------------------------+ |
|  | Credits Directory   |  | Commit Timeline     |  | Cross-Version Diff  |  | Patch Matcher & Studio        | |
|  | - Credited Bios     |  | - Top 10 Ranking    |  | - File Tree Diff    |  | - get_maintainer.pl Matcher   | |
|  | - PGP & SCM Links   |  | - Chronological Log |  | - Kconfig Evolution |  | - In-Browser Diff & Staging   | |
|  | - Linked Roles      |  | - Developer Profile |  | - Status Filters    |  | - RFC-2822 format-patch Export| |
|  | - Postal Addresses  |  | - Commit Inspector  |  | - Line Diff View    |  | - 1-Click Clipboard / Download| |
|  +---------------------+  +---------------------+  +---------------------+  +-------------------------------+ |
|  +---------------------+  +---------------------+  +---------------------+  +-------------------------------+ |
|  | Interactive DAG     |  | Pahole Struct Layout|  | Squarified Treemap  |  | Function Call Graph           | |
|  | - HTML5 Canvas 2D   |  | - Byte Offsets/Size |  | - Squarified Layout |  | - Bidirectional Callers/Callee| |
|  | - Sugiyama (LR/TB)  |  | - Padding Holes     |  | - Depth Slider (1-5)|  | - m_symbol_def / m_symbol_ref | |
|  | - Force Simulation  |  | - 64B Cachelines    |  | - Subsystem Colors  |  | - 1-Click Jump to Source      | |
|  | - Concentric Radial |  | - Reorder Optimizer |  | - Drill-Down Breadcr|  | - Call Site File & Line Flow  | |
|  | - SVG Export & Pan  |  | - Cache Split Alert |  | - LOC / File Metric |  | - Two-Column Flow Hierarchy   | |
|  +---------------------+  +---------------------+  +---------------------+  +-------------------------------+ |
|  +---------------------+  +---------------------+  +---------------------+  +-------------------------------+ |
|  | Global Symbol Search|  | Token Context Action|  | Upgraded C-AST View |  | Global Symbol XRef            | |
|  | - Topbar Search Bar |  | - Interactive Token |  | - Statements & Expr |  | - m_symbol_def Definitions   | |
|  | - Ctrl+K Autocomplete| | - Go to Definition  |  | - Designated Inits  |  | - Categorized References      | |
|  | - Debounced Typeahead | | - Find XRef Usages  |  | - Inline Assembly   |  | - Calls, MemberRefs, Types    | |
|  | - Keyboard Navigation| | - Open Call Graph   |  | - Conditional Tags  |  | - Declarations & Other        | |
|  | - Direct Line Jump  |  | - Inspect AST Node  |  | - Suppressed Subtags|  | - Instant Coordinate Jump     | |
|  +---------------------+  +---------------------+  +---------------------+  +-------------------------------+ |
|                                                                                                               |
|  +---------------------------------------------------------------------------------------------------------+  |
|  | Client-Side Storage Tier: IndexedDB (`KernelInfoDB` v1: metadata, file, kconfig, subsystems, blame)     |  |
|  | Tab Manager: 21 Tab Types | URL Hash Coordinator: `#{version}/{path}:L{start}-L{end}`                   |  |
|  +---------------------------------------------------------------------------------------------------------+  |
+----------------------------------------------------+----------------------------------------------------------+
                                                     | REST API Requests (HTTP / JSON)
                                                     v
+---------------------------------------------------------------------------------------------------------------+
|                                            FASTAPI API SERVER                                                 |
|                                            (`webapp/main.py`)                                                 |
|                                                                                                               |
|  +--------------------------------+  +--------------------------------+  +---------------------------------+  |
|  | Version & File Services        |  | Kconfig Engine & Solver        |  | Subsystem & Credits Services    |  |
|  | - Path & Directory Traversal   |  | - Scoped Architecture Hierarchy|  | - 500+ Subsystem Catalog        |  |
|  | - Code Tags & Spatial Maps     |  | - Dynamic Target Presets       |  | - Subsystem Roster & Matcher    |  |
|  | - Container Depth Resolver     |  | - Defconfig File Parser        |  | - Developer Profiles & Metrics  |  |
|  | - Git Blame & Multi-Commit Map |  | - 20-Pass Selection Enforcer   |  | - CREDITS Directory & PGP Keys  |  |
|  | - File History & Lifecycle     |  | - Import / Export Formatter    |  | - Latest Patch Spotlight        |  |
|  +--------------------------------+  +--------------------------------+  +---------------------------------+  |
|  +--------------------------------+  +--------------------------------+  +---------------------------------+  |
|  | Semantic Analysis & Diff       |  | Visual Modeling & Symbols      |  | Database Connection Manager     |  |
|  | - Cross-Version Tree Diff      |  | - Canvas Dependency DAG Engine |  | - MySQL Connection Pooling      |  |
|  | - Kconfig Evolution Analysis   |  | - Squarified Treemap Generator |  | - Docker & Local Host Failover  |  |
|  | - Symbol XRef (m_symbol_ref)   |  | - Pahole Memory Layout Engine  |  | - In-Memory Performance Caches  |  |
|  | - Fast Prefix Symbol Search    |  | - Function Call Graph Engine   |  | - Composite DB Index Fastpaths  |  |
|  | - Patch Maintainers Matcher    |  | - Tag Hierarchy & Timelines    |  | - Direct Integer `vid` Fastpaths|  |
|  | - RFC-2822 format-patch Engine |  | - AST Node Container Depth     |  | - Dev Introspection Table Counts|  |
|  | - Clang compile_commands Expor |  | - Dev Endpoints Schema Catalog |  | - Robust Reconnection Fallback  |  |
|  +--------------------------------+  +--------------------------------+  +---------------------------------+  |
+----------------------------------------------------+----------------------------------------------------------+
                                                     |
                     +-------------------------------+-------------------------------+
                     | Direct SQL Queries                                            | Subprocess & Disk Fallback
                     v                                                               v
+----------------------------------------------------+  +-------------------------------------------------------+
|               MySQL Relational Layer               |  |                 Linux Git Repository                  |
|             (`main` / `test` Database)             |  |                   (`linux/` Folder)                   |
|  - 25 Relational Schema Tables                     |  |  - Raw C Source Code & Header Files                   |
|  - High-Precision Spatial Token Coordinates        |  |  - Architecture Defconfigs (`arch/*/configs/*`)       |
|  - Structural Deduplication Hashes (`m_ast_hash`)   |  |  - Git Commit Log, Multi-Contributor Trailers & Trees |
|  - Materialized Bridges & Container Hierarchies    |  |  - Git Blame Annotations & Commit Hunks               |
+----------------------------------------------------+  +-------------------------------------------------------+
```

---

## 2. Database Schema & Relational Integration

The web application interfaces directly with the MySQL relational database defined in [`core/DBLayout.py`](core/DBLayout.py):

| Table Name | `table_id` | Primary Key | Key Columns Utilized by Webapp | Role in Webapp Subsystems |
| :--- | :--- | :--- | :--- | :--- |
| `m_v_main` | 0 | `vid` | `vid`, `vname` | Release version selector, version lifespan boundaries (`vid_s`, `vid_e`). |
| `m_file_name` | 1 | `fnid` | `fnid`, `fname` | Unique path registry for tree browsing, sidebar traversal, defconfig discovery, and file resolution. |
| `m_file` | 2 | `fid` | `fid`, `vid_s`, `vid_e`, `ftype`, `s_stat`, `e_stat` | File lifecycle tracking (`Added`, `Modified`, `Renamed`, `Deleted`), file types (Dir=0, C=1, Kconfig=2, Rust=3). |
| `m_bridge_file` | 3 | `(vid, fnid)` | `vid`, `fnid`, `fid` | Resolves active file instances (`fid`) for a given version (`vid`) and path (`fnid`). |
| `m_moved_file` | 4 | `(s_fid, e_fid)` | `s_fid`, `e_fid` | File renaming and historical movement tracking. |
| `m_type_descriptor` | 5 | `type_id` | `type_id`, `name` | Syntax construct descriptor registry (`C_struct`, `CPPro_define`, `C_Compound`, `Kconfig_Config`, etc.) used for syntax coloring and AST sandbox. |
| `m_ast` | 6 | `ast_id` | `ast_id`, `name`, `type_id` | AST node registry linked to token spans, struct members, and hierarchy containers. |
| `m_ast_container` | 7 | `(ast_id, priority)` | `ast_id`, `priority`, `type_id`, `ref_ast_id` | Recursive parent-child AST relationships (struct members, function parameters, compound statement blocks) used for container depth computation. |
| `m_ast_include` | 8 | `ast_id` | `ast_id`, `fnid` | Preprocessor `#include` directive dependencies. |
| `m_ast_debug` | 9 | `ast_id` | `ast_id`, `ast_raw` | Serialized JSON AST dumps for dev inspection. |
| `m_tag_code` | 10 | `hash` | `hash`, `code` | Global deduplicated source code snippet text registry. |
| `m_tag` | 11 | `(tag_id, vid_s)` | `tag_id`, `vid_s`, `vid_e`, `hash`, `ast_id`, `hl_s`, `hl_l` | Source code occurrence tags linking AST nodes to code snippets. |
| `m_bridge_tag` | 12 | `(fid, tag_id)` | `fid`, `tag_id`, `line_s`, `line_e`, `char_s`, `char_e` | Tag spatial coordinates (line numbers and character offsets within source files). |
| `m_map_ast` | 13 | `(map_id, ...)` | `map_id`, `line_s`, `char_s`, `line_e`, `char_e`, `ast_id` | High-precision token coordinate spans inside tag snippets. |
| `m_bridge_map` | 14 | `(tag_id, map_id)`| `tag_id`, `map_id` | Bridges spatial AST token maps to their parent code tags. |
| `m_ast_hash` | 15 | `hash` | `hash`, `ast_id` | SHA-256 structural deduplication cache for AST nodes. |
| `m_kconfig_symbol` | 16 | `(kcid, vid_s)` | `kcid`, `vid_s`, `vid_e`, `name`, `type`, `prompt`, `def_val`, `help`, `ast_id` | Normalized Kconfig symbol definitions, types (`bool`, `tristate`, `string`, `hex`, `int`), prompts, default values, and help text. |
| `m_kconfig_relation` | 17 | `(kcid, rel_type, ...)` | `kcid`, `target_name`, `rel_type`, `cond_ast_id`, `priority` | Directed relational dependency graph (`depends_on`=1, `select`=2, `imply`=3, `choice_member`=4). |
| `m_kconfig_tree` | 18 | `(tree_id, vid)` | `tree_id`, `vid`, `parent_id`, `node_type`, `title`, `kcid`, `priority`, `dep_ast_id`, `ast_id` | Hierarchical Menuconfig tree nodes (`menu`=1, `choice`=2, `config`=3, `menuconfig`=4, `comment`=5) with sibling priorities. |
| `m_kconfig_kbuild` | 19 | `(kcid, vid, ...)` | `kcid`, `vid`, `fid`, `compile_mode`, `target_obj` | Kbuild compilation map linking configuration symbols to compiled `.o` object files and source files (`obj-y`=1, `obj-m`=2, `conditional`=3). |
| `m_maintainer_person` | 20 | `person_id` | `person_id`, `name`, `email` | Developer identity registry for maintainers, reviewers, authors, and committers. |
| `m_maintainer_section` | 21 | `(sec_id, vid_s)` | `sec_id`, `vid_s`, `vid_e`, `name`, `status`, `scm_tree`, `web_page`, `mailing_list`, `ast_id` | Subsystem catalog (`EXT4 FILE SYSTEM`, `NETWORKING [GENERAL]`, `ARM ARCHITECTURE`, etc.). |
| `m_maintainer_member` | 22 | `(sec_id, person_id, ...)` | `sec_id`, `person_id`, `role_type`, `priority` | Subsystem member rosters with roles (`Maintainer`=1, `Reviewer`=2, `Person`=3, `Other`=4). |
| `m_maintainer_pattern` | 23 | `(sec_id, pat_type, ...)` | `sec_id`, `pat_type`, `pattern`, `priority` | Wildcard file matching rules (`File`=1, `Exclude`=2, `Keyword`=3, `Regex`=4). |
| `m_maintainer_file` | 24 | `(vid, fid, sec_id)` | `vid`, `fid`, `sec_id` | Materialized bridge between files and their governing subsystems. |
| `m_credits_entry` | 25 | `(credit_id, vid_s)` | `credit_id`, `vid_s`, `vid_e`, `person_id`, `web_page`, `pgp_key`, `description`, `snail_mail`, `ast_id` | Historical `CREDITS` file entries linking developers to contribution narratives, homepages, PGP keys, and snail mail. |
| `m_commit` | 26 | `commit_id` | `commit_id`, `vid`, `commit_hash`, `author_id`, `author_date`, `committer_id`, `committer_date`, `subject`, `message` | Git commit log and patch registry. |
| `m_bridge_commit_person` | 27 | `(commit_id, person_id, ...)` | `commit_id`, `person_id`, `role_type`, `priority` | Multi-contributor bridge (`Author`=1, `Committer`=2, `Co-developed-by`=3, `Signed-off-by`=4, `Reviewed-by`=5, `Acked-by`=6, `Tested-by`=7, `Reported-by`=8, `Suggested-by`=9, `Merged-by`=10, `Requested-by`=11). |
| `m_bridge_commit_file` | 28 | `(commit_id, fid)` | `commit_id`, `vid`, `fid`, `change_type` | Files touched per commit. |
| `m_bridge_commit_tag` | 29 | `(commit_id, tag_id)` | `commit_id`, `vid`, `fid`, `tag_id` | Code tags modified per commit. |
| `m_symbol_def` | 30 | `def_id` | `def_id`, `vid`, `fid`, `tag_id`, `ast_id`, `name`, `type_id`, `line_s`, `line_e` | Authoritative symbol definitions across the kernel codebase. Indexed by `(vid, name)`. |
| `m_symbol_ref` | 31 | `ref_id` | `ref_id`, `vid`, `fid`, `tag_id`, `ast_id`, `role`, `line`, `char_s` | Global symbol usage references (Declaration=1, TypeUsage=2, Call=3, MemberRef=4, DeclRef=5). Indexed by `(vid, ast_id)`. |

---

## 3. API Server Subsystem Reference (`webapp/main.py`)

The backend server is implemented using **FastAPI** with **MySQL connection pooling** (`mysql.connector.pooling.MySQLConnectionPool`), automatic failover, and high-performance in-memory caching.

### 3.1. Connection Management, Failover & Caching

#### `DatabaseManager` Class
- **Host Resolution**:
  1. Checks environment variable `MYSQL_HOST`.
  2. If running inside a Docker container (presence of `/.dockerenv`), uses `host.docker.internal`.
  3. Otherwise defaults to `127.0.0.1`.
- **Port Resolution**: Checks `MYSQL_PORT` or defaults to `3306`.
- **Database Selection**: Checks `MYSQL_DATABASE` or dynamically scans candidate databases (`test`, `main`).
- **Connection Pool**: Initializes `MySQLConnectionPool` named `kernelinfo_pool_<pid>_<dbname>` with a pool size of up to 10 connections.
- **Failover & Reconnect**: If pooled connection fails, attempts a fresh standalone connection on demand.

#### Global In-Memory Caches in `webapp/main.py`
To eliminate redundant database lookups and subprocess executions, `webapp/main.py` maintains specialized memory caches:
- `_VERSION_CACHE`: Maps release string `vname` (e.g. `"v3.0"`) to integer `vid` (e.g. `0`) and vice versa. Preloaded on startup.
- `_DEFCONFIG_CACHE`: Caches parsed defconfig symbol dictionaries keyed by `(version, file_path)`.
- `_GIT_COMMITS_CACHE`: Caches parsed git commit lists keyed by `version`.
- `_GIT_HUNKS_CACHE`: Caches file blame hunks and commit hashes keyed by `(version, file_path)`.
- `_FILE_SUBSYSTEMS_CACHE`: Caches resolved subsystem lists keyed by `(version, file_path)`.
- `_MAINTAINER_CACHE`: Caches parsed maintainer section objects keyed by `version`.
- `_CREDITS_CACHE`: Caches parsed credit records keyed by `version`.

---

### 3.2. Pydantic Request Models

The backend defines 6 Pydantic models for structured POST request bodies:

```python
class AutoSolveRequest(BaseModel):
    target_symbol: str
    current_values: dict[str, str] = {}

class DiffConfigRequest(BaseModel):
    active_config: dict[str, str] = {}
    target_defconfig: str | None = None
    custom_config: dict[str, str] | None = None

class PatchReviewRequest(BaseModel):
    patch_text: str

class AstQueryRequest(BaseModel):
    type_id: int | None = None
    type_name: str | None = None
    name_pattern: str | None = None
    path_prefix: str | None = None
    container_depth: int | None = None
    limit: int = 50
    offset: int = 0

class FootprintRequest(BaseModel):
    kconfig_values: dict[str, str] = {}

class FormatPatchRequest(BaseModel):
    file_path: str
    original_content: str
    modified_content: str
    author_name: str = "Kernel Developer"
    author_email: str = "developer@kernel.org"
    commit_subject: str = "kernel: apply updates"
    commit_message: str = ""
```

---

### 3.3. Exhaustive REST Endpoint Catalog

The backend exposes **49 route registrations (38 unique endpoints)** across 21 functional domains:

```
+---------------------------------------------------------------------------------------------------------+
|                                    COMPLETE BACKEND ENDPOINT CATALOG                                    |
+---------------------------------------------------------------------------------------------------------+
| Method | Route Path                                        | Function Handler                           |
+--------+---------------------------------------------------+--------------------------------------------+
| GET    | /                                                 | read_root()                                |
| GET    | /app, /webapp                                     | serve_webapp()                             |
| GET    | /api/versions, /versions                          | get_all_versions()                         |
| GET    | /api/type_descriptors, /type_descriptors          | get_type_descriptors()                     |
| GET    | /api/version/{version_name}/browse/               | browse_path()                              |
| GET    | /api/version/{version_name}/browse/{path:path}     | browse_path()                              |
| GET    | /v/{version_name}/, /v/{version_name}/{path:path}  | browse_path() (Short Aliases)              |
| GET    | /api/file/{fid}                                   | get_file_by_id()                           |
| GET    | /api/tag/{tag_id}                                 | get_tag_by_id()                            |
| GET    | /api/tag/{tag_id}/timeline                        | get_tag_timeline()                         |
| GET    | /api/ast/{ast_id}/tree                            | get_ast_container_tree()                   |
| GET    | /api/version/{version_name}/kconfig/search        | search_kconfig_symbols()                   |
| GET    | /api/version/{version_name}/kconfig/symbol/{name} | get_kconfig_symbol_detail()                |
| GET    | /api/version/{version_name}/kconfig/tree          | get_kconfig_tree()                         |
| GET    | /api/version/{version_name}/kconfig/env-presets    | get_kconfig_env_presets()                  |
| GET    | /api/version/{version_name}/kconfig/defconfigs    | get_kconfig_defconfigs()                   |
| GET    | /api/version/{version_name}/kconfig/defconfig     | get_kconfig_defconfig_content()            |
| POST   | /api/version/{version_name}/kconfig/validate      | validate_kconfig_assignments()             |
| POST   | /api/version/{version_name}/kconfig/export        | export_kconfig_file()                      |
| POST   | /api/version/{version_name}/kconfig/import        | import_kconfig_file()                      |
| GET    | /api/version/{version_name}/maintainers           | get_maintainers_overview()                 |
| GET    | /api/version/{version_name}/maintainer/section/{s}| get_maintainer_section_detail()            |
| GET    | /api/version/{version_name}/person/{p}            | get_person_profile()                       |
| GET    | /api/version/{version_name}/commits               | get_version_commits()                      |
| GET    | /api/version/{version_name}/commit/{hash_or_id}   | get_commit_detail()                        |
| GET    | /api/version/{version_name}/file/{fid}/blame      | get_file_blame()                           |
| GET    | /api/version/{version_name}/timeline              | get_commit_timeline()                      |
| GET    | /api/version/{version_name}/credits               | get_credits_overview()                     |
| GET    | /api/dev/tables                                   | get_dev_table_counts()                     |
| GET    | /api/dev/endpoints                                | get_dev_endpoints()                        |
| GET    | /api/diff/versions/{v1}/{v2}                      | get_versions_diff()                        |
| GET    | /api/diff/kconfig/{v1}/{v2}                       | get_kconfig_diff()                         |
| GET    | /api/version/{version_name}/xref/{symbol_name}    | get_symbol_xref()                          |
| GET    | /api/version/{version_name}/symbol/{symbol_name}  | get_symbol_detail()                        |
| GET    | /api/symbols/search                               | search_symbols()                           |
| GET    | /api/symbols/lookup                               | lookup_symbols()                           |
| GET    | /api/ast/{ast_id}/tree                            | get_ast_container_tree()                   |
| GET    | /api/version/{version_name}/kconfig/graph/{symbol} | get_kconfig_graph()                        |
| POST   | /api/version/{version_name}/kconfig/autosolve     | autosolve_kconfig()                        |
| POST   | /api/version/{version_name}/kconfig/diff_config   | diff_kconfig_configurations()              |
| POST   | /api/version/{version_name}/patch/maintainers     | match_patch_maintainers()                  |
| GET    | /api/version/{version_name}/export/compile_commands| export_compile_commands()                  |
| GET    | /api/version/{version_name}/struct/layout/{struct} | get_struct_layout()                         |
| GET    | /api/version/{version_name}/treemap               | get_codebase_treemap()                     |
| GET    | /api/version/{version_name}/callgraph/{function}  | get_function_callgraph()                   |
| POST   | /api/version/{version_name}/patch/format          | generate_formatted_patch()                  |
+---------------------------------------------------------------------------------------------------------+
```

#### 1. Root & Static Serving
- **`GET /`** (`read_root`):
  - Returns service status, connected database host, connected database name, webapp URL (`/app`), and OpenAPI docs URL (`/docs`).
- **`GET /app`** / **`GET /webapp`** (`serve_webapp`):
  - Returns [`webapp/webapp.html`](webapp/webapp.html) as a static `FileResponse` with `media_type="text/html"`.

#### 2. Version & Syntax Descriptors
- **`GET /api/versions`** / **`GET /versions`** (`get_all_versions`):
  - Returns `list[dict[str, Any]]` of all versions from `m_v_main`: `[{"vid": 0, "vname": "v3.0"}]`. Injects `Cache-Control: public, max-age=3600`.
- **`GET /api/type_descriptors`** / **`GET /type_descriptors`** (`get_type_descriptors`):
  - Returns `list[dict[str, Any]]` of all syntax construct categories from `m_type_descriptor`: `[{"type_id": 1, "name": "C_struct"}, ...]`. Injects `Cache-Control: public, max-age=86400`.

#### 3. Unified Path Browsing & Source Code Inspection
- **`GET /api/version/{version_name}/browse/`**
- **`GET /api/version/{version_name}/browse/{path:path}`**
- **`GET /v/{version_name}/`**
- **`GET /v/{version_name}/{path:path}`** (`browse_path`):
  - **Directory Browsing**: When `path` is empty or matches a directory:
    - Queries `m_file_name` with prefix aggregation to return `sub_dirs: list[dict]` and direct child `files: list[dict]` with file lifecycle indicators (`s_stat`, `e_stat`).
  - **File Inspection**: When `path` matches a file:
    - Queries `m_file` metadata (`fid`, `vid_s`, `vid_e`, `ftype`, `s_stat`, `e_stat`).
    - Queries revision history from `m_bridge_file`.
    - Queries code tags (`m_bridge_tag`, `m_tag`, `m_ast`, `m_type_descriptor`, `m_ast_debug`).
    - Queries spatial AST token coordinate maps (`m_bridge_map`, `m_map_ast`).
    - Computes container depths across all AST nodes via `compute_container_depths`.
    - Resolves governing subsystem maintainers via `resolve_subsystems_for_file_internal`.
    - Returns `{ type: "file", fid, file_path, tags, maps, container_depths, subsystems, history }`.

#### 4. Direct Entity Lookups
- **`GET /api/file/{fid}`** (`get_file_by_id`):
  - Looks up a file directly by numeric `fid`, returning tags, maps, container depths, subsystems, and revision history.
- **`GET /api/tag/{tag_id}`** (`get_tag_by_id`):
  - Returns snippet code, character offsets, linked AST metadata, and spatial token coordinate maps for a specific code tag.
- **`GET /api/tag/{tag_id}/timeline`** (`get_tag_timeline`):
  - Returns cross-version lifecycle history, chronological version snapshots, structured unified line diffs, and associated git commits for a tag. Power the interactive Draggable Version Timeline Scrubber in the WebApp.
- **`GET /api/ast/{ast_id}/tree?depth=3`** (`get_ast_container_tree`):
  - Traverses recursive `m_ast_container` child relationships down to the requested `depth` (1 to 10), returning a nested tree of child AST nodes, relationship priority ranks, and descriptor names.

#### 5. Kconfig Subsystem Endpoints
- **`GET /api/version/{version_name}/kconfig/search?q={query}&type={type}&limit=50&offset=0`** (`search_kconfig_symbols`):
  - Searches symbol names (stripping `CONFIG_`), user prompts, and help text. Filters by symbol data type (`bool`, `tristate`, `string`, `hex`, `int`).
- **`GET /api/version/{version_name}/kconfig/symbol/{name_or_kcid}`** (`get_kconfig_symbol_detail`):
  - Fetches symbol metadata, prompt, default value, help text, lifecycle versions.
  - Direct relations: `depends_on`, `selects`, `implies` from `m_kconfig_relation`.
  - Reverse relations: `selected_by` and `implied_by`.
  - Kbuild object compilation mapping: Queries `m_kconfig_kbuild` to return compiled `.o` object files and source files (`obj-y`, `obj-m`, conditional).
- **`GET /api/version/{version_name}/kconfig/tree?arch={arch}`** (`get_kconfig_tree`):
  - Returns hierarchical Menuconfig tree scoped to `arch` (normalizing `x86_64` $\rightarrow$ `x86`, `arm64` $\rightarrow$ `arm64`). Synthesizes root-level menus for unparented subsystems (`drivers/`, `fs/`, `net/`, `security/`, `crypto/`, `lib/`, `kernel/power/`, `block/`).
- **`GET /api/version/{version_name}/kconfig/env-presets`** (`get_kconfig_env_presets`):
  - Discovers all supported target architectures from `arch/%/Kconfig`. Queries environment symbols (`ARCH`, `SRCARCH`, `64BIT`, `CROSS_COMPILE`, `CC_IS_GCC`, etc.) and compiler presets.
- **`GET /api/version/{version_name}/kconfig/defconfigs?arch={arch}`** (`get_kconfig_defconfigs`):
  - Returns all architecture defconfigs (`arch/{arch}/configs/*defconfig*`) with canonical baseline detection.
- **`GET /api/version/{version_name}/kconfig/defconfig?file_path={path}&arch={arch}`** (`get_kconfig_defconfig_content`):
  - Reads raw defconfig content via `git show` or disk read, parses `# CONFIG_FOO is not set` $\rightarrow$ `"n"` and `CONFIG_FOO=y` $\rightarrow$ `"y"`.
- **`POST /api/version/{version_name}/kconfig/validate`** (`validate_kconfig_assignments`):
  - Validates symbol assignments against relational rules, iteratively enforcing minimum values for selected symbols and verifying `depends_on` conditions.
- **`POST /api/version/{version_name}/kconfig/export`** (`export_kconfig_file`):
  - Serializes symbol dictionary into standard Linux kernel `.config` file text.
- **`POST /api/version/{version_name}/kconfig/import`** (`import_kconfig_file`):
  - Parses uploaded `.config` text payload into a symbol assignments dictionary.

#### 6. Maintainers, Subsystems & Credits Endpoints
- **`GET /api/version/{version_name}/maintainers?q={q}&status={status}`** (`get_maintainers_overview`):
  - Returns catalog of all kernel subsystems in the active version with maintainer/reviewer rosters and pattern counts. Cross-references `m_credits_entry` to flag credited developers.
- **`GET /api/version/{version_name}/maintainer/section/{sec_id_or_name:path}`** (`get_maintainer_section_detail`):
  - Resolves subsystem section by numeric ID or title string. Returns maintainers, reviewers, mailing lists, SCM trees, web pages, pattern rules (`F:`, `X:`, `K:`, `R:`), and matching repository files table.
- **`GET /api/version/{version_name}/person/{person_id_or_email:path}`** (`get_person_profile`):
  - Resolves developer profile by ID or email/name. Gathers maintained subsystems, `CREDITS` narrative, personal homepage, PGP key, snail mail, Git contribution metrics (Authored, Co-developed, Signed-off, Reviewed, Merged, Requested commits), and **Latest Patch Spotlight**.
- **`GET /api/version/{version_name}/credits?q={q}`** (`get_credits_overview`):
  - Returns directory of all credited Linux contributors in `CREDITS` with biographical narratives, homepages, PGP keys, and linked maintainer roles.

#### 7. Git Commits, Blame & Timeline Endpoints
- **`GET /api/version/{version_name}/commits?q={q}&author={author}&limit=50&offset=0`** (`get_version_commits`):
  - Returns chronological commit list with multi-contributor roles and merge commit metadata (`is_merge`, pull request requester, merged branch origin, shortlog summaries).
- **`GET /api/version/{version_name}/commit/{commit_hash_or_id:path}`** (`get_commit_detail`):
  - Full commit details: commit hash, author, committer, timestamps, subject, full commit message body, touched files (`m_bridge_commit_file`), and modified code tags (`m_bridge_commit_tag`).
- **`GET /api/version/{version_name}/file/{fid}/blame`** (`get_file_blame`):
  - Returns line-by-line git blame annotations mapped to code tags with primary author, commit hash, date, and multi-commit revision counts (`+N` commits).
- **`GET /api/version/{version_name}/timeline?limit=10`** (`get_commit_timeline`):
  - Aggregates commit timeline and computes **Top Contributor Ranking** by commit frequency.

#### 8. Cross-Version Semantic Diff Endpoints
- **`GET /api/diff/versions/{v1}/{v2}`** (`get_versions_diff`):
  - Compares the file trees of release `v1` and `v2`, categorizing all paths into `added`, `removed`, `modified` (based on `fid` changes and `s_stat`/`e_stat`), and `unchanged`.
- **`GET /api/diff/kconfig/{v1}/{v2}`** (`get_kconfig_diff`):
  - Compares Kconfig symbols across releases, tracking symbol additions, removals, prompt alterations, type changes, and default value modifications.

#### 9. Global Symbol Search, XRef & Autocomplete Endpoints
- **`GET /api/version/{version_name}/xref/{symbol_name}`** (`get_symbol_xref`):
  - Locates the authoritative symbol definition in `m_symbol_def` and queries `m_symbol_ref` for all global references across the kernel source tree. Returns categorized references: `calls` (Call role=3), `member_refs` (MemberRef role=4), `type_usages` (TypeUsage role=2), `declarations` (Declaration role=1), and `other` with file paths, line numbers, and character spans. Backed by composite index `idx_symbol_ref_vid_ast` for sub-15ms latency.
- **`GET /api/version/{version_name}/symbol/{symbol_name}`** (`get_symbol_detail`):
  - Resolves authoritative symbol definition from `m_symbol_def`, declarations from `m_symbol_ref` (role=1), and usages from `m_symbol_ref` (roles 2-5).
- **`GET /api/symbols/search?version_name={v}&q={q}&limit=30`** (`search_symbols`):
  - Fast prefix search across all defined symbols in `m_symbol_def` using composite index `idx_symbol_def_vid_name`, returning symbol name, type descriptor (`C_functionprotodecl`, `C_structdecl`, etc.), file path, lines, `ast_id`, and `tag_id`.
- **`GET /api/symbols/lookup?version_name={v}&q={q}&limit=20`** (`lookup_symbols`):
  - Ultra-fast autocomplete prefix string suggestions for topbar search and quick lookup.

#### 10. Interactive Dependency DAG Graph Endpoint
- **`GET /api/version/{version_name}/kconfig/graph/{symbol_name}?depth=2`** (`get_kconfig_graph`):
  - Traverses `depends_on` (outgoing) and reverse-dependency `selects` / `selected_by` (incoming) relations. Returns a node-link graph payload for visual Canvas rendering.

#### 11. Smart Kconfig Auto-Solver & Config Compare Endpoints
- **`POST /api/version/{version_name}/kconfig/autosolve`** (`autosolve_kconfig`):
  - Accepts `AutoSolveRequest`. Evaluates prerequisite dependency trees for blocked symbols and computes the minimal set of required symbol toggles to satisfy all constraints.
- **`POST /api/version/{version_name}/kconfig/diff_config`** (`diff_kconfig_configurations`):
  - Accepts `DiffConfigRequest`. Compares active configuration against a defconfig or custom config dictionary, returning match percentages and mismatch tables.

#### 12. Patch Reviewer & Subsystem Maintainers Matcher Endpoint
- **`POST /api/version/{version_name}/patch/maintainers`** (`match_patch_maintainers`):
  - Accepts `PatchReviewRequest`. Parses unified diff text, extracts touched files and line ranges, executes pattern matching against all 500+ kernel subsystems, and formats `get_maintainer.pl`-style `TO:` (Maintainers) and `CC:` (Reviewers & Mailing Lists) recipient rosters.

#### 13. AST Container Tree & Hierarchy Endpoint
- **`GET /api/ast/{ast_id}/tree?version_name={v}&depth=3`** (`get_ast_container_tree`):
  - Recursively resolves `m_ast_container` child relationships down to requested depth (1-10). Annotates each node with `tag_id` using index `idx_tag_ast_id` on `m_tag`. Nodes without tags (such as inner statements, expressions, and initializers from the upgraded `c_ast` parser) return `tag_id: None`, enabling the UI to suppress tag timeline buttons and tag badges for untagged constructs.

#### 14. Clang `compile_commands.json` Exporter Endpoint
- **`GET /api/version/{version_name}/export/compile_commands?arch=x86`** (`export_compile_commands`):
  - Generates standard JSON Compilation Database mapping all C source files to architecture compiler flags for integration into VS Code, `clangd`, and CLion.

#### 15. C Struct Memory Layout & Pahole Visualizer Endpoint
- **`GET /api/version/{version_name}/struct/layout/{struct_name}`** (`get_struct_layout`):
  - Traverses `m_ast`, `m_ast_container`, and `m_type_descriptor` for any C `struct` or `union`. Calculates byte offsets, member sizes, alignments, internal padding holes, tail padding, and 64-byte cacheline groupings with cacheline crossing warnings and reordering optimizations.

#### 16. Codebase Treemap Map Hierarchy Endpoint
- **`GET /api/version/{version_name}/treemap?max_depth=3`** (`get_codebase_treemap`):
  - Returns nested directory and file hierarchies with file counts and line weights for squarified treemap rendering.

#### 17. Function Call Graph (Callers/Callees Flow) Endpoint
- **`GET /api/version/{version_name}/callgraph/{function_name}`** (`get_function_callgraph`):
  - Discovers function definition in `m_symbol_def` and maps inbound call sites across other files via `m_symbol_ref` (Call role=3). Discovers outbound child function invocations and helper calls within the function's AST node boundaries. Backed by composite index lookups for immediate response.

#### 18. In-Browser Patch Staging & `git format-patch` Generator Endpoint
- **`POST /api/version/{version_name}/patch/format`** (`generate_formatted_patch`):
  - Accepts `FormatPatchRequest`. Computes unified diffs with Python's `difflib`, resolves subsystem maintainers, and generates standard `git format-patch` RFC-2822 email text with `From:`, `Date:`, `Subject: [PATCH]`, `To:`, `Cc:`, `Signed-off-by:`, and diff statistics.

#### 19. Dev Introspection Endpoints
- **`GET /api/dev/tables`** (`get_dev_table_counts`):
  - Executes `SELECT COUNT(*)` across schema tables in MySQL and returns live row counts.
- **`GET /api/dev/endpoints`** (`get_dev_endpoints`):
  - Returns an interactive schema catalog of all API endpoints with descriptions and sample test arguments.

---

## 4. Frontend Client Architecture (`webapp/webapp.html`)

The frontend is a zero-dependency, ultra-responsive Single-Page Application implemented in **Vanilla HTML5, CSS3, and JavaScript (ES2022)**.

### 4.1. Global State Management

The client manages centralized state in JavaScript memory:

| State Variable | Type | Purpose & Lifecycle |
| :--- | :--- | :--- |
| `API_BASE` | `string` | Backend URL (auto-detected from `window.location.origin` or overridden via `localStorage` / prompt). |
| `currentVersion` | `string` | Active Linux release tag (e.g. `"v3.0"`). |
| `currentAppMode` | `string` | Active primary workspace mode (`"explorer"`, `"kconfig"`, `"maintainers"`, `"credits"`). |
| `openTabs` | `Array<TabObject>` | Multi-tab document stack: `[{ id, type, version, path, title, targetStartLine, targetEndLine, data }]`. |
| `activeTabIndex` | `number` | Index of the currently focused tab in `openTabs`. |
| `selectedAstId` | `number \| null` | Focused AST node in AST Container Inspector. |
| `currentInspectorDepth` | `number` | Depth slider value for recursive AST hierarchy tree (1 to 10). |
| `highlightSettings` | `Record<string, { color, enabled, label, lang, group }>` | Semantic syntax category color palette with language tags and group classifications. |
| `activeHighlightLang` | `string` | Active language filter tab (`"all"`, `"c"`, `"cppro"`, `"asm"`, `"kconfig"`). |
| `cpproHighlightEnabled` | `boolean` | Toggle state for `#if`/`#ifdef`/`#elif`/`#else` conditional scope highlighting. |
| `blameViewEnabled` | `boolean` | Toggle state for interactive Git Blame line annotations in the code gutter. |
| `kconfigTreeData` | `Array<KconfigNode>` | Complete hierarchical Kconfig tree data for the active architecture. |
| `kconfigValues` | `Record<string, string>` | Current working symbol assignments dictionary (`"y"`, `"m"`, `"n"`, string values). |
| `kconfigForcedSymbols` | `Record<string, { forcedBy: string, value: string }>` | Symbols forced to active values by `selects` relations. |
| `kconfigUnmetSymbols` | `Record<string, { reason: string }>` | Symbols with unsatisfied `depends_on` conditions. |
| `kconfigDrillDownMode` | `boolean` | Navigation mode toggle (`true` = drill-down submenu mode, `false` = full tree mode). |
| `kconfigMenuPath` | `Array<{ treeId, title }>` | Active submenu breadcrumb navigation trail in drill-down mode. |
| `targetProfile` | `ProfileObject` | Active target architecture, bitness, and compiler toolchain settings. |
| `activeDefconfigName` | `string \| null` | Name of the currently loaded defconfig baseline. |
| `tuiCursorRow` | `number` | Active highlighted row index in Terminal Menuconfig (TUI). |
| `tuiActiveButtonIndex` | `number` | Focused action button in TUI bottom bar (0 to 6). |
| `activeDagLayoutMode` | `string` | Active DAG layout algorithm (`"sugiyama_lr"`, `"sugiyama_tb"`, `"force"`, `"radial"`). |
| `activeTourPreset` | `TourPreset \| null`| Active code tour definition and current step index. |

---

### 4.2. Client-Side IndexedDB Storage Tier (`IDBStorageManager`)

The frontend integrates an enterprise-grade client-side storage engine using the browser's **IndexedDB API**:

- **Database Name**: `KernelInfoDB`
- **Database Version**: `1`
- **Dedicated Object Stores**:
  1. `metadata`: Release version catalogs, type descriptors, target architecture presets.
  2. `file_cache`: Source code tags, AST coordinate maps, container depths, and file lifecycle data.
  3. `kconfig_cache`: Scoped Menuconfig trees, symbol relations, defconfig key-value mappings.
  4. `subsystems_cache`: Subsystem section records, maintainer rosters, pattern rules, credits entries.
  5. `blame_cache`: Line-by-line git blame annotations and author mappings.
- **Storage Methods**:
  - `IDBStorageManager.get(storeName, key)`: Retrieves cached object; returns `null` on cache miss.
  - `IDBStorageManager.set(storeName, key, value)`: Stores serializable object asynchronously.
  - `IDBStorageManager.clear(storeName)`: Clears a specific store or all stores.
  - `IDBStorageManager.getStats()`: Computes total cached record counts across all stores.
- **Cache-First Acceleration**: Tab switches, Kconfig navigation, maintainer lookups, and AST coordinate queries resolve in **< 1ms** directly from IndexedDB without network latency.

---

### 4.3. URL Hash Synchronization & Deep Linking

The client synchronizes navigation state with browser URL hashes:
- **Format**: `#{version}/{path}:L{startLine}-L{endLine}` (e.g. `#v3.0/include/linux/lockd/bind.h:L14-L28`).
- **Bi-directional Binding**:
  - Clicking a line or Shift+clicking a line range updates the URL hash via `history.replaceState()`.
  - Clicking on an active single line toggles off/deselects the line range and clears hash coordinates.
  - Direct URL loading or hash changes automatically open the matching tab, scroll the target line into view (`scrollIntoView({ block: 'center' })`), and apply temporary highlight pulses.

---

## 5. Client Workspaces & Feature Subsystems

The frontend features **17 dedicated workspace views** and **18 tab types**:

```
+---------------------------------------------------------------------------------------------------------------+
|                                            17 FRONTEND WORKSPACES                                             |
+---------------------------------------------------------------------------------------------------------------+
| 1. #explorerWorkspace      | Source code tree, code viewer, token maps, containers, git blame, token popover  |
| 2. #astInspector (panel)   | Slide-over panel for recursive AST container hierarchy with conditional tags     |
| 3. #kconfigWorkspace       | Menuconfig GUI: Drill-Down vs Full Tree, 20-pass constraint engine, search, insp |
| 4. #tuiWorkspace           | Authentic Terminal Menuconfig emulator with full keyboard event interceptor      |
| 5. #maintainersWorkspace   | Subsystem catalog grid, maintainers/reviewers rosters, pattern rules, CREDITS ⭐ |
| 6. #creditsWorkspace       | Linux CREDITS directory, developer biographies, PGP keys, snail mail, roles      |
| 7. #timelineWorkspace      | Top 10 Contributor Leaderboard ranking & chronological release patch stream      |
| 8. #diffWorkspace          | Cross-Version Diff: File Tree Diff & Kconfig Symbol Evolution with delta stats   |
| 9. #patchWorkspace         | Patch Reviewer & Subsystem Matcher (get_maintainer.pl TO/CC recipient generator) |
| 10. #treemapWorkspace      | Interactive Squarified Codebase Treemap Map with breadcrumbs & directory colors  |
| 11. #dagWorkspace          | HTML5 Canvas Dependency DAG Graph: Sugiyama (LR/TB), Force-Directed & Radial     |
| 12. #structWorkspace       | C Struct Memory Layout & Pahole Visualizer: byte offsets, padding, 64B cachelines|
| 13. #callgraphWorkspace    | Function Call Graph: bidirectional Inbound Callers & Outbound Callees flow trees |
| 14. #patchStudioWorkspace  | In-Browser Patch Staging Studio & RFC-2822 git format-patch email generator      |
| 15. #xrefWorkspace         | Global Symbol Cross-Reference Studio: definitions, categorized references map    |
| 16. #subsystemWorkspace    | Subsystem Detail Tab View: maintainers, reviewers, mailing lists, matching files |
| 17. #personWorkspace       | Developer Profile Tab View: metrics, Latest Patch card, bio, CREDITS match, logs|
| 18. #commitWorkspace       | Git Commit Detail Tab View: SHA, subject, body, trailers, touched files, tags    |
+---------------------------------------------------------------------------------------------------------------+
```

### 5.1. Explorer & Source Code AST Viewer (`#explorerWorkspace`)

#### File Tree Sidebar (`#sidebarTree`)
- Hierarchical folder traversal with parent directory navigation (`.. (Parent)`).
- Instant client-side filtering via debounced search input (`filterSidebarTree()`).
- Direct tab spawning from folder view or file clicks.

#### Tab Management Bar (`#tabsBar`)
- Multi-tab document interface supporting all 21 tab types.
- Visual status indicators (`📁` for directories, `📄` for files, `🔀` for diff, `⚙️` for kconfig, `📟` for tui, etc.).
- Tab close (`✕`) and new tab (`＋ Tab`) actions.

#### Highlighting & Token Resolution Algorithm (`buildHighlightedSource` & `highlightLineText`)
- **Outer Tag Isolation**: Sorts tags by `line_s` ASC, `char_s` ASC, and span length DESC to identify primary outermost base lines.
- **Client-Side Lexical Syntax Tokenizer (`lexCLine`)**:
  - High-performance, single-pass lexical scanner that analyzes every source line into discrete tokens (keywords, storage classes, primitive types, struct/union/enum declarations, identifiers, member references, function calls, numeric/hex constants, string/char literals, operators, and preprocessor directives).
  - Maintains state across multi-line block comments (`/* ... */`).
  - Differentiates member accesses (`.` and `->`) to highlight member fields in Coral/Red (`#e06c75`).
  - Differentiates function names/calls (`foo(...)`) in Blue (`#61afef`).
  - Identifies uppercase macros/constants in Amber (`#d19a66`).
  - Maps preprocessor directives (`#include`, `#define`, `#if`, `#ifdef`, etc.) and header strings (`<...>`, `"..."`).
- **Sub-Token Semantic Coloring Inside Functions & Structs**:
  - Eliminates monochromatic parent blocks: in constructs such as `struct s { int a; }` or function bodies, `struct s` receives struct styling, `int` is styled as a primitive type (`#56b6c2`), and `a` is styled as a member/identifier rather than uniformly inheriting the parent tag's type.
  - Sub-tokens retain the enclosing AST parent tag's context (`ast_id`, `ast_name`, `type_name`) so clicking still opens the AST Inspector (`inspectAst()`).
- **Innermost Spatial Token Resolution**:
  - For each lexical token, resolves overlapping spatial coordinate maps (`m_map_ast` and `m_bridge_tag`). If a specific child map is found, its construct type takes priority over the lexical default.
- **Dual-Context Rich Tooltips**:
  - Displays both fine-grained token classification and enclosing AST node metadata: `Token: <token_label> (<typeKey>) • AST: <ast_name> (<type_name>)`.
- **Comprehensive Semantic AST Syntax Highlighting**:
  - Full construct-specific syntax coloring mapped directly to `ASTT` enum construct categories across all supported languages (C/C++, Preprocessor, Assembly, Kconfig, and Maintainers).
  - Uses the **One Dark Pro** palette as standard:
    - **Keywords & Control Flow (`#c678dd`)**: `C_Keyword`, `C_IfStmt`, `C_SwitchStmt`, `C_WhileStmt`, `C_ForStmt`, `C_ReturnStmt`, etc.
    - **Compound Types & Declarations (`#e5c07b`)**: `C_struct`, `C_structdecl`, `C_union`, `C_enum`, `C_enumdecl`.
    - **Primitive Types (`#56b6c2`)**: `C_void`, `C_int`, `C_char`, `C_short`, `C_long`, `C_bool`, `C_float`, `C_double`, `C_signed`, `C_unsigned`, kernel typedefs (`u8`, `u16`, `u32`, `u64`, `atomic_t`, etc.).
    - **Storage Classes & Specifiers (`#c678dd`)**: `C_SCstatic`, `C_SCextern`, `C_SCtypedef`, `C_FSinline`, `C_AS__Alignas`.
    - **Type Qualifiers (`#e5c07b`)**: `C_Qconst`, `C_Qvolatile`, `C_Qrestrict`, `C_Q_Atomic`.
    - **Functions & Prototypes (`#61afef`)**: `C_functionproto`, `C_functionprotodecl`, `C_CallExpr`.
    - **Pointers & Operators (`#56b6c2` / `#abb2bf`)**: `C_pointer`, `C_BinaryOperator`, `C_UnaryOperator`.
    - **Member References (`#e06c75`)**: `C_MemberRefExpr` (`.` and `->`).
    - **Literals & Constants (`#98c379` / `#d19a66`)**: `C_Literal_String` (strings/characters), `C_Literal_Number` (numbers/hex/floats).
    - **Preprocessor & Conditionals (`#d19a66` / `#98c379` / `#e06c75`)**: `CPPro_define`, `CPPro_include`, `CPPro_if`, `CPPro_ifdef`, `CPPro_else`, `CPPro_error`.
    - **Comments (`#5c6370`)**: `C_Comment`, `ASM_Comment`.
    - **Assembly (`#d19a66` / `#61afef` / `#e5c07b`)**: `ASM_Macro`, `ASM_Directive`, `ASM_Instruction`, `ASM_Label`.
    - **Kconfig Constructs (`#61afef` / `#e5c07b` / `#98c379` / `#c678dd`)**: `Kconfig_Menu`, `Kconfig_Config`, `Kconfig_Source`, `Kconfig_Depends_On`.
- **Kconfig Symbol Auto-Linking**: Uses regular expressions (`\bCONFIG_([A-Za-z0-9_]+)\b`) to automatically detect configuration symbols within comments, macros, and source code, rendering clickable interactive chips that open the Kconfig Symbol Modal.
- **CPPro Conditional Scope Folding & Highlighting**:
  - Identifies multi-line `#if`, `#ifdef`, `#ifndef`, `#elif`, `#else`, and `#endif` blocks.
  - Inserts custom collapse buttons (`▼`/`▶`) on the first line of each conditional block.
  - Provides toolbar buttons: `Highlight Conditionals`, `Collapse #if`, `Expand #if`, `Expand All`, `Collapse All`.
- **Interactive Git Blame Gutter**:
  - Displays author avatars with deterministic HSL colors, initials, author names, and short commit SHAs.
  - Multi-commit badge (`+N`) for code blocks modified across multiple commits.
  - Hovering over a blame cell highlights all source lines belonging to that tag snippet (`tag-blame-highlight`).
  - Clicking a blame cell opens the developer profile modal or commit modal.
- **Global Symbol Search Bar (`#globalSymbolSearchInput` & `#globalSymbolSearchDropdown`)**:
  - Centered in the top navigation bar with `Ctrl+K` hotkey activation.
  - Debounced (250ms) typeahead autocomplete querying `/api/symbols/search` powered by `m_symbol_def`.
  - Supports keyboard navigation (`ArrowUp`, `ArrowDown`, `Enter`, `Escape`).
  - Directly opens the target file and jumps to the symbol definition line (`openPathAndHighlightLine`).
- **Interactive Token Context Action Popover (`#tokenActionPopover`)**:
  - Clicking any semantic source code token in the viewer opens a lightweight context popover with 4 one-click actions:
    1. **Go to Definition**: Looks up the symbol definition in `m_symbol_def` and navigates directly to the source file and line.
    2. **Find References (XRef)**: Opens the Categorized XRef studio displaying all callers, member references, and type usages.
    3. **Function Call Graph**: Opens the bidirectional Inbound Callers / Outbound Callees call graph.
    4. **Inspect AST Node**: Slides open the AST Container Inspector for the enclosing AST node.

### 5.2. AST Container Hierarchy Inspector (`#astInspector`)
- Slide-over panel for deep structural AST analysis down to 10 container depth levels.
- Displays AST Node ID, symbol name, construct type descriptor, and raw JSON parser dump.
- **Upgraded C-AST Parser Construct Badges**:
  - Highlights specialized constructs from the upgraded `c_ast` parser: Statements (`is_stmt`), Expressions (`is_expr`), Designated Initializer Lists (`is_init`), and Assembly (`is_asm`).
- **Conditional Tag Badge & Timeline Suppression**:
  - Tag timeline badges/buttons (`🕒 Tag #...`) are strictly rendered *only* when the AST node has an associated tag in `m_tag` (`node.tag_id`).
  - For untagged constructs (e.g. inner statements, sub-expressions, and initializers where tag creation is suppressed), tag buttons and timeline triggers are completely omitted, preventing phantom lookups.
- Recursively renders child container relationships with priority ranks and relation types.

### 5.3. Kconfig Web Menuconfig GUI (`#kconfigWorkspace`)
- **Dual View Modes**:
  1. **Drill-Down Navigation Mode (`kconfigDrillDownMode = true`)**: Displays only items in the current submenu with breadcrumb navigation and parent menu jump button (`[ ⬆ .. ] Up to Parent Menu`).
  2. **Full Tree Mode (`kconfigDrillDownMode = false`)**: Displays complete multi-level tree hierarchy with indentation.
- **Global Cross-Directory Symbol Search**: Searches symbol names, prompts, help text, and file paths.
- **20-Pass Bidirectional Constraint Propagation Engine (`evaluateKconfigConstraints`)**:
  - **Forced Symbols (`selects`)**: Enables target symbols and evaluates cascading selections iteratively up to 20 passes. Displays `🔒 Forced` badges with tooltips indicating the active forcing symbols.
  - **Dependency Guarding (`depends_on`)**: Evaluates boolean expressions (`&&`, `||`, `!`, `!=`, `=`). Unmet dependencies display `⚠️ Unmet` badges.
  - **Derived Architecture & Bitness Synchronization**: Toggling `CONFIG_64BIT` dynamically updates `X86_64` and `X86_32` state and toolchain profiles.

### 5.4. Terminal Menuconfig (TUI) Engine (`#tuiWorkspace`)
- Authentic browser-based ncurses/dialog replica styled after Linux `make menuconfig`.
- **Keyboard Navigation Engine (`onTuiKeyDown`)**:
  - `Up` / `Down` / `k` / `j`: Move cursor row.
  - `Left` / `Right` / `Tab`: Cycle action buttons (`< Select >`, `< Defconfig >`, `< Exit >`, `< Target >`, `< Help >`, `< Save >`, `< Load >`).
  - `Enter`: Execute active button or enter submenu.
  - `Space` / `Y` / `N` / `M`: Toggle/set configuration values.
  - `?` / `H`: Open contextual documentation dialog.
  - `/`: Open symbol search dialog.
  - `D`: Open Defconfig manager.
  - `T`: Open Target Architecture & Toolchain profile manager.
  - `Esc` / `Q`: Exit submenu or return to Web GUI.

### 5.5. Interactive Canvas Dependency DAG Visualizer (`#dagWorkspace` & `#graphModal`)
- Interactive HTML5 Canvas 2D rendering engine for Kconfig relations and AST hierarchies.
- **3 Layout Algorithms**:
  1. **Sugiyama Hierarchical Layout (`layoutHierarchicalSugiyama`)**: Supports Left-to-Right (`LR`) and Top-to-Bottom (`TB`) orientations with topological layer assignment, crossing minimization, and orthogonal/bezier edge routing.
  2. **Force-Directed Simulation (`layoutForceSimulation`)**: Coulomb repulsion between nodes, Hooke spring attraction along edges, and velocity damping.
  3. **Concentric Radial Layout (`layoutConcentricRadial`)**: Focuses on root node at center with concentric orbital shells based on graph hop distance.
- **Canvas Interaction**: Smooth drag-and-pan, mouse wheel zooming, node selection, expanding connected neighbors, node attribute cards, and 1-click **Export as SVG** (`exportDagAsSvg`).

### 5.6. C Struct Memory Layout & Pahole Alignment Visualizer (`#structWorkspace` & `#structLayoutModal`)
- Computes member byte offsets, sizes, alignments, internal padding holes, and tail padding for C structs and unions.
- Groups struct fields into 64-byte L1/L2 cacheline blocks.
- Highlights cacheline crossing warnings and provides greedy alignment reordering optimizations that save memory bytes.

### 5.7. Squarified Codebase Treemap Map (`#treemapWorkspace`)
- Implements the Bruls-Huizing-van Wijk squarified treemap partition algorithm.
- Displays color-coded directory clusters (`fs/`, `drivers/`, `net/`, `kernel/`, `mm/`, `arch/`) with adjustable depth slider (1-5), hover tooltips, and interactive drill-down breadcrumb navigation.

### 5.8. Function Call Graph Studio (`#callgraphWorkspace` & `#callGraphModal`)
- Bidirectional call flow trees powered by `m_symbol_def` and `m_symbol_ref` (Call role=3):
  - **Inbound Callers**: Discovers all external call sites with file paths, line numbers, and jump-to-source navigation.
  - **Outbound Callees**: Discovers child function invocations and helper calls within the function body.
- 1-click jump-to-source navigation into the Explorer.

### 5.9. In-Browser Patch Staging & `git format-patch` Studio (`#patchStudioWorkspace` & `#patchStudioModal`)
- Side-by-side / unified diff computation using Python's `difflib`.
- Auto-resolves subsystem maintainers and formats standard RFC-2822 `git format-patch` emails with `From:`, `Date:`, `Subject: [PATCH]`, `To:`, `Cc:`, `Signed-off-by:`, and diff statistics.
- 1-click copy to clipboard and `.patch` file export.

### 5.10. Cross-Version Semantic Diff (`#diffWorkspace`)
- Dual sub-tabs:
  - **File Tree Diff**: Categorizes file paths into `added`, `removed`, `modified`, and `unchanged` with delta summary cards and status filters.
  - **Kconfig Symbol Evolution**: Tracks symbol additions, removals, prompt alterations, type changes, and default value modifications across releases.

### 5.11. Global Symbol Cross-Reference (XRef) (`#xrefWorkspace` & `#xrefModal`)
- Powered by `m_symbol_def` and `m_symbol_ref` with composite database index acceleration (`idx_symbol_def_vid_name`, `idx_symbol_ref_vid_ast`).
- Categorizes all occurrences into structured sections:
  - **Authoritative Definitions**: Declared location with construct type, file path, and line bounds.
  - **Function Calls (`Call`)**: Inbound call sites across the codebase.
  - **Member References (`MemberRef`)**: Struct/union field accesses (`.` and `->`).
  - **Type Usages (`TypeUsage`)**: Variable, parameter, and cast references.
  - **Declarations (`Declaration`)**: Forward prototypes and external declarations.
- 1-click coordinate jump (`openPathAndHighlightLine`) with smooth scrolling.

### 5.12. Patch Reviewer & Subsystem Maintainers Matcher (`#patchWorkspace`)
- Parses unified diff text, extracts touched files and line ranges, and formats `get_maintainer.pl`-style `TO:` (Maintainers) and `CC:` (Reviewers & Mailing Lists) recipient rosters with 1-click clipboard copy.

### 5.13. Dev Introspection Dashboard & API Runner (`#devModal`)
- Row count dashboard across all schema tables with 1-click refresh.
- Interactive API endpoint runner with parameter forms, dynamic URL builders, and live JSON response inspection.

---

## 6. Complete Inventory of Client Modals

The application provides specialized modals accessible via toolbars, hotkeys, or contextual actions:

| Modal ID | Modal Title / Purpose | Trigger Condition | Key Actions & Controls |
| :--- | :--- | :--- | :--- |
| `#kconfigProfileModal` | `🌐 Target Architecture & Compiler Profile` | Toolbar button / hotkey in Kconfig GUI | Architecture selector, bitness toggle, toolchain selector (GCC/Clang), cross-compile prefix, apply profile. |
| `#tuiProfileModal` | `⚙️ Architecture Default Configuration (Defconfig)` | `< Target >` button / `T` hotkey in TUI | TUI architecture selector and profile settings. |
| `#tuiHelpModal` | `TUI Contextual Help & Documentation` | `< Help >` button / `?` / `H` hotkey in TUI | Navigation keys legend, hotkeys reference, symbol documentation. |
| `#tuiSearchModal` | `TUI Symbol Search Dialog` | `/` hotkey in TUI | Symbol search input, matching results list, direct jump to symbol. |
| `#tuiDefconfigModal` | `TUI Defconfig Profile Selector` | `< Defconfig >` button / `D` hotkey in TUI | Architecture defconfig catalog, apply canonical baseline, apply selected defconfig. |
| `#kconfigDefconfigModal` | `⚙️ Defconfig Baseline Selector` | Toolbar button in Kconfig GUI | Architecture defconfig catalog, canonical defconfig button, search filter, apply defconfig. |
| `#kconfigSymbolInfoModal`| `Interactive Kconfig Symbol Inspector` | Clicking any `CONFIG_*` chip in code or tree | Complete symbol metadata, dependencies, reverse dependencies, compiled objects, set value from code, jump to tree. |
| `#fileHistoryModal` | `📜 File Revision Timeline` | Clicking `(Modified in v3.0)` badge in file header | Complete version lifespan table, inception release, concluding release, change status badges, open in version links. |
| `#kconfigImportModal` | `📥 Import Linux Kernel .config File` | Toolbar `Import .config` button | File picker (`FileReader`), raw text input area, parse and batch-apply symbol values. |
| `#kconfigExportModal` | `💾 Export Kernel-Compatible .config` | Toolbar `Export .config` button / `< Save >` | Linux `.config` syntax generator, 1-click copy to clipboard, download `.config` file. |
| `#highlightModal` | `Highlight Color Palette & Visibility Manager` | Top nav / editor toolbar `🎨 Syntax Palette` button | Tabbed multi-language customizer (All, C, CPPro, ASM, Kconfig), grouped category sections, search/filter box, live swatches, visibility checkboxes, reset defaults. |
| `#devModal` | `⚙️ Developer Introspection & Endpoint Runner` | Dev button in bottom footer | Database table row counts, IndexedDB cache stats, clear IDB cache, interactive API endpoint parameter runner. |
| `#subsystemModal` | `🏷️ Subsystem Details` | Clicking subsystem title or maintainer badge | Maintainers/reviewers rosters, mailing list, SCM tree, web page, pattern rules, matching repository files table. |
| `#personModal` | `👤 Developer & Contributor Profile` | Clicking developer avatar or maintainer name | Bio, CREDITS cross-reference (`⭐`), Git contribution counters, Latest Patch spotlight, maintained subsystems roster. |
| `#commitModal` | `📜 Commit Details` | Clicking commit hash or blame cell | SHA, subject, body, trailers, pull request merge origin (`🌿`), shortlog, touched files, modified tags. |
| `#xrefModal` | `🌐 Global Symbol Cross-Reference (XRef)` | Top nav / editor context action | Symbol input, primary definition card, categorized usages table (Calls, MemberRefs, TypeUsages, Declarations) with jump links. |
| `#graphModal` | `🕸️ Interactive Dependency & Container DAG` | Top nav / Kconfig inspector action | Fullscreen Canvas visualizer, layout mode selector (Sugiyama LR/TB, Force, Radial), node spacing, zoom, SVG export. |
| `#exportModal` | `💾 Export Hub & Integration Tools` | Top nav export action | Clang `compile_commands.json` exporter, Linux `.config` downloader. |
| `#kconfigCompareModal` | `📊 Compare Configuration with Defconfig` | Toolbar `Compare Defconfig` button | Active config vs defconfig selector, match percentage gauge, mismatch symbol comparison table. |
| `#kconfigAutosolveModal` | `🪄 Auto-Solve Prerequisite Dependencies` | Clicking `🪄 Auto-Solve` badge on unmet symbol| Prerequisite dependency tree, minimal required toggles list, 1-click apply all toggles. |
| `#structLayoutModal` | `📐 C Struct Memory Layout & Pahole Visualizer`| Top nav / AST inspector action | Struct name input, total size, padding bytes count, 64B cacheline block visualizer, alignment reordering panel. |
| `#callGraphModal` | `🌳 Function Call Graph & Callers / Callees Flow`| Top nav / function context action | Function name input, two-column caller/callee trees with jump-to-source links. |
| `#patchStudioModal` | `✍️ In-Browser Patch Staging Studio` | Top nav / editor patch action | File path input, original vs modified editor, real-time diff preview, RFC-2822 format-patch email generator. |
| `#tagTimelineModal` | `🕒 Tag Evolution & Cross-Version Timeline` | Clicking `🕒 Tag #...` button in code or AST inspector | Cross-version timeline slider, status badges (unmodified, modified, moved), line additions/deletions, animated player. |

---

## 7. Mathematical & Algorithmic Formulations

### 7.1. Innermost AST Token Winning Algorithm
Given a source line with character indices $c \in [0, L-1]$ and a set of spatial token maps $M = \{m_1, m_2, \dots, m_k\}$ overlapping character $c$:
$$\text{Winner}(c) = \arg\min_{m \in M} (\text{char\_e}(m) - \text{char\_s}(m))$$
Ties are broken by highest AST node priority rank. Adjacent character columns sharing the same winning token are compressed into contiguous DOM spans.

### 7.2. Kconfig 20-Pass Iterative Selection Closure
Let $V_t(s)$ be the value of symbol $s$ at iteration $t$. For each selection relation $r = (s \xrightarrow{\text{select}} u)$ where $V_t(s) \in \{\text{'y'}, \text{'m'}\}$:
$$V_{t+1}(u) = \max(V_t(u), V_t(s))$$
The algorithm iterates until $V_{t+1} = V_t$ or $t = 20$. Any symbol where $V(u)$ was elevated by a selection is marked `🔒 Forced` with reference to $s$.

### 7.3. Sugiyama Hierarchical DAG Layout
1. **Layer Assignment**: Assigns layer rank $L(v)$ using longest path from source nodes:
   $$L(v) = \max_{(u, v) \in E} (L(u) + 1)$$
2. **Crossing Minimization**: Reorders vertices within layer $L_k$ using the barycenter heuristic:
   $$\text{Barycenter}(v) = \frac{1}{|N(v) \cap L_{k-1}|} \sum_{u \in N(v) \cap L_{k-1}} \text{pos}(u)$$
3. **Coordinate Assignment**: Distributes layers along primary axis with minimum node spacing $S_x, S_y$.

### 7.4. Pahole Memory Offset & Padding Calculation
For a struct with ordered members $m_1, m_2, \dots, m_n$ having sizes $s_i$ and alignment requirements $a_i$:
$$\text{Offset}(m_1) = 0$$
$$\text{PaddingBefore}(m_i) = (\text{Offset}(m_{i-1}) + s_{i-1}) \pmod{a_i} \neq 0 \implies a_i - ((\text{Offset}(m_{i-1}) + s_{i-1}) \pmod{a_i})$$
$$\text{Offset}(m_i) = \text{Offset}(m_{i-1}) + s_{i-1} + \text{PaddingBefore}(m_i)$$
$$\text{TotalStructSize} = \left\lceil \frac{\text{Offset}(m_n) + s_n}{\max(a_i)} \right\rceil \times \max(a_i)$$

---

## 8. Audit of Inefficiencies, Bottlenecks & Optimization Plan

### 8.1. Backend API Server Inefficiencies (`webapp/main.py`)
1. **N+1 Queries in File Browsing (`browse_path` & `get_file_by_id`)**: Large `IN (...)` queries for spatial maps when files have 500+ tags.
2. **Dynamic Subsystem Pattern Matching Fallback**: Fallback to regex evaluation when `m_maintainer_file` is not materialized.
3. **Git Subprocess Invocations**: Repetitive spawning of `git -C linux log` or `git show` without unified disk caching.
4. **SQL String Pattern Searches**: Broad `LIKE %query%` searches across 15,000+ Kconfig symbols without fulltext indexes.

### 8.2. Frontend Client Inefficiencies (`webapp/webapp.html`)
1. **Monolithic Code Highlighting DOM Construction**: Large files (5,000+ lines) build large HTML strings in a single pass.
2. **Full Kconfig Tree Re-rendering**: Toggling a single boolean in Full Tree mode (2,000+ nodes) triggers a full DOM rebuild instead of targeted in-place mutation.
3. **Non-Virtualized Subsystem & Credits Grids**: 500+ cards rendered into DOM simultaneously.

### 8.3. Optimization Roadmap
- **Client-Side IndexedDB Tier (`IDBStorageManager`)**: Cache-first resolution for immutable release datasets (<1ms response).
- **Direct VID Communication**: Caching `vname <-> vid` to eliminate repetitive `SELECT vid FROM m_v_main` roundtrips.
- **Universal Input Debouncing**: Applied 250ms debouncing across all search inputs.
- **Static Precompiled Regular Expressions**: Global regex singletons eliminating per-character allocations.

---

*Specification Author: Antigravity AI Engineering Assistant*
*Repository: KernelInfo-Parser / MapleCircuit*
