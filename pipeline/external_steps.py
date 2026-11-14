"""Source facts, CVE list and release statistics steps."""
from core.globalstuff import (
    G,
    COLOR,
)
from core import ingest_state
from core.config import (
    get_parser_config,
)

from typing import Any
from pathlib import Path
from parser import facts_ingest
from pipeline.git_ingest import refresh_person_identities
from pipeline.state import MF, gp, logger




# SHAs known complete for every family, per (fact formats, network, repository); a change of the fact format or of
# the family set, or another repository / network, selects another set
_FACTS_MEMO: dict[tuple, set[bytes]] = {}


def _facts_memo_key() -> tuple:
    """Memo key of the current fact formats, family set, network and repository."""
    return (tuple(sorted(facts_ingest.FAMILIES.items())), getattr(gp, "network_id", None),
            getattr(gp, "repo_id", None), str(getattr(MF, "repo_dir", "")))


def processing_source_facts(version: str) -> int:
    """Step 6.9: extract the per-blob facts `m_blob_facts` lacks of this version's files (parser/facts_ingest.py):
    functions, calls and ops tables of the `.c` / `.h` blobs (family 1), Kernel API macros of the `.c` / `.h` / `.S`
    blobs (family 2), their `#include` directives (family 3), Kconfig definitions (family 4), Documentation
    labels (family 5) and man page facts (family 6), each blob read once; returns how many blobs were extracted."""
    if gp.VID == 0 or not getattr(gp, "file_blob_hashes", None):
        return 0
    blobs = facts_ingest.fact_blobs(gp.file_blob_hashes)

    def read_text(path: str) -> str | None:
        try:
            text = MF.get_file(path, gp.Version_Name)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug(f"[Source Facts] cannot read {path}: {exc}")
            return None
        # get_file() decodes latin-1 (one char per byte): back to the bytes, read as the webapp reads blobs
        return text.encode("latin-1", "replace").decode("utf-8", "replace") if text is not None else None

    try:
        workers = max(1, min(int(get_parser_config().get("facts_workers", 4)), G.CPUS or 1))
    except (TypeError, ValueError):
        workers = 4
    with G.DB() as db:
        if db is None:
            return 0
        key = _facts_memo_key()
        if key not in _FACTS_MEMO:
            _FACTS_MEMO.clear()
            _FACTS_MEMO[key] = set()
        result = facts_ingest.ingest(db, blobs, read_text, workers, known=_FACTS_MEMO[key])
    if result["extracted"]:
        logger.info(
            f"[Source Facts] '{version}': {result['extracted']} of {result['blobs']} source blob(s) extracted "
            f"({result['rows']} fact rows) in {result['seconds']}s."
        )
    return int(result["extracted"])


def processing_cve_list(force: bool = False, kernel_repo: str | None = None, repo_id: int | None = None) -> dict[str, Any] | None:
    """Bring m_cve / m_cve_commit to the HEAD of the CVE list clone (config cve.repo_dir, parser/cve/cve_ingest.py),
    then resolve its abbreviated commit hashes in the kernel repository (m_cve_commit_hash) when one is given.
    Nothing is read when the HEAD is the one already ingested. Returns the ingestion summary (None: no clone)."""
    from parser.cve import cve_ingest
    repo = cve_ingest.repo_dir()
    if repo is None:
        return None
    try:
        with G.DB() as db:
            if db is None:
                return None
            summary = cve_ingest.ingest(db, repo, force=force)
            if kernel_repo and repo_id and summary.get("state") != "missing":
                summary["prefixes"] = cve_ingest.resolve_commit_prefixes(db, kernel_repo, repo_id)
            return summary
    except Exception as exc:  # noqa: BLE001 - the CVE list never blocks parsing
        logger.warning(COLOR.yellow(f"[CVE] Ingesting {repo} failed: {exc}"))
        return {"state": "error", "error": str(exc)}


def processing_release_stats(repo_path: str, repo_id: int) -> dict[str, Any] | None:
    """Summarize the release tags of the repository not summarized yet (parser/git_ast/release_stats.py,
    core/API.md section 10). Skipped while a version ingestion is interrupted: its rollback deletes the
    persons created after its watermark, with every row naming them, so no row may name them before it ran.
    Never blocks parsing: a failure is logged and the run goes on."""
    from parser.git_ast import release_stats
    try:
        with G.DB() as db:
            if db is None:
                return None
            interrupted = db.count_rows("m_ingest_state", {"status": ingest_state.INGEST_PARSING})
            if interrupted:
                logger.info(COLOR.yellow(
                    f"[Release stats] Skipped: {interrupted} version ingestion(s) interrupted; "
                    "they are summarized once the version is recovered."
                ))
                return {"state": "deferred"}
            summary = release_stats.ingest(db, Path(repo_path), repo_id, distributed=bool(getattr(G, "distributed", False)))
            if summary.get("persons_added"):
                refresh_person_identities(db, "release statistics added persons")
    except Exception as exc:  # noqa: BLE001 - release statistics never block parsing
        logger.warning(COLOR.yellow(f"[Release stats] Summarizing the release tags of {repo_path} failed: {exc}"))
        return {"state": "error", "error": str(exc)}
    if summary["state"] == "current":
        logger.info(f"[Release stats] Current ({summary['tags']} release tags).")
    else:
        logger.info(COLOR.green(
            f"[Release stats] Summarized {summary['segments']} release tag(s) in {summary['seconds']}s "
            f"({summary['persons_added']} new person(s))."
        ))
    return summary
