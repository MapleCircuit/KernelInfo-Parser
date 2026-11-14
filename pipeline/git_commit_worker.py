"""Forked worker that parses the git commits of a version."""
from core.globalstuff import (
    G,
    COLOR,
)
import os
import sys
import subprocess as sp

from typing import Any
import tempfile
import pickle
from core.FileHandler import mark_owner
from pipeline.state import MF, _MP, gp, logger




_GIT_COMMIT_RESULT = "commits.pkl"


def _git_commit_worker(
    old_ver: str | int,
    ver: str,
    result_path: str,
    repo_dir: str | None = None,
    limit: int | None = None,
    known_count: int | None = None,
) -> None:
    """Worker process that parses git commits and diff hunks in parallel with file AST parsing.

    Also executes patch-id calculation, diff-tree blob extraction, and lore mailing-list resolution
    in the background, overlapping Step 6.5 completely with multicore file parsing.
    """
    try:
        from parser.git_ast import GitCommitParser
        git_parser = GitCommitParser(repo_dir=repo_dir)
        try:
            commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(
                old_ver, ver, limit=limit, known_commit_count=known_count
            )
        except TypeError:
            commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(
                old_ver, ver, limit=limit
            )

        ml_results = None
        commit_patch_ids: dict[str, str] = {}
        commit_file_blobs: dict[str, list[tuple[str, str, str]]] = {}
        if commits:
            try:
                from parser.mailing_list.ml_client import MailingListClient
                from parser.mailing_list.ml_types import CommitFeatures

                non_merge_hashes = [
                    c.commit_hash for c in commits
                    if not getattr(c, "parents", None) or len(c.parents) < 2
                ]
                all_hashes = [c.commit_hash for c in commits]

                if non_merge_hashes and repo_dir:
                    try:
                        p_log = sp.Popen(
                            ["git", "-C", repo_dir, "show", "--format=%H", "--patch", "--no-color", "--stdin"],
                            stdin=sp.PIPE,
                            stdout=sp.PIPE,
                            stderr=sp.DEVNULL,
                        )
                        p_patch = sp.Popen(
                            ["git", "patch-id", "--stable"],
                            stdin=p_log.stdout,
                            stdout=sp.PIPE,
                            stderr=sp.DEVNULL,
                            text=True,
                        )
                        if p_log.stdin:
                            p_log.stdin.write("\n".join(non_merge_hashes).encode("ascii") + b"\n")
                            p_log.stdin.close()
                        p_patch_out, _ = p_patch.communicate(timeout=180)
                        p_log.wait(timeout=10)
                        for line in p_patch_out.splitlines():
                            parts = line.strip().split()
                            if len(parts) >= 2:
                                commit_patch_ids[parts[1]] = parts[0]
                    except Exception as e:
                        logger.debug(f"Worker patch-id extraction failed: {e}")

                if all_hashes and repo_dir:
                    try:
                        p_dt = sp.run(
                            ["git", "-C", repo_dir, "diff-tree", "--stdin", "-r"],
                            input="\n".join(all_hashes),
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=180,
                        )
                        if p_dt.returncode == 0:
                            cur_h = None
                            for line in p_dt.stdout.splitlines():
                                line_s = line.strip()
                                if not line_s:
                                    continue
                                if not line_s.startswith(":"):
                                    cur_h = line_s
                                elif cur_h:
                                    parts = line_s.split("\t", 1)
                                    if len(parts) >= 2:
                                        meta = parts[0].split()
                                        fpath = parts[1]
                                        if len(meta) >= 5:
                                            old_b = meta[2]
                                            new_b = meta[3]
                                            pre_sha = old_b if len(old_b) == 40 and old_b != "0" * 40 else ""
                                            post_sha = new_b if len(new_b) == 40 and new_b != "0" * 40 else ""
                                            if pre_sha or post_sha:
                                                b_list = commit_file_blobs.setdefault(cur_h, [])
                                                if len(b_list) < 10:
                                                    b_list.append((pre_sha, post_sha, fpath))
                    except Exception as e:
                        logger.debug(f"Worker diff-tree extraction failed: {e}")

                features_list = []
                for c in commits:
                    c_hash = c.commit_hash
                    parents = getattr(c, "parents", None) or []
                    patch_id = commit_patch_ids.get(c_hash)
                    file_blobs = commit_file_blobs.get(c_hash, [])
                    features = CommitFeatures(
                        commit_id=0,
                        commit_hash=c_hash,
                        subject=getattr(c, "subject", "") or "",
                        author_name=getattr(c, "author_name", "") or "",
                        author_email=getattr(c, "author_email", "") or "",
                        author_date=int(getattr(c, "author_date", 0) or 0),
                        commit_ts=int(getattr(c, "committer_date", 0) or getattr(c, "author_date", 0) or 0),
                        commit_message=getattr(c, "message", "") or "",
                        patch_id=patch_id,
                        file_blobs=file_blobs[:10],
                        is_merge=len(parents) >= 2,
                    )
                    features_list.append(features)

                if features_list:
                    client = MailingListClient(git_dir=repo_dir)
                    try:
                        ml_results = client.resolve_commits(features_list)
                    finally:
                        # stop_tunnel is a no-op for the tunnel inherited from the parent
                        # (SSHTunnelManager.stop() never signals another process' ssh), and
                        # reaps a tunnel this worker had to open itself instead of orphaning it.
                        client.close(stop_tunnel=True)
            except Exception as e:
                logger.debug(f"Worker mailing list resolution skipped/failed: {e}")

        tmp_path = f"{result_path}.tmp"
        with open(tmp_path, "wb") as f:
            pickle.dump((commits, file_hunks_map, ml_results, commit_patch_ids, commit_file_blobs), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_path, result_path)
    except Exception as e:
        logger.error(f"Git commit worker error: {e}", exc_info=True)
        sys.exit(1)


def _start_git_commit_worker() -> None:
    """Fork the git commit parser for gp.Old_Version_Name..gp.Version_Name to run alongside AST parsing."""
    gp.stop_commit_worker()
    base_dir = G.RAMDISK if os.path.isdir(G.RAMDISK) else None
    result_dir = tempfile.mkdtemp(prefix="kip-commits-", dir=base_dir)
    mark_owner(result_dir)
    gp.PURGE_LIST.append(result_dir)
    gp._commit_result_dir = result_dir
    # Size of the range as Step 1 walked it (None when it did not: the parser then counts the range itself)
    known_count = getattr(gp, "commit_range_count", None)
    proc = _MP.Process(
        target=_git_commit_worker,
        args=(
            gp.Old_Version_Name,
            gp.Version_Name,
            os.path.join(result_dir, _GIT_COMMIT_RESULT),
            MF.get_repo_dir(),
            getattr(gp, "commit_limit", None),
            known_count,
        ),
        name="git-commit-parser",
        daemon=True,
    )
    proc.start()
    gp._commit_proc = proc


def _collect_git_commit_result() -> tuple[Any, Any] | None:
    """Wait for the background git commit parser and load its result.

    Returns:
        `(commits, file_hunks_map)`, or None when no worker ran, it failed, or the deadline passed.
    """
    proc = getattr(gp, "_commit_proc", None)
    result_dir = getattr(gp, "_commit_result_dir", None)
    if proc is None or not result_dir:
        return None
    try:
        while proc.is_alive():
            proc.join(timeout=1.0)
            if proc.is_alive() and G.is_deadline_exceeded():
                logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached while waiting for the git commit worker."))
                return None
        if proc.exitcode != 0:
            logger.warning(f"Git commit worker exited with code {proc.exitcode}; parsing commits in-process.")
            return None
        with open(os.path.join(result_dir, _GIT_COMMIT_RESULT), "rb") as f:
            res = pickle.load(f)  # noqa: S301
            if isinstance(res, tuple):
                if len(res) == 5:
                    commits, file_hunks_map, ml_results, commit_patch_ids, commit_file_blobs = res
                    if ml_results is not None:
                        gp._precomputed_ml_results = (ml_results, commit_patch_ids, commit_file_blobs)
                    return (commits, file_hunks_map)
                elif len(res) >= 2:
                    return (res[0], res[1])
            return res
    except Exception as e:
        logger.warning(f"Failed to load precomputed commits from background worker: {e}")
        return None
    finally:
        gp.stop_commit_worker()
