"""Source facts, CVE list and release statistics steps."""
from core.globalstuff import (
    G,
    COLOR,
)
from core import ingest_state
from core.config import (
    get_repository,
    resolve_repo_insights,
    resolve_preparse_tags,
)

import contextlib
import inspect
import time
from typing import Any
from collections.abc import Mapping, Sequence
from pathlib import Path
from parser import facts_ingest
from parser.git_ast.release_order import parse_tag, release_key, release_ranges, series_ord_for
from pipeline.git_ingest import refresh_person_identities
from pipeline.git_network import feature_enabled
from pipeline.multicore import preparse_worker_count, run_unified_preparse_pool
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

    workers = preparse_worker_count()
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


def processing_insights(
    repo_path: str,
    repo_id: int,
    from_tag: str | None = None,
    *,
    workers: int | None = None,
) -> dict[str, Any] | None:
    """Ingest pre-computed Merge Window pull, Subsystem Health, and delta summaries for release segments
    (parser/git_ast/release_stats.py)."""
    if not feature_enabled("insights"):
        logger.debug("[Insights] Skipped: 'insights' feature is disabled.")
        return None
    from parser.git_ast import release_stats
    eff_workers = preparse_worker_count(workers=workers)
    try:
        with G.DB() as db:
            if db is None:
                return None
            kwargs: dict[str, Any] = {"from_tag": from_tag}
            if "workers" in inspect.signature(release_stats.ingest_insights).parameters:
                kwargs["workers"] = eff_workers
            return release_stats.ingest_insights(db, Path(repo_path), repo_id, **kwargs)
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
    workers: int | None = None,
) -> dict[str, Any] | None:
    """Materialize per-revision KAPI catalogs and release summaries (parser/kapi_ingest.py) when the
    'api' feature is enabled and the tag is selected for 'api' pre-parsing."""
    if not feature_enabled("api"):
        logger.debug("[KAPI] Skipped: 'api' feature is disabled.")
        return None
    from core.config import is_tag_selected_for_preparse
    from parser import kapi_ingest
    from parser.kapi_ingest import _git_release_tags

    repo_entry = None
    with contextlib.suppress(Exception):
        repo_entry = get_repository(str(repo_path))
    eff_workers = preparse_worker_count(workers=workers)
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
                    workers=eff_workers,
                )
            return kapi_ingest.ingest_kapi(
                db,
                str(repo_path),
                repo_id,
                arches=arches,
                workers=eff_workers,
                repo_entry=repo_entry,
            )
    except Exception as exc:  # noqa: BLE001 - KAPI pre-parsing never blocks parsing
        logger.warning(COLOR.yellow(f"[KAPI] Pre-parsing KAPI for {repo_path} ({tag or 'all'}) failed: {exc}"))
        return {"state": "error", "error": str(exc)}


_UNIFIED_PREPARSE_SHARED: dict[str, Any] = {}
_PRE_IN_CHUNK = 1000
_PREPARSE_CHECK_TABLES = (
    "m_release_segment",
    "m_release_person",
    "m_commit_release",
    "m_person_first_release",
    "m_release_pull",
    "m_release_section_health",
    "m_cve_commit",
    "m_cve_commit_hash",
    "m_commit",
    "m_git_ref",
    "m_repository",
    "m_ingest_state",
    "m_blob_facts",
    "m_kapi_catalog",
    "m_kapi_release_summary",
    "insights_subsystem_version_delta",
    "insights_subsystem_author_delta",
    "insights_stable_version_delta",
)


def _unified_preparse_worker(item: dict[str, Any]) -> dict[str, Any]:
    """Pure worker entrypoint: run one Git pass per release range for deltas, segment stats, and KAPI."""
    started = time.perf_counter()
    tag = item["tag"]
    repo_path = item["repo_path"]
    repo = Path(repo_path)
    shared = _UNIFIED_PREPARSE_SHARED
    insights_cfg = item.get("insights_cfg") or shared.get("insights_cfg") or {}
    catchall_section = str(insights_cfg.get("catchall_section", "THE REST") or "THE REST")
    maintainers_file = str(insights_cfg.get("maintainers_file", "MAINTAINERS") or "MAINTAINERS")

    out: dict[str, Any] = {
        "tag": tag,
        "commit_id": item.get("commit_id"),
        "segment_id": item.get("segment_id"),
        "need_subsystems": bool(item.get("need_subsystems")),
        "need_stable": bool(item.get("need_stable")),
        "need_pulls": bool(item.get("need_pulls")),
        "need_api": bool(item.get("need_api")),
        "need_segment": bool(item.get("need_segment")),
        "deltas": None,
        "pulls": None,
        "sec_stats": None,
        "sections": None,
        "segment": None,
        "kapi": None,
        "errors": [],
    }

    if out["need_subsystems"] or out["need_stable"] or out["need_pulls"]:
        try:
            from parser.git_ast import insights_deltas
            from parser.git_ast import release_stats
            from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher

            same_span = bool(item.get("same_span", True))
            want_deltas = out["need_subsystems"] or out["need_stable"]
            if want_deltas:
                deltas = insights_deltas.extract_release_deltas_from_git(
                    repo,
                    tag,
                    item["sha"],
                    item.get("parent_tag"),
                    item.get("parent_sha"),
                    catchall_section=catchall_section,
                    cve_commit_hashes=shared.get("cve_commit_hashes"),
                    sha_to_release=shared.get("sha_to_release"),
                    compute_pulls=bool(out["need_pulls"] and same_span),
                    insights_cfg=insights_cfg,
                    maintainers_file=maintainers_file,
                )
                out["deltas"] = deltas
                sections = deltas.get("sections") if isinstance(deltas, dict) else None
                out["sections"] = sections
                if out["need_pulls"] and same_span and isinstance(deltas, dict):
                    out["pulls"] = deltas.get("pulls")
                    out["sec_stats"] = deltas.get("sec_stats")
                elif out["need_pulls"]:
                    sec_list = list(sections or ())
                    matcher = MaintainerMatcher(sec_list)
                    if not sec_list:
                        sec_list, matcher = release_stats.load_tag_maintainers(
                            None, repo, tag, item["sha"], maintainers_file=maintainers_file
                        )
                        out["sections"] = sec_list
                    pulls, sec_stats = release_stats.compute_tag_insights(
                        repo, tag, item["sha"], item.get("pull_prev_sha"), sec_list, matcher, insights_cfg=insights_cfg
                    )
                    out["pulls"] = pulls
                    out["sec_stats"] = sec_stats
            elif out["need_pulls"]:
                sec_list, matcher = release_stats.load_tag_maintainers(
                    None, repo, tag, item["sha"], maintainers_file=maintainers_file
                )
                pulls, sec_stats = release_stats.compute_tag_insights(
                    repo, tag, item["sha"], item.get("pull_prev_sha"), sec_list, matcher, insights_cfg=insights_cfg
                )
                out["sections"] = sec_list
                out["pulls"] = pulls
                out["sec_stats"] = sec_stats
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"deltas: {exc}")

    if out["need_segment"]:
        try:
            from parser.git_ast import release_stats
            out["segment"] = release_stats.compute(repo, item["seg_range"])
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"segment: {exc}")

    if out["need_api"] and item.get("commit_id") is not None:
        try:
            from parser import kapi_ingest
            out["kapi"] = kapi_ingest.extract_kapi_release_payload(
                repo_path,
                tag,
                arches=tuple(item.get("arches") or ("x86_64",)),
                existing_facts=shared.get("existing_facts"),
                workers=int(item.get("kapi_workers") or 1),
            )
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"kapi: {exc}")

    out["seconds"] = round(time.perf_counter() - started, 3)
    return out


def _preparse_existing_tables(db: Any) -> set[str]:
    """Return subset of pre-parse tables present in `db`."""
    if hasattr(db, "existing_tables"):
        return set(db.existing_tables(_PREPARSE_CHECK_TABLES))
    present: set[str] = set()
    if hasattr(db, "table_exists"):
        for name in _PREPARSE_CHECK_TABLES:
            if db.table_exists(name):
                present.add(name)
    return present


def _preparse_commit_ids(
    db: Any,
    existing_tables: set[str],
    repo_id: int | None,
    api_tags: Sequence[str],
    tag_shas: Mapping[str, str],
) -> dict[str, int]:
    """Bulk-resolve `tag -> commit_id` from `m_git_ref` and `m_commit`."""
    resolved: dict[str, int] = {}
    tags_list = list(dict.fromkeys(api_tags))
    if not tags_list:
        return resolved

    if "m_git_ref" in existing_tables:
        for i in range(0, len(tags_list), _PRE_IN_CHUNK):
            chunk = tags_list[i : i + _PRE_IN_CHUNK]
            where: dict[str, Any] = {"ref_name": chunk}
            if repo_id is not None:
                where["repo_id"] = int(repo_id)
            rows = db.select_rows("m_git_ref", ["ref_name", "commit_id"], where, order_by=["-commit_id"], as_dict=True)
            for row in rows:
                rname = str(row.get("ref_name") or "")
                cid = row.get("commit_id")
                if rname and cid is not None and rname not in resolved:
                    resolved[rname] = int(cid)

    missing_tags = [t for t in tags_list if t not in resolved and tag_shas.get(t)]
    if missing_tags and "m_commit" in existing_tables:
        network_id: int | None = None
        if repo_id is not None and "m_repository" in existing_tables:
            rep = db.select_one("m_repository", ["network_id"], {"repo_id": int(repo_id)}, as_dict=True)
            if rep and rep.get("network_id") is not None:
                network_id = int(rep["network_id"])
        sha_to_tags: dict[bytes, list[str]] = {}
        for t in missing_tags:
            try:
                sha_bytes = bytes.fromhex(tag_shas[t])
            except ValueError:
                continue
            sha_to_tags.setdefault(sha_bytes, []).append(t)
        sha_keys = list(sha_to_tags.keys())
        for i in range(0, len(sha_keys), _PRE_IN_CHUNK):
            chunk = sha_keys[i : i + _PRE_IN_CHUNK]
            c_where: dict[str, Any] = {"commit_hash": chunk}
            if network_id is not None:
                c_where["network_id"] = network_id
            rows = db.select_rows("m_commit", ["commit_id", "commit_hash"], c_where, order_by=["-commit_id"], as_dict=True)
            for row in rows:
                cid = row.get("commit_id")
                ch = row.get("commit_hash")
                if cid is None or not ch:
                    continue
                for t in sha_to_tags.get(bytes(ch), ()):
                    resolved.setdefault(t, int(cid))
    return resolved


def _preparse_current_kapi_cids(
    db: Any,
    existing_tables: set[str],
    cids: Sequence[int],
    arch_list: tuple[str, ...],
) -> set[int]:
    """Return `commit_id`s whose KAPI catalogs and release summaries are current for `arch_list`."""
    if not cids or "m_kapi_catalog" not in existing_tables or "m_kapi_release_summary" not in existing_tables:
        return set()
    from parser import kapi_facts as kf
    from parser.kapi_catalog import ARCH_INDEPENDENT_KINDS, CATALOG_FORMAT

    unique_cids = sorted(set(int(c) for c in cids))
    shared_kinds: dict[int, set[str]] = {}
    sum_arches: dict[int, set[str]] = {}
    needed_shared = set(ARCH_INDEPENDENT_KINDS)
    needed_arches = set(arch_list)

    for i in range(0, len(unique_cids), _PRE_IN_CHUNK):
        chunk = unique_cids[i : i + _PRE_IN_CHUNK]
        cat_rows = db.select_rows(
            "m_kapi_catalog",
            ["commit_id", "kind", "catalog_format", "kapi_format"],
            {"commit_id": chunk, "arch": ""},
            as_dict=True,
        )
        for r in cat_rows:
            if int(r.get("catalog_format") or 0) == CATALOG_FORMAT and int(r.get("kapi_format") or 0) == kf.KAPI_FORMAT:
                shared_kinds.setdefault(int(r["commit_id"]), set()).add(str(r["kind"]))

        sum_rows = db.select_rows(
            "m_kapi_release_summary",
            ["commit_id", "arch", "catalog_format", "kapi_format"],
            {"commit_id": chunk, "arch": list(arch_list)},
            as_dict=True,
        )
        for r in sum_rows:
            if int(r.get("catalog_format") or 0) == CATALOG_FORMAT and int(r.get("kapi_format") or 0) == kf.KAPI_FORMAT:
                sum_arches.setdefault(int(r["commit_id"]), set()).add(str(r["arch"]))

    return {
        cid
        for cid in unique_cids
        if needed_shared.issubset(shared_kinds.get(cid, set())) and needed_arches.issubset(sum_arches.get(cid, set()))
    }


def _preparse_stored_delta_parents(
    db: Any,
    table_name: str,
    existing_tables: set[str],
    tags_list: Sequence[str],
) -> dict[str, str]:
    """Return `{version_tag: parent_tag}` for sentinel rows currently stored in `table_name`."""
    if table_name not in existing_tables or not tags_list:
        return {}
    if not db.column_exists(table_name, "version_tag"):
        return {}

    unique_tags = list(dict.fromkeys(tags_list))
    stored: dict[str, str] = {}
    has_sub = db.column_exists(table_name, "subsystem_name")
    has_parent = db.column_exists(table_name, "parent_tag")
    cols = ["version_tag", "parent_tag"] if has_parent else ["version_tag"]
    for i in range(0, len(unique_tags), _PRE_IN_CHUNK):
        chunk = unique_tags[i : i + _PRE_IN_CHUNK]
        where: dict[str, Any] = {"version_tag": chunk}
        if has_sub:
            where["subsystem_name"] = ""
        for row in db.select_rows(table_name, cols, where, as_dict=False):
            vt_raw = row[0]
            pt_raw = row[1] if has_parent and len(row) > 1 else ""
            vt = vt_raw.decode("utf-8", "replace") if isinstance(vt_raw, (bytes, bytearray)) else str(vt_raw or "")
            pt = pt_raw.decode("utf-8", "replace") if isinstance(pt_raw, (bytes, bytearray)) else str(pt_raw or "")
            if vt:
                stored[vt] = pt
    return stored


def _preparse_cve_hashes(
    db: Any,
    existing_tables: set[str],
    repo_id: int | None,
) -> set[bytes] | None:
    """Pre-read 20-byte CVE fix commit hashes from `m_cve_commit` and `m_cve_commit_hash`."""
    with contextlib.suppress(ImportError):
        from parser.git_ast import insights_deltas
        if hasattr(insights_deltas, "load_cve_commit_hashes"):
            loaded = insights_deltas.load_cve_commit_hashes(db, repo_id)
            return loaded or None

    hashes: set[bytes] = set()
    if "m_cve_commit" in existing_tables:
        for val in db.select_values("m_cve_commit", "commit_hash", {"role": 1}, distinct=True):
            if isinstance(val, (bytes, bytearray)) and len(val) == 20:
                hashes.add(bytes(val))
            elif val:
                s = str(val).strip().lower()
                if len(s) == 40:
                    with contextlib.suppress(ValueError):
                        hashes.add(bytes.fromhex(s))
    if repo_id is not None and "m_cve_commit_hash" in existing_tables:
        for val in db.select_values("m_cve_commit_hash", "commit_hash", {"repo_id": int(repo_id)}, distinct=True):
            if val:
                hashes.add(bytes(val))
    return hashes or None


def _preparse_sha_to_release(
    db: Any,
    repo: Path,
    tag_shas: Mapping[str, str],
    existing_tables: set[str],
    repo_id: int | None,
    seg_id_to_tag: Mapping[int, str],
    workers: int,
) -> dict[str, tuple[str, int]] | None:
    """Pre-read `commit_sha -> (release_tag, series_ord)` from `m_commit_release`, else Git."""
    mapping: dict[str, tuple[str, int]] = {}
    if repo_id is not None and seg_id_to_tag and "m_commit_release" in existing_tables:
        rows = db.select_rows(
            "m_commit_release",
            ["commit_hash", "segment_id", "chain"],
            {"repo_id": int(repo_id)},
            order_by=["chain"],
            as_dict=False,
        )
        for ch, sid, _chain in rows:
            tag = seg_id_to_tag.get(int(sid))
            if tag and ch:
                sha_hex = bytes(ch).hex() if isinstance(ch, (bytes, bytearray)) else str(ch).strip().lower()
                if sha_hex and sha_hex not in mapping:
                    mapping[sha_hex] = (tag, series_ord_for(tag))
    if mapping:
        return mapping
    with contextlib.suppress(ImportError):
        from parser.git_ast import insights_deltas
        if hasattr(insights_deltas, "build_sha_to_release"):
            return insights_deltas.build_sha_to_release(repo, tag_shas, workers=workers) or None
    return None


def _preparse_existing_kapi_facts(db: Any, existing_tables: set[str]) -> dict[bytes, bytes] | None:
    """Pre-read family 2 `blob_hash -> raw data` from `m_blob_facts`."""
    if "m_blob_facts" not in existing_tables:
        return None
    from parser import kapi_facts as kf
    rows = db.select_rows(
        "m_blob_facts",
        ["blob_hash", "data"],
        {"family": kf.FAMILY, "format": kf.KAPI_FORMAT},
        as_dict=False,
    )
    if not rows:
        return None
    return {bytes(b_hash): (bytes(data) if data is not None else b"") for b_hash, data in rows if b_hash}


def _resolve_preparse_target_sets(
    all_tags: list[str],
    tag_shas: dict[str, str],
    repo_entry: dict[str, Any] | None,
    tags: Sequence[str] | None,
    from_tag: str | None,
    want_insights: bool,
    want_stable: bool,
    want_api: bool,
) -> tuple[set[str], set[str], set[str]]:
    """Compute `(subsystems_tags, stable_tags, api_tags)` honoring `tags` override and `from_tag`."""
    if tags is not None:
        override = [t for t in dict.fromkeys(str(x).strip() for x in tags if str(x).strip()) if t in tag_shas]
        if from_tag:
            fk = release_key(from_tag)
            override = [t for t in override if release_key(t) >= fk]
        sub_tags = {t for t in override if (p := parse_tag(t)) is None or p["kind"] != "stable"} if want_insights else set()
        st_tags = {t for t in override if (p := parse_tag(t)) is not None and p["kind"] == "stable"} if want_stable else set()
        kapi_tags = set(override) if want_api else set()
        return sub_tags, st_tags, kapi_tags

    sub_tags = set(resolve_preparse_tags(repo_entry, "insights", all_tags)) if want_insights else set()
    st_tags = (
        {t for t in all_tags if (p := parse_tag(t)) is not None and p["kind"] == "stable"}
        if want_stable
        else set()
    )
    kapi_tags = set(resolve_preparse_tags(repo_entry, "api", all_tags)) if want_api else set()
    if from_tag:
        fk = release_key(from_tag)
        sub_tags = {t for t in sub_tags if release_key(t) >= fk}
        st_tags = {t for t in st_tags if release_key(t) >= fk}
        kapi_tags = {t for t in kapi_tags if release_key(t) >= fk}
    return sub_tags, st_tags, kapi_tags


def processing_unified_preparse(
    repo_path: str,
    repo_id: int | None,
    *,
    include_insights: bool = True,
    include_stable: bool = True,
    include_api: bool = True,
    tags: Sequence[str] | None = None,
    from_tag: str | None = None,
    arches: tuple[str, ...] = ("x86_64",),
    workers: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run unified single-pass multi-core pre-parsing for Subsystem Insights, Stable deltas, and KAPI.

    Resolves release tags and ranges once, pre-reads DB state in the parent, executes pure Git
    extraction across all CPU cores via `run_unified_preparse_pool`, and persists all outputs
    serially inside parent `with db.transaction():` blocks.
    """
    started = time.perf_counter()
    want_insights = bool(include_insights) and feature_enabled("insights")
    want_stable = bool(include_stable) and feature_enabled("insights")
    want_api = bool(include_api) and feature_enabled("api")
    if not (want_insights or want_stable or want_api):
        logger.debug("[Pre-parse] Skipped: requested features are disabled.")
        return {
            "state": "skipped",
            "reason": "features_disabled",
            "tags": 0,
            "work_items": 0,
            "insights_built": 0,
            "stable_built": 0,
            "api_built": 0,
            "segments_built": 0,
            "skipped_current": 0,
            "api_skipped_no_commit": 0,
            "workers": 0,
            "errors": [],
            "seconds": 0.0,
        }

    from parser import kapi_ingest
    from parser.git_ast import release_stats

    repo_entry = None
    with contextlib.suppress(Exception):
        repo_entry = get_repository(str(repo_path))
    insights_cfg = resolve_repo_insights(repo_entry)
    catchall_section = str(insights_cfg.get("catchall_section", "THE REST") or "THE REST")
    arch_list = tuple(dict.fromkeys((a or "x86_64").strip().lower() for a in (arches or ("x86_64",)) if str(a).strip())) or ("x86_64",)

    repo = Path(repo_path)
    tag_shas = release_stats.release_tags(repo)
    if tags is not None:
        for raw_t in tags:
            t_name = str(raw_t or "").strip()
            if t_name and t_name not in tag_shas:
                sha = kapi_ingest._resolve_commit_sha(str(repo_path), t_name)
                if sha:
                    tag_shas[t_name] = sha

    all_tags = sorted(tag_shas.keys(), key=release_key)
    ranges = release_ranges(all_tags)
    range_by_tag = {r["tag"]: r for r in ranges}

    sub_tags, st_tags, kapi_tags = _resolve_preparse_target_sets(
        all_tags, tag_shas, repo_entry, tags, from_tag, want_insights, want_stable, want_api
    )
    candidate_tags = sorted(sub_tags | st_tags | kapi_tags, key=release_key)
    if not candidate_tags:
        return {
            "state": "current",
            "tags": 0,
            "work_items": 0,
            "insights_built": 0,
            "stable_built": 0,
            "api_built": 0,
            "segments_built": 0,
            "skipped_current": 0,
            "api_skipped_no_commit": 0,
            "workers": 0,
            "errors": [],
            "seconds": round(time.perf_counter() - started, 2),
        }

    with G.DB() as db:
        if db is None:
            return {
                "state": "unavailable",
                "tags": len(candidate_tags),
                "work_items": 0,
                "insights_built": 0,
                "stable_built": 0,
                "api_built": 0,
                "segments_built": 0,
                "skipped_current": 0,
                "api_skipped_no_commit": 0,
                "workers": 0,
                "errors": [],
                "seconds": round(time.perf_counter() - started, 2),
            }

        existing_tables = _preparse_existing_tables(db)
        interrupted = (
            db.count_rows("m_ingest_state", {"status": ingest_state.INGEST_PARSING})
            if "m_ingest_state" in existing_tables
            else 0
        )
        if interrupted:
            logger.warning(COLOR.yellow(
                f"[Pre-parse] {interrupted} version ingestion(s) interrupted; "
                "deferring m_release_segment / m_maintainer_person writes."
            ))

        stored_segs = (
            release_stats.stored_segments(db, repo_id)
            if (repo_id is not None and "m_release_segment" in existing_tables)
            else {}
        )
        seg_id_by_tag = {t: row[0] for t, row in stored_segs.items()}
        seg_id_to_tag = {row[0]: t for t, row in stored_segs.items()}

        api_commit_ids = (
            _preparse_commit_ids(db, existing_tables, repo_id, sorted(kapi_tags), tag_shas)
            if kapi_tags
            else {}
        )
        current_kapi_cids = (
            _preparse_current_kapi_cids(db, existing_tables, list(api_commit_ids.values()), arch_list)
            if (not force and api_commit_ids)
            else set()
        )

        delta_candidate_tags = sorted(sub_tags | st_tags, key=release_key)
        delta_parent_map: dict[str, str | None] = {}
        if delta_candidate_tags:
            try:
                from parser.git_ast import insights_deltas as _id_mod
                if hasattr(_id_mod, "_resolve_delta_parents"):
                    delta_parent_map = _id_mod._resolve_delta_parents(
                        db, tag_shas, delta_candidate_tags, explicit_tags=(tags is not None)
                    )
                else:
                    delta_parent_map = {
                        t: (range_by_tag[t]["range_base"] if t in range_by_tag else None)
                        for t in delta_candidate_tags
                    }
            except ImportError:
                delta_parent_map = {
                    t: (range_by_tag[t]["range_base"] if t in range_by_tag else None)
                    for t in delta_candidate_tags
                }

        candidate_sids = [seg_id_by_tag[t] for t in candidate_tags if t in seg_id_by_tag]
        if not force:
            stored_subsys = _preparse_stored_delta_parents(
                db, "insights_subsystem_version_delta", existing_tables, candidate_tags
            )
            stored_stable = _preparse_stored_delta_parents(
                db, "insights_stable_version_delta", existing_tables, candidate_tags
            )
            if candidate_sids and "m_release_pull" in existing_tables and "m_release_section_health" in existing_tables:
                pull_done_sids: set[int] = set()
                health_done_sids: set[int] = set()
                for i in range(0, len(candidate_sids), _PRE_IN_CHUNK):
                    chunk = candidate_sids[i : i + _PRE_IN_CHUNK]
                    pull_done_sids.update(
                        int(s) for s in db.select_values(
                            "m_release_pull", "segment_id",
                            {"segment_id": chunk, "format": release_stats.MW_FORMAT}, distinct=True,
                        )
                    )
                    health_done_sids.update(
                        int(s) for s in db.select_values(
                            "m_release_section_health", "segment_id",
                            {"segment_id": chunk, "format": release_stats.HEALTH_FORMAT}, distinct=True,
                        )
                    )
            else:
                pull_done_sids = set()
                health_done_sids = set()
        else:
            stored_subsys = {}
            stored_stable = {}
            pull_done_sids, health_done_sids = set(), set()

        work_items: list[dict[str, Any]] = []
        skipped_current = 0
        api_skipped_no_commit = 0

        for tag in candidate_tags:
            sha = tag_shas[tag]
            rng = range_by_tag.get(tag)
            parsed = parse_tag(tag)
            is_final_main = bool(parsed and parsed["kind"] == "mainline" and parsed["final"])
            sid = seg_id_by_tag.get(tag)
            cid = api_commit_ids.get(tag)
            want_parent = delta_parent_map.get(tag) or ""

            need_sub = False
            need_pulls = False
            if tag in sub_tags:
                sub_row_done = (tag in stored_subsys) and (stored_subsys.get(tag) == want_parent)
                pulls_ok = (
                    (not is_final_main)
                    or ("m_release_pull" not in existing_tables)
                    or (sid is not None and sid in pull_done_sids and sid in health_done_sids)
                )
                need_sub = force or not sub_row_done
                need_pulls = is_final_main and (force or not pulls_ok)

            need_st = False
            if tag in st_tags:
                st_row_done = (
                    (tag in stored_stable)
                    and (stored_stable.get(tag) == want_parent)
                    and (tag in stored_subsys)
                    and (stored_subsys.get(tag) == want_parent)
                )
                need_st = force or not st_row_done

            need_api = False
            if tag in kapi_tags:
                if cid is None:
                    api_skipped_no_commit += 1
                else:
                    need_api = force or (cid not in current_kapi_cids)

            need_seg = False
            seg_range: dict[str, Any] | None = None
            if (need_sub or need_st or need_pulls) and repo_id is not None and "m_release_segment" in existing_tables and not interrupted and rng is not None:
                base = rng["segment_base"]
                diff_base = rng["range_base"]
                want_seg = (
                    bytes.fromhex(sha),
                    base or "",
                    bytes.fromhex(tag_shas[base]) if base else release_stats._NO_HASH,
                    diff_base or "",
                    release_stats.FORMAT,
                )
                if tag not in stored_segs or stored_segs[tag][1:6] != want_seg:
                    need_seg = True
                    seg_range = {
                        **rng,
                        "sha": sha,
                        "base_sha": tag_shas[base] if base else None,
                        "diff_base_sha": tag_shas[diff_base] if diff_base else None,
                        "chain": release_stats.CHAIN_MAINLINE if rng["kind"] == "mainline" else release_stats.CHAIN_STABLE,
                    }

            if not (need_sub or need_st or need_pulls or need_api or need_seg):
                skipped_current += 1
                continue

            parent_tag = delta_parent_map.get(tag)
            parent_sha = tag_shas.get(parent_tag) if parent_tag else None
            pull_prev_tag = rng["range_base"] if rng else None
            pull_prev_sha = tag_shas.get(pull_prev_tag) if pull_prev_tag else None
            work_items.append({
                "tag": tag,
                "sha": sha,
                "parent_tag": parent_tag,
                "parent_sha": parent_sha,
                "pull_prev_sha": pull_prev_sha,
                "same_span": (parent_sha or "") == (pull_prev_sha or ""),
                "replace_deltas": bool(force or tag in stored_subsys or tag in stored_stable),
                "repo_path": str(repo_path),
                "commit_id": cid,
                "segment_id": sid,
                "arches": arch_list,
                "need_subsystems": need_sub,
                "need_stable": need_st,
                "need_pulls": need_pulls,
                "need_api": need_api,
                "need_segment": need_seg,
                "seg_range": seg_range,
                "kapi_workers": 1,
            })

        if not work_items:
            return {
                "state": "current",
                "tags": len(candidate_tags),
                "work_items": 0,
                "insights_built": 0,
                "stable_built": 0,
                "api_built": 0,
                "segments_built": 0,
                "skipped_current": skipped_current,
                "api_skipped_no_commit": api_skipped_no_commit,
                "workers": 0,
                "errors": [],
                "seconds": round(time.perf_counter() - started, 2),
            }

        eff_workers = preparse_worker_count(task_count=len(work_items), workers=workers)
        if len(work_items) == 1:
            work_items[0]["kapi_workers"] = preparse_worker_count(workers=workers)

        need_any_deltas = any(w["need_subsystems"] or w["need_stable"] for w in work_items)
        need_any_stable = any(w["need_stable"] for w in work_items)
        need_any_api = any(w["need_api"] for w in work_items)
        need_any_seg = any(w["need_segment"] for w in work_items)

        cve_commit_hashes = _preparse_cve_hashes(db, existing_tables, repo_id) if need_any_deltas else None
        sha_to_release = (
            _preparse_sha_to_release(db, repo, tag_shas, existing_tables, repo_id, seg_id_to_tag, eff_workers)
            if need_any_stable
            else None
        )
        existing_facts = _preparse_existing_kapi_facts(db, existing_tables) if need_any_api else None

        persons = release_stats._Persons(db, distributed=bool(getattr(G, "distributed", False))) if need_any_seg else None
        replaced_mainline = False
        first_releases: dict[int, tuple[tuple, int]] | None = None
        if need_any_seg:
            replaced_mainline = any(
                w["tag"] in stored_segs and w["seg_range"]["chain"] == release_stats.CHAIN_MAINLINE
                for w in work_items
                if w["need_segment"]
            )
            tag_of_mainline = {row[0]: t for t, row in stored_segs.items() if row[6] == release_stats.CHAIN_MAINLINE}
            first_releases = None if replaced_mainline else release_stats._first_releases(db, repo_id, tag_of_mainline)
            old_ids = [stored_segs[w["tag"]][0] for w in work_items if w["need_segment"] and w["tag"] in stored_segs]
            if old_ids and "m_commit_release" in existing_tables:
                with db.transaction():
                    db.delete_rows("m_commit_release", {"segment_id": old_ids})

        counters = {
            "insights_built": 0,
            "stable_built": 0,
            "api_built": 0,
            "segments_built": 0,
        }
        errors: list[dict[str, str]] = []
        maintainers_file = str(insights_cfg.get("maintainers_file", "MAINTAINERS") or "MAINTAINERS")

        def _persist_result(item: dict[str, Any], res: dict[str, Any]) -> None:
            tag_name = item["tag"]
            for err in res.get("errors") or ():
                errors.append({"tag": tag_name, "error": str(err)})

            seg_data = res.get("segment")
            deltas = res.get("deltas")
            pulls = res.get("pulls")
            sec_stats = res.get("sec_stats")
            sections = res.get("sections") or ()
            if item.get("need_pulls") and pulls is not None and sections and hasattr(release_stats, "_lookup_tag_section_ids"):
                sec_id_map = release_stats._lookup_tag_section_ids(db, tag_name, item["sha"], maintainers_file)
                if sec_id_map:
                    for sec in sections:
                        k = getattr(sec, "name", "").strip().lower()
                        if k in sec_id_map:
                            sec.sec_id = sec_id_map[k]
                    for pl in pulls:
                        if not pl.get("sec_id") and pl.get("sec_name"):
                            k = str(pl["sec_name"]).strip().lower()
                            if k in sec_id_map:
                                pl["sec_id"] = sec_id_map[k]

            try:
                if seg_data is not None and persons is not None:
                    persons.mint(db, (key for key, _role in seg_data["people"]))

                from parser.git_ast import insights_deltas as _deltas_mod
                has_in_tx = hasattr(_deltas_mod, "write_deltas_in_tx")
                if seg_data is not None or (deltas is not None and has_in_tx) or (item.get("need_pulls") and pulls is not None):
                    with db.transaction():
                        if seg_data is not None and persons is not None:
                            old_seg = stored_segs.get(tag_name)
                            new_sid = release_stats.write_segment(
                                db,
                                repo_id,
                                item["seg_range"],
                                seg_data,
                                persons,
                                old_seg[0] if old_seg else None,
                                first_releases,
                            )
                            item["segment_id"] = new_sid
                            counters["segments_built"] += 1

                        if deltas is not None and has_in_tx:
                            _deltas_mod.write_deltas_in_tx(db, deltas, replace=bool(item.get("replace_deltas")))

                        sid = item.get("segment_id")
                        if (
                            sid is not None
                            and item.get("need_pulls")
                            and "m_release_pull" in existing_tables
                            and "m_release_section_health" in existing_tables
                        ):
                            if pulls is not None:
                                release_stats.write_release_pulls(db, sid, pulls, fmt=release_stats.MW_FORMAT)
                            if sec_stats is not None:
                                release_stats.write_release_section_health(
                                    db,
                                    sid,
                                    sections,
                                    sec_stats,
                                    catchall_section=catchall_section,
                                    fmt=release_stats.HEALTH_FORMAT,
                                )

                if deltas is not None:
                    if not has_in_tx:
                        _deltas_mod.write_insights_deltas(db, deltas)
                    if item.get("need_subsystems"):
                        counters["insights_built"] += 1
                    if item.get("need_stable"):
                        counters["stable_built"] += 1

                kapi_payload = res.get("kapi")
                if kapi_payload is not None and item.get("commit_id") is not None:
                    kapi_ingest.write_kapi_release_payload(db, int(item["commit_id"]), kapi_payload)
                    counters["api_built"] += 1
            except Exception as exc:  # noqa: BLE001 - persist per-tag errors without aborting remaining tags
                logger.warning(COLOR.yellow(f"[Pre-parse] Persisting {tag_name} failed: {exc}"))
                errors.append({"tag": tag_name, "error": str(exc)})

        _UNIFIED_PREPARSE_SHARED.clear()
        _UNIFIED_PREPARSE_SHARED.update({
            "insights_cfg": insights_cfg,
            "cve_commit_hashes": cve_commit_hashes,
            "sha_to_release": sha_to_release,
            "existing_facts": existing_facts,
        })
        try:
            run_unified_preparse_pool(
                work_items,
                _unified_preparse_worker,
                workers=eff_workers,
                on_result=_persist_result,
                ordered=need_any_seg,
            )
        finally:
            _UNIFIED_PREPARSE_SHARED.clear()

        if replaced_mainline and repo_id is not None and not interrupted:
            mainline = {row[0]: t for t, row in release_stats.stored_segments(db, repo_id).items() if row[6] == release_stats.CHAIN_MAINLINE}
            with db.transaction():
                release_stats._rebuild_first_releases(db, repo_id, mainline)
        if persons is not None and persons.added:
            refresh_person_identities(db, "unified pre-parse added persons")

    built_total = counters["insights_built"] + counters["stable_built"] + counters["api_built"] + counters["segments_built"]
    elapsed = round(time.perf_counter() - started, 2)
    logger.info(
        f"[Pre-parse] Processed {len(work_items)}/{len(candidate_tags)} tag(s) across {eff_workers} worker(s) "
        f"(insights={counters['insights_built']}, stable={counters['stable_built']}, api={counters['api_built']}, "
        f"segments={counters['segments_built']}, skipped={skipped_current}, errors={len(errors)}) in {elapsed}s."
    )
    return {
        "state": "updated" if built_total else "current",
        "tags": len(candidate_tags),
        "work_items": len(work_items),
        "insights_built": counters["insights_built"],
        "stable_built": counters["stable_built"],
        "api_built": counters["api_built"],
        "segments_built": counters["segments_built"],
        "skipped_current": skipped_current,
        "api_skipped_no_commit": api_skipped_no_commit,
        "workers": eff_workers,
        "errors": errors,
        "seconds": elapsed,
    }

