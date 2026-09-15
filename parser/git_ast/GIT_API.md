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

## 2. Relational Schema Mapping (Tables 26–29)

`git_ast` populates four core relational tables tracking commit metadata, contributors, affected files, and modified code tags:

| Table ID | Table Name | Columns | Primary Key | `no_duplicate` | Description |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **26** | `m_commit` | `(commit_id, vid, commit_hash, author_id, author_date, committer_id, committer_date, subject, message)` | `("commit_id",)` | `False` | Git commit registry. Primary key `commit_id` is AUTO_INCREMENT. |
| **27** | `m_bridge_commit_person` | `(commit_id, person_id, role_type, priority)` | `("commit_id", "person_id", "role_type")` | `False` | Contributor associations: author, committer, and trailers. |
| **28** | `m_bridge_commit_file` | `(commit_id, vid, fid, change_type)` | `("commit_id", "fid")` | `False` | Files touched per commit (`change_type`: `"M"`, `"A"`, `"D"`, `"R"`). |
| **29** | `m_bridge_commit_tag` | `(commit_id, vid, fid, tag_id)` | `("commit_id", "tag_id")` | `False` | Code tags modified per commit revision. |

### 2.1. Staging Example

```python
# 1. Register Commit
commit_row = m_commit.set(
    None,            # commit_id (AUTO_INCREMENT)
    VID,             # vid
    commit_hash,     # 40-char git commit SHA
    author_id,       # FK -> m_maintainer_person.person_id
    author_date,     # Unix epoch timestamp
    committer_id,    # FK -> m_maintainer_person.person_id
    committer_date,  # Unix epoch timestamp
    subject[:255],   # Commit subject line
    body,            # Full commit message
)
commit_id = commit_row[2][0]

# 2. Stage Contributor Roles
m_bridge_commit_person.set(commit_id, author_id, CommitRole.Author, 0)
m_bridge_commit_person.set(commit_id, committer_id, CommitRole.Committer, 1)
for priority, (person_id, role) in enumerate(trailers):
    m_bridge_commit_person.set(commit_id, person_id, role, priority + 2)

# 3. Stage Modified Files & Tags
for fid, change_type in modified_files:
    m_bridge_commit_file.set(commit_id, VID, fid, change_type)

for fid, tag_id in affected_tags:
    m_bridge_commit_tag.set(commit_id, VID, fid, tag_id)
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
- Correlated tags are staged into `m_bridge_commit_tag(commit_id, vid, fid, tag_id)`.
- This enables web applications and static analysis tools to query all commits that modified a specific function, struct, or macro across kernel versions in $O(1)$ time.

---

## 5. Performance Invariants & Architectural Rules

1. **Streaming Delimited Ingestion**:
   - Uses binary control characters (`RECORD_SEP`, `FIELD_SEP`, `BODY_END_SEP`) rather than custom regex or multiline string splits, preventing catastrophic backtracking and parsing 100,000+ kernel commits in seconds.
2. **Subprocess Buffer Protection**:
   - `_run_git()` caps output buffer at `max_output_bytes = 50MB` with explicit process timeouts (120s) to prevent host memory exhaustion during mega-merges.
3. **Bisect Interval Queries**:
   - Spatial hunk-to-tag correlation uses $O(\log N)$ interval searching via Python's `bisect` module, avoiding $O(N \cdot M)$ scans between commits and tags.
4. **Bulk Staging & Cache Bypass**:
   - Commit batches are staged with TableEngine secondary in-memory caching disabled (`update_in_mem_indexes=False`) to avoid maintaining hundreds of thousands of ephemeral commit indexes in RAM.
