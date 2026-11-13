"""Mailing-list resolution of version commits."""
from core.globalstuff import (
    G,
)
from core.config import (
    get_mailing_list_config,
)
import subprocess as sp

from core.DBLayout import (
    m_commit_email,
)
from pipeline.git_network import _lookup_commit_ids
from pipeline.state import MF, gp, logger




def resolve_version_mailing_list_commits(
    commits: list,
    commit_hash_to_id: dict[str, int],
    repo_path: str | None = None,
    storage_dir: str | None = None,
) -> int:
    """Resolve mailing list patch emails and pull request threads for commits in the active version."""
    from parser.mailing_list.ml_client import MailingListClient
    from parser.mailing_list.ml_types import CommitFeatures

    ml_cfg = get_mailing_list_config()
    if not ml_cfg.get("enabled", True):
        logger.debug("Mailing list commit resolution is disabled in configuration.")
        return 0

    if not commits:
        return 0

    repo_dir = repo_path or getattr(gp, "repo_path", None) or MF.get_repo_dir()

    features_list: list[CommitFeatures] = []
    merge_commits: list[tuple[int, str, list[str]]] = []

    for c in commits:
        c_hash = c.commit_hash
        c_id = commit_hash_to_id.get(c_hash)
        if not c_id:
            continue
        parents = getattr(c, "parents", None) or []
        if len(parents) >= 2:
            merge_commits.append((c_id, c_hash, parents))

    results = None
    precomputed_ml = getattr(gp, "_precomputed_ml_results", None)
    if precomputed_ml:
        results, commit_patch_ids, commit_file_blobs = precomputed_ml
        gp._precomputed_ml_results = None

    if results is None:
        commit_patch_ids: dict[str, str] = {}
        commit_file_blobs: dict[str, list[tuple[str, str, str]]] = {}
        non_merge_hashes = [
            c.commit_hash for c in commits
            if commit_hash_to_id.get(c.commit_hash) and (not getattr(c, "parents", None) or len(c.parents) < 2)
        ]
        all_known_hashes = [
            c.commit_hash for c in commits
            if commit_hash_to_id.get(c.commit_hash)
        ]

        if non_merge_hashes:
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
                p_patch_out, _ = p_patch.communicate(timeout=120)
                p_log.wait(timeout=10)
                for line in p_patch_out.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        commit_patch_ids[parts[1]] = parts[0]
            except Exception as e:
                logger.debug(f"Batch patch-id extraction failed: {e}")

        if all_known_hashes:
            try:
                p_dt = sp.run(
                    ["git", "-C", repo_dir, "diff-tree", "--stdin", "-r"],
                    input="\n".join(all_known_hashes),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
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
                logger.debug(f"Batch diff-tree extraction failed: {e}")

        for c in commits:
            c_hash = c.commit_hash
            c_id = commit_hash_to_id.get(c_hash)
            if not c_id:
                continue

            parents = getattr(c, "parents", None) or []
            patch_id = commit_patch_ids.get(c_hash)
            file_blobs = commit_file_blobs.get(c_hash, [])

            features = CommitFeatures(
                commit_id=c_id,
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

    client_kwargs = {}
    if storage_dir:
        client_kwargs["storage_dir"] = storage_dir
    client = MailingListClient(**client_kwargs)

    total_staged = 0
    try:
        if results is None:
            if not features_list:
                return 0
            results = client.resolve_commits(features_list)
        for c_hash, ctx in results.items():
            c_id = commit_hash_to_id.get(c_hash)
            if c_id is not None:
                staged_rows = client.stage_resolved_context(m_commit_email, c_id, ctx)
                total_staged += len(staged_rows)

        # Pull request propagation across merge commits
        for m_cid, m_hash, m_parents in merge_commits:
            ctx = results.get(m_hash)
            if ctx and ctx.pull_request and ctx.pull_request.mids:
                pr_mid = ctx.pull_request.mids[0]
                pr_tid = ctx.pull_request.tid
                try:
                    rev_res = sp.run(
                        ["git", "-C", repo_dir, "rev-list", f"{m_parents[0]}..{m_hash}"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if rev_res.returncode == 0:
                        child_hashes = [h.strip() for h in rev_res.stdout.splitlines() if h.strip() and h.strip() != m_hash]
                        child_ids = [commit_hash_to_id[h] for h in child_hashes if h in commit_hash_to_id]
                        if child_ids:
                            propagated = client.propagate_pull_request_to_children(m_commit_email, child_ids, pr_mid, pr_tid)
                            total_staged += propagated
                except Exception as e:
                    logger.debug(f"Pull request propagation error for merge {m_hash[:12]}: {e}")

        logger.info(f"Resolved mailing list links for {len(results)} commits (staged {total_staged} m_commit_email rows).")
    finally:
        client.close()

    return total_staged


def standalone_resolve_mailing_list(
    repo_path: str,
    commit_range: str,
    storage_dir: str | None = None,
) -> None:
    """Standalone CLI resolution of commits to mailing list emails."""
    from parser.git_ast import GitCommitParser

    logger.info(f"Starting standalone mailing list resolution for range '{commit_range}' in '{repo_path}'...")
    git_parser = GitCommitParser(repo_dir=repo_path)
    if ".." in commit_range:
        old_rev, new_rev = commit_range.split("..", 1)
    else:
        old_rev, new_rev = None, commit_range

    commits = git_parser.parse_version_commits(old_rev, new_rev, with_files=False)
    if not commits:
        logger.warning(f"No commits found in range '{commit_range}'.")
        return

    logger.info(f"Parsed {len(commits)} commits for mailing list resolution.")
    network_id = getattr(gp, "network_id", 1)
    commit_hashes = [c.commit_hash for c in commits]
    commit_hash_to_id: dict[str, int] = {}
    with G.DB() as db:
        commit_hash_to_id = _lookup_commit_ids(db, network_id, commit_hashes)

    unregistered = [c for c in commits if c.commit_hash not in commit_hash_to_id]
    if unregistered:
        logger.info(f"{len(unregistered)} commits are not yet in m_commit; resolving {len(commit_hash_to_id)} known commits.")

    target_commits = [c for c in commits if c.commit_hash in commit_hash_to_id]
    if not target_commits:
        logger.warning("No commits from the specified range exist in m_commit. Ingest git DAG first.")
        return

    try:
        if hasattr(G, "TE") and G.TE is not None and not getattr(G.TE, "tables", None):
            G.TE.start([m_commit_email], G.DB)
        staged = resolve_version_mailing_list_commits(
            target_commits,
            commit_hash_to_id,
            repo_path=repo_path,
            storage_dir=storage_dir,
        )
        if hasattr(G.TE, "commit_all"):
            G.TE.commit_all()
        elif hasattr(G.TE, "commit"):
            G.TE.commit(m_commit_email.table_id)
        logger.info(f"Standalone mailing list resolution complete: staged {staged} m_commit_email entries.")
    except (FileNotFoundError, ConnectionError, RuntimeError) as exc:
        logger.error(f"[Mailing List] {exc}")
        G.emergency_shutdown(1)
