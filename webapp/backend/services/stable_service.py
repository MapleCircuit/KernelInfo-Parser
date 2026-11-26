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

import hashlib
import itertools
import logging
import re
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import HTTPException

from cache_engine import Eq, In, Range
from parser.git_ast.commit_relations import parse_relations, resolve_prefixes
from parser.git_ast.insights_deltas import extract_release_deltas_from_git
from parser.git_ast.release_order import (
    release_ranges,
    series_ord_for,
    series_track_for,
)
from webapp.backend.config import get_active_insights_config, get_active_repo_name
from webapp.backend.services import commit_relations, release_service
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

REPORT_FORMAT = 3  # 3: attaches summary, scoped git fallback without full index build
_DISK = DiskCache("stable-missing", max_bytes=128 << 20)
_VER = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


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
    cached = release_service.tag_dates()
    dates: dict[str, int] = {n: cached[n] for n in names if cached.get(n)}
    missing = [n for n in names if n not in dates and n in tags]
    if missing:
        shas = sorted({tags[n] for n in missing})
        sha_dates: dict[str, int] = {}
        for i in range(0, len(shas), 500):
            out = release_service._git(["log", "--no-walk=unsorted", "--format=%H %ct", *shas[i:i + 500]], timeout=60) or ""
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    sha_dates[parts[0]] = int(parts[1])
        for n in missing:
            sha = tags.get(n)
            if sha in sha_dates:
                dates[n] = sha_dates[sha]
    return {n: dates[n] for n in names if n in dates}


def series_scope(series: str) -> dict[str, Any]:
    """What a report reads: {available, reason?, stable_tags, last_stable, key, last_at, later}. `later` are
    the mainline tags after the series released before its last stable release (tag commit time <=
    last_at): a commit counts by the first mainline release containing it, not by its author date."""
    tags = release_service.tag_commits()
    p = release_service.parse_tag(series)
    if p is None or not p["final"] or series not in tags:
        return {"available": False, "reason": f"{series} is not a release of this repository"}
    stable_tags = sorted(
        (t for t in tags if (q := release_service.parse_tag(t)) and q["series"] == p["series"] and q["kind"] == "stable"),
        key=release_service.release_key,
    )
    if not stable_tags:
        return {"available": False, "reason": f"The repository has no stable tags of {series}.y"}
    last_stable = stable_tags[-1]
    key = release_service.release_key(series)
    after = [
        t for t in tags
        if (q := release_service.parse_tag(t))
        and q["kind"] == "mainline"
        and q["family"] == p["family"]
        and release_service.release_key(t) > key
    ]
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


def _indexes_ready() -> bool:
    return (
        commit_relations.relation_index.built()
        and commit_relations.relation_index.is_current()
        and release_service.release_index.is_current()
    )


def build_report(series: str, progress=None, use_db: bool = False) -> dict[str, Any]:
    scope = series_scope(series)
    if not scope["available"]:
        return {"series": series, "available": False, "reason": scope["reason"], "candidates": []}
    if use_db:
        hit = _report_db(series, scope, progress)
        if hit is None:
            raise RuntimeError(f"database stable report failed for {series}")
        return hit
    if _indexes_ready():
        return _report_sqlite(series, scope, progress)
    return _report_git_scoped(series, scope, progress)


def _git_stdout(args: list[str], *, repo_dir: Any, timeout: float = 180.0) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _target_releases_from_git(
    repo_dir: Any,
    tags: dict[str, str],
    series: str,
    series_key: tuple,
    target_shas: set[str],
) -> dict[str, str]:
    """First mainline release <= `series` containing each target SHA in `target_shas`."""
    if not target_shas:
        return {}
    out: dict[str, str] = {}
    try:
        db_hits = release_service.db_lookup(sorted(target_shas))
        for sha, info in db_hits.items():
            ml = info.get("mainline")
            if ml and release_service.release_key(ml) <= series_key:
                out[sha] = ml
    except Exception as exc:  # noqa: BLE001
        logger.debug("db_lookup for target releases failed: %s", exc)
    remaining = target_shas - set(out)
    if not remaining:
        return out
    if release_service.release_index.stats().get("built_at"):
        try:
            idx_hits = release_service.release_index.lookup_many(sorted(remaining))
            for sha, info in idx_hits.items():
                ml = info.get("mainline")
                if ml and release_service.release_key(ml) <= series_key:
                    out[sha] = ml
        except Exception as exc:  # noqa: BLE001
            logger.debug("release_index lookup for target releases failed: %s", exc)
    remaining = target_shas - set(out)
    if not remaining:
        return out

    ranges = [
        r for r in release_ranges(tags)
        if r["kind"] == "mainline" and release_service.release_key(r["tag"]) <= series_key
    ]
    if not ranges:
        return {s: out.get(s, series) for s in target_shas}

    def walk(r: dict[str, Any]) -> tuple[str, list[str]]:
        base = r["segment_base"]
        args = ["rev-list", tags[r["tag"]]] + ([f"^{tags[base]}"] if base and base in tags else [])
        res = _git_stdout(args, repo_dir=repo_dir, timeout=180)
        hits = [line for line in res.splitlines() if line in remaining]
        return r["tag"], hits

    with ThreadPoolExecutor(max_workers=release_service.RANGE_WORKERS) as pool:
        for rel_tag, hits in pool.map(walk, ranges):
            for sha in hits:
                if sha not in out:
                    out[sha] = rel_tag
    for sha in target_shas:
        out.setdefault(sha, series)
    return out


def _report_git_scoped(series: str, scope: dict[str, Any], progress=None) -> dict[str, Any]:
    """Scoped Git fallback for unparsed versions: walks only `(series, last_later]` and `(series, last_stable]`
    without triggering a full-repo SQLite index build."""
    repo_dir = release_service.get_repo_dir()
    tags = release_service.tag_commits()
    series_sha = tags[series]
    later_ordered = sorted(scope["later"], key=release_service.release_key)
    if not later_ordered:
        return _result(series, scope, {}, set(), lambda e: "", "git")

    if progress:
        progress(1, 4, "series ancestors")
    rev_out = _git_stdout(["rev-list", series_sha], repo_dir=repo_dir, timeout=180)
    ancestors: set[bytes] = {bytes.fromhex(line) for line in rev_out.splitlines() if _FULL_SHA.match(line)}

    max_later = later_ordered[-1]
    fmt = "%x1e%H%x1f%at%x1f%s%x1f%B"
    log_out = _git_stdout(
        [
            "log", "--no-merges", "--no-color", "-E", "-i",
            "--grep=^[[:space:]]*Fixes:", "--grep=^[[:space:]]*Cc:.*stable@",
            f"--format={fmt}", tags[max_later], f"^{series_sha}",
        ],
        repo_dir=repo_dir,
        timeout=300,
    )

    if progress:
        progress(2, 4, "marked commits")
    parsed_records: list[tuple[bytes, int, str, dict[str, Any]]] = []
    unresolved_prefixes: set[str] = set()
    for chunk in log_out.split("\x1e"):
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f", 3)
        if len(parts) < 4:
            continue
        sha_hex, at_raw, subject, body = parts[0].strip(), parts[1].strip(), parts[2], parts[3]
        if not _FULL_SHA.match(sha_hex):
            continue
        rels = parse_relations(body)
        if not rels["fixes"] and not rels["stable_cc"]:
            continue
        for pfx, _subj in rels["fixes"]:
            if len(pfx) < 40:
                unresolved_prefixes.add(pfx)
        parsed_records.append((
            bytes.fromhex(sha_hex),
            int(at_raw) if at_raw.isdigit() else 0,
            subject[:commit_relations.SUBJECT_MAX],
            rels,
        ))

    resolved = resolve_prefixes(repo_dir, unresolved_prefixes) if unresolved_prefixes else {}
    target_shas_in_series: set[str] = set()
    for _src, _at, _subj, rels in parsed_records:
        for pfx, _ in rels["fixes"]:
            full = pfx if len(pfx) == 40 else resolved.get(pfx.lower())
            if full and _FULL_SHA.match(full) and bytes.fromhex(full) in ancestors:
                target_shas_in_series.add(full)

    target_releases = _target_releases_from_git(repo_dir, tags, series, scope["key"], target_shas_in_series)

    cand: dict[bytes, dict[str, Any]] = {}
    for src, at, subject, rels in parsed_records:
        base = {"tag": "", "at": at, "subject": subject}
        for note in dict.fromkeys(rels["stable_cc"]):
            _add(cand, src, commit_relations.KIND_STABLE_CC, note, None, series, base)
        for pfx, _ in rels["fixes"]:
            full = pfx if len(pfx) == 40 else resolved.get(pfx.lower())
            if full and full in target_releases:
                _add(cand, src, commit_relations.KIND_FIXES, "", (full, target_releases[full]), series, base)

    if not cand:
        return _result(series, scope, {}, set(), lambda e: "", "git")

    cand_hex = {c.hex() for c in cand}
    ranges_by_tag = {r["tag"]: r for r in release_ranges(tags)}
    later_segs: list[tuple[str, str]] = []
    for idx, t in enumerate(later_ordered):
        sb = (ranges_by_tag.get(t) or {}).get("segment_base")
        if sb and sb in tags and release_service.release_key(sb) >= scope["key"]:
            base_tag = sb
        else:
            base_tag = later_ordered[idx - 1] if idx > 0 else series
        later_segs.append((base_tag, t))

    def walk_later(item: tuple[str, str]) -> tuple[str, list[bytes]]:
        base_tag, t = item
        out = _git_stdout(["rev-list", tags[t], f"^{tags[base_tag]}"], repo_dir=repo_dir, timeout=180)
        return t, [bytes.fromhex(line) for line in out.splitlines() if line in cand_hex]

    with ThreadPoolExecutor(max_workers=release_service.RANGE_WORKERS) as pool:
        for t, hits in pool.map(walk_later, later_segs):
            for sha_b in hits:
                if sha_b in cand and not cand[sha_b]["tag"]:
                    cand[sha_b]["tag"] = t

    cand = {k: v for k, v in cand.items() if v["tag"]}
    if not cand:
        return _result(series, scope, {}, set(), lambda e: "", "git")

    if progress:
        progress(3, 4, f"{len(cand):,} candidates: checking backports")
    last_stable_sha = tags[scope["last_stable"]]
    up_out = _git_stdout(
        ["log", "--no-merges", "--no-color", "--format=%x1e%H%x1f%B", last_stable_sha, f"^{series_sha}"],
        repo_dir=repo_dir,
        timeout=180,
    )
    p8_to_cands: dict[str, list[bytes]] = {}
    for c in cand:
        p8_to_cands.setdefault(c.hex()[:8], []).append(c)

    backported: set[bytes] = set()
    for chunk in up_out.split("\x1e"):
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f", 1)
        if len(parts) < 2:
            continue
        own_sha, body = parts[0].strip(), parts[1]
        for u in parse_relations(body)["upstream"]:
            if own_sha and own_sha.startswith(u):
                continue
            if len(u) == 40:
                ub = bytes.fromhex(u)
                if ub in cand:
                    backported.add(ub)
            elif len(u) >= 8:
                for c in p8_to_cands.get(u[:8], ()):
                    if c.hex().startswith(u):
                        backported.add(c)
            else:
                for c in cand:
                    if c.hex().startswith(u):
                        backported.add(c)

    return _result(series, scope, cand, backported, lambda e: e["tag"], "git")


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
    in_later = (
        {sha: tag_idx for sha, tag_idx in first_tag.scan([Eq("chain", 0), In("tag_idx", sorted(later_mainline))],
                                                          columns=("sha", "tag_idx"))}
        if later_mainline else {}
    )
    if not in_later:
        return _result(series, scope, {}, set(), lambda e: "", "index")
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
    None when a query fails."""
    from db_engine.spec import Join, Value
    from webapp.backend.database.helpers import (
        get_active_network_id,
        get_active_repo_id,
    )
    from webapp.backend.database.pool import get_reader
    k_fix, k_cc, k_up = commit_relations.KIND_FIXES, commit_relations.KIND_STABLE_CC, commit_relations.KIND_UPSTREAM
    tags = release_service.tag_commits()
    key = scope["key"]
    stable_set = set(scope["stable_tags"])
    try:
        with get_reader() as cur:
            repo_id = get_active_repo_id(cur)
            network_id = get_active_network_id(cur)
            if repo_id is None or repo_id <= 0 or network_id is None:
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
            resolved = resolve_prefixes(release_service.get_repo_dir(), unresolved) if unresolved else {}

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
            stable_sids_list = sorted(stable_sids)
            if cand and stable_sids_list:
                srcs = list(cand)
                for sid_i in range(0, len(stable_sids_list), 500):
                    sid_part = stable_sids_list[sid_i:sid_i + 500]
                    for i in range(0, len(srcs), 500):
                        part = srcs[i:i + 500]
                        for r in cur.select_rows(
                            ("m_commit_relation", "r"),
                            {"target_hash": "r.target_hash"},
                            {"r.kind": k_up, "c.network_id": network_id, "r.target_hash": part,
                             "cr.segment_id": sid_part},
                            joins=[
                                Join(("m_commit", "c"), {"c.commit_id": "r.commit_id"}),
                                Join(("m_commit_release", "cr"), {
                                    "cr.repo_id": Value(repo_id), "cr.commit_hash": "c.commit_hash",
                                    "cr.chain": Value(release_service.DB_CHAIN_STABLE),
                                }),
                            ],
                        ):
                            backported.add(bytes(r["target_hash"]))

                p8_to_cands: dict[str, list[bytes]] = {}
                for c in cand:
                    p8_to_cands.setdefault(c.hex()[:8], []).append(c)

                for sid_i in range(0, len(stable_sids_list), 500):
                    sid_part = stable_sids_list[sid_i:sid_i + 500]
                    for r in cur.select_rows(
                        ("m_commit_relation", "r"),
                        {"target_prefix": "r.target_prefix"},
                        {"r.kind": k_up, "r.target_hash": zero, "c.network_id": network_id,
                         "cr.segment_id": sid_part},
                        joins=[
                            Join(("m_commit", "c"), {"c.commit_id": "r.commit_id"}),
                            Join(("m_commit_release", "cr"), {
                                "cr.repo_id": Value(repo_id), "cr.commit_hash": "c.commit_hash",
                                "cr.chain": Value(release_service.DB_CHAIN_STABLE),
                            }),
                        ],
                    ):
                        pre = _textv(r["target_prefix"]).lower()
                        if pre:
                            matching = (
                                [c for c in p8_to_cands.get(pre[:8], ()) if c.hex().startswith(pre)]
                                if len(pre) >= 8
                                else [c for c in cand if c.hex().startswith(pre)]
                            )
                            for c in matching:
                                backported.add(c)
    except Exception as exc:  # noqa: BLE001 - the SQLite path answers
        logger.warning("stable report for %s from the database failed: %s", series, exc)
        return None
    return _result(series, scope, cand, backported, lambda e: e["tag"], "db")


_SUM_COLS = (
    "total_commits",
    "backport_commits",
    "fixes_tag_commits",
    "cc_stable_commits",
    "cve_linked_commits",
    "lines_added",
    "lines_removed",
    "same_cycle_fixes_count",
    "cross_release_fixes_count",
    "resolved_fixes_count",
)


def _load_cve_commit_hashes() -> set[bytes] | None:
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        from webapp.backend.database.pool import get_reader

        with get_reader() as reader:
            repo_id = get_active_repo_id(reader)
            if repo_id is None or repo_id <= 0 or not reader.existing_tables(["m_cve_commit"]):
                return None
            out: set[bytes] = set()
            short_prefixes: list[str] = []
            for r in reader.select_rows("m_cve_commit", ["commit_hash"], {"role": 1}, distinct=True):
                h = _textv(r.get("commit_hash")).strip().lower()
                if _FULL_SHA.match(h):
                    out.add(bytes.fromhex(h))
                elif h:
                    short_prefixes.append(h)
            if short_prefixes and reader.existing_tables(["m_cve_commit_hash"]):
                for i in range(0, len(short_prefixes), 1000):
                    part = short_prefixes[i:i + 1000]
                    for r in reader.select_rows(
                        "m_cve_commit_hash", ["commit_hash"], {"repo_id": repo_id, "prefix": part}
                    ):
                        bh = r.get("commit_hash")
                        if bh:
                            out.add(bytes(bh))
            return out
    except Exception:  # noqa: BLE001
        return None


def stable_summary(series: str, from_ref: str | None = None, to_ref: str | None = None) -> dict[str, Any]:
    """Aggregated stable-series delta metrics over `(from_ref, to_ref]` on `stable:<series>`.

    Reads pre-parsed `insights_stable_version_delta` via `ReadIntents.stable_aggregated_metrics(...)` and
    computes live Git boundary fallback (`extract_release_deltas_from_git`) only for unparsed boundary
    segments, merging additive counters and weighted `avg_fix_latency_days`.
    """
    tags = release_service.tag_commits()
    p = release_service.parse_tag(series)
    if p is None or not p["final"] or series not in tags:
        return {"series": series, "available": False, "reason": f"{series} is not a release of this repository"}
    stable_tags = sorted(
        (t for t in tags if (q := release_service.parse_tag(t)) and q["series"] == p["series"] and q["kind"] == "stable"),
        key=release_service.release_key,
    )
    if not stable_tags:
        return {"series": series, "available": False, "reason": f"The repository has no stable tags of {series}.y"}

    from_tag = from_ref if from_ref is not None else series
    to_tag = to_ref if to_ref is not None else stable_tags[-1]
    if from_tag != series and from_tag not in stable_tags:
        raise HTTPException(status_code=400, detail=f"Invalid from_ref for series {series}: {from_ref}")
    if to_tag != series and to_tag not in stable_tags:
        raise HTTPException(status_code=400, detail=f"Invalid to_ref for series {series}: {to_ref}")

    from_key = release_service.release_key(from_tag)
    to_key = release_service.release_key(to_tag)
    if from_key > to_key:
        raise HTTPException(status_code=400, detail=f"from_ref {from_tag} is after to_ref {to_tag}")

    chain = [series, *stable_tags]
    window_segs: list[tuple[str, str]] = [
        (chain[i - 1], chain[i])
        for i in range(1, len(chain))
        if from_key < release_service.release_key(chain[i]) <= to_key
    ]
    if not window_segs:
        return {
            "series": series, "available": True, "from_ref": from_tag, "to_ref": to_tag,
            "source": "database", "releases": 0, "db_releases": 0, "git_releases": 0,
            "commits": 0, "backports": 0, "fixes": 0, "cc_stable": 0, "cve_commits": 0,
            "lines_added": 0, "lines_removed": 0, "same_cycle_fixes": 0, "cross_release_fixes": 0,
            "avg_fix_latency_days": None,
        }

    track = series_track_for(stable_tags[0])
    from_ord = 0 if from_tag == series else series_ord_for(from_tag)
    to_ord = series_ord_for(to_tag)
    try:
        td = release_service.tag_dates()
    except Exception:  # noqa: BLE001
        td = {}

    covered_tags: list[str] = []
    missing_segs: list[tuple[str, str]] = list(window_segs)
    acc: dict[str, int] = {c: 0 for c in _SUM_COLS}
    lat_sum = 0.0
    lat_cnt = 0

    try:
        from webapp.backend.database.helpers import get_active_repo_id
        from webapp.backend.database.pool import get_reader

        with get_reader() as reader:
            repo_id = get_active_repo_id(reader)
            if repo_id is not None and repo_id > 0 and hasattr(reader, "stable_version_deltas"):
                v_rows = reader.stable_version_deltas(
                    track, from_ord=from_ord, to_ord=to_ord, base_mainline_tag=series
                )
                by_tag: dict[str, tuple[str, int]] = {}
                stale: set[str] = set()
                for r in v_rows:
                    vt = _textv(r.get("version_tag"))
                    if not vt:
                        continue
                    rts = int(r.get("release_ts") or 0)
                    if rts > 0 and td.get(vt, 0) > 0 and rts != int(td[vt]):
                        stale.add(vt)
                        continue
                    if vt not in by_tag:
                        by_tag[vt] = (_textv(r.get("parent_tag")), rts)
                for vt in stale:
                    by_tag.pop(vt, None)

                covered_tags = [
                    curr_t for prev_t, curr_t in window_segs
                    if curr_t in by_tag and by_tag[curr_t][0] == prev_t
                ]
                covered_set = set(covered_tags)
                missing_segs = [(prev_t, curr_t) for prev_t, curr_t in window_segs if curr_t not in covered_set]
                if covered_tags and hasattr(reader, "stable_aggregated_metrics"):
                    agg_rows = reader.stable_aggregated_metrics(
                        track,
                        from_ord=from_ord,
                        to_ord=to_ord,
                        base_mainline_tag=series,
                        tags=covered_tags if missing_segs else None,
                        by_subsystem=False,
                    )
                    for row in agg_rows:
                        for c in _SUM_COLS:
                            acc[c] += int(row.get(c) or 0)
                        if "sum_fix_latency_days" in row:
                            lat_sum += float(row.get("sum_fix_latency_days") or 0.0)
                            lat_cnt += int(row.get("resolved_fixes_count") or 0)
                        elif row.get("avg_fix_latency_days") is not None:
                            w = int(row.get("resolved_fixes_count") or row.get("fixes_tag_commits") or 0)
                            if w > 0:
                                lat_sum += float(row["avg_fix_latency_days"]) * w
                                lat_cnt += w
    except Exception as exc:  # noqa: BLE001
        logger.debug("stable_summary DB delta scan failed for %s: %s", series, exc)
        covered_tags = []
        missing_segs = list(window_segs)
        acc = {c: 0 for c in _SUM_COLS}
        lat_sum = 0.0
        lat_cnt = 0

    if missing_segs:
        from webapp.backend.services.insights_service import _load_maintainers

        repo_dir = release_service.get_repo_dir()
        cfg = get_active_insights_config()
        catchall = str(cfg.get("catchall_section", "THE REST") or "")
        maint_file = str(cfg.get("maintainers_file", "MAINTAINERS") or "MAINTAINERS")
        sections, matcher = _load_maintainers(to_tag, tags[to_tag])
        cve_hashes = _load_cve_commit_hashes()
        cve_sig = len(cve_hashes) if cve_hashes else 0
        for prev_t, curr_t in missing_segs:
            curr_sha = tags.get(curr_t)
            prev_sha = tags.get(prev_t)
            if not curr_sha:
                continue
            seg_ck = f"{get_active_repo_name()}|stable-seg-delta|1|{curr_t}|{curr_sha}|{prev_sha or '-'}|{cve_sig}"
            seg_rows = _DISK.get(seg_ck)
            if seg_rows is None:
                extracted = extract_release_deltas_from_git(
                    repo_dir,
                    curr_t,
                    curr_sha,
                    prev_t,
                    prev_sha,
                    sections=sections,
                    matcher=matcher,
                    catchall_section=catchall,
                    cve_commit_hashes=cve_hashes,
                    sha_to_release=None,
                    compute_pulls=False,
                    insights_cfg=cfg,
                    maintainers_file=maint_file,
                )
                seg_rows = [
                    row for row in (extracted.get("stable_deltas") or ())
                    if _textv(row.get("subsystem_name"))
                ]
                _DISK.put(seg_ck, seg_rows)
            for row in seg_rows:
                for c in _SUM_COLS:
                    acc[c] += int(row.get(c) or 0)
                lat_sum += float(row.get("sum_fix_latency_days") or 0.0)
                lat_cnt += int(row.get("resolved_fixes_count") or 0)

    source = "database" if not missing_segs else ("mixed" if covered_tags else "git")
    return {
        "series": series,
        "available": True,
        "from_ref": from_tag,
        "to_ref": to_tag,
        "source": source,
        "releases": len(window_segs),
        "db_releases": len(covered_tags),
        "git_releases": len(missing_segs),
        "commits": acc["total_commits"],
        "backports": acc["backport_commits"],
        "fixes": acc["fixes_tag_commits"],
        "cc_stable": acc["cc_stable_commits"],
        "cve_commits": acc["cve_linked_commits"],
        "lines_added": acc["lines_added"],
        "lines_removed": acc["lines_removed"],
        "same_cycle_fixes": acc["same_cycle_fixes_count"],
        "cross_release_fixes": acc["cross_release_fixes_count"],
        "avg_fix_latency_days": round(lat_sum / lat_cnt, 2) if lat_cnt > 0 else None,
    }


def _safe_stable_summary(series: str) -> dict[str, Any] | None:
    try:
        res = stable_summary(series)
        return res if res.get("available") else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("stable_summary attachment skipped for %s: %s", series, exc)
        return None


def _run_missing_job(series: str, progress: Any, *, use_db: bool, mode: str) -> dict[str, Any]:
    if mode == "db":
        res = build_report(series, progress, use_db=True)
    elif mode == "sqlite":
        scope = series_scope(series)
        if not scope["available"]:
            return {"series": series, "available": False, "reason": scope["reason"], "candidates": []}
        res = _report_sqlite(series, scope, progress)
    else:
        scope = series_scope(series)
        if not scope["available"]:
            return {"series": series, "available": False, "reason": scope["reason"], "candidates": []}
        res = _report_git_scoped(series, scope, progress)
    if res.get("available"):
        res["summary"] = _safe_stable_summary(series)
    return res


def missing_report(series: str, wait: float = 0.0) -> dict[str, Any]:
    """Job state of the report ({state, progress, result?}). Checks availability first, uses DB when
    covered, existing SQLite indexes when current, or fast scoped Git fallback without building full indexes."""
    scope = series_scope(series)
    if not scope["available"]:
        return {"state": "done", "result": {"series": series, "available": False, "reason": scope["reason"], "candidates": []}}

    use_db = db_scope_covered(scope)
    cache_store: Any = _DISK
    if use_db:
        mode = "db"
        cache_key = (f"{get_active_repo_name()}|{series}|{REPORT_FORMAT}|db|{commit_relations.relations_token()}|"
                     f"{release_service.release_token()}")
    elif _indexes_ready():
        rel_state = release_service.ensure_release_index(require_sqlite=True)
        idx_state = commit_relations.ensure_index()
        for idx_s in (rel_state, idx_state):
            st = idx_s.get("state")
            if st == "failed":
                return {"state": "failed", "error": idx_s.get("error") or "index build failed",
                        "progress": idx_s.get("progress") or {}}
            if st != "done":
                return {"state": "building", "progress": idx_s.get("progress") or {}, "waiting_for": "indexes"}
        if rel_state.get("refreshing") or idx_state.get("refreshing"):
            mode = "git"
            cache_store = None
            cache_key = f"{get_active_repo_name()}|{series}|{REPORT_FORMAT}|refreshing"
        else:
            mode = "sqlite"
            built = (
                commit_relations.relation_index.meta("built_at") or "0",
                str(release_service.release_index.stats().get("built_at") or 0),
            )
            cache_key = f"{get_active_repo_name()}|{series}|{REPORT_FORMAT}|{built[0]}|{built[1]}"
    else:
        mode = "git"
        tags = release_service.tag_commits()
        later_sig = hashlib.sha1(
            "|".join(f"{t}:{tags.get(t, '')}" for t in sorted(scope["later"])).encode("utf-8")
        ).hexdigest()[:12]
        cache_key = (
            f"{get_active_repo_name()}|{series}|{REPORT_FORMAT}|git|"
            f"{tags.get(series, '')}|{tags.get(scope['last_stable'], '')}|{later_sig}"
        )

    key = f"stable-missing:{cache_key}"
    state = jobs.submit(
        key,
        lambda progress: _run_missing_job(series, progress, use_db=use_db, mode=mode),
        cache=cache_store,
        cache_key=cache_key if cache_store is not None else None,
    )
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(key, wait) or state
    return state


def stable_series() -> list[dict[str, Any]]:
    """Releases that have stable tags: [{series, stable_tags, last}] newest first."""
    tags = release_service.tag_commits()
    groups: dict[tuple, list[str]] = {}
    for t in tags:
        p = release_service.parse_tag(t)
        if p and p["kind"] == "stable":
            groups.setdefault(p["series"], []).append(t)
    out: list[dict[str, Any]] = []
    for v in groups.values():
        s = release_service._series_tag_for(v[0], tags)
        if s and s in tags:
            out.append({"series": s, "stable_tags": len(v), "last": max(v, key=release_service.release_key)})
    out.sort(key=lambda x: release_service.release_key(x["series"]), reverse=True)
    return out
