# Git Commit & Contributor Subsystem API Specification (`git_ast`)

Authoritative architectural specification and relational staging reference for `parser/git_ast/` ([git_commit_parser.py](parser/git_ast/git_commit_parser.py), [git_types.py](parser/git_ast/git_types.py)). Designed as an exhaustive reference for AI agents extending, maintaining, or querying kernel commit histories, multi-person trailer contributions, merge lineages, and commit-to-code tag bridges.

---

## 1. High-Level Architecture & Pipeline Flow

The Git Commit subsystem extracts commit histories, author/committer identities, structured trailers, modified files, and diff hunks, bridging Git revisions directly to source occurrence tags (`m_tag`).

```
==================================================================================================
GIT COMMIT & MULTI-CONTRIBUTOR PARSING PIPELINE
--------------------------------------------------------------------------------------------------
Kernel Git Repository (linux/)
  │
  ▼ [main.py: processing_git_commits(version) (Step 6.5)]
Streaming Delimiter-Delimited Git Log Execution:
  ├── git log <range> --format="%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%cn%x1f%ce%x1f%ct%x1f%s%x1f%B%x1d" --name-status
  ├── RECORD_SEP (\x1e): Delimits individual commit records
  ├── FIELD_SEP (\x1f): Delimits commit metadata fields
  └── BODY_END_SEP (\x1d): Delimits commit message from modified file change list
  │
  ▼ [GitCommitParser.parse_commit_stream() (git_commit_parser.py)]
Commit Processing & Trailer Extraction:
  ├── Committer & Author: Ingests into m_maintainer_person (Table 20)
  ├── Structured Trailers (TRAILER_PATTERN regex):
  │     └── Co-developed-by, Signed-off-by, Reviewed-by, Acked-by, Tested-by, Reported-by, Suggested-by
  ├── Merge Metadata (PULL_FROM_RE, BRANCH_MERGE_RE):
  │     └── Identifies subsystem trees, pull request maintainers, and merged branch URLs
  └── Diff Hunks & File Status:
        └── Extracts changed files, rename mappings, and diff line coordinate intervals
  │
  ▼ [GitCommitParser.stage_commits() -> TableEngine Staging]
Relational Database Mapping:
  ├── Commit Registry: m_commit (Table 26)
  ├── Contributor Roles: m_bridge_commit_person (Table 27)
  ├── Modified Files: m_bridge_commit_file (Table 28)
  └── Code Occurrence Tag Bridge: m_bridge_commit_tag (Table 29)
        └── _map_hunk_to_tags: Bisect interval search over m_bridge_tag coordinates
==================================================================================================
```

---

## 2. Relational Schema Mapping (Tables 26–29, 36–39)

`git_ast` populates core relational tables tracking commit metadata, contributors, affected files, code tags, multi-remote networks, and DAG topology within the content-addressable Git DAG:

| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **26** | `m_commit` | `(commit_id, network_id, commit_hash, author_id, author_date, committer_id, committer_date, subject, message)` | `("commit_id",)` | `False` | Git commit registry. `commit_hash` is `BINARY(20)` SHA-1. Primary key `commit_id` is AUTO_INCREMENT. |
| **27** | `m_bridge_commit_person` | `(commit_id, person_id, role_type, priority)` | `("commit_id", "person_id", "role_type")` | `False` | Contributor associations: author, committer, and trailers. |
| **28** | `m_commit_file` | `(commit_id, fnid, blob_id, old_fnid, old_blob_id, change_type)` | `("commit_id", "fnid")` | `False` | First-parent commit tree deltas (aliased as `m_bridge_commit_file`). |
| **29** | `m_bridge_commit_tag` | `(commit_id, blob_id, tag_id)` | `("commit_id", "tag_id")` | `False` | Code tags modified per commit revision. |
| **36** | `m_git_network` | `(network_id, project_id, name, root_commit_hash)` | `("network_id",)` | `False` | Multi-remote repository network sharing the same initial root commit. |
| **37** | `m_repository` | `(repo_id, network_id, name, git_url, default_branch)` | `("repo_id",)` | `False` | Tracked Git repository clone/remote instance. |
| **38** | `m_git_ref` | `(ref_id, repo_id, ref_type, ref_name, commit_id, tagger_id, tagger_date, tag_message)` | `("ref_id",)` | `False` | Named Git reference (tag or branch), aliased as `m_v_main`. |
| **39** | `m_commit_parent` | `(commit_id, parent_commit_id, parent_idx)` | `("commit_id", "parent_idx")` | `False` | Directed acyclic graph (DAG) parent lineage per commit. |

### 2.0. Merge Commits Stage No File Rows

Commit file rows and diff hunks both come from one `git log -p -U0` stream (`parse_version_commits_with_hunks`). **`git log -p` prints no diff for a commit with more than one parent**, so a merge commit reaches `parse_commit_log_with_hunks` with an empty patch part and is staged with `files = []` — no `m_commit_file` rows and no `m_bridge_commit_tag` rows, even when the merge resolved a conflict itself (an "evil merge", e.g. `1f922d0770` in `v3.0`, which changes `arch/sparc/include/asm/ptrace.h`).

This is deliberate: `--diff-merges=first-parent` would make each merge repeat the whole delta of the branch it merges, which on the full history up to `v3.0` grows the log stream from ~53 MB to ~734 MB per release range and would re-attribute every merged hunk to the merge commit as well. A merge's first-parent delta is instead resolved from Git **on demand** by the webapp (`webapp/backend/services/git_service.py:_fill_missing_commit_files`, `webapp/API.md` §`/api/commits/{v}/detail/{id}`), which is what keeps the *Modified Files* list in agreement with the commit's rendered diff.

### 2.1. Staging Example

```python
# 1. Register Commit
commit_row = m_commit.set(
    None,            # commit_id (AUTO_INCREMENT)
    network_id,      # network_id (FK -> m_git_network)
    commit_hash,     # 20-byte binary git commit SHA (bytes)
    author_id,       # FK -> m_maintainer_person.person_id
    author_date,     # Unix epoch timestamp
    committer_id,    # FK -> m_maintainer_person.person_id
    committer_date,  # Unix epoch timestamp
    subject[:500],   # Commit subject line
    body,            # Full commit message
)
commit_id = commit_row[2][0]

# 2. Stage Contributor Roles
m_bridge_commit_person.set(commit_id, author_id, CommitRole.Author, 0)
m_bridge_commit_person.set(commit_id, committer_id, CommitRole.Committer, 1)
for priority, (person_id, role) in enumerate(trailers):
    m_bridge_commit_person.set(commit_id, person_id, role, priority + 2)

# 3. Stage Modified Files & Tags
for fnid, blob_id, old_fnid, old_blob_id, change_type in modified_files:
    m_commit_file.set(commit_id, fnid, blob_id, old_fnid, old_blob_id, change_type)

for blob_id, tag_id in affected_tags:
    m_bridge_commit_tag.set(commit_id, blob_id, tag_id)
```

---

## 3. Contributor Taxonomy & Structured Trailers

Kernel commits frequently feature contributions from multiple developers beyond the git commit author and committer. `git_commit_parser.py` parses structured trailers defined in `git_types.py:CommitRole`:

| Role Constant | `CommitRole` Value | Trailer Prefix in Commit Message |
| :--- | :---: | :--- |
| `CommitRole.Author` | 1 | Git Author header (`Author: Name <email>`) |
| `CommitRole.Committer` | 2 | Git Committer header (`Commit: Name <email>`) |
| `CommitRole.CoDevelopedBy` | 3 | `Co-developed-by: Name <email>` |
| `CommitRole.SignedOffBy` | 4 | `Signed-off-by: Name <email>` |
| `CommitRole.ReviewedBy` | 5 | `Reviewed-by: Name <email>` |
| `CommitRole.AckedBy` | 6 | `Acked-by: Name <email>` |
| `CommitRole.TestedBy` | 7 | `Tested-by: Name <email>` |
| `CommitRole.ReportedBy` | 8 | `Reported-by: Name <email>` |
| `CommitRole.SuggestedBy` | 9 | `Suggested-by: Name <email>` |

Contributors are sanitized and stored in `m_maintainer_person` (Table 20), linking maintainer identities with git author and trailer histories across releases.

---

## 4. Diff Hunk Extraction & Line Coordinate Tag Bridging

`git_ast` bridges commits to AST code occurrence tags (`m_tag`) using diff coordinate spatial indexing:

### 4.1. Hunk Interval Representation (`CommitDiffHunk`)
- For modified files, executes `git diff-tree -p -U0 <commit>` to extract line modification intervals:
  - `old_start, old_count`: Modified range in previous version.
  - `new_start, new_count`: Modified range in active version.

### 4.2. Binary Search Tag Correlation (`_map_hunk_to_tags`)
- Collects active tags for the modified file from `m_bridge_tag` sorted by start line:
  ```python
  tag_intervals = [(line_s, line_e, tag_id), ...]
  ```
- Uses `bisect` over `tag_intervals` to locate code tags overlapping the diff hunk range `[new_start, new_start + new_count]`.
- Correlated tags are staged into `m_bridge_commit_tag(commit_id, blob_id, tag_id)`.
- This enables web applications and static analysis tools to query all commits that modified a specific function, struct, or macro across kernel versions in $O(1)$ time.

---

## 5. Multi-Tree DAG Ingestion & Cross-Tree Content Deduplication

### 5.1. Root Commit Discovery & Git Network Resolution (`resolve_git_network_and_repo`)
When indexing multiple Git repositories (e.g., mainline `torvalds/linux` alongside subsystem trees like `linux-next`), the parser discovers the repository's root commit via:
```bash
git rev-list --max-parents=0 HEAD
```
For Linux, this resolves to root commit `1da177e4c3f41524e886b7f1b8a0c1fc7321cac2` ("Linux-2.6.12-rc2").
- Trees that share the identical root commit hash are registered under the same `m_git_network` record.
- Distinct repositories pointing to that network are tracked individually in `m_repository(repo_id, network_id, name, git_url, default_branch)`.

### 5.2. Complete Git DAG Ingestion (`ingest_git_dag`)
The Git commit DAG is ingested upfront before file AST parsing:
- **Topology**: Traverses full commit lineages, staging parents into `m_commit_parent(commit_id, parent_commit_id, parent_idx)`.
- **References**: Registers tags and branches into `m_git_ref(ref_id, repo_id, ref_type, ref_name, commit_id, ...)`.
- **Backward Compatibility**: `gp.VID` is anchored to the target Git reference's `head_commit_id`, ensuring downstream relational tables seamlessly align with `m_bridge_checkpoint` and `m_v_main`.

### 5.3. Content-Addressable Blob Deduplication Across Trees
Git trees are enumerated via `git ls-tree -r {version}` into 20-byte SHA-1 digests.
- Before queuing files for parallel worker AST parsing, existing blobs in `m_file_blob` with matching `(blob_hash, ftype)` and valid `m_bridge_tag` records are linked directly into `m_bridge_checkpoint` and `m_commit_file`.
- Deduplicated files bypass Phase 1 worker queues entirely, eliminating redundant AST extraction across branches or trees sharing identical source files.

---

## 6. Performance Invariants & Architectural Rules

1. **Streaming Delimited Ingestion**:
   - Uses binary control characters (`RECORD_SEP`, `FIELD_SEP`, `BODY_END_SEP`) rather than custom regex or multiline string splits, preventing catastrophic backtracking and parsing 100,000+ kernel commits in seconds.
   - Record chunks are trimmed with `strip(" \t\r\n")`, never bare `strip()`: Python treats `\x1c`–`\x1f` as whitespace, and a bare strip removes the trailing `BODY_END_SEP` of merge commits (which have no `--name-status` file list), silently dropping every merge and every DAG edge through it.
2. **Subprocess Buffer Protection**:
   - `_run_git()` caps output buffer at `max_output_bytes = 50MB` with explicit process timeouts (120s) by default; callers may raise both (e.g. `parse_version_commits()` allows 4 GiB / 1800 s for whole-history ranges). Output past `max_output_bytes` is cut mid-record, so the cap logs a warning when it truncates, and a timeout logs the whole command (`--format=<spec>` elided) with the deadline it exceeded — not just `git -C <repo> log`.
   - `parse_version_commits(old_rev, new_rev, limit=None, with_files=True)`: Without `old_rev` the complete history up to `new_rev` is returned (no implicit cap), so the commit DAG reaches the root commit; `limit` bounds the count. `with_files=False` omits `--name-status` for DAG-only ingestion (≈9 s for the 255k commits up to v3.0).
   - `count_commits(revision_arg)`: `git rev-list --count` walks the DAG without reading a blob, sizing even a 1.5M-commit range in ~10 s (-1 when git cannot report it).
   - **Diff-stream commit caps** (`parse_version_commits_with_hunks()`): `git log -p -U0` streams every changed line of every commit in the range (~1,800 commits/s, ~1.3 KB/commit), so a wide range is gigabytes and tens of minutes. With no explicit `limit` the newest commits only are streamed: `INITIAL_HUNK_COMMIT_CAP = 1000` without `old_rev` (whole history), and `RANGE_HUNK_COMMIT_CAP = 20000` for `old_rev..new_rev`, applied after `count_commits()` sizes the range (or when it cannot). The subprocess timeout scales with the commit budget (`budget / 100`, floor 300 s, ceiling 3600 s) and the buffer is 1 GiB. Commits past the cap keep the `m_commit`/`m_commit_parent`/`m_bridge_commit_person` rows `ingest_git_dag()` already staged; they only get no `m_commit_file`/`m_bridge_commit_tag` rows, and the cap logs which range it bounded. Without the cap a branch tip far from the prior release (e.g. `v2.6.12-rc2..assoc-array-fixes`, 1,485,104 commits) hit the subprocess timeout and returned nothing, dropping file and tag rows for the whole version; `main.py --limit N` (`gp.commit_limit`) overrides the caps, bounding both the DAG ingest and the diff stream.
3. **Interval Index Queries**:
   - Spatial hunk-to-tag correlation (`map_tags_to_commits`, more than 8 hunks) uses `_HunkIndex`: hunks sorted by start line with a max-end segment tree, returning the hunks overlapping a tag's line range in $O(\log H + \text{matches})$ (in hunk start order) instead of scanning every hunk that starts before the tag ends.
4. **Bulk Staging & Cache Bypass**:
   - Commit batches are staged with TableEngine secondary in-memory caching disabled (`update_in_mem_indexes=False`) to avoid maintaining hundreds of thousands of ephemeral commit indexes in RAM.
5. **Commit DAG Parent Linking**:
   - `git log` lists children before parents. `ingest_git_dag()` registers the range in bulk (`_ingest_commit_range()`): existing commits are looked up by hash in chunks of 1,000, persons are loaded once, and new persons, commits (ids assigned oldest-first), parents and contributors are inserted with multi-row statements instead of several round trips and transaction commits per commit. Persons are identified by `(name, email)` (`_person_key()`), the same key the TableEngine uses for `m_maintainer_person`. The resulting hash -> id map is kept on `gp.commit_ids_by_hash`; `processing_git_commits()` reuses it (looking any other hash, including out-of-range parents, up in one batch) and only creates persons for commits not yet registered. It defers `m_commit_parent` rows until every commit of the range has an id.
   - Files reused by Step 4.5's blob deduplication (`gp.deduped_paths`) are not parsed: `processing_git_commits()` loads their tags from `m_bridge_tag` (`_load_blob_tags()`) and maps them to the range's commits like parsed files.
   - `main.py --limit N` bounds the commits `ingest_git_dag()` records per version range (default: all).
6. **Dynamic Repository Routing**:
   - Multi-tree parsing dynamically routes git subprocess invocations to the active repository directory via `MF.set_repo_dir(repo_dir)`, maintaining independent Git repository context across diverse tree checkouts.
7. **Binary vs Hexadecimal Commit Hash Formatting**:
   - Relational tables store commit hashes in compact 20-byte `BINARY(20)` format. Public APIs, services, and webapp endpoints must format binary digests as 40-character lowercase hexadecimal strings (`format_commit_hash(val)`: `bytes(val).hex()`) and use `UNHEX(%s)` for SQL queries.

