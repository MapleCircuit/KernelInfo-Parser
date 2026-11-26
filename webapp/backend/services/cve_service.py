"""webapp/backend/services/cve_service.py - CVE links (H4, webapp/API.md section 4.11).

The parser ingests the CVE list (config `cve.repo_dir`: the CVE Program's `cvelistV5`, or the kernel CNA's
`vulns.git`) into `m_cve_source`, `m_cve` and `m_cve_commit` whenever the clone's HEAD moved
(`parser/cve/cve_ingest.py`, `core/API.md` section 9). This module only reads those tables:

- `status()`: the list ingested (name, layout, HEAD, record count, when);
- `for_commit()`: the CVEs a commit fixes or introduced, from `m_cve_commit` (full SHA and the abbreviations
  references use);
- the records themselves (`parser/cve/cve_records.py` `parse_record()` output, stored compressed) are loaded once
  per ingestion (`built_at`, checked at most every minute) and kept in memory for the release reports and the
  detail page.

A release is evaluated per record (`affects`): the kernel CNA's version entries decide for releases and stable
updates (not for -rc tags); otherwise commit containment from the release index (section 2.17) — a range
counts when its fix is on the release's branch — and, for `commit` records, the fixes' stable backports and
the commits their `Fixes:` tags name (commit relation index, section 2.16).
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import HTTPException

from db_engine.spec import Agg, Join
from parser.cve.cve_records import (  # noqa: F401 - re-exported for callers and tests
    _VERSION_TEXT,
    CVE_ID,
    KIND_NAMES,
    RECORD_FORMAT,
    ROLE_FIX,
    ROLE_INTRODUCED,
    description_bound,
    kernel_commits,
    parse_record,
    summarize_versions,
    version_status,
)
from parser.cve.cve_records import (
    HEXSHA as _HEXSHA,
)
from parser.cve.cve_releases import (
    CVE_RELEASE_FORMAT,
    ID_TO_BASIS,
    ID_TO_STATE,
    BaseReleaseContext,
    earliest_final_release,
    is_fixed_here,
)
from parser.cve.cve_releases import (
    affects as _affects,
)
from parser.git_ast.commit_relations import KIND_FIXES
from webapp.backend.config import RepoScopedCache
from webapp.backend.database.pool import get_reader
from webapp.backend.services import release_service as rs
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

REPORT_FORMAT = 5
SOURCE_TTL = 60.0
_FULL = re.compile(r"^[0-9a-f]{40}$")
_CVSS_OP_ALIASES = {
    "<": "lt",
    "lt": "lt",
    ">": "gt",
    "gt": "gt",
    "=": "eq",
    "==": "eq",
    "eq": "eq",
    ">=": "ge",
    "≥": "ge",
    "ge": "ge",
}

_DISK = DiskCache("cve-index", max_bytes=256 << 20, max_entries=6)
_PREFETCH_CACHE: RepoScopedCache = RepoScopedCache(maxsize=4)
_SOURCE: dict[str, Any] = {"checked": 0.0, "row": None}
_MEM: dict[str, Any] = {"built_at": None, "head": None, "records": {}}
_CATALOG: dict[str, Any] = {"built_at": None, "head": None, "rows": []}
_LOCK = threading.Lock()


# ------------------------------------------------------------------------------------- the ingested list

def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")


def source(fresh: bool = False) -> dict[str, Any] | None:
    """The CVE list the parser ingested (m_cve_source), checked at most every SOURCE_TTL seconds; None if none."""
    now = time.monotonic()
    with _LOCK:
        if not fresh and now - _SOURCE["checked"] < SOURCE_TTL:
            return _SOURCE["row"]
    row = None
    try:
        with get_reader() as reader:
            r = reader.select_one(
                "m_cve_source", ["name", "layout", "head", "format", "records", "built_at"], order_by=["-built_at"]
            )
        if r:
            row = {"name": _text(r["name"]), "layout": _text(r["layout"]), "head": bytes(r["head"]).hex(),
                   "format": int(r["format"]), "records": int(r["records"]), "built_at": int(r["built_at"])}
    except Exception as exc:  # noqa: BLE001 - no database, or one without the CVE tables
        logger.debug("m_cve_source unavailable: %s", exc)
    with _LOCK:
        _SOURCE.update(checked=now, row=row)
    return row


def _decode(data: Any) -> dict[str, Any] | None:
    import json
    import zlib
    try:
        rec = json.loads(zlib.decompress(bytes(data)))
    except (zlib.error, ValueError, TypeError):
        return None
    return rec if isinstance(rec, dict) else None


def records() -> dict[str, dict[str, Any]]:
    """Every ingested record by id (loaded once per ingestion)."""
    src = source()
    if src is None:
        return {}
    key = (src["built_at"], src["head"])
    with _LOCK:
        if (_MEM["built_at"], _MEM["head"]) == key:
            return _MEM["records"]
    out: dict[str, dict[str, Any]] = {}
    started = time.time()
    with get_reader() as reader:
        for r in reader.select_rows("m_cve", ["cve_id", "data"]):
            rec = _decode(r["data"])
            if rec is not None:
                out[_text(r["cve_id"])] = rec
    logger.info("Loaded %d CVE records in %.1f s", len(out), time.time() - started)
    with _LOCK:
        _MEM.update(built_at=src["built_at"], head=src["head"], records=out)
    return out


def catalog_rows() -> list[dict[str, Any]]:
    """Cached, id-descending summary rows for all ingested CVE records."""
    src = source()
    if src is None:
        return []
    key = (src["built_at"], src["head"])
    with _LOCK:
        if (_CATALOG["built_at"], _CATALOG["head"]) == key:
            return _CATALOG["rows"]
    all_recs = records()
    rows: list[dict[str, Any]] = []
    for rec in all_recs.values():
        cvss = rec.get("cvss") or {}
        summary = summarize_versions(rec)
        rows.append({
            "id": rec["id"],
            "title": rec.get("title") or (rec.get("description") or "")[:160],
            "kind": rec.get("kind", ""),
            "published": rec.get("published", ""),
            "score": cvss.get("score"),
            "severity": cvss.get("severity") or "",
            "kev": rec.get("kev"),
            "cwes": rec.get("cwes") or [],
            "versions": summary["text"],
            "affected_ranges": summary["affected"],
            "fixed_versions": summary["fixed"],
            "unfixed": summary["unfixed"],
            "files": (rec.get("files") or [])[:5],
        })
    rows.sort(key=lambda r: r["id"], reverse=True)
    with _LOCK:
        _CATALOG.update(built_at=src["built_at"], head=src["head"], rows=rows)
    return rows


def catalog(q: str = "", kind: str = "", version: str = "", offset: int = 0, limit: int = 50000) -> dict[str, Any]:
    """Filterable, paginated CVE catalog built purely from ingested records (no git, no release_index)."""
    src = source()
    if src is None:
        from parser.cve.cve_ingest import repo_dir
        return {
            "configured": repo_dir() is not None,
            "ingested": False,
            "total": 0,
            "counts": {"total": 0, "cna": 0, "commit": 0, "text": 0, "kev": 0, "high": 0, "unfixed": 0},
            "cves": [],
        }
    rows = catalog_rows()
    counts = {
        "total": len(rows),
        "cna": sum(1 for r in rows if r["kind"] == "cna"),
        "commit": sum(1 for r in rows if r["kind"] == "commit"),
        "text": sum(1 for r in rows if r["kind"] == "text"),
        "kev": sum(1 for r in rows if r["kev"]),
        "high": sum(1 for r in rows if r["score"] is not None and r["score"] >= 7),
        "unfixed": sum(1 for r in rows if r["unfixed"]),
    }
    filtered = rows
    if kind:
        filtered = [r for r in filtered if r["kind"] == kind]
    ver = version.strip()
    if ver:
        if ver[0] in ("v", "V"):
            ver = ver[1:]
        if not _VERSION_TEXT.fullmatch(ver):
            filtered = []
        else:
            all_recs = records()
            filtered = [
                r for r in filtered
                if version_status((all_recs.get(r["id"]) or {}).get("entries") or [], ver) == "affected"
            ]
    terms = q.strip().lower().split()
    if terms and filtered:
        filtered = [
            r for r in filtered
            if all(
                t in " ".join([
                    r["id"],
                    r["title"],
                    r["severity"],
                    " ".join(r["cwes"]),
                    r["versions"],
                    " ".join(r["files"]),
                ]).lower()
                for t in terms
            )
        ]
    return {
        "configured": True,
        "ingested": True,
        "head": src["head"],
        "layout": src["layout"],
        "total": len(filtered),
        "counts": counts,
        "cves": filtered[offset:offset + limit],
    }


def kinds(recs: dict[str, dict[str, Any]]) -> dict[str, int]:
    return {k: sum(1 for r in recs.values() if r.get("kind") == k) for k in ("cna", "commit", "text")}


def status() -> dict[str, Any]:
    from parser.cve.cve_ingest import repo_dir
    src = source(fresh=True)
    if src is None:
        return {"configured": repo_dir() is not None, "ingested": False}
    return {"configured": True, "ingested": True, "name": src["name"], "layout": src["layout"], "head": src["head"],
            "indexed_head": src["head"], "records": src["records"], "built_at": src["built_at"], "state": "done",
            "format": src["format"], "current_format": RECORD_FORMAT}


def for_commit(sha: str, upstream: Iterable[str] = ()) -> dict[str, list[str]]:
    """CVEs a commit fixes or introduced, and those its upstream commit fixes (a stable backport)."""
    empty = {"fixes": [], "introduced": [], "fixes_upstream": []}
    sha = (sha or "").lower()
    ups = [str(u).lower() for u in upstream if _FULL.match(str(u).lower())]
    if not _FULL.match(sha) or source() is None:
        return empty

    def keys(full: str) -> list[str]:
        return [full] + [full[:n] for n in range(12, 40)]  # references that abbreviate the commit

    wanted = {k: full for full in [sha, *ups] for k in keys(full)}
    hits: dict[str, dict[int, list[str]]] = {}
    try:
        with get_reader() as reader:
            for r in reader.select_rows(
                "m_cve_commit", ["commit_hash", "role", "cve_id"], {"commit_hash": sorted(wanted)}, order_by=["cve_id"]
            ):
                full = wanted.get(_text(r["commit_hash"]))
                if full is None:
                    continue
                lst = hits.setdefault(full, {}).setdefault(int(r["role"]), [])
                cid = _text(r["cve_id"])
                if cid not in lst:
                    lst.append(cid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("m_cve_commit unavailable: %s", exc)
        return empty
    own = hits.get(sha, {}).get(ROLE_FIX, [])
    intro = hits.get(sha, {}).get(ROLE_INTRODUCED, [])
    up = [c for u in ups for c in hits.get(u, {}).get(ROLE_FIX, []) if c not in own]
    return {"fixes": own, "introduced": intro, "fixes_upstream": list(dict.fromkeys(up))}


# ------------------------------------------------------------------------------------- releases

def stored_prefixes(prefixes: list[str]) -> dict[str, str]:
    """{prefix: full SHA} of the abbreviated hashes m_cve_commit_hash resolved for the active repository
    (read only; an older database without the table answers {})."""
    out: dict[str, str] = {}
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        with get_reader() as reader:
            repo_id = get_active_repo_id(reader)
            for i in range(0, len(prefixes), 1000):
                part = prefixes[i:i + 1000]
                for r in reader.select_rows(
                    "m_cve_commit_hash", ["prefix", "commit_hash"], {"repo_id": repo_id, "prefix": part}
                ):
                    prefix = r["prefix"].decode() if isinstance(r["prefix"], (bytes, bytearray)) else r["prefix"]
                    out[prefix] = bytes(r["commit_hash"]).hex()
    except Exception as exc:  # noqa: BLE001 - git resolves them
        logger.debug("m_cve_commit_hash lookup failed: %s", exc)
    return out


class Context(BaseReleaseContext):
    """Containment of commits in one release tag, memoized: release membership (first tags), stable
    backports and Fixes: targets (m_commit_relation first, else the commit relation index when built)."""

    def __init__(self, release: str) -> None:
        self.release = release
        self._rkeys: dict[str, Any] = {}
        self._ptags: dict[str, Any] = {}
        self.p = self.parse_tag(release)
        self.version = release.removeprefix("v")
        self.key = self.release_key(release)
        is_stable = bool(self.p and self.p.get("kind") != "next" and self.p.get("stable"))
        self.base_key = self.release_key(rs.series_name(self.p["series"])) if is_stable else None
        self._tags: dict[str, dict[str, Any] | None] = {}
        self._full: dict[str, str | None] = {}
        self._backports_complete: bool | None = None

    def release_key(self, tag: str) -> Any:
        if tag not in self._rkeys:
            self._rkeys[tag] = rs.release_key(tag)
        return self._rkeys[tag]

    def parse_tag(self, tag: str) -> Any:
        if tag not in self._ptags:
            self._ptags[tag] = rs.parse_tag(tag)
        return self._ptags[tag]

    def full(self, sha: str | None) -> str | None:
        if not sha:
            return None
        if _FULL.match(sha):
            return sha
        if sha not in self._full:
            self._full[sha] = rs.resolve_sha(sha) if _HEXSHA.match(sha) else None
        return self._full[sha]

    def prefetch(self, shas: Iterable[str]) -> None:
        """Load the first tags of many SHAs at once (a report reads ~100k of them). Abbreviated ones are
        resolved first from m_cve_commit_hash (one query; the parser resolves them at CVE ingestion), so
        full() runs `git rev-parse` only for prefixes the database does not hold."""
        src = source()
        cache_token = (src["built_at"] if src else 0, rs.release_token())
        with _LOCK:
            cached_entry = _PREFETCH_CACHE.get(cache_token)
            if cached_entry is None:
                cached_full: dict[str, str | None] = {}
                cached_tags: dict[str, dict[str, Any] | None] = {}
                _PREFETCH_CACHE[cache_token] = (cached_full, cached_tags)
            else:
                cached_full, cached_tags = cached_entry
            self._full.update(cached_full)
            self._tags.update(cached_tags)

        shas = list(dict.fromkeys(s for s in shas if s))
        short = [s for s in shas if not _FULL.match(s) and _HEXSHA.match(s) and s not in self._full]
        if short:
            resolved_short = stored_prefixes(short)
            self._full.update(resolved_short)
            with _LOCK:
                cached_full.update(resolved_short)
        shas = [self._full.get(s, s) if not _FULL.match(s) else s for s in shas]
        want = [s for s in dict.fromkeys(shas) if s and _FULL.match(s) and s not in self._tags]
        if want:
            found = rs.release_lookup(want)
            new_tags: dict[str, dict[str, Any] | None] = {}
            for s in want:
                hit = found.get(s)
                entry = hit if hit and (hit.get("mainline") or hit.get("stable")) else None
                self._tags[s] = entry
                new_tags[s] = entry
            with _LOCK:
                cached_tags.update(new_tags)

    def tags(self, sha: str | None) -> dict[str, Any] | None:
        full = self.full(sha)
        if not full:
            return None
        if full not in self._tags:
            hit = rs.release_lookup([full]).get(full)
            self._tags[full] = hit if hit and (hit.get("mainline") or hit.get("stable")) else None
        return self._tags[full]

    def _backport_tags(self) -> list[str]:
        """The releases whose commits contains() reads backports from: mainline tags up to the release and,
        for a stable release, its series' stable tags up to it."""
        out = []
        if not self.p:
            return out
        is_next = self.p.get("kind") == "next"
        for t in rs.tag_commits():
            q = self.parse_tag(t)
            if not q:
                continue
            if q.get("kind") == "next":
                if is_next and self.release_key(t) <= self.key:
                    out.append(t)
            elif (
                (not q["stable"] and self.release_key(t) <= self.key)
                or (
                    not is_next
                    and self.p["stable"]
                    and q["stable"]
                    and q["series"] == self.p["series"]
                    and q["stable"] <= self.p["stable"]
                )
            ):
                out.append(t)
        return out

    def backports(self, sha: str) -> list[str]:
        """Backports of a commit: m_commit_relation when every commit of the releases contains() reads is
        ingested (commit_relations.tags_ingested), else the relation index when built."""
        full = self.full(sha)
        if not full:
            return []
        from webapp.backend.services import commit_relations
        if self._backports_complete is None:
            try:
                self._backports_complete = commit_relations.tags_ingested(self._backport_tags())
            except Exception:  # noqa: BLE001
                self._backports_complete = False
        try:
            return commit_relations.backports_of(full, complete=self._backports_complete)
        except Exception:  # noqa: BLE001
            return []

    def fixes_tags(self, sha: str) -> list[str]:
        """Targets of a commit's Fixes: tags: m_commit_relation when the commit is ingested, else the
        relation index when built."""
        full = self.full(sha)
        if not full:
            return []
        from webapp.backend.services import commit_relations
        try:
            return commit_relations.fixes_of(full)
        except Exception:  # noqa: BLE001
            return []


def affects(rec: dict[str, Any], release: str, ctx: Context | None = None) -> tuple[str, str]:
    """(state, basis) of a record for a release tag. state: affected | fixed | possibly | unaffected | unknown;
    basis: record (the kernel CNA's data) | fixes-tag | reference | description | versions."""
    return _affects(rec, release, ctx or Context(release))


def normalize_cvss_op(op: str | None) -> str:
    """Normalize a CVSS comparison operator (`<`, `>`, `=`, `>=`, `≥`, `lt`, `gt`, `eq`, `ge`)."""
    raw = str(op or "").strip().lower()
    if not raw:
        return ""
    norm = _CVSS_OP_ALIASES.get(raw)
    if norm is None:
        raise HTTPException(status_code=400, detail=f"Invalid cvss_op: {op!r}; expected one of <, >, =, >=, ≥")
    return norm


def clamp_cvss_score(score: float | None) -> float | None:
    """Validate and clamp a CVSS score threshold to `[0.0, 10.0]` (1-decimal precision)."""
    if score is None:
        return None
    try:
        val = float(score)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cvss_score: {score!r}") from exc
    if not math.isfinite(val):
        raise HTTPException(status_code=400, detail="CVSS score must be a finite number")
    return round(min(10.0, max(0.0, val)), 1)


def matches_cvss(score: Any, op: str, threshold: float) -> bool:
    """True when `score` satisfies `op` against clamped `threshold`; excludes unscored/null CVEs across all ops."""
    if score is None or isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    val = float(score)
    if not math.isfinite(val):
        return False
    t = round(min(10.0, max(0.0, float(threshold))), 1)
    if op == "eq":
        return abs(round(val, 1) - t) < 0.05
    if op == "lt":
        return round(val, 4) < t - 1e-9
    if op == "gt":
        return round(val, 4) > t + 1e-9
    if op == "ge":
        return round(val, 4) >= t - 1e-9
    return False


def filter_report_by_cvss(
    rows: list[dict[str, Any]],
    cvss_op: str = "",
    cvss_score: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any] | None]:
    """Filter release-report CVE rows by CVSS operator & threshold and recompute counts."""
    op = normalize_cvss_op(cvss_op)
    score = clamp_cvss_score(cvss_score)
    if not op and score is None:
        counts = {k: sum(r["state"] == k for r in rows) for k in ("affected", "possibly", "fixed")}
        counts["fixed_here"] = sum(1 for r in rows if r.get("fixed_here"))
        return rows, counts, None
    op = op or "ge"
    score = 7.0 if score is None else score
    filtered = [r for r in rows if matches_cvss(r.get("score"), op, score)]
    counts = {k: sum(r["state"] == k for r in filtered) for k in ("affected", "possibly", "fixed")}
    counts["fixed_here"] = sum(1 for r in filtered if r.get("fixed_here"))
    return filtered, counts, {"op": op, "score": score}


def _format_report_row(
    rec: dict[str, Any],
    state: str,
    basis: str,
    fixed_here: bool,
    introduced_release: str | None = None,
) -> dict[str, Any]:
    fixes = [r["fixed"] for r in (rec.get("ranges") or ()) if r.get("fixed")]
    cvss = rec.get("cvss") or {}
    return {
        "id": rec["id"],
        "title": rec.get("title") or (rec.get("description") or "")[:160],
        "state": state,
        "basis": basis,
        "kind": rec.get("kind", ""),
        "published": rec.get("published", ""),
        "fixes": fixes[:4],
        "fixed_here": fixed_here,
        "introduced_release": introduced_release,
        "score": cvss.get("score"),
        "severity": cvss.get("severity") or "",
        "kev": rec.get("kev"),
        "cwes": rec.get("cwes") or [],
    }


def _db_introduced_releases(
    reader: Any,
    repo_id: int,
    recs: list[dict[str, Any]],
) -> dict[str, str | None]:
    """Resolve earliest first final mainline release for each record's introducing commit(s) (read only)."""
    rec_intros: dict[str, list[str]] = {}
    commit_fixes: dict[str, list[str]] = {}
    for rec in recs:
        cid = rec["id"]
        direct = [r["introduced"] for r in (rec.get("ranges") or ()) if r.get("introduced")]
        if direct:
            rec_intros[cid] = direct
        elif rec.get("kind") == "commit":
            fixes = [r["fixed"] for r in (rec.get("ranges") or ()) if r.get("fixed")]
            if fixes:
                commit_fixes[cid] = fixes
    if not rec_intros and not commit_fixes:
        return {}

    raw_shas = [s for lst in rec_intros.values() for s in lst] + [s for lst in commit_fixes.values() for s in lst]
    short_shas = sorted({s for s in raw_shas if not _FULL.match(s)})
    prefix_map: dict[str, str] = {}
    if short_shas:
        for i in range(0, len(short_shas), 1000):
            part = short_shas[i:i + 1000]
            for r in reader.select_rows(
                "m_cve_commit_hash", ["prefix", "commit_hash"], {"repo_id": repo_id, "prefix": part}
            ):
                prefix_map[_text(r["prefix"])] = bytes(r["commit_hash"]).hex()

    if commit_fixes:
        fix_fulls = sorted({s if _FULL.match(s) else prefix_map[s] for lst in commit_fixes.values() for s in lst if _FULL.match(s) or s in prefix_map})
        fixes_targets: dict[str, list[str]] = {}
        if fix_fulls:
            try:
                from webapp.backend.database.helpers import get_active_network_id
                network_id = get_active_network_id(reader)
                if network_id is not None:
                    cid_by_sha: dict[str, int] = {}
                    for i in range(0, len(fix_fulls), 1000):
                        part = [bytes.fromhex(s) for s in fix_fulls[i:i + 1000]]
                        for r in reader.select_rows(
                            "m_commit", ["commit_id", "commit_hash"], {"network_id": network_id, "commit_hash": part}
                        ):
                            cid_by_sha[bytes(r["commit_hash"]).hex()] = int(r["commit_id"])
                    if cid_by_sha:
                        sha_by_cid = {v: k for k, v in cid_by_sha.items()}
                        cids = sorted(sha_by_cid)
                        for i in range(0, len(cids), 1000):
                            for r in reader.select_rows(
                                "m_commit_relation",
                                ["commit_id", "target_hash"],
                                {"commit_id": cids[i:i + 1000], "kind": KIND_FIXES},
                                order_by=["idx"],
                            ):
                                fsha = sha_by_cid.get(int(r["commit_id"]))
                                th = r.get("target_hash")
                                if fsha and th:
                                    fixes_targets.setdefault(fsha, []).append(bytes(th).hex())
            except Exception as exc:  # noqa: BLE001 - optional enrichment on older schemas
                logger.debug("Fixes: target lookup skipped: %s", exc)
        for cid, lst in commit_fixes.items():
            resolved = [
                t for s in lst
                if (full := (s if _FULL.match(s) else prefix_map.get(s)))
                for t in fixes_targets.get(full, ())
            ]
            if resolved:
                rec_intros[cid] = resolved

    full_by_cid: dict[str, list[str]] = {}
    all_full: set[str] = set()
    for cid, lst in rec_intros.items():
        fulls = [full for s in lst if (full := (s if _FULL.match(s) else prefix_map.get(s))) and _FULL.match(full)]
        if fulls:
            full_by_cid[cid] = fulls
            all_full.update(fulls)
    if not all_full:
        return {}
    tag_names = set(rs.tag_commits())
    tags_by_sha = rs.release_lookup(sorted(all_full))
    return {
        cid: earliest_final_release((tags_by_sha.get(s) for s in fulls), tag_names)
        for cid, fulls in full_by_cid.items()
    }


def _db_kind_counts(reader: Any) -> dict[str, int] | None:
    counts = {"cna": 0, "commit": 0, "text": 0}
    for row in reader.aggregate("m_cve", {"n": Agg("COUNT", "*")}, group_by=["kind"]):
        name = KIND_NAMES.get(int(row["kind"]))
        if name is None:
            return None
        counts[name] = int(row["n"] or 0)
    return counts


def db_release_report(release: str) -> dict[str, Any] | None:
    """Pre-evaluated CVE report for `release` from `m_cve_release` / `m_cve_release_state` (read only),
    or None when absent, stale against `m_cve_source` / tag commit, or format differs."""
    src = source()
    if src is None:
        return None
    tag_sha = rs.tag_commits().get(release)
    if not tag_sha:
        return None
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        with get_reader() as reader:
            repo_id = get_active_repo_id(reader)
            seg = reader.select_one(
                "m_release_segment", ["segment_id", "tag_hash"], {"repo_id": repo_id, "tag": release}
            )
            if not seg or bytes(seg["tag_hash"]).hex() != tag_sha:
                return None
            segment_id = int(seg["segment_id"])
            st = reader.select_one(
                "m_cve_release_state",
                ["cve_head", "cve_layout", "cve_format", "rel_format"],
                {"segment_id": segment_id},
            )
            if not st:
                return None
            if bytes(st["cve_head"]).hex() != src["head"]:
                src = source(fresh=True)
                if src is None or bytes(st["cve_head"]).hex() != src["head"]:
                    return None
            if (
                _text(st["cve_layout"]) != src["layout"]
                or int(st["cve_format"]) != RECORD_FORMAT
                or int(st["rel_format"]) != CVE_RELEASE_FORMAT
            ):
                return None

            with _LOCK:
                mem_recs = (
                    _MEM["records"]
                    if (_MEM["built_at"], _MEM["head"]) == (src["built_at"], src["head"])
                    else None
                )
            kind_counts = kinds(mem_recs) if mem_recs is not None else _db_kind_counts(reader)
            if kind_counts is None:
                kind_counts = kinds(records())
                with _LOCK:
                    mem_recs = (
                        _MEM["records"]
                        if (_MEM["built_at"], _MEM["head"]) == (src["built_at"], src["head"])
                        else None
                    )

            matched: list[tuple[dict[str, Any], str, str, bool]] = []
            if mem_recs is not None:
                db_rows = reader.select_rows(
                    "m_cve_release",
                    ["cve_id", "state", "basis", "fixed_here"],
                    {"segment_id": segment_id},
                    order_by=["-cve_id"],
                )
                for r in db_rows:
                    cid = _text(r["cve_id"])
                    rec = mem_recs.get(cid)
                    state_str = ID_TO_STATE.get(int(r["state"]))
                    if rec is None or state_str is None:
                        return None
                    basis_str = ID_TO_BASIS.get(int(r["basis"]), "")
                    matched.append((rec, state_str, basis_str, bool(int(r["fixed_here"]))))
            else:
                db_rows = reader.select_rows(
                    ("m_cve_release", "r"),
                    {
                        "cve_id": "r.cve_id",
                        "state": "r.state",
                        "basis": "r.basis",
                        "fixed_here": "r.fixed_here",
                        "data": "c.data",
                    },
                    {"r.segment_id": segment_id},
                    joins=[Join(("m_cve", "c"), {"c.cve_id": "r.cve_id"})],
                    order_by=["-r.cve_id"],
                )
                for r in db_rows:
                    rec = _decode(r["data"])
                    state_str = ID_TO_STATE.get(int(r["state"]))
                    if rec is None or state_str is None:
                        return None
                    basis_str = ID_TO_BASIS.get(int(r["basis"]), "")
                    matched.append((rec, state_str, basis_str, bool(int(r["fixed_here"]))))
            intro_releases = _db_introduced_releases(reader, repo_id, [rec for rec, *_ in matched])
            rows = [
                _format_report_row(rec, state_str, basis_str, fixed_here, intro_releases.get(rec["id"]))
                for rec, state_str, basis_str, fixed_here in matched
            ]
    except Exception as exc:  # noqa: BLE001 - older DB without m_cve_release* or read fallback
        logger.debug("db_release_report(%s) unavailable: %s", release, exc)
        return None

    rows.sort(key=lambda r: r["id"], reverse=True)
    counts = {k: sum(r["state"] == k for r in rows) for k in ("affected", "possibly", "fixed")}
    counts["fixed_here"] = sum(1 for r in rows if r["fixed_here"])
    return {
        "release": release,
        "head": src["head"],
        "layout": src["layout"],
        "kinds": kind_counts,
        "cves": rows,
        "counts": counts,
        "source": "database",
    }


def release_report(release: str, progress: Callable | None = None) -> dict[str, Any]:
    src = source()
    if src is None:
        raise RuntimeError("The CVE list has not been ingested (python3 main.py --cve)")
    all_recs = records()
    ctx = Context(release)
    recs = list(all_recs.values())
    if progress:
        progress(0, len(recs), "looking up commits")
    ctx.prefetch(s for rec in recs for r in rec["ranges"] for s in (r.get("introduced"), r.get("fixed")))
    matched: list[tuple[dict[str, Any], str, str, bool]] = []
    for i, rec in enumerate(recs):
        if progress and i % 1000 == 0:
            progress(i, len(recs), "checking records")
        state, basis = affects(rec, release, ctx)
        if state not in ("affected", "fixed", "possibly"):
            continue
        matched.append((rec, state, basis, is_fixed_here(rec, state, ctx)))
    rec_intros: dict[str, list[str]] = {}
    for rec, *_ in matched:
        direct = [r["introduced"] for r in (rec.get("ranges") or ()) if r.get("introduced")]
        if direct:
            rec_intros[rec["id"]] = direct
        elif rec.get("kind") == "commit":
            inferred = [s for r in (rec.get("ranges") or ()) if r.get("fixed") for s in ctx.fixes_tags(r["fixed"])]
            if inferred:
                rec_intros[rec["id"]] = inferred
    ctx.prefetch(s for lst in rec_intros.values() for s in lst)
    tag_names = set(rs.tag_commits())
    rows = [
        _format_report_row(
            rec,
            state,
            basis,
            fixed_here,
            earliest_final_release((ctx.tags(s) for s in rec_intros.get(rec["id"], ())), tag_names),
        )
        for rec, state, basis, fixed_here in matched
    ]
    rows.sort(key=lambda r: r["id"], reverse=True)
    counts = {k: sum(r["state"] == k for r in rows) for k in ("affected", "possibly", "fixed")}
    counts["fixed_here"] = sum(1 for r in rows if r["fixed_here"])
    return {"release": release, "head": src["head"], "layout": src["layout"], "kinds": kinds(all_recs),
            "cves": rows, "counts": counts, "source": "database" if rs.db_covers_all() else "git"}


def report(release: str, wait: float = 0.0) -> dict[str, Any]:
    if release not in rs.tag_commits():
        raise HTTPException(status_code=404, detail=f"Not a release of this repository: {release}")
    src = source()
    if src is None:
        from parser.cve.cve_ingest import repo_dir
        return {"state": "done", "result": {"configured": repo_dir() is not None, "ingested": False, "cves": []}}
    hit = db_release_report(release)
    if hit is not None:
        return {"state": "done", "result": hit}
    dep = rs.ensure_release_index()
    if dep.get("state") != "done":
        return dep
    head = f"{src['head']}|{src['built_at']}"
    try:
        from webapp.backend.services.commit_relations import relations_token
        rel_built = relations_token()
    except Exception:  # noqa: BLE001
        rel_built = ""
    from webapp.backend.config import get_active_repo_name
    ck = (f"cve-report|{REPORT_FORMAT}|{RECORD_FORMAT}|{get_active_repo_name()}|{head}|{release}|"
          f"{rs.release_token()}|{rel_built}")
    state = jobs.submit(f"cve-report:{ck}", lambda p: release_report(release, p), cache=_DISK, cache_key=ck)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"cve-report:{ck}", wait) or state
    return state


def record(cve_id: str, release: str = "") -> dict[str, Any]:
    if not CVE_ID.match(cve_id or ""):
        raise HTTPException(status_code=400, detail="CVE id must look like CVE-2024-12345")
    if source() is None:
        raise HTTPException(status_code=404, detail="The CVE list has not been ingested (python3 main.py --cve)")
    rec = records().get(cve_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"{cve_id} is not a Linux kernel record of the CVE list")
    from webapp.backend.services.commit_facts import git_facts, release_badges
    ctx = Context(release) if release and rs.parse_tag(release) else None
    inferred: dict[str, list[str]] = {}
    probe = ctx or Context("")
    if rec["kind"] == "commit":
        for r in rec["ranges"]:
            if r.get("fixed"):
                inferred[r["fixed"]] = probe.fixes_tags(r["fixed"])[:6]
    raw_shas = [s for r in rec["ranges"] for s in (r.get("introduced"), r.get("fixed")) if s]
    raw_shas += [s for lst in inferred.values() for s in lst]
    probe.prefetch(raw_shas)
    sha_map: dict[str, str] = {}
    for s in raw_shas:
        full = probe.full(s)
        if full and _FULL.match(full):
            sha_map[s] = full

    full_shas = list(dict.fromkeys(sha_map.values()))
    facts = git_facts(full_shas[:200])
    badges = release_badges(full_shas[:200])

    def describe(sha: str | None) -> dict[str, Any] | None:
        if not sha:
            return None
        full = sha_map.get(sha, sha)
        return {"sha": sha, "subject": (facts.get(full) or facts.get(sha) or {}).get("subject", ""),
                "release": badges.get(full) or badges.get(sha)}

    ranges = []
    for r in rec["ranges"]:
        intro = r.get("introduced") or ""
        intro_full = sha_map.get(intro, intro)
        fix = r.get("fixed") or ""
        fix_full = sha_map.get(fix, fix)
        item = {
            **r,
            "introduced_subject": (facts.get(intro_full) or facts.get(intro) or {}).get("subject", ""),
            "fixed_subject": (facts.get(fix_full) or facts.get(fix) or {}).get("subject", ""),
            "introduced_release": badges.get(intro_full) or badges.get(intro),
            "fixed_release": badges.get(fix_full) or badges.get(fix),
            "basis": "record" if rec["kind"] == "cna" else "reference",
        }
        if r.get("fixed") in inferred:
            item["inferred_introduced"] = [describe(s) for s in inferred[r["fixed"]]]
        ranges.append(item)
    out = {**rec, "ranges": ranges, "version_summary": summarize_versions(rec),
           "url": f"https://www.cve.org/CVERecord?id={cve_id}"}
    if ctx is not None:
        state, basis = affects(rec, release, ctx)
        out["release_state"] = {"release": release, "state": state, "basis": basis}
    return out


def cves_for_file(version: str, file_path: str) -> dict[str, Any]:
    """O(1) security badge lookup for a source file at a version, querying Table 65 m_cve_file."""
    from webapp.backend.security.jail import is_safe_rel_path
    clean_path = str(file_path or "").strip().strip("/")
    if not clean_path or not is_safe_rel_path(clean_path):
        raise HTTPException(status_code=400, detail="Invalid file path")
    from webapp.backend.database.helpers import safe_decode
    with get_reader() as reader:
        cve_ids = [safe_decode(r["cve_id"]) for r in reader.select_rows("m_cve_file", ["cve_id"], {"file_path": clean_path}) if r.get("cve_id")]
    rel = version
    if rel and not rs.parse_tag(rel):
        try:
            rel = rs.resolve_release_tag(rel).get("tag") or ""
        except HTTPException:
            rel = ""
    all_recs = records()
    ctx = Context(rel) if rel and rs.parse_tag(rel) else None
    if ctx is not None:
        ctx.prefetch(
            s
            for cid in cve_ids
            if (rec := all_recs.get(cid))
            for r in (rec.get("ranges") or ())
            for s in (r.get("introduced"), r.get("fixed"))
            if s
        )
    results = []
    for cid in cve_ids:
        rec = all_recs.get(cid)
        if not rec:
            continue
        state, basis = affects(rec, rel, ctx) if ctx else ("unknown", "")
        cvss = rec.get("cvss") or {}
        results.append({
            "cve_id": cid,
            "title": rec.get("title") or rec.get("description", "")[:160],
            "state": state,
            "basis": basis,
            "severity": cvss.get("severity") or "",
            "score": cvss.get("score"),
        })
    results.sort(key=lambda r: (0 if r["state"] == "affected" else (1 if r["state"] == "possibly" else 2), r["cve_id"]))
    return {
        "version": version,
        "file_path": clean_path,
        "total": len(results),
        "affected_count": sum(1 for r in results if r["state"] == "affected"),
        "cves": results,
    }
