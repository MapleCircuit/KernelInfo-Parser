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
import re
import threading
import time
from typing import Any, Callable, Iterable

from fastapi import HTTPException

from parser.cve.cve_records import (  # noqa: F401 - re-exported for callers and tests
    CVE_ID,
    HEXSHA as _HEXSHA,
    KIND_NAMES,
    RECORD_FORMAT,
    ROLE_FIX,
    ROLE_INTRODUCED,
    description_bound,
    kernel_commits,
    parse_record,
    version_status,
)
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.services import release_service as rs
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

REPORT_FORMAT = 3
SOURCE_TTL = 60.0
_FULL = re.compile(r"^[0-9a-f]{40}$")

_DISK = DiskCache("cve-index", max_bytes=256 << 20, max_entries=6)
_SOURCE: dict[str, Any] = {"checked": 0.0, "row": None}
_MEM: dict[str, Any] = {"built_at": None, "records": {}}
_LOCK = threading.Lock()


# ------------------------------------------------------------------------------------- the ingested list

def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")


def source(fresh: bool = False) -> dict[str, Any] | None:
    """The CVE list the parser ingested (m_cve_source), checked at most every SOURCE_TTL seconds; None if none."""
    now = time.monotonic()
    if not fresh and now - _SOURCE["checked"] < SOURCE_TTL:
        return _SOURCE["row"]
    row = None
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT name, layout, head, format, records, built_at FROM m_cve_source ORDER BY built_at DESC LIMIT 1")
            r = cur.fetchone()
        if r:
            row = {"name": _text(r["name"]), "layout": _text(r["layout"]), "head": bytes(r["head"]).hex(),
                   "format": int(r["format"]), "records": int(r["records"]), "built_at": int(r["built_at"])}
    except Exception as exc:  # noqa: BLE001 - no database, or one without the CVE tables
        logger.debug("m_cve_source unavailable: %s", exc)
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
    with _LOCK:
        if _MEM["built_at"] == src["built_at"]:
            return _MEM["records"]
    out: dict[str, dict[str, Any]] = {}
    started = time.time()
    with get_db_cursor() as cur:
        cur.execute("SELECT cve_id, data FROM m_cve")
        for r in cur.fetchall():
            rec = _decode(r["data"])
            if rec is not None:
                out[_text(r["cve_id"])] = rec
    logger.info("Loaded %d CVE records in %.1f s", len(out), time.time() - started)
    with _LOCK:
        _MEM.update(built_at=src["built_at"], records=out)
    return out


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
        return [full] + [full[:n] for n in (12, 13, 14, 16)]  # references that abbreviate the commit

    wanted = {k: full for full in [sha, *ups] for k in keys(full)}
    hits: dict[str, dict[int, list[str]]] = {}
    try:
        with get_db_cursor() as cur:
            keys_list = sorted(wanted)
            cur.execute(
                f"SELECT commit_hash, role, cve_id FROM m_cve_commit WHERE commit_hash IN ({','.join(['%s'] * len(keys_list))}) "  # noqa: S608
                "ORDER BY cve_id",
                keys_list,
            )
            for r in cur.fetchall():
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
    # Introducing commits are recorded in full (40 hex digits), so only the full SHA matches them
    intro = hits.get(sha, {}).get(ROLE_INTRODUCED, [])
    up = [c for u in ups for c in hits.get(u, {}).get(ROLE_FIX, []) if c not in own]
    return {"fixes": own, "introduced": intro, "fixes_upstream": list(dict.fromkeys(up))}


# ------------------------------------------------------------------------------------- releases

def stored_prefixes(prefixes: list[str]) -> dict[str, str]:
    """{prefix: full SHA} of the abbreviated hashes m_cve_commit_hash resolved for the active repository
    (SELECT only; an older database without the table answers {})."""
    out: dict[str, str] = {}
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        with get_db_cursor() as cursor:
            repo_id = get_active_repo_id(cursor)
            for i in range(0, len(prefixes), 1000):
                part = prefixes[i:i + 1000]
                cursor.execute(
                    f"SELECT prefix, commit_hash FROM m_cve_commit_hash WHERE repo_id = %s AND prefix IN ({','.join(['%s'] * len(part))});",
                    (repo_id, *part),
                )
                for r in cursor.fetchall():
                    prefix = r["prefix"].decode() if isinstance(r["prefix"], (bytes, bytearray)) else r["prefix"]
                    out[prefix] = bytes(r["commit_hash"]).hex()
    except Exception as exc:  # noqa: BLE001 - git resolves them
        logger.debug("m_cve_commit_hash lookup failed: %s", exc)
    return out


class Context:
    """Containment of commits in one release tag, memoized: release membership (first tags), stable
    backports and Fixes: targets (m_commit_relation first, else the commit relation index when built)."""

    def __init__(self, release: str) -> None:
        self.release = release
        self.p = rs.parse_tag(release)
        self.version = release[1:] if release.startswith("v") else release
        self.key = rs.release_key(release)
        self.base_key = rs.release_key(rs.series_name(self.p["series"])) if self.p else None
        self._tags: dict[str, dict[str, Any] | None] = {}
        self._full: dict[str, str | None] = {}
        self._backports_complete: bool | None = None

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
        shas = list(dict.fromkeys(s for s in shas if s))
        short = [s for s in shas if not _FULL.match(s) and _HEXSHA.match(s) and s not in self._full]
        if short:
            self._full.update(stored_prefixes(short))
        shas = [self._full.get(s, s) if not _FULL.match(s) else s for s in shas]
        want = [s for s in dict.fromkeys(shas) if s and _FULL.match(s) and s not in self._tags]
        found = rs.release_lookup(want)
        for s in want:
            hit = found.get(s)
            self._tags[s] = hit if hit and (hit.get("mainline") or hit.get("stable")) else None

    def tags(self, sha: str | None) -> dict[str, Any] | None:
        full = self.full(sha)
        if not full:
            return None
        if full not in self._tags:
            hit = rs.release_lookup([full]).get(full)
            self._tags[full] = hit if hit and (hit.get("mainline") or hit.get("stable")) else None
        return self._tags[full]

    def contains(self, sha: str | None) -> bool:
        t = self.tags(sha)
        if not t or not self.p:
            return False
        m, s = t.get("mainline"), t.get("stable")
        if not self.p["stable"]:
            return bool(m) and rs.release_key(m) <= self.key
        if m and rs.release_key(m) <= self.base_key:
            return True
        q = rs.parse_tag(s) if s else None
        return bool(q) and q["series"] == self.p["series"] and q["stable"] <= self.p["stable"]

    def applies(self, fix: str) -> bool:
        """A fixing commit on the release's branch (or not released anywhere yet)."""
        t = self.tags(fix)
        if not t:
            return True
        if t.get("mainline"):
            return True
        q = rs.parse_tag(t["stable"]) if t.get("stable") else None
        return bool(self.p and self.p["stable"] and q and q["series"] == self.p["series"])

    def first_here(self, sha: str | None) -> bool:
        """The commit first appears in this release: its stable update, its -rc, or its cycle (-rc1 .. final)."""
        t = self.tags(sha)
        if not t or not self.p:
            return False
        first = t.get("stable") if self.p["stable"] else t.get("mainline")
        if not first:
            return False
        if self.p["stable"] or self.p["pre"]:
            return first == self.release
        q = rs.parse_tag(first)
        return bool(q) and not q["stable"] and q["series"] == self.p["series"]

    def _backport_tags(self) -> list[str]:
        """The releases whose commits contains() reads backports from: mainline tags up to the release and,
        for a stable release, its series' stable tags up to it."""
        out = []
        for t in rs.tag_commits():
            q = rs.parse_tag(t)
            if not q or not self.p:
                continue
            if not q["stable"] and rs.release_key(t) <= self.key:
                out.append(t)
            elif self.p["stable"] and q["stable"] and q["series"] == self.p["series"] and q["stable"] <= self.p["stable"]:
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
    basis: record (the kernel CNA's data) | fixes-tag | reference | description."""
    ctx = ctx or Context(release)
    if ctx.p is None:
        return "unknown", ""
    kind = rec.get("kind", "cna")
    if kind == "cna":
        if not ctx.p["pre"] and rec.get("entries"):
            st = version_status(rec["entries"], ctx.version)
            if st:
                return st, "record"
        verdict = None
        pending = False
        is_affected = False
        for r in rec.get("ranges") or []:
            fix, intro = r.get("fixed"), r.get("introduced")
            released = ctx.tags(fix) is not None if fix else False
            if released and not ctx.applies(fix):
                continue  # the fix of another stable branch
            fix_in = released and ctx.contains(fix)
            intro_in = intro is None or ctx.contains(intro)
            if intro_in and fix_in:
                verdict = "fixed"
                break
            if intro_in and not fix_in:
                if fix and not released:
                    pending = True  # a fix in no release yet (or unknown to this clone): its branch is unknown
                    continue
                is_affected = True
        if verdict:
            return verdict, "record"
        if is_affected or pending:
            return "affected", "record"
        return "unaffected", "record"
    if kind == "commit":
        fixes = [r["fixed"] for r in rec.get("ranges") or [] if r.get("fixed")]
        for f in fixes:
            if ctx.contains(f) or any(ctx.contains(b) for b in ctx.backports(f)):
                return "fixed", "reference"
        intros = [i for f in fixes for i in ctx.fixes_tags(f)]
        if intros:
            return ("affected" if any(ctx.contains(i) for i in intros) else "unaffected"), "fixes-tag"
        return ("possibly", "reference") if fixes else ("unknown", "")
    if not ctx.p["pre"] and rec.get("entries"):
        st = version_status(rec["entries"], ctx.version)
        if st == "affected":
            return "possibly", "description" if any(e.get("source") == "description" for e in rec["entries"]) else "versions"
    return "unknown", ""


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
    rows = []
    for i, rec in enumerate(recs):
        if progress and i % 1000 == 0:
            progress(i, len(recs), "checking records")
        state, basis = affects(rec, release, ctx)
        if state not in ("affected", "fixed", "possibly"):
            continue
        fixes = [r["fixed"] for r in rec["ranges"] if r.get("fixed")]
        cvss = rec.get("cvss") or {}
        rows.append({"id": rec["id"], "title": rec["title"] or rec["description"][:160], "state": state, "basis": basis,
                     "kind": rec["kind"], "published": rec["published"], "fixes": fixes[:4],
                     "fixed_here": state == "fixed" and any(ctx.first_here(f) for f in fixes),
                     "score": cvss.get("score"), "severity": cvss.get("severity") or "", "kev": rec.get("kev")})
    rows.sort(key=lambda r: r["id"], reverse=True)
    counts = {k: sum(r["state"] == k for r in rows) for k in ("affected", "possibly", "fixed")}
    counts["fixed_here"] = sum(1 for r in rows if r["fixed_here"])
    return {"release": release, "head": src["head"], "layout": src["layout"], "kinds": kinds(all_recs),
            "cves": rows, "counts": counts}


def report(release: str, wait: float = 0.0) -> dict[str, Any]:
    if release not in rs.tag_commits():
        raise HTTPException(status_code=404, detail=f"Not a release of this repository: {release}")
    src = source()
    if src is None:
        from parser.cve.cve_ingest import repo_dir
        return {"state": "done", "result": {"configured": repo_dir() is not None, "ingested": False, "cves": []}}
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
    out = {**rec, "ranges": ranges, "url": f"https://www.cve.org/CVERecord?id={cve_id}"}
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
    with get_db_cursor() as cursor:
        cursor.execute("SELECT cve_id FROM m_cve_file WHERE file_path = %s;", (clean_path,))
        cve_ids = [safe_decode(r["cve_id"]) for r in cursor.fetchall() if r.get("cve_id")]
    all_recs = records()
    ctx = Context(version) if version else None
    results = []
    for cid in cve_ids:
        rec = all_recs.get(cid)
        if not rec:
            continue
        state, basis = affects(rec, version, ctx) if ctx else ("unknown", "")
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
