"""parser/mailing_list/ml_types.py - Data types & Enums for Mailing List resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import re
from typing import Any


class MLRelationType(IntEnum):
    """Relation of the mailing list email to the target git commit."""
    Patch = 1
    CoverLetter = 2
    SiblingPatch = 3
    PullRequest = 4


class MLMatchTier(IntEnum):
    """Waterfall lookup tier and confidence for the matched email."""
    Trailer = 1         # Authoritative Link: or Message-Id: trailer
    PatchID = 2         # Cryptographic git patch-id --stable match
    BlobHash = 3        # Post-image (XDFPOST) or Pre-image (XDFPRE) blob match
    BotReply = 4        # Bot / maintainer reply referencing the applied commit SHA (XNQ)
    MetadataFallback = 5# Subject, author, and date window fallback
    PullRequest = 6     # Merge commit matched to [GIT PULL] announcement


@dataclass
class SmsgRecord:
    """Unpacked overview metadata from public-inbox over.sqlite3 ddd record."""
    num: int = 0
    tid: int = 0
    sid: int = 0
    ts: int = 0
    ds: int = 0
    subject: str = ""
    author: str = ""
    references: list[str] = field(default_factory=list)
    mids: list[str] = field(default_factory=list)
    git_blob: str | None = None
    bytes: int = 0
    lines: int = 0

    def __init__(
        self,
        num: int = 0,
        tid: int = 0,
        sid: int = 0,
        ts: int = 0,
        ds: int = 0,
        subject: str = "",
        author: str = "",
        references: list[str] | None = None,
        mids: list[str] | None = None,
        git_blob: str | None = None,
        bytes: int = 0,
        lines: int = 0,
        # Aliases:
        subj: str | None = None,
        from_hdr: str | None = None,
        blob: str | None = None,
    ):
        self.num = num
        self.tid = tid
        self.sid = sid
        self.ts = ts
        self.ds = ds
        self.subject = subj if subj is not None else subject
        self.author = from_hdr if from_hdr is not None else author
        self.references = list(references or [])
        self.mids = list(mids or [])
        self.git_blob = blob if blob is not None else git_blob
        self.bytes = bytes
        self.lines = lines

    @property
    def subj(self) -> str:
        return self.subject

    @subj.setter
    def subj(self, val: str) -> None:
        self.subject = val

    @property
    def from_hdr(self) -> str:
        return self.author

    @from_hdr.setter
    def from_hdr(self, val: str) -> None:
        self.author = val

    @property
    def blob(self) -> str | None:
        return self.git_blob

    @blob.setter
    def blob(self, val: str | None) -> None:
        self.git_blob = val

    def to_dict(self) -> dict[str, Any]:
        return {
            "num": self.num,
            "tid": self.tid,
            "sid": self.sid,
            "ts": self.ts,
            "ds": self.ds,
            "subject": self.subject,
            "author": self.author,
            "references": self.references,
            "mids": self.mids,
            "git_blob": self.git_blob,
            "bytes": self.bytes,
            "lines": self.lines,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SmsgRecord:
        return cls(
            num=int(data.get("num", 0)),
            tid=int(data.get("tid", 0)),
            sid=int(data.get("sid", 0)),
            ts=int(data.get("ts", 0)),
            ds=int(data.get("ds", 0)),
            subject=str(data.get("subject", "")),
            author=str(data.get("author", "")),
            references=list(data.get("references", [])),
            mids=list(data.get("mids", [])),
            git_blob=data.get("git_blob"),
            bytes=int(data.get("bytes", 0)),
            lines=int(data.get("lines", 0)),
        )


@dataclass
class CommitFeatures:
    """Git commit properties extracted for 5-tier lookup."""
    sha: str = ""
    author_name: str = ""
    author_email: str = ""
    author_ts: int = 0
    commit_ts: int = 0
    subject: str = ""
    message: str = ""
    trailer_mids: list[str] = field(default_factory=list)
    patch_ids: list[str] = field(default_factory=list)
    file_blobs: list[tuple[str, str, str]] = field(default_factory=list)  # (pre_blob, post_blob, filepath)
    is_merge: bool = False
    merge_branch: str = ""
    pull_from_name: str = ""
    pull_from_email: str = ""
    commit_id: int = 0

    def __init__(
        self,
        sha: str = "",
        author_name: str = "",
        author_email: str = "",
        author_ts: int = 0,
        commit_ts: int = 0,
        subject: str = "",
        message: str = "",
        trailer_mids: list[str] | None = None,
        patch_ids: list[str] | None = None,
        file_blobs: list[tuple[str, str, str]] | None = None,
        is_merge: bool = False,
        merge_branch: str = "",
        pull_from_name: str = "",
        pull_from_email: str = "",
        commit_id: int = 0,
        # Backward/convenience aliases:
        commit_hash: str | None = None,
        author_date: int | None = None,
        commit_message: str | None = None,
        patch_id: str | None = None,
        blob_hashes: list[str] | None = None,
    ):
        self.sha = commit_hash if commit_hash is not None else sha
        self.author_name = author_name
        self.author_email = author_email
        self.author_ts = author_date if author_date is not None else author_ts
        self.commit_ts = commit_ts or self.author_ts
        self.subject = subject
        self.message = commit_message if commit_message is not None else message
        self.trailer_mids = list(trailer_mids or [])
        self.patch_ids = list(patch_ids or [])
        if patch_id and patch_id not in self.patch_ids:
            self.patch_ids.append(patch_id)
        self.file_blobs = list(file_blobs or [])
        if blob_hashes:
            for b in blob_hashes:
                self.file_blobs.append(("", b, ""))
        self.is_merge = is_merge
        self.merge_branch = merge_branch
        self.pull_from_name = pull_from_name
        self.pull_from_email = pull_from_email
        self.commit_id = commit_id

        # Auto-extract trailers if not provided
        if not self.trailer_mids and self.message:
            link_mids = re.findall(
                r"(?:lore\.kernel\.org/(?:r/|[^/\s]+/)|patch\.msgid\.link/)([^\s/>]+)",
                self.message,
                re.IGNORECASE,
            )
            msgid_mids = re.findall(
                r"^Message-I[dD]:\s*<?([^>\s]+)>?",
                self.message,
                re.IGNORECASE | re.MULTILINE,
            )
            self.trailer_mids = list(dict.fromkeys(m.strip("<> \t\r\n") for m in (link_mids + msgid_mids)))

        # Auto-detect merge commit and merge_branch if not provided
        if not self.is_merge and self.subject:
            if self.subject.startswith("Merge ") or "Merge tag" in self.subject or "Merge branch" in self.subject:
                self.is_merge = True

        if not self.merge_branch and self.is_merge and self.subject:
            m_bm = re.search(r"Merge (?:branch|tag)\s+'([^']+)'", self.subject, re.IGNORECASE)
            if m_bm:
                self.merge_branch = m_bm.group(1)

    @property
    def commit_hash(self) -> str:
        return self.sha

    @property
    def author_date(self) -> int:
        return self.author_ts

    @property
    def commit_message(self) -> str:
        return self.message

    @property
    def patch_id(self) -> str | None:
        return self.patch_ids[0] if self.patch_ids else None

    @property
    def blob_hashes(self) -> list[str]:
        return [b[1] for b in self.file_blobs if len(b) > 1 and b[1]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "author_ts": self.author_ts,
            "commit_ts": self.commit_ts,
            "subject": self.subject,
            "message": self.message,
            "trailer_mids": self.trailer_mids,
            "patch_ids": self.patch_ids,
            "file_blobs": self.file_blobs,
            "is_merge": self.is_merge,
            "merge_branch": self.merge_branch,
            "pull_from_name": self.pull_from_name,
            "pull_from_email": self.pull_from_email,
            "commit_id": self.commit_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommitFeatures:
        return cls(
            sha=str(data.get("sha", "")),
            author_name=str(data.get("author_name", "")),
            author_email=str(data.get("author_email", "")),
            author_ts=int(data.get("author_ts", 0)),
            commit_ts=int(data.get("commit_ts", 0)),
            subject=str(data.get("subject", "")),
            message=str(data.get("message", "")),
            trailer_mids=list(data.get("trailer_mids", [])),
            patch_ids=list(data.get("patch_ids", [])),
            file_blobs=[(b[0], b[1], b[2]) for b in data.get("file_blobs", [])],
            is_merge=bool(data.get("is_merge", False)),
            merge_branch=str(data.get("merge_branch", "")),
            pull_from_name=str(data.get("pull_from_name", "")),
            pull_from_email=str(data.get("pull_from_email", "")),
            commit_id=int(data.get("commit_id", 0)),
        )


@dataclass
class ResolvedCommitContext:
    """Resolved mailing list thread and patch series context for a commit."""
    commit_sha: str = ""
    matched_tier: int = 0
    patch_email: SmsgRecord | None = None
    cover_letter: SmsgRecord | None = None
    pull_request: SmsgRecord | None = None
    sibling_patches: list[SmsgRecord] = field(default_factory=list)
    patch_version: int = 1
    patch_idx: int = 0
    patch_total: int = 0
    commit_id: int = 0

    def __init__(
        self,
        commit_sha: str = "",
        matched_tier: int = 0,
        patch_email: SmsgRecord | None = None,
        cover_letter: SmsgRecord | None = None,
        pull_request: SmsgRecord | None = None,
        sibling_patches: list[SmsgRecord] | None = None,
        patch_version: int = 1,
        patch_idx: int = 0,
        patch_total: int = 0,
        commit_id: int = 0,
        commit_hash: str | None = None,
    ):
        self.commit_sha = commit_hash if commit_hash is not None else commit_sha
        self.matched_tier = int(matched_tier)
        self.patch_email = patch_email
        self.cover_letter = cover_letter
        self.pull_request = pull_request
        self.sibling_patches = list(sibling_patches or [])
        self.patch_version = int(patch_version)
        self.patch_idx = int(patch_idx)
        self.patch_total = int(patch_total)
        self.commit_id = commit_id

    @property
    def commit_hash(self) -> str:
        return self.commit_sha

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "matched_tier": int(self.matched_tier),
            "patch_email": self.patch_email.to_dict() if self.patch_email else None,
            "cover_letter": self.cover_letter.to_dict() if self.cover_letter else None,
            "pull_request": self.pull_request.to_dict() if self.pull_request else None,
            "sibling_patches": [s.to_dict() for s in self.sibling_patches],
            "patch_version": self.patch_version,
            "patch_idx": self.patch_idx,
            "patch_total": self.patch_total,
            "commit_id": self.commit_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResolvedCommitContext:
        patch_email = SmsgRecord.from_dict(data["patch_email"]) if data.get("patch_email") else None
        cover_letter = SmsgRecord.from_dict(data["cover_letter"]) if data.get("cover_letter") else None
        pull_request = SmsgRecord.from_dict(data["pull_request"]) if data.get("pull_request") else None
        sibling_patches = [SmsgRecord.from_dict(s) for s in data.get("sibling_patches", [])]
        return cls(
            commit_sha=str(data.get("commit_sha", "")),
            matched_tier=int(data.get("matched_tier", 0)),
            patch_email=patch_email,
            cover_letter=cover_letter,
            pull_request=pull_request,
            sibling_patches=sibling_patches,
            patch_version=int(data.get("patch_version", 1)),
            patch_idx=int(data.get("patch_idx", 0)),
            patch_total=int(data.get("patch_total", 0)),
            commit_id=int(data.get("commit_id", 0)),
        )
