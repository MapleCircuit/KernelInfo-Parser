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
import itertools
from typing import Any, Callable

from cache_engine import Eq, In, IsNull, Range
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
    relations = commit_relations.relation_index.space()
    releases = release_service.release_index.space()
    rel, commits = relations.store("rel"), relations.store("commits")
    first_tag, tags = releases.store("first_tag"), releases.store("tags")
    tag_rows = list(tags.scan(columns=("idx", "name", "chain")))
    names = {idx: name for idx, name, _chain in tag_rows}
    later_mainline = {idx: name for idx, name, chain in tag_rows if chain == "mainline" and name in scope["later"]}
    stable_set = set(scope["stable_tags"])
    stable_idx = {idx for idx, name, chain in tag_rows if chain == "stable" and name in stable_set}
    if progress:
        progress(1, 4, "candidates")
    # Marked commits (Fixes: / Cc: stable) in insertion order, kept when their first mainline tag is a later release
    marked = rel.scan(In("kind", (commit_relations.KIND_FIXES, commit_relations.KIND_STABLE_CC)),
                      columns=("src", "kind", "note", "prefix", "dst"))
    # Commits whose first mainline release is a later one (one scan of the release index instead of a lookup per marked row)
    in_later = {sha: tag_idx for sha, tag_idx in first_tag.scan([Eq("chain", 0), In("tag_idx", sorted(later_mainline))],
                                                                  columns=("sha", "tag_idx"))}
    cand: dict[bytes, dict[str, Any]] = {}
    release_keys: dict[str, tuple] = {}
    targets_seen: dict[tuple, Any] = {}
    total = 0
    while True:
        batch = list(itertools.islice(marked, 2000))
        if not batch:
            break
        total += len(batch)
        kept = [(r, in_later[r[0]]) for r in batch if r[0] in in_later]
        info = commits.get_many({r[0] for r, _ in kept})
        # first mainline release of every full-hash Fixes: target of the batch, in one lookup
        dst_first = first_tag.get_many({(r[4], 0) for r, _ in kept if r[1] == commit_relations.KIND_FIXES and r[4]})
        for (src, kind, note, prefix, dst), tag_idx in kept:
            row = info.get(src)
            target = None
            if kind == commit_relations.KIND_FIXES:
                memo_key = (dst, prefix)   # many commits fix the same target
                if memo_key not in targets_seen:
                    targets_seen[memo_key] = _contained_target(first_tag, names, dst, prefix, key, dst_first, release_keys)
                target = targets_seen[memo_key]
            _add(cand, src, kind, note, target, series, {"tag_idx": tag_idx, "at": row[1] if row else None,
                                                         "subject": row[2] if row else None})
    if progress:
        progress(3, 4, f"{total:,} marked commits, {len(cand):,} candidates: checking backports")
    backported: set[bytes] = set()
    pending = list(cand)
    for i in range(0, len(pending), 1000):
        part = pending[i:i + 1000]
        # Upstream relations naming a candidate by full hash, then by an abbreviated hash (B12): one scan each per batch
        by_dst: dict[bytes, list[bytes]] = {}
        for src, dst in rel.scan([In("dst", part), Eq("kind", commit_relations.KIND_UPSTREAM)], columns=("src", "dst")):
            by_dst.setdefault(dst, []).append(src)
        by_p8: dict[str, list[tuple[bytes, str | None]]] = {}
        for b, pre, p8, dst in rel.scan(
                [In("p8", sorted({c.hex()[:8] for c in part})), Eq("kind", commit_relations.KIND_UPSTREAM)],
                columns=("src", "prefix", "p8", "dst")):
            if dst is None:   # filtered here: `dst IS NULL` makes SQLite walk the dst index instead of p8's
                by_p8.setdefault(p8, []).append((b, pre))
        backports = {c: by_dst.get(c, []) + [b for b, pre in by_p8.get(c.hex()[:8], ()) if c.hex().startswith(pre or "~")]
                     for c in part}
        stable_firsts = first_tag.get_many({(b, 1) for bs in backports.values() for b in bs})
        for c, bs in backports.items():
            if any((b, 1) in stable_firsts and stable_firsts[(b, 1)][2] in stable_idx for b in bs):
                backported.add(c)
    return _result(series, scope, cand, backported, lambda e: later_mainline.get(e["tag_idx"], ""), "index")


def _contained_target(first_tag: Any, names: dict[int, str], dst: bytes | None, prefix: str | None,
                      series_key: tuple, known: dict | None = None, release_keys: dict | None = None) -> tuple[str, str] | None:
    """(sha, first release) of a Fixes: target when the series contains it (first mainline tag <= series).
    `known` holds already fetched `first_tag` records by `(sha, chain)`; `release_keys` memoizes `release_key()`."""
    if dst:
        rec = known.get((dst, 0)) if known is not None else first_tag.get((dst, 0))
        rows = [(dst, names[rec[2]])] if rec and rec[2] in names else []
    else:
        p = (prefix or "").lower()
        if len(p) < 8:
            return None
        even = p[: len(p) // 2 * 2]
        lo = bytes.fromhex(even)
        hi = (int.from_bytes(lo, "big") + 1).to_bytes(len(lo), "big") if lo != b"\xff" * len(lo) else None
        found = first_tag.scan([Range("sha", ge=lo, lt=hi), Eq("chain", 0)], order_by=("sha",), limit=50)
        rows = [(sha, names[idx]) for sha, _chain, idx in found if sha.hex().startswith(p) and idx in names]
        if len(rows) != 1:
            return None  # unknown or ambiguous
    if not rows:
        return None
    sha, name = rows[0]
    if release_keys is not None:
        if name not in release_keys:
            release_keys[name] = release_service.release_key(name)
        name_key = release_keys[name]
    else:
        name_key = release_service.release_key(name)
    if name_key > series_key:
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
    """The report from m_commit_relation, m_commit_release, m_release_segment and m_commit (read only);
    None when a query fails (the SQLite path answers)."""
    from parser.git_ast.commit_relations import resolve_prefixes
    from webapp.backend.config import get_repo_dir
    from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id
    from db_engine.spec import Join, Value
    from webapp.backend.database.pool import get_reader
    k_fix, k_cc, k_up = commit_relations.KIND_FIXES, commit_relations.KIND_STABLE_CC, commit_relations.KIND_UPSTREAM
    tags = release_service.tag_commits()
    key = scope["key"]
    stable_set = set(scope["stable_tags"])
    try:
        with get_reader() as cur:
            repo_id = get_active_repo_id(cur)
            network_id = get_active_network_id(cur)
            if network_id is None:
                return None
            seg_tag: dict[int, str] = {}
            for r in cur.select_rows("m_release_segment", ["segment_id", "tag", "tag_hash", "chain"], {"repo_id": repo_id}):
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
                rows.extend(cur.select_rows(
                    ("m_commit_release", "cr"),
                    {"commit_id": "c.commit_id", "commit_hash": "c.commit_hash", "author_date": "c.author_date",
                     "subject": "c.subject", "kind": "r.kind", "idx": "r.idx", "note": "r.note",
                     "target_hash": "r.target_hash", "target_prefix": "r.target_prefix", "segment_id": "cr.segment_id"},
                    {"cr.repo_id": repo_id, "cr.chain": release_service.DB_CHAIN_MAINLINE, "cr.segment_id": part,
                     "r.kind": [k_fix, k_cc]},
                    joins=[
                        Join(("m_commit", "c"), {"c.network_id": Value(network_id), "c.commit_hash": "cr.commit_hash"}),
                        Join(("m_commit_relation", "r"), {"r.commit_id": "c.commit_id"}),
                    ],
                ))
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
                for r in cur.select_rows(
                    "m_commit_release", ["commit_hash", "segment_id"],
                    {"repo_id": repo_id, "chain": release_service.DB_CHAIN_MAINLINE,
                     "commit_hash": [bytes.fromhex(t) for t in part]},
                ):
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
                for r in cur.select_rows(
                    ("m_commit_relation", "r"),
                    {"target_hash": "r.target_hash", "segment_id": "cr.segment_id"},
                    {"r.kind": k_up, "c.network_id": network_id, "r.target_hash": part},
                    joins=[
                        Join(("m_commit", "c"), {"c.commit_id": "r.commit_id"}),
                        Join(("m_commit_release", "cr"), {
                            "cr.repo_id": Value(repo_id), "cr.commit_hash": "c.commit_hash",
                            "cr.chain": Value(release_service.DB_CHAIN_STABLE),
                        }),
                    ],
                ):
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
