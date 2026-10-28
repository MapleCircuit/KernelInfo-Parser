"""parser/mailing_list/ml_resolver.py - Deterministic Commit-to-Email & Pull Request Resolver."""
from __future__ import annotations

import logging
from pathlib import Path
import re
import subprocess
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

    def extract_commit_features_from_git(self, commit_sha: str) -> CommitFeatures:
        """Extract all lookup features directly from the local git repository."""
        if not self.git_dir:
            raise ValueError("Git repository path not configured on resolver.")

        fmt = "%H%x00%an%x00%ae%x00%at%x00%ct%x00%s%x00%B%x00%P"
        out = subprocess.check_output(
            ["git", f"--git-dir={self.git_dir}", "show", "-s", f"--format={fmt}", commit_sha],
            text=True,
            errors="replace",
        )
        parts = out.split("\x00")
        sha = parts[0]
        an = parts[1]
        ae = parts[2]
        at = int(parts[3]) if parts[3].isdigit() else 0
        ct = int(parts[4]) if parts[4].isdigit() else 0
        subj = parts[5]
        body = parts[6] if len(parts) > 6 else ""
        parents = parts[7].strip().split() if len(parts) > 7 else []

        is_merge = len(parents) > 1

        # Extract Message-IDs from trailers
        mids = self.LINK_MID_RE.findall(body) + self.MSGID_TRAILER_RE.findall(body)
        dedup_mids = list(dict.fromkeys(m.strip("<>") for m in mids))

        # Check for pull request hints in merge commit message
        merge_branch = ""
        pull_name = ""
        pull_email = ""
        if is_merge:
            m_pull = self.PULL_FROM_RE.search(body)
            if m_pull:
                pull_name = m_pull.group(1).strip()
                pull_email = m_pull.group(2).strip() if m_pull.group(2) else ""
            m_bm = self.BRANCH_MERGE_RE.search(subj) or self.BRANCH_MERGE_RE.search(body)
            if m_bm:
                merge_branch = m_bm.group(1).strip()

        # Compute patch-ids across all 4 diff algorithms for non-merge commits
        patch_ids: list[str] = []
        file_blobs: list[tuple[str, str, str]] = []

        if not is_merge:
            for algo in ("myers", "histogram", "patience", "minimal"):
                try:
                    diff_proc = subprocess.Popen(
                        ["git", f"--git-dir={self.git_dir}", "show", f"--diff-algorithm={algo}", sha],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    pid_out = subprocess.check_output(
                        ["git", "patch-id", "--stable"],
                        stdin=diff_proc.stdout,
                        text=True,
                        stderr=subprocess.DEVNULL,
                    ).strip()
                    diff_proc.wait()
                    if pid_out:
                        pid = pid_out.split()[0]
                        if pid not in patch_ids:
                            patch_ids.append(pid)
                except Exception as exc:
                    logger.debug("Failed patch-id algo %s for %s: %s", algo, sha[:8], exc)

            # Extract pre/post file blob SHA-1s via diff-tree
            try:
                dt_out = subprocess.check_output(
                    ["git", f"--git-dir={self.git_dir}", "diff-tree", "-r", "--no-commit-id", sha],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                for line in dt_out.splitlines():
                    if "\t" not in line:
                        continue
                    meta, filepath = line.split("\t", 1)
                    meta_parts = meta.split(" ")
                    if len(meta_parts) >= 5:
                        pre_b = meta_parts[2]
                        post_b = meta_parts[3]
                        if not pre_b.startswith("0000000") or not post_b.startswith("0000000"):
                            file_blobs.append((pre_b, post_b, filepath))
            except Exception as exc:
                logger.debug("Failed diff-tree for %s: %s", sha[:8], exc)

        return CommitFeatures(
            sha=sha,
            author_name=an,
            author_email=ae,
            author_ts=at,
            commit_ts=ct,
            subject=subj,
            message=body,
            trailer_mids=dedup_mids,
            patch_ids=patch_ids,
            file_blobs=file_blobs,
            is_merge=is_merge,
            merge_branch=merge_branch,
            pull_from_name=pull_name,
            pull_from_email=pull_email,
        )

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
            terms: list[str] = ["s:PULL"]
            if feat.merge_branch:
                terms.append(f's:"{feat.merge_branch}"')
            if feat.pull_from_name:
                terms.append(f'f:"{feat.pull_from_name}"')

            q_str = " AND ".join(terms)
            try:
                flags = xapian.QueryParser.FLAG_BOOLEAN | xapian.QueryParser.FLAG_PHRASE
                query = self.store.qp.parse_query(q_str, flags)
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

        # Fallback to subject scans in over.sqlite3
        if feat.merge_branch:
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
                q = xapian.Query(
                    xapian.Query.OP_OR, [f"XPATCHID{pid}" for pid in feat.patch_ids]
                )
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
                    post_queries = [
                        xapian.Query(xapian.Query.OP_WILDCARD, f"XDFPOST{post_b[:10].lower()}")
                        for _, post_b, _ in feat.file_blobs
                        if not post_b.startswith("0000000")
                    ]
                    if post_queries:
                        q = xapian.Query(xapian.Query.OP_OR, post_queries)
                        cands = self.store.execute_xapian(q)
                        matched = self._pick_best_candidate(cands, feat.commit_ts)
                        if matched:
                            matched_tier = int(MLMatchTier.BlobHash)
                except Exception as exc:
                    logger.debug("Tier 3 blob error for %s: %s", feat.sha[:8], exc)

            if not matched and hasattr(self.store, "lookup_by_blob"):
                for _, post_b, _ in feat.file_blobs:
                    if post_b and not post_b.startswith("0000000"):
                        cands = self.store.lookup_by_blob(post_b)
                        matched = self._pick_best_candidate(cands, feat.commit_ts)
                        if matched:
                            matched_tier = int(MLMatchTier.BlobHash)
                            break

        # ---------------------------------------------------------------------
        # Tier 4: Reverse Commit-SHA Lookup in Applied-Bot Replies (XNQ)
        # ---------------------------------------------------------------------
        if not matched and feat.file_blobs and HAS_XAPIAN and self.store.xap_db is not None and self.store.qp is not None and xapian is not None:
            try:
                primary_file = feat.file_blobs[0][2]
                q_str = f"nq:{feat.sha[:12]} AND dfn:{primary_file}"
                flags = xapian.QueryParser.FLAG_BOOLEAN | xapian.QueryParser.FLAG_PHRASE
                cands = self.store.execute_xapian(self.store.qp.parse_query(q_str, flags))
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
            # Query over table for matching subject
            core_subject = re.sub(r"\[.*?\]", "", feat.subject).strip()
            if len(core_subject) > 10:
                cands = self.store.search_over_subjects(core_subject[:40], limit=5)
                matched = self._pick_best_candidate(cands, feat.commit_ts)
                if matched:
                    matched_tier = int(MLMatchTier.MetadataFallback)

        if not matched:
            return None

        return self._reconstruct_series_context(feat.sha, matched_tier, matched)

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
            elif self.PATCH_NUM_RE.search(msg.subject) and msg.author == patch_msg.author:
                ctx.sibling_patches.append(msg)

        # Fallback if cover letter was root of references but lacked [0/N] tag
        if not ctx.cover_letter and patch_msg.references:
            root_cand = self.store.get_by_mid(patch_msg.references[0])
            if root_cand and self.COVER_LETTER_RE.search(root_cand.subject):
                ctx.cover_letter = root_cand

        return ctx

    def resolve_batch(self, commits: list[CommitFeatures]) -> dict[str, ResolvedCommitContext]:
        """Resolve a batch of commits, returning a mapping of commit_sha -> ResolvedCommitContext."""
        results: dict[str, ResolvedCommitContext] = {}
        for feat in commits:
            try:
                res = self.resolve(feat)
                if res:
                    results[feat.sha] = res
            except Exception as exc:
                logger.warning("Error resolving commit %s: %s", feat.sha[:8], exc)
        return results

    # Alias for convenience
    resolve_commit = resolve


# Module-level convenience function
extract_patch_metadata = KernelCommitEmailResolver.extract_patch_metadata

