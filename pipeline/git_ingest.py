"""Git DAG ingestion, version ingest state, checkpoints and version registration."""
from core.globalstuff import (
    G,
    COLOR,
)
from core import ingest_state, distributed
import os
import subprocess as sp

import time
from typing import Any
import contextlib
from core import checkpoints
from core.TableHandling import Table
from db_engine import (
    Join,
    ge,
    gt,
    ne,
)
from core.DBLayout import (
    TABLES,
    m_maintainer_person,
    m_commit,
    m_commit_parent,
    m_commit_link,
    m_commit_relation,
    m_git_ref,
    m_bridge_commit_person,
)
from pipeline.blob_reuse import _person_key
from pipeline.changesets import file_fid_cache, file_fnid_cache
from pipeline.commits import commit_relation_rows
from pipeline.git_network import _lookup_commit_ids, resolve_git_network_and_repo
from pipeline.state import MF, gp, logger




def _ingest_commit_range(db: Any, commits: list[Any], network_id: int) -> dict[str, int]:
    """Register `commits` (newest first, as git log lists them) with their parents and contributors.

    Existing commits and persons are looked up and new rows inserted in bulk: a few statements per
    thousand commits instead of several round trips and transaction commits per commit.

    Returns:
        Commit hash -> commit_id for every commit of the range and every parent already registered.
    """
    if not commits:
        return {}
    ordered = [c for c in reversed(commits) if isinstance(c.commit_hash, str) and len(c.commit_hash) == 40]
    in_range = [c.commit_hash for c in ordered]
    outside_parents = {p for c in ordered for p in (c.parents or ())} - set(in_range)
    commit_ids = _lookup_commit_ids(db, network_id, [*in_range, *outside_parents])

    person_ids: dict[tuple[str, str], int] = {}
    try:
        rows = [
            (int(pid), name or "", email or "")
            for pid, name, email in db.select_rows("m_maintainer_person", ["person_id", "name", "email"], as_dict=False)
        ]
        # Rows written before identities were normalized can carry stray control bytes, so two
        # of them may now share one key. The row already stored in its normalized form wins
        # (lowest id among equals), so commits map to the identity a merge would keep.
        rows.sort(key=lambda r: ((r[1], r[2]) != _person_key(r[1], r[2]), r[0]))
        for pid, name, email in rows:
            person_ids.setdefault(_person_key(name, email), pid)
    except Exception as exc:  # noqa: BLE001 - persons are then looked up one by one
        logger.warning(f"Could not preload the known persons ({exc}); looking them up one by one.")
    new_persons: list[tuple[int, str, str]] = []
    next_ids: dict[str, int] = {}
    id_cursors: dict[str, int] = {}
    id_limits: dict[str, int] = {}

    target_cid = getattr(gp, "VID", 0) or 0
    if target_cid:
        if target_cid == getattr(gp, "Old_VID", None) or target_cid in commit_ids.values():
            target_cid = 0
        elif db is not None:
            try:
                if ingest_state.get_status(db, network_id, target_cid) == ingest_state.INGEST_DONE:
                    target_cid = 0
            except Exception:
                pass

    def _next_id(table: Table) -> int:
        if getattr(G, "distributed", False):
            name = table.table_name
            if name not in id_cursors or id_cursors[name] > id_limits[name]:
                block_size = 1000 if name == "m_commit" else 256
                start_id, end_id = db.reserve_id_block(table, block_size, commit_id=target_cid)
                id_cursors[name] = start_id
                id_limits[name] = end_id
            assigned = id_cursors[name]
            id_cursors[name] = assigned + 1
            return assigned
        if table.table_name not in next_ids:
            next_ids[table.table_name] = db.get_next_id(table)
        assigned = next_ids[table.table_name]
        next_ids[table.table_name] = assigned + 1
        return assigned

    def _person_id(name: str | None, email: str | None) -> int:
        key = _person_key(name, email)
        pid = person_ids.get(key)
        if pid is None:
            row = db.select(m_maintainer_person, (None, key[0], key[1]))
            pid = row[0] if row else None
        if pid is None:
            pid = _next_id(m_maintainer_person)
            new_persons.append((pid, key[0], key[1]))
        person_ids[key] = pid
        return pid

    new_commits: list[tuple] = []
    for commit in ordered:
        if commit.commit_hash in commit_ids:
            continue
        author_pid = _person_id(commit.author_name, commit.author_email)
        committer_pid = _person_id(
            commit.committer_name or commit.committer_email or commit.author_name,
            commit.committer_email or commit.author_email,
        )
        c_id = _next_id(m_commit)
        commit_ids[commit.commit_hash] = c_id
        new_commits.append((
            c_id,
            network_id,
            bytes.fromhex(commit.commit_hash),
            author_pid,
            int(commit.author_date or 0),
            committer_pid,
            int(commit.committer_date or 0),
            (commit.subject or "")[:500],
            commit.message or "",
        ))

    # Parents and contributors of every commit in the range (both tables skip rows already present)
    parent_rows: list[tuple[int, int, int]] = []
    contributor_rows: dict[tuple[int, int, int], tuple[int, int, int, int]] = {}
    for commit in ordered:
        c_id = commit_ids[commit.commit_hash]
        for p_idx, p_hash in enumerate(commit.parents or ()):
            p_cid = commit_ids.get(p_hash)
            if p_cid is not None:
                parent_rows.append((c_id, p_cid, p_idx))
        for contrib in commit.contributors:
            pid = _person_id(contrib.name, contrib.email)
            contributor_rows.setdefault((c_id, pid, int(contrib.role)), (c_id, pid, int(contrib.role), int(contrib.priority)))

    # What the new commits' messages say about other commits and mail (core/API.md section 6, Step 1)
    relation_rows, link_rows = commit_relation_rows(
        [(row[0], row[2].hex(), row[8]) for row in new_commits], getattr(MF, "repo_dir", None))

    for table, rows in (
        (m_maintainer_person, new_persons),
        (m_commit, new_commits),
        (m_commit_parent, parent_rows),
        (m_bridge_commit_person, list(contributor_rows.values())),
        (m_commit_relation, relation_rows),
        (m_commit_link, link_rows),
    ):
        if rows:
            db.insert(table, tuple(rows))
    return commit_ids


def ingest_git_dag(
    version_or_ref: str,
    network_id: int,
    repo_id: int,
    repo_path: str,
    limit: int | None = None,
) -> tuple[int, bool, bool]:
    """Ingest git commit DAG history up to version_or_ref and register git reference in m_git_ref.

    `already_exists` is also True when version_or_ref is a new ref on a commit another ref already
    checkpointed (tags sharing a commit, e.g. v2.6.32-rc1/-rc2): the ref is registered, nothing re-parsed.

    Returns:
        (head_commit_id, is_checkpoint, already_exists)
    """
    repo_path = repo_path or getattr(MF, "repo_dir", None) or "linux"
    git_dir = os.path.join(repo_path, ".git") if os.path.isdir(os.path.join(repo_path, ".git")) else repo_path

    # Resolve commit SHA for version_or_ref
    c_res = sp.run(
        ["git", f"--git-dir={git_dir}", "rev-parse", f"{version_or_ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    head_commit_hex = c_res.stdout.strip()
    if not head_commit_hex or len(head_commit_hex) != 40:
        c_res2 = sp.run(
            ["git", f"--git-dir={git_dir}", "rev-parse", version_or_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        head_commit_hex = c_res2.stdout.strip()

    head_commit_bytes = bytes.fromhex(head_commit_hex) if len(head_commit_hex) == 40 else b"\x00" * 20

    tag_check = sp.run(
        ["git", f"--git-dir={git_dir}", "tag", "-l", version_or_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    is_tag = bool(tag_check.stdout.strip() == version_or_ref)
    branch_check = sp.run(
        ["git", f"--git-dir={git_dir}", "for-each-ref", "--count=1", f"refs/heads/{version_or_ref}", f"refs/remotes/{version_or_ref}"],
        capture_output=True,
        text=True,
        check=False,
    )
    # m_git_ref.ref_type: 1 = tag, 2 = branch, 3 = bare commit (e.g. the repository's first commit)
    ref_type = 1 if is_tag else (2 if branch_check.stdout.strip() else 3)
    is_checkpoint = is_tag or (gp.VID == 0 or getattr(gp, "Old_VID", 0) == 0)

    # A previous ingestion of this ref or commit: finished (skip it), or interrupted (roll it back and parse again)
    already_ingested = False
    with G.DB() as db:
        ref_row = db.select(m_git_ref, (None, repo_id, None, version_or_ref, None, None, None, None))
        commit_row = db.select(m_commit, (None, network_id, head_commit_bytes, None, None, None, None, None, None))
        head_commit_id = commit_row[0] if commit_row else None
        known_cid = ref_row[4] if ref_row else head_commit_id
        state = _previous_ingest(db, network_id, known_cid, version_or_ref) if known_cid is not None and is_checkpoint else "new"
        if state == "done":
            if ref_row:
                gp.Old_VID = gp.VID
                gp.VID = known_cid
                gp.Old_Version_Name = gp.Version_Name
                gp.Version_Name = version_or_ref
                return known_cid, is_checkpoint, True
            # Another ref (e.g. v2.6.32-rc1 for v2.6.32-rc2) already ingested this commit: only register the ref
            already_ingested = True
            logger.info(
                f"Ref '{version_or_ref}' points to commit_id {known_cid}, already ingested by another ref; "
                f"registering it as an alias."
            )
        elif state == "in_progress":
            logger.info(
                f"Ingestion of '{version_or_ref}' (commit_id {known_cid}) is actively in progress by a heartbeating peer node; "
                f"preserving partial rows."
            )
        elif state == "interrupted":
            logger.warning(
                f"Ingestion of '{version_or_ref}' (commit_id {known_cid}) was interrupted; rolling back its partial rows."
            )
            if not ingest_state.rollback_version(db, network_id, known_cid, TABLES):
                _purge_partial_version(db, known_cid)
            _forget_rolled_back_rows()
        elif state == "legacy_partial":
            # Interrupted before ingestion states existed: only its version-scoped rows can be identified
            logger.warning(
                f"Prior ingestion for ref '{version_or_ref}' (commit_id {known_cid}) was incomplete; "
                f"purging partial checkpoint and commit records for a clean restart."
            )
            _purge_partial_version(db, known_cid)

    gp.commit_ids_by_hash = {}
    if head_commit_id is None:
        from parser.git_ast import GitCommitParser
        git_parser = GitCommitParser(repo_dir=repo_path)
        base_rev = getattr(gp, "Old_Version_Name", None)
        # The DAG only needs commit metadata and parents; per-commit file lists come from processing_git_commits
        commits = git_parser.parse_version_commits(old_rev=base_rev, new_rev=version_or_ref, limit=limit, with_files=False)
        logger.info(f"[Git DAG] Ingesting {len(commits)} commits up to '{version_or_ref}'")
        with G.DB() as db:
            commit_hash_to_id = _ingest_commit_range(db, commits, network_id)
        # Reused by processing_git_commits() instead of one m_commit lookup per commit
        gp.commit_ids_by_hash = commit_hash_to_id
        head_commit_id = commit_hash_to_id.get(head_commit_hex)

    if head_commit_id is None:
        with G.DB() as db:
            commit_row = db.select(m_commit, (None, network_id, head_commit_bytes, None, None, None, None, None, None))
            if commit_row:
                head_commit_id = commit_row[0]
            else:
                if getattr(G, "distributed", False):
                    head_commit_id = db.reserve_id_block(m_commit, 1)[0]
                else:
                    head_commit_id = db.get_next_id(m_commit)
                db.insert(m_commit, (
                    head_commit_id,
                    network_id,
                    head_commit_bytes,
                    1, 0, 1, 0, f"Commit {version_or_ref}", "",
                ))

    # Register in m_git_ref
    with G.DB() as db:
        ref_row = db.select(m_git_ref, (None, repo_id, None, version_or_ref, None, None, None, None))
        if ref_row:
            ref_id = ref_row[0]
            try:
                db.update(m_git_ref, (ref_id, repo_id, ref_type, version_or_ref, head_commit_id, None, None, None))
            except Exception:
                pass
        else:
            if getattr(G, "distributed", False):
                ref_id = db.reserve_id_block(m_git_ref, 1)[0]
            else:
                ref_id = db.get_next_id(m_git_ref)
            try:
                db.insert(m_git_ref, (ref_id, repo_id, ref_type, version_or_ref, head_commit_id, None, None, None))
            except Exception as e_ins:
                ref_row = db.select(m_git_ref, (None, repo_id, None, version_or_ref, None, None, None, None))
                if ref_row:
                    ref_id = ref_row[0]
                    try:
                        db.update(m_git_ref, (ref_id, repo_id, ref_type, version_or_ref, head_commit_id, None, None, None))
                    except Exception:
                        pass
                else:
                    raise e_ins
        if not already_ingested and db is not None:
            # From here on every row the run writes lies above these watermarks (see core/ingest_state.py)
            ingest_state.begin(
                db,
                network_id,
                head_commit_id,
                TABLES,
                base_commit_id=getattr(gp, "Old_VID", 0) or 0,
                worker_id=getattr(G, "node_id", "") or "",
            )
        if getattr(G, "distributed", False) and getattr(G, "node_id", None):
            if heartbeat_daemon := getattr(G, "heartbeat_daemon", None):
                heartbeat_daemon.set_active(head_commit_id)
            if getattr(gp, "_active_task_id", None) and db is not None:
                db.update_rows("m_parse_task", {"commit_id": head_commit_id}, {"task_id": gp._active_task_id})

    if getattr(G, "distributed_role", None) != distributed.ROLE_WORKER and not getattr(gp, "_repair_base_set", False):
        gp.Old_VID = gp.VID
        gp.Old_Version_Name = gp.Version_Name
    gp.VID = head_commit_id
    gp.Version_Name = version_or_ref
    return head_commit_id, is_checkpoint, already_ingested


def _previous_ingest(db: Any, network_id: int, commit_id: int, version_or_ref: str) -> str:
    """State of an earlier ingestion of `commit_id`: "done", "in_progress", "interrupted", "legacy_partial" or "new".

    `m_ingest_state` decides when present. Versions ingested before it existed have no row: they count
    as done when their checkpoint covers at least 90% of the tree (the row is then backfilled), else
    as a partial legacy run when they have checkpoint rows.
    """
    status = ingest_state.get_status(db, network_id, commit_id)
    if status == ingest_state.INGEST_DONE:
        return "done"
    if status == ingest_state.INGEST_PARSING:
        if getattr(G, "distributed", False):
            now = int(time.time())
            if db.row_exists(
                ("m_ingest_state", "s"),
                {"s.network_id": network_id, "s.commit_id": commit_id, "s.worker_id": ne(""), "n.heartbeat_at": ge(now - 180)},
                joins=[Join(("m_parse_node", "n"), {"n.node_id": "s.worker_id"})],
            ):
                return "in_progress"
        return "interrupted"
    row_cnt = db.count_rows("m_bridge_checkpoint", {"commit_id": commit_id})
    if not row_cnt:
        return "new"
    tree_files = len(MF.git_file_list(version_or_ref).splitlines())
    if tree_files > 0 and row_cnt >= max(1, int(tree_files * 0.9)):
        ingest_state.set_status(db, network_id, commit_id, ingest_state.INGEST_DONE)
        return "done"
    return "legacy_partial"


def _resolve_legacy_state(ref: str, version_states: dict[str, tuple[int, str]], network_id: int) -> str:
    """Decide a "legacy" ref (checkpoint rows, no m_ingest_state row) with `_previous_ingest()`'s 90% rule.

    Updates `version_states` with "done" (the state row is then backfilled) or "legacy_partial".
    """
    cid = version_states[ref][0]
    with G.DB() as db:
        state = _previous_ingest(db, network_id, cid, ref)
    version_states[ref] = (cid, state)
    return state


def _purge_partial_version(db: Any, commit_id: int) -> None:
    """Remove the version-scoped rows of an interrupted run that left no watermarks."""
    ingest_state.purge_partial_version(db, commit_id)


def _forget_rolled_back_rows() -> None:
    """Drop in-process caches that may still hold rows a rollback removed."""
    if hasattr(G.TE, "discard_carried"):
        G.TE.discard_carried()
    file_fid_cache.clear()
    file_fnid_cache.clear()
    for cache in ("blob_symbol_cache", "blob_symbol_used"):
        getattr(gp, cache, {}).clear()


def finish_version_ingest(commit_id: int | None) -> None:
    """Mark the version as completely ingested (the webapp lists it from now on), then store the previous
    version's checkpoint as a delta when it is close to an earlier full one (core/checkpoints.py): from now on
    this version is the one the next run reads as a full tree."""
    if not commit_id:
        return
    with G.DB() as db:
        if db is not None:
            if getattr(G, "distributed_role", None) == distributed.ROLE_WORKER and getattr(gp, "_active_task_id", None):
                now = int(time.time())
                leased = db.row_exists(
                    "m_parse_task",
                    {
                        "task_id": gp._active_task_id,
                        "worker_id": getattr(G, "node_id", ""),
                        "status": distributed.TASK_LEASED,
                        "lease_expires_at": gt(now),
                    },
                )
                if not leased:
                    raise RuntimeError(f"Worker lease for task {gp._active_task_id} expired or was revoked; aborting completion.")
            ingest_state.set_status(db, getattr(gp, "network_id", 1), commit_id, ingest_state.INGEST_DONE)
            refresh_person_identities(db, "version ingested")
            _compact_checkpoint(db, getattr(gp, "Old_VID", 0), commit_id)
            if getattr(G, "distributed", False) and getattr(G, "distributed_role", None) != distributed.ROLE_WORKER:
                from core.distributed import unlock_dependent_tasks
                unlock_dependent_tasks(db, getattr(gp, "network_id", 1), commit_id, base_ref_name=getattr(gp, "Version_Name", None))


def refresh_person_identities(db: Any, reason: str, only_if_incomplete: bool = False) -> int | None:
    """Store the identity of every person alias (m_person_identity, parser/git_ast/identity.py). The master
    (or a standalone run) does it; a worker's new persons are picked up at the master's next refresh.
    With only_if_incomplete, nothing is done when every person already has a row. Never blocks parsing."""
    if db is None or (getattr(G, "distributed_role", "standalone") == distributed.ROLE_WORKER and reason != "worker shutdown"):
        return None
    from parser.git_ast.identity import refresh_person_identities as refresh
    try:
        if only_if_incomplete:
            persons = db.count_rows("m_maintainer_person", {"person_id": gt(0)})
            stored = db.count_rows("m_person_identity")
            if persons == stored:
                return 0
        started = time.perf_counter()
        changed = refresh(db)
        if changed:
            logger.info(f"[Identities] {changed} person identity row(s) updated ({reason}) in {time.perf_counter() - started:.1f}s.")
        return changed
    except Exception as exc:  # noqa: BLE001 - identities are an optimization for the webapp
        logger.warning(COLOR.yellow(f"[Identities] Refreshing m_person_identity failed ({reason}): {exc}"))
        with contextlib.suppress(Exception):
            db.rollback()
        return None


def _compact_checkpoint(db: Any, prev_vid: int | None, current_vid: int) -> None:
    """Best effort: a failed compaction leaves the full checkpoint in place."""
    if not prev_vid or prev_vid == current_vid:
        return
    try:
        started = time.perf_counter()
        res = checkpoints.compact(
            db,
            getattr(gp, "network_id", 1),
            prev_vid,
            current=current_vid,
            preferred_base=getattr(gp, "Old_VID", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[Checkpoint] Keeping commit_id {prev_vid}'s checkpoint full: compaction failed: {exc}")
        return
    if res:
        logger.info(
            f"[Checkpoint] commit_id {prev_vid} stored as a delta of {res['base']}: {res['changed'] + res['removed']} "
            f"of {res['total']} rows kept ({res['dropped']} dropped) in {time.perf_counter() - started:.1f}s."
        )


def _ensure_full_checkpoint(commit_id: int | None) -> None:
    """Expand `commit_id`'s checkpoint when it is a delta (the TableEngine and Steps 4-6 read it in full)."""
    if not commit_id:
        return
    with G.DB() as db:
        if db is None:
            return
        restored = checkpoints.expand(db, commit_id)
    if restored:
        logger.info(f"[Checkpoint] Expanded the delta checkpoint of commit_id {commit_id} ({restored} rows restored).")
    if restored:
        logger.info(f"[Checkpoint] Expanded the delta checkpoint of commit_id {commit_id} ({restored} rows restored).")


def create_new_vid(name: str) -> bool:
    """Register or synchronize active version in m_git_ref (m_v_main). Returns True if version already complete."""
    network_id, repo_id = resolve_git_network_and_repo(MF.repo_dir)
    head_cid, is_checkpoint, already_exists = ingest_git_dag(
        name, network_id, repo_id, MF.repo_dir, limit=getattr(gp, "commit_limit", None)
    )
    return already_exists
