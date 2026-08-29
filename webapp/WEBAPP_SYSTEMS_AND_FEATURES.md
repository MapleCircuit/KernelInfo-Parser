# KernelInfo-Parser Developer Web Application: Full Architecture, Systems & Features Specification

This document provides an exhaustive, authoritative technical specification of all features, subsystems, REST endpoints, UI controllers, rendering algorithms, and state machines within the **KernelInfo-Parser Developer Web Application** (comprising the **FastAPI Backend Server** in [`webapp/main.py`](file:///home/scottviger/dev/KernelInfo-Parser/webapp/main.py) and the **Single-Page Application Client** in [`webapp/webapp.html`](file:///home/scottviger/dev/KernelInfo-Parser/webapp/webapp.html)).

---

## 1. Executive Summary & Architectural Topology

The KernelInfo-Parser Web Application is a high-performance developer introspection and analysis platform designed to explore Linux kernel source code, relational Abstract Syntax Trees (AST), spatial coordinate mappings, hierarchical configuration menus (Kconfig), subsystem maintainers, credits, git commit timelines, and code blame annotations across kernel release versions (e.g. `v3.0`).

```
+---------------------------------------------------------------------------------------+
|                                    CLIENT BROWSER                                     |
|  Single-Page Application (Vanilla HTML5 / CSS3 / ES2022 JavaScript - No Framework)   |
|                                                                                       |
|  +-------------------+  +--------------------+  +-------------------+  +-----------+  |
|  | Explorer & AST    |  | Kconfig Web GUI    |  | Terminal TUI      |  | Maintain- |  |
|  | - File Tree       |  | - Drill-Down / Tree|  | - Keyboard Nav    |  |   ers Hub |  |
|  | - Token Highlight |  | - Constraint Engine|  | - ANSI Dialogs    |  | - Roster  |  |
|  | - Line Linking    |  | - Live Edit & Sync |  | - Hotkeys (?/Y/N) |  | - Matcher |  |
|  | - Container Depth |  | - Target Profile   |  | - Search (/ )     |  | - Files   |  |
|  | - Git Blame / Tag |  | - Defconfig Loader |  | - Defconfig Modal |  | - Credits |  |
|  +-------------------+  +--------------------+  +-------------------+  +-----------+  |
|  +-------------------+  +--------------------+  +-------------------+  +-----------+  |
|  | Credits Directory |  | Commit Timeline    |  | AST Inspector     |  | Dev Modal |  |
|  | - Contributor Bio |  | - Ranking / Stats  |  | - Recursive Tree  |  | - Tables  |  |
|  | - PGP & SCM Links |  | - Merge Patches    |  | - Depth Slider    |  | - Runner  |  |
|  +-------------------+  +--------------------+  +-------------------+  +-----------+  |
+-------------------------------------------+-------------------------------------------+
                                            | REST API Requests (HTTP / JSON)
                                            v
+---------------------------------------------------------------------------------------+
|                                   FASTAPI API SERVER                                  |
|                                 (`webapp/main.py`)                                    |
|                                                                                       |
|  +-------------------------+  +-------------------------+  +-----------------------+  |
|  | Version & File Services |  | Kconfig Engine & Tree   |  | Maintainer & Credits  |  |
|  | - Browse Hierarchy      |  | - Hierarchical Scoping  |  | - Subsystem Catalog   |  |
|  | - Tag Spatial Maps      |  | - Constraint Validation |  | - Person Profiles     |  |
|  | - File History & Blame  |  | - Defconfig Extraction  |  | - Pattern Matching    |  |
|  +-------------------------+  +-------------------------+  +-----------------------+  |
|  +-------------------------+  +-------------------------+  +-----------------------+  |
|  | AST Hierarchy Service   |  | Git Commit & Timeline   |  | Database Manager      |  |
|  | - m_ast_container Tree  |  | - Multi-Contributor Log |  | - Connection Pooling  |  |
|  | - Depth Computation     |  | - Merge Extraction     |  | - Reconnect Resilience|  |
|  +-------------------------+  +-------------------------+  +-----------------------+  |
+-----------------------------------+-----------------------+---------------------------+
                                    |                       |
                 +------------------+                       +------------------+
                 | Direct SQL Queries                                          | Subprocess Queries
                 v                                                             v
+----------------------------------+                        +----------------------------------+
|      MySQL Relational Layer      |                        |       Linux Git Repository       |
|  (`main` / `test` Database)      |                        |        (`linux/` Folder)         |
|  - 25 Relational Schema Tables   |                        |  - Raw Source Code & Headers     |
|  - Spatial Maps & Bridges        |                        |  - Defconfig Files & Makefiles   |
|  - AST Containers & Hashes       |                        |  - Git Log, Blame & Commit Trees |
+----------------------------------+                        +----------------------------------+
```

---

## 2. Database Schema & Relational Integration

The API server connects to the MySQL backend containing 25 normalized tables defined in [`core/DBLayout.py`](file:///home/scottviger/dev/KernelInfo-Parser/core/DBLayout.py):

| Table Name | `table_id` | Primary Key | Key Columns Utilized by Webapp | Role in Webapp Subsystems |
| :--- | :--- | :--- | :--- | :--- |
| `m_v_main` | 0 | `vid` | `vid`, `vname` | Release version selector, version lifespan boundaries (`vid_s`, `vid_e`). |
| `m_file_name` | 1 | `fnid` | `fnid`, `fname` | Unique path registry for tree browsing, sidebar traversal, and file resolution. |
| `m_file` | 2 | `fid` | `fid`, `vid_s`, `vid_e`, `ftype`, `s_stat`, `e_stat` | File lifecycle tracking (`Added`, `Modified`, `Renamed`, `Deleted`), file types (Dir=0, C=1, Kconfig=2, Rust=3). |
| `m_bridge_file` | 3 | `(vid, fnid)` | `vid`, `fnid`, `fid` | Resolves active file instances (`fid`) for a given version (`vid`) and path (`fnid`). |
| `m_moved_file` | 4 | `(s_fid, e_fid)` | `s_fid`, `e_fid` | File renaming and historical movement tracking. |
| `m_type_descriptor` | 5 | `type_id` | `type_id`, `name` | Syntax construct descriptor registry (`C_struct`, `CPPro_define`, `C_Compound`, etc.) used for syntax coloring. |
| `m_ast` | 6 | `ast_id` | `ast_id`, `name`, `type_id` | AST node registry linked to token spans and hierarchy containers. |
| `m_ast_container` | 7 | `(ast_id, priority)` | `ast_id`, `priority`, `type_id`, `ref_ast_id` | Recursive parent-child AST relationships (struct members, function parameters, compound statement blocks). |
| `m_ast_include` | 8 | `ast_id` | `ast_id`, `fnid` | Preprocessor `#include` directive dependencies. |
| `m_ast_debug` | 9 | `ast_id` | `ast_id`, `ast_raw` | Serialized JSON AST dumps for dev inspection. |
| `m_tag` | 10 | `(tag_id, vid_s)` | `tag_id`, `vid_s`, `vid_e`, `code`, `ast_id`, `hl_s`, `hl_l` | Source code snippets, base tokens, and multi-line code tags. |
| `m_bridge_tag` | 11 | `(fid, tag_id)` | `fid`, `tag_id`, `line_s`, `line_e`, `char_s`, `char_e` | Tag spatial coordinates (line numbers and character offsets within source files). |
| `m_map_ast` | 12 | `(map_id, ...)` | `map_id`, `line_s`, `char_s`, `line_e`, `char_e`, `ast_id` | High-precision token coordinate spans inside tag snippets. |
| `m_bridge_map` | 13 | `(tag_id, map_id)`| `tag_id`, `map_id` | Bridges spatial AST token maps to their parent code tags. |
| `m_ast_hash` | 14 | `hash` | `hash`, `ast_id` | SHA-256 structural deduplication cache for AST nodes. |
| `m_kconfig_symbol` | 15 | `(kcid, vid_s)` | `kcid`, `vid_s`, `vid_e`, `name`, `type`, `prompt`, `def_val`, `help`, `ast_id` | Normalized Kconfig symbol definitions, types (bool, tristate, string, hex, int), prompts, default values, and help text. |
| `m_kconfig_relation` | 16 | `(kcid, rel_type, ...)` | `kcid`, `target_name`, `rel_type`, `cond_ast_id`, `priority` | Directed relational dependency graph (`depends_on`=1, `select`=2, `imply`=3, `choice_member`=4). |
| `m_kconfig_tree` | 17 | `(tree_id, vid)` | `tree_id`, `vid`, `parent_id`, `node_type`, `title`, `kcid`, `priority`, `dep_ast_id`, `ast_id` | Hierarchical Menuconfig tree nodes (`menu`=1, `choice`=2, `config`=3, `menuconfig`=4, `comment`=5) with sibling priorities. |
| `m_kconfig_kbuild` | 18 | `(kcid, vid, ...)` | `kcid`, `vid`, `fid`, `compile_mode`, `target_obj` | Kbuild compilation map linking configuration symbols to compiled `.o` object files and source files (`obj-y`=1, `obj-m`=2, `conditional`=3). |
| `m_maintainer_person` | 19 | `person_id` | `person_id`, `name`, `email` | Developer identity registry for maintainers, reviewers, authors, and committers. |
| `m_maintainer_section` | 20 | `(sec_id, vid_s)` | `sec_id`, `vid_s`, `vid_e`, `name`, `status`, `scm_tree`, `web_page`, `mailing_list`, `ast_id` | Subsystem catalog (`EXT4 FILE SYSTEM`, `NETWORKING [GENERAL]`, `ARM ARCHITECTURE`, etc.). |
| `m_maintainer_member` | 21 | `(sec_id, person_id, ...)` | `sec_id`, `person_id`, `role_type`, `priority` | Subsystem member rosters with roles (`Maintainer`=1, `Reviewer`=2, `Person`=3, `Other`=4). |
| `m_maintainer_pattern` | 22 | `(sec_id, pat_type, ...)` | `sec_id`, `pat_type`, `pattern`, `priority` | Wildcard file matching rules (`File`=1, `Exclude`=2, `Keyword`=3, `Regex`=4). |
| `m_maintainer_file` | 23 | `(vid, fid, sec_id)` | `vid`, `fid`, `sec_id` | Materialized bridge between files and their governing subsystems. |
| `m_credits_entry` | 24 | `(credit_id, vid_s)` | `credit_id`, `vid_s`, `vid_e`, `person_id`, `web_page`, `pgp_key`, `description`, `snail_mail`, `ast_id` | Historical `CREDITS` file entries linking developers to contribution narratives and PGP keys. |
| `m_commit` | 25 | `commit_id` | `commit_id`, `vid`, `commit_hash`, `author_id`, `author_date`, `committer_id`, `committer_date`, `subject`, `message` | Git commit log and patch registry. |
| `m_bridge_commit_person` | 26 | `(commit_id, person_id, ...)` | `commit_id`, `person_id`, `role_type`, `priority` | Multi-contributor bridge (`Author`=1, `Committer`=2, `Co-developed-by`=3, `Signed-off-by`=4, `Reviewed-by`=5, `Acked-by`=6, `Tested-by`=7, `Reported-by`=8, `Suggested-by`=9, `Merged-by`=10, `Requested-by`=11). |
| `m_bridge_commit_file` | 27 | `(commit_id, fid)` | `commit_id`, `vid`, `fid`, `change_type` | Files touched per commit. |
| `m_bridge_commit_tag` | 28 | `(commit_id, tag_id)` | `commit_id`, `vid`, `fid`, `tag_id` | Code tags modified per commit. |

---

## 3. API Server Subsystem Reference (`webapp/main.py`)

The backend server is implemented using **FastAPI** with **MySQL connection pooling** (`mysql.connector.pooling.MySQLConnectionPool`) and automatic failover.

### 3.1. Connection Management & Resilience (`DatabaseManager`)
- **Host Resolution**: Resolves MySQL host using `MYSQL_HOST` environment variable, Docker container detection (`/.dockerenv` -> `host.docker.internal`), or fallback `127.0.0.1`.
- **Pool Initialization**: Spawns a pool of up to 10 connections (`kernelinfo_pool_<pid>_<dbname>`) targeting candidate databases (`test`, `main`).
- **Dynamic Reconnection**: If pooled connection fails, attempts standalone `mysql.connector.connect()` on demand.

### 3.2. REST Endpoint Catalog

#### Root & Static Serving
- **`GET /`**: Returns service health status, connected database host/database name, webapp URL, and OpenAPI docs link.
- **`GET /app`** / **`GET /webapp`**: Serves the single-page application frontend (`webapp/webapp.html`) via `FileResponse`.

#### Version & Syntax Descriptors
- **`GET /api/versions`** / **`GET /versions`**: Queries `m_v_main` and returns all registered kernel versions:
  ```json
  [{"vid": 0, "vname": "v3.0"}]
  ```
- **`GET /api/type_descriptors`** / **`GET /type_descriptors`**: Queries `m_type_descriptor` and returns all 100+ AST construct categories (`C_struct`, `CPPro_define`, `C_Compound`, `Kconfig_Config`, etc.) to initialize frontend highlight configuration.

#### Unified Path Browsing & Source Code Inspection
- **`GET /api/version/{version_name}/browse/`**
- **`GET /api/version/{version_name}/browse/{path:path}`**
  - **Directory Handling**: If path is empty or matches a directory, executes prefix string aggregation on `m_file_name` to return immediate subdirectories (`sub_dirs`) and direct child files (`files`) with lifecycle statuses (`s_stat`, `e_stat`).
  - **File Handling**: If path matches a source file:
    1. Fetches file metadata, inception version (`vid_s`), concluding version (`vid_e`), and revision history from `m_bridge_file`.
    2. Queries all code tags from `m_bridge_tag` joined with `m_tag`, `m_ast`, `m_type_descriptor`, and `m_ast_debug`.
    3. Queries high-precision spatial AST coordinate maps from `m_bridge_map` joined with `m_map_ast`.
    4. Computes hierarchical container depths (`compute_container_depths`) across all AST nodes in `m_ast_container`.
    5. Resolves associated subsystem maintainers and review rosters via `resolve_subsystems_for_file_internal`.

#### Direct Entity Lookup
- **`GET /api/file/{fid}`**: Fetches file metadata, revision history, code tags, spatial AST maps, and governing subsystems directly by File ID.
- **`GET /api/tag/{tag_id}`**: Fetches snippet code, byte/character offsets, AST metadata, and spatial coordinate maps for a specific code tag.
- **`GET /api/ast/{ast_id}/tree?depth={depth}`**: Recursively traverses `m_ast_container` child relationships down to the specified depth (1 to 10), returning a nested JSON tree of child AST nodes, relationship priority ranks, and descriptor names.

#### Kconfig Subsystem Endpoints
- **`GET /api/version/{version_name}/kconfig/search?q={query}&type={type}&limit={limit}&offset={offset}`**:
  - Full-text search across symbol names, user prompts, and help text.
  - Automatically strips leading `CONFIG_` prefixes.
  - Filters by symbol data type (`bool`, `tristate`, `string`, `hex`, `int`).
  - Returns paginated results with source file definitions (`m_bridge_tag` / `m_bridge_file`).
- **`GET /api/version/{version_name}/kconfig/symbol/{name_or_kcid}`**:
  - Fetches complete metadata for a symbol by KCID or identifier string.
  - Resolves inception release, active lifecycle status, and revision history.
  - Direct relations: Extracts `depends_on`, `selects`, and `implies` from `m_kconfig_relation`.
  - Reverse relations: Queries `selected_by` and `implied_by` across symbols targeting this symbol.
  - Kbuild object compilation mapping: Queries `m_kconfig_kbuild` to return all C source files and object files (`obj-y`, `obj-m`, conditional) compiled when this symbol is active.
- **`GET /api/version/{version_name}/kconfig/tree?arch={arch}`**:
  - Queries `m_kconfig_tree` joined with `m_kconfig_symbol`.
  - Normalizes target architecture (e.g. `x86_64` -> `x86`, `arm64` -> `arm64`).
  - Scopes tree nodes to active architecture, filtering out non-matching `arch/<foreign>/` Kconfig menus.
  - Dynamically synthesizes parent menus for unparented subsystem nodes (`drivers/`, `fs/`, `net/`, `security/`, `crypto/`, `lib/`, `kernel/power/`, `block/`).
  - Attaches relational dependency lists (`depends_on`, `selects`, `implies`, `selected_by`) to each tree node.
- **`GET /api/version/{version_name}/kconfig/env-presets`**:
  - Dynamically queries `arch/%/Kconfig` paths from `m_bridge_file` to discover all supported target architectures.
  - Queries environment and toolchain configuration symbols (`ARCH`, `SRCARCH`, `SUBARCH`, `64BIT`, `32BIT`, `CROSS_COMPILE`, `CC_IS_GCC`, `CC_IS_CLANG`, etc.).
  - Returns compiler presets for GCC and Clang/LLVM.
- **`GET /api/version/{version_name}/kconfig/defconfigs?arch={arch}`**:
  - Queries `arch/{arch}/configs/%defconfig%` from `m_file_name`.
  - Identifies canonical baseline defaults (e.g. `x86_64_defconfig` for x86, `defconfig` for arm64).
- **`GET /api/version/{version_name}/kconfig/defconfig?file_path={path}&arch={arch}`**:
  - Reads raw defconfig content via `git show <version>:<path>` or local disk read.
  - Parses Linux `.config` syntax (including `# CONFIG_FOO is not set` -> `"n"` and `CONFIG_FOO=y` -> `"y"`).
  - Returns parsed dictionary of symbol assignments and target bitness.
- **`POST /api/version/{version_name}/kconfig/validate`**:
  - Validates symbol assignments against relational rules.
  - Iteratively forces minimum values (`y`/`m`) for symbols selected by active configurations.
  - Checks satisfiability of `depends_on` conditions, reporting unmet dependencies and adjusted values.
- **`POST /api/version/{version_name}/kconfig/export`**:
  - Serializes provided symbol assignments into standard Linux kernel `.config` file format.
- **`POST /api/version/{version_name}/kconfig/import`**:
  - Parses uploaded `.config` text payload into symbol dictionary.

#### Maintainers, Subsystems & Credits Endpoints
- **`GET /api/version/{version_name}/maintainers?q={q}&status={status}`**:
  - Queries `m_maintainer_section` joined with `m_maintainer_member` and `m_maintainer_person`.
  - Provides in-memory parser fallback (`parser.maintainer_ast.MaintainerParser`) if database tables are unpopulated.
  - Cross-references member email addresses and names against `m_credits_entry` to flag credited developers.
  - Returns full catalog of subsystems with maintainer/reviewer rosters and pattern counts.
- **`GET /api/version/{version_name}/maintainer/section/{sec_id_or_name}`**:
  - Resolves subsystem section by numeric ID or title string.
  - Queries materialized file mappings (`m_maintainer_file`) or evaluates wildcard rules (`MaintainerMatcher`) to return all matching repository files.
  - Returns maintainers, reviewers, mailing lists, SCM git trees, web pages, and pattern rules.
- **`GET /api/version/{version_name}/person/{person_id_or_email}`**:
  - Looks up developer profile by numeric Person ID or email/name string.
  - Gathers all maintained and reviewed subsystems.
  - Cross-references historical `CREDITS` narrative, homepage, PGP key, and postal address.
  - Queries git contributions via `m_bridge_commit_person` or Git commit parser:
    - Calculates contribution metrics: Authored commits, Co-developed commits, Signed-off commits, Reviewed commits, Merged commits, Requested commits.
    - Identifies the developer's **Latest Patch** (subject, date, SHA, files touched).
    - Returns recent commits list.
- **`GET /api/version/{version_name}/credits?q={q}`**:
  - Queries `m_credits_entry` joined with `m_maintainer_person`.
  - Links credited authors to active subsystem roles.
  - Full-text search across names, emails, contribution summaries, and PGP fingerprints.

#### Git Commits, Blame & Timeline Endpoints
- **`GET /api/version/{version_name}/commits?q={q}&author={author}&limit={limit}&offset={offset}`**:
  - Returns chronological commit list for the release.
  - Extracts multi-contributor roles from `m_bridge_commit_person`.
  - Extracts merge commit metadata (`is_merge`, pull request requester name/email, merged branch origin, shortlog summaries).
- **`GET /api/version/{version_name}/commit/{commit_hash_or_id}`**:
  - Full commit details: commit hash, author, committer, Unix epoch timestamps, subject, full commit message body.
  - Touched files from `m_bridge_commit_file` with change types (`A`, `M`, `D`).
  - Modified code tags from `m_bridge_commit_tag`.
- **`GET /api/version/{version_name}/file/{fid}/blame`**:
  - Returns line-by-line git blame annotations mapped to code tags.
  - Correlates each code tag with commits from `m_bridge_commit_tag` and `m_commit`.
  - Identifies primary author, commit hash, date, and multi-commit revision counts (`+N` commits).
- **`GET /api/version/{version_name}/timeline?limit={limit}`**:
  - Aggregates commit timeline and computes **Top Contributor Ranking** by commit frequency.

#### Dev Introspection Endpoints
- **`GET /api/dev/tables`**: Executes `SELECT COUNT(*)` across all 25 schema tables in MySQL and returns row counts.
- **`GET /api/dev/endpoints`**: Returns an interactive schema catalog of all API endpoints with descriptions and test arguments.

---

## 4. Frontend Client Architecture (`webapp/webapp.html`)

The frontend is a zero-dependency, ultra-responsive Single-Page Application implemented in **Vanilla HTML5, CSS3, and JavaScript (ES2022)**.

### 4.1. Global State Management
The client manages centralized state in JavaScript memory:
- `API_BASE`: Backend URL (auto-detected from `window.location` or overridden via prompt / `localStorage`).
- `currentVersion`: Active Linux release tag (e.g. `"v3.0"`).
- `currentAppMode`: Active primary workspace mode (`"explorer"`, `"kconfig"`, `"tui"`, `"maintainers"`, `"credits"`, `"timeline"`).
- `openTabs`: Multi-tab document stack `[{ type, version, path, title, targetStartLine, targetEndLine, data }]`.
- `activeTabIndex`: Currently focused tab index.
- `selectedAstId`: Focused AST node in AST Container Inspector.
- `currentInspectorDepth`: Depth slider value for recursive AST hierarchy tree.
- `highlightSettings`: Syntax category color palette with visibility toggles.
- `containerDepthPalette`: 6-level hierarchical color palette (`Color A` through `Color F`) applied strictly to container items.
- `containerColoringEnabled`: Toggle state for AST container depth coloring.
- `cpproHighlightEnabled`: Toggle state for `#if`/`#ifdef`/`#elif`/`#else` conditional scope highlighting.
- `blameViewEnabled`: Toggle state for interactive Git Blame line annotations.

### 4.2. URL Hash Synchronization & Deep Linking
The client synchronizes navigation state with browser URL hashes:
- Format: `#{version}/{path}:L{startLine}-L{endLine}` (e.g. `#v3.0/include/linux/lockd/bind.h:L14-L28`).
- **Bi-directional Binding**:
  - Clicking a line or Shift+clicking a line range updates the URL hash via `history.replaceState()`.
  - Clicking on a single active line toggles off/deselects the line range and clears hash coordinates.
  - Direct URL loading or hash changes automatically open the matching tab, scroll the target line into view (`scrollIntoView({ block: 'center' })`), and highlight the line span.

---

## 5. Client Workspace & Feature Subsystems

### 5.1. Explorer & Source Code AST Viewer (`#explorerWorkspace`)

#### File Tree Sidebar (`#sidebarTree`)
- Hierarchical folder traversal with parent directory navigation (`.. (Parent)`).
- Instant client-side filtering via search input (`filterSidebarTree()`).
- Direct tab spawning from folder view or file clicks.

#### Tab Management Bar (`#tabsBar`)
- Multi-tab document interface supporting directory browsers and source files.
- Visual status indicators (`📁` for directories, `📄` for files).
- Tab close (`✕`) and new tab (`+`) actions.

#### Highlighting & Token Resolution Algorithm (`buildHighlightedSource` & `highlightLineText`)
- **Outer Tag Isolation**: Sorts tags by `line_s` ASC, `char_s` ASC, and span length DESC to identify primary outermost base lines.
- **Token Collision & Nesting Resolution**: For each character column on a line, evaluates all overlapping spatial coordinate maps (`m_map_ast` and `m_bridge_tag`), assigning token ownership to the innermost (smallest span) AST construct.
- **Contiguous Run Compression**: Groups characters sharing the same winning AST token to generate optimal HTML spans without DOM fragmentation.
- **Container Depth Hierarchy Coloring**:
  - Distinguishes between standalone syntax tokens and nested container elements (`m_ast_container`).
  - Applies deterministic depth colors:
    - **Level 0 (Color A - `#e5c07b`)**: Outer Structs and Compound blocks.
    - **Level 1 (Color B - `#61afef`)**: Members and Function Prototypes.
    - **Level 2 (Color C - `#98c379`)**: Parameters and Return Types.
    - **Level 3 (Color D - `#c678dd`)**: Inner Qualifiers and Types.
    - **Level 4 (Color E - `#56b6c2`)**: Deep Nested Types.
    - **Level 5 (Color F - `#e06c75`)**: Level 5+ Containers.
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

### 5.2. AST Container Hierarchy Inspector (`#astInspector`)
- Slide-over panel for deep structural AST analysis.
- Displays AST Node ID, symbol name, construct type descriptor, and raw JSON parser dump.
- Interactive slider controlling traversal depth (1 to 10 levels).
- Recursively renders child container relationships with priority ranks and relation types.

### 5.3. Kconfig Web Menuconfig GUI (`#kconfigWorkspace`)

```
+---------------------------------------------------------------------------------------+
|  🔍 Search Kconfig symbols...  | Type Filter [All] | 🔀 Mode: Drill-Down | 🌐 Target | ⚙️ Defconfig |
+---------------------------------------------------------------------------------------+
|  [⬆ Up to Parent]  Main Menu > File systems > Ext4       | ⚙️ Defconfig: x86_64_defconfig     |
+------------------------------------+--------------------------------------------------+
|  TREE / MENU PANEL                 |  INSPECTOR PANEL                                 |
|  [ ] 64-bit kernel (CONFIG_64BIT)  |  CONFIG_EXT4_FS - "Ext4 POSIX File System"       |
|  [*] Enable loadable module support|  Type: tristate | Current: < * > Built-in (y)     |
|  [--->] File systems  --->         |  ----------------------------------------------  |
|    <*> The Extended 4 (ext4)       |  🌱 Added: v3.0 | 📌 Span: v3.0 -> Active        |
|    <M> Ext4 Extents Support        |  📁 Location: fs/ext4/Kconfig:12                 |
|                                    |  📦 Compiled Objects: ext4.o (obj-$(CONFIG_...)) |
|                                    |  📖 Help Text: Ext4 is the default filesystem... |
|                                    |  🔗 Depends on: BLOCK && EXPERIMENTAL            |
|                                    |  ✨ Selects: CRC32, JBD2, FS_MBCACHE             |
|                                    |  🔄 Selected by: (None)                          |
+------------------------------------+--------------------------------------------------+
```

#### Dual View Architecture
1. **Drill-Down Navigation Mode (`kconfigDrillDownMode = true`)**:
   - Replicates authentic Linux menu hierarchy navigation: Displays only items in the current submenu.
   - Breadcrumb navigation bar with parent menu jump button (`[ ⬆ .. ] Up to Parent Menu`).
   - Double-clicking or clicking `Enter >` navigates into submenus (`[--->]`).
2. **Full Tree Mode (`kconfigDrillDownMode = false`)**:
   - Displays complete multi-level tree hierarchy with indentation.

#### Global Cross-Directory Symbol Search
- Searches symbol names (with or without `CONFIG_`), prompts, help text, and file paths.
- Automatically overrides scoped navigation during active search, displaying match counts and direct breadcrumb clearing.

#### Bidirectional Constraint Propagation Engine (`evaluateKconfigConstraints`)
- **Forced Symbols (`selects`)**:
  - When a symbol with `selects` is enabled (`y` or `m`), forces all target symbols to active values.
  - Evaluates cascading selections iteratively up to 20 passes to resolve deep dependency chains.
  - Locked symbols display `🔒 Forced` badges and disabled toggle buttons with tooltips indicating which active symbols forced them.
- **Dependency Guarding (`depends_on`)**:
  - Evaluates boolean expressions (`&&`, `||`, `!`, `!=`, `=`).
  - Unmet dependencies display `⚠️ Unmet` badges; disabling/enabling prohibited until parent conditions are satisfied.
- **Derived Architecture & Bitness Synchronization**:
  - Toggling `CONFIG_64BIT` dynamically updates `X86_64` and `X86_32` state and toolchain profiles.

### 5.4. Terminal Menuconfig (TUI) Engine (`#tuiWorkspace`)
- Full browser-based ncurses/dialog replica styled after Linux `make menuconfig`.
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

### 5.5. Target Architecture & Toolchain Profile Manager (`#kconfigProfileModal`)
- Dynamic architecture presets queried from `arch/*/Kconfig` (x86_64, i386, arm64, arm, ppc64, ppc32, sparc64, sparc32, riscv64, etc.).
- Bitness auto-derivation (32-bit vs 64-bit).
- Compiler toolchain selector (GCC vs Clang / LLVM with version tracking).
- Cross-compiler prefix configuration (`CROSS_COMPILE`) and custom environment variable injection.

### 5.6. Defconfig Profile Engine (`#kconfigDefconfigModal` & `#tuiDefconfigModal`)
- Auto-discovery of architecture-specific default configurations (`arch/{arch}/configs/*defconfig`).
- One-click application of canonical baselines (`make defconfig`).
- Searchable catalog of specialized hardware/board defconfigs.
- Live badge indicator in workspace header displaying active defconfig name and symbol count.

### 5.7. Configuration Import & Export Modals
- **Export Modal (`#kconfigExportModal`)**: Generates valid Linux `.config` text with one-click clipboard copy and `.config` file download.
- **Import Modal (`#kconfigImportModal`)**: File picker (`FileReader`) and text input to upload existing `.config` files and batch-apply symbol values.

### 5.8. Subsystems & Maintainers Hub (`#maintainersWorkspace`)
- Grid catalog of all kernel subsystems for the active release.
- Interactive search by subsystem title, maintainer/reviewer name, email, or file pattern.
- Status filters (`Supported`, `Maintained`, `Orphan`, etc.).
- Maintainer and Reviewer role pills with CREDITS verification stars (`⭐`).
- Subsystem detail modal with pattern rule list (`F:` / `X:`) and searchable table of matching repository files with direct open links.

### 5.9. Kernel Credits Directory (`#creditsWorkspace`)
- Directory of all credited contributors in Linux `CREDITS`.
- Displays contributor narratives, personal homepages, PGP key fingerprints, postal addresses, and linked maintainer roles.
- Interactive search across all contributor metadata.

### 5.10. Commit Timeline & Developer Profiles (`#timelineWorkspace`)
- **Top Contributor Ranking**: Leaderboard of top 10 developers by commit frequency with avatar badges.
- **Chronological Patch Stream**: Stream of release commits with author avatars, commit SHAs, co-developers, signers, reviewers, and modified file counts.
- **Developer Profile Modal (`#personModal`)**:
  - Full developer bio and CREDITS cross-reference.
  - **Git Contribution Breakdown**: Authored, Co-developed, Signed-off, Reviewed, Merged, and Requested commit counters.
  - **Latest Patch Spotlight**: Featured card displaying the developer's most recent contribution with one-click detail inspection.
  - Maintained subsystems roster.
- **Commit Details Modal (`#commitModal`)**:
  - SHA hash, commit subject, full commit message body with trailers.
  - Pull request / merge submitter details, origin branch (`🌿`), and merged patch shortlog.
  - Touched files with change status badges and direct file navigation.

### 5.11. Dev Introspection & Interactive API Runner (`#devModal`)
- Row count dashboard across all 25 database schema tables with one-click refresh.
- Interactive API endpoint tester with parameter forms, URL builders, and live JSON response inspection.

---

## 6. Audit of Inefficiencies & Unoptimized Parts

To prepare for technical optimizations, this section documents all identified performance bottlenecks, N+1 query patterns, caching deficiencies, and frontend rendering inefficiencies:

### 6.1. Backend API Server Inefficiencies (`webapp/main.py`)

1. **N+1 Queries in File Browsing (`browse_path` & `get_file_by_id`)**:
   - For every file requested, `browse_path` queries code tags, and then executes an `IN (...)` query for spatial maps. If there are 500+ tags, it executes large IN clauses and multiple child queries.
   - For file revision history, a separate `GROUP BY` query is executed per file request instead of leveraging in-memory cache.
2. **Repeated Dynamic Subsystem Matching per File Request (`resolve_subsystems_for_file_internal`)**:
   - When a source file is inspected, the server queries `m_maintainer_file`. If not found, it instantiates `MaintainerMatcher([target_sec])` and iterates over patterns dynamically.
   - While `_MAINTAINER_CACHE` caches section objects, individual file matching results are not memoized.
3. **Redundant Git Subprocess Invocations in Commit / Defconfig Endpoints**:
   - `get_version_commits`, `get_commit_detail`, and `get_file_blame` fall back to running `git -C linux log` or `git show` subprocesses without caching the parsed git commit objects in memory. Subsequent requests re-spawn `git` processes.
4. **Unindexed / Broad SQL String Pattern Searches in Kconfig Search**:
   - `search_kconfig_symbols` performs multiple `LIKE %query%` conditions across `name`, `prompt`, and `help`. On large symbol tables (15,000+ symbols), broad string wildcards cause full table scans.
5. **Redundant Relations Queries in `get_kconfig_tree`**:
   - `get_kconfig_tree` executes three separate broad queries (`m_kconfig_tree`, `m_tag`/`m_file_name` for file paths, and `m_kconfig_relation`). These can be optimized with indexed joins or unified caching.
6. **No HTTP Response Caching Headers**:
   - Immutable historical version data (e.g. `v3.0` metadata, type descriptors, defconfigs) does not emit `Cache-Control` or `ETag` headers, causing the browser to re-fetch identical data on tab switches.

### 6.2. Frontend Client Inefficiencies (`webapp/webapp.html`)

1. **Monolithic DOM Reconstruction in Code Highlighting (`buildHighlightedSource`)**:
   - `buildHighlightedSource` generates massive HTML strings for the entire file (thousands of lines) and assigns `gutter.innerHTML = ...` and `body.innerHTML = ...` in one pass. For files with 5,000+ lines, this causes layout thrashing and high peak memory usage.
2. **Re-rendering the Entire Kconfig Tree on Single Value Toggle**:
   - When a single boolean or tristate configuration value is changed, `renderKconfigTree()` rebuilds the entire menu DOM tree from scratch. In Full Tree mode (2,000+ items), this causes perceptible UI latency.
   - The UI should update only the toggled item and affected forced/unmet items in-place.
3. **Repetitive Regex Parsing in Token Highlighting**:
   - `renderSegmentWithKconfigTokens` instantiates and executes `/\bCONFIG_([A-Za-z0-9_]+)\b/g` on every token segment on every line during rendering.
4. **Non-Virtualized Lists in Subsystems & Credits Hubs**:
   - The Subsystems grid renders all 500+ subsystem cards into the DOM simultaneously. A virtualized or paginated list/grid would significantly reduce DOM node count.
5. **Redundant Local Storage Serialization**:
   - Every close of the highlight modal parses and serializes full palette objects even if no changes were made.

---

## 7. Optimization Implementation Plan & Technical Roadmap

To address all unoptimized components identified in Section 6, the following architectural enhancements are prioritized:

### 7.1. Client-Side IndexedDB Storage Tier (`IDBStorageManager`)
- **Immutable Release Dataset Caching**:
  - Leverage browser IndexedDB (100MB+ asynchronous storage) with dedicated object stores (`metadata`, `file_cache`, `kconfig_cache`, `subsystems_cache`, `blame_cache`).
  - Cache-first strategy: file switching, Kconfig submenus, maintainer cards, and AST coordinate maps resolve in **< 1ms** directly from local IndexedDB without HTTP/MySQL roundtrips.
- **Universal Input Debouncing**:
  - Applied `debounce(fn, wait)` across all search inputs (file tree, Kconfig, maintainers, credits, timeline, defconfigs).
- **Static Precompiled Regexes**:
  - Global `KCONFIG_SYM_REGEX` eliminates per-token RegExp allocations.
- **Targeted Line Range Selection**:
  - $O(\Delta)$ line element tracking avoids full DOM queries on click.

### 7.2. Backend Simplification & Server Optimizations
- **Direct VID Communication & Join Elimination**:
  - Client caches `vname <-> vid` in IndexedDB at boot and communicates directly via integer `vid`, eliminating repetitive `SELECT vid FROM m_v_main WHERE vname = %s;` lookups and joins across backend queries.
- **In-Memory Defconfig & Git Caching**:
  - Added `_DEFCONFIG_CACHE`, `_GIT_COMMITS_CACHE`, `_GIT_HUNKS_CACHE`, and `_FILE_SUBSYSTEMS_CACHE` in [`webapp/main.py`](file:///home/scottviger/dev/KernelInfo-Parser/webapp/main.py).
- **HTTP Cache Headers**:
  - Injected `Cache-Control: public, max-age=3600` on static and release version endpoints.

---

## 8. Advanced Developer Feature Systems

The web application integrates 7 advanced developer systems providing comprehensive cross-release evolution tracking, code navigation, visual dependency modeling, constraint resolution, and compiler tooling:

```
+---------------------------------------------------------------------------------------------------------+
|                                    7 ADVANCED FEATURE SYSTEMS OVERVIEW                                  |
+---------------------------------------------------------------------------------------------------------+
| 1. Cross-Version Semantic Diff   | AST & Kconfig Evolution comparison between any two release versions  |
| 2. Global Symbol XRef (Def/Usage)| Global "Go to Definition" & "Find References" across all kernel files |
| 3. Interactive Dependency Graph  | Interactive HTML5 Canvas DAG for Kconfig relations & AST hierarchies |
| 4. Smart Kconfig Auto-Solver     | 1-click unmet dependency solver, .config vs defconfig side-by-side diff|
| 5. Patch Review Maintainer Match | Real-time get_maintainer.pl patch analyzer & code ownership matcher  |
| 6. AST Semantic Query Sandbox    | Structural search for structs, macros, conditionals, & return types  |
| 7. Clang compile_commands Export | Instant Clang Compilation Database generator from Kbuild object map  |
+---------------------------------------------------------------------------------------------------------+
```

### 8.1. Cross-Version File & Kconfig Semantic Diff
- **File Hierarchy Comparison (`GET /api/diff/versions/{v1}/{v2}`)**:
  - Compares the file trees of release `v1` and `v2`, categorizing all paths into `added`, `removed`, `modified` (based on `fid` changes and `s_stat`/`e_stat`), and `unchanged`.
  - Summary metrics card highlights added/removed file deltas and provides direct 1-click links to inspect files in the Explorer.
- **Kconfig Evolution Analysis (`GET /api/diff/kconfig/{v1}/{v2}`)**:
  - Tracks symbol additions, removals, prompt alterations, type changes, and default value modifications across releases.

### 8.2. Global Symbol Cross-Reference (XRef) & Autocomplete
- **Symbol Cross-Reference (`GET /api/version/{version}/xref/{symbol_name}`)**:
  - Locates the primary AST definition in `m_ast` joined with `m_type_descriptor`, `m_tag`, and `m_bridge_tag`.
  - Queries all global usage tags across the entire kernel source tree, mapping line and column coordinates for instant jump-to-source navigation.
- **Fast Autocomplete Lookup (`GET /api/version/{version}/symbol_lookup?q={query}`)**:
  - Debounced prefix search over all indexed AST identifiers with type tags.

### 8.3. Interactive Canvas Dependency DAG Graph Visualizer
- **DAG Generation Endpoint (`GET /api/version/{version}/kconfig/graph/{symbol_name}?depth=2`)**:
  - Traverses `depends_on` (outgoing) and reverse-dependency `selects` / `selected_by` (incoming) relations from `m_kconfig_relation` and `m_kconfig_symbol`.
  - Returns a node-link payload rendered on an interactive HTML5 Canvas with force/radial layout, drag-and-pan, zoom controls, and click-to-inspect symbol attributes.

### 8.4. Smart Kconfig Auto-Solver & Config Compare
- **Dependency Auto-Solver (`POST /api/version/{version}/kconfig/autosolve`)**:
  - Evaluates prerequisite dependency trees for symbols blocked by `Unmet Dependency` constraints. Computes the exact minimal set of required symbols to enable and applies all toggles with a single click.
- **Config Comparison Engine (`POST /api/version/{version}/kconfig/diff_config`)**:
  - Compares the developer's live working `.config` against built-in architecture defconfigs (`x86_64_defconfig`, `i386_defconfig`, `arm_defconfig`, etc.) or custom uploaded configs, computing match percentages and mismatch tables.

### 8.5. Patch Reviewer & Subsystem Maintainers Matcher
- **Patch Matcher (`POST /api/version/{version}/patch/maintainers`)**:
  - Accepts raw unified diff or patch text (`diff --git a/... b/...`), extracts touched file paths and modified line numbers, and executes pattern matching against all 500+ kernel subsystem sections.
  - Automatically formats `get_maintainer.pl`-style `TO:` (Maintainers) and `CC:` (Reviewers & Mailing Lists) recipient lists with 1-click clipboard copy.

### 8.6. AST Semantic Query Sandbox
- **Structural Query Endpoint (`POST /api/version/{version}/ast/query`)**:
  - Enables multi-constraint searching over kernel AST nodes using `type_id`, `type_name`, `name_pattern` (wildcards), and `path_prefix` filters.
  - Returns paginated results with direct jump-to-source links.

### 8.7. Clang `compile_commands.json` Exporter
- **Compilation Database Generator (`GET /api/version/{version}/export/compile_commands?arch={arch}`)**:
  - Generates standard JSON Compilation Database mapping all C source files to architecture compiler flags for immediate integration into VS Code, `clangd`, and CLion.

---

## 9. Next-Generation Developer Systems & Tooling Hub

The web application integrates 6 additional cutting-edge developer subsystems for memory inspection, interactive visual architecture maps, kernel image size estimation, function call flows, codebase tours, and in-browser patch preparation:

```
+---------------------------------------------------------------------------------------------------------------+
|                                 6 NEXT-GENERATION DEVELOPER SYSTEMS OVERVIEW                                  |
+---------------------------------------------------------------------------------------------------------------+
| 8. Struct Memory Layout (Pahole) | Byte offsets, padding holes, and 64-byte cache line alignment visualizer   |
| 9. Subsystem Treemap Map         | Squarified interactive visual treemap colored by subsystem LOC / status    |
| 10. Kconfig Bloat-O-Meter        | Live kernel binary size & compiled source footprint estimator for .config  |
| 11. Function Call Graph Hierarchy| Recursive Caller / Callee flow tree & interactive DAG visualizer           |
| 12. Code Tour & Architecture Walk| Interactive multi-step guided codebase tour with IndexedDB persistence     |
| 13. Patch Staging & format-patch | In-browser patch editor, staged changes diff, and git format-patch export  |
+---------------------------------------------------------------------------------------------------------------+
```

### 9.1. C Struct Memory Layout & Pahole Alignment Visualizer
- **Memory Offset & Alignment Calculation (`GET /api/version/{version}/struct/layout/{struct_name}`)**:
  - Traverses `m_ast`, `m_ast_container`, and `m_type_descriptor` for any C `struct` or `union` definition.
  - Computes byte offsets, member sizes, alignments, and detects internal padding holes before members and tail padding.
  - Groups struct fields into 64-byte L1/L2 cacheline blocks, displaying cacheline crossing warnings and greedy alignment reordering optimizations that save memory bytes.

### 9.2. Interactive Codebase Treemap Map
- **Squarified Hierarchy Endpoint (`GET /api/version/{version}/treemap?max_depth=3`)**:
  - Returns nested directory and file hierarchies with file counts and line weights.
  - Renders a squarified, responsive treemap in the client browser with color-coded directory clusters (`fs/`, `drivers/`, `net/`, `kernel/`, `mm/`, `arch/`) and 1-click drill-down navigation into the Explorer.

### 9.3. Kconfig Footprint & Binary Size Estimator (*Bloat-O-Meter*)
- **Footprint Simulation Engine (`POST /api/version/{version}/kconfig/footprint`)**:
  - Traverses `m_kconfig_kbuild` and `m_bridge_file` for all active symbols (`=y` or `=m`) in current `kconfigValues`.
  - Aggregates unique compiled C source files, estimating total kernel Source Lines of Code (LOC) and uncompressed binary image footprint (`vmlinux` in MB).
  - Displays a detailed breakdown table mapping each compiled object back to its enabling Kconfig symbol.

### 9.4. Function Call Graph & Callers / Callees Flow
- **Call Flow Hierarchy (`GET /api/version/{version}/callgraph/{function_name}`)**:
  - Discovers function declarations in `m_ast` and maps all inbound call sites across other files via `m_tag`.
  - Discovers outbound child function invocations and helper calls within the function's own line boundaries.
  - Displays a two-column call flow tree with 1-click jump-to-source navigation.

### 9.5. Interactive Code Tour & Architecture Walkthrough Studio
- **Curated Walkthrough Presets (`GET /api/version/{version}/tours/presets`)**:
  - Provides pre-authored interactive architectural tours:
    - **VFS File Open Journey**: Traces `sys_open` $\rightarrow$ `do_sys_open` $\rightarrow$ `path_openat` $\rightarrow$ `ext4_file_open`.
    - **Slab Memory Allocator Journey**: Traces `kmalloc` $\rightarrow$ `kmem_cache_alloc` $\rightarrow$ `cache_grow`.
  - Stepper UI highlights lines in the Explorer, explains key subsystem concepts, and guides developers step-by-step through complex kernel execution flows.

### 9.6. In-Browser Patch Staging & `git format-patch` Studio
- **Patch Staging & RFC-2822 Generator (`POST /api/version/{version}/patch/format`)**:
  - Allows in-browser staging of source modifications, computing unified diffs with Python's `difflib`.
  - Resolves subsystem maintainers for the target file and generates a standard `git format-patch` compliant email with `From:`, `Date:`, `Subject: [PATCH]`, `To:`, `Cc:`, `Signed-off-by:`, and diff statistics.
  - Offers 1-click copy to clipboard and `.patch` file export.

---

*Document Author: Antigravity AI Engineering Assistant*
*Repository: KernelInfo-Parser / MapleCircuit*


