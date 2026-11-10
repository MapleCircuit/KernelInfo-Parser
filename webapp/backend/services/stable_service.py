"""webapp/backend/services/stable_service.py - Stable backports (H2, webapp/API.md section 4.10).

For a stable series vX.Y: the mainline commits released after vX.Y (and before the series' last stable
release) that were marked for stable — `Cc: stable` whose annotation covers X.Y — or that fix a commit
vX.Y contains, and that have no backport in the series' stable tags.

"Released before" is the first mainline release containing the commit (its tag's commit time), not the
commit's author date.

The database answers when it covers the series (`db_scope_covered()`): `m_commit_relation` (Cc: stable,
Fixes:, backports), `m_commit_release` / `m_release_segment` (first mainline release of each commit,
first stable release of each backport) and `m_commit`, with SELECTs only. Otherwise the two SQLite
indexes answer: the commit relation index (section 2.16) and the release index (section 2.17), the
latter attached to the former's connection so the candidate scan is one query.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Callable

from webapp.backend.config import get_active_repo_name
from webapp.backend.services import commit_relations, release_service
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

REPORT_FORMAT = 2  # 2: candidates bounded by their first release date, not their author date (B13)
_DISK = DiskCache("stable-missing", max_bytes=128 << 20)
_VER = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


def _num(text: str) -> tuple[int, ...] | None:
    m = _VER.search(text or "")
    if not m:
        return None
    a, b, c = m.groups()
    # v2.6.x series carry three numbers; later ones two
    return (int(a), int(b), int(c)) if (int(a), int(b)) == (2, 6) and c is not None else (int(a), int(b))


def series_tuple(series: str) -> tuple[int, ...] | None:
    return _num(series)


def annotation_applies(annotation: str, series: str) -> bool:
    """Whether a `Cc: stable # <annotation>` covers the series.

    "" (no annotation) -> every series; "3.0+" / ">= 3.0" -> 3.0 and later; "v3.0" / "3.0.x" / "3.0" ->
    exactly 3.0; "2.6.32-3.2" -> that range; several comma-separated parts -> any of them. A comment
    without a version (`# for v3.0 and later` is read as 3.0+) applies when nothing contradicts it."""
    target = series_tuple(series)
    if target is None:
        return False
    text = (annotation or "").strip().lower()
    if not text or not _VER.search(text):
        return True
    for part in re.split(r"[,;/]| and ", text):
        part = part.strip()
        if not part:
            continue
        rng = re.match(r".*?v?(\d+\.\d+(?:\.\d+)?)\s*(?:-|\.\.|to)\s*v?(\d+\.\d+(?:\.\d+)?)", part)
        if rng:
            lo, hi = _num(rng.group(1)), _num(rng.group(2))
            if lo and hi and lo[:len(target)] <= target <= hi[:len(target)] + (999,):
                return True
            continue
        n = _num(part)
        if n is None:
            continue
        plus = "+" in part or "later" in part or ">=" in part or "newer" in part
        n = n[:len(target)] if len(n) >= len(target) else n
        if plus and target >= n:
            return True
        if not plus and target == n:
            return True
    return False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(commit_relations.relation_index.path()), timeout=60)
    conn.execute("ATTACH DATABASE ? AS ri", (str(release_service.release_index.path()),))
    return conn


def _tag_dates(tags: dict[str, str], names: list[str]) -> dict[str, int]:
    """Commit time (%ct) of each named tag's commit, with one `git log --no-walk`."""
    shas = sorted({tags[n] for n in names if n in tags})
    dates: dict[str, int] = {}
    for i in range(0, len(shas), 500):
        out = release_service._git(["log", "--no-walk=unsorted", "--format=%H %ct", *shas[i:i + 500]], timeout=60) or ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                dates[parts[0]] = int(parts[1])
    return {n: dates[tags[n]] for n in names if tags.get(n) in dates}


def series_scope(series: str) -> dict[str, Any]:
    """What a report reads: {available, reason?, stable_tags, last_stable, key, last_at, later}. `later` are
    the mainline tags after the series released before its last stable release (tag commit time <=
    last_at): a commit counts by the first mainline release containing it, not by its author date."""
    tags = release_service.tag_commits()
    p = release_service.parse_tag(series)
    if p is None or not p["final"] or series not in tags:
        return {"available": False, "reason": f"{series} is not a release of this repository"}
    stable_tags = sorted((t for t in tags if (q := release_service.parse_tag(t)) and q["series"] == p["series"] and q["stable"]),
                         key=release_service.release_key)
    if not stable_tags:
        return {"available": False, "reason": f"The repository has no stable tags of {series}.y"}
    last_stable = stable_tags[-1]
    key = release_service.release_key(series)
    after = [t for t in tags if (q := release_service.parse_tag(t)) and not q["stable"] and release_service.release_key(t) > key]
    dates = _tag_dates(tags, [last_stable, *after])
    last_at = dates.get(last_stable, 0)
    later = {t for t in after if t in dates and dates[t] <= last_at}
    return {"available": True, "stable_tags": stable_tags, "last_stable": last_stable, "key": key, "last_at": last_at,
            "later": later}


def _result(series: str, scope: dict[str, Any], cand: dict[bytes, dict[str, Any]], backported: set[bytes],
            mainline_of: Callable[[dict[str, Any]], str], source: str) -> dict[str, Any]:
    missing = []
    for src, e in cand.items():
        if src in backported:
            continue
        missing.append({
            "sha": src.hex(), "subject": e["subject"] or "", "date": e["at"] or 0,
            "reasons": e["reasons"], "annotation": e["annotation"],
            "fixes": [{"sha": t[0], "release": t[1]} for t in e["fixes"][:5]],
            "mainline_release": mainline_of(e),
        })
    missing.sort(key=lambda m: (release_service.release_key(m["mainline_release"]), m["date"], m["sha"]))
    counts = {"cc-stable": sum("cc-stable" in m["reasons"] for m in missing), "fixes": sum("fixes" in m["reasons"] for m in missing)}
    return {"series": series, "available": True, "source": source, "last_stable": scope["last_stable"],
            "stable_tags": len(scope["stable_tags"]), "candidates_checked": len(cand), "missing": len(missing),
            "counts": counts, "candidates": missing}


def _add(cand: dict[bytes, dict[str, Any]], src: bytes, kind: int, note: str, target: tuple[str, str] | None,
         series: str, base: dict[str, Any]) -> None:
    """One Cc: stable / Fixes: row of a candidate commit (shared by both paths)."""
    if kind == commit_relations.KIND_STABLE_CC:
        if not annotation_applies(note or "", series):
            return
        entry = cand.setdefault(src, {"reasons": [], "annotation": "", "fixes": [], **base})
        if "cc-stable" not in entry["reasons"]:
            entry["reasons"].append("cc-stable")
        entry["annotation"] = note or ""
    elif target:
        entry = cand.setdefault(src, {"reasons": [], "annotation": "", "fixes": [], **base})
        if "fixes" not in entry["reasons"]:
            entry["reasons"].append("fixes")
        entry["fixes"].append(target)


def build_report(series: str, progress=None, use_db: bool = False) -> dict[str, Any]:
    scope = series_scope(series)
    if not scope["available"]:
        return {"series": series, "available": False, "reason": scope["reason"], "candidates": []}
    if use_db:
        hit = _report_db(series, scope, progress)
        if hit is not None:
            return hit
    return _report_sqlite(series, scope, progress)


def _report_sqlite(series: str, scope: dict[str, Any], progress=None) -> dict[str, Any]:
    key = scope["key"]
    conn = _connect()
    try:
        tag_rows = conn.execute("SELECT idx, name, chain FROM ri.tags").fetchall()
        later_mainline = {idx: name for idx, name, chain in tag_rows if chain == "mainline" and name in scope["later"]}
        stable_set = set(scope["stable_tags"])
        stable_idx = {idx for idx, name, chain in tag_rows if chain == "stable" and name in stable_set}
        if progress:
            progress(1, 4, "candidates")
        rows = conn.execute(
            """
            SELECT r.src, r.kind, r.note, r.prefix, r.dst, f.tag_idx, c.at, c.subject
            FROM rel r
            JOIN ri.first_tag f ON f.sha = r.src AND f.chain = 0
            LEFT JOIN commits c ON c.sha = r.src
            WHERE r.kind IN (?, ?)
            ORDER BY r.rowid
            """,
            (commit_relations.KIND_FIXES, commit_relations.KIND_STABLE_CC),
        ).fetchall()
        if progress:
            progress(2, 4, f"{len(rows):,} marked commits")
        cand: dict[bytes, dict[str, Any]] = {}
        for src, kind, note, prefix, dst, tag_idx, at, subject in rows:
            if tag_idx not in later_mainline:
                continue
            target = _contained_target(conn, dst, prefix, key) if kind == commit_relations.KIND_FIXES else None
            _add(cand, src, kind, note, target, series, {"tag_idx": tag_idx, "at": at, "subject": subject})
        if progress:
            progress(3, 4, f"{len(cand):,} candidates: checking backports")
        backported: set[bytes] = set()
        for src in cand:
            hexsha = src.hex()
            rows = conn.execute(
                "SELECT f.tag_idx FROM rel r JOIN ri.first_tag f ON f.sha = r.src AND f.chain = 1 WHERE r.dst = ? AND r.kind = ?",
                (src, commit_relations.KIND_UPSTREAM)).fetchall()
            # Backports naming the commit by an abbreviated hash (B12)
            rows += [(t,) for t, pre in conn.execute(
                "SELECT f.tag_idx, r.prefix FROM rel r JOIN ri.first_tag f ON f.sha = r.src AND f.chain = 1 "
                "WHERE r.p8 = ? AND r.kind = ? AND r.dst IS NULL", (hexsha[:8], commit_relations.KIND_UPSTREAM)) if hexsha.startswith(pre or "~")]
            if any(b[0] in stable_idx for b in rows):
                backported.add(src)
    finally:
        conn.close()
    return _result(series, scope, cand, backported, lambda e: later_mainline.get(e["tag_idx"], ""), "index")


def _contained_target(conn: sqlite3.Connection, dst: bytes | None, prefix: str | None, series_key: tuple) -> tuple[str, str] | None:
    """(sha, first release) of a Fixes: target when the series contains it (first mainline tag <= series)."""
    if dst:
        rows = conn.execute("SELECT f.sha, t.name FROM ri.first_tag f JOIN ri.tags t ON t.idx = f.tag_idx WHERE f.sha = ? AND f.chain = 0", (dst,)).fetchall()
    else:
        p = (prefix or "").lower()
        if len(p) < 8:
            return None
        even = p[: len(p) // 2 * 2]
        lo = bytes.fromhex(even)
        hi = (int.from_bytes(lo, "big") + 1).to_bytes(len(lo), "big") if lo != b"\xff" * len(lo) else None
        q = "SELECT f.sha, t.name FROM ri.first_tag f JOIN ri.tags t ON t.idx = f.tag_idx WHERE f.chain = 0 AND f.sha >= ?"
        args: list[Any] = [lo]
        if hi is not None:
            q += " AND f.sha < ?"
            args.append(hi)
        rows = [r for r in conn.execute(q + " LIMIT 50", args).fetchall() if r[0].hex().startswith(p)]
        if len(rows) != 1:
            return None  # unknown or ambiguous
    if not rows:
        return None
    sha, name = rows[0]
    if release_service.release_key(name) > series_key:
        return None
    return sha.hex(), name


# ----------------------------------------------------------------------------------- database path

def _hexv(v: Any) -> str:
    return bytes(v).hex() if v is not None else ""


def _textv(v: Any) -> str:
    return bytes(v).decode("utf-8", "replace") if isinstance(v, (bytes, bytearray, memoryview)) else str(v or "")


def db_scope_covered(scope: dict[str, Any]) -> bool:
    """The database answers the report: relation rows cover the stored commits, release membership covers
    every tag, and every commit of the later mainline releases and of the series' stable releases is
    ingested (commit_relations.tags_ingested)."""
    if not scope.get("available"):
        return False
    return commit_relations.tags_ingested(sorted(scope["later"]) + list(scope["stable_tags"]))


def _report_db(series: str, scope: dict[str, Any], progress=None) -> dict[str, Any] | None:
    """The report from m_commit_relation, m_commit_release, m_release_segment and m_commit (SELECT only);
    None when a query fails (the SQLite path answers)."""
    from parser.git_ast.commit_relations import resolve_prefixes
    from webapp.backend.config import get_repo_dir
    from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id
    from webapp.backend.database.pool import get_db_cursor
    k_fix, k_cc, k_up = commit_relations.KIND_FIXES, commit_relations.KIND_STABLE_CC, commit_relations.KIND_UPSTREAM
    tags = release_service.tag_commits()
    key = scope["key"]
    stable_set = set(scope["stable_tags"])
    try:
        with get_db_cursor() as cur:
            repo_id = get_active_repo_id(cur)
            network_id = get_active_network_id(cur)
            if network_id is None:
                return None
            cur.execute("SELECT segment_id, tag, tag_hash, chain FROM m_release_segment WHERE repo_id = %s;", (repo_id,))
            seg_tag: dict[int, str] = {}
            for r in cur.fetchall():
                tag = _textv(r["tag"])
                if tags.get(tag) == _hexv(r["tag_hash"]):
                    seg_tag[int(r["segment_id"])] = tag
            later_sids = [sid for sid, t in seg_tag.items() if t in scope["later"]]
            stable_sids = {sid for sid, t in seg_tag.items() if t in stable_set}
            if progress:
                progress(1, 4, "candidates")
            rows: list[dict[str, Any]] = []
            for i in range(0, len(later_sids), 200):
                part = later_sids[i:i + 200]
                cur.execute(
                    f"""
                    SELECT c.commit_id, c.commit_hash, c.author_date, c.subject, r.kind, r.idx, r.note, r.target_hash,
                           r.target_prefix, cr.segment_id
                    FROM m_commit_release cr
                    JOIN m_commit c ON c.network_id = %s AND c.commit_hash = cr.commit_hash
                    JOIN m_commit_relation r ON r.commit_id = c.commit_id AND r.kind IN (%s, %s)
                    WHERE cr.repo_id = %s AND cr.chain = %s AND cr.segment_id IN ({",".join(["%s"] * len(part))});
                    """,
                    (network_id, k_fix, k_cc, repo_id, release_service.DB_CHAIN_MAINLINE, *part),
                )
                rows.extend(cur.fetchall())
            rows.sort(key=lambda r: (int(r["commit_id"]), int(r["kind"]), int(r["idx"])))
            if progress:
                progress(2, 4, f"{len(rows):,} marked commits")
            # Fixes: targets, their first mainline release
            zero = commit_relations.ZERO_HASH
            unresolved = {_textv(r["target_prefix"]).lower() for r in rows
                          if int(r["kind"]) == k_fix and bytes(r["target_hash"]) == zero and r["target_prefix"]}
            resolved = resolve_prefixes(get_repo_dir(), unresolved) if unresolved else {}

            def target_of(r: dict[str, Any]) -> str | None:
                h = bytes(r["target_hash"])
                return h.hex() if h != zero else resolved.get(_textv(r["target_prefix"]).lower())

            targets = sorted({t for r in rows if int(r["kind"]) == k_fix and (t := target_of(r))})
            first: dict[str, str] = {}
            for i in range(0, len(targets), 500):
                part = targets[i:i + 500]
                cur.execute(
                    f"SELECT commit_hash, segment_id FROM m_commit_release WHERE repo_id = %s AND chain = %s "
                    f"AND commit_hash IN ({','.join(['%s'] * len(part))});",
                    (repo_id, release_service.DB_CHAIN_MAINLINE, *(bytes.fromhex(t) for t in part)),
                )
                for r in cur.fetchall():
                    tag = seg_tag.get(int(r["segment_id"]))
                    if tag:
                        first[_hexv(r["commit_hash"])] = tag
            cand: dict[bytes, dict[str, Any]] = {}
            for r in rows:
                src = bytes(r["commit_hash"])
                kind = int(r["kind"])
                target = None
                if kind == k_fix:
                    t = target_of(r)
                    tag = first.get(t) if t else None
                    target = (t, tag) if tag and release_service.release_key(tag) <= key else None
                _add(cand, src, kind, _textv(r["note"]), target, series,
                     {"tag": seg_tag.get(int(r["segment_id"]), ""), "at": int(r["author_date"] or 0),
                      "subject": _textv(r["subject"])[:commit_relations.SUBJECT_MAX]})
            if progress:
                progress(3, 4, f"{len(cand):,} candidates: checking backports")
            backported: set[bytes] = set()
            srcs = list(cand)
            for i in range(0, len(srcs), 500):
                part = srcs[i:i + 500]
                cur.execute(
                    f"""
                    SELECT r.target_hash, cr.segment_id
                    FROM m_commit_relation r
                    JOIN m_commit c ON c.commit_id = r.commit_id
                    JOIN m_commit_release cr ON cr.repo_id = %s AND cr.commit_hash = c.commit_hash AND cr.chain = %s
                    WHERE r.kind = %s AND c.network_id = %s AND r.target_hash IN ({",".join(["%s"] * len(part))});
                    """,
                    (repo_id, release_service.DB_CHAIN_STABLE, k_up, network_id, *part),
                )
                for r in cur.fetchall():
                    if int(r["segment_id"]) in stable_sids:
                        backported.add(bytes(r["target_hash"]))
    except Exception as exc:  # noqa: BLE001 - the SQLite path answers
        logger.warning("stable report for %s from the database failed: %s", series, exc)
        return None
    return _result(series, scope, cand, backported, lambda e: e["tag"], "db")


def missing_report(series: str, wait: float = 0.0) -> dict[str, Any]:
    """Job state of the report ({state, progress, result?}). The database answers when it covers the
    series (db_scope_covered); otherwise 'building' until both SQLite indexes are ready."""
    scope = series_scope(series)
    use_db = db_scope_covered(scope)
    if use_db:
        cache_key = (f"{get_active_repo_name()}|{series}|{REPORT_FORMAT}|db|{commit_relations.relations_token()}|"
                     f"{release_service.release_token()}")
    else:
        for state in (release_service.ensure_release_index(require_sqlite=True), commit_relations.ensure_index()):
            if state.get("state") != "done":
                return {"state": "building", "progress": state.get("progress") or {}, "waiting_for": "indexes"}
        built = (commit_relations.relation_index.meta("built_at") or "0", str(release_service.release_index.stats().get("built_at") or 0))
        cache_key = f"{get_active_repo_name()}|{series}|{REPORT_FORMAT}|{built[0]}|{built[1]}"
    key = f"stable-missing:{cache_key}"
    state = jobs.submit(key, lambda progress: build_report(series, progress, use_db=use_db), cache=_DISK, cache_key=cache_key)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(key, wait) or state
    return state


def stable_series() -> list[dict[str, Any]]:
    """Releases that have stable tags: [{series, stable_tags, last}] newest first."""
    tags = release_service.tag_commits()
    groups: dict[str, list[str]] = {}
    for t in tags:
        p = release_service.parse_tag(t)
        if p and p["stable"]:
            groups.setdefault(release_service.series_name(p["series"]), []).append(t)
    out = [{"series": s, "stable_tags": len(v), "last": max(v, key=release_service.release_key)}
           for s, v in groups.items() if s in tags]
    out.sort(key=lambda x: release_service.release_key(x["series"]), reverse=True)
    return out
