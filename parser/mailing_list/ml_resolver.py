"""parser/mailing_list/ml_resolver.py - Deterministic Commit-to-Email & Pull Request Resolver."""
from __future__ import annotations

import logging
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from parser.mailing_list.ml_store import HAS_XAPIAN, PublicInboxLocalStore, xapian
from parser.mailing_list.ml_types import (
    CommitFeatures,
    MLMatchTier,
    ResolvedCommitContext,
    SmsgRecord,
)

logger = logging.getLogger(__name__)


class KernelCommitEmailResolver:
    """Implements the 5-tier commit-to-email waterfall, thread reconstruction, and pull request matching."""

    # A commit can touch hundreds of files. Each blob probed costs a Xapian wildcard
    # expansion (and, without an index, a scan), so only the first few are tried: a
    # patch that reached the list matches on any one of its hunks.
    MAX_BLOB_PROBES = 4
    # Siblings recorded for a series; a thread can hold thousands of messages.
    MAX_SIBLING_PATCHES = 128
    # Hex characters a blob id must have before it may be turned into a query term.
    MIN_BLOB_PREFIX = 8
    # Terms a single wildcard may expand to. A wildcard is expanded into an OR of every
    # matching term, so an unbounded one is a memory bomb on a multi-million message
    # archive, however selective the pattern looks.
    MAX_WILDCARD_EXPANSION = 1000

    LINK_MID_RE = re.compile(
        r"(?:lore\.kernel\.org/(?:r/|[^/\s]+/)|patch\.msgid\.link/)([^\s/>]+)",
        re.IGNORECASE,
    )
    MSGID_TRAILER_RE = re.compile(
        r"^Message-I[dD]:\s*<?([^>\s]+)>?",
        re.IGNORECASE | re.MULTILINE,
    )
    COVER_LETTER_RE = re.compile(r"\[[^\]]*\b0+/\d+[^\]]*\]")
    PATCH_NUM_RE = re.compile(r"\[[^\]]*\b(\d+)/(\d+)[^\]]*\]")
    PATCH_VERSION_RE = re.compile(r"\[[^\]]*\bv(\d+)\b[^\]]*\]", re.IGNORECASE)
    GIT_PULL_SUBJECT_RE = re.compile(r"\[(?:GIT\s+)?PULL(?:\]|\s+)", re.IGNORECASE)
    REPLY_SUBJECT_RE = re.compile(r"^\s*(?:re|aw|fwd?)\s*:", re.IGNORECASE)
    PATCH_SUBJECT_RE = re.compile(r"\[[^\]]*\bpatch\b", re.IGNORECASE)

    PULL_FROM_RE = re.compile(
        r"Pull\s+(?:.*?\s+)?from\s+([^:\n<]+?)\s*(?:<([^>\n\r]+)>)?\s*:",
        re.IGNORECASE,
    )
    BRANCH_MERGE_RE = re.compile(
        r"Merge (?:branch|tag)\s+'([^']+)'\s+of\s+(\S+)",
        re.IGNORECASE,
    )

    def __init__(self, store: PublicInboxLocalStore, kernel_git_dir: str | Path | None = None):
        self.store = store
        self.git_dir = str(kernel_git_dir) if kernel_git_dir else ""

    @staticmethod
    def extract_patch_metadata(subject: str) -> tuple[int, int, int]:
        """Extract (version, index, total) from patch subject line."""
        # Version: default 1 unless v2, v3, etc. explicitly present
        v_match = KernelCommitEmailResolver.PATCH_VERSION_RE.search(subject)
        version = int(v_match.group(1)) if v_match else 1

        # Index / Total: e.g. [PATCH 2/5] -> (2, 5). Single patch -> (1, 1). Cover letter [0/5] -> (0, 5).
        n_match = KernelCommitEmailResolver.PATCH_NUM_RE.search(subject)
        if n_match:
            idx = int(n_match.group(1))
            total = int(n_match.group(2))
        else:
            idx = 1
            total = 1
        return version, idx, total

    @classmethod
    def _normalize_blob(cls, blob: str | None) -> str:
        """Lowercased blob id, or "" when it cannot be queried.

        A deleted file has no post-image and an added file has no pre-image, and the
        extractor represents the missing side as an empty string (older callers use all
        zeroes), so both must be rejected before a blob becomes a query term.
        """
        b = (blob or "").strip().lower()
        if len(b) < cls.MIN_BLOB_PREFIX or b.strip("0") == "":
            return ""
        if any(c not in "0123456789abcdef" for c in b):
            return ""
        return b

    @classmethod
    def _blob_term_query(cls, prefix: str, blob: str | None) -> Any | None:
        """Bounded wildcard query for one blob term, or None if the blob is unusable.

        An unusable blob must never reach Xapian as a bare prefix: a wildcard on
        `XDFPOST` alone expands to every blob term in the archive, which allocated over
        a gigabyte per commit and grew the daemon until the kernel killed it.
        """
        clean = cls._normalize_blob(blob)
        if not clean or xapian is None:
            return None
        pattern = f"{prefix}{clean[:10]}"
        limit_first = getattr(xapian.Query, "WILDCARD_LIMIT_FIRST", None)
        if limit_first is not None:
            return xapian.Query(
                xapian.Query.OP_WILDCARD, pattern, cls.MAX_WILDCARD_EXPANSION, limit_first
            )
        return xapian.Query(xapian.Query.OP_WILDCARD, pattern)

    @classmethod
    def blob_queries_for(cls, file_blobs: list[tuple[str, str, str]]) -> list[Any]:
        """One bounded blob query per changed file, post-image preferred.

        A modification is matched on its post-image, which is what a patch applied to
        the list produces; a deletion has none, so its pre-image (`XDFPRE`) is used
        instead of the file being skipped.
        """
        queries: list[Any] = []
        for entry in file_blobs[: cls.MAX_BLOB_PROBES]:
            pre_b, post_b = (entry[0], entry[1]) if len(entry) > 1 else ("", "")
            q = cls._blob_term_query("XDFPOST", post_b) or cls._blob_term_query("XDFPRE", pre_b)
            if q is not None:
                queries.append(q)
        return queries

    @staticmethod
    def _pick_best_candidate(candidates: list[SmsgRecord], commit_ts: int) -> SmsgRecord | None:
        """Disambiguate candidates: prioritize most recent revision prior to commit merge."""
        if not candidates:
            return None
        if commit_ts > 0:
            # Candidate must be before commit (with up to 2 days grace for clock skew)
            # and within 180 days prior to merge
            valid = [c for c in candidates if (commit_ts - 180 * 86400) <= c.ds <= (commit_ts + 2 * 86400)]
            if not valid:
                return None
            return max(valid, key=lambda c: c.ds)
        return max(candidates, key=lambda c: c.ds)

    def resolve_pull_request(self, feat: CommitFeatures) -> SmsgRecord | None:
        """Locate [GIT PULL] announcement email for a merge commit."""
        # Check explicit Link: trailers first
        for mid in feat.trailer_mids:
            rec = self.store.get_by_mid(mid)
            if rec and self.GIT_PULL_SUBJECT_RE.search(rec.subject):
                return rec

        # Probabilistic / text search if Xapian is available
        if HAS_XAPIAN and self.store.xap_db is not None and self.store.qp is not None and xapian is not None:
            # Quotes in a branch or author name would break out of the phrase and
            # change the query's shape, so they are stripped rather than interpolated.
            branch = (feat.merge_branch or "").replace('"', " ").strip()
            author = (feat.pull_from_name or "").replace('"', " ").strip()
            terms: list[str] = ["s:PULL"]
            if branch:
                terms.append(f's:"{branch}"')
            if author:
                terms.append(f'f:"{author}"')

            q_str = " AND ".join(terms)
            try:
                flags = xapian.QueryParser.FLAG_BOOLEAN | xapian.QueryParser.FLAG_PHRASE
                query = self.store.parse_query(q_str, flags)
                cands = self.store.execute_xapian(query, limit=10)
                best = self._pick_best_candidate(cands, feat.commit_ts)
                if best:
                    return best
            except Exception as exc:
                logger.debug("Pull request Xapian search error: %s", exc)

        # Check store search_pull_request if available
        if hasattr(self.store, "search_pull_request") and feat.merge_branch:
            cands = self.store.search_pull_request(feat.merge_branch)
            best = self._pick_best_candidate(cands, feat.commit_ts)
            if best:
                return best

        # Fallback to subject scans in over.sqlite3 (only without Xapian: the scan covers
        # just the newest overview rows and costs hundreds of decompressions per commit)
        if feat.merge_branch and not (HAS_XAPIAN and self.store.xap_db is not None):
            patterns = [feat.merge_branch]
            parts = re.split(r"[_\-/\s]+", feat.merge_branch)
            if parts and len(parts[0]) >= 3 and parts[0] not in patterns:
                patterns.append(parts[0])
            for pat in patterns:
                matched_recs: list[SmsgRecord] = []
                for rec in self.store.search_over_subjects(pat, limit=20):
                    if self.GIT_PULL_SUBJECT_RE.search(rec.subject):
                        matched_recs.append(rec)
                best = self._pick_best_candidate(matched_recs, feat.commit_ts)
                if best:
                    return best
        return None

    def resolve(self, feat: CommitFeatures) -> ResolvedCommitContext | None:
        """Execute 5-tier waterfall and thread reconstruction for a single commit."""
        matched: SmsgRecord | None = None
        matched_tier = 0
        pull_request_record: SmsgRecord | None = None

        # Special handling for Merge Commits
        if feat.is_merge:
            pull_request_record = self.resolve_pull_request(feat)
            if pull_request_record:
                return ResolvedCommitContext(
                    commit_sha=feat.sha,
                    matched_tier=int(MLMatchTier.PullRequest),
                    pull_request=pull_request_record,
                )
            return None

        # ---------------------------------------------------------------------
        # Tier 1: Direct Trailer Message-ID Lookup
        # ---------------------------------------------------------------------
        for mid in feat.trailer_mids:
            rec = self.store.get_by_mid(mid)
            if rec:
                matched = rec
                matched_tier = int(MLMatchTier.Trailer)
                break

        # ---------------------------------------------------------------------
        # Tier 2: Multi-Algorithm Stable Patch-ID Match
        # ---------------------------------------------------------------------
        if not matched and feat.patch_ids and HAS_XAPIAN and self.store.xap_db is not None and xapian is not None:
            try:
                patch_terms = [f"XPATCHID{pid.strip()}" for pid in feat.patch_ids if pid and pid.strip()]
                if not patch_terms:
                    raise ValueError("no usable patch ids")
                q = xapian.Query(xapian.Query.OP_OR, patch_terms)
                cands = self.store.execute_xapian(q)
                matched = self._pick_best_candidate(cands, feat.commit_ts)
                if matched:
                    matched_tier = int(MLMatchTier.PatchID)
            except Exception as exc:
                logger.debug("Tier 2 patch-id error for %s: %s", feat.sha[:8], exc)

        # ---------------------------------------------------------------------
        # Tier 3: Post-Image & Pre-Image File Blob Hash Match
        # ---------------------------------------------------------------------
        if not matched and feat.file_blobs:
            if HAS_XAPIAN and self.store.xap_db is not None and xapian is not None:
                try:
                    post_queries = self.blob_queries_for(feat.file_blobs)
                    if post_queries:
                        q = xapian.Query(xapian.Query.OP_OR, post_queries)
                        cands = self.store.execute_xapian(q)
                        matched = self._pick_best_candidate(cands, feat.commit_ts)
                        if matched:
                            matched_tier = int(MLMatchTier.BlobHash)
                except Exception as exc:
                    logger.debug("Tier 3 blob error for %s: %s", feat.sha[:8], exc)

            if not matched and hasattr(self.store, "lookup_by_blob"):
                for entry in feat.file_blobs[: self.MAX_BLOB_PROBES]:
                    pre_b, post_b = (entry[0], entry[1]) if len(entry) > 1 else ("", "")
                    blob = self._normalize_blob(post_b) or self._normalize_blob(pre_b)
                    if not blob:
                        continue
                    cands = self.store.lookup_by_blob(blob)
                    matched = self._pick_best_candidate(cands, feat.commit_ts)
                    if matched:
                        matched_tier = int(MLMatchTier.BlobHash)
                        break

        # ---------------------------------------------------------------------
        # Tier 4: Reverse Commit-SHA Lookup in Applied-Bot Replies (XNQ)
        # ---------------------------------------------------------------------
        if not matched and feat.file_blobs and HAS_XAPIAN and self.store.xap_db is not None and self.store.qp is not None and xapian is not None:
            try:
                primary_file = (feat.file_blobs[0][2] or "").strip().replace('"', " ").strip()
                if not primary_file:
                    raise ValueError("no file path available for the bot-reply lookup")
                q_str = f'nq:{feat.sha[:12]} AND dfn:"{primary_file}"' 
                flags = xapian.QueryParser.FLAG_BOOLEAN | xapian.QueryParser.FLAG_PHRASE
                cands = self.store.execute_xapian(self.store.parse_query(q_str, flags))
                for reply in cands:
                    for ref_mid in reversed(reply.references):
                        parent = self.store.get_by_mid(ref_mid)
                        if parent and "PATCH" in parent.subject:
                            matched = parent
                            matched_tier = int(MLMatchTier.BotReply)
                            break
                    if matched:
                        break
            except Exception as exc:
                logger.debug("Tier 4 bot reply error for %s: %s", feat.sha[:8], exc)

        # ---------------------------------------------------------------------
        # Tier 5: Constrained Structural Metadata Fallback
        # ---------------------------------------------------------------------
        if not matched and feat.subject:
            core_subject = re.sub(r"\[.*?\]", "", feat.subject).strip()
            if len(core_subject) > 10:
                cands = self.search_subject_candidates(core_subject)
                # The subject index also matches replies ("Re: [PATCH] ..."); prefer the
                # submission itself so the staged Message-ID is the patch, not a reply.
                for pool in self._subject_candidate_pools(cands):
                    matched = self._pick_best_candidate(pool, feat.commit_ts)
                    if matched:
                        matched_tier = int(MLMatchTier.MetadataFallback)
                        break

        if not matched:
            return None

        return self._reconstruct_series_context(feat.sha, matched_tier, matched)

    @classmethod
    def _subject_candidate_pools(cls, cands: list[SmsgRecord]) -> list[list[SmsgRecord]]:
        """Tier 5 preference order: patch submissions, then non-replies, then anything."""
        if not cands:
            return []
        originals = [c for c in cands if not cls.REPLY_SUBJECT_RE.match(c.subject)]
        patches = [c for c in originals if cls.PATCH_SUBJECT_RE.search(c.subject)]
        pools = [p for p in (patches, originals, cands) if p]
        # Drop duplicate pools so a candidate set is not scored twice
        unique: list[list[SmsgRecord]] = []
        for pool in pools:
            if not unique or len(pool) != len(unique[-1]):
                unique.append(pool)
        return unique

    def search_subject_candidates(self, core_subject: str, limit: int = 5) -> list[SmsgRecord]:
        """Tier 5 subject lookup.

        Prefers the indexed Xapian subject term; the `over.sqlite3` substring scan is
        only used when Xapian is unavailable, since that scan decompresses hundreds of
        overview rows per commit and dominates batch resolution time.
        """
        if HAS_XAPIAN and self.store.xap_db is not None and self.store.qp is not None and xapian is not None:
            phrase = core_subject[:60].replace('"', " ").strip()
            if phrase:
                try:
                    flags = xapian.QueryParser.FLAG_PHRASE | xapian.QueryParser.FLAG_BOOLEAN
                    query = self.store.parse_query(f's:"{phrase}"', flags)
                    return self.store.execute_xapian(query, limit=max(limit, 10))
                except Exception as exc:
                    logger.debug("Tier 5 Xapian subject search error: %s", exc)
            return []

        return self.store.search_over_subjects(core_subject[:40], limit=limit)

    def _reconstruct_series_context(
        self, commit_sha: str, matched_tier: int, patch_msg: SmsgRecord
    ) -> ResolvedCommitContext:
        """Fetch full thread from over.sqlite3 and classify cover letters & siblings."""
        thread_msgs = self.store.get_thread_by_tid(patch_msg.tid) if patch_msg.tid > 0 else []
        version, idx, total = self.extract_patch_metadata(patch_msg.subject)

        ctx = ResolvedCommitContext(
            commit_sha=commit_sha,
            matched_tier=matched_tier,
            patch_email=patch_msg,
            patch_version=version,
            patch_idx=idx,
            patch_total=total,
        )

        for msg in thread_msgs:
            if msg.num == patch_msg.num:
                continue
            if self.COVER_LETTER_RE.search(msg.subject):
                ctx.cover_letter = msg
            elif (
                len(ctx.sibling_patches) < self.MAX_SIBLING_PATCHES
                and self.PATCH_NUM_RE.search(msg.subject)
                and msg.author == patch_msg.author
            ):
                ctx.sibling_patches.append(msg)

        # Fallback if cover letter was root of references but lacked [0/N] tag
        if not ctx.cover_letter and patch_msg.references:
            root_cand = self.store.get_by_mid(patch_msg.references[0])
            if root_cand and self.COVER_LETTER_RE.search(root_cand.subject):
                ctx.cover_letter = root_cand

        return ctx

    def resolve_batch_until(
        self,
        commits: list[CommitFeatures],
        deadline: float | None = None,
        should_stop: Any = None,
    ) -> tuple[dict[str, ResolvedCommitContext], list[str]]:
        """Resolve commits until `deadline` (time.monotonic()) elapses.

        Returns (resolved, pending) where `pending` lists the SHAs that were not
        attempted because the budget ran out. The deadline, and the optional
        `should_stop` predicate (used by the daemon to stop under memory pressure), are
        checked between commits, so the caller gets partial results instead of a stalled
        request or an exhausted host.
        """
        results: dict[str, ResolvedCommitContext] = {}
        pending: list[str] = []

        for i, feat in enumerate(commits):
            out_of_time = deadline is not None and time.monotonic() >= deadline
            out_of_room = should_stop is not None and should_stop()
            if i > 0 and (out_of_time or out_of_room):
                pending = [c.sha for c in commits[i:]]
                logger.warning(
                    "%s exhausted after %d/%d commits; %d deferred",
                    "Memory budget" if out_of_room else "Time budget",
                    i,
                    len(commits),
                    len(pending),
                )
                break
            try:
                res = self.resolve(feat)
                if res:
                    results[feat.sha] = res
            except Exception as exc:
                logger.warning("Error resolving commit %s: %s", feat.sha[:8], exc)

        return results, pending

    def resolve_batch(self, commits: list[CommitFeatures]) -> dict[str, ResolvedCommitContext]:
        """Resolve a batch of commits, returning a mapping of commit_sha -> ResolvedCommitContext."""
        results, _pending = self.resolve_batch_until(commits, deadline=None)
        return results

    # Alias for convenience
    resolve_commit = resolve


# Module-level convenience function
extract_patch_metadata = KernelCommitEmailResolver.extract_patch_metadata

