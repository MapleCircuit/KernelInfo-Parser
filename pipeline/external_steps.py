"""Source facts, CVE list and release statistics steps."""
from core.globalstuff import (
    G,
    COLOR,
)
from core import ingest_state
from core.config import (
    get_parser_config,
)

import contextlib
from typing import Any
from pathlib import Path
from parser import facts_ingest
from pipeline.git_ingest import refresh_person_identities
from pipeline.git_network import feature_enabled
from pipeline.state import MF, gp, logger




# SHAs known complete for every family, per (fact formats, network, repository); a change of the fact format or of
# the family set, or another repository / network, selects another set
_FACTS_MEMO: dict[tuple, set[bytes]] = {}


_FACT_FEATURE_MAP: dict[int, str] = {
    2: "api",
    4: "kconfig",
    5: "docs",
    6: "man",
}


def _selected_fact_families() -> tuple[int, ...] | None:
    """Fact family ids enabled for the active repository, or None when all families are enabled."""
    selected = tuple(
        f for f in facts_ingest.FAMILIES
        if f not in _FACT_FEATURE_MAP or feature_enabled(_FACT_FEATURE_MAP[f])
    )
    if len(selected) == len(facts_ingest.FAMILIES):
        return None
    return selected


def _facts_memo_key(families: tuple[int, ...] | None = None) -> tuple:
    """Memo key of the current fact formats, family set, network and repository."""
    return (tuple(sorted(facts_ingest.FAMILIES.items())), families, getattr(gp, "network_id", None),
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
    families = _selected_fact_families()
    with G.DB() as db:
        if db is None:
            return 0
        key = _facts_memo_key(families)
        if key not in _FACTS_MEMO:
            _FACTS_MEMO.clear()
            _FACTS_MEMO[key] = set()
        ingest_kwargs: dict[str, Any] = {"known": _FACTS_MEMO[key]}
        if families is not None:
            ingest_kwargs["families"] = families
        result = facts_ingest.ingest(db, blobs, read_text, workers, **ingest_kwargs)
    if result["extracted"]:
        logger.info(
            f"[Source Facts] '{version}': {result['extracted']} of {result['blobs']} source blob(s) extracted "
            f"({result['rows']} fact rows) in {result['seconds']}s."
        )
    return int(result["extracted"])


def processing_cve_list(
    force: bool = False,
    kernel_repo: str | None = None,
    repo_id: int | None = None,
    pull: bool = False,
) -> dict[str, Any] | None:
    """Bring m_cve / m_cve_commit to the HEAD of the CVE list clone (config cve.repo_dir, parser/cve/cve_ingest.py),
    resolve abbreviated commit hashes in the kernel repository (m_cve_commit_hash), and incrementally sync
    pre-evaluated release verdicts (m_cve_release / m_cve_release_state). Returns the ingestion summary (None: no clone)."""
    from parser.cve import cve_ingest, cve_releases
    repo = cve_ingest.repo_dir()
    if repo is None:
        return None
    try:
        if pull:
            pull_res = cve_ingest.pull_repo(repo)
            if pull_res.get("error"):
                logger.warning(COLOR.yellow(f"[CVE] git pull failed for {repo}: {pull_res['error']}"))
            elif pull_res.get("before") != pull_res.get("after"):
                logger.info(
                    f"[CVE] Pulled {repo}: {str(pull_res.get('before') or '')[:12]} -> {str(pull_res.get('after') or '')[:12]}"
                )
        with G.DB() as db:
            if db is None:
                return None
            summary = cve_ingest.ingest(db, repo, force=force)
            if kernel_repo and repo_id and summary.get("state") != "missing":
                resolved_prefixes: list[str] = []
                summary["prefixes"] = cve_ingest.resolve_commit_prefixes(
                    db, kernel_repo, repo_id, resolved_out=resolved_prefixes
                )
                extra_cves: list[str] = []
                if resolved_prefixes and summary.get("state") != "full":
                    extra_cves = cve_releases.cve_ids_for_prefixes(db, resolved_prefixes)
                summary["releases"] = cve_releases.sync(
                    db,
                    repo_id,
                    kernel_repo=Path(kernel_repo),
                    cve_repo=repo,
                    prev_head=summary.get("prev_head"),
                    changed_ids=summary.get("changed_ids"),
                    extra_cve_ids=extra_cves,
                    force=force,
                    prefixes_resolved=True,
                )
            return summary
    except Exception as exc:  # noqa: BLE001 - the CVE list never blocks parsing
        logger.warning(COLOR.yellow(f"[CVE] Ingesting {repo} failed: {exc}"))
        return {"state": "error", "error": str(exc)}


def processing_cve_releases(
    repo_path: str,
    repo_id: int,
    force: bool = False,
    from_tag: str | None = None,
) -> dict[str, Any] | None:
    """Sync pre-evaluated release-to-CVE verdicts (m_cve_release / m_cve_release_state) for `repo_id`."""
    if not feature_enabled("cve"):
        logger.debug("[CVE Releases] Skipped: 'cve' feature is disabled.")
        return None
    from parser.cve import cve_ingest, cve_releases
    try:
        with G.DB() as db:
            if db is None:
                return None
            return cve_releases.sync(
                db,
                repo_id,
                kernel_repo=Path(repo_path),
                cve_repo=cve_ingest.repo_dir(),
                force=force,
                from_tag=from_tag,
            )
    except Exception as exc:  # noqa: BLE001 - CVE release sync never blocks parsing
        logger.warning(COLOR.yellow(f"[CVE Releases] Syncing release CVEs for {repo_path} failed: {exc}"))
        return {"state": "error", "error": str(exc)}


def processing_release_stats(repo_path: str, repo_id: int) -> dict[str, Any] | None:
    """Summarize the release tags of the repository not summarized yet (parser/git_ast/release_stats.py,
    core/API.md section 10). Skipped while a version ingestion is interrupted: its rollback deletes the
    persons created after its watermark, with every row naming them, so no row may name them before it ran.
    Never blocks parsing: a failure is logged and the run goes on."""
    if not (feature_enabled("commits") or feature_enabled("insights")):
        logger.debug("[Release stats] Skipped: both 'commits' and 'insights' features are disabled.")
        return None
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


def processing_insights(repo_path: str, repo_id: int, from_tag: str | None = None) -> dict[str, Any] | None:
    """Ingest pre-computed Merge Window pull and Subsystem Health summaries for release segments
    (parser/git_ast/release_stats.py)."""
    if not feature_enabled("insights"):
        logger.debug("[Insights] Skipped: 'insights' feature is disabled.")
        return None
    from parser.git_ast import release_stats
    try:
        with G.DB() as db:
            if db is None:
                return None
            return release_stats.ingest_insights(db, Path(repo_path), repo_id, from_tag=from_tag)
    except Exception as exc:  # noqa: BLE001 - insights never block parsing
        logger.warning(COLOR.yellow(f"[Insights] Ingesting insights for {repo_path} failed: {exc}"))
        return {"state": "error", "error": str(exc)}


def processing_kapi(
    repo_path: str,
    repo_id: int | None,
    tag: str | None = None,
    *,
    commit_id: int | None = None,
    arches: tuple[str, ...] = ("x86_64",),
) -> dict[str, Any] | None:
    """Materialize per-revision KAPI catalogs and release summaries (parser/kapi_ingest.py) when the
    'api' feature is enabled and the tag is selected for 'api' pre-parsing."""
    if not feature_enabled("api"):
        logger.debug("[KAPI] Skipped: 'api' feature is disabled.")
        return None
    from core.config import get_parser_config, get_repository, is_tag_selected_for_preparse
    from parser import kapi_ingest
    from parser.kapi_ingest import _git_release_tags

    repo_entry = None
    with contextlib.suppress(Exception):
        repo_entry = get_repository(str(repo_path))
    try:
        workers = max(1, min(int(get_parser_config().get("facts_workers", 4)), G.CPUS or 1))
    except (TypeError, ValueError):
        workers = 1
    try:
        with G.DB() as db:
            if db is None:
                return None
            if tag is not None:
                all_tags = _git_release_tags(str(repo_path))
                if not is_tag_selected_for_preparse(repo_entry, "api", tag, all_tags):
                    logger.debug("[KAPI] Skipped %s: not selected for 'api' pre-parsing.", tag)
                    return {"state": "skipped", "reason": "not_selected", "tag": tag}
                return kapi_ingest.ingest_kapi_release(
                    db,
                    str(repo_path),
                    repo_id,
                    tag,
                    commit_id=commit_id,
                    arches=arches,
                    workers=workers,
                )
            return kapi_ingest.ingest_kapi(
                db,
                str(repo_path),
                repo_id,
                arches=arches,
                workers=workers,
                repo_entry=repo_entry,
            )
    except Exception as exc:  # noqa: BLE001 - KAPI pre-parsing never blocks parsing
        logger.warning(COLOR.yellow(f"[KAPI] Pre-parsing KAPI for {repo_path} ({tag or 'all'}) failed: {exc}"))
        return {"state": "error", "error": str(exc)}

