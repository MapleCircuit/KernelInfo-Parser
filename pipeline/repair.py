"""Repair pipeline over canonical tags."""
from core.globalstuff import (
    G,
    COLOR,
)
from core import ingest_state

from core.DBLayout import (
    TABLES,
    m_git_ref,
)
from pipeline.git_ingest import _forget_rolled_back_rows, _purge_partial_version
from pipeline.state import gp, logger
from pipeline.update import update




def orchestrate_repair_pipeline(
    canonical_tags: list[str],
    version_states: dict[str, tuple[int, str]],
    network_id: int,
    repo_id: int,
    repo_path: str,
    backfill_target: str | None = None,
    dry_run: bool = False,
) -> bool:
    """Detect missing/interrupted versions and re-align downstream differential links.

    Returns True if repair operations were performed (or planned in dry_run).
    """
    gaps: list[tuple[str | None, list[str], str | None]] = []

    if backfill_target:
        target_clean = backfill_target.strip()
        matched_idx = None
        for candidate in (target_clean, f"v{target_clean}", target_clean.lstrip("v")):
            if candidate in canonical_tags:
                matched_idx = canonical_tags.index(candidate)
                break
        if matched_idx is None:
            logger.error(COLOR.red(f"[Repair Pipeline] Target version '{backfill_target}' not found in repository tag sequence."))
            return False
        v_gap = canonical_tags[matched_idx]
        v_pred = canonical_tags[matched_idx - 1] if matched_idx > 0 else None
        v_succ = next((t for t in canonical_tags[matched_idx + 1:] if version_states.get(t, (0, "new"))[1] == "done"), None)
        gaps.append((v_pred, [v_gap], v_succ))
    else:
        # General repair: scan canonical_tags for missing versions that have an ingested successor
        i = 0
        n = len(canonical_tags)
        while i < n:
            st = version_states.get(canonical_tags[i], (0, "new"))[1]
            if st != "done":
                gap_start = i
                while i < n and version_states.get(canonical_tags[i], (0, "new"))[1] != "done":
                    i += 1
                missing_list = canonical_tags[gap_start:i]
                v_succ = canonical_tags[i] if i < n and version_states.get(canonical_tags[i], (0, "new"))[1] == "done" else None
                if v_succ:
                    v_pred = canonical_tags[gap_start - 1] if gap_start > 0 else None
                    gaps.append((v_pred, missing_list, v_succ))
            else:
                i += 1

    if not gaps:
        logger.info(COLOR.green("[Repair Pipeline] No missing versions or misaligned successors detected. Database history is contiguous."))
        return False

    if dry_run:
        for pred, missing_list, succ in gaps:
            logger.info(COLOR.cyan(
                f"[Repair Pipeline - Dry Run] Gap detected: missing {missing_list} between predecessor '{pred}' and successor '{succ}'."
            ))
            logger.info(f"  -> Action 1: Ingest {missing_list} in sequence using baseline '{pred}'.")
            if succ:
                logger.info(f"  -> Action 2: Purge version differential rows of successor '{succ}' and re-ingest with base '{missing_list[-1]}'.")
        return True

    for pred, missing_list, succ in gaps:
        current_base = pred
        for m_ver in missing_list:
            logger.info(COLOR.cyan(f"[Repair Pipeline] Backfilling missing version '{m_ver}' (prior base: '{current_base}')..."))
            if current_base and current_base in version_states:
                gp.Old_VID = version_states[current_base][0]
                gp.Old_Version_Name = current_base
            else:
                gp.Old_VID = 0
                gp.Old_Version_Name = None
            cid_m, st_m = version_states.get(m_ver, (0, "new"))
            if st_m == "interrupted" and cid_m > 0:
                with G.DB() as db:
                    if db is not None:
                        if not ingest_state.rollback_version(db, network_id, cid_m, TABLES):
                            _purge_partial_version(db, cid_m)
                _forget_rolled_back_rows()
            update(m_ver, repo_path=repo_path)
            with G.DB() as db:
                v_row = db.select(m_git_ref, (None, repo_id, None, m_ver, None, None, None, None))
                if v_row and v_row[4]:
                    version_states[m_ver] = (v_row[4], "done")
            current_base = m_ver

        if succ and succ in version_states:
            succ_cid = version_states[succ][0]
            logger.info(COLOR.yellow(
                f"[Repair Pipeline] Re-aligning downstream successor '{succ}' (commit_id {succ_cid}) with new base '{current_base}'..."
            ))
            with G.DB() as db:
                if db is not None:
                    pred_cid = version_states.get(current_base, (0,))[0] if current_base else None
                    ingest_state.purge_version_differential_rows(db, succ_cid, pred_commit_id=pred_cid)
            _forget_rolled_back_rows()
            if current_base and current_base in version_states:
                gp.Old_VID = version_states[current_base][0]
                gp.Old_Version_Name = current_base
            update(succ, repo_path=repo_path)
            logger.info(COLOR.green(f"[Repair Pipeline] Successfully re-aligned downstream successor '{succ}' with base '{current_base}'."))

    return True
