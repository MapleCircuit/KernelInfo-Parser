"""git_commit_parser.py - High-performance Git commit and multi-contributor parser.

Extracts commit histories, author/committer information, trailer contributors
(Co-developed-by, Signed-off-by, Reviewed-by, etc.), modified file maps,
and correlates code occurrence tags (m_tag) to commit revisions.
"""
import re
import bisect
import subprocess
import logging
from pathlib import Path
from typing import Any, Iterator, Sequence

from core.globalstuff import G
from parser.git_ast.git_types import (
    CommitRole,
    GitContributor,
    GitCommit,
    CommitDiffHunk,
)

logger = logging.getLogger(__name__)

# Control delimiters for robust machine-readable git log parsing
RECORD_SEP = "\x1e"  # Separates commits
FIELD_SEP = "\x1f"   # Separates fields within a commit
BODY_END_SEP = "\x1d"  # Separates body from file status list

# `git log -p` streams every changed line of every commit, so the diff stream of a wide range (a branch
# tip thousands of releases away from the prior one spans >1M commits) is gigabytes and hours of git time.
# These caps bound the stream when no explicit limit is given: the commits beyond them keep their DAG rows
# from ingest_git_dag(), they only get no m_commit_file/m_bridge_commit_tag rows.
INITIAL_HUNK_COMMIT_CAP = 1000  # whole history up to new_rev (no old_rev)
RANGE_HUNK_COMMIT_CAP = 20000   # old_rev..new_rev

# Regex to extract structured git trailers (supports Co-authored-by, Reported-and-tested-by, <email>, and bare email)
TRAILER_PATTERN = re.compile(
    r"^(Co-developed-by|Co-authored-by|Originally-by|Signed-off-by|Reviewed-by|Acked-by|Tested-by|Debugged-by|Reported-by|Reported-and-tested-by|Reviewed-and-tested-by|Suggested-by|Fix-suggested-by|Merged-by|Requested-by):\s*(?:([^<\n\r]+?)\s*)??(?:<([^>\n\r]+)>|([^\s<>@]+@[^\s<>@]+))",
    re.IGNORECASE | re.MULTILINE,
)


def iter_trailers(message: str) -> Iterator[tuple[str, str, str]]:
    """(trailer prefix, name, email) of every contributor trailer of a message (TRAILER_PATTERN).

    The one reading of a trailer's groups, shared by the parser and the webapp (release passes): a
    trailer without a name (`Reported-by: a@b`, `Reported-by: <a@b>`) takes its address as the name;
    the email is the `<...>` part, else the bare address."""
    for match in TRAILER_PATTERN.finditer(message or ""):
        prefix, name, email_angle, email_bare = match.groups()
        email = (email_angle or email_bare or "").strip()
        yield prefix, (name or "").strip() or email, email


# Regexes to extract Fixes: tags and revert references
FIXES_PATTERN = re.compile(
    r"^Fixes:\s*([0-9a-fA-F]{7,40})",
    re.IGNORECASE | re.MULTILINE,
)
REVERT_PATTERN = re.compile(
    r"(?:This reverts commit\s+|^Revert\s+[\"']?)([0-9a-fA-F]{7,40})",
    re.IGNORECASE | re.MULTILINE,
)

# Regexes to extract pull request requesters, maintainers, and merged branches
PULL_FROM_RE = re.compile(
    r"Pull\s+(?:.*?\s+)?from\s+([^:\n<]+?)\s*(?:<([^>\n\r]+)>)?\s*:",
    re.IGNORECASE,
)
FROM_HEADER_RE = re.compile(
    r"From:\s*([^<\n\r]+?)\s*<([^>\n\r]+)>",
    re.IGNORECASE,
)
BRANCH_MERGE_RE = re.compile(
    r"Merge (?:branch|tag)\s+'([^']+)'\s+of\s+(\S+)",
    re.IGNORECASE,
)
GIT_URL_MERGE_RE = re.compile(
    r"Merge (git://\S+|https://\S+|ssh://\S+)",
    re.IGNORECASE,
)
GENERIC_MERGE_BRANCH_RE = re.compile(
    r"Merge (?:branches?|tag)\s+(.+)",
    re.IGNORECASE,
)
URL_MAINTAINER_RE = re.compile(
    r"/(?:pub/scm/linux/kernel/git|kernel/git)/([a-zA-Z0-9_\-]+)/",
    re.IGNORECASE,
)


class _HunkIndex:
    """Hunks sorted by start line with a max-end segment tree.

    Finds the hunks overlapping `[line_s, line_e]` in O(log H + matches), instead of scanning every hunk
    that starts before `line_e` for each tag; results come in start order, as the linear scan returned them.
    """

    def __init__(self, hunks: list[tuple[int, int, int]]) -> None:
        self.hunks = sorted(hunks, key=lambda h: h[0])
        self.starts = [h[0] for h in self.hunks]
        size = 1
        while size < len(self.hunks):
            size *= 2
        self.size = size
        tree = [-1] * (2 * size)
        for i, (_start, end, _cid) in enumerate(self.hunks):
            tree[size + i] = end
        for i in range(size - 1, 0, -1):
            tree[i] = max(tree[2 * i], tree[2 * i + 1])
        self.tree = tree

    def overlapping(self, line_s: int, line_e: int) -> list[int]:
        """Commit ids of the hunks with `start <= line_e` and `end >= line_s`, in hunk start order."""
        idx_end = bisect.bisect_right(self.starts, line_e)
        found: list[int] = []
        stack = [(1, 0, self.size)]
        while stack:
            node, lo, hi = stack.pop()
            if lo >= idx_end or self.tree[node] < line_s:
                continue
            if hi - lo == 1:
                found.append(self.hunks[lo][2])
                continue
            mid = (lo + hi) // 2
            stack.append((2 * node + 1, mid, hi))
            stack.append((2 * node, lo, mid))
        return found


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")


def _map_line(hunks: list[tuple[int, int, int, int]], line: int) -> int:
    """Line of a commit's pre-image mapped into its post-image through its hunks `(old_s, old_c, new_s, new_c)`.

    Lines after a hunk shift by its size change; a line the commit replaced maps into the replacement (the
    line before it when the commit only deleted).
    """
    delta = 0
    for old_s, old_c, new_s, new_c in hunks:
        if old_c == 0:  # pure insertion after line old_s
            if line > old_s:
                delta += new_c
                continue
            break
        if line < old_s:
            break
        if line < old_s + old_c:
            return new_s + min(line - old_s, new_c - 1) if new_c else max(new_s, 1)
        delta += new_c - old_c
    return line + delta


class _HeadLineMapper:
    """Moves hunk line ranges of a commit range's commits into the newest commit's coordinates.

    `git log -p` gives each commit's hunks in that commit's own version of the file, while tags carry the
    lines of the version at the end of the range. Commits are fed newest first (git log order): a commit's
    ranges are mapped through the hunks of every newer commit that touched the file, and its own hunks are
    then kept for the older ones. Renames carry the chain to the old name; a file (re)created by a newer
    commit ends it, since older commits edited another file of that name.
    """

    def __init__(self) -> None:
        # Path as older commits name it -> hunks of the newer commits touching it (oldest last), None: ended
        self._chains: dict[str, list[list[tuple[int, int, int, int]]] | None] = {}
        self._head: dict[str, str] = {}

    def add(
        self, path: str, old_path: str, change: str, hunks: list[tuple[int, int, int, int]]
    ) -> tuple[str, list[tuple[int, int]]]:
        """Record one commit's hunks of `path`; returns the file's head path and the hunks' head line ranges."""
        chain = self._chains.get(path, [])
        head = self._head.get(path, path)
        mapped: list[tuple[int, int]] = []
        if chain is not None:
            for _old_s, _old_c, new_s, new_c in hunks:
                start, end = new_s, new_s + max(1, new_c) - 1
                for newer in reversed(chain):
                    start, end = _map_line(newer, start), _map_line(newer, end)
                mapped.append((start, max(start, end)))
            chain.append(sorted(hunks))
        if change == "A":
            self._chains[path] = None
        elif old_path and old_path != path:
            self._chains[old_path] = chain
            self._head[old_path] = head
            self._chains[path] = None
        else:
            self._chains[path] = chain
        return head, mapped


def _describe_git_cmd(cmd: Sequence[str]) -> str:
    """Render git arguments for a log line, eliding the control-character `--format=` spec."""
    return " ".join("--format=<spec>" if a.startswith("--format=") else a for a in cmd)


class GitCommitParser:
    """Parser for git commits, merge lineages, multi-person trailers, and tag-to-commit mappings."""

    def __init__(self, repo_dir: str | Path | None = None) -> None:
        if repo_dir is None:
            self.repo_dir = str(getattr(G, "linux_directory", Path("linux")))
        else:
            self.repo_dir = str(repo_dir)

    def _run_git(self, cmd: list[str], max_output_bytes: int = 50 * 1024 * 1024, timeout: float = 120) -> str:
        """Execute git command safely with timeout and return decoded stdout."""
        full_cmd = ["git", "-C", self.repo_dir] + cmd
        try:
            res = subprocess.run(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
            if res.returncode != 0:
                err_msg = res.stderr.decode("utf-8", errors="replace").strip()
                logger.warning(f"Git command '{' '.join(full_cmd[:4])}...' returned {res.returncode}: {err_msg}")
                return ""
            if len(res.stdout) > max_output_bytes:
                logger.warning(
                    f"Git command '{_describe_git_cmd(cmd)}' produced {len(res.stdout) / 1048576:.0f}MB; "
                    f"truncated to {max_output_bytes / 1048576:.0f}MB (the records past the cut are lost)"
                )
            out = res.stdout[:max_output_bytes]
            return out.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            logger.error(f"Git command timed out after {timeout:.0f}s: git -C {self.repo_dir} {_describe_git_cmd(cmd)}")
            return ""
        except Exception as e:
            logger.error(f"Failed to execute git command: {e}")
            return ""

    def count_commits(self, revision_arg: str) -> int:
        """Commit count of a revision range (`A..B` or a single ref), or -1 when git cannot report it.

        `git rev-list --count` walks the DAG without reading any blob, so it sizes even a 1.5M-commit
        range in seconds; `parse_version_commits_with_hunks()` uses it to bound its diff stream.
        """
        out = self._run_git(["rev-list", "--count", revision_arg, "--"], max_output_bytes=4096, timeout=600).strip()
        return int(out) if out.isdigit() else -1

    @classmethod
    def extract_merge_metadata(
        cls,
        subject: str,
        message: str,
        parents: list[str],
    ) -> tuple[bool, str | None, str | None, str | None, list[str]]:
        """Extract merge flag, pull requester maintainer, branch origin, and patch summaries.

        Returns:
            Tuple of (is_merge, requester_name, requester_email, merged_branch, merged_commits_summary)
        """
        is_merge = len(parents) >= 2 or subject.startswith("Merge ") or subject.startswith("Merge:")
        if not is_merge:
            return False, None, None, None, []

        merged_branch = None
        requester_name = None
        requester_email = None
        merged_commits_summary: list[str] = []

        # 1. Identify merged branch / tag / repository
        m_branch = BRANCH_MERGE_RE.search(subject) or BRANCH_MERGE_RE.search(message)
        if m_branch:
            merged_branch = f"'{m_branch.group(1)}' of {m_branch.group(2)}"
        else:
            m_url = GIT_URL_MERGE_RE.search(subject) or GIT_URL_MERGE_RE.search(message)
            if m_url:
                merged_branch = m_url.group(1)
            else:
                m_gen = GENERIC_MERGE_BRANCH_RE.search(subject)
                if m_gen:
                    merged_branch = m_gen.group(1).strip()
                elif subject.startswith("Merge "):
                    merged_branch = subject[6:].strip()

        # 2. Extract pull requester / maintainer
        m_pull = PULL_FROM_RE.search(message)
        if m_pull:
            requester_name = m_pull.group(1).strip()
            requester_email = m_pull.group(2).strip() if m_pull.group(2) else ""
        else:
            m_from = FROM_HEADER_RE.search(message)
            if m_from:
                requester_name = m_from.group(1).strip()
                requester_email = m_from.group(2).strip()
            elif merged_branch:
                m_maint = URL_MAINTAINER_RE.search(merged_branch)
                if m_maint:
                    requester_name = m_maint.group(1).strip()
                    requester_email = f"{requester_name}@kernel.org"

        # 3. Extract embedded patch topics and summary lines
        for line in message.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith(("Merge ", "Pull ", "From:", "Signed-off-by:", "Acked-by:", "Reviewed-by:", "---", "diff ")):
                continue
            if (line.startswith("  ") or line_str.startswith("* ")) and len(line_str) > 3:
                clean_line = line_str[2:].strip() if line_str.startswith("* ") else line_str
                if clean_line and not clean_line.endswith(":") and clean_line not in merged_commits_summary:
                    merged_commits_summary.append(clean_line)

        return True, requester_name, requester_email, merged_branch, merged_commits_summary

    def parse_version_commits(
        self,
        old_rev: str | None,
        new_rev: str,
        limit: int | None = None,
        with_files: bool = True,
    ) -> list[GitCommit]:
        """Extract all commits between old_rev and new_rev with full metadata and contributors.

        Args:
            old_rev: Prior version git ref or tag (if None or empty tree, inspects the whole history up to new_rev).
            new_rev: Target version git ref or tag (e.g. 'v3.0').
            limit: Maximum commits to parse (None = every commit in the range).
            with_files: Include the --name-status file list of each commit.

        Returns:
            List of GitCommit objects with parsed trailers and modified file lists.
        """
        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        if not old_rev or old_rev == EMPTY_TREE or str(old_rev) == "0":
            # Initial version: the complete history so the commit DAG reaches the root commit
            revision_arg = new_rev
        else:
            revision_arg = f"{old_rev}..{new_rev}"
        max_flag = [f"-n{limit}"] if limit else []

        fmt_spec = (
            f"{RECORD_SEP}%H{FIELD_SEP}%P{FIELD_SEP}%an{FIELD_SEP}%ae{FIELD_SEP}%at{FIELD_SEP}"
            f"%cn{FIELD_SEP}%ce{FIELD_SEP}%ct{FIELD_SEP}%s{FIELD_SEP}%B{BODY_END_SEP}"
        )

        git_args = ["log", f"--format={fmt_spec}"] + (["--name-status"] if with_files else []) + max_flag + [revision_arg, "--"]

        # Whole-history ranges (e.g. 250k+ commits up to v3.0) produce hundreds of MB of log output
        raw_output = self._run_git(git_args, max_output_bytes=4 * 1024 * 1024 * 1024, timeout=1800)
        if not raw_output:
            return []

        return self.parse_commit_log(raw_output)

    def parse_version_commits_with_hunks(
        self,
        old_rev: str | None,
        new_rev: str,
        limit: int | None = None,
        known_commit_count: int | None = None,
    ) -> tuple[list[GitCommit], dict[str, list[tuple[int, int, str]]]]:
        """Extract all commits and diff hunks in a single unified git log stream.

        Args:
            old_rev: Prior version git ref or tag (if None or empty tree, inspects up to new_rev).
            new_rev: Target version git ref or tag (e.g. 'v3.0').
            limit: Maximum commits to parse.
            known_commit_count: Optional known commit count for range to avoid duplicate `rev-list --count`.

        Returns:
            Tuple of:
              - List of GitCommit objects with parsed trailers and modified file lists.
              - Dictionary mapping file_path -> [(new_start, new_end, commit_hash), ...]
        """
        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        range_count = 0
        capped = False
        if not old_rev or old_rev == EMPTY_TREE or str(old_rev) == "0":
            revision_arg = new_rev
            effective_limit = limit or INITIAL_HUNK_COMMIT_CAP
        else:
            revision_arg = f"{old_rev}..{new_rev}"
            effective_limit = limit
            if effective_limit is None:
                # Sizing the range first (seconds even over 1.5M commits) keeps a branch tip far from the
                # prior release from starving the whole stream on the subprocess timeout
                if known_commit_count is not None and known_commit_count >= 0:
                    range_count = known_commit_count
                else:
                    range_count = self.count_commits(revision_arg)
                if range_count < 0 or range_count > RANGE_HUNK_COMMIT_CAP:
                    effective_limit = RANGE_HUNK_COMMIT_CAP
                    capped = True
                    logger.warning(
                        f"[Git] '{revision_arg}' spans "
                        f"{range_count if range_count >= 0 else 'an unreportable number of'} commits: reading diff "
                        f"hunks for the newest {RANGE_HUNK_COMMIT_CAP} only. The older commits of the range keep "
                        f"their DAG rows and get m_commit_file rows from a name-status pass, but no "
                        f"m_bridge_commit_tag rows."
                    )
        max_flag = [f"-n{effective_limit}"] if effective_limit else []
        # ~1,800 commits/s of -U0 patch text, with a floor for the pathological commit and a hard ceiling
        timeout = min(3600.0, max(300.0, (effective_limit or range_count) / 100.0))

        fmt_spec = (
            f"{RECORD_SEP}%H{FIELD_SEP}%P{FIELD_SEP}%an{FIELD_SEP}%ae{FIELD_SEP}%at{FIELD_SEP}"
            f"%cn{FIELD_SEP}%ce{FIELD_SEP}%ct{FIELD_SEP}%s{FIELD_SEP}%B{BODY_END_SEP}"
        )

        git_args = [
            "log",
            "-p",
            "-U0",
            "--full-index",
            f"--format={fmt_spec}",
        ] + max_flag + [revision_arg, "--"]

        raw_output = self._run_git(git_args, max_output_bytes=1024 * 1024 * 1024, timeout=timeout)
        if not raw_output:
            return [], {}

        commits, file_hunks_map = self.parse_commit_log_with_hunks(raw_output)
        if capped and not limit:
            skip_count = len(commits)
            remain_args = [
                "log",
                f"--format={fmt_spec}",
                "--name-status",
                f"--skip={skip_count}",
                revision_arg,
                "--",
            ]
            # A name-status log lists a few thousand commits per second; an unsized range gets the long timeout
            remaining = range_count - skip_count if range_count > 0 else 0
            remain_timeout = min(3600.0, max(300.0, remaining / 500.0)) if remaining > 0 else 1800.0
            remain_out = self._run_git(remain_args, max_output_bytes=512 * 1024 * 1024, timeout=remain_timeout)
            if remain_out:
                older_commits = self.parse_commit_log(remain_out)
                commits.extend(older_commits)

        return commits, file_hunks_map

    @classmethod
    def parse_commit_log(cls, raw_output: str) -> list[GitCommit]:
        """Parse raw git log output into a list of GitCommit objects."""
        if not raw_output:
            return []

        commits: list[GitCommit] = []

        # Support delimited binary separator format or text delimiter format
        if RECORD_SEP in raw_output:
            raw_chunks = raw_output.split(RECORD_SEP)
        else:
            raw_chunks = [c for c in raw_output.split("COMMIT_DELIM_START_") if c.strip()]

        for chunk in raw_chunks:
            # str.strip() treats the \x1c-\x1f separators as whitespace; stripping them would drop
            # the BODY_END_SEP of merge commits (which have no file list after it)
            chunk = chunk.strip(" \t\r\n")
            if not chunk:
                continue

            parents: list[str] = []
            if RECORD_SEP in raw_output or FIELD_SEP in chunk:
                if BODY_END_SEP not in chunk:
                    continue
                main_part, file_part = chunk.split(BODY_END_SEP, 1)
                fields = main_part.split(FIELD_SEP)
                if len(fields) < 8:
                    continue

                if len(fields) >= 9:
                    # New format with %P
                    commit_hash = fields[0].strip()
                    parents_raw = fields[1].strip()
                    parents = [p for p in parents_raw.split() if p]
                    author_name = fields[2].strip()
                    author_email = fields[3].strip()
                    try:
                        author_date = int(fields[4].strip())
                    except ValueError:
                        author_date = 0

                    committer_name = fields[5].strip()
                    committer_email = fields[6].strip()
                    try:
                        committer_date = int(fields[7].strip())
                    except ValueError:
                        committer_date = 0

                    subject = fields[8].strip()
                    message = fields[9].strip() if len(fields) > 9 else subject
                else:
                    # Legacy 8-field format without %P
                    commit_hash = fields[0].strip()
                    author_name = fields[1].strip()
                    author_email = fields[2].strip()
                    try:
                        author_date = int(fields[3].strip())
                    except ValueError:
                        author_date = 0

                    committer_name = fields[4].strip()
                    committer_email = fields[5].strip()
                    try:
                        committer_date = int(fields[6].strip())
                    except ValueError:
                        committer_date = 0

                    subject = fields[7].strip()
                    message = fields[8].strip() if len(fields) > 8 else subject
            else:
                # Text key-value format
                commit_hash = ""
                author_name = ""
                author_email = ""
                author_date = 0
                committer_name = ""
                committer_email = ""
                committer_date = 0
                subject = ""
                message = ""
                file_part = ""

                lines = chunk.splitlines()
                idx = 0
                while idx < len(lines):
                    line = lines[idx]
                    if line.startswith("HASH:"):
                        commit_hash = line[5:].strip()
                    elif line.startswith(("PARENTS:", "PARENT:")):
                        parents = [p for p in line.split(":", 1)[1].strip().split() if p]
                    elif line.startswith("ANAME:"):
                        author_name = line[6:].strip()
                    elif line.startswith("AEMAIL:"):
                        author_email = line[7:].strip()
                    elif line.startswith("ADATE:"):
                        try:
                            author_date = int(line[6:].strip())
                        except ValueError:
                            author_date = 0
                    elif line.startswith("CNAME:"):
                        committer_name = line[6:].strip()
                    elif line.startswith("CEMAIL:"):
                        committer_email = line[7:].strip()
                    elif line.startswith("CDATE:"):
                        try:
                            committer_date = int(line[6:].strip())
                        except ValueError:
                            committer_date = 0
                    elif line.startswith("SUBJ:"):
                        subject = line[5:].strip()
                    elif line.startswith("BODY_START"):
                        idx += 1
                        body_lines = []
                        while idx < len(lines) and not lines[idx].startswith("BODY_END"):
                            body_lines.append(lines[idx])
                            idx += 1
                        message = "\n".join(body_lines).strip()
                    elif line.startswith("COMMIT_DELIM_END"):
                        pass
                    elif line.startswith(("M\t", "A\t", "D\t", "R\t")):
                        file_part += line + "\n"
                    idx += 1

            if not commit_hash:
                continue

            # Extract merge metadata
            is_merge, req_name, req_email, merged_branch, merged_summaries = cls.extract_merge_metadata(
                subject, message, parents
            )

            # Parse contributors (Author, Committer, Requester, Merger, Trailers)
            contributors: list[GitContributor] = []
            seen_contributors: set[tuple[str, str, int]] = set()

            if is_merge:
                # 1. Merger (Role 10)
                if author_name or author_email:
                    contributors.append(
                        GitContributor(
                            name=author_name or author_email,
                            email=author_email,
                            role=CommitRole.MERGED_BY,
                            priority=0,
                        )
                    )
                    seen_contributors.add((author_name.lower(), author_email.lower(), int(CommitRole.MERGED_BY)))

                # 2. Pull Requester / Submitter (Role 11)
                if req_name or req_email:
                    r_name = req_name or req_email
                    r_mail = req_email or ""
                    r_key = (r_name.lower(), r_mail.lower(), int(CommitRole.REQUESTED_BY))
                    if r_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=r_name,
                                email=r_mail,
                                role=CommitRole.REQUESTED_BY,
                                priority=1,
                            )
                        )
                        seen_contributors.add(r_key)

                # 3. Committer (Role 2) if distinct from author
                if (committer_name or committer_email) and (committer_name != author_name or committer_email != author_email):
                    c_key = (committer_name.lower(), committer_email.lower(), int(CommitRole.COMMITTER))
                    if c_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=committer_name or committer_email,
                                email=committer_email,
                                role=CommitRole.COMMITTER,
                                priority=2,
                            )
                        )
                        seen_contributors.add(c_key)
            else:
                # 1. Author (Role 1)
                if author_name or author_email:
                    contributors.append(
                        GitContributor(
                            name=author_name or author_email,
                            email=author_email,
                            role=CommitRole.AUTHOR,
                            priority=0,
                        )
                    )
                    seen_contributors.add((author_name.lower(), author_email.lower(), int(CommitRole.AUTHOR)))

                # 2. Committer (Role 2)
                if (committer_name or committer_email) and (committer_name != author_name or committer_email != author_email):
                    c_key = (committer_name.lower(), committer_email.lower(), int(CommitRole.COMMITTER))
                    if c_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=committer_name or committer_email,
                                email=committer_email,
                                role=CommitRole.COMMITTER,
                                priority=1,
                            )
                        )
                        seen_contributors.add(c_key)

            # 4. Trailers in commit message body
            priority_idx = len(contributors)
            for trailer_tag, t_name, t_email in iter_trailers(message):
                for t_role in CommitRole.roles_from_trailer_prefix(trailer_tag):
                    t_key = (t_name.lower(), t_email.lower(), int(t_role))
                    if t_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=t_name or t_email,
                                email=t_email,
                                role=t_role,
                                priority=priority_idx,
                            )
                        )
                        seen_contributors.add(t_key)
                        priority_idx += 1

            # Fixes and reverts
            fixes_commits = [m.group(1).lower() for m in FIXES_PATTERN.finditer(message)]
            reverted_commits = [m.group(1).lower() for m in REVERT_PATTERN.finditer(message)]

            # 5. Parse modified files
            modified_files: list[tuple[str, str]] = []
            file_blobs: dict[str, tuple[str, str, str]] = {}
            for file_line in file_part.strip().splitlines():
                file_line = file_line.strip()
                if not file_line:
                    continue
                if file_line.startswith(":"):
                    parts = file_line.split("\t")
                    if len(parts) >= 2:
                        meta = parts[0].split()
                        if len(meta) >= 5:
                            status = meta[4]
                            ch_type = status[0].upper()
                            old_sha = meta[2].strip("0")
                            new_sha = meta[3].strip("0")
                            if ch_type in ("R", "C") and len(parts) >= 3:
                                old_p = parts[1].strip()
                                new_p = parts[2].strip()
                            else:
                                new_p = parts[1].strip()
                                old_p = "" if ch_type == "A" else new_p
                            modified_files.append((ch_type, new_p))
                            if old_sha or new_sha or old_p != new_p:
                                file_blobs[new_p] = (old_p, old_sha, new_sha)
                else:
                    parts = file_line.split("\t")
                    if len(parts) >= 2:
                        change_type = parts[0][0].upper()
                        if change_type in ("R", "C") and len(parts) >= 3:
                            old_path = parts[1].strip()
                            file_path = parts[2].strip()
                            modified_files.append((change_type, file_path))
                            file_blobs[file_path] = (old_path, "", "")
                        else:
                            file_path = parts[-1].strip()
                            modified_files.append((change_type, file_path))

            commits.append(
                GitCommit(
                    commit_hash=commit_hash,
                    author_name=author_name,
                    author_email=author_email,
                    author_date=author_date,
                    committer_name=committer_name,
                    committer_email=committer_email,
                    committer_date=committer_date,
                    subject=subject,
                    message=message,
                    contributors=contributors,
                    files=modified_files,
                    file_blobs=file_blobs,
                    is_merge=is_merge,
                    parents=parents,
                    merge_requester_name=req_name,
                    merge_requester_email=req_email,
                    merged_branch=merged_branch,
                    merged_commits_summary=merged_summaries,
                    fixes_commits=fixes_commits,
                    reverted_commits=reverted_commits,
                )
            )

        return commits

    @classmethod
    def parse_commit_log_with_hunks(
        cls,
        raw_output: str,
    ) -> tuple[list[GitCommit], dict[str, list[tuple[int, int, str]]]]:
        """Parse raw git log output with patch diffs into commits and a file-to-hunks interval map.

        Returns:
            Tuple of (commits_list, file_hunks_map) where file_hunks_map is:
            {file_path: [(start, end, commit_hash), ...]}, lines (and paths) of the newest version in the
            range (`_HeadLineMapper`).
        """
        if not raw_output:
            return [], {}

        commits: list[GitCommit] = []
        file_hunks_map: dict[str, list[tuple[int, int, str]]] = {}
        hunk_header_re = _HUNK_HEADER_RE
        # Hunk lines are moved into the coordinates of the range's newest version, where the tags are
        head_mapper = _HeadLineMapper()

        if RECORD_SEP in raw_output:
            raw_chunks = raw_output.split(RECORD_SEP)
        else:
            raw_chunks = [c for c in raw_output.split("COMMIT_DELIM_START_") if c.strip()]

        for chunk in raw_chunks:
            # str.strip() treats the \x1c-\x1f separators as whitespace; stripping them would drop
            # the BODY_END_SEP of merge commits (which have no file list after it)
            chunk = chunk.strip(" \t\r\n")
            if not chunk:
                continue

            main_part = ""
            patch_part = ""
            parents: list[str] = []

            if RECORD_SEP in raw_output or FIELD_SEP in chunk:
                if BODY_END_SEP in chunk:
                    main_part, patch_part = chunk.split(BODY_END_SEP, 1)
                else:
                    main_part = chunk
                fields = main_part.split(FIELD_SEP)
                if len(fields) < 8:
                    continue

                if len(fields) >= 9:
                    # New format with %P
                    commit_hash = fields[0].strip()
                    parents_raw = fields[1].strip()
                    parents = [p for p in parents_raw.split() if p]
                    author_name = fields[2].strip()
                    author_email = fields[3].strip()
                    try:
                        author_date = int(fields[4].strip())
                    except ValueError:
                        author_date = 0

                    committer_name = fields[5].strip()
                    committer_email = fields[6].strip()
                    try:
                        committer_date = int(fields[7].strip())
                    except ValueError:
                        committer_date = 0

                    subject = fields[8].strip()
                    message = fields[9].strip() if len(fields) > 9 else subject
                else:
                    # Legacy 8-field format without %P
                    commit_hash = fields[0].strip()
                    author_name = fields[1].strip()
                    author_email = fields[2].strip()
                    try:
                        author_date = int(fields[3].strip())
                    except ValueError:
                        author_date = 0

                    committer_name = fields[4].strip()
                    committer_email = fields[5].strip()
                    try:
                        committer_date = int(fields[6].strip())
                    except ValueError:
                        committer_date = 0

                    subject = fields[7].strip()
                    message = fields[8].strip() if len(fields) > 8 else subject
            else:
                # Text key-value format
                commit_hash = ""
                author_name = ""
                author_email = ""
                author_date = 0
                committer_name = ""
                committer_email = ""
                committer_date = 0
                subject = ""
                message = ""
                patch_part = ""

                lines = chunk.splitlines()
                idx = 0
                while idx < len(lines):
                    line = lines[idx]
                    if line.startswith("HASH:"):
                        commit_hash = line[5:].strip()
                    elif line.startswith(("PARENTS:", "PARENT:")):
                        parents = [p for p in line.split(":", 1)[1].strip().split() if p]
                    elif line.startswith("ANAME:"):
                        author_name = line[6:].strip()
                    elif line.startswith("AEMAIL:"):
                        author_email = line[7:].strip()
                    elif line.startswith("ADATE:"):
                        try:
                            author_date = int(line[6:].strip())
                        except ValueError:
                            author_date = 0
                    elif line.startswith("CNAME:"):
                        committer_name = line[6:].strip()
                    elif line.startswith("CEMAIL:"):
                        committer_email = line[7:].strip()
                    elif line.startswith("CDATE:"):
                        try:
                            committer_date = int(line[6:].strip())
                        except ValueError:
                            committer_date = 0
                    elif line.startswith("SUBJ:"):
                        subject = line[5:].strip()
                    elif line.startswith("BODY_START"):
                        idx += 1
                        body_lines = []
                        while idx < len(lines) and not lines[idx].startswith("BODY_END"):
                            body_lines.append(lines[idx])
                            idx += 1
                        message = "\n".join(body_lines).strip()
                    elif line.startswith("COMMIT_DELIM_END"):
                        pass
                    else:
                        patch_part += line + "\n"
                    idx += 1

            if not commit_hash:
                continue

            # Extract merge metadata
            is_merge, req_name, req_email, merged_branch, merged_summaries = cls.extract_merge_metadata(
                subject, message, parents
            )

            # Parse contributors (Author, Committer, Requester, Merger, Trailers)
            contributors: list[GitContributor] = []
            seen_contributors: set[tuple[str, str, int]] = set()

            if is_merge:
                # 1. Merger (Role 10)
                if author_name or author_email:
                    contributors.append(
                        GitContributor(
                            name=author_name or author_email,
                            email=author_email,
                            role=CommitRole.MERGED_BY,
                            priority=0,
                        )
                    )
                    seen_contributors.add((author_name.lower(), author_email.lower(), int(CommitRole.MERGED_BY)))

                # 2. Pull Requester / Submitter (Role 11)
                if req_name or req_email:
                    r_name = req_name or req_email
                    r_mail = req_email or ""
                    r_key = (r_name.lower(), r_mail.lower(), int(CommitRole.REQUESTED_BY))
                    if r_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=r_name,
                                email=r_mail,
                                role=CommitRole.REQUESTED_BY,
                                priority=1,
                            )
                        )
                        seen_contributors.add(r_key)

                # 3. Committer (Role 2) if distinct from author
                if (committer_name or committer_email) and (committer_name != author_name or committer_email != author_email):
                    c_key = (committer_name.lower(), committer_email.lower(), int(CommitRole.COMMITTER))
                    if c_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=committer_name or committer_email,
                                email=committer_email,
                                role=CommitRole.COMMITTER,
                                priority=2,
                            )
                        )
                        seen_contributors.add(c_key)
            else:
                # 1. Author (Role 1)
                if author_name or author_email:
                    contributors.append(
                        GitContributor(
                            name=author_name or author_email,
                            email=author_email,
                            role=CommitRole.AUTHOR,
                            priority=0,
                        )
                    )
                    seen_contributors.add((author_name.lower(), author_email.lower(), int(CommitRole.AUTHOR)))

                # 2. Committer (Role 2)
                if (committer_name or committer_email) and (committer_name != author_name or committer_email != author_email):
                    c_key = (committer_name.lower(), committer_email.lower(), int(CommitRole.COMMITTER))
                    if c_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=committer_name or committer_email,
                                email=committer_email,
                                role=CommitRole.COMMITTER,
                                priority=1,
                            )
                        )
                        seen_contributors.add(c_key)

            priority_idx = len(contributors)
            for trailer_tag, t_name, t_email in iter_trailers(message):
                for t_role in CommitRole.roles_from_trailer_prefix(trailer_tag):
                    t_key = (t_name.lower(), t_email.lower(), int(t_role))
                    if t_key not in seen_contributors:
                        contributors.append(
                            GitContributor(
                                name=t_name or t_email,
                                email=t_email,
                                role=t_role,
                                priority=priority_idx,
                            )
                        )
                        seen_contributors.add(t_key)
                        priority_idx += 1

            # Fixes and reverts
            fixes_commits = [m.group(1).lower() for m in FIXES_PATTERN.finditer(message)]
            reverted_commits = [m.group(1).lower() for m in REVERT_PATTERN.finditer(message)]

            # Parse patch_part for modified files, their blobs and diff hunks (header lines are only read
            # between `diff --git` and the first hunk, so patch content can never be taken for them)
            modified_files: list[tuple[str, str]] = []
            file_blobs: dict[str, tuple[str, str, str]] = {}
            entries: list[dict[str, Any]] = []
            seen_files: set[str] = set()
            cur: dict[str, Any] | None = None
            in_header = False

            for line in patch_part.splitlines():
                if line.startswith("diff --git "):
                    rest = line[len("diff --git "):]
                    guess = rest[2: 2 + (len(rest) - 5) // 2] if len(rest) >= 5 and rest.startswith("a/") else ""
                    if guess and f"a/{guess} b/{guess}" == rest:
                        a_path, b_path = guess, guess
                    else:
                        m_paths = _DIFF_GIT_RE.match(line)
                        a_path, b_path = m_paths.groups() if m_paths else ("", line.split(" ")[-1].removeprefix("b/"))
                    cur = {"path": b_path, "old_path": a_path, "change": "M", "hunks": [], "old_sha": "", "new_sha": ""}
                    entries.append(cur)
                    in_header = True
                    continue
                if cur is not None and in_header and not line.startswith("@@ "):
                    if line.startswith("new file mode"):
                        cur["change"] = "A"
                    elif line.startswith("deleted file mode"):
                        cur["change"] = "D"
                    elif line.startswith("copy from "):
                        cur["change"] = "C"
                        cur["old_path"] = line.split(" from ", 1)[1]
                    elif line.startswith("copy to "):
                        cur["change"] = "C"
                        cur["path"] = line.split(" to ", 1)[1]
                    elif line.startswith("rename from "):
                        cur["change"] = "R"
                        cur["old_path"] = line.split(" from ", 1)[1]
                    elif line.startswith("rename to "):
                        cur["change"] = "R"
                        cur["path"] = line.split(" to ", 1)[1]
                    elif line.startswith("similarity index"):
                        pass
                    elif line.startswith("index "):
                        shas = line[6:].split(" ", 1)[0].split("..")
                        if len(shas) == 2:
                            cur["old_sha"], cur["new_sha"] = (sha if sha.strip("0") else "" for sha in shas)
                    continue
                if line.startswith("@@ ") and cur is not None:
                    in_header = False
                    h_match = hunk_header_re.match(line)
                    if h_match:
                        os_, oc, ns, nc = h_match.groups()
                        cur["hunks"].append((int(os_), 1 if oc is None else int(oc), int(ns), 1 if nc is None else int(nc)))
                elif line.startswith(("M\t", "A\t", "D\t", "R", "C", ":")) and cur is None:
                    # Name-status records or raw records (logs without patches)
                    if line.startswith(":"):
                        # :100644 100644 <old_sha> <new_sha> <status>\t<path1>[\t<path2>]
                        parts = line.split("\t")
                        if len(parts) >= 2:
                            meta = parts[0].split()
                            if len(meta) >= 5:
                                status = meta[4]
                                ch_type = status[0].upper()
                                old_sha = meta[2].strip("0")
                                new_sha = meta[3].strip("0")
                                if ch_type in ("R", "C") and len(parts) >= 3:
                                    old_p = parts[1].strip()
                                    new_p = parts[2].strip()
                                else:
                                    new_p = parts[1].strip()
                                    old_p = "" if ch_type == "A" else new_p
                                if new_p not in seen_files:
                                    seen_files.add(new_p)
                                    modified_files.append((ch_type, new_p))
                                if old_sha or new_sha or old_p != new_p:
                                    file_blobs[new_p] = (old_p, old_sha, new_sha)
                    else:
                        parts = line.strip().split("\t")
                        if len(parts) >= 2:
                            ch_type = parts[0][0].upper()
                            if ch_type in ("R", "C") and len(parts) >= 3:
                                old_p = parts[1].strip()
                                new_p = parts[2].strip()
                                if new_p not in seen_files:
                                    seen_files.add(new_p)
                                    modified_files.append((ch_type, new_p))
                                file_blobs[new_p] = (old_p, "", "")
                            else:
                                f_path = parts[-1].strip()
                                if f_path not in seen_files:
                                    seen_files.add(f_path)
                                    modified_files.append((ch_type, f_path))

            for entry in entries:
                path = entry["path"]
                if path not in seen_files:
                    seen_files.add(path)
                    modified_files.append((entry["change"], path))
                if entry["old_sha"] or entry["new_sha"]:
                    file_blobs[path] = (entry["old_path"], entry["old_sha"], entry["new_sha"])
                head, ranges = head_mapper.add(path, entry["old_path"], entry["change"], entry["hunks"])
                if ranges:
                    file_hunks_map.setdefault(head, []).extend((s_, e_, commit_hash) for s_, e_ in ranges)

            commits.append(
                GitCommit(
                    commit_hash=commit_hash,
                    author_name=author_name,
                    author_email=author_email,
                    author_date=author_date,
                    committer_name=committer_name,
                    committer_email=committer_email,
                    committer_date=committer_date,
                    subject=subject,
                    message=message,
                    contributors=contributors,
                    files=modified_files,
                    file_blobs=file_blobs,
                    is_merge=is_merge,
                    parents=parents,
                    merge_requester_name=req_name,
                    merge_requester_email=req_email,
                    merged_branch=merged_branch,
                    merged_commits_summary=merged_summaries,
                    fixes_commits=fixes_commits,
                    reverted_commits=reverted_commits,
                )
            )

        return commits, file_hunks_map

    @classmethod
    def extract_diff_hunks_from_patch(cls, patch_text: str, file_path: str, commit_hash: str = "") -> list[CommitDiffHunk]:
        """Parse unified diff hunks from a git diff or patch text."""
        hunks: list[CommitDiffHunk] = []
        hunk_header_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)

        for match in hunk_header_re.finditer(patch_text):
            old_s, old_c, new_s, new_c = match.groups()
            old_start = int(old_s)
            old_count = int(old_c) if old_c is not None else 1
            new_start = int(new_s)
            new_count = int(new_c) if new_c is not None else 1

            hunks.append(
                CommitDiffHunk(
                    commit_hash=commit_hash,
                    file_path=file_path,
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                )
            )
        return hunks

    def extract_file_hunks(
        self,
        old_rev: str | None,
        new_rev: str,
        file_path: str,
    ) -> list[CommitDiffHunk]:
        """Extract diff hunk line intervals for a file across commits, in the newest version's lines."""
        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        if not old_rev or old_rev == EMPTY_TREE or str(old_rev) == "0":
            rev_spec = [new_rev]
        else:
            rev_spec = [f"{old_rev}..{new_rev}"]

        git_args = [
            "log",
            "-p",
            "-U0",
            "--format=%x1e%H",
        ] + rev_spec + ["--", file_path]
        mapper = _HeadLineMapper()

        raw_output = self._run_git(git_args)
        if not raw_output:
            return []

        hunks: list[CommitDiffHunk] = []
        chunks = raw_output.split(RECORD_SEP)

        for chunk in chunks:
            # str.strip() treats the \x1c-\x1f separators as whitespace; stripping them would drop
            # the BODY_END_SEP of merge commits (which have no file list after it)
            chunk = chunk.strip(" \t\r\n")
            if not chunk:
                continue
            lines = chunk.splitlines()
            if not lines:
                continue
            commit_hash = lines[0].strip()
            parsed_hunks = self.extract_diff_hunks_from_patch(chunk, file_path, commit_hash=commit_hash)
            # Newest commit first: move each commit's hunks into the newest version's lines
            _head, ranges = mapper.add(
                file_path, file_path, "M", [(h.old_start, h.old_count, h.new_start, h.new_count) for h in parsed_hunks]
            )
            for h, (start, end) in zip(parsed_hunks, ranges):
                h.new_start, h.new_count = start, end - start + 1
            hunks.extend(parsed_hunks)

        return hunks

    @staticmethod
    def _tag_fields(tag: Any) -> tuple[int, int, int, int] | None:
        """`(tag_id, fid, line_s, line_e)` of a tag given as a tuple/list or a dict."""
        if isinstance(tag, dict):
            line_s = tag.get("line_s", 1)
            return tag.get("tag_id", 0), tag.get("fid", 0), line_s, tag.get("line_e", line_s)
        if isinstance(tag, (tuple, list)):
            line_s = tag[2] if len(tag) > 2 else 1
            return (
                tag[0] if len(tag) > 0 else 0,
                tag[1] if len(tag) > 1 else 0,
                line_s,
                tag[3] if len(tag) > 3 else line_s,
            )
        return None

    @staticmethod
    def _hunk_matcher(resolved_hunks: list[tuple[int, int, int]]) -> Any:
        """Function `(line_s, line_e) -> commit ids of the hunks overlapping it` (a scan for a few hunks)."""
        if len(resolved_hunks) <= 8:
            return lambda ls, le: [cid for hs, he, cid in resolved_hunks if not (le < hs or ls > he)]
        return _HunkIndex(resolved_hunks).overlapping

    def map_tags_to_commits(
        self,
        tags: Sequence[Any],  # [(tag_id, fid, line_s, line_e), ...] or list of dicts
        file_path: str = "",
        old_rev: str | None = None,
        new_rev: str = "",
        commit_hash_to_id: dict[str, int] | None = None,
        hunks_by_commit: dict[int, list[CommitDiffHunk]] | None = None,
        file_hunks_map: dict[str, list[tuple[int, int, str]]] | None = None,
    ) -> Any:
        """Match each tag to the commits whose hunks intersect the tag's line range.

        A tag no hunk touches (unchanged code of a changed file) is linked to no commit. Hunk lines must be in
        the coordinates of the version the tags belong to (`parse_commit_log_with_hunks()` and
        `extract_file_hunks()` map them there).

        Returns:
            List of tuples `[(cid, fid, tag_id), ...]` or, with `hunks_by_commit`, a mapping
            `{tag_id: [cid, ...]}`.
        """
        if not tags:
            return [] if hunks_by_commit is None else {}
        commit_hash_to_id = commit_hash_to_id or {}

        if hunks_by_commit is not None:
            # Mode A: hunks already grouped by commit id (e.g. unit tests)
            resolved = [
                (h.new_start, h.new_start + max(1, h.new_count) - 1, cid)
                for cid, hunk_list in hunks_by_commit.items()
                for h in hunk_list
            ]
            match = self._hunk_matcher(resolved)
            tag_to_cids: dict[int, list[int]] = {}
            for t in tags:
                fields = self._tag_fields(t)
                if fields is not None:
                    tag_id, _fid, line_s, line_e = fields
                    tag_to_cids[tag_id] = list(dict.fromkeys(match(line_s, line_e)))
            return tag_to_cids

        if file_hunks_map is not None:
            # Mode B: the in-memory hunk map of one diff stream
            raw = [(hs, he, commit_hash_to_id.get(c_hash)) for hs, he, c_hash in file_hunks_map.get(file_path, ())]
        else:
            # Mode C: one `git log -p` for this file
            raw = [
                (h.new_start, h.new_start + max(1, h.new_count) - 1, commit_hash_to_id.get(h.commit_hash))
                for h in self.extract_file_hunks(old_rev, new_rev, file_path)
            ]
        resolved = [(hs, he, cid) for hs, he, cid in raw if cid is not None]
        if not resolved:
            return []

        match = self._hunk_matcher(resolved)
        bridges: list[tuple[int, int, int]] = []
        seen: set[tuple[int, int]] = set()
        for t in tags:
            fields = self._tag_fields(t)
            if fields is None:
                continue
            tag_id, fid, line_s, line_e = fields
            for cid in dict.fromkeys(match(line_s, line_e)):
                if (cid, tag_id) not in seen:
                    seen.add((cid, tag_id))
                    bridges.append((cid, fid, tag_id))
        return bridges
