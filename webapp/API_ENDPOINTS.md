# WebApp REST API Endpoint Catalog (`webapp/API_ENDPOINTS.md`)

Authoritative specification for Section 4 (`§4.1`–`§4.13`) of the WebApp subsystem. For core architecture, security, caching, sync, and configuration (`§1`–`§3`, `§6`–`§8`), see `webapp/API.md`. For frontend modules and views (`§5.0`–`§5.38`), see `webapp/API_FRONTEND.md`.

---

## Table of Contents
- 4. REST API Endpoint Catalog
  - 4.1. Version Catalog [L32-L217]
  - 4.2. Filesystem & Code View API [L218-L511]
  - 4.3. Symbols, Cross-References & AST Inspection API [L512-L892]
  - 4.4. Kconfig & Architecture Defconfigs API [L893-L1123]
  - 4.5. Maintainers & CREDITS API [L1124-L1400]
  - 4.6. Git Commits & Patch Management API [L1401-L1819]
  - 4.7. Visual Modeling & Analysis Tools API [L1820-L2167]
  - 4.8. Linux Kernel Mailing List (LKML) & Public-Inbox API [L2168-L2483]
  - 4.9. Kernel API Catalogs (`kapi.py`) [L2484-L2576]
  - 4.10. Insights API (`insights.py`) [L2577-L2683]
  - 4.11. CVE Records API (`cve.py`) [L2684-L2734]
  - 4.12. Watch API (`watch.py`) [L2735-L2753]
  - 4.13. Linux man-pages API (`man.py`) [L2754-L2798]

---

## 4. REST API Endpoint Catalog

All API endpoints reside under the `/api` prefix. Both canonical version-scoped paths and backward-compatible
alias routes are supported.

---

### 4.1. Version Catalog

#### `GET /api/versions`
Versions of the active repository (§2.10): parsed refs from `m_git_ref` (`"indexed": true`) plus every
commit-pointing Git tag that was not parsed (`"indexed": false`, `vid: 0`), sorted in Git version order
(`versionsort`; release order for a man-pages repository, `webapp/API.md` §2.10). Tags pointing at trees (e.g. `v2.6.11`) are skipped.
- **Query Parameters**:
  - `include_rc` (query, bool): If `true`, unparsed pre-release tags (`-rc`, `-pre`, `-alpha`, `-beta`) are
    included; parsed refs are always listed. Default `true`.
  - `compact` (query, bool): If `true`, the compact positional form below (what the frontend reads). Default
    `false`.
- **Entry fields**: `vid`, `vname`, `commit_hash`, `is_prerelease`, `ref_type` (1 tag, 2 branch, 3 commit),
  `indexed`, `stamp` (client cache generation, §2.11).
- **Caching (§2.13)**: built once per repository state and sent pre-encoded (gzip when accepted) with a weak
  `ETag` and `Cache-Control: no-cache`; `If-None-Match` naming the current ETag answers `304`.
- **Compact form** (`compact=true`): `{"g": "<generation>", "n": [vname, …], "h": "<12-hex commit of each
  entry, concatenated>", "p": [parse time | null, …], "t": [[index, ref_type], …]}`, entry `i` being `n[i]`,
  `h[12i:12i+12]` (`000000000000`: no known commit), `p[i]` (when its ingestion finished, `0` before
  `m_ingest_state`; `null` for a tag only Git has) and `ref_type` 1 unless listed in `t`. Clients derive
  `indexed` (`p[i] !== null`), `is_prerelease` (the name) and `stamp` (`<h>.<p>` or `<h>.git`); `vid` is not
  sent. ETag `W/"c1-<g>"`.
  - **Delta**: a request whose `If-None-Match` names an earlier generation the server still holds
    (`W/"c1-<g>"`, last 64 generations) gets `{"g": "<generation>", "b": "<the named generation>", "d": [op,
    …]}` (`Cache-Control: no-store`, no ETag) when that is smaller than the full catalog: each `op` is either
    `[start, count]` (copy `count` entries of generation `b` from `start`) or an entry `[vname, sha12, parse
    time, ref_type]` with trailing defaults (`null`, `1`) omitted; applied in order they give the current
    list. The current generation answers `304`, an unknown one the full catalog.
  - Example (a version finished parsing):
    `{"g":"c70f7902200bc097","b":"b0b340edcc7a2375","d":[[0,2],["v2.6.12-rc2","1da177e4c3f4",1793498865],[3,5802]]}`.
- **Unfinished versions**: a ref whose ingestion is still running or was interrupted (`m_ingest_state.status =
  1`, see `core/API.md` §4.1) is not listed as indexed (`helpers.finished_ref_sql()`); its tag appears as
  unparsed (Git-only) until the run completes. Version-name resolution (`get_version_info`) and the history
  views (`indexed_versions`) apply the same rule.
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
List the configured repositories (core config `"repositories"`, auto-detected under `workspace.repos_dir`)
joined with `m_repository`/`m_project`. Each entry has `repo` (the name sent as `X-KI-Repo`), `name`, `slug`,
`project` (the project slug, `linux` or `man-pages`, `webapp/API.md` §2.10), `path`, `default_branch`,
`git_url`, `repo_id`, `indexed` (parsed into the database), `available` (work tree present), `tag_views`
(configured regex tag layouts; a man-pages entry without them gets the `man-pages-X.*` view), and `active`;
the envelope adds `active`. Clients
refetch it only when the `projects` sync token changes or on a manual check (§2.11), not with every parsed
version.
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
Retrieve the active repository's Git branches: finished indexed refs (`m_git_ref.ref_type = 2` for its
`repo_id`) followed by local and remote-tracking Git branches (e.g. `stable/linux-6.6.y`) discovered via `git
for-each-ref` and filtered through the repository's `branch_filter`, alongside `tag_views` (configured regex
tag grouping layouts). Unindexed branches remain immediately browsable in the webapp directly through Git.
Each entry carries a `stamp` (§2.11) built from the branch's current Git head, so a branch that moved
invalidates its cached data. Built once per repository state and sent pre-encoded with a weak `ETag` (`304` on
a matching `If-None-Match`, §2.13).
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
Tag names reachable from one branch: the releases that belong to its history (`git for-each-ref
--merged=<branch> refs/tags`). This is what scopes the header's Tag and RC lists (§5.13) — `v2.6.16.62` was
tagged on `stable/linux-2.6.16.y` and is not reachable from `master`, so it is offered for that branch only.
- **Query Parameters**: `branch` (string, required): a branch name (any revision resolves). Validated like
  every revision (§2.1); `400` when it is not a plain ref, `404` when it does not resolve, `503` when the Git
  walk failed or timed out (not cached; the client then keeps every tag listed).
- **Cost**: Git walks the whole history of the branch, so the first answer costs ~0.3 s for a stable branch
  and ~18 s for `master` of Linux (1.5 M commits; ~6 s with a Git commit-graph written in the repository). The
  answer is cached per `(repository, branch head)` in the server (32 entries, never stale: a branch that moved
  has another head) and in the client under the branch's stamp (§2.11), so it is computed once per branch
  position. The default branch of each repository is walked when the server starts (§2.12), and concurrent
  requests for one head share a single walk. The frontend requests it with a long timeout and off the critical
  path: the dropdowns render complete and narrow when it arrives.
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
Change tokens of the server (§2.11): `{"db", "frontend", "parsed", "git", "projects", "repo"}`. Never cached
by the service worker.
- **Query Parameters**: `fresh` (bool, default `false`): recompute every token instead of serving the
  server-side cached values (frontend 5 s, db/projects 30 s, parsed/git 15 s). The frontend never sends it:
  every sync, the manual *Check for Updates* included, reads the cached tokens.
```json
{ "db": "4cf36a4c8bf6", "frontend": "65c1f2a0583b", "parsed": "af7532653070", "git": "07886de81433", "projects": "5b0e1f7c2a94", "repo": "linux" }
```
Every `/api/` response also carries `X-KIP-Sync: <db>.<parsed>.<git>`.

#### `GET /api/system/ingest`
Ingestion status of the active repository and the server's own state (`webapp/backend/routers/system.py`,
G11), for the status tab (§5.37) and the settings popover (§5.5). Read with `SELECT`s only (§2.5):
`m_ingest_state` of the repository's network (status 1 = parsing, 2 = done; core/API.md §4.1) joined to
`m_commit`, the version names from `m_git_ref` of the repository, and `information_schema.TABLES` of the
configured schema. Cached for 10 s per repository.
- **`catalog`**: `tags` (tags in Git), `parsed`, `parsing`, `git_only` (tags not parsed).
- **`parsing`**: `[{version, commit, updated_at, stale}]`; `stale` when the entry was not updated for 30
  minutes — an interrupted run, which the parser rolls back on its next start.
- **`recent`**: the last 20 completions `[{version, commit, finished_at}]`.
- **`db`**: `{tables: [{name, rows_estimate, data_mb, index_mb}], total_mb}`; `available: false` and `error`
  when the database cannot be reached.
- **`sync`**: the tokens of `GET /api/sync`; **`server`**: `uptime_s`, `prewarm` (`idle` / `running` / `done`,
  §2.12), `jobs` (§2.15: building jobs with their progress, finished and failed counts), `caches` (in-memory
  caches with their size and bound), `disk_cache` (namespaces, entries and bytes, the global cap; §2.14).
```json
{
  "repository": "linux",
  "catalog": {"tags": 5807, "parsed": 3746, "parsing": 1, "git_only": 2061},
  "parsing": [{"version": "v5.4.81", "commit": "fc8334619167", "updated_at": 1793903867, "stale": false}],
  "recent": [{"version": "v5.4.80", "commit": "315443293a2d", "finished_at": 1793903866}],
  "db": {"tables": [{"name": "m_ast", "rows_estimate": 812345678, "data_mb": 40211.3, "index_mb": 9877.0}], "total_mb": 130755.9},
  "available": true,
  "sync": {"db": "e3a4169b1264", "frontend": "6cc6724a0d1b", "parsed": "907c976c3da8", "git": "8fb88aecdbb9", "projects": "c853313776af", "repo": "linux"},
  "server": {"uptime_s": 3600, "prewarm": "done", "jobs": {"building": [], "done": 2, "failed": 0, "workers": 2}, "caches": [{"name": "grep results", "size": 12, "maxsize": 128}], "disk_cache": {"namespaces": [{"name": "kconfig-models", "entries": 27, "bytes": 58071930}], "max_mb": 1024}},
  "generated_at": 1793903933
}
```

---

### 4.2. Filesystem & Code View API

#### `GET /api/fs/tree/{version_name}`
Retrieve the directory entries of a path in the kernel repository.
- **Parameters**:
  - `version_name` (path): Revision: a release tag, a branch, or any full/short commit SHA.
  - `path` (query): Directory path relative to repository root (e.g. `"kernel"`, `""` for root).
- **Behavior**:
  - The listing comes from the Git tree of that revision (complete for every commit); file entries are
    annotated with `fid` (`blob_id`) and `ftype` from `m_file_blob` by blob hash. Files whose blobs were never
    parsed have `fid: null` and `ftype: 7` (Raw).
  - Automatically identifies subdirectories and filters out directory records (`ftype == 0`) from the file
    entries list to prevent duplication.
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
Content of a file at a revision, with the AST token map of its indexed blob and the commit that last changed
it.
- **Source Retrieval Engine**: Uses persistent `git cat-file --batch` workers (`GitReader`) to stream exact
  blob contents directly from Git for the requested revision in sub-millisecond time (~0.1ms small / ~0.2ms
  big files). Preserves 100% byte fidelity without relying on local working tree checkouts. One stream answers
  one read at a time, so `git_reader` (`GitReaderPool`) keeps up to `READERS_PER_REPO` (4) of them per
  repository, spawned only when every existing one is busy: a file view no longer waits behind a history
  timeline (hundreds of blobs), a Kconfig model build or an include walk.
- **Indexed blob**: found by content: the Git blob SHA of the file is `m_file_blob.blob_hash`, so the tokens
  always belong to the text served, at a tag, at a branch that moved since it was parsed, or at a commit never
  ingested. Tokens are cached per `blob_id` (`_FILE_TOKENS_CACHE`), so revisions sharing a file content share
  them. A file whose blob was never parsed is served with `fid: null`, `ftype: null` and `tokens: []`.
- **Directory Detection**: If the path resolves to a Git `tree`, the endpoint returns the directory listing.
- **Last changed** (`file_info.last_changed`, `GitService.get_file_last_changed(version, path, blob_id)`):
  from the database when the file's blob is indexed, exactly one ingested commit wrote that content at that
  path (`m_commit_file (fnid, blob_id)`, version head commits left out: older databases hold version-diff rows
  there), the revision is a finished version and the writer was ingested no later than it (`source: "db"`,
  cached per (path, blob)); otherwise `git log -n1 <rev> -- <path>` (8 s timeout). Zero writers (the first
  version, content a merge produced) or several (content written twice) use Git. On v3.0, 51 of 63 random
  files are answered by the database, all equal to `git log`.
- **Binary / oversized files**: a NUL in the first KiB answers `is_binary: true`, more than 50 MiB
  `is_oversized: true`, both without `content` and with a `message` the code view shows in place of the file.
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
`tokens` rows are `[line_s, char_s, line_e, char_e, ast_id, type_id]`. `ftype` at the root is what the code
view's syntax scoping reads (`AstOverlay.setFileInfo`, §5.2).

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

#### `GET /api/fs/references/{version_name}`
What refers to a file at a revision (`FilesystemService.get_file_references`, `SELECT` only), the reader of
the parser's references:
- **Parameters**: `path` (a safe relative path), `limit` (entries per list, 1–1000, default 200).
- **Sources**: `m_file_reference` rows targeting the file's `fnid` — `ref_type` 1 includes (`included_by`), 2
  Kconfig `source` (`sourced_by`), 5 documentation mentions (`documented_in`, with the matched `details`) —
  read from the covering index `fref_tgt_src_idx` (`core/API.md` §4 row 28; ~0.1 s for the ~118k rows of
  `include/linux/sched.h` across all versions, which a row lookup per entry took 6 s to read), plus
  `m_kbuild_symbol` rules whose target is the file (`built_by`: one entry per (Makefile, line) with its `rule`
  and the `symbols` of the rule and its enclosing conditions). Rows are stored per (source path, blob): only
  those whose source blob is the revision's blob of that path count (`git_tree_resolver.blob_paths`). Include
  rows come from the parser's clang run, so they reflect its `include/asm` link and the `#if` branches active
  there (`includes_source: "parser"`, §2.7 item 4). A Git-only revision only has the rows of blobs the parser
  has seen; `indexed: false` when the file's own blob is not indexed.
- **Response**: `{ "version", "path", "indexed", "includes_source": "parser", "included_by": [{path, line}],
  "sourced_by": [{path, line}], "documented_in": [{path, line, details}], "built_by": [{path, line, rule,
  symbols}], "totals": {<list>: n}, "truncated", "elapsed_ms" }`, lists sorted by path and line and capped at
  `limit` each (`totals` counts before the cap). An unknown path answers empty lists; `400` invalid path,
  `404` revision unknown to Git.

#### `GET /api/fs/resolve_include`
Resolve a preprocessor `#include <...>` or `#include "..."` directive to an authoritative git repository file
path. Guarantees directory exclusion (`ftype != 0`) to prevent directory false-positives.
- **Parameters**:
  - `version` (query): Kernel version string (default `"v3.0"`).
  - `header` (query): Raw or cleaned include path (e.g. `"<linux/init.h>"`, `"linux/init.h"`, `"#include
    <linux/init.h>"`). Required.
  - `ast_id` (query): Optional AST Node ID of the include directive to check direct `m_ast_include` links.
  - `current_file` (query): Optional referring source file path (e.g. `"arch/x86/kernel/setup.c"`) to
    prioritize architecture-specific or relative header matches.
  - `arch` (query, optional): ARCH whose build layout resolves the include (`definition_arch`: unknown values
    fall back to `x86_64`).
- **Jailing**: `header` and `current_file` become candidate repository paths, so both are rejected with `HTTP
  400 Bad Request` unless `is_safe_rel_path()` accepts them (a traversing header such as
  `../../../../etc/passwd` is never resolved).
- **Resolution Strategy**:
  0. The compiler's search for the ARCH's build layout (`arch_layout_service.resolve_include`, §2.7 item 4):
     `"name"` in `current_file`'s directory first, then the `-I` directories, through build-time symlinks and
     generated wrappers (`<asm/atomic.h>` → `include/asm-x86_64/atomic.h` at v2.6.12 for x86_64,
     `arch/x86/include/asm/atomic.h` at v3.0); method `build_layout`, with `arch`. Steps 1-5 only run when it
     finds nothing (e.g. generated headers, `tools/`).
  1. If `current_file` is supplied, queries compiler-verified provenance from `m_file_reference`
     (`ref_type=1`, the rows of `current_file`'s blob at that path: `source_blob_id`/`source_fnid` joined to
     the revision's `m_bridge_checkpoint`).
  2. If `ast_id` is supplied, queries `m_ast_include` join `m_file_name`.
  3. Generates prioritized candidate search paths: relative to `current_file` directory, `header`,
     `include/{header}`, `include/uapi/{header}`, and `arch/{arch}/include/{header}`.
  4. Matches candidates against active non-directory indexed files (`ftype != 0`) in `m_checkpoint_tree` /
     `m_file_blob` / `m_file_name`.
  5. Falls back to the candidates present in the revision's Git tree (method `git_tree`); the work tree on
     disk is never consulted, since it may hold another revision.
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
Directory hierarchy of the revision weighted by bytes and file counts, built from `git ls-tree -r -l`
(`GitTreeResolver.list_tree_sizes`, LRU cached). Children are sorted by size, descending.
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

#### `POST /api/version/{version_name}/treemap/build`
The treemap of a subtree with what a configuration compiles (K4, `kbuild_index_service.build_treemap`). Body
`{arch, assignments: {SYM: value}, path, max_depth}` — assignments without the `CONFIG_` prefix (it is
accepted and stripped), at most 30,000, values `y`/`m`/`n`/strings/numbers (400 otherwise). Each object's
interned condition of the Kbuild index (§4.4) is evaluated once (`evaluate()`, the Python twin of
`build_condition.js`); a source file takes the best value of its objects (`y` > `m` > `n`); a configuration of
at least 50 symbols (an engine's resolved one) counts every symbol it leaves out as unset, a smaller one
leaves them unknown; `.c`/`.S` files of other architectures' trees are *not built*; headers and files no
Kbuild object names are *unknown*, never "not built" (as in §5.16). Response: the treemap shape above with,
per node, `built_y`, `built_m`, `not_built`, `unknown` (bytes) and `files_y`, `files_m`, `files_n`,
`files_unknown`. 202 while the index builds; results cached per (index, assignments digest, path, depth)
(`RepoScopedCache(maxsize=32)`); ~0.3 s once the index exists.

#### `GET /api/fs/search/{version_name}`
Fuzzy file-path search over the revision's Git tree (used by the Ctrl+K palette).
- **Parameters**: `q` (query), `limit` (query, default 50, max 200).
- **Ranking**: exact basename → basename prefix → basename substring → path substring → subsequence (fewest
  gaps first, queries of 2+ characters). Each keystroke scans the revision's search index
  (`GitTreeResolver.search_index`, lowercased once per commit) and keeps only the best `limit` matches
  (`heapq.nsmallest`); `total` counts every match.
- **Response**: `{ "version", "query", "total", "files": [{ "path", "name", "score" }] }`

#### `GET /api/fs/grep/{version_name}`
Full-text search of every file of a revision: `git grep` over its commit through `grep_service` (§2.1.3, which
does the validation, caps, concurrency limit and caching). Used by the palette's `%` mode (§5.15) and the
Search tab (§5.34).
- **Parameters**: `q` (query, 1–256 characters, one line), `mode` (`fixed` default: literal text, `-F`;
  `regex`: POSIX ERE, `-E`), `case` (`1`: match case; default case-insensitive), `word` (`1`: whole words),
  `path` (repeatable, at most 8: `drivers/net/`, `*.c`, `!*.h`; see §2.1.3 for the pathspec rules), `limit`
  (default 2,000, max 20,000 matches overall), `per_file` (default 50, max 5,000).
- **Errors**: `400` for an unsafe revision, an empty or multi-line query, an invalid path filter or a regex
  git rejects; `404` for an unknown revision; `503` when two searches already run for the repository (§2.1.3).
- **Response**: the matches sorted by path, then line. `truncated` tells the client a cap was reached (narrow
  the search); a `timed_out` answer is partial and carries `Cache-Control: no-store` so it is never stored
  offline.
```json
{
  "version": "v3.0", "query": "SYSCALL_DEFINE3(read", "mode": "fixed", "case": false, "word": false, "paths": [],
  "sha": "02f8c6aee8df3cdc935e9bdd4f2d020306035dbe",
  "matches": [{"path": "fs/read_write.c", "line": 402, "text": "SYSCALL_DEFINE3(read, unsigned int, fd, char __user *, buf, size_t, count)", "ranges": [[0, 20]], "clipped": false}],
  "files": 2, "total": 3, "truncated": false, "timed_out": false, "elapsed_ms": 412
}
```

#### `GET /api/fs/outline/{version_name}`
Definitions of one file at a revision, for the code view's outline, sticky scroll and the palette's `:name`
mode (§5.21, §5.15; `webapp/backend/services/outline_service.py`). The path's blob comes from the Git tree
(`git_tree_resolver.list_tree()`, then `map_blob_hashes()` for its `blob_id`); its definitions are the
`m_symbol_def` rows of that blob (per-blob and shared rows, `SELECT` only), at most 5,000. Answers are cached
per blob SHA (`RepoScopedCache(maxsize=1024)`): content addressed, so never stale.
- **Parameters**: `path` (query, repository-relative; traversal answers 400, a path absent from the revision
  404).
- **Kinds** (`outline_kind` from the AST type name and the linkage): `function` (function definitions,
  `SYSCALL_DEFINE`, assembly entries, Rust `fn`), `prototype` (declarations without a body, linkage 2), `type`
  (struct, union, enum, typedef, Rust types, impl blocks and modules, so methods nest under their `impl`),
  `macro` (including `macro_rules!`), `constant` (enum constants, Rust variants and fields), `variable`, `api`
  (`EXPORT_SYMBOL`, module parameters and attributes, a Rust `module!`'s name and parameters, tracepoints,
  per-CPU variables, initcalls, device tables, sysfs attributes), `kconfig`, `label`.
- **Nesting**: items are sorted by line; an item whose line range lies strictly inside a function, a type or a
  Kconfig menu is nested under the innermost one (enum constants under their enum, a `static` inside a
  function under it). Duplicate rows (a definition seen both shared and per blob) are merged.
- **Unindexed files** (the blob is not in `m_file_blob`) answer `{"indexed": false, "items": []}`. Kconfig
  entries are tags rather than symbol definitions, so a Kconfig file answers no items and the client outlines
  it from its text (§5.21).
```json
{
  "version": "v3.0", "path": "kernel/fork.c", "blob": "8f5bb4d1c2…", "indexed": true, "count": 83, "truncated": false,
  "items": [
    {"name": "copy_process", "kind": "function", "type_name": "C_functionprotodecl", "line_s": 1049, "line_e": 1418, "linkage": 1, "children": []}
  ]
}
```

#### `GET /api/fs/raw/{version_name}`
Serve an image blob of the revision (documentation figures). Only `svg`, `png`, `jpg`/`jpeg`, `gif` are served
(else 404); a `name.*` path picks the first existing of `.svg/.png/.jpg/.jpeg/.gif` (Sphinx convention).
Responses carry `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` so scripts
embedded in SVG never run; files over 50 MiB return 413.
- **Parameters**: `path` (query, jailed relative path), `repo` (query, optional repository for `<img>`
  requests that cannot send headers).

#### `GET /api/fs/compile_commands/{version_name}/export`
Download a Clang JSON Compilation Database (`compile_commands.json`) with one entry per `.c` file of the
revision's Git tree, excluding other architectures' `arch/<name>/` subtrees, as an attachment
(`Content-Disposition: attachment; filename="compile_commands-<version>-<arch>.json"`, both names reduced to
file-name characters). Commands include the arch/generic include paths, `include/linux/kconfig.h`,
`-D__KERNEL__` and `-nostdinc`.
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
Search symbol definitions by name prefix across functions, variables, structs, enums, enumerator constants
(`EnumConstant`), typedefs, and macros.
- **Revision scoping of symbol queries** (also `detail/{name}`, `xref/{name}`, `include/{ast_id}`, the
  callgraph and Pahole, §4.7): `version_name` may be a tag, branch, or any commit SHA, including commits that
  were never ingested into `m_commit`. `SymbolService._scope` builds `{blob_id: [paths]}` from the revision's
  Git tree (`GitTreeResolver.blob_paths`) and falls back to `m_bridge_checkpoint` only when Git is
  unavailable. Only definitions whose blob is in that tree are returned, with `file_path` taken from the tree.
  A symbol is therefore found at a commit whenever its file's exact content was parsed in any version; files
  whose content was never parsed contribute no symbols. The first query per commit costs ~0.3 s to build the
  blob map; later queries are cached.
- **Ranking and cost**: the exact name first, then shorter names, then by name; definitions of one name by
  path and line. Ordering the `m_symbol_def` view itself sorted every prefix match of every indexed version
  (328 s for `s`, 4.7 s for `sched`), so the search ranks distinct names first: `_name_page` reads `NAME_PAGE`
  (400) names at a time from the name indexes of `m_tag_def` and `m_blob_symbol_def` (`GROUP BY name ORDER BY
  LENGTH(name), name`, continuing after the last key) and merges them. Their definitions are then read in
  batches of names (`FIRST_NAME_BATCH` = 8, doubling up to 128, at most `SEARCH_CANDIDATES` rows each) and
  scoped to the revision until `limit` are kept or `MAX_NAME_PAGES` (8) pages were read. Warm, `s` answers in
  ~0.7 s and `sched` in ~1.1 s.
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
> All symbol endpoints (`search`, `detail`, `xref`) map raw AST type names (e.g. `C_enumequal` &rarr;
> `EnumConstant`, `C_functionproto` &rarr; `FunctionDecl`, `C_struct` &rarr; `StructDecl`, `C_SCtypedef`
> &rarr; `Typedef`) to readable presentation labels.


#### `GET /api/symbols/{version_name}/detail/{symbol_name}`
The definition of a symbol in the revision's tree (what Go to Definition opens). References are not part of
it: `xref/{name}` computes them, only for the views that list them.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `symbol_name` (path): Symbol identifier.
  - `arch` (query, default `x86_64`): ARCH value whose definition wins when the symbol is defined per
    architecture (§2.7); also the ARCH whose Kconfig model answers `CONFIG_*` / Kconfig symbols.
  - `from_file` (query, optional): file the symbol is used in; the definitions its `#include` chain reaches
    for `arch` win (§2.7 item 4).
- **Response** also carries `arch`, `from_file` and `definitions` (every in-tree definition in rank order, at
  most 50: `[{file, line_s, line_e, type_name, ast_id, linkage, visible}]`, `visible` = reached from
  `from_file`, `null` without it); `file_path`/`definition` are the first one.
- **Kconfig symbols** (`CONFIG_*`, or a name no C definition has): answered from the ARCH's Kconfig model with
  `is_kconfig: true`, `type_name: "kconfig"` and the model's detail under `kconfig`.
- **Response**:
```json
{
  "version": "v3.0",
  "name": "start_kernel",
  "type_name": "FunctionDecl",
  "file_path": "init/main.c",
  "line": 490,
  "line_s": 490,
  "definition": { "def_id": 1, "ast_id": 42, "tag_id": 7, "type_id": 21, "type_name": "FunctionDecl", "fid": 684, "file": "init/main.c", "line_s": 490, "line_e": 680, "attributes": [{ "name": "size_bytes", "val": "64" }] },
  "definitions": [{ "file": "init/main.c", "line_s": 490, "line_e": 680, "type_name": "FunctionDecl", "ast_id": 42, "linkage": 0, "visible": null }],
  "arch": "x86_64",
  "from_file": null
}
```

#### `GET /api/symbols/{version_name}/hover/{symbol_name}?arch=&from_file=`
The hover card of a symbol (G04, `webapp/backend/services/hover_service.py`): the definition `detail` ranks
first for `arch` and `from_file` (what Go to Definition opens, §2.7) with
- `kind` (the AST type name), `file`, `line`, `definitions` (how many in-tree definitions exist);
- `signature`: the definition's first lines up to the body's `{` (a struct/union/enum shows `struct inode { …
  }`), a `;`, or the end of a `#define` continuation, at most 6 lines;
- `doc`: the kernel-doc comment (`/** … */`) right above it — `{summary, description (first paragraph),
  params: [{name, text}] (≤ 12), more, line}` — or null (a plain comment is not kernel-doc);
- for Kconfig symbols, `kind: "kconfig"` and `kconfig: {name, type, prompt, depends_on, selects,
  defined_for_arch, help (first paragraph), more}` (the value comes from the browser's followed engine).
`found: false` (200) when nothing defines the name. Cached per (commit, name, ARCH, file) in a memory LRU of
4,096; the browser keeps answers in the `symbols` store. v3.0: `kstrdup` from `fs/ext4/super.c` → `char
*kstrdup(const char *s, gfp_t gfp)`, *allocate space for and copy an existing string*, `mm/util.c:19`.

#### `GET /api/symbols/{version_name}/xref/{symbol_name}`
Retrieve both definition location(s) and usage references (declarations, type usages, call sites, member
references, declaration references, macro expansions, told apart by each reference's `type` badge) across the
kernel codebase.
- **Parameters**:
  - `version_name` (path): Kernel version string.
  - `symbol_name` (path): Symbol identifier.
  - `limit` (query, optional int, 1-5000): Maximum number of reference records to return (useful for symbols
    with thousands of references like `kmalloc`). Always capped at `MAX_XREF_REFS` (5000), including when no
    limit is given.
  - `arch` (query, default `x86_64`): orders `definitions` for that ARCH (§2.7); `definition` is the first.
- **Definitions**: only definitions whose blob is in the revision's tree, so every `file` is a path of this
  revision. When the index knows the symbol but holds no definition of it in this tree — the revision's copy
  of the defining file was never parsed, which is common at a commit between two ingested releases —
  `definitions` is `[]`, `definition` is `null` and `definition_unindexed` is `true` (the references still
  resolve, see below).
- **References**: only references in blobs of the revision's tree are returned, grouped by role (`Decl`,
  `TypeUsage`, `Call`, `MemberRef`, `DeclRef` — which also takes unknown roles as `Ref` —, `MacroExp`) and
  ordered by file and line within a role. `file` is the path of the referencing blob in that tree (best-ranked
  for `arch` when the blob sits at several paths; not `target_fnid`, which is the target's file), `caller` is
  the name of the AST of the tag the reference sits in (e.g. the enclosing function), `fid` the referencing
  blob, and `limit` applies after scoping.
- **Reference targets**: a reference resolves to the AST of the definition its blob was parsed against, so the
  targets are the ASTs of the in-tree definitions, or — without any — every indexed AST of the symbol (at most
  `MAX_XREF_TARGETS` (64); one per content of the defining file), which is what keeps the usages of a revision
  with no in-tree definition. An AST is a target only when `m_ast.name` is the symbol's name: a marker
  definition (`EXPORT_SYMBOL(sym)`, an initcall) records the symbol's name against the AST of the *macro*,
  which is content-addressed and therefore one shared node for every symbol marked that way (180k
  `C_ExportSymbolDef` rows share 4 ASTs in a 2.6.x index), so using it would return the references of all of
  them. Such a definition is still listed in `definitions` (it is a real source location). Rust uses of a C
  symbol through bindgen (`bindings::kfree(p)`, `*mut bindings::page`) reference the `Rust_BindingRef` AST
  named by the symbol rather than one of its definitions (`parser/rust_ast/RUST_API.md` §7);
  `binding_ref_asts()` adds it to the targets (one `m_ast` lookup by name and type), so they are listed with
  the C references, in `.rs` files, with their role badge. A Rust item C calls (`#[no_mangle]`, `#[export_name
  = "x"]`) is a definition of its C name (`linkage` 0).
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
  - `version_name` (path): Revision (AST nodes are content addressed, so it does not change the answer and the
    service ignores it).
  - `ast_id` (path): Root AST Node ID to expand.
  - `depth` (query): Traversal depth limit (0-12, default 3).
- **Size bound**: AST nodes are content-addressed, so one subtree is referenced by many parents: subtrees are
  built once per (node, remaining depth) and reused, but a reused subtree still serializes at every position
  it occupies, so the body grows multiplicatively with depth. `MAX_AST_TREE_NODES` (20,000 serialized nodes)
  bounds it at any depth: each node built counts one, each reuse of a built subtree counts its whole size, and
  the first child that does not fit cuts the remaining children of its parent, which then carries `truncated:
  true`. Each node lists its children once, in `m_ast_container` priority order. A former `containers` list
  repeated every child next to its priority, read by no client; it doubled the body at every level and its
  positions were counted only after each subtree was finished, so the bound did not hold (397,000 positions,
  55 MB at depth 12 under `copy_process`; now 4,155 positions, 0.45 MB).
- **Cost**: the tree is fetched level by level (`m_ast_container` and `m_ast` rows of a whole level per query,
  `AST_TREE_CHUNK` = 1000 ids), not with one query per node: depth 3 below `copy_process` takes ~40 ms instead
  of ~250 ms (105 queries).
- **Response**: the root node itself. Children with `type_id` > 20 (inner statements and expressions) carry
  `tag_id: null`. Compiler attributes, struct/union size & alignment, and macro replacement bodies from
  `m_ast_attribute` are reported in `attributes`.
```json
{
  "ast_id": 42,
  "tag_id": 1234,
  "name": "start_kernel",
  "type_id": 5,
  "type_name": "FunctionDecl",
  "attributes": [
    { "name": "size_bytes", "val": "64" }
  ],
  "children": [
    {
      "ast_id": 43,
      "tag_id": null,
      "name": "setup_arch",
      "type_id": 21,
      "type_name": "CallExpr",
      "attributes": [],
      "children": []
    }
  ]
}
```

#### `GET /api/symbols/{version_name}/include/{ast_id}`
Retrieve imported symbols and target header file path for a preprocessor `CPPro_include` AST node.
- **Parameters**:
  - `version_name` (path): Revision.
  - `ast_id` (path): AST Node ID of the include directive. If `ast_id <= 0`, it is found from `tag_id`, else
    in the blob `file_path` has at this revision (the include tag covering `line`, else the one naming
    `header`).
  - `tag_id` (query): Optional tag ID of the include node.
  - `file_path` (query): Path of the source file containing the include.
  - `line` (query): 1-indexed line number of the include in the source file.
  - `header` (query): Include header target (e.g. `<linux/init.h>` or `linux/init.h`).
- **Definitions**: each symbol's definition is taken from the revision's tree (`SymbolService._scope`): in the
  header itself first, then in its directory.
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
Release-by-release history of one definition (function, struct, union, enum, typedef, macro, global, ASM
label, Rust item...) in the active repository (`HistoryService.get_symbol_history`,
`webapp/backend/services/history_service.py`).
- **Parameters**: `path` and `line` (query) plus optional `name` (the symbol under the cursor) and `ast_id`
  select the anchor: the narrowest tag of the file at `version_name` covering `line` that is named `name`
  (else has `ast_id`, else is a definition in `m_symbol_def`). When `name` is not defined at `path:line` (the
  cursor is on a use), the definition is located through `m_symbol_def` (the requested file first, then the
  `arch` query parameter's ranking (§2.7, default `x86_64`), then definitions with a body, then `.c` files).
  An anchor that is a Kconfig `config`/`menuconfig` entry returns the Kconfig history below. `include_rc`
  (bool, default `false`): also list pre-release tags (`-rc`, `-pre`, `-alpha`, `-beta`, `RC_RE`); by default
  only final releases are listed. `branch` (optional): the branch the timeline follows, see **Branch scope**.
- **Branch scope** (`history_scope()`): a timeline follows **one branch**: the indexed tags reachable from it
  (the `GET /api/branch/tags` walk, cached per branch head) and the branch's own ref when it is indexed; tags
  of other branches and bare-commit refs are left out, so stable point releases (`v2.6.32.71`) never
  interleave with mainline releases on `master`, and a stable branch lists the mainline releases up to its
  fork followed by its own point releases. Without `branch` it is chosen for `version_name`: the version
  itself when it names a branch, the default branch when the version is one of its tags (or is not a tag, e.g.
  a commit), else the branches whose name carries the version's release number (`stable/linux-2.6.32.y` for
  `v2.6.32.71`; at most 3 walked), the first one reaching it; the default branch when none does. `400` for a
  branch that is not a plain ref, `404` unknown, `503` when Git could not walk it. The response carries
  `branch` (the branch followed) and `rc_count` (pre-release tags of that branch, listed or not).
- **Identity**: versions are matched by the anchor's identity, `m_ast.name` + `type_id`, never by `ast_id` (a
  function's `ast_id` hashes signature + body, so it changes with every code change). When a blob holds
  several matches, a member of the anchor's `m_moved_tag` lineage (transitive, both directions, bounded to 500
  tags) wins, then the one closest to the previous version's line; a renamed lineage member is used only when
  no tag of the same name remains.
- **Algorithm**: the file's blob at every listed ref comes from one `git cat-file --batch-check` (content hash
  → `m_file_blob`). Per ref: same `tag_id` as the previous ref, or another tag with byte-identical code (its
  hash covers more than the definition's text) → `unchanged` (before, such a tag read `cosmetic` with an empty
  diff); another tag → `modified`, or `cosmetic` when the code is identical once comments and whitespace are
  removed; first appearance → `introduced` (`added` when it reappears); no match → `removed`/`absent`; blob
  never parsed → `unindexed`; blob parsed without the definition although a non-indented line of the file
  still names the symbol → `unparsed` (an indexing gap, the previous state is kept). Code comes from on-demand
  Git blob slicing via `m_bridge_tag` coordinates (cached in memory); diffs (with context lines) compare
  against the last present version.
- **Renames** (`_follow_renames()`): when the file has no blob at a ref but had an indexed one at the previous
  ref, the parser's rename rows (`m_blob_transition`: old blob → new blob, written for each file renamed in a
  version) and that ref's checkpoint (`m_checkpoint_tree`) name the new path; with exactly one candidate the
  timeline continues there (the remaining refs resolved on the new path; each entry carries its `path`, the
  first one at the new path `renamed_from`, shown as *renamed from* in the modal). Several candidates or none:
  `removed` as before. At most 5 renames are followed.
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
Release-by-release history of every definition of `CONFIG_<symbol>` (`CONFIG_` prefix optional) across all
Kconfig files (`HistoryService.get_kconfig_history`).
- **Parameters**: `include_rc` (bool, default `false`) and `branch` as above (the branch scope applies the
  same way); `version_name` picks the reported `path` and, without `branch`, the branch followed.
- **Algorithm**: the Kconfig tags of the symbol (`m_ast` name + `Kconfig_Config`/`Kconfig_Menuconfig` →
  `m_tag` → `m_bridge_tag`) give the blobs defining it and, through `m_bridge_checkpoint`/`m_commit_file`, the
  candidate paths; one `git cat-file --batch-check` resolves every (ref, path). Each defining file is parsed
  with `KconfigParser` (cached per blob SHA) to extract the definition's properties, including the
  dependencies inherited from enclosing `if`/`menu`/`choice` blocks. Definitions are paired per path (in line
  order) between consecutive refs: `added`, `removed`, `unchanged` (same tag or identical code), `modified` (a
  property changed) or `cosmetic` (text changed, properties identical). The entry status aggregates them
  (`introduced`, `added`, `modified`, `cosmetic`, `removed`, `unchanged`, `absent`, `unindexed`).
- **Response**: `{ "kind": "kconfig", "name", "ast_name": "CONFIG_<name>", "path", "paths", "include_rc",
  "branch", "rc_count", "total_versions", "change_count", "snippets": {tag_id: code}, "timeline": [{ "vname",
  "vid", "is_release", "prev_vname", "status", "lines_added", "lines_removed", "defs": [{ "path", "status",
  "tag_id", "line_s", "line_e", "facts", "changes", "diff", "lines_added", "lines_removed" }] }] }`. `facts` =
  `{ line_s, line_e, kind: config|menuconfig, type, prompt, defaults[], depends_on[], inherited_depends[],
  selects[], implies[], ranges[], choice, options[], help }` (conditions rendered as `X if COND`); `changes` =
  `[{ field, before, after }]` for scalar fields (`kind`, `type`, `prompt`, `choice`, `help`) and `[{ field,
  added[], removed[], before[], after[] }]` for list fields.

#### `GET /api/history/commits`
Commits behind one change of a history timeline (`HistoryService.get_change_commits`).
- **Parameters**: `path`, `to` (the version where the change is observed), `from` (the previous timeline
  version; omitted: the whole history up to `to`), `line_s`/`line_e` (the definition's lines at `to`), `limit`
  (1-500, default 100). Revisions must match `^[A-Za-z0-9][A-Za-z0-9._/+-]*$`.
- **Database** (`_db_change_commits()`, `source: "db"`, `SELECT` only): when `from` and `to` are release tags
  joined by release segments (`walk_segments()`, §2.17) and by finished ingestions (each version's
  `m_ingest_state.base_commit_id` leads from `to` back to `from`). With a line range `to` must have been
  ingested directly on `from` (one range): the tags of `to`'s blob overlapping the lines (`m_bridge_tag`), the
  commits Step 6.5 bridged to them (`m_bridge_commit_tag`: each commit's hunks mapped to `to`'s coordinates),
  kept when in those segments (`m_commit_release`: a blob that reappears does not bring the commits of an
  older range). Without a line range: the commits whose `m_commit_file` rows name the path, as new or old
  name. Newest first (committer date, then `commit_id`). Merges have no file rows, so they are never listed
  (`git log` shows a merge only when it differs from every parent for the path); `git log -L` may also list
  commits that only moved the lines.
- **Git** (`source: "git"`, every other case: branches, unindexed blobs, ranges the ingestions do not join):
  `git log -L<line_s>,<line_e>:<path> --no-patch from..to` (exact: the commits that touched those lines);
  without a line range (a removed definition) `git log from..to -- path`. LRU cached (1024 entries) per
  repository, path, range and lines.
- **Response**: `{ "source": "db"|"git", "path", "from", "to", "line_s", "line_e", "total", "commits": [{
  "commit_hash", "commit_hash_short", "author_name", "author_email", "author_date", "subject" }] }`.

#### `GET /api/docs/{version_name}/labels`
Index of every `.. _label:` target under `Documentation/` at the revision (LRU cached per commit), used to
resolve `:ref:` across documents: from the parser's stored labels (`m_blob_facts` family 5,
`parser/doc_labels.py`, which reads a title in the same window `git grep -A6` gives) when every
`Documentation/**/*.rst` blob of the revision has them, assembled in tree order (the first label wins); else
`git grep` with context.
- **Response**: `{ "version", "total", "source": "db"|"git", "labels": { "<label>": { "path", "line", "title"
  } } }` (labels lowercased; `title` is the section the label precedes, when one follows).

#### `GET /api/docs/{version_name}/kernel-doc`
kernel-doc comments of a source file, filtered like the `.. kernel-doc::` directive.
- **Parameters**: `path` (query), `export` / `internal` (bool: only / never `EXPORT_SYMBOL*` identifiers),
  `functions` (space- or comma-separated identifiers), `doc` (a `DOC:` section title), `no_identifiers`.
- **Response**: `{ "version", "path", "total", "exported": [...], "entries": [{ "kind":
  "function|macro|struct|union|enum|typedef|doc", "name", "brief", "signature", "params": [{ "name", "desc"
  }], "sections": [{ "title", "body" }], "line" }] }`. Descriptions keep kernel-doc markup (`@param`,
  `%CONST`, `&struct foo`) for the client to render.

---

### 4.4. Kconfig & Architecture Defconfigs API

All Kconfig endpoints read the Git objects of the requested revision (never the work tree), accept an optional
**toolchain profile** (`toolchain` query parameter holding JSON on GET endpoints, `toolchain` body field of
the auto-solve POST; see `GET /toolchain` below) and are scoped by an **ARCH value** (`arch` parameter,
default `x86_64`): the value `make ARCH=<arch>` would receive. The top Makefile maps some values onto another
source directory (`i386`/`x86_64` -> `x86`, `sparc32`/`sparc64` -> `sparc`, `sh64` -> `sh`, ...); `x86`,
`sparc` and `sh` themselves are the generic forms whose bitness is chosen in menuconfig. The model is built by
`parser/kconfig_ast/kconfig_model.py` and evaluated by `parser/kconfig_ast/kconfig_eval.py` (server) and
`kconfig_engine.js` (browser); both reproduce the kernel's `conf` output byte for byte (see
`parser/kconfig_ast/KCONFIG_API.md` section 8). Models are cached per (repository, revision, ARCH, toolchain)
in a 6-entry LRU, and persist across restarts as gzipped JSON under `~/.cache/kernelinfo/kconfig-models/`
(`KERNELINFO_CACHE_DIR` overrides the base directory; 32 files, least recently used evicted, ~4 MB each for a
modern tree), keyed by commit SHA, ARCH, toolchain profile and a hash of the model-building code, so a code
change or a moved branch never reuses a stale model (a cold build of a modern tree takes ~2-3 s, a cached one
~0.3 s). A build costs about a second of CPU and tens of MB, and the toolchain profile is part of the key, so
builds are bounded: each key has its own lock (concurrent requests for one model wait for that single build
instead of duplicating it, and never block requests for other models), at most two models are built at once
across all keys, and the cache lock is never held while a model is built.

#### `GET /api/kconfig/{version_name}/toolchain`
Default toolchain profiles of a revision. Linux 4.18+ Kconfig runs `$(shell,...)` checks against the compiler,
linker and assembler while it is parsed; the server answers them from a profile
(`parser/kconfig_ast/kconfig_toolchain.py`). Response: `{ "version", "arch", "kernelversion", "macros": true,
"gcc": {profile}, "clang": {profile} }` where a profile is `{ "cc", "cc_version", "ld", "ld_version",
"assembler", "binutils_version", "cross_compile", "llvm_utils", "pahole_version", "rustc_version",
"gcc_plugins", "host_release", "env", "probes" }`. `macros: false` means the revision predates the macro
language: only environment variables (`option env=`) matter.
- **Toolchain parameter**: validated before use — unknown keys are ignored, each value is coerced to its
  field's type, `env`/`probes` must be objects of at most 128 string entries (each value at most 512
  characters), and a wrong shape (`{"env": "text"}`) answers `HTTP 400 Bad Request` rather than failing inside
  the model build. It is any subset of the profile, e.g. `{"cc": "clang", "cc_version": "18.1.3", "env":
  {"CC_VERSION_TEXT": "Ubuntu clang version 18.1.3"}, "probes": {"cc-option:-fsanitize=kcfi": "n"}}`. Missing
  fields take the defaults of that kernel release. `env` overrides any environment variable Kconfig reads;
  `probes` forces a check (`"y"`/`"n"`) or a command output (text), keyed as reported in the model's
  `toolchain.probes`.

#### `GET /api/kconfig/{version_name}/tree`
The finalized configuration model of one ARCH, in compact transport form, for the client-side engine
(`decodeModel()` in `kconfig_engine.js`).
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
  `semantics` lists `conf` behaviours of this revision (detected from its `scripts/kconfig` sources) that both
  engines follow. `toolchain` reports what the build environment contributed: every environment variable read
  (`env_used`), every toolchain check with its answer and source (`toolchain` = derived from the profile,
  `knowledge` = known first gcc/clang versions, `assumed` = no data, assumed to succeed, `override`), the
  Kconfig entries using it, and `$(error-if)`/`$(warning-if)`/`$(info)` messages.
  Expression nodes: `["s", name]`, `["c", text]`, `["&"|"|", a, b]`, `["!", a]`, `["="|"!="|"<"|"<="|">"|">=",
  a, b]`; `-1` = no expression. Menu `kind` codes: 0 root, 1 menu, 2 choice, 3 config, 4 menuconfig, 5
  comment, 6 if. Symbol keys: `t` type, `p` prompt visibilities, `d` defaults `[value, condition]`, `r`
  ranges, `s` selects, `i` implies, `rd` reverse dependency, `im` implied-by, `dd` direct dependency, `m`
  defining menu entries, `ch` choice of a choice value, `ic`/`op`/`mb` choice flags and members, `au`
  automatic (env/choice), `env`.

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}`
Arch-scoped details of a symbol (with or without `CONFIG_`). The same name can be declared in different files
per architecture (e.g. `64BIT`: `arch/x86/Kconfig` for x86_64, `arch/sparc/Kconfig` for sparc64).
- **Parameters**: `arch` (query, default `x86_64`).
- **Response**: `name`, `arch`, `srcarch`, `defined_for_arch`, `type` (1 bool, 2 tristate, 3 string, 4 hex, 5
  int) / `type_name`, `prompt`, `prompts`, `help`, primary `file_path` / `line_s` / `line_e` / `definition`,
  `declarations` and `definitions` (every definition for this ARCH: `file_path`, `line_s`, `line_e`, `prompt`,
  `kind`, `help`), `depends_on` (symbols named by its own `depends on` lines), `selects`, `implies`,
  `defaults` (`[{value, cond}]`), `def_val`, `ranges`, `choice`, `env`, `kconfig_refs` (entries referring to
  it), `kbuild` (Makefile rules, see usage), `compiled_files` (`[{file_path, target_obj, compile_mode,
  makefile, line, other_arch}]`: the existing files among the `kbuild` rules of this revision). Reverse
  dependencies and evaluated expressions are not part of it: the KConfig view reads them from its own engine
  (§5.8).
- When the symbol is not declared for this ARCH: `defined_for_arch: false` and `declarations` lists its
  declarations in other architectures' Kconfig files (404 if declared nowhere), at most 50 by path then line.
  For a finished version they come from the Kconfig entry tags of the revision's Kconfig blobs
  (`KconfigService._other_arch_definitions_db()`: `m_bridge_tag` of those blobs → `m_tag` → `m_ast` named
  after the symbol, `Kconfig_Config` / `Kconfig_Menuconfig`; `line_e` is the entry's last line), ~0.03 s;
  other revisions run `git grep '^\s*(menu)?config\s+NAME\s*$'` over `*Kconfig*` (`line_e` = `line_s`).

#### `GET /api/kconfig/{version_name}/symbol/{symbol_name}/usage`
Where `CONFIG_<symbol>` is used (Git grep of the revision, cached; a `git grep` that fails or times out
answers 503 and is not cached).
- **Parameters**: `arch` (default `x86_64`), `limit` (1-2000, default 400). `symbol_name` must be an
  identifier (§2.1.2) or the request is rejected with 400.
- **`kbuild` source** (`KconfigService.kbuild_usage()`, shared by the symbol detail): from `m_kbuild_symbol`
  (`core/API.md` §4 row 61) when every Makefile/Kbuild file of the revision's tree has its (blob, path) pair
  stored (`_kbuild_pairs_covered()`, marker rows of files without rules included; cached per commit), one
  indexed query on the symbol, rows kept for the revision's pairs, one entry per binding (a rule beats a
  condition); otherwise `git grep -l -w -F CONFIG_<name> -- *Makefile* *Kbuild*` and `KbuildParser` on the
  matches. Both paths build entries with `parser/kbuild_symbols.py` (`usage_rule()`), read at most 200
  Makefiles, and resolve a missing `.c` object as `.S`, then `.rs`.
- **Response**: `kbuild` (`[{makefile, line, rule, target_obj, path, exists, kind: object|composite|directory,
  via: rule|condition, compile_mode, other_arch}]`: objects and directories built when the option is enabled,
  including rules inside `ifeq ($(CONFIG_X),y)` blocks), `source_refs` (`[{file_path, line, text,
  other_arch}]` in `.c .h .S .lds .dts .dtsi`, `CONFIG_X` and `CONFIG_X_MODULE`), `source_ref_count`,
  `source_refs_truncated` (the symbol has more than 20,000 references, so the list was cut before being
  cached; the view shows the count with a `+`), `source_file_count`, `kconfig_refs` (`[{kind:
  depends_on|select|imply|default|prompt_condition|select_condition|imply_condition, by, file, line}]`).
  `other_arch` marks paths in an architecture tree (`arch/<a>/`, or `include/asm-<a>/` in 2.6.x) the ARCH's
  build layout does not use (§2.7).

#### `GET /api/kconfig/{version_name}/build`
Kbuild build condition of source files: under which configuration `make ARCH=<arch>` compiles each one
(`KconfigService.build_conditions`, `parser/kbuild_conditions.py`, see `parser/kconfig_ast/KCONFIG_API.md`
§6.4). The condition is an expression over `CONFIG_*` symbols that the client evaluates against its own
configuration (`build_condition.js`), so one response serves every configuration of the ARCH. Resolvers are
cached per (revision, ARCH) (4 entries); the first request of a revision reads its Makefiles and lists the
`#include`d `.c`/`.S` sources once (`_included_sources()`: from the parser's stored directives, `m_blob_facts`
family 3, when every `.c`/`.h`/`.S` blob of the commit has them — directives start a line, so one inside a
comment no longer counts — else one `git grep`, ~1 s; kept on disk per commit SHA, `included-sources`).
- **Parameters**: `path` (query, repeatable, up to 64), `arch` (default `x86_64`).
- **Response**: `{version, arch, srcarch, files: {path: {status, expr, rules, reason}}}`:
  - `status`: `rule` (Kbuild rules name the object; `expr` says when it is built), `other_arch` (in an
    architecture tree, `arch/<a>/` or 2.6.x `include/asm-<a>/`, that the ARCH's build layout does not use,
    §2.7: never built or used, `expr` = `"n"`), `no_rule` (the fully parsed Kbuild file of the directory does
    not mention the object: never built, `expr` = `"n"`), `unknown` (headers, files outside Kbuild, objects
    built by custom rules, host programs, `#include`d sources, lists computed by `$(shell)`/`$(wildcard)`:
    `expr` = `"?"`).
  - `expr`: `"y"`/`"m"`/`"n"`/`"?"`, `{"sym": X}`, `{"def": X}`, `{"eq": [a, b]}` (make strings of literals
    and `{"val": X}`), `{"not": e}`, `{"and": [...]}`, `{"or": [...]}`; three-valued (unknown never yields
    `"n"` on its own).
  - `rules`: `[{makefile, line, text}]`: the object's rule first, then the directory chain up to the top-level
    goal list.
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

#### `GET /api/kconfig/{version_name}/kbuild/index?arch=` · `POST …/kbuild/modules` · `GET …/kbuild/file?arch=&path=`
Kbuild object index of one revision and ARCH (S04, `webapp/backend/services/kbuild_index_service.py`): every
object the Kbuild files name (`KbuildConditionResolver.objects()`, parser/kconfig_ast/KCONFIG_API.md §6.4)
with its module name, kind, build condition (directory chain included; evaluated client-side by
`build_condition.js`) and member sources. Built by a background job (§2.15, ~5 s for v3.0 x86_64: 6,338
objects, 5,889 distinct conditions), cached on disk per (repository, commit, ARCH, format) and in an in-memory
LRU of 4; every endpoint answers 202 while it builds.
- **`index`**: `{sha, arch, srcarch, exprs: [condition, …], objects: [{t: target, n: modname, k: kind, e:
  condition index, m: [[source, condition index], …]}]}` — conditions are interned (many objects share a
  directory chain); ~1.1 MB before gzip; `ETag` from the cache key.
- **`modules`** (`POST {arch, names: [...]}`, at most 2,000 names; dashes and underscores are the same):
  `{modules: {name: {objects: [{target, expr, members, module_capable}], required_symbols}}, unmatched}` —
  `required_symbols` are the symbols whose value `n` makes the condition `n` (`required_symbols()`),
  `module_capable` whether a tristate symbol can make it `m`.
- **`file`**: the objects one source is part of, with their condition, the member's own condition, the members
  and the rules naming them.
- **`modules` with `"gates": true`** (K1) also returns `module_gates`: every symbol gating a module-capable
  object (`module_gates()`, 5,271 at v3.0 x86_64) — the options *Hardware… → Trim* may turn off.

#### `GET /api/kconfig/{version_name}/fragments`
Configuration fragments `scripts/kconfig/merge_config.sh` merges (K2): `kernel/configs/*.config` and
`arch/<srcarch>/configs/*.config` at the revision (`git ls-tree`; none before v3.17, 12 at v6.9.9 x86_64).
- **Response**: `{ "version", "arch", "fragments": [{ "name", "path", "scope": "common" | <srcarch> }] }`.
  Their text comes from `GET …/defconfig?file_path=<path>`.

#### `GET /api/kconfig/{version_name}/defconfigs`
Defconfigs of the ARCH's source directory: `arch/<srcarch>/defconfig` and every file under
`arch/<srcarch>/configs/` (nested ones such as powerpc `40x/acadia_defconfig` included). The one flagged
`is_canonical` is what `make ARCH=<arch> defconfig` loads: `KBUILD_DEFCONFIG` from `arch/<srcarch>/Makefile`
evaluated with ARCH/SRCARCH known, `CONFIG_*` undefined and `CROSS_COMPILE` set (e.g. `x86` ->
`i386_defconfig`, `x86_64` -> `x86_64_defconfig`, `powerpc` -> `ppc64_defconfig`), else
`arch/<srcarch>/defconfig`.
- **Response**: `{ "version", "arch", "defconfigs": [{ "name", "file_path", "fid", "is_canonical" }] }`.

#### `GET /api/kconfig/{version_name}/defconfig`
Raw defconfig text and its literal assignments. Nothing is injected: everything a defconfig leaves out is
derived by kconfig when it is loaded.
- **Parameters**: `file_path` (name or path; empty = the ARCH's default defconfig;
  `kernel/configs/<name>.config` for a fragment — any other `kernel/` path, and any `..`, is refused with
  400), `arch`, `toolchain`.
- **Response**: `{ "version", "name", "file_path", "arch", "srcarch", "bits", "values", "content" }`; `bits`
  is `64BIT` after resolving the defconfig with kconfig (null for architectures without that symbol).

#### `GET /api/kconfig/{version_name}/presets`
Every ARCH value of the revision: one per `arch/*` directory plus the top Makefile's aliases.
- **Response**: `{ "version", "default_arch", "targets": [{ "id", "label", "arch", "srcarch", "bits", "env":
  {"ARCH", "SRCARCH", "KERNELVERSION"} }] }`. The default defconfig of each ARCH is the one `GET /defconfigs`
  flags.

#### `GET /api/kconfig/{version_name}/search`
Search symbol names and prompts of the ARCH model (`CONFIG_` prefix optional). Parameters: `q`, `limit`
(1-500, default 25), `arch`. Response: `{ "version", "arch", "query", "count", "total", "symbols": [{ "name",
"type", "type_name", "prompt", "help", "file_path", "line_s" }] }`.

#### `GET /api/kconfig/{version_name}/graph`
Direct relations of one symbol in the ARCH model: its own `depends on` symbols (`depends_on` edges), `selects`
/ `implies` targets, and selectors (`selects` edges into it). Parameters: `symbol`, `arch`, `toolchain`.
Response: `{ "version", "arch", "symbol", "edges": [{ "from", "to", "type" }] }`.

#### `POST /api/kconfig/{version_name}/autosolve`
Legal changes (the ones menuconfig would accept, applied in order) that make `target_symbol` reach `value`
from `current_values`. The solver satisfies prompt visibility recursively (AND: both sides, OR: cheapest side,
`!X`: lowers X), selects prompt-less symbols through a selector, and refuses symbols forced by select, fixed
by the architecture or without prompt.
- **Body**: `{ "target_symbol": "KVM_INTEL", "value": "y", "arch": "x86_64", "toolchain": {...},
  "current_values": {"KVM": "n"} }`.
- **Response**: `{ "solution_found": true, "toggles_needed": [{ "symbol": "KVM", "from": "n", "set_to": "y",
  "to": "y" }, { "symbol": "KVM_INTEL", "from": "n", "set_to": "y", "to": "y" }], "total_toggles": 2,
  "blocked_by": [], "final_value": "y", "target": "KVM_INTEL", "arch": "x86_64" }`.

Validation, import, export and configuration comparison run in the browser on the same model
(`kconfig_engine.js`, §5.8); the server only builds the model and answers the auto-solver.

---

### 4.5. Maintainers & CREDITS API

**`@domain` search** (`_domain_query`, `_address_in_domain`): a `q` that starts with `@` (e.g. `@intel.com`)
filters by email domain instead of substring. An address matches when its host is the domain or one of its
subdomains, case-insensitively (`@intel.com` matches `x@intel.com` and `x@linux.intel.com`, not `x@lintel.com`
or `x@intel.com.au`; `@linux.intel.com` narrows to that subdomain). Each address of a comma-joined email field
is tested on its own, with surrounding `<>`, `[]`, `()` and quotes ignored; disguised addresses (`x at
intel.com`, `x#intel.com`) do not match. Names, subsystem names and CREDITS descriptions are not searched. A
bare `@`, or a query with a second `@` or whitespace (`jdoe@intel`), is an ordinary substring search.
`/overview` and `/credits` resolve the matching `person_id`s first (`MaintainerService._domain_person_ids`: a
`LIKE` prefilter, then the host test per address).

**Version scoping** (`MaintainerService._version_scope`): sections and CREDITS entries are content addressed.
A section unchanged since an earlier MAINTAINERS keeps the row stored with that earlier blob
(`m_maintainer_section.blob_id`), so the sections of a version are found through its MAINTAINERS blob's tags
(`m_bridge_tag` → `m_tag.ast_id`). One `ast_id` names every stored version of a section (the AST is keyed by
name), so one row per section name is chosen by `parser/maintainer_ast/maintainer_store.py`
(`MAINTAINER_API.md` §2.3): the row whose own blob places the same tag, else the newest row stored no later
than this blob, else the newest. CREDITS entries likewise through `m_credits_entry.ast_id`, one per (name,
person). v3.0 lists 925 sections and 507 CREDITS entries, exactly its files' contents. A version whose
MAINTAINERS (or CREDITS) blob was never parsed lists none of them (`indexed: false` in the overview) rather
than every section ever parsed. The id lists are cached per blob (`_BLOB_SECTIONS_CACHE`,
`_BLOB_CREDITS_CACHE`, 64 entries each); the subsystem matcher per version (`_MAINTAINER_CACHE`, 16 entries).

**File → subsystems** (`MaintainerService.sections_for_paths`, used by `resolve_subsystems_for_file()` and
`/match`): the sections of a path come from `m_maintainer_file` (Step 6.7, `MAINTAINER_API.md` §5) restricted
to the version's sections, ordered by a matcher over only those sections (the full matcher's order: sections
match independently). Every path matches THE REST (`F: */`), so a path without rows has not been computed yet;
it, an unknown path, a revision whose MAINTAINERS was never indexed, and a database with the older blob-keyed
`m_maintainer_file` (`_section_file_rows_readable()`) use the full matcher. `/match` reads all touched paths
in one query.

#### `GET /api/maintainers/{version_name}/overview`
Search and list the subsystems of the version's `MAINTAINERS` file.
- **Parameters**:
  - `version_name` (path): Revision.
  - `q` (query): Search filter across subsystem names, maintainer names, and emails. `@domain` keeps the
    sections with a member whose address is in that domain (see **`@domain` search**).
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
Retrieve comprehensive details for a subsystem section, including member rosters, file matching rules (`F:`,
`X:`), and the files it covers.
- `files`: the paths of the version's tree the section version covers (`m_maintainer_file`, filtered by the
  revision's tree), the first `SECTION_FILES_LIMIT` (500) by path; `file_count` is the total and
  `files_truncated` tells whether the list was cut.
- `members[].in_credits`: an alias of the member (same identity rule as the Developers directory) has an entry
  in the version's CREDITS file.
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
  "patchwork": "",
  "bug_url": "",
  "chat_channel": "",
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
  "file_count": 2,
  "files_truncated": false
}
```

#### `GET /api/maintainers/{version_name}/person/{person_id_or_email}`
#### `GET /api/maintainers/{version_name}/person?email=...`
Retrieve developer profile, CREDITS biographical data, maintained subsystems, and git contribution statistics.
An email address goes in the `email` query parameter (its `@`, `<`, `/` do not survive a path segment); a
numeric id or a name in the path.
Subsystems, the CREDITS entry, recent commits and contribution stats are aggregated over every alias persona
of the developer (same identity rule as the Developers directory, see **Persona identity merging** below), so
a profile always matches its directory card. Contribution stats count distinct commits per role. An email/name
lookup prefers a named persona over a name-less one.
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
Emulate `scripts/get_maintainer.pl` by evaluating touched paths or raw patch content against the subsystem
pattern rules of the version's MAINTAINERS. The `in_credits` flags come from the version's CREDITS, read once
per version (`_CREDITS_KEYS_CACHE`).
A file without `m_maintainer_file` rows is matched against every MAINTAINERS pattern, so at most
`MAX_PATCH_FILES` (200) files of one patch are resolved; the rest are counted in `skipped_files_count` (the
patch reviewer says how many were left out) and not matched (2,000 files took ~14 s before this cap, and the
cost was paid even when nothing matched).
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
Returns one entry per developer: several rows of one entry and the same person under other alias personas
collapse into the newest entry. Its addresses are unioned into `emails`, empty bio fields are filled from
older revisions, and `email` falls back to the merged list when the newest revision lost it. Names are cleaned
(see **Persona identity merging**).
- **Parameters**:
  - `version_name` (path): Revision.
  - `q` (query): Search on name, email and description; `@domain` matches the entry's addresses only (see
    **`@domain` search**).
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
Search and list all kernel developers, maintainers, reviewers, and contributors recorded in the kernel persona
registry (`m_maintainer_person`).

**Persona identity merging**: the registry holds one row per (name, email) alias, so one developer appears
under many `person_id`s. Rows are merged into one entry per identity (`MaintainerService._identity_index`,
cached per repository until the registry or the stored identities change). The parser stores each alias's
identity in `m_person_identity` (`core/API.md` §4 row 59, refreshed after each version, after release
statistics and at start), so the index is read from it; aliases without a stored row (registered since its
last refresh, whatever their `person_id`) get their own `identity_key()` (no name-less merge) until the next one, and a database without stored rows computes the
index from the whole registry (`build_identity_index`, `parser/git_ast/identity.py`):
- Identity = name folded for case/whitespace, with surrounding quotes and a leaked `From:` prefix stripped
  (`_clean_name`, also the displayed name; `clean_name` / `identity_key` of `parser/git_ast/identity.py`,
  shared with the parser's release statistics).
- A name-less persona joins the named identity that owns its address; when several do (typo'd name, nickname),
  the one using it on the most personas, then the largest, wins.
- Named identities are never merged with each other by email: transitive email links chain distinct people
  together (shared/mistyped addresses), so `"Greg KH"` stays separate from `"Greg Kroah-Hartman"`.
- The merged entry uses the alias with a name and email, most subsystems, CREDITS, commits as
  `person_id`/`email`; flags are OR-ed over aliases, `subsystems_count` is the distinct subsystem names across
  aliases. The search and `role` filter apply to the merged identity (a match on any alias returns the whole
  identity); sorting happens after merging.
- **Parameters**:
  - `version_name` (path): Revision (`subsystems_count`, `is_maintainer`, `is_reviewer` and `in_credits` count
    only its MAINTAINERS and CREDITS).
  - `q` (query): Case-insensitive search on developer name or email. `@domain` keeps the identities with an
    alias address in that domain, and each entry then carries `matched_emails`: those addresses, the
    representative's own first (the Developers card shows the first).
  - `role` (query): Role filter (`"all"`, `"maintainer"`, `"reviewer"`, `"credits"`). Default `"all"`.
  - `sort` (query): Sort order (`"activity"` for subsystems count descending, or `"alpha"` for alphabetical
    A-Z). Default `"activity"`.
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
Search the indexed commits of the request's repository, newest first (the Commits view's search; its
unfiltered list is the timeline below).
- **Repository scoping**: commit ids are global, so the query is restricted to the repository's Git network
  (`m_commit.network_id` = `m_repository.network_id`, `helpers.get_active_network_id`): other repositories of
  the database never appear. A repository that was never parsed answers an empty list.
- **Current-date filtering & Git fallback**: By default, commits with future dates (`author_date > now`) are
  excluded to hide erroneous timestamps. When `q` is provided, future-dated commits can be matched if searched
  for. If `q` names an unparsed commit hash or prefix not in `m_commit`, results fall back to querying the Git
  CLI directly.
- **Parameters**:
  - `version_name` (path): Revision (must resolve, else 404).
  - `page` (query): Page number (1 or more, default 1).
  - `limit` (query): Commits per page (1-500, default 50). Bound again in the service, which also clamps the
    derived `OFFSET` (`MAX_COMMIT_PAGE`, `MAX_COMMIT_OFFSET`), since both are bound straight into SQL.
  - `q` (query): Filter by commit subject, hash prefix (matched via indexed binary hash range
    `helpers.commit_prefix_range`), or author name. Wildcards are escaped (§2.2), so `%` and `_` match
    literally. Commits are traversed using primary key order (`c.commit_id DESC`), preventing full-table
    filesort timeouts.
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
- **Unparsed commits & Git fallback**: When a commit hash or reference is not found in `m_commit`, the service
  falls back to inspecting the Git repository directly (`git log`, `git show`, `git diff-tree`), parsing
  commit headers, trailers, and modified files in memory without database writes. Trailers are read as version
  ingestion stores them (`git_service.message_trailers()`: `git_commit_parser.iter_trailers()` with
  `CommitRole.from_trailer_prefix()`, every role, name-less and bare-address trailers, persons normalized by
  `identity.person_key()`, one per person and role), named with `ROLE_NAMES`, and merged into `contributors`
  by `merge_contributors()`, the database path's rule, so a commit lists the same contributors from either
  source.
- **Modified files**: `files` comes from `m_commit_file` (`files_source: "db"`). `git log -p` emits no diff
  for a merge, so the parser stages **no file rows for merge commits** (`parser/git_ast/GIT_API.md` §2); for
  any commit whose staged list is empty the service resolves it live with `git diff -M --name-status <first
  parent> <commit>` and reports `files_source: "git"` — the same comparison `.../diff/{hash}` renders, so the
  *Modified Files* count can no longer disagree with **± Show changes**. A commit that genuinely changes
  nothing keeps an empty list and `files_source: "none"`. `is_merge` is set from the commit's parent count in
  `m_commit_parent`, which also gives the first parent of that diff (Git is asked, `rev-list --parents`, only
  for a commit without parent rows: a root, or one whose parents were not ingested), and a list longer than
  5000 entries is cut with `files_truncated: true`.
- **Parameters**:
  - `version_name` (path): Revision (not used: commit ids and hashes are global).
  - `commit_id_or_hash` (path): Commit integer ID, 40-character SHA-1 hash, or a short hex prefix (4-39
    digits). A prefix is a range over the binary hash in which every digit counts
    (`helpers.commit_prefix_range`: an odd length bounds its last digit, so the 7-digit abbreviations Git
    prints resolve exactly); a prefix two commits share answers `HTTP 400` (ambiguous), as does anything that
    is not hex. The same range rule resolves short SHAs used as a version name (`get_version_info`, where an
    ambiguous prefix resolves to nothing).
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
Execute on-demand live resolution of a Git commit against the Public-Inbox / LKML archive without pre-indexed
database writes.
- **Parameters**:
  - `version_name` (path): Revision (not used: commit ids and hashes are global).
  - `commit_id_or_hash` (path): Commit integer ID or hex hash.
- **Read-Only Safety**: Strictly adheres to the zero-write backend invariant; queries the commit features,
  extracts `git patch-id` and blob hashes, invokes `MailingListClient.resolve_commits()`, and synthesizes
  linked email records dynamically in memory without executing any `INSERT` or `UPDATE` SQL queries. For
  unparsed Git-only commits, non-numeric `commit_id` references are safely sanitized to integer `0` in
  `CommitFeatures` before querying the mailing list daemon.
- **Limits**: the commit's patch is read with the 8 MiB cap of `.../diff/{hash}` (a larger one gets no patch
  id, since a truncated patch would hash differently), and every Git step has a 30 s timeout.
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
Retrieve an anchored, bi-directional chronological commit timeline relative to a release tag or version
commit. Resolves the version's anchor commit (e.g. `v3.0` -> commit 854 "Linux 3.0") and navigates forward
into newer commits (`direction="future"`), backward into older commits (`direction="past"`), or centered
around the anchor (`direction="around"`). Uses primary key indexed ordering on `commit_id` for instantaneous
(<0.002s) execution. Every query stays inside the repository's Git network (see the list above), so two
repositories of one database never interleave. Future-dated commits (`author_date > now`) are excluded from
default timelines to omit corrupt timestamps. If `version_name` names an unparsed revision or branch outside
`m_commit`, the timeline reads commits directly from Git (skipping any with timestamps beyond the current
date).
- **Parameters**:
  - `version_name` (path): Kernel version string, branch, or commit SHA.
  - `limit` (query): Maximum commits to retrieve (1-250, default 100; the same bound in every direction).
  - `direction` (query): Seek direction: `"past"` (commits leading up to or before anchor, default),
    `"future"` (commits after anchor), or `"around"` (centered around anchor commit).
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
Structured unified diff of one commit against its **first parent** (root commits diff against the empty tree;
merges show what the merge brought into the first-parent line). Read straight from Git (`git diff -M
-U<context> <parent> <commit>`), so it works for any commit in the repository, parsed or not.
- **Parameters**:
  - `commit_hash` (path): 4–40 hex digits (anything else → `400`; unknown commit → `404`).
  - `context` (query): Context lines per hunk (default 3, 0–20).
- **Limits**: Git output is streamed and cut at 8 MiB; at most 6000 rows are returned. Past the row cap the
  remaining rows are only counted, so `additions`/`deletions` stay exact and affected files carry `truncated:
  true` (their remaining hunks are omitted). Rows longer than 400 characters are clipped.
- **Rows**: `[kind, old_line, new_line, text]` with `kind` `"+"`, `"-"` or `" "`; the side a row does not
  exist on is `null`.
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
Line-by-line `git blame` of a file at a revision (`git blame --porcelain`: each commit's author and summary
are sent by Git once, on its first line, and attached to every line of it). Output lines are split strictly by
newline (`\n`) rather than universal line breaks (`splitlines()`), ensuring in-file control characters (e.g.
form feeds `\f` / `\x0c`) preserve exact line alignment.
- **Parameters**:
  - `version_name` (path): Revision (validated by `safe_rev`, §2.1.1).
  - `path` (path): Jailed repository path (e.g. `"init/main.c"`); traversal answers 400, a failed blame 404, a
    blame running over 12 s (30 s with `copy`) 504.
  - `move` (query, `1`): follow lines moved within the file (`-M`).
  - `copy` (query, `1`): follow lines copied or moved from other files of the same commit (`-C`, one level;
    compares against every file the commit changed, so the timeout is 30 s and `slow: true` reports a blame
    that took more than 10 s).
  - `ws` (query, `1`): ignore whitespace changes (`-w`).
- **Per-line fields** beyond the commit, author, date and subject (from the porcelain output,
  `GitService.parse_blame_porcelain`): `orig_line` (the line's number in the blamed commit's version of the
  file, from the header `<sha> <orig_line> <final_line> [<n>]`), `source_path` (the file's name in that
  commit: it differs when `-M`/`-C` followed a move or copy), `previous_sha` / `previous_path` (from `previous
  <sha> <path>`: the parent of the blamed commit and the file's name there, what *Blame before this change*
  opens, §5.21; absent for a root commit).
- **Response**:
```json
{
  "version": "v3.0",
  "vid": 1,
  "path": "init/main.c",
  "options": {"move": false, "copy": false, "ignore_ws": false},
  "slow": false,
  "elapsed_ms": 784,
  "lines": [
    {
      "line": 17,
      "orig_line": 17,
      "commit_hash": "9b5609fd773e6ac0b1d6d6e1bf68f32cca64e06b",
      "author_name": "Ingo Molnar",
      "author_email": "mingo@elte.hu",
      "date": 1202978469,
      "subject": "stackprotector: include files",
      "source_path": "init/main.c",
      "previous_sha": "72370f2a5b227bd3817593a6b15ea3f53f51dfcb",
      "previous_path": "init/main.c"
    }
  ]
}
```
Clients cache answers per revision stamp, options and path (`blame2_<v@stamp>_<letters>_<path>`).

#### `GET /api/commits/{version_name}/history/{path:path}`
Retrieve chronological Git commit history touching a specific file up to a given version/tag/commit, along
with immediate `last_changed` metadata.
- **In-Memory LRU Caching**: Leverages a 1024-entry thread-safe LRU cache (`_cached_git_file_history`) on
  `(rev, rel_path, limit)`, providing instantaneous (<0.05ms) sub-millisecond responses on repeated queries
  without spawning subprocesses.
- **Resilience Guard**: Wrapped with an 8-second execution timeout guard returning `HTTP 504 Gateway Timeout`
  on prolonged Git operations. Caller services (`FilesystemService`) safely fallback to `None` if unindexed or
  missing, preventing file viewer crashes.
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

#### `POST /api/version/{version_name}/patch/series`
A multi-file patch series in `git format-patch --cover-letter --base` form (P3,
`webapp/backend/services/patch_series.py`).
- **Body**: `{base_commit, author: {name, email}, to: [], cc: [], version, cover: {subject, body} | null,
  patches: [{subject, body, files: [{path, original_blob, modified_content}]}]}` (≤ 50 patches, ≤ 5 MB of file
  text; each path jailed as in §2.1 and in exactly one patch; addresses checked RFC-5322-lite).
  `original_blob` is the blob SHA the browser computed for the text it edited (empty for a new file): it must
  still be the blob at the base commit, else **409** (*changed at the base commit: reload it*).
- **Output**: one message per patch — `From 000…000 Mon Sep 17 00:00:00 2001`, `From:`, `Date:`, `Subject:
  [PATCH n/N]` (`[PATCH vK n/N]`, zero-padded past 9; `[PATCH]` alone for one patch), `To:`/`Cc:`, the
  description with a `Signed-off-by:` added when missing, `---`, the diffstat (`create mode` lines for new
  files), and diffs with `diff --git`, `index <old7>..<new7> <mode>` (`new file mode 100644`), 3 lines of
  context and `\ No newline at end of file`; new blob ids are computed like `git hash-object` without writing.
  With two patches or more, `0000-cover-letter.patch` carries `[PATCH 0/N]`, the blurb, the shortlog and the
  total diffstat; `base-commit: <sha>` ends the first message. Signature `-- ` / `2.43.0`.
- **Response**: `{patches: [{name, subject, text}], mbox, diffstat, base, check: {applies, patches: [{name,
  status}]}}` — `check` applies the generated series to the base in memory (P1 applier). `git am` accepts the
  mbox (tested on a scratch repository).

#### `POST /api/version/{version_name}/patch/format`
Generate an authentic RFC-2822 standard email formatted patch file (`git format-patch`) from in-browser
virtual editor modifications.
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
- **Behavior**: The diffstat (`N +-`, insertions/deletions) is computed from the unified diff; `diff` is empty
  and no `diff --git` header is emitted when the contents are identical.
- **Response**:
```json
{
  "diff": "--- a/fs/ext4/super.c\n+++ b/fs/ext4/super.c\n@@ -1,2 +1,2 @@\n int a = 1;\n-int b = 2;\n+int b = 3;\n",
  "formatted_patch": "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\nFrom: Kernel Developer <dev@kernel.org>\nSubject: [PATCH] ext4: update b value\n\n--- a/fs/ext4/super.c\n+++ b/fs/ext4/super.c\n@@ -1,2 +1,2 @@\n int a = 1;\n-int b = 2;\n+int b = 3;\n-- \nKernelInfo-Parser\n"
}
```

---

#### `GET /api/commits/{version_name}/released/{commit_hash}` · `POST /api/commits/{version_name}/released`
First tag and release containing a commit (H3, release index §2.17). `commit_hash`: 7–40 hex characters (else
400).
- **Response**: `{"commit": "<40 hex>", "first_tag": "v3.1-rc6", "first_release": "v3.1", "first_stable":
  null, "released": true, "indexed": true}`, or `{"released": false, "branches": ["origin/master", …]}` sent
  with `Cache-Control: no-store` (valid until tags are fetched). `202` with progress while the release index
  is first built (§2.15).
- **Batched form**: `POST` with `{"hashes": [...]}` (1–200) answers `{"results": {hash: {sha, first_tag,
  first_release, first_stable, released}}, "index": {"state": "done"}}` (`no-store`).

#### `GET /api/commits/{version_name}/relations/{commit_hash}` · `POST /api/commits/{version_name}/relations`
What a commit fixes and what fixed it, reverts, backports, `Cc: stable` and mail links (H1, H2; §2.16:
`m_commit_relation` for ingested commits, `source: "db"`, else the commit relation index, `source: "index"`),
each related commit enriched with `subject`, `author`, `email`, `date` (one `git log --no-walk`), `released`
(its first release, §2.17) and `commit_id` when the parser ingested it (one `SELECT` on `m_commit`).
- **Response**: `{commit, subject, date, released, fixes: [{sha|null, prefix, subject, resolved, released,
  …}], fixed_by: [...], fixed_by_total?, reverts, reverted_by, upstream, backports, stable_cc: ["3.0+"],
  links: [mid], source: "db"|"index", index: {state, database, built_at, commits?, relations?, links?},
  refreshing}`. An unresolved `Fixes:` prefix keeps its quoted subject (`sha: null`). `202` while the SQLite
  index is first built for a commit the database does not hold; `no-store` while it refreshes.
- **Batched form** (timeline chips): `POST` with `{"hashes": [...]}` (1–200) answers counts `{"results":
  {hash: {fixes, fixed_by, reverted_by, backports, stable_cc}}}`: ingested commits counted in the database
  (`db_counts()`), the others from the SQLite index (202 only when it is building and the database answered
  none).

#### `GET /api/commits/{version_name}/query`
Structured commit query run with `git log` (H8, `webapp/backend/services/commit_query.py`), newest first, from
the revision (or a range / release cycle). Every argument is built on the server from validated values: paths
through the text-search pathspec validator (§2.1.3, glob magic added on the server); names, trailer values and
text length-capped (100), single-line and escaped for POSIX ERE (they match as text, after `--author=` /
`--committer=` / `--grep=`; `-i` throughout); dates `YYYY-MM-DD`; revisions through `safe_rev`, resolved to
SHAs, ranges joined on the server.
- **Parameters**: `path` (repeatable, ≤ 8: `fs/ext4`, `*.h`, `!drivers/`), `author`, `committer`, `role`
  (repeatable `role:value`; roles `reviewer`, `acked`, `tested`, `reported`, `suggested`, `signed`, `codev`,
  `cc`, `fixes`, `link` → `--grep=^<Trailer>:.*<value>`, combined with the text by `--all-match`), `since`,
  `until`, `range` (`A..B`), `release` (that release's cycle, `range_of`, §2.17), `merges` (`only` / `none`),
  `section` (a MAINTAINERS section id or name: its `F:` patterns as paths, `X:` as exclusions), `text`,
  `page`, `limit` (≤ 200).
- **Response**: the shape of `/list` — `{version, page, limit, total: null, has_more, range, commits:
  [{commit_hash, author_name, author_email, author_date, subject, is_merge, commit_id?}]}`; counting is not
  offered (it would cost a second walk). `has_more` comes from one extra row. 400 for invalid values, 504
  after 45 s.

### 4.7. Visual Modeling & Analysis Tools API

#### `GET /api/tools/{version_name}/implementations?struct=&field=` · `?function=`
Ops implementations (N1, `webapp/backend/services/ops_service.py`): the functions struct initializers assign
to function-pointer fields, which the call graph cannot see. From the revision's source index (§2.19), so any
revision works and every struct and function is answered from memory once it is assembled (202 with progress
before, §2.15): `struct <S> <name>[…] [attrs] = { … }` initializers with their designated `.<field> =
<function>` / `= &<function>` entries — arrays of structs, and one nested level as `outer.inner`
(`platform_driver` → `driver.pm`); a value is kept when it resolves to a function like a call made in that
file would (its own static first), so `THIS_MODULE`, other tables and calls `= f(…)` are not implementations;
positional initializers are counted (`positional_skipped`), not resolved. Tables generated by macros
(`DEFINE_SIMPLE_ATTRIBUTE`, `*_FOPS`) are not seen.
- **`struct` (+ `field`)**: `{struct, tables, files, positional_skipped, fields: [{field, count}], field?,
  implementations?: [{function, path, line, table: {name, path, line}}]}`. v3.0 `file_operations`: 1,653
  tables in 1,024 files (29 ms once the index is in memory), `read` has 1,372 implementations, `do_sync_read`
  among them from `ext4_file_operations` (`fs/ext4/file.c:255`).
- **`function`**: the tables using it — `{function, uses: [{struct, field, table, table_line, path, line}]}`.
  v3.0 `ext4_file_write` → `file_operations.aio_write` of `ext4_file_operations`.

#### `POST /api/tools/{version_name}/checkpatch`
`scripts/checkpatch.pl` of a trusted release tag on a patch (P4; rules in §2.18). `version_name` only selects
the repository.
- **Body**: `{patch: "<git format-patch text>"}` (≤ 2 MB).
- **Response**: `{enabled: false, config_key, reason}` while off; else `{enabled: true, script_ref, results:
  [{level: "ERROR"|"WARNING"|"CHECK", type, message, file, line}], counts: {ERROR, WARNING, CHECK}, raw,
  exit_code, timed_out, elapsed_ms}` — `results` are the `--terse` lines (`line` is the patch line), other
  output is in `raw`. A patch with trailing whitespace and a long line reports `ERROR:TRAILING_WHITESPACE` and
  `WARNING:LONG_LINE*` at their patch lines (~0.15 s with v7.3's script, also under `bwrap`).

#### `GET /api/tools/pahole/compare?a=&b=&struct=` · `GET /api/tools/{version_name}/pahole/{struct_name}/history?include_rc=`
Struct layout over time (N3, `webapp/backend/services/pahole_compare.py`), on top of the Pahole layout below.
- **`compare`**: both layouts (both revisions through `safe_rev`), members matched by name — `same`, `added`,
  `removed`, `moved` (offset), `resized`, `type_changed`; a removed and an added member of the same type at
  the same offset are flagged `renamed_to` / `renamed_from` (a guess). `{struct, a: {total_size, holes_count,
  padding_bytes, cache_lines_used, is_estimate, version, file_path, line_s}, b: {…}, members: [{name, status,
  a: {offset, size, type} | null, b}], delta: {total_size, holes_count, padding_bytes, cache_lines_used},
  counts, is_estimate}` (an estimate on either side marks the result). Cached per (commit a, commit b,
  struct). v3.0 → v4.0 `list_head`: 2 members `same`, 16 bytes both.
- **`history`**: the releases where the definition was introduced or changed (the symbol history's timeline,
  §5.17; cosmetic edits excluded), each with its layout summary — `{struct, version, path, stops: [{version,
  available, total_size, holes_count, padding_bytes, cache_lines_used, is_estimate} | {version, available:
  false, reason}]}` (a release whose blobs were never indexed is `available: false`). A background job (§2.15;
  ~5 s per stop), cached on disk per (commit, struct).
- Layouts follow the parser's view: members under `#ifdef CONFIG_…` are not configuration-dependent here.

#### `GET /api/tools/{version_name}/pahole/{struct_name}`
Pahole-style member offsets, alignment holes and 64-byte cachelines of a C struct or union, verified via
compiler AST attributes and source extraction. `404` when the revision defines no struct or union of that
name.
- **Definition**: the `C_structdecl` / `C_uniondecl` definitions of the name in the revision's tree
  (`SymbolService._scope`), ranked: definitions with members before forward declarations, then kernel headers
  before trees that carry their own copy for host programs (`tools/`, `scripts/`, `samples/`,
  `Documentation/`), then `.h` files, then `definition_rank` (§2.7). `struct list_head` at v3.0 is
  `include/linux/types.h:222`, 16 bytes.
- **Sizes & Offsets**: member sizes and bit offsets are read from compiler AST attributes (`m_ast_attribute`:
  `size_bytes`, `offset_bits`, `bitfield_width`), which are extracted directly by Libclang during AST parsing.
  For members where compiler attributes are absent, size falls back to the AST category (`_EXACT_SIZES` and
  `_GUESSED_SIZES`). Member declarator types and array subscripts (e.g. `unsigned char [16]` for `id[16]`) are
  extracted directly from the struct definition in Git source. `is_estimate` is true only when any member
  lacks compiler attributes and uses category estimation (`struct adb_ids` is exact 20 bytes; `is_estimate:
  false`).
- **Parameters**:
  - `version_name` (path): Revision.
  - `struct_name` (path): C struct identifier (e.g. `"task_struct"`; a `struct ` / `union ` prefix is
    stripped).
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

#### `GET /api/tools/{version_name}/callpath?from=&to=&max_depth=6&k=5&exclude=&indirect=0&arch=`
How one function reaches another (N2, `webapp/backend/services/callpath_service.py`): a bidirectional
breadth-first search over the revision's source index (§2.19, in memory) — forward from every node named
`from` along callees, backward from every node named `to` along callers, always expanding the smaller frontier
and stopping at the first level where they meet; when one side runs dry the other continues alone. Up to `k`
(≤ 20) shortest simple chains, each hop with the call site of the call to the next one. Calls made in other
architectures' trees (`arch` — default the version's default ARCH, as for definitions, §2.7), in the other
word size's files of its own tree and in user-space trees (`tools/`, `scripts/`, …) are not followed. With
`indirect=1` a call through a struct member also reaches the functions ops tables assign to that field (by
field name, approximate; such hops carry `via`). Limits: `max_depth` (≤ 10), 50,000 expanded functions, 20 s;
`exclude` lists hubs not to go through (`printk,kmalloc`). Names must be C identifiers (400). No database
query.
- **Response** (202 with progress while the revision's index is assembled, §2.15; then milliseconds):
  `{version, from, to, paths: [[{function, file?, path, line, via?}]], explored, budget_exhausted, timed_out,
  depth, found: {from, to}, arch, indirect, elapsed_ms}` — `path`/`line` is the call site in that function
  (null on the last hop), `file` is set on a static function or macro (its own file), `via` on a hop through
  an ops table, `found` counts the nodes of each name. v3.0: `start_kernel → kernel_thread` gives
  `start_kernel (init/main.c:629) → rest_init[init/main.c] (init/main.c:357) → kernel_thread` in ~90 ms (the
  former database search took 25–40 s); with `indirect=1`, `sys_write → vfs_write → do_sync_write ⇢
  ext4_file_write` (`via: file_operations.aio_write`). An unreachable pair returns no paths with the explored
  count.

#### `GET /api/tools/{version_name}/callgraph/{function_name}`
Retrieve bidirectional caller and callee execution flow. Consolidates multiple call sites of the same function
with call frequencies and coordinate tracking.
- **Function**: the in-tree definitions of the name that are functions
  (`C_functionproto`/`C_functionprotodecl` or Rust `fn`, not macros), scoped to the revision's tree like the
  symbol endpoints; the one shown (`file_path`, lines, callees) has a body (not a prototype, and an
  `ExternalDef` before a header inline stub, a C function before a Rust one of the same name, a Rust function
  C calls — `#[no_mangle]` — before its C prototype), ranked by `definition_rank` (§2.7).
- **Callers** are the calls to any in-tree declaration that is the function itself (a call binds to the
  declaration visible where it is made, usually a header prototype; at most 8 ASTs, `m_ast.name` must be the
  function's name), found in this revision's blobs and named after the AST of the tag containing the call;
  Rust calls through bindgen (`bindings::foo(..)`, the `Rust_BindingRef` AST of the name, §4.3) are callers
  too. **Callees** are the calls inside the function's lines, each with the files defining the callee in this
  revision (`file_path` is null when none does); a Rust function's calls of C functions name the callee only,
  so their files are those of the in-tree function definitions of that name. At most `MAX_CALL_SITES` (2000)
  call sites are kept per direction (`truncated: true` beyond). Reference lookup goes through every indexed
  blob holding the targets (`rows_of_targets_sql`), so a widely used function takes seconds (`schedule` at
  v3.0: 542 callers, ~25 s); the frontend allows 60 s.
- **Parameters**:
  - `version_name` (path): Revision.
  - `function_name` (path): C (or Rust) function identifier.
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
- **Behavior**: See §2.8. The whole trees are compared; scoping to a directory is what the rename search below
  offers. `source` is `"git"` when both trees came from Git (Path C) and `"db"` for the database fallback.
  `fid`, `fid_old`, `fid_new` are indexed `blob_id`s, or `0` when the blob was never parsed. `changes` holds
  one row per change, sorted by path (8,790 rows at v3.0→v3.1, ~1.2 MiB of JSON); `symbols_added` /
  `symbols_removed` are set for the first 50 modified source files (§2.8 Level 2). Renamed entries carry
  `similarity` (percent, 100 = identical content). `renames` describes the rename pairing: `similarity` (the
  threshold, 50), `source` (`"git"` for Git's detection, `"exact"` when only identical content was paired),
  `exhaustive` (every deleted/added pair was compared) and `limit_needed` (the rename limit Git asked for when
  it was not, else 0).
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
Paths present at both revisions with the same content: the files behind `summary.unchanged_count` of the
revision diff, which does not list them (28,828 paths, ~1 MiB of JSON at v3.0→v3.1).
- **Parameters**: `version_a`, `version_b` (query, required).
- **Behavior**: `DiffService.get_unchanged_files()` reads both trees exactly like the revision diff
  (`_fetch_trees`: Git trees, or the database for both sides) and returns the sorted paths whose content key
  is equal on both sides (~0.7 s cold at v3.0→v3.1, then served from the tree LRU cache).
- **Response**:
```json
{ "v1": "v3.0", "v2": "v3.1", "source": "git", "count": 28828, "files": ["COPYING", "Documentation/.gitignore"] }
```

#### `GET /api/tools/diff/renames`
Exhaustive rename search: compares every deleted with every added file between two revisions (Git trees only),
optionally under one directory.
- **Parameters**:
  - `version_a` (query, required): Base revision.
  - `version_b` (query, required): Target revision.
  - `path` (query): Directory (or file) the search is limited to on both sides (validated with
    `is_safe_rel_path`, passed as a `:(literal)` pathspec); empty = whole tree.
  - `similarity` (query, 1–100, default 50): Minimum content similarity in percent.
- **Behavior**: `DiffService.search_renames()` counts the added and deleted paths under `path` and the
  candidate pairs (`added × deleted` after identical copies, which Git pairs without comparing). Above
  `MAX_RENAME_SEARCH_PAIRS` (60,000,000) nothing runs and `reason` is `"too_many"`, so the caller narrows
  `path`. Otherwise `git diff-tree -M<similarity>% -l<max(added, deleted)+1>` compares every pair (110 s
  timeout; `reason: "failed"` when Git fails). `complete` is true when every pair was compared. Only pairs of
  a path deleted and a path added under `path` are returned, so a rename whose other end lies outside `path`
  is not found by a scoped search. Timings: v3.0→v3.1 whole tree (811,530 pairs) ~0.9 s; v3.0→v4.0
  `drivers/net` (1,857,397 pairs, 804 renames at 30%) ~2.6 s; v3.0→v4.0 whole tree is 202 million pairs (~60 s
  for Git), so it is refused. Low thresholds pair unrelated files that share boilerplate (license headers).
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
  - `similarity` (query, 1–100, default 50): Rename threshold of the diff, so a pair found below 50% (rename
    search) shows as one rename diff instead of a deletion and an addition.
- **Behavior**: Both revisions are resolved with `git rev-parse --verify` (option-like names are rejected) and
  diffed with `git diff -M<similarity>%`; both paths are passed as `:(literal)` pathspecs. Output above 512
  KiB is truncated (`truncated: true`).
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
Changed lines between **any two files at any two revisions** (the code view's **⇄ Diff**, §5.21): the same
path across releases, a renamed file, or two unrelated files.
- **Parameters**:
  - `version_a`, `path_a` (query, required): Base revision and repository-relative path.
  - `version_b`, `path_b` (query, required): Target revision and path.
- **Behavior**: `DiffService.get_blob_diff()` validates both paths (`is_safe_rel_path`; else `400`), resolves
  both revisions (`resolve_commit_sha`; unknown or option-like → `404`) and looks each path up with `git
  ls-tree -z <sha> -- :(literal)<path>` (absent → `404`, a directory → `400`). Equal blob SHAs answer
  `identical: true` with no hunks. Otherwise `git diff --no-color --no-ext-diff -U0 <blob_a> <blob_b>` runs on
  the two object SHAs (nothing user-supplied reaches the command line); stdout is streamed and cut at 8 MiB,
  at most 200,000 rows are kept and rows are **not** clipped (`parse_unified_diff(..., max_chars=None)`), so
  `truncated: true` means the hunks stop early. Only changed lines are returned: the client rebuilds the full
  file from the side it already shows (§5.21). `binary` is set when Git reports binary content.
- **Rows**: `[kind, old_line, new_line, text]` as in the commit diff (`"+"` / `"-"`; the other side is
  `null`). Text keeps a trailing `\r` of CRLF files.
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
Compare Kconfig source files and option definitions between two revisions (release tags, branches, or commit
SHAs).
- **Parameters**:
  - `v1` (path): Base revision (e.g. `"v3.0"`).
  - `v2` (path): Target revision (e.g. `"v3.1"`).
- **Behavior**: See §2.8 Level 3. Both `files` and `symbols` come from Git and are complete; `unindexed_files`
  is always empty (kept for compatibility). Each definition in `before`/`after` carries `file`, `line`, `type`
  (1 bool, 2 tristate, 3 string, 4 hex, 5 int), `prompt` (text without its condition), `def_val` (first
  default) and `facts` (the property record of the symbol history, `GET
  /api/kconfig/{version_name}/symbol/{symbol_name}/history`); a symbol with one definition on each side also
  gets `changes` (`[{field, before, after}]` / `[{field, added, removed, before, after}]`). Measured: v3.0 ->
  v3.1 takes ~2 s (174 modified files), v6.17 -> v6.18 ~3 s.
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

The Mailing List subsystem connects the web application to the public-inbox / LKML archive (via direct storage
read or the lightweight remote micro-daemon over SSH tunnel), providing thread inspection, unified patch diff
parsing, full-text search, and offline IndexedDB fallback.

#### `GET /api/mail/status`
Check daemon status, reachability, active port, and the base URL of the self-hosted public-inbox mirror
(`lore.kernelinfo.org`, a clone of `lore.kernel.org`).
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
Retrieve full email details including RFC-822 headers, body text, raw payload, and pre-parsed interactive
patch diffs.
- **Parameters**:
  - `mid` (query): RFC-822 Message-ID (e.g. `"<20210515120000.1234-1-dev@kernel.org>"` or
    `"20210515120000.1234-1-dev@kernel.org"`). Automatically supports URL decoding, angle-bracket removal,
    space-to-plus recovery (`+` converted to space in query strings), and direct `xref3` binary OID fallback.
  The service also accepts a sequential article number in `mid`.
- **In-Memory LRU Cache**: 512 entries cached in-memory in `MailingListService._email_cache`. Stale or error
  entries are automatically bypassed.
- **Client Cache**: `api.getMailMessage` operates `networkFirst: true`. IndexedDB never caches failed or empty
  email responses.
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
  "commit_message": "Commit message body text...\n\nSigned-off-by: Developer Name <dev@kernel.org>",
  "commentary": "v2: addressed reviewer comments",
  "diffstat": "fs/ext4/extents.c | 17 +++++++++++++----",
  "base_commit": "e826bf7a9288",
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
- **Bounded page**: a thread is unbounded in principle (the reference archive's largest holds 12,342 messages,
  each carrying a compressed overview blob), so the archive store caps the messages unpacked per request at
  `MAX_THREAD_MESSAGES` (1000); the endpoint answers that first page. `total` is the thread's real message
  count from an indexed count, and `truncated` states whether messages beyond the returned page remain — so a
  client must read `total` as the thread size and `messages` as a page of it, not as the whole thread.
- **In-Memory LRU Cache**: 128 entries cached in `MailingListService._thread_cache`, each reused for
  `LIVE_CACHE_TTL` (60 s) only: the archive is fetched continuously and a thread gains replies.
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
List every revision (`v1`, `v2`, ...) of the patch series a thread belongs to. Revisions are normally posted
as separate threads sharing one base subject, so the series is reconstructed by normalizing the requested
thread's subject (`normalize_series_subject`: reply prefixes and leading `[PATCH v3 2/5]` tags removed) and
matching that base subject against the archive search index. At most `MAX_VERSION_THREADS` (8) sibling threads
are expanded.
- **Parameters**:
  - `tid` (query): Thread integer identifier.
  - `mid` (query): Optional Message-ID, used both to resolve `tid` and to pin `current_version` when a single
    thread carries messages of more than one revision.
- **Grouping**: every message of each thread is folded into the entry of its own revision (`patch_version`); a
  reply carrying no patch tag inherits the revision of the earliest series message in its thread. `total`
  counts all messages of the revision, `patches` only the patches, cover letters and pull requests.
- **In-Memory LRU Cache**: keyed by `tid:mid`, bounded by the thread cache size (128 entries) in
  `MailingListService._version_cache`, reused for `LIVE_CACHE_TTL` (60 s): a new revision of the series is a
  new thread.
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

#### `GET /api/mail/merged`
The commits a patch or a whole series became, with their first release (P2,
`webapp/backend/services/merged_service.py`).
- **Parameters**: `mid` (a message-id, brackets and URL-encoding removed) and/or `tid` (the thread: the whole
  series); one is required (400).
- **Sources**: `m_commit_email` rows with relation Patch, CoverLetter or SiblingPatch for the message or
  thread, joined to `m_commit` of the active repository's network (`SELECT` only) — used when the table has
  its `commit_email_mid_idx` / `commit_email_tid_idx` indexes (created by the parser at start, core/API.md
  §8.2; checked through `information_schema.STATISTICS`). Otherwise, or with no row, commits whose `Link:` /
  `Message-Id:` trailer names the message (§2.16: `m_commit_link` once the database covers the stored commits,
  without building the SQLite index; else the commit relation index) (for a thread, each of its messages, at
  most 300). Each commit gets its first release (§2.17).
- **Response**: `{mid, tid, source: "db" | "trailers", commits: [{hash, subject, relation: "patch" | "cover" |
  "sibling", match_tier, patch_version, patch_idx, patch_total, message_id, released}], series: {total,
  merged, first_release}}` — `first_release` is the earliest release containing one of the commits.

#### `POST /api/mail/apply` · `GET /api/mail/apply/{apply_id}/file?path=`
Apply archive patches to any revision in memory (P1, `webapp/backend/services/patch_apply.py`): no worktree,
no Git object, no database row is written.
- **Body**: `{revision, message_ids: [...]}` in series order (≤ 50 messages, ≤ 5 MB of raw mail, ≤ 500 files).
  Each message's diff part is taken from its decoded RFC-822 payload (`email` package: quoted-printable,
  base64, charsets, patch attachments); hunks are read by their line counts, so the `-- ` signature and
  trailing text are ignored. A cover letter (no diff) is listed as `no-diff`; its `base-commit:` (git
  format-patch `--base`) is reported.
- **Applier** (`git am` without `-3`, no fuzz): a hunk's pre-image must match exactly at its new position or,
  searching outward, at the nearest line (an *offset*); a hunk starting at line 0/1 must match at the
  beginning and one without trailing context at the end (git `apply.c` `match_beginning` / `match_end`). New,
  deleted and renamed files are handled; binary patches are `unsupported`. A patch with a failing file changes
  nothing (as `git am` stops); later patches still run. Outcomes match `git apply --check` (tests on fixtures;
  v5.10, v6.6 and v7.0 checked on a real 2-patch series).
- **Response**: `{apply_id, base: {revision, sha}, base_commit: {commit, resolved, sha} | null, message_ids,
  patches: [{message_id, subject, author, status: applied|failed|no-diff, offset, files: [{path, old_path,
  change, status: applied|offset|created|deleted|renamed|failed|unsupported, reason, hunks: [{status, line,
  offset} | {status: "failed", expected_line, nearest: {line, matched, of}, context}]}]}], summary: {patches,
  applied, offset, failed, files}}`. `apply_id` is sha256 of (revision sha, message ids): the same request
  rebuilds the same id.
- **`file`**: the patched file against its base as -U0 hunks in the shape of `GET /api/tools/diff/blobs`
  (`[kind, old line, new line, text]` rows, plus `additions`, `deletions`, `identical`; `text` for a created
  file). Results live in a memory LRU of 16 (`RepoScopedCache`); an evicted id answers **410** and the client
  applies again.

#### `GET /api/mail/search`
Full-text search across public-inbox mail archive with Boolean and term prefixes.
- **Parameters**:
  - `q` (query): Search terms or prefixes (e.g. `s:ext4`, `f:torvalds`, `m:20210515`).
  - `page` (query): 1-indexed page number (default 1).
  - `limit` (query): Items per page (1-100, default 50).
  - `filter` (query): Filtering preset: `"all"`, `"patch"`, `"cover"`, `"pull"`.
- **Future-Dated Messages**: an empty `q` is the chronological browse listing (`ORDER BY ds DESC`), where a
  message carrying a `Date:` header in the future — a skewed sender clock or a forged header — would sort
  ahead of every real email. Those rows are therefore excluded from the empty-query listing by the archive
  store (`PublicInboxLocalStore.search_emails`, `ds <= now + FUTURE_DATE_SKEW_SECONDS`, a one-hour tolerance),
  and `MailingListService._drop_future_dated` re-applies the same horizon to the returned page so the
  invariant also holds against a micro-daemon predating it (lowering `total` by what the page dropped). **A
  non-empty `q` is never filtered**: a search that matches a future-dated message still returns it, and the
  filter presets apply unchanged in both cases.
- **Reported `total`**: where the Xapian bindings are available, `total` is Xapian's estimated match count
  (`get_matches_estimated()`) for the whole archive. Otherwise it is a lower bound — the offset plus the
  returned page, plus one when more remain — and the response always carries `has_more`; the frontend roster
  also treats a page that adds no new message as the end of the results (§5.22).
- **Paging cost is flat**: `limit` and `offset` are clamped by the archive store (`MAX_SEARCH_LIMIT` 200,
  `MAX_SEARCH_OFFSET` 5,000,000) and the offset is applied by Xapian or by SQL `LIMIT ? OFFSET ?` rather than
  by slicing a fetched list, so a deep page costs what the first one does. Without Xapian, the
  subject-substring fallback streams a bounded scan (`MAX_SEARCH_SCAN` 20,000 rows) and keeps only the
  requested page.
- **In-Memory LRU Cache**: 64 entries cached in `MailingListService._search_cache`, reused for
  `LIVE_CACHE_TTL` (60 s), so the browse listing shows new mail within a minute. The client asks network-first
  and keeps only the browse listing (no query) in IndexedDB for offline use (§5.1).
- **Lore links**: `lore_url` of a search carries the query URL-encoded (`quote_plus`), as does a commit's
  fallback link built from its subject.
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

### 4.9. Kernel API Catalogs (`webapp/backend/routers/kapi.py`, `webapp/backend/services/kapi_service.py`)
Catalogs of any revision (git-only ones included), assembled from the Kernel API records of its blobs
(`parser/kapi_facts.py`, `kapi_service.revision_records()`), never by searching the whole tree:
- **Records per blob** (`kapi_facts.extract(text)`, pure, shared by the parser and the webapp): the macro
  calls of one `.c`/`.h`/`.S` file — exports (`x`), syscall definitions (`s`), parameters (`p`), module
  metadata (`m`), tracepoints with the file's `TRACE_SYSTEM` (`e`, `ts`), device tables with their initializer
  entries, the file's quoted includes and simple `#define`s (`v`, `vi`, `vd`). Lines are chosen with the line
  patterns the catalogs used with `git grep`, on the text with comments, strings and character literals
  blanked (`source_facts.mask()`), so commented-out macros are not taken (v5.8: 4 exports, 2 parameters, the
  metadata of 11 modules and one device table fewer than the grep-based catalogs, all inside comments);
  arguments are read from the original text (`invocation()` reads a macro call to its matching parenthesis,
  strings and comments respected; `split_args()` splits its arguments). A blob does not know its paths, so
  which files a catalog takes is decided at assembly (syscalls from `.c`, tracepoints from
  `include/trace/events/` and `*trace*.h`, parameters from `.c`/`.h` outside `tools/` and `scripts/`...).
  Binary files (`git grep -I`) and files over 4 MB have none.
- **Where records come from**: first the parser's `m_blob_facts` rows of family 2 (current `KAPI_FORMAT`, Step
  6.9, `core/API.md` §6), then a local SQLite store (`source-index/kapi-v<n>.sqlite3`,
  `kapi_service.kapi_store`, the source index's `FactStore`, same cap `webapp.source_index.max_mb`); the blobs
  neither has are extracted here once, in `webapp.source_index.workers` low-priority `spawn` processes
  (§2.19). A revision's records (only the files that have any, in tree order) stay in memory for the two most
  recently used commits, shared by all catalogs; concurrent catalogs of one commit build them once.
- **Catalogs**: each is built once per (commit, ARCH) as a background job (§2.15; 202 with progress) and
  cached on disk (§2.14; `CATALOG_FORMAT` 2 and `KAPI_FORMAT` in the key). Module ownership comes from the
  Kbuild object index (§4.4). Timings, records extracted locally from scratch (2 workers) then each catalog:
  v3.0 9 s, then syscalls 0.01 s, exports 0.03 s, parameters 0.08 s, tracepoints 0.01 s, modules 0.2 s,
  devices 1.2 s; v5.8 18.5 s, then at most 2.3 s (devices) — the grep-based catalogs took 15.5 s (v3.0) and 47
  s (v5.8) for the six.
- **`GET /api/kapi/diff?a=&b=&arch=&kinds=`** (A6, `kapi_diff_service.py`): the Kernel API changelog between
  two revisions (both through `safe_rev`): for each kind (`syscalls`, `exports`, `params`, `tracepoints`,
  `modules`; default all) `{added, removed, changed}` — keys are syscall name/ABI, export symbol, parameter
  full name, tracepoint name, module name; *changed* lists `{key, field, from, to}` (number, entry, arguments;
  export variant and namespace; parameter type and permissions; module options, license, object) or, for
  tracepoints, `{fields_added, fields_removed, printk_changed}` — plus `counts`. Renames are not inferred
  (removed + added). 202 while a catalog of either side builds (the progress names it); cached per (commits,
  ARCH, kinds). v3.0 → v3.1 x86_64: 3 syscall changes (`getcpu` added, `nfsservctl` now `sys_ni_syscall`), 564
  export changes.
- **`GET /api/kapi/{v}/syscalls?arch=`** (A1): numbers from the ARCH's syscall table — `syscall_64.tbl` /
  `syscall_32.tbl` under `arch/x86/entry/syscalls/` (v4.2+) or `arch/x86/syscalls/` (v3.3+),
  `arch/<srcarch>/kernel/syscalls/syscall.tbl` or `arch/<srcarch>/tools/syscall*.tbl` (recent arches), else
  `#define __NR_<name> <n>` + `__SYSCALL(__NR_<name>, <entry>)` headers (`arch/x86/include/asm/unistd_64.h` at
  v3.0; `asm-generic/unistd.h`), and for i386 before v3.3 the `.long` table
  `arch/x86/kernel/syscall_table_32.S` (the index is the number); `x32`, `oabi`, `spu` ABIs are left out.
  Definitions are the `SYSCALL_DEFINEn` / `COMPAT_SYSCALL_DEFINEn` invocations of `*.c` with their arguments.
  Response `{arch, srcarch, sources: [{kind: tbl|header|table_S, path}], syscalls: [{nr, abi, name, entry,
  compat_entry, status: defined|ni_syscall|no_definition, definition: {path, line_s, macro, args: [{type,
  name}]}|null}], unnumbered: [...]}` (v3.0 x86_64: 309 numbers, 291 defined, 14 `sys_ni_syscall`).
- **`GET /api/kapi/{v}/exports?q=&variant=&ns=&offset=&limit=`** (A2): `EXPORT_SYMBOL`, `_GPL`, `_GPL_FUTURE`,
  `_NS`, `_NS_GPL`, `EXPORT_UNUSED_SYMBOL[_GPL]` of `*.c`/`*.S`/`*.h`: `{total, counts: {variant: n}, exports:
  [{symbol, variant, namespace, path, line}]}` (18,364 at v3.0). **`GET …/exports/{symbol}?arch=`**: every
  export site of the symbol with the object containing it (`{modname, target, expr}`: built in or a module,
  and when).
- **`GET /api/kapi/{v}/params?arch=`** (A3): `__setup`, `early_param`, `core_param`, `module_param*` (files of
  other architectures left out): `{kind: setup|early|core|module, name, full_name (<modname>.<param> for
  module parameters, KBUILD_MODNAME from the Kbuild index), takes_value, type, perm, handler, description
  (MODULE_PARM_DESC of the same file), path, line, snippet, doc: {line, tags}|null, undocumented}` plus
  `stale_docs` (documentation entries no definition matches — a heuristic, names can be built by macros) and
  `doc_path` (`Documentation/kernel-parameters.txt`, later `Documentation/admin-guide/kernel-parameters.txt`;
  tab-indented `name=` entries with `[TAGS]`). **`GET …/params/doc?line=`**: the documentation entry at a
  line.
- **`GET /api/kapi/{v}/tracepoints?q=`** (A4): `TRACE_EVENT*`, `DEFINE_EVENT*`, `DECLARE_EVENT_CLASS`,
  `DECLARE_TRACE` of `include/trace/events/` and `*trace*.h`: `{name, kind: event|class|raw, class, system
  (#define TRACE_SYSTEM), proto, args, fields: [{kind, type, name, size}] (TP_STRUCT__entry), printk, path,
  line}` — a `DEFINE_EVENT` inherits its class's prototype, fields and format (708 at v3.0). **`GET
  …/tracepoints/{name}`**: call sites `trace_<name>(` / `trace_<name>_rcuidle(` (at most 500; comments left
  out).
- **`GET /api/kapi/{v}/modules?arch=`** (A5): the objects of the Kbuild index whose condition can be `m`, with
  their Kconfig options (`required_symbols`, computed once per distinct condition), member sources and
  metadata from their files — `MODULE_LICENSE`, `MODULE_AUTHOR`, `MODULE_DESCRIPTION`, `MODULE_ALIAS`,
  `MODULE_FIRMWARE`, `MODULE_DEVICE_TABLE(bus, table)`, `module_init` / `module_exit` / `module_<bus>_driver`
  (5,891 at v3.0 x86_64). **`GET …/modules/{modname}?arch=`**: one module with its parameters (A3), exports
  (A2) and maintainers (`MaintainerService.match_maintainers` on its sources).
- **`GET /api/kapi/{v}/devices?bus=&q=&arch=`** (N4, `device_index_service.py`): the entries of every
  `MODULE_DEVICE_TABLE(bus, table)` of `*.c` — `{tables, total, entries: [{bus, id, table, path, line,
  text}]}` (`id` e.g. `pci 8086:10d3`, `pci class 0x010601/0xffffff`, `of fsl,imx6q-uart`, `acpi PNP0C0A`).
  Table initializers are read from the source; `PCI_DEVICE`, `PCI_VDEVICE`, `PCI_DEVICE_CLASS`,
  `PCI_DEVICE_SUB`, `USB_DEVICE*`, `USB_INTERFACE_INFO`, designated initializers (`.vendor`, `.class`,
  `.class_mask`, `.idVendor`, `.compatible`, `.id`, …), positional PCI initializers and string concatenation
  are understood; macro names resolve through `include/linux/pci_ids.h`, the driver file and its quoted
  includes (3 levels; the nearer include wins, the driver's own `#define`s over all, a `ChainMap` per file).
  Tables, entries, the driver's includes and `#define`s come from the Kernel API records (above); only the
  included headers and `pci_ids.h` are read, each once. Built once per commit as a background job (§2.15; 202
  with progress), cached on disk (§2.14, `INDEX_FORMAT` 3); v3.0: 1,865 tables, 13,035 entries (PCI 4,968, USB
  4,871, PCMCIA 935, I²C 719…; buses other than PCI, USB, OF and ACPI are listed but not decoded).
- **`POST /api/kapi/{v}/devices/match`** body `{text, arch}` (at most 64 KB, 2,000 lines): drivers for pasted
  hardware ids, one per line — `lspci -nn` (`[8086:10d3]`, class `[0200]`), `lsusb` (`ID 046d:c52b`),
  modaliases (`pci:v…d…sv…sd…bc…sc…i…`, `usb:v…p…`, `of:N…T…C<compatible>`, `acpi:<ID>:`), bare `vvvv:dddd`
  (tried as PCI and USB), OF compatibles or ACPI ids. PCI entries match with the kernel's
  `pci_match_one_device` semantics (`PCI_ANY_ID` fields, `class & class_mask`); `match_kind` is `exact`
  (vendor and device) or `class` (a class-only entry). Answer: `{lines: [{input, ids, matches: [{match_kind,
  bus, id, table, path, line, module, target, options, expr}]}], unmatched}` — the module, its `.ko` target,
  Kconfig options and condition come from the Kbuild object index of `arch` (§4.4). 503 while the index is
  still building (after a 60 s wait). v3.0: `8086:10d3` → `e1000e`, `8086:1c20` → `snd_hda_intel`.

### 4.10. Insights API (`webapp/backend/routers/insights.py`)
Reports over the history, served under `/api/insights/{version_name}/…`. Long ones run as background jobs and
answer `202` with progress until done (§2.15, `job_answer`). They answer for the request's repository
(`X-KI-Repo` / `?repo=`), whatever its project: release tags are that repository's `release_tag_names()`
(`webapp/API.md` §2.17; `vX.Y…` for the kernel, `man-pages-X.Y…` for the man-pages, any `<prefix>X.Y[.Z][-rcN]`
scheme), and a release's chain, previous release, -rc count and first-timer history stay within its tag
family. The MAINTAINERS-based reports (subsystem health, merge-window sections) and CVE reports have nothing to
read in a tree without MAINTAINERS / kernel commits.

#### `GET /api/insights/{version_name}/stable/series`
Releases that have stable tags: `{"series": [{"series": "v3.0", "stable_tags": 101, "last": "v3.0.101"}, …]}`,
newest first (`stable_service.stable_series`).

#### `GET /api/insights/{version_name}/stable/missing`
Missing stable backports of a series (H2, `webapp/backend/services/stable_service.py`): mainline commits first
released after the series and before its last stable release (`series_scope()`: the first mainline tag
containing the commit, with its tag commit's time at or before the last stable tag's; `REPORT_FORMAT` 2 —
format 1 bounded commits by their author date instead, so a commit released after the series ended was listed
and one authored earlier but released in time was not) that either carry `Cc: stable` with an annotation
covering it (`annotation_applies`: none → every series; `3.0+`; `v3.0` / `3.0.x` / `3.0`; `2.6.32-3.2`;
comma-separated lists) or fix (`Fixes:`) a commit the series contains, and that no stable tag of the series
carries as a backport (`commit <sha> upstream.`, §2.16). **Database**: when `db_scope_covered()` (relation
rows ready, release membership covering every tag, and every commit of those later mainline releases and of
the series' stable releases ingested, `tags_ingested()`), the report is computed with `SELECT`s over
`m_commit_release`, `m_release_segment`, `m_commit_relation` and `m_commit` (`source: "db"`) and no SQLite
index is built. **Otherwise** one query over the relation index with the release index attached (`source:
"index"`). The result is cached on disk per (repository, series, format, source tokens). v3.0 on the reference
clone (SQLite path): 5,074 marked commits checked in 1.7 s.
- **Parameters**: `series` (a release with stable tags, `safe_rev`), `limit` (default 5,000, max 20,000),
  `offset`.
- **Response**: `{series, available, reason?, last_stable, stable_tags, candidates_checked, missing, counts:
  {"cc-stable", "fixes"}, total, offset, candidates: [{sha, subject, date, reasons: ["cc-stable" | "fixes"],
  annotation, fixes: [{sha, release}], mainline_release}]}` sorted by mainline release. A release without
  stable tags answers `available: false` with a reason, rather than reporting everything missing. Backports
  without an upstream reference are not recognized.

---

#### `GET /api/insights/{version_name}/release-stats` · `GET /api/insights/release-stats/series?tag=…`
Statistics of a release (H6, `insights_service.release_stats`; the version is the release tag): `{release,
prev, source, commits, merges, authors, first_time_authors, first_timers: [...], files_changed, lines_added,
lines_removed, days, rc_count, top: {authors, reviewers, testers, reporters, ackers, signers: [{person, email,
count, share}]}, employers: [{org, commits, authors}]}`. `source` says where the figures come from:
- **`database`** (`db_release_stats`, `SELECT` only, ~40–150 ms): the parser's release statistics
  (`m_release_segment`, `m_release_person`, `m_person_first_release`; `core/API.md` §10), used only when they
  cover the release exactly as the repository's tags cut it now — every tag of the release's chain in
  `previous(tag)..tag` summarized (`parser/git_ast/release_order.release_ranges`) with the same commit,
  segment base and `release_stats.FORMAT`, and the release's own diff base equal to `previous(tag)`. Commits
  and merges are the sums of those segments (a release: its -rc segments), the diffstat is the release's own
  row; person counts are folded into identities with the Developers directory's `_identity_index` (§4.5) and
  employers mapped from each author's address at read time; first-time authors are the identities whose
  aliases' earliest `m_person_first_release` segment lies in the range (release order, not author dates).
- **`git`** (the fallback: tables missing, repository never parsed, a tag fetched or moved since the parser
  ran): commits from the release's commit pass (§2.17); merges and line counts from `git rev-list --count
  --merges` and `git diff --shortstat`; identities merged with `maintainer_service._identity_key`; a
  first-timer is an author whose first commit on the mainline chain of the release's tag family (one `git log`
  over history from its newest mainline tag, cached per head) is not dated before the release's earliest
  commit of theirs. A job (202 while building, ~20 s cold for
  v3.0) cached on disk per tag commit (`REPORT_FORMAT` 2).
- **Both**: a commit counts once per identity and role (two aliases or a repeated trailer in one commit count
  once; the author's own Signed-off-by is left out); top lists hold the 25 largest counts, ties in identity
  order; `first_time_authors` is `null` and `first_timers` empty for a stable update (its commits are
  backports of mainline commits); employers come from `webapp/backend/data/email_domains.json` (domain →
  organization; personal providers count as *(Individual / unknown)*, other unknown domains as *(domain)*; the
  file is plain data reviewed like code). The two sources agree on every figure except first-time authors
  (v3.0: 234 by release order, 232 by author date; v7.2: 650 vs 660).
The `series` form returns, per tag, the database figures, else cached Git statistics (`{release, commits,
authors, first_time_authors, days}`), else `{release, pending: true}` and queues the Git job; at most 40 tags.

#### `GET /api/insights/{version_name}/merge-window`
The pulls of a release (H5; a final release `vX.Y` / `<prefix>X.Y` only, else 400; `rc1` is null for a
release without release candidates): per first-parent merge of `<prev>..<tag>` —
`{merge, parents, date, day (days since the previous release), after_rc1, subject, source_url, ref, sender,
commits (git rev-list --count --no-merges p1..p2), dirs (git diff --dirstat=files,3,cumulative, top 8),
section (the MAINTAINERS T: tree matching the source, by URL or by the tree owner's directory when that owner
maintains one tree), thread (the [GIT PULL] mail: m_commit_email relation 4), body}` — plus `{prev, rc1,
rc1_day, totals: {pulls, commits, before_rc1, by_day: [{day, commits}]}}`. A job cached per tag commit (~10 s
cold for v3.0, 389 pulls).

#### `GET /api/insights/{version_name}/subsystems?window=4` · `GET /api/insights/{version_name}/subsystems/{sec_id}`
Health of every MAINTAINERS section over the last `window` (1–12) final releases up to the given one (H7):
each commit counts for every section one of its files matches (the release's `MaintainerMatcher`, *THE REST*
excluded). Per section `{sec_id, name, status, commits, per_release, authors, review_coverage (share of
commits with a Reviewed-by or Acked-by from someone other than the author), bus_factor (fewest authors
covering half the commits), top_authors, maintainers: [{person, email, role M|R, last_active (last authored,
signed-off, reviewed, acked or tested commit in the window)}], flags}`; flags `orphan_active` (Orphan/Obsolete
with ≥ 5 commits), `silent_maintainers`, `single_author` (bus factor 1, ≥ 10 commits), `no_review` (coverage <
10 %, ≥ 10 commits), `no_activity`. **Sources** (`source`): `"database"` when it covers every release of the
window (`health_coverage()`: each range previous final release → release maps to release segments in the
current format, `walk_segments()` §2.17; finished ingestions join the two tags; the segments' non-merge
commits in `m_commit` are as many as they counted) — commits from `m_commit_release` ⋈ `m_commit` without a
second parent, their files from `m_commit_file` (new and old name, as `--no-renames` lists both sides of a
rename), trailers from `m_bridge_commit_person` (reviewed, acked, signed-off, tested), the sections of the
tag's tree files from `m_maintainer_file` and of other paths (deleted before the tag) from the matcher,
`SELECT` only, no `git log`; else `"git"`: one `git log --no-merges --no-renames --name-only` pass per release
(`release_commits_now`, cached on disk per range). Both paths feed the same aggregation
(`_aggregate_health()`) in `git log` order, so they give identical rows. A job cached per (tag commit, window,
release membership token, `HEALTH_FORMAT` 3); the `{sec_id}` form returns one section.

#### `GET /api/insights/{version_name}/cves?state=affected|possibly|fixed`
Linux kernel CVEs affecting a release, possibly affecting it, or fixed in it (H4, §4.11): `{release, head,
layout, kinds, counts: {affected, possibly, fixed, fixed_here}, total, cves: [{id, title, state, basis, kind,
published, fixes, fixed_here, score, severity, kev}]}` (`fixed_here`: a fix first released in this release —
its -rc cycle, or this stable update); `{configured, ingested: false, cves: []}` before the parser ingested a
CVE list. A job cached per (ingested HEAD and time, release, release index build, commit relation index
build); ~1–4 s.

### 4.11. CVE Records API (`webapp/backend/routers/cve.py`, `webapp/backend/services/cve_service.py`)
The parser ingests the CVE list into the database (`m_cve_source`, `m_cve`, `m_cve_commit`; `core/API.md` §9):
config `cve.repo_dir` (§7.1, default `cvelistV5` under the project root) names a clone of the CVE Program's
list (`cvelistV5`, every CNA: the kernel's records picked with one `git grep`) or of the kernel CNA's
`vulns.git`; the operator keeps it current (`git pull`), the parser reads each new HEAD at the start of a run
(`python3 main.py --cve` does only that), incrementally. The webapp only reads the tables: nothing is rebuilt
when it starts.
- **Kinds** (`parser/cve/cve_records.py` `parse_record`): `cna` — issued by the kernel CNA: git ranges per
  branch (`version` introducing commit, `lessThan` fixing commit; the root commit `1da177e4c3f4` as introducer
  means *since the beginning*, no fix means unfixed on that branch) and version entries per affected product,
  each with its own `defaultStatus`; `commit` — another CNA's record whose references (CNA and ADP containers)
  link a kernel commit (`github.com/torvalds/linux/commit/…`,
  `git.kernel.org/…/linux/kernel/git/<tree>.git/commit/?id=…`, gitweb `;a=commit;h=`,
  `git.kernel.org/linus/…`, `…/stable/c/…`; 12–40 hex digits; other git.kernel.org projects do not count):
  fixes with an unknown introduction; `text` — about the kernel (product `Linux`/`kernel`, or the description)
  without a commit: versions from the product entries, else from the description (`before 4.8.3`, `through
  6.0.6`: the highest bound). Also kept: title, English description (3,000 characters), dates, programFiles,
  references (`http(s)` only), CVSS (the CNA's, else an ADP's: score, severity, vector, version, source), CISA
  KEV date, SSVC exploitation, and CWE weakness identifiers (`cwes`). cvelistV5 at 2026-11-06: 22,023 kernel
  records (18,202 `cna`, 1,746 `commit`, 2,075 `text`).
- **Reading**: `source()` reads `m_cve_source` (at most every 60 s); the records (`m_cve.data`) are loaded
  once per ingestion (`built_at`) into memory, 22k in ~0.8 s; `for_commit()` answers from `m_cve_commit` (the
  full SHA and its 12/13/14/16-character prefixes, which references abbreviate) without loading them. Release
  reports are jobs (§2.15) cached on disk (§2.14) per ingestion, release and release/relation index builds.
- **Release states** (`affects(record, release)` → `(state, basis)`): for `cna` records the version entries
  decide on releases and stable updates — an entry answers on its own (an explicit range, else its
  `defaultStatus`), the version is *affected* when one entry says so, and *unaffected* becomes *fixed* when an
  affected range of the same line starts at or below it (`5.4.262 ≤ x < 5.4.269` makes 5.4.270 fixed) — and
  the git ranges decide on -rc tags: a range counts when its fix is on the release's branch (a mainline fix
  for mainline tags; for a stable update, mainline fixes and the fixes of its own series; a fix in no release
  yet counts unless another range says fixed), *affected* when its introduction is contained and its fix is
  not, from the first tags of release membership (§2.17: `m_commit_release`, or the SQLite index when the
  database does not cover every tag; no `release-index` job runs when it does). `commit` records: *fixed* when
  a fix or one of its stable backports (§2.16: `m_commit_relation` when every commit of the releases read is
  ingested, `tags_ingested()`, else the commit relation index when built) is in the release; else *affected* /
  *unaffected* from the commits the fixes name in `Fixes:` tags (basis `fixes-tag`; `m_commit_relation` for
  ingested fixes), else *possibly* (basis `reference`). `text` records: *possibly* when their versions include
  the release (basis `description` / `versions`). Abbreviated commit hashes of the records are resolved from
  `m_cve_commit_hash` (one query per report, `Context.prefetch()`; the parser resolves them at CVE ingestion,
  `core/API.md` §9); only prefixes it lacks run `git rev-parse`. Example: CVE-2016-5195 is *possibly* at v3.0
  and v4.8, *fixed* at v4.8.3 (its backport) and v4.9.
- `GET /api/cve/status` → `{configured, ingested: false}` before the first ingestion, else `{configured,
  ingested, name, layout, head, indexed_head, records, built_at, state, format, current_format}`.
- `GET /api/cve/{cve_id}?release=` (`CVE-\d{4}-\d{4,}`, else 400) → the record with each range's commit
  subjects and first releases (`basis`; a `commit` record's ranges add `inferred_introduced` from the fix's
  `Fixes:` tags), the cve.org URL and, with `release`, `release_state: {release, state, basis}`; 404 when the
  list was never ingested or the id is not a kernel record.
- The relations endpoint (§4.6) adds `cves: {fixes: [ids], introduced: [ids], fixes_upstream: [ids]}` for the
  commit (`fixes_upstream`: the CVEs its upstream commit fixes — it is a stable backport).


### 4.12. Watch API (`webapp/backend/routers/watch.py`, `webapp/backend/services/watch_service.py`)
#### `POST /api/watch/changes`
What changed for watched items between two revisions (G10), read-only. **Body**: `{from, to, items: [{key,
type, path | name, arch?}]}` (≤ 200 items; both revisions through `safe_rev`, then resolved to commits). Per
type:
- `file`: the blob at both revisions — `added`, `removed`, `modified`, `same`;
- `dir`: `git diff-tree -r --name-status` under the prefix — `{summary: "N files changed", paths: [{status,
  path}] (≤ 200), truncated}`;
- `symbol` / `kconfig`: the definition Go to Definition opens at both revisions (`detail`, ranked for `arch`;
  a C symbol's extent from its `definition`), its text compared after `normalize_code` — `modified`,
  `cosmetic` (whitespace or comments only: not a change), `added`, `removed`, `same`, `absent` (no definition
  at either revision: symbols need an indexed revision);
- `section`: the MAINTAINERS entry with that title (up to the next blank line), whitespace-normalized.
**Response**: `{from, to, from_sha, to_sha, changed, items: [{key, type, status, changed, summary, links:
{compare: {a, b, path}, commits: {path | section}, history: {version, path, line, name}}, error?}]}`; an
invalid path or name answers that item's `error`, not the request. v3.0 → v3.1: `kernel/fork.c` modified,
`copy_process` and `fork_init` modified, `list_add` same, `fs/ext4/` 21 files changed.


### 4.13. Linux man-pages API (`webapp/backend/routers/man.py`, `webapp/backend/services/man_service.py`)
Man pages of the Linux man-pages repository (`webapp/API.md` §2.20). The index, page and tags endpoints answer
from the request's repository when it is the man-pages (`X-KI-Repo` header or `?repo=`; the header wins), else
from the configured man-pages repository; 404 when none is configured. Revisions pass `safe_rev` (400),
unknown ones answer 404.

#### `GET /api/man/{v}/index`
Every page file of revision `v`, one entry per page, sorted by section (`SECTION_ORDER`) then name.
- **Response**: `{"version": v, "repo", "commit", "source": "db" | "git", "total", "pages": [{"name", "section",
  "path", "summary", "so"}]}`. `so` is the repository path a `.so` alias finally resolves to (null for a
  page); `summary` is the NAME line after `\-` (an alias's own, else its target's). `source` is `db` when
  every page blob has a stored family 6 row.

#### `GET /api/man/{v}/page`
- **Query Parameters**: `path` (a page path in either layout, `man/man2/read.2` or `man2/read.2`) or `name`
  (`^[A-Za-z0-9_.:+-]+$`) with optional `section` (`^[0-9][a-z]*$`; without it the first section in
  `SECTION_ORDER` that has the page, with it that section then its siblings, `2` -> `2type`, `2const`);
  `kernel`, `kernel_repo` (the kernel revision and repository the reader came from).
- **Response**: `{"version", "repo", "path", "resolved_path", "name", "section", "aliases", "facts", "doc",
  "date", "linux": {"repo", "version", "syscall", "symbols"}}`. `path`, `name`, `section` name the requested
  page; `resolved_path` the page its `.so` chain ends at, whose document model (`doc`, format 1,
  `parser/man_ast/MAN_API.md`; unreleased `.TH` placeholders replaced at a release tag) and facts
  (`man_facts.extract()`) are returned; `aliases` are its NAME entries; `date` the committer date (unix) of the
  revision's commit (a tag's: the commit it names, never the tag's creation date). `linux.version` is `kernel`
  when it resolves in `linux.repo`, else the newest `vX.Y` kernel release whose commit is dated on or before
  `date`; `linux.syscall` the page name for section `2` (else null); `linux.symbols` the
  SYNOPSIS identifiers. `linux.repo` / `linux.version` are null without a kernel repository.
- **Errors**: 400 for an invalid or missing path / name / section, 404 when the page does not exist at `v`.

#### `GET /api/man/resolve`
For the request's repository (a kernel): our copy of `name(section)` for kernel revision `kernel`.
- **Query Parameters**: `name` (required), `section` (optional), `kernel` (default: the repository's newest
  `vX.Y` release, else `HEAD`).
- **Response** (found): `{"available": true, "repo": "<man-pages repository>", "version": "man-pages-X.Y",
  "path", "name", "section", "kernel", "kernel_date", "tag_date", "url"}` — the newest `man-pages-*` release
  tag whose commit is dated on or before the kernel revision's commit (`kernel_date`, `tag_date`: committer
  dates of those commits; the oldest tag when all are later; older tags, then newer ones, when the page is
  missing there); `section` is the section found. Otherwise `{"available": false, "url":
  "https://man7.org/linux/man-pages/man{S}/{name}.{S}.html", "name", "section"}` (plus `kernel`,
  `kernel_date` when the revision resolved); `url` is the man7.org page index without a section. 400 for an
  invalid name, section or kernel revision.

#### `GET /api/man/tags`
- **Response**: `{"repo", "tags": [{"name", "date", "commit"}]}` — every `man-pages-*` tag of the man-pages
  repository with its release date (unix: the committer date of the commit it names), newest first.
