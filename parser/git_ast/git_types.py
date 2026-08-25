"""git_types.py - Type definitions and dataclasses for Git commit and blame parsing."""
from enum import IntEnum
from dataclasses import dataclass, field


class CommitRole(IntEnum):
    """Contributor role types within a git commit."""
    AUTHOR = 1
    COMMITTER = 2
    CO_DEVELOPED_BY = 3
    SIGNED_OFF_BY = 4
    REVIEWED_BY = 5
    ACKED_BY = 6
    TESTED_BY = 7
    REPORTED_BY = 8
    SUGGESTED_BY = 9
    MERGED_BY = 10
    REQUESTED_BY = 11
    OTHER = 12

    @classmethod
    def from_trailer_prefix(cls, prefix: str) -> "CommitRole":
        """Map git commit trailer tag prefix to CommitRole enum."""
        p = prefix.strip().lower()
        if "co-developed-by" in p:
            return cls.CO_DEVELOPED_BY
        if "signed-off-by" in p:
            return cls.SIGNED_OFF_BY
        if "reviewed-by" in p:
            return cls.REVIEWED_BY
        if "acked-by" in p:
            return cls.ACKED_BY
        if "tested-by" in p:
            return cls.TESTED_BY
        if "reported-by" in p:
            return cls.REPORTED_BY
        if "suggested-by" in p:
            return cls.SUGGESTED_BY
        if "merged-by" in p or "merger" in p:
            return cls.MERGED_BY
        if "requested-by" in p or "pull-from" in p:
            return cls.REQUESTED_BY
        return cls.OTHER


@dataclass(slots=True)
class GitContributor:
    """Represents an individual contributor (author, committer, reviewer, signer, requester, merger) on a commit."""
    name: str
    email: str
    role: CommitRole
    priority: int = 0
    person_id: int | None = None


@dataclass(slots=True)
class GitCommit:
    """Represents a parsed git commit with full metadata, merge lineage, and contributors."""
    commit_hash: str
    author_name: str
    author_email: str
    author_date: int
    committer_name: str
    committer_email: str
    committer_date: int
    subject: str
    message: str
    contributors: list[GitContributor] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)  # (change_type, file_path)
    commit_id: int | None = None
    author_person_id: int | None = None
    committer_person_id: int | None = None
    is_merge: bool = False
    parents: list[str] = field(default_factory=list)
    merge_requester_name: str | None = None
    merge_requester_email: str | None = None
    merged_branch: str | None = None
    merged_commits_summary: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CommitDiffHunk:
    """Represents a unified diff hunk in a commit for spatial tag coordinate mapping."""
    commit_hash: str
    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
