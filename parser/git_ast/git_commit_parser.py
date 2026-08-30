"""git_commit_parser.py - High-performance Git commit and multi-contributor parser.

Extracts commit histories, author/committer information, trailer contributors
(Co-developed-by, Signed-off-by, Reviewed-by, etc.), modified file maps,
and correlates code occurrence tags (m_tag) to commit revisions.
"""
import re
import os
import subprocess
import logging
from pathlib import Path
from typing import Any, Sequence

from core.globalstuff import G, SafeDataType
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

# Regex to extract structured git trailers
TRAILER_PATTERN = re.compile(
    r"^(Co-developed-by|Signed-off-by|Reviewed-by|Acked-by|Tested-by|Reported-by|Suggested-by|Merged-by|Requested-by):\s*(.+?)\s*<([^>\n\r]+)>",
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


class GitCommitParser:
    """Parser for git commits, merge lineages, multi-person trailers, and tag-to-commit mappings."""

    def __init__(self, repo_dir: str | Path | None = None) -> None:
        if repo_dir is None:
            self.repo_dir = str(getattr(G, "linux_directory", Path("linux")))
        else:
            self.repo_dir = str(repo_dir)

    def _run_git(self, cmd: list[str], max_output_bytes: int = 50 * 1024 * 1024) -> str:
        """Execute git command safely with timeout and return decoded stdout."""
        full_cmd = ["git", "-C", self.repo_dir] + cmd
        try:
            res = subprocess.run(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
            if res.returncode != 0:
                err_msg = res.stderr.decode("utf-8", errors="replace").strip()
                logger.warning(f"Git command '{' '.join(full_cmd[:4])}...' returned {res.returncode}: {err_msg}")
                return ""
            out = res.stdout[:max_output_bytes]
            return out.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            logger.error(f"Git command timed out: {' '.join(full_cmd[:4])}")
            return ""
        except Exception as e:
            logger.error(f"Failed to execute git command: {e}")
            return ""

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
    ) -> list[GitCommit]:
        """Extract all commits between old_rev and new_rev with full metadata and contributors.

        Args:
            old_rev: Prior version git ref or tag (if None or empty tree, inspects up to new_rev).
            new_rev: Target version git ref or tag (e.g. 'v3.0').
            limit: Maximum commits to parse (optional limit for initial tags if needed).

        Returns:
            List of GitCommit objects with parsed trailers and modified file lists.
        """
        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        if not old_rev or old_rev == EMPTY_TREE or str(old_rev) == "0":
            # Initial version: if limit not specified, get recent commits for initial release tag
            revision_arg = new_rev
            max_flag = [f"-n{limit}"] if limit else ["-n1000"]
        else:
            revision_arg = f"{old_rev}..{new_rev}"
            max_flag = [f"-n{limit}"] if limit else []

        fmt_spec = (
            f"{RECORD_SEP}%H{FIELD_SEP}%P{FIELD_SEP}%an{FIELD_SEP}%ae{FIELD_SEP}%at{FIELD_SEP}"
            f"%cn{FIELD_SEP}%ce{FIELD_SEP}%ct{FIELD_SEP}%s{FIELD_SEP}%B{BODY_END_SEP}"
        )

        git_args = [
            "log",
            f"--format={fmt_spec}",
            "--name-status",
        ] + max_flag + [revision_arg, "--"]

        raw_output = self._run_git(git_args)
        if not raw_output:
            return []

        return self.parse_commit_log(raw_output)

    def parse_version_commits_with_hunks(
        self,
        old_rev: str | None,
        new_rev: str,
        limit: int | None = None,
    ) -> tuple[list[GitCommit], dict[str, list[tuple[int, int, str]]]]:
        """Extract all commits and diff hunks in a single unified git log stream.

        Args:
            old_rev: Prior version git ref or tag (if None or empty tree, inspects up to new_rev).
            new_rev: Target version git ref or tag (e.g. 'v3.0').
            limit: Maximum commits to parse.

        Returns:
            Tuple of:
              - List of GitCommit objects with parsed trailers and modified file lists.
              - Dictionary mapping file_path -> [(new_start, new_end, commit_hash), ...]
        """
        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        if not old_rev or old_rev == EMPTY_TREE or str(old_rev) == "0":
            revision_arg = new_rev
            max_flag = [f"-n{limit}"] if limit else ["-n1000"]
        else:
            revision_arg = f"{old_rev}..{new_rev}"
            max_flag = [f"-n{limit}"] if limit else []

        fmt_spec = (
            f"{RECORD_SEP}%H{FIELD_SEP}%P{FIELD_SEP}%an{FIELD_SEP}%ae{FIELD_SEP}%at{FIELD_SEP}"
            f"%cn{FIELD_SEP}%ce{FIELD_SEP}%ct{FIELD_SEP}%s{FIELD_SEP}%B{BODY_END_SEP}"
        )

        git_args = [
            "log",
            "-p",
            "-U0",
            f"--format={fmt_spec}",
        ] + max_flag + [revision_arg, "--"]

        raw_output = self._run_git(git_args)
        if not raw_output:
            return [], {}

        return self.parse_commit_log_with_hunks(raw_output)

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
            chunk = chunk.strip()
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
            for match in TRAILER_PATTERN.finditer(message):
                trailer_tag, t_name, t_email = match.groups()
                t_role = CommitRole.from_trailer_prefix(trailer_tag)
                t_name = t_name.strip()
                t_email = t_email.strip()
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

            # 5. Parse modified files
            modified_files: list[tuple[str, str]] = []
            for file_line in file_part.strip().splitlines():
                file_line = file_line.strip()
                if not file_line:
                    continue
                parts = file_line.split("\t")
                if len(parts) >= 2:
                    change_type = parts[0][0].upper()
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
                    is_merge=is_merge,
                    parents=parents,
                    merge_requester_name=req_name,
                    merge_requester_email=req_email,
                    merged_branch=merged_branch,
                    merged_commits_summary=merged_summaries,
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
            {file_path: [(new_start, new_end, commit_hash), ...]}
        """
        if not raw_output:
            return [], {}

        commits: list[GitCommit] = []
        file_hunks_map: dict[str, list[tuple[int, int, str]]] = {}
        hunk_header_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

        if RECORD_SEP in raw_output:
            raw_chunks = raw_output.split(RECORD_SEP)
        else:
            raw_chunks = [c for c in raw_output.split("COMMIT_DELIM_START_") if c.strip()]

        for chunk in raw_chunks:
            chunk = chunk.strip()
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
            for match in TRAILER_PATTERN.finditer(message):
                trailer_tag, t_name, t_email = match.groups()
                t_role = CommitRole.from_trailer_prefix(trailer_tag)
                t_name = t_name.strip()
                t_email = t_email.strip()
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

            # Parse patch_part for modified files and diff hunks
            modified_files: list[tuple[str, str]] = []
            seen_files: set[str] = set()
            current_file = None
            current_change = "M"

            for line in patch_part.splitlines():
                line_str = line.strip()
                if not line_str:
                    continue

                if line_str.startswith("diff --git "):
                    if current_file and current_file not in seen_files:
                        seen_files.add(current_file)
                        modified_files.append((current_change, current_file))
                    parts = line_str.split(" ")
                    if len(parts) >= 4:
                        b_path = parts[3]
                        current_file = b_path[2:] if b_path.startswith("b/") else b_path
                        current_change = "M"
                elif line_str.startswith("new file mode"):
                    current_change = "A"
                elif line_str.startswith("deleted file mode"):
                    current_change = "D"
                elif line_str.startswith("similarity index"):
                    current_change = "R"
                elif line_str.startswith("--- ") and "/dev/null" in line_str:
                    current_change = "A"
                elif line_str.startswith("+++ "):
                    if "/dev/null" in line_str:
                        current_change = "D"
                    elif current_file and current_file not in seen_files:
                        seen_files.add(current_file)
                        modified_files.append((current_change, current_file))
                elif line_str.startswith("@@ ") and current_file:
                    h_match = hunk_header_re.match(line_str)
                    if h_match:
                        if current_file not in seen_files:
                            seen_files.add(current_file)
                            modified_files.append((current_change, current_file))
                        ns = int(h_match.group(1))
                        nc = int(h_match.group(2)) if h_match.group(2) is not None else 1
                        ne = ns + max(1, nc) - 1
                        if current_file not in file_hunks_map:
                            file_hunks_map[current_file] = []
                        file_hunks_map[current_file].append((ns, ne, commit_hash))
                elif line_str.startswith(("M\t", "A\t", "D\t", "R\t")):
                    parts = line_str.split("\t")
                    if len(parts) >= 2:
                        c_type = parts[0][0].upper()
                        f_path = parts[-1].strip()
                        if f_path not in seen_files:
                            seen_files.add(f_path)
                            modified_files.append((c_type, f_path))

            if current_file and current_file not in seen_files:
                modified_files.append((current_change, current_file))

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
                    is_merge=is_merge,
                    parents=parents,
                    merge_requester_name=req_name,
                    merge_requester_email=req_email,
                    merged_branch=merged_branch,
                    merged_commits_summary=merged_summaries,
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
        """Extract diff hunk line intervals for a file across commits."""
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

        raw_output = self._run_git(git_args)
        if not raw_output:
            return []

        hunks: list[CommitDiffHunk] = []
        chunks = raw_output.split(RECORD_SEP)

        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            lines = chunk.splitlines()
            if not lines:
                continue
            commit_hash = lines[0].strip()
            parsed_hunks = self.extract_diff_hunks_from_patch(chunk, file_path, commit_hash=commit_hash)
            hunks.extend(parsed_hunks)

        return hunks

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
        """Match each tag to all intersecting commits that modified the tag's line range.

        Returns:
            List of tuples `[(cid, fid, tag_id), ...]` or mapping dict `{tag_id: [cid, ...]}`.
        """
        if not tags:
            return [] if hunks_by_commit is None else {}

        # Mode A: Direct hunk-to-commit mapping dictionary passed (e.g. in unit tests)
        if hunks_by_commit is not None:
            tag_to_cids: dict[int, list[int]] = {}
            for t in tags:
                tid = t["tag_id"] if isinstance(t, dict) else (t[0] if len(t) > 0 else 0)
                ls = t["line_s"] if isinstance(t, dict) else (t[2] if len(t) > 2 else 1)
                le = t["line_e"] if isinstance(t, dict) else (t[3] if len(t) > 3 else ls)
                matched_cids: list[int] = []

                for cid, hunk_list in hunks_by_commit.items():
                    for h in hunk_list:
                        hs = h.new_start
                        he = h.new_start + max(1, h.new_count) - 1
                        if not (le < hs or ls > he):
                            if cid not in matched_cids:
                                matched_cids.append(cid)
                tag_to_cids[tid] = matched_cids
            return tag_to_cids

        # Mode B: Pre-indexed single-pass in-memory file hunks map
        if file_hunks_map is not None:
            if commit_hash_to_id is None:
                commit_hash_to_id = {}

            hunks = file_hunks_map.get(file_path, [])
            tag_commit_bridges: list[tuple[int, int, int]] = []
            seen_bridges: set[tuple[int, int]] = set()

            for t in tags:
                if isinstance(t, dict):
                    tag_id = t.get("tag_id", 0)
                    fid = t.get("fid", 0)
                    line_s = t.get("line_s", 1)
                    line_e = t.get("line_e", line_s)
                elif isinstance(t, (tuple, list)):
                    tag_id = t[0] if len(t) > 0 else 0
                    fid = t[1] if len(t) > 1 else 0
                    line_s = t[2] if len(t) > 2 else 1
                    line_e = t[3] if len(t) > 3 else line_s
                else:
                    continue

                matched_commit_ids: set[int] = set()

                for hunk_s, hunk_e, c_hash in hunks:
                    if not (line_e < hunk_s or line_s > hunk_e):
                        cid = commit_hash_to_id.get(c_hash)
                        if cid is not None:
                            matched_commit_ids.add(cid)

                if not matched_commit_ids and hunks:
                    first_cid = commit_hash_to_id.get(hunks[0][2])
                    if first_cid is not None:
                        matched_commit_ids.add(first_cid)

                for cid in matched_commit_ids:
                    bridge_key = (cid, tag_id)
                    if bridge_key not in seen_bridges:
                        seen_bridges.add(bridge_key)
                        tag_commit_bridges.append((cid, fid, tag_id))

            return tag_commit_bridges

        # Mode C: Fallback to individual file hunk extraction
        if commit_hash_to_id is None:
            commit_hash_to_id = {}

        hunks_list = self.extract_file_hunks(old_rev, new_rev, file_path)
        tag_commit_bridges = []
        seen_bridges = set()

        for t in tags:
            if isinstance(t, dict):
                tag_id = t.get("tag_id", 0)
                fid = t.get("fid", 0)
                line_s = t.get("line_s", 1)
                line_e = t.get("line_e", line_s)
            elif isinstance(t, (tuple, list)):
                tag_id = t[0] if len(t) > 0 else 0
                fid = t[1] if len(t) > 1 else 0
                line_s = t[2] if len(t) > 2 else 1
                line_e = t[3] if len(t) > 3 else line_s
            else:
                continue

            matched_commit_ids = set()

            for h in hunks_list:
                hunk_s = h.new_start
                hunk_e = h.new_start + max(1, h.new_count) - 1
                if not (line_e < hunk_s or line_s > hunk_e):
                    cid = commit_hash_to_id.get(h.commit_hash)
                    if cid is not None:
                        matched_commit_ids.add(cid)

            if not matched_commit_ids and hunks_list:
                first_cid = commit_hash_to_id.get(hunks_list[0].commit_hash)
                if first_cid is not None:
                    matched_commit_ids.add(first_cid)

            for cid in matched_commit_ids:
                bridge_key = (cid, tag_id)
                if bridge_key not in seen_bridges:
                    seen_bridges.add(bridge_key)
                    tag_commit_bridges.append((cid, fid, tag_id))

        return tag_commit_bridges
