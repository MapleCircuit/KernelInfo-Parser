"""Commit relations and `processing_git_commits`."""
from core.globalstuff import (
    G,
    COLOR,
    type_check,
)
import subprocess as sp

from typing import Any
from collections.abc import Iterable
from collections import defaultdict
from core.DBLayout import (
    m_file_name,
    m_bridge_tag,
    m_maintainer_person,
    m_commit,
    m_commit_file,
    m_commit_parent,
    m_commit_link,
    m_commit_relation,
    m_bridge_checkpoint,
    m_bridge_commit_person,
    m_bridge_commit_tag,
    m_file_blob,
)
from pipeline.blob_reuse import _SQL_IN_CHUNK, _person_key
from pipeline.changesets import file_fnid_cache, get_fnid_for_path
from pipeline.git_commit_worker import _collect_git_commit_result
from pipeline.git_network import _lookup_commit_ids
from pipeline.mailing_list_steps import resolve_version_mailing_list_commits
from pipeline.state import MF, gp, logger




def _load_blob_tags(blob_ids: Iterable[int]) -> dict[int, list[tuple[int, int, int, int]]]:
    """Tags of already-parsed blobs as `blob_id -> [(tag_id, blob_id, line_s, line_e), ...]` from m_bridge_tag."""
    wanted = sorted(set(blob_ids))
    tags: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    if not wanted:
        return tags
    with G.DB() as db:
        try:
            for i in range(0, len(wanted), _SQL_IN_CHUNK):
                chunk = wanted[i : i + _SQL_IN_CHUNK]
                rows = db.select_rows(
                    "m_bridge_tag", ["blob_id", "tag_id", "line_s", "line_e"], {"blob_id": chunk}, as_dict=False,
                )
                for blob_id, tag_id, line_s, line_e in rows:
                    tags[int(blob_id)].append((int(tag_id), int(blob_id), int(line_s), int(line_e)))
        except Exception as exc:
            logger.debug(f"_load_blob_tags query failed: {exc}")
    return tags


def _commit_blob_id(path: str, sha: str) -> int:
    """m_file_blob id of a blob a commit read or wrote (0 for none): the version's own blob when it is the one,
    else the blob's row, staged when new."""
    if not sha:
        return 0
    head_hash = gp.file_blob_hashes.get(path) if getattr(gp, "file_blob_hashes", None) else None
    if head_hash is not None and head_hash.hex() == sha and path in gp.file_blob_ids:
        return gp.file_blob_ids[path]
    return G.TE.set(m_file_blob.table_id, (None, bytes.fromhex(sha), type_check(path)))[0]


def commit_relation_rows(new_messages: list[tuple[int, str, str]], repo_path: Any) -> tuple[list[tuple], list[tuple]]:
    """m_commit_relation / m_commit_link rows of newly inserted commits ((commit_id, sha, message);
    parser/git_ast/commit_relations.py). Abbreviated targets are resolved once for the batch, with one
    `git cat-file`; a prefix naming no commit or several keeps a zero target_hash."""
    from parser.git_ast import commit_relations as cr
    parsed = [(cid, *cr.relation_rows(msg or "", sha)) for cid, sha, msg in new_messages]
    prefixes = set().union(*(cr.prefixes_to_resolve(rels) for _cid, rels, _links in parsed)) if parsed else set()
    resolved: dict[str, str] = {}
    if prefixes and repo_path:
        try:
            resolved = cr.resolve_prefixes(repo_path, prefixes)
        except (OSError, sp.SubprocessError) as exc:
            logger.warning(f"Commit relation prefixes not resolved ({len(prefixes)}): {exc}")
    rel_rows: list[tuple] = []
    link_rows: list[tuple] = []
    for cid, rels, links in parsed:
        r, lk = cr.table_rows(cid, rels, links, resolved)
        rel_rows.extend(r)
        link_rows.extend(lk)
    if parsed:
        logger.info(f"Commit relations: {len(rel_rows)} relation(s), {len(link_rows)} mail link(s) for {len(parsed)} new "
                    f"commit(s); {len(resolved)} of {len(prefixes)} abbreviated target(s) resolved.")
    return rel_rows, link_rows


def stage_commit_relations(new_messages: list[tuple[int, str, str]], repo_path: Any) -> tuple[int, int]:
    """Stage through the TableEngine the relation rows of commits Step 6.5 inserted itself (commits Step 1
    did not register; those get their rows in _ingest_commit_range)."""
    rel_rows, link_rows = commit_relation_rows(new_messages, repo_path)
    for row in rel_rows:
        G.TE.set(m_commit_relation.table_id, row)
    for row in link_rows:
        G.TE.set(m_commit_link.table_id, row)
    return len(rel_rows), len(link_rows)


def processing_git_commits(version: str) -> None:
    """Parse git commits for active version, link contributors, and bridge tags to commits."""
    from parser.git_ast import GitCommitParser
    repo_path = getattr(gp, "repo_path", None) or MF.get_repo_dir()
    git_parser = GitCommitParser(repo_dir=repo_path)
    network_id = getattr(gp, "network_id", 1)

    commits = None
    file_hunks_map = None
    precomputed = _collect_git_commit_result()
    if precomputed is not None:
        commits, file_hunks_map = precomputed

    if commits is None:
        known_count = len(gp.commit_ids_by_hash) if getattr(gp, "commit_ids_by_hash", None) else None
        commits, file_hunks_map = git_parser.parse_version_commits_with_hunks(
            gp.Old_Version_Name, gp.Version_Name, limit=getattr(gp, "commit_limit", None), known_commit_count=known_count
        )
    if not commits:
        # An empty diff stream (git timed out or the range is empty) leaves the version without
        # m_commit_file/m_bridge_commit_tag rows, so it is never silent
        logger.warning(
            f"No commit diff stream for version '{version}' "
            f"('{gp.Old_Version_Name}'..'{gp.Version_Name}'); no file or tag rows staged for its commits."
        )
        return

    logger.info(f"Processing {len(commits)} git commits for version '{version}'...")

    # Commit ids of the range: Step 1 (ingest_git_dag) registered them, anything else is looked up in bulk
    commit_hash_to_id: dict[str, int] = {}
    known_ids = dict(getattr(gp, "commit_ids_by_hash", None) or {})
    unknown = [h for c in commits for h in (c.commit_hash, *(c.parents or ())) if h not in known_ids]
    if unknown:
        with G.DB() as db:
            known_ids.update(_lookup_commit_ids(db, network_id, unknown))

    pending_parents: list[tuple[int, list[str]]] = []
    new_messages: list[tuple[int, str, str]] = []
    head_commit_id = None
    for commit in commits:
        if G.is_deadline_exceeded():
            logger.warning(COLOR.yellow("[Duration Limit] Execution deadline reached during commit processing. Stopping early..."))
            break
        c_hash = commit.commit_hash
        commit_id = known_ids.get(c_hash)
        c_match = commit_id is not None
        if not c_match:
            # Format commit_hash as 20 bytes
            if isinstance(c_hash, str):
                c_hash_bytes = bytes.fromhex(c_hash) if len(c_hash) == 40 else c_hash.encode("ascii")
            else:
                c_hash_bytes = c_hash
            author_res = G.TE.set(m_maintainer_person.table_id, (None, *_person_key(commit.author_name, commit.author_email)))
            author_pid = author_res[0] if author_res else 1
            committer_res = G.TE.set(
                m_maintainer_person.table_id,
                (None, *_person_key(commit.committer_name or commit.committer_email or commit.author_name,
                                    commit.committer_email or commit.author_email)),
            )
            committer_pid = committer_res[0] if committer_res else author_pid
            # Insert commit (commit_id, network_id, commit_hash, author_id, author_date, committer_id, committer_date, subject, message)
            commit_res = G.TE.set(
                m_commit.table_id,
                (
                    None,
                    network_id,
                    c_hash_bytes,
                    author_pid,
                    commit.author_date,
                    committer_pid,
                    commit.committer_date,
                    commit.subject[:500],
                    commit.message,
                ),
            )
            commit_id = commit_res[0]
            new_messages.append((commit_id, c_hash if isinstance(c_hash, str) else bytes(c_hash).hex(), commit.message))
        commit.commit_id = commit_id
        commit_hash_to_id[commit.commit_hash] = commit_id
        if head_commit_id is None:
            head_commit_id = commit_id

        # Commit parents are linked after the loop: git log lists children before their parents
        if not c_match and hasattr(commit, "parents") and commit.parents:
            pending_parents.append((commit_id, commit.parents))

        # Insert contributors (Author, Committer, Co-developed-by, Signed-off-by, Reviewed-by, etc.)
        if not c_match:
            for contrib in commit.contributors:
                c_res = G.TE.set(m_maintainer_person.table_id, (None, *_person_key(contrib.name, contrib.email)))
                c_pid = c_res[0] if c_res else 1
                G.TE.set(
                    m_bridge_commit_person.table_id,
                    (
                        commit_id,
                        c_pid,
                        int(contrib.role),
                        int(contrib.priority),
                    ),
                )

        # Files the commit touched, with the blobs it read and wrote (blobs of intermediate commits get their
        # m_file_blob row here; they are parsed if a later version contains them)
        for change_type, file_path in commit.files:
            old_path, old_sha, new_sha = commit.file_blobs.get(file_path, (file_path, "", ""))
            fnid = get_fnid_for_path(file_path)
            if fnid is None:
                fnid = G.TE.set(m_file_name.table_id, (None, file_path[:255]))[0]
                file_fnid_cache[file_path] = fnid
            old_fnid = fnid if not old_path or old_path == file_path else (get_fnid_for_path(old_path) or 0)
            G.TE.set(
                m_commit_file.table_id,
                (
                    commit_id,
                    fnid,
                    _commit_blob_id(file_path, new_sha),
                    old_fnid if old_sha else 0,
                    _commit_blob_id(old_path or file_path, old_sha),
                    change_type[:1],
                ),
            )

    # Link parents now that every commit of the range has an id; parents outside the range were looked up above
    for commit_id, parent_hashes in pending_parents:
        for p_idx, p_hash in enumerate(parent_hashes):
            p_cid = commit_hash_to_id.get(p_hash) or known_ids.get(p_hash)
            if p_cid is not None:
                G.TE.set(m_commit_parent.table_id, (commit_id, p_cid, p_idx))

    # What the new commits' messages say about other commits and mail (m_commit_relation, m_commit_link)
    stage_commit_relations(new_messages, repo_path)

    # Link tags to commits for all changed files and evacuate executed ChangeSet memory
    # Use gp.file_pre_extracted_tags and gp.file_blob_ids directly to avoid decompressing 70,000 ChangeSets!
    if hasattr(gp, "file_pre_extracted_tags") and gp.file_pre_extracted_tags:
        for c_path, file_tags in gp.file_pre_extracted_tags.items():
            blob_id = gp.file_blob_ids.get(c_path, 0)
            if blob_id == 0:
                continue
            if file_tags:
                tag_bridges = git_parser.map_tags_to_commits(
                    file_tags,
                    c_path,
                    commit_hash_to_id=commit_hash_to_id,
                    file_hunks_map=file_hunks_map,
                )
                for cid, b_id, tid in tag_bridges:
                    G.TE.set(
                        m_bridge_commit_tag.table_id,
                        (
                            cid,
                            b_id,
                            tid,
                        ),
                    )
    else:
        for file_path, cs_obj in list(gp.ChangeSet_Dict.items()):
            if not cs_obj or not getattr(cs_obj, "current_path", None):
                continue
            c_path = cs_obj.current_path
            blob_id = getattr(cs_obj, "blob_id", 0)
            if blob_id == 0:
                continue

            # Collect tags for this file from ChangeSet operations or pre-extracted tags
            file_tags = getattr(cs_obj, "pre_extracted_tags", None)
            if file_tags is None:
                file_tags = []
                for op in getattr(cs_obj, "cs", []):
                    if op and len(op) >= 3 and op[0] == m_bridge_tag.table_id:
                        cols = op[2]
                        if len(cols) >= 4:
                            tag_id = cols[1] if not isinstance(cols[1], tuple) else None
                            line_s = cols[2] if isinstance(cols[2], int) else 1
                            line_e = cols[3] if isinstance(cols[3], int) else line_s
                            if tag_id is not None:
                                file_tags.append((tag_id, blob_id, line_s, line_e))

            if file_tags:
                tag_bridges = git_parser.map_tags_to_commits(
                    file_tags,
                    c_path,
                    commit_hash_to_id=commit_hash_to_id,
                    file_hunks_map=file_hunks_map,
                )
                for cid, b_id, tid in tag_bridges:
                    G.TE.set(
                        m_bridge_commit_tag.table_id,
                        (
                            cid,
                            b_id,
                            tid,
                        ),
                    )

            # Evacuate internal AST memory from executed ChangeSet
            if hasattr(cs_obj, "cs") and isinstance(cs_obj.cs, list):
                cs_obj.cs.clear()
            if hasattr(cs_obj, "store_dict") and isinstance(cs_obj.store_dict, dict):
                cs_obj.store_dict.clear()
            if hasattr(cs_obj, "cs_result") and isinstance(cs_obj.cs_result, list):
                cs_obj.cs_result.clear()

    # Files reused by Step 4.5's blob deduplication were not parsed: their tags come from m_bridge_tag
    deduped = [
        p for p in sorted(getattr(gp, "deduped_paths", ()))
        if file_hunks_map and p in file_hunks_map and p not in gp.file_pre_extracted_tags and gp.file_blob_ids.get(p)
    ]
    if deduped:
        blob_tags = _load_blob_tags({gp.file_blob_ids[p] for p in deduped})
        for c_path in deduped:
            file_tags = blob_tags.get(gp.file_blob_ids[c_path])
            if not file_tags:
                continue
            tag_bridges = git_parser.map_tags_to_commits(
                file_tags,
                c_path,
                commit_hash_to_id=commit_hash_to_id,
                file_hunks_map=file_hunks_map,
            )
            for cid, b_id, tid in tag_bridges:
                G.TE.set(m_bridge_commit_tag.table_id, (cid, b_id, tid))

    # Stage m_bridge_checkpoint for milestone release version
    checkpoint_cid = gp.VID if (gp.VID and gp.VID != 0) else head_commit_id
    if checkpoint_cid is not None and checkpoint_cid != 0:
        if hasattr(gp, "file_blob_ids") and gp.file_blob_ids:
            blob_items = list(gp.file_blob_ids.items())
        elif getattr(gp, "ChangeSet_Dict", None):
            blob_items = [(f_path, getattr(cs_o, "blob_id", 0)) for f_path, cs_o in gp.ChangeSet_Dict.items()]
        else:
            blob_items = []

        if blob_items:
            existing_count = 0
            existing_fnids: set[int] = set()
            try:
                with G.DB() as db:
                    existing_count = db.count_rows("m_bridge_checkpoint", {"commit_id": checkpoint_cid})
                    if 0 < existing_count < len(blob_items):
                        existing_fnids = set(db.select_values("m_bridge_checkpoint", "fnid", {"commit_id": checkpoint_cid}))
            except Exception as e:
                logger.debug(f"Note on checking m_bridge_checkpoint existing rows: {e}")

            if existing_count >= len(blob_items) and existing_count > 0:
                logger.info(f"Milestone checkpoint for commit {checkpoint_cid} is already fully populated ({existing_count} files); skipping.")
            else:
                staged_count = 0
                for f_path, b_id in blob_items:
                    if b_id != 0:
                        f_nid = get_fnid_for_path(f_path)
                        if f_nid is not None and f_nid not in existing_fnids:
                            existing_fnids.add(f_nid)
                            G.TE.set(
                                m_bridge_checkpoint.table_id,
                                (
                                    checkpoint_cid,
                                    f_nid,
                                    b_id,
                                ),
                            )
                            staged_count += 1
                if staged_count > 0:
                    logger.info(f"Staged {staged_count} milestone snapshot files into m_bridge_checkpoint for commit {checkpoint_cid}.")

    # Step 6.5: Resolve mailing list emails for ingested commits
    try:
        resolve_version_mailing_list_commits(commits, commit_hash_to_id, repo_path)
    except Exception as e:
        logger.warning(f"Mailing list email resolution skipped or encountered error: {e}")
