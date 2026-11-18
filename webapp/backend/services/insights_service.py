"""webapp/backend/services/insights_service.py - Release reports (webapp/API.md section 4.10):
release statistics (H6), merge windows (H5) and subsystem health (H7).

Every report is built from Git through the release index's per-release passes (section 2.17) and
cached on disk per (repository, release commit, parameters): a released tag never changes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from db_engine.spec import Agg, Join, NotExists, Value, gt
from parser.git_ast.git_types import CommitRole
from parser.git_ast.release_order import release_ranges
from webapp.backend.config import RepoScopedCache, get_active_insights_config, get_active_repo_name, get_repo_dir
from webapp.backend.services import release_service as rs
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)


def _insights_cfg() -> dict[str, Any]:
    return get_active_insights_config()

REPORT_FORMAT = 2  # 2: source, first-timers only for mainline, identities once per commit
MW_FORMAT = 5  # merge-window: 5 resolves sender across body variants, parent tip committers, and tree fallbacks
HEALTH_FORMAT = 3  # subsystem health: 3 carries `source` (database or git)
_DISK = DiskCache("insights", max_bytes=256 << 20)
_DOMAINS_FILE = Path(__file__).resolve().parents[1] / "data" / "email_domains.json"
_FULL = re.compile(r"^[0-9a-f]{40}$")
TOP = 25


def _git(args: list[str], timeout: float = 120.0) -> str:
    try:
        out = subprocess.run(["git", "-C", str(get_repo_dir()), *args], capture_output=True, text=True, timeout=timeout,
                             errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git {args[0]} failed: {exc}") from exc
    if out.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()[:200]}")
    return out.stdout


# ------------------------------------------------------------------------------- identities and employers

def identity(name: str, email: str) -> str:
    """One developer under several (name, email) aliases: the Developers directory's key (section 4.5)."""
    from webapp.backend.services.maintainer_service import _identity_key
    return _identity_key(name, email)


_DOMAINS: dict[str, Any] | None = None


def employer(email: str) -> str:
    """Organization of an address from webapp/backend/data/email_domains.json (subdomains included)."""
    global _DOMAINS
    if _DOMAINS is None:
        try:
            _DOMAINS = json.loads(_DOMAINS_FILE.read_text())
        except (OSError, ValueError):
            _DOMAINS = {"domains": {}, "personal": []}
    domain = (email or "").rsplit("@", 1)[-1].strip().lower().strip(">")
    if not domain or "@" not in (email or ""):
        return "(Individual / unknown)"
    if domain in set(_DOMAINS.get("personal", [])):
        return "(Individual / unknown)"
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        org = _DOMAINS.get("domains", {}).get(".".join(parts[i:]))
        if org:
            return org
    return f"({domain})"


def _job(key: str, build: Callable[[Callable], Any], cache_key: str, wait: float) -> dict[str, Any]:
    state = jobs.submit(key, build, cache=_DISK, cache_key=cache_key)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(key, wait) or state
    return state


def _release_pass(tag: str) -> list[dict[str, Any]]:
    # Inline, not a sub-job: a report already runs on a worker (section 2.15)
    return rs.release_commits_now(tag)


# ------------------------------------------------------------------------------- first-time authors

def author_first_seen(progress: Callable | None = None, family: str = rs.KERNEL_FAMILY, *, head: str | None = None) -> dict[str, int]:
    """identity -> time of its first commit on the mainline chain of a tag family (`""`: the kernel scheme; one
    pass over history, cached per head)."""
    if head is None:
        releases = [t for t in rs.releases("mainline") if rs.family_of(t) == family]
        if not releases:
            return {}
        head = rs.tag_commits()[releases[-1]]
    cache_key = f"{get_active_repo_name()}|first-seen|{REPORT_FORMAT}|{head}"
    hit = _DISK.get(cache_key)
    if hit is not None:
        return hit
    if progress:
        progress(None, None, "first commit of every author")
    out = _git(["log", "--no-merges", "--format=%an%x1f%ae%x1f%at", head], timeout=900)
    first: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3 or not parts[2].isdigit():
            continue
        key = identity(parts[0], parts[1])
        t = int(parts[2])
        if key not in first or t < first[key]:
            first[key] = t
    _DISK.put(cache_key, first)
    return first


# ------------------------------------------------------------------------------- H6 release stats
# Two sources, one report (_report): the parser's release statistics (m_release_segment, m_release_person,
# m_person_first_release; core/API.md section 10), read when they cover the release exactly as Git cuts it
# today, else the release's commit pass from Git (a background job, the fallback for tags not summarized yet).

_ROLE_NAMES = {CommitRole.REVIEWED_BY: "reviewed_by", CommitRole.TESTED_BY: "tested_by", CommitRole.REPORTED_BY: "reported_by",
               CommitRole.SIGNED_OFF_BY: "signed_off_by", CommitRole.ACKED_BY: "acked_by"}
_RANGES: RepoScopedCache = RepoScopedCache(maxsize=4)


def _top(counter: Counter, names: dict[str, tuple[str, str]], total: int) -> list[dict[str, Any]]:
    # Ties in identity order, so both sources list the same people
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    return [{"person": names.get(k, (k, ""))[0], "email": names.get(k, ("", ""))[1], "count": n,
             "share": round(n / total, 4) if total else 0} for k, n in top]


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")


def _tag_meta(tag: str, *, latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """{prev, days, rc_count} of a release: tag metadata, read from Git (milliseconds)."""
    if tag == rs.LATEST_RELEASE:
        info = latest or rs.latest_info()
        prev_name = info["prev"]
        prev_sha = info["prev_sha"]
        head_sha = info["sha"]
        days = None
        if prev_sha:
            try:
                at_head = int((rs._git(["log", "-1", "--format=%ct", head_sha]) or "").strip())
                at_prev = int((rs._git(["log", "-1", "--format=%ct", prev_sha]) or "").strip())
                days = round((at_head - at_prev) / 86400)
            except (ValueError, KeyError):
                days = None
        return {"prev": prev_name, "days": days, "rc_count": info["rc_count"]}
    prev_name = rs.previous(tag)
    tags = rs.tag_commits()
    days = None
    if prev_name:
        td = rs.tag_dates()
        if tag in td and prev_name in td and td[tag] and td[prev_name]:
            days = round((td[tag] - td[prev_name]) / 86400)
        else:
            try:
                at = {t: int((rs._git(["log", "-1", "--format=%ct", tags[t]]) or "").strip()) for t in (tag, prev_name)}
                days = round((at[tag] - at[prev_name]) / 86400)
            except (ValueError, KeyError):
                days = None
    series = rs.parse_tag(tag)["series"]
    rc_count = sum(1 for t in tags if (p := rs.parse_tag(t)) and p["series"] == series and p["pre"])
    return {"prev": prev_name, "days": days, "rc_count": rc_count}


def _db_authors_before(tag_or_prev: str | None, family: str) -> set[str] | None:
    """When tag_or_prev is a release tag, checks if m_release_segment covers all mainline release tags of
    family at or before tag_or_prev at release_stats.FORMAT. If so, returns prior author identities from DB."""
    if not tag_or_prev:
        return set()
    tags = rs.tag_commits()
    if tag_or_prev not in tags:
        return None
    target_key = rs.release_key(tag_or_prev)
    prior_tags = [
        t for t in rs.releases("mainline")
        if (p := rs.parse_tag(t)) and p["family"] == family and rs.release_key(t) <= target_key
    ]
    if not prior_tags:
        return set()
    try:
        from parser.git_ast import release_stats
        from webapp.backend.database.helpers import get_active_repo_id
        from webapp.backend.database.pool import get_reader
        from webapp.backend.services.maintainer_service import MaintainerService

        with get_reader() as cur:
            repo_id = get_active_repo_id(cur)
            stored_rows = cur.release_segments(repo_id=repo_id, tags=prior_tags)
            stored_by_tag: dict[str, Any] = {_text(r["tag"]): r for r in stored_rows}
            sids: list[int] = []
            for t in prior_tags:
                row = stored_by_tag.get(t)
                if not row:
                    return None
                if int(row["chain"]) != release_stats.CHAIN_MAINLINE:
                    return None
                if int(row["format"]) != release_stats.FORMAT:
                    return None
                if bytes(row["tag_hash"]).hex() != tags.get(t):
                    return None
                sids.append(int(row["segment_id"]))

            pids: set[int] = set()
            for i in range(0, len(sids), 1000):
                chunk = sids[i:i + 1000]
                first_rows = cur.select_rows(
                    "m_person_first_release",
                    ["person_id"],
                    {"repo_id": repo_id, "segment_id": chunk},
                )
                for fr in first_rows:
                    pids.add(int(fr["person_id"]))

            if not pids:
                return set()

            pid_to_key, _ = MaintainerService._identity_index(cur)
            missing = [p for p in pids if p not in pid_to_key]
            missing_persons: dict[int, tuple[str, str]] = {}
            for i in range(0, len(missing), 1000):
                part = missing[i:i + 1000]
                for pr in cur.select_rows("m_maintainer_person", ["person_id", "name", "email"], {"person_id": part}):
                    missing_persons[int(pr["person_id"])] = (_text(pr["name"]), _text(pr["email"]))

            prior_author_keys: set[str] = set()
            for p in pids:
                if p in pid_to_key:
                    prior_author_keys.add(pid_to_key[p])
                elif p in missing_persons:
                    prior_author_keys.add(identity(*missing_persons[p]))
            return prior_author_keys
    except Exception as exc:  # noqa: BLE001
        logger.debug("DB author history before %s unavailable: %s", tag_or_prev, exc)
        return None


def _report(tag: str, source: str, *, commits: int, merges: int, diffstat: tuple[int | None, int, int],
            authors: Counter, roles: dict[str, Counter], names: dict[str, tuple[str, str]],
            employers: dict[str, dict[str, Any]], first_timers: list[str] | None,
            latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """The release stats answer (webapp/API.md section 4.10) from identity-level counts of either source.
    first_timers None: not applicable (a stable update's commits are backports of mainline commits)."""
    meta = _tag_meta(tag, latest=latest)
    files, added, removed = diffstat
    out = {
        "release": tag, "prev": meta["prev"], "source": source, "commits": commits, "merges": merges, "authors": len(authors),
        "first_time_authors": None if first_timers is None else len(first_timers),
        "first_timers": [{"person": names[k][0], "email": names[k][1], "commits": authors[k]}
                         for k in sorted(first_timers or [], key=lambda k: (-authors[k], k))[:200]],
        "files_changed": files, "lines_added": added, "lines_removed": removed,
        "days": meta["days"], "rc_count": meta["rc_count"],
        "top": {"authors": _top(authors, names, commits), "reviewers": _top(roles["reviewed_by"], names, commits),
                "testers": _top(roles["tested_by"], names, commits), "reporters": _top(roles["reported_by"], names, commits),
                "ackers": _top(roles["acked_by"], names, commits), "signers": _top(roles["signed_off_by"], names, commits)},
        "employers": sorted(({"org": o, "commits": v["commits"], "authors": len(v["authors"])} for o, v in employers.items()),
                            key=lambda e: (-e["commits"], e["org"]))[:40],
    }
    if tag == rs.LATEST_RELEASE:
        info = latest or rs.latest_info()
        out["branch"] = info["branch"]
        out["head"] = info["sha"]
    return out


def _new_employers() -> dict[str, dict[str, Any]]:
    return defaultdict(lambda: {"commits": 0, "authors": set()})


def compute_release_stats(tag: str, progress: Callable | None = None, *, latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Release stats from Git: the release's commit pass, `git rev-list --count --merges`, `git diff --shortstat`."""
    info = (latest or rs.latest_info()) if tag == rs.LATEST_RELEASE else None
    if info is not None:
        prev, sha = info["prev_sha"], info["sha"]
        commits = rs.release_commits_now(tag, range_override=(prev, sha))
        mainline = True
        ml = rs.releases("mainline")
        family = rs.family_of(info["prev"]) if info["prev"] else (rs.family_of(ml[0]) if ml else rs.KERNEL_FAMILY)
        first_head: str | None = sha
    else:
        prev, sha = rs.range_of(tag)
        commits = _release_pass(tag)
        p = rs.parse_tag(tag)
        mainline = p["kind"] == "mainline"
        family = p["family"]
        first_head = None
    rng = [sha] + ([f"^{prev}"] if prev else [])
    merges = int((_git(["rev-list", "--count", "--merges", *rng]) or "0").strip() or 0)
    stat = _git(["diff", "--shortstat", prev, sha]) if prev else ""
    m_files = re.search(r"(\d+) files? changed", stat)
    m_add = re.search(r"(\d+) insertions?", stat)
    m_del = re.search(r"(\d+) deletions?", stat)
    prior_authors: set[str] | None = None
    first_seen: dict[str, int] = {}
    if mainline:
        prev_tag = info["prev"] if info is not None else rs.previous(tag)
        prior_authors = _db_authors_before(prev_tag, family) if prev_tag else set()
        if prior_authors is None:
            if info is not None and info.get("prev_sha"):
                first_seen = author_first_seen(progress, family, head=info["prev_sha"])
            else:
                first_seen = author_first_seen(progress, family, head=first_head)
    names: dict[str, tuple[str, str]] = {}
    authors: Counter = Counter()
    roles = {k: Counter() for k in _ROLE_NAMES.values()}
    employers = _new_employers()
    earliest: dict[str, int] = {}
    for c in commits:
        key = identity(c["author"], c["email"])
        names.setdefault(key, (c["author"], c["email"]))
        authors[key] += 1
        earliest[key] = min(earliest.get(key, c["date"]), c["date"])
        org = employer(c["email"])
        employers[org]["commits"] += 1
        employers[org]["authors"].add(key)
        for role, people in (c.get("trailers") or {}).items():
            if role not in roles:
                continue
            seen: set[str] = set()
            for name, email in people:
                k = identity(name, email)
                names.setdefault(k, (name, email))
                if (role == "signed_off_by" and k == key) or k in seen:
                    continue  # the author's own sign-off; an identity once per commit
                seen.add(k)
                roles[role][k] += 1
    if mainline:
        if prior_authors is not None:
            first_timers = [k for k in authors if k not in prior_authors]
        elif info is not None and info.get("prev_sha"):
            first_timers = [k for k in earliest if k not in first_seen or first_seen[k] >= earliest[k]]
        else:
            first_timers = [k for k, t in earliest.items() if k in first_seen and first_seen[k] >= t]
    else:
        first_timers = None
    return _report(tag, "git", commits=len(commits), merges=merges,
                   diffstat=(int(m_files.group(1)) if m_files else None, int(m_add.group(1)) if m_add else 0,
                             int(m_del.group(1)) if m_del else 0),
                   authors=authors, roles=roles, names=names, employers=employers, first_timers=first_timers,
                   latest=info)


def _release_range(tag: str) -> dict[str, Any] | None:
    """{range_base, kind, tags: {tag on the release's chain in range_base..tag: (segment base)}} from Git's tags
    (the chain of the release's tag family: a repository may tag several prefixed families)."""
    tags = rs.tag_commits()
    hit = _RANGES.get("ranges")
    if hit is None or hit[0] is not tags:
        all_ranges = release_ranges(tags)
        hit = (tags, {r["tag"]: r for r in all_ranges},
               {r["tag"]: (rs.release_key(r["tag"]), (p := rs.parse_tag(r["tag"]))["series"], p["family"]) for r in all_ranges})
        _RANGES["ranges"] = hit
    _, ranges, keys = hit
    r = ranges.get(tag)
    if r is None:
        return None
    lo = rs.release_key(r["range_base"]) if r["range_base"] else None
    hi, series, family = keys[tag]
    members = {t: x["segment_base"] for t, x in ranges.items()
               if x["kind"] == r["kind"] and keys[t][2] == family and keys[t][0] <= hi and (lo is None or keys[t][0] > lo)
               and (r["kind"] == "mainline" or keys[t][1] == series)}
    return {"range_base": r["range_base"], "kind": r["kind"], "tags": members}


def _release_commit_source(network_id: int | None, repo_id: int | None, segs: list[int]) -> tuple[Any, list[Join], list[Any]]:
    """(source, joins, where) of the non-merge commits of release segments `segs` in `m_commit` (`git log
    --no-merges`): `m_commit_release` rows joined to their commit, without a second parent."""
    return (
        ("m_commit_release", "cr"),
        [Join(("m_commit", "c"), {"c.network_id": Value(network_id), "c.commit_hash": "cr.commit_hash"})],
        [
            {"cr.repo_id": repo_id, "cr.segment_id": segs},
            NotExists(("m_commit_parent", "mp"), {"mp.commit_id": "c.commit_id"}, {"mp.parent_idx": gt(0)}),
        ],
    )


def db_release_stats(tag: str) -> dict[str, Any] | None:
    """Release stats from the parser's release statistics (read only), or None when they do not cover
    the release: tables missing, repository never parsed, a tag of the range not summarized, or summarized
    against other commits, bases or another format than Git and the parser agree on now."""
    if tag == rs.LATEST_RELEASE:
        return None
    from parser.git_ast.release_stats import FORMAT
    rng = _release_range(tag)
    if rng is None:
        return None
    tags = rs.tag_commits()
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        from webapp.backend.database.pool import get_reader
        from webapp.backend.services.maintainer_service import MaintainerService
        with get_reader() as cur:
            repo_id = get_active_repo_id(cur)
            if repo_id is None or repo_id <= 0:
                return None
            members = list(rng["tags"])
            seg = {r["tag"]: r for r in cur.release_segments(repo_id=repo_id, tags=members)}
            for t, base in rng["tags"].items():
                r = seg.get(t)
                if (r is None or bytes(r["tag_hash"]).hex() != tags[t] or _text(r["base_tag"]) != (base or "")
                        or bytes(r["base_hash"]).hex() != (tags[base] if base else "0" * 40) or int(r["format"]) != FORMAT):
                    return None
            own = seg[tag]
            if _text(own["diff_base_tag"]) != (rng["range_base"] or ""):
                return None
            ids = [int(r["segment_id"]) for r in seg.values()]
            counts = [
                (int(r["person_id"]), int(r["role_type"]), int(r["commits"]))
                for r in cur.release_person_counts(ids)
            ]
            pids = sorted({p for p, _, _ in counts})
            persons: dict[int, tuple[str, str]] = {}
            for i in range(0, len(pids), 1000):
                part = pids[i:i + 1000]
                persons.update({
                    int(r["person_id"]): (_text(r["name"]), _text(r["email"]))
                    for r in cur.select_rows("m_maintainer_person", ["person_id", "name", "email"], {"person_id": part})
                })
            pid_to_key, members_of = MaintainerService._identity_index(cur)
            key_of = {p: pid_to_key.get(p) or identity(*persons.get(p, ("", ""))) for p in pids}
            first_of: dict[str, tuple] = {}
            if rng["kind"] == "mainline":
                author_keys = {key_of[p] for p, role, _ in counts if role == int(CommitRole.AUTHOR)}
                aliases = sorted({a for k in author_keys for a in members_of.get(k, [])} | {p for p, role, _ in counts if role == int(CommitRole.AUTHOR)})
                firsts = [
                    (int(r["person_id"]), int(r["segment_id"]))
                    for r in cur.person_first_releases(repo_id, aliases)
                ]
                sids = sorted({s for _, s in firsts})
                seg_tag: dict[int, str] = {}
                for i in range(0, len(sids), 1000):
                    part = sids[i:i + 1000]
                    seg_tag.update({
                        int(r["segment_id"]): r["tag"]
                        for r in cur.release_segments(segment_ids=part)
                    })
                for p, s in firsts:
                    if s not in seg_tag:
                        continue
                    k = pid_to_key.get(p) or key_of.get(p)
                    rk = rs.release_key(seg_tag[s])
                    if k and (k not in first_of or rk < first_of[k]):
                        first_of[k] = rk
    except Exception as exc:  # noqa: BLE001 - no tables yet, database unreachable: Git answers
        logger.debug("release stats of %s not read from the database: %s", tag, exc)
        return None
    authors: Counter = Counter()
    roles = {k: Counter() for k in _ROLE_NAMES.values()}
    employers = _new_employers()
    weight: dict[int, int] = Counter()
    for p, role, n in counts:
        k = key_of[p]
        weight[p] += n
        if role == int(CommitRole.AUTHOR):
            authors[k] += n
            org = employer(persons.get(p, ("", ""))[1])
            employers[org]["commits"] += n
            employers[org]["authors"].add(k)
        elif (name := _ROLE_NAMES.get(CommitRole(role))) is not None:
            roles[name][k] += n
    names: dict[str, tuple[str, str]] = {}
    for p in sorted(weight, key=lambda p: (-weight[p], p)):  # an identity shows its busiest alias
        names.setdefault(key_of[p], persons.get(p, (str(p), "")))
    first_timers = None
    if rng["kind"] == "mainline":
        lo = rs.release_key(rng["range_base"]) if rng["range_base"] else None
        hi = rs.release_key(tag)
        first_timers = [k for k in authors if k in first_of and first_of[k] <= hi and (lo is None or first_of[k] > lo)]
    return _report(tag, "database", commits=sum(int(r["commits"]) for r in seg.values()),
                   merges=sum(int(r["merges"]) for r in seg.values()),
                   diffstat=(int(own["files_changed"]), int(own["lines_added"]), int(own["lines_removed"])) if rng["range_base"] else (None, 0, 0),
                   authors=authors, roles=roles, names=names, employers=employers, first_timers=first_timers)


def _check_release(tag: str) -> None:
    if tag == rs.LATEST_RELEASE:
        rs.latest_info()
        return
    p = rs.parse_tag(tag)
    if p is None or tag not in rs.tag_commits():
        raise HTTPException(status_code=404, detail=f"Not a release of this repository: {tag}")


def _stats_key(tag: str, *, latest: dict[str, Any] | None = None) -> str:
    if tag == rs.LATEST_RELEASE:
        info = latest or rs.latest_info()
        return f"{get_active_repo_name()}|stats|{REPORT_FORMAT}|latest|{info['prev_sha'] or '-'}|{info['sha']}|{info['rc_count']}"
    return f"{get_active_repo_name()}|stats|{REPORT_FORMAT}|{rs.tag_commits()[tag]}"


def release_stats(tag: str, wait: float = 0.0) -> dict[str, Any]:
    """Job state of a release's stats: from the parser's tables when they cover it (answered at once),
    else the Git job (202 while it builds, cached on disk per tag commit)."""
    _check_release(tag)
    if tag == rs.LATEST_RELEASE:
        info = rs.latest_info()
        ck = _stats_key(tag, latest=info)
        return _job(f"insights:{ck}", lambda p, _info=info: compute_release_stats(tag, p, latest=_info), ck, wait)
    hit = db_release_stats(tag)
    if hit is not None:
        return {"state": "done", "result": hit}
    ck = _stats_key(tag)
    return _job(f"insights:{ck}", lambda p: compute_release_stats(tag, p), ck, wait)


def release_stats_series(tags: list[str]) -> list[dict[str, Any]]:
    """Trend data of several releases: the parser's tables, else cached Git statistics only (no build:
    missing ones are queued)."""
    out = []
    for tag in tags[:40]:
        if tag not in rs.tag_commits():
            continue
        ck = _stats_key(tag)
        hit = _DISK.get(ck)
        if hit is None and rs.parse_tag(tag):
            hit = db_release_stats(tag)
            if hit is not None:
                _DISK.put(ck, hit)
        if hit is None:
            release_stats(tag)
            out.append({"release": tag, "pending": True})
            continue
        out.append({"release": tag, "commits": hit["commits"], "authors": hit["authors"],
                    "first_time_authors": hit["first_time_authors"], "days": hit["days"]})
    return out


# ------------------------------------------------------------------------------- H5 merge window

_PULL_RE = (
    re.compile(r"^Merge (?:tag|branch) '([^']+)' of (\S+)"),
    re.compile(r"^Merge (?:branch )?(?:'([^']+)' )?(?:of )?((?:git|https?|ssh)://\S+)"),
    re.compile(r"^Merge (?:remote-tracking )?(?:tag|branch) '([^']+)'(?: into \S+)?$"),
)


def normalize_url(url: str) -> str:
    u = (url or "").strip().rstrip("/").lower()
    u = re.sub(r"^(git|https?|ssh)://", "", u)
    u = re.sub(r"\.git$", "", u)
    u = u.replace("git.kernel.org/pub/scm/", "git.kernel.org/")
    return u


def parse_pull(subject: str) -> dict[str, str]:
    """{ref, url} of a merge subject ("Merge tag 'x' of git://…", "Merge branch 'y' of …", "Merge git://…")."""
    s = subject or ""
    m = _PULL_RE[0].match(s)
    if m:
        return {"ref": m.group(1), "url": m.group(2)}
    m = _PULL_RE[1].match(s)
    if m:
        url = m.group(2)
        ref = m.group(1) or ""
        tail = s[m.end():].strip()
        if not ref and tail:
            ref = tail.split()[0].strip("'")
        return {"ref": ref, "url": url}
    m = _PULL_RE[2].match(s)
    if m:
        return {"ref": m.group(1), "url": ""}
    return {"ref": "", "url": ""}


def _tree_index(tag: str) -> dict[str, dict[str, Any]]:
    """normalized T: tree url -> {sec_id, name} from the release's MAINTAINERS (best effort)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from webapp.backend.services.maintainer_service import MaintainerService
        sections, _ = MaintainerService().get_maintainer_data(tag)
    except Exception as exc:  # noqa: BLE001 - an unparsed release has no MAINTAINERS rows
        logger.debug("MAINTAINERS of %s unavailable: %s", tag, exc)
        return out
    owners: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sec in sections:
        for tree in re.split(r"[\n;]", sec.scm_tree or ""):
            tree = re.sub(r"^git\s+", "", tree.strip())
            url = (tree.split()[0] if tree else "").rstrip(",.;")
            if "://" not in url:
                continue  # "quilt …", "hg …": not a git tree a pull comes from
            entry = {"sec_id": sec.sec_id, "name": sec.name}
            norm = normalize_url(url)
            out.setdefault(norm, entry)
            if owner_of(norm) and entry not in owners[owner_of(norm)]:
                owners[owner_of(norm)].append(entry)
    # A tree renamed since MAINTAINERS was written (linux-2.6-kgdb vs kgdb) still matches through its
    # owner's directory when that owner maintains a single tree
    for own, entries in owners.items():
        if len(entries) == 1:
            out.setdefault(f"owner:{own}", entries[0])
    return out


def owner_of(norm_url: str) -> str:
    """`git.kernel.org/linux/kernel/git/jwessel/kgdb` -> `git.kernel.org/linux/kernel/git/jwessel`."""
    m = re.match(r"(git\.kernel\.org/.*/git/[^/]+)/", norm_url + "/")
    return m.group(1) if m and "/" in norm_url[len(m.group(1)):] else ""


def match_tree(trees: dict[str, dict[str, Any]], url: str) -> dict[str, Any] | None:
    norm = normalize_url(url)
    return trees.get(norm) or (trees.get(f"owner:{owner_of(norm)}") if owner_of(norm) else None)


_REF_STOPWORDS = frozenset({
    "for", "to", "from", "of", "in", "on", "linus", "torvalds", "upstream", "misc", "linux", "next",
    "urgent", "fixes", "fix", "updates", "update", "changes", "change", "cleanup", "cleanups",
    "master", "main", "head", "devel", "dev", "work", "tag", "tags", "branch", "branches",
    "merge", "pull", "pulls", "request", "the", "and", "stable", "testing", "current", "baseline",
})
_SEC_STOPWORDS = frozenset({
    "driver", "drivers", "subsystem", "support", "linux", "kernel", "the", "and", "for", "with", "common", "core",
})
_SIGNED_OFF_RE = re.compile(r"^Signed-off-by:\s*(.+?)\s*(?:<([^>\n]+)>)?\s*$", re.M | re.I)


def _norm_person_name(name: str) -> str:
    from parser.git_ast.identity import clean_name
    from parser.maintainer_ast.maintainer_types import clean_identity_text

    return clean_name(clean_identity_text(name)).casefold()


def _strip_paren_name(name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip()


def _split_sender_identity(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    m = re.search(r"<([^>]+)>", raw)
    email = m.group(1).strip().lower() if m else ""
    name = re.sub(r"\s*<[^>]*>", "", raw).strip()
    return name, email


def _load_maintainers(tag: str, sha: str | None = None) -> tuple[list[Any], Any]:
    """Load MAINTAINERS sections and matcher for `tag`, falling back to `git show <sha>:MAINTAINERS` when unparsed."""
    from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
    from parser.maintainer_ast.maintainer_parser import MaintainerParser
    maint_file = str(_insights_cfg().get("maintainers_file", "MAINTAINERS") or "").strip()
    if not maint_file:
        return [], MaintainerMatcher([])
    sections: list[Any] = []
    matcher: Any = None
    try:
        from webapp.backend.services.maintainer_service import MaintainerService
        sections, matcher = MaintainerService().get_maintainer_data(tag)
    except Exception as exc:  # noqa: BLE001
        logger.debug("MAINTAINERS DB lookup of %s unavailable: %s", tag, exc)
    if not sections:
        rev = sha or tag
        try:
            text = _git(["show", f"{rev}:{maint_file}"], timeout=30)
            if text.strip():
                sections = MaintainerParser(text).parse()
                matcher = MaintainerMatcher(sections)
        except Exception as exc:  # noqa: BLE001 - repository may lack MAINTAINERS
            logger.debug("MAINTAINERS git fallback of %s unavailable: %s", tag, exc)
    if matcher is None:
        matcher = MaintainerMatcher([])
    return sections, matcher


class _PullSectionResolver:
    """Resolve a merge pull request's MAINTAINERS section via T: tree URL, changed files/dirs, and maintainer."""

    def __init__(self, sections: list[Any], matcher: Any, insights_cfg: dict[str, Any] | None = None) -> None:
        from parser.maintainer_ast.maintainer_types import MaintainerRole

        cfg = insights_cfg if insights_cfg is not None else _insights_cfg()
        self.catchall_section = str(cfg.get("catchall_section", "THE REST") or "")
        self.lead_mergers = [m.strip().lower() for m in (cfg.get("lead_mergers") or []) if m and str(m).strip()]
        self.generic_repo_names = {str(n).strip().lower() for n in (cfg.get("generic_repo_names") or ("linux", "linux-2.6", "kernel")) if n}
        self.ref_stopwords = set(_REF_STOPWORDS) | {str(w).strip().lower() for w in (cfg.get("generic_ref_words") or ()) if w}

        self.matcher = matcher
        self.sections = [s for s in sections if not self.catchall_section or s.name != self.catchall_section]
        self.by_name: dict[str, Any] = {s.name: s for s in self.sections}
        self.tree_map: dict[str, list[Any]] = defaultdict(list)
        self.owner_trees: dict[str, set[str]] = defaultdict(set)
        self.owner_secs: dict[str, list[Any]] = defaultdict(list)
        self.maint_email: dict[str, dict[str, int]] = defaultdict(dict)
        self.maint_name: dict[str, dict[str, int]] = defaultdict(dict)
        self.sec_tokens: dict[str, set[str]] = {}

        for sec in self.sections:
            self.sec_tokens[sec.name] = {
                tok for tok in re.split(r"[^a-z0-9]+", sec.name.lower())
                if len(tok) >= 2 and tok not in _SEC_STOPWORDS
            }
            for tree in re.split(r"[\n;]", sec.scm_tree or ""):
                tree = re.sub(r"^git\s+", "", tree.strip())
                url = (tree.split()[0] if tree else "").rstrip(",.;")
                if "://" not in url:
                    continue
                norm = normalize_url(url)
                if sec not in self.tree_map[norm]:
                    self.tree_map[norm].append(sec)
                own = owner_of(norm)
                if own:
                    self.owner_trees[own].add(norm)
                    if sec not in self.owner_secs[own]:
                        self.owner_secs[own].append(sec)
            for member in sec.members:
                weight = 2 if member.role == MaintainerRole.MAINTAINER else 1
                email = (member.email or "").strip().lower()
                if email:
                    prev = self.maint_email[email].get(sec.name, 0)
                    if weight > prev:
                        self.maint_email[email][sec.name] = weight
                for candidate_name in (member.name, _strip_paren_name(member.name)):
                    norm_n = _norm_person_name(candidate_name)
                    if norm_n:
                        prev = self.maint_name[norm_n].get(sec.name, 0)
                        if weight > prev:
                            self.maint_name[norm_n][sec.name] = weight

    def tree_candidates(self, url: str) -> list[Any]:
        if not url:
            return []
        norm = normalize_url(url)
        if norm in self.tree_map:
            return list(self.tree_map[norm])
        own = owner_of(norm)
        if own and len(self.owner_trees.get(own, ())) == 1:
            return list(self.owner_secs.get(own, ()))
        return []

    def maintainer_weights(self, people: list[tuple[str, str]]) -> dict[str, int]:
        weights: dict[str, int] = {}
        for raw_name, raw_email in people:
            name_part, embedded_email = _split_sender_identity(raw_name)
            email = (raw_email or embedded_email).strip().lower()
            if email and email in self.maint_email:
                for sec_name, w in self.maint_email[email].items():
                    weights[sec_name] = max(weights.get(sec_name, 0), w)
            for cand in (name_part, _strip_paren_name(name_part)):
                norm_n = _norm_person_name(cand)
                if norm_n and norm_n in self.maint_name:
                    for sec_name, w in self.maint_name[norm_n].items():
                        weights[sec_name] = max(weights.get(sec_name, 0), w)
        return weights

    def _is_lead_merger(self, name: str) -> bool:
        if not name:
            return False
        n = name.lower()
        return any(lm in n for lm in self.lead_mergers)

    def _ref_tokens(
        *args: Any,
        generic_repo_names: set[str] | None = None,
        ref_stopwords: set[str] | None = None,
        **kwargs: Any,
    ) -> set[str]:
        if args and isinstance(args[0], _PullSectionResolver):
            self = args[0]
            ref = args[1] if len(args) > 1 else kwargs.get("ref", "")
            subject = args[2] if len(args) > 2 else kwargs.get("subject", "")
            url = args[3] if len(args) > 3 else kwargs.get("url", "")
            gen_names = self.generic_repo_names if generic_repo_names is None else generic_repo_names
            stopwords = self.ref_stopwords if ref_stopwords is None else ref_stopwords
        else:
            ref = args[0] if len(args) > 0 else kwargs.get("ref", "")
            subject = args[1] if len(args) > 1 else kwargs.get("subject", "")
            url = args[2] if len(args) > 2 else kwargs.get("url", "")
            gen_names = {"linux", "linux-2.6", "kernel"} if generic_repo_names is None else generic_repo_names
            stopwords = set(_REF_STOPWORDS) if ref_stopwords is None else ref_stopwords

        parts = [ref or subject or ""]
        if url:
            repo_seg = url.strip().rstrip("/").rsplit("/", 1)[-1]
            if repo_seg.lower().endswith(".git"):
                repo_seg = repo_seg[:-4]
            if repo_seg and repo_seg.lower() not in gen_names:
                parts.append(repo_seg)
        out = set()
        for src in parts:
            for tok in re.split(r"[^a-z0-9]+", src.lower()):
                if len(tok) < 2 or tok in stopwords or tok.isdigit():
                    continue
                if re.fullmatch(r"v?\d+|rc\d*", tok):
                    continue
                out.add(tok)
        return out

    @staticmethod
    def _pattern_specificity(sec: Any, paths: list[str]) -> int:
        from parser.maintainer_ast.maintainer_types import PatternType

        best = 0
        for pat in sec.patterns:
            if pat.pat_type != PatternType.FILE:
                continue
            raw = pat.pattern.strip().lstrip("/")
            if not raw or raw in ("*", "*/"):
                continue
            prefix = raw.split("*", 1)[0].split("?", 1)[0]
            if not prefix:
                continue
            if any(p.startswith(prefix) or p == raw for p in paths):
                best = max(best, len(prefix))
        return best

    def resolve(
        self,
        *,
        url: str,
        ref: str,
        subject: str,
        sender: str,
        sender_email: str = "",
        body: str,
        committer: str,
        committer_email: str,
        changed_paths: list[str],
    ) -> dict[str, Any] | None:
        if not self.sections:
            return None
        tree_cands = self.tree_candidates(url)
        if len(tree_cands) == 1:
            s = tree_cands[0]
            return {"sec_id": s.sec_id, "name": s.name}

        # Collect candidate people (pull sender, non-lead merger on local merges, tag Signed-off-by)
        people: list[tuple[str, str]] = []
        if sender and not self._is_lead_merger(sender):
            people.append((sender, sender_email))
        if not url and committer and not self._is_lead_merger(committer):
            people.append((committer, committer_email))
        if not people:
            for sob_name, sob_email in _SIGNED_OFF_RE.findall(body or ""):
                if not self._is_lead_merger(sob_name):
                    people.append((sob_name, sob_email))
        maint_w = self.maintainer_weights(people)

        file_counts: Counter = Counter()
        if self.matcher and changed_paths:
            for path in changed_paths:
                for s in self.matcher.match_file(path):
                    if not self.catchall_section or s.name != self.catchall_section:
                        file_counts[s.name] += 1

        ref_toks = self._ref_tokens(ref, subject, url)
        ref_overlap = {
            name: len(ref_toks & toks)
            for name, toks in self.sec_tokens.items()
            if ref_toks & toks
        }
        tree_names = {s.name for s in tree_cands}

        if file_counts:
            pool_names = set(file_counts)
            if tree_names and (tree_names & pool_names):
                pool_names &= tree_names
            max_files = max(file_counts[n] for n in pool_names)

            def score(name: str) -> tuple[int, int, int, int, int, int]:
                fc = file_counts[name]
                mw = maint_w.get(name, 0)
                # Prefer a maintainer's own section over a broader unmaintained umbrella when it covers >= 50% of files
                maint_match = 1 if (mw > 0 and fc * 2 >= max_files) else 0
                return (
                    1 if name in tree_names else 0,
                    maint_match,
                    fc,
                    mw,
                    ref_overlap.get(name, 0),
                    self._pattern_specificity(self.by_name[name], changed_paths),
                )

            best_name = max(pool_names, key=score)
            s = self.by_name[best_name]
            return {"sec_id": s.sec_id, "name": s.name}

        # No file matches: resolve from unique maintainer or maintainer + tag/tree disambiguation
        if tree_cands:
            best = max(
                tree_cands,
                key=lambda s: (maint_w.get(s.name, 0), ref_overlap.get(s.name, 0)),
            )
            return {"sec_id": best.sec_id, "name": best.name}

        maint_only = [self.by_name[n] for n, w in maint_w.items() if w >= 2 and n in self.by_name]
        if len(maint_only) == 1:
            s = maint_only[0]
            return {"sec_id": s.sec_id, "name": s.name}
        if len(maint_only) > 1:
            scored = [(ref_overlap.get(s.name, 0), s) for s in maint_only]
            top_ov = max(ov for ov, _ in scored)
            if top_ov > 0:
                winners = [s for ov, s in scored if ov == top_ov]
                if len(winners) == 1:
                    return {"sec_id": winners[0].sec_id, "name": winners[0].name}
        return None


def _changed_files(parent_sha: str, merge_sha: str, limit: int = 2000) -> list[str]:
    try:
        out = _git(["diff", "--name-only", parent_sha, merge_sha], timeout=60)
        return [line.strip() for line in out.splitlines() if line.strip()][:limit]
    except RuntimeError:
        return []


def _parent_committers(shas: list[str]) -> dict[str, tuple[str, str]]:
    """Batch-fetch `sha -> (committer_name, committer_email)` for second-parent tips of merges."""
    unique = [s for s in dict.fromkeys(shas) if _FULL.match(s)]
    out_map: dict[str, tuple[str, str]] = {}
    for i in range(0, len(unique), 500):
        chunk = unique[i:i + 500]
        try:
            out = _git(["log", "--no-walk=unsorted", "--format=%H%x1f%cn%x1f%ce", *chunk], timeout=60)
        except RuntimeError:
            continue
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 3 and _FULL.match(parts[0].strip()):
                out_map[parts[0].strip()] = (parts[1].strip(), parts[2].strip().lower())
    return out_map


def _fallback_sender(
    body_sender: str,
    *,
    p2_name: str,
    p2_email: str,
    committer: str,
    url: str,
    body: str,
    tree_cands: list[Any],
) -> tuple[str, str]:
    """Resolve pull sender from commit message body, second-parent committer, tag trailers, tree maintainers, or merger."""
    if body_sender:
        return body_sender, ""
    if p2_name and (not url or p2_name != committer):
        return p2_name, p2_email
    if url:
        for sob_name, sob_email in _SIGNED_OFF_RE.findall(body or ""):
            if sob_name.strip() and sob_name.strip() != committer:
                return sob_name.strip(), sob_email.strip().lower()
        from parser.maintainer_ast.maintainer_types import MaintainerRole

        maintainers = [
            (member.name, (member.email or "").strip().lower())
            for sec in tree_cands
            for member in getattr(sec, "members", ())
            if member.role == MaintainerRole.MAINTAINER and getattr(member, "name", "")
        ]
        unique_names = list(dict.fromkeys(name for name, _ in maintainers))
        if len(unique_names) == 1:
            return maintainers[0]
    return (p2_name or committer), p2_email


def _batch_pull_commit_counts(
    parsed_merges: list[tuple[dict[str, Any], list[str], dict[str, str], str]],
    span: list[str],
    prev: str | None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        out = rs._git(["rev-list", "--parents", *span], timeout=60)
        if not out:
            return counts
        parents_map: dict[str, list[str]] = {}
        non_merges: set[str] = set()
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            c = parts[0]
            ps = parts[1:]
            parents_map[c] = ps
            if len(ps) < 2:
                non_merges.add(c)

        mainline_seen: set[str] = set()
        if prev:
            mainline_seen.add(prev)
        curr: str | None = span[0]
        while curr and curr in parents_map:
            mainline_seen.add(curr)
            ps = parents_map[curr]
            curr = ps[0] if ps else None

        chronological = sorted(parsed_merges, key=lambda item: item[0]["date"])

        for m, parents, _, _ in chronological:
            p0, p1 = parents[0], parents[1]
            m_sha = m["sha"]
            if p1 in mainline_seen:
                counts[m_sha] = 0
                mainline_seen.add(m_sha)
                continue

            queue = [p1]
            visited = {p1}
            pull_non_merges = 0
            fallback = False

            while queue:
                node = queue.pop(0)
                if node in mainline_seen:
                    continue
                if node in non_merges:
                    pull_non_merges += 1
                node_parents = parents_map.get(node)
                if node_parents is None:
                    if prev is not None and node != prev:
                        fallback = True
                        break
                    continue
                for pr in node_parents:
                    if pr not in visited:
                        visited.add(pr)
                        queue.append(pr)

            if fallback:
                c_str = rs._git(["rev-list", "--count", "--no-merges", p1, f"^{p0}"], timeout=60)
                try:
                    counts[m_sha] = int((c_str or "0").strip() or 0)
                except ValueError:
                    counts[m_sha] = 0
            else:
                counts[m_sha] = pull_non_merges

            mainline_seen.add(m_sha)
            mainline_seen.update(visited)
    except Exception as exc:  # noqa: BLE001
        logger.debug("batch pull commit counts failed: %s", exc)
    return counts


def _batch_pull_dirstats(span: list[str]) -> dict[str, list[dict[str, Any]]]:
    batch_dirs: dict[str, list[dict[str, Any]]] = {}
    try:
        out = rs._git(["log", "--first-parent", "--merges", "-m", "--dirstat=files,3,cumulative", "--format=%x1e%H", *span], timeout=60)
        for chunk in (out or "").split("\x1e"):
            lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
            if not lines:
                continue
            sha = lines[0]
            dirs: list[dict[str, Any]] = []
            for line in lines[1:9]:
                pct, _, d = line.partition("% ")
                if d:
                    try:
                        dirs.append({"dir": d.strip(), "percent": float(pct)})
                    except ValueError:
                        pass
            batch_dirs[sha] = dirs
    except Exception as exc:  # noqa: BLE001
        logger.debug("batch pull dirstats failed: %s", exc)
    return batch_dirs


def _batch_pull_changed_files(span: list[str]) -> dict[str, list[str]]:
    batch_files: dict[str, list[str]] = {}
    try:
        out = rs._git(["log", "--first-parent", "--merges", "-m", "--no-renames", "--format=%x1e%H", "--name-only", *span], timeout=60)
        for chunk in (out or "").split("\x1e"):
            lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
            if not lines:
                continue
            sha = lines[0]
            batch_files[sha] = lines[1:]
    except Exception as exc:  # noqa: BLE001
        logger.debug("batch pull changed files failed: %s", exc)
    return batch_files


def compute_merge_window(tag: str, progress: Callable | None = None, *, latest: dict[str, Any] | None = None) -> dict[str, Any]:
    info = (latest or rs.latest_info()) if tag == rs.LATEST_RELEASE else None
    if info is not None:
        prev, sha = info["prev_sha"], info["sha"]
        merges = rs.merges_now(tag, range_override=(prev, sha))
        rc1 = info["rc1"]
        rc1_sha = info["rc1_sha"]
        prev_name = info["prev"]
        maint_rev = sha
    else:
        prev, sha = rs.range_of(tag)
        merges = rs.merges_now(tag)
        tags = rs.tag_commits()
        p = rs.parse_tag(tag)
        milestone_rc = int(_insights_cfg().get("milestone_rc", 1))
        rc1 = next((t for t in tags if (q := rs.parse_tag(t)) and q["series"] == p["series"] and q["pre"] == milestone_rc), None) if milestone_rc > 0 else None
        rc1_sha = tags.get(rc1) if rc1 else None
        prev_name = rs.previous(tag)
        maint_rev = tag
    td = rs.tag_dates()
    if prev_name and prev_name in td and td[prev_name]:
        start = td[prev_name]
    else:
        start_s = (rs._git(["log", "-1", "--format=%ct", prev], timeout=10) or "").strip() if prev else ""
        start = int(start_s) if start_s.isdigit() else 0
    if rc1 and rc1 in td and td[rc1]:
        rc1_at = td[rc1]
    else:
        rc1_s = (rs._git(["log", "-1", "--format=%ct", rc1_sha], timeout=10) or "").strip() if rc1_sha else ""
        rc1_at = int(rc1_s) if rc1_s.isdigit() else None
    sections, matcher = _load_maintainers(maint_rev, sha)
    resolver = _PullSectionResolver(sections, matcher)
    parsed_merges: list[tuple[dict[str, Any], list[str], dict[str, str], str]] = []
    need_p2: list[str] = []
    for m in merges:
        parents = [x for x in m["parents"] if _FULL.match(x)]
        if len(parents) < 2:
            continue
        pull = parse_pull(m["subject"])
        body_sender = _sender(m["body"])
        parsed_merges.append((m, parents, pull, body_sender))
        if not body_sender:
            need_p2.append(parents[1])
    p2_committers = _parent_committers(need_p2) if need_p2 else {}
    from webapp.backend.services.commit_facts import commit_ids
    ids = commit_ids([m["sha"] for m, _, _, _ in parsed_merges])
    threads = _pull_threads(list(ids.values()))

    span = [sha] + ([f"^{prev}"] if prev else [])
    batch_counts: dict[str, int] = {}
    batch_dirs: dict[str, list[dict[str, Any]]] = {}
    batch_files: dict[str, list[str]] = {}
    if len(parsed_merges) >= 2:
        batch_counts = _batch_pull_commit_counts(parsed_merges, span, prev)
        batch_dirs = _batch_pull_dirstats(span)
        if resolver.sections and any(len(resolver.tree_candidates(pull["url"]) if pull["url"] else []) != 1 for _, _, pull, _ in parsed_merges):
            batch_files = _batch_pull_changed_files(span)

    pulls = []
    total = len(parsed_merges)
    for i, (m, parents, pull, body_sender) in enumerate(parsed_merges):
        if progress and i % 20 == 0:
            progress(i, total, "pulls")
        count = batch_counts.get(m["sha"])
        if count is None:
            count = int((_git(["rev-list", "--count", "--no-merges", parents[1], f"^{parents[0]}"], timeout=60) or "0").strip() or 0)
        dirs = batch_dirs.get(m["sha"])
        if dirs is None:
            dirs = []
            try:
                for line in _git(["diff", "--dirstat=files,3,cumulative", parents[0], m["sha"]], timeout=60).splitlines()[:8]:
                    pct, _, d = line.strip().partition("% ")
                    if d:
                        dirs.append({"dir": d.strip(), "percent": float(pct)})
            except RuntimeError:
                pass
        tree_cands = resolver.tree_candidates(pull["url"]) if pull["url"] else []
        p2_name, p2_email = p2_committers.get(parents[1], ("", ""))
        sender, sender_email = _fallback_sender(
            body_sender,
            p2_name=p2_name,
            p2_email=p2_email,
            committer=m["committer"],
            url=pull["url"],
            body=m["body"],
            tree_cands=tree_cands,
        )
        if len(tree_cands) == 1:
            sec = {"sec_id": tree_cands[0].sec_id, "name": tree_cands[0].name}
        else:
            if m["sha"] in batch_files:
                paths = batch_files[m["sha"]]
            else:
                paths = _changed_files(parents[0], m["sha"]) if resolver.sections else []
            if not paths and dirs:
                paths = [d["dir"] for d in dirs]
            sec = resolver.resolve(
                url=pull["url"],
                ref=pull["ref"],
                subject=m["subject"],
                sender=sender,
                sender_email=sender_email,
                body=m["body"],
                committer=m["committer"],
                committer_email=m.get("committer_email", ""),
                changed_paths=paths,
            )
        day = round((m["date"] - start) / 86400, 1) if start else None
        thread = threads.get(ids.get(m["sha"]))
        pulls.append({
            "merge": m["sha"], "parents": parents, "date": m["date"], "day": day,
            "after_rc1": bool(rc1_at and m["date"] > rc1_at), "subject": m["subject"],
            "source_url": pull["url"], "ref": pull["ref"], "sender": sender,
            "commits": count, "dirs": dirs, "section": sec, "thread": thread, "body": m["body"][:3000],
        })
    by_day: Counter = Counter()
    for pl in pulls:
        if pl["day"] is not None:
            by_day[int(pl["day"])] += pl["commits"]
    out = {
        "release": tag, "prev": prev_name, "source": "git", "rc1": rc1, "rc1_day": round((rc1_at - start) / 86400, 1) if rc1_at and start else None,
        "pulls": pulls,
        "totals": {"pulls": len(pulls), "commits": sum(x["commits"] for x in pulls),
                   "before_rc1": sum(1 for x in pulls if not x["after_rc1"]),
                   "by_day": [{"day": d, "commits": by_day[d]} for d in sorted(by_day)]},
    }
    if info is not None:
        out["branch"] = info["branch"]
        out["head"] = info["sha"]
    return out


def db_merge_window(tag: str) -> dict[str, Any] | None:
    """The merge-window report from m_release_pull (read only), or None when unparsed or format differs."""
    if tag == rs.LATEST_RELEASE:
        return None
    try:
        from webapp.backend.database.helpers import get_active_repo_id
        from webapp.backend.database.pool import get_reader

        tags = rs.tag_commits()
        prev_name = rs.previous(tag)
        with get_reader() as cur:
            repo_id = get_active_repo_id(cur)
            segs = cur.release_segments(repo_id=repo_id, tags=[tag])
            seg = segs[0] if segs else None
            if not seg:
                return None
            if bytes(seg["tag_hash"]).hex() != tags.get(tag):
                return None
            if _text(seg["diff_base_tag"]) != (prev_name or ""):
                return None
            segment_id = int(seg["segment_id"])
            rows = cur.release_pulls(segment_id)
            if not rows or any(int(r["format"]) != MW_FORMAT for r in rows):
                return None

            td = rs.tag_dates()
            if prev_name and prev_name in td and td[prev_name]:
                start = td[prev_name]
            else:
                prev_sha = tags.get(prev_name) if prev_name else None
                start_s = (rs._git(["log", "-1", "--format=%ct", prev_sha], timeout=10) or "").strip() if prev_sha else ""
                start = int(start_s) if start_s.isdigit() else 0

            milestone_rc = int(_insights_cfg().get("milestone_rc", 1))
            p = rs.parse_tag(tag)
            rc1 = next((t for t in tags if (q := rs.parse_tag(t)) and q["series"] == p["series"] and q["pre"] == milestone_rc), None) if milestone_rc > 0 else None
            rc1_sha = tags.get(rc1) if rc1 else None
            if rc1 and rc1 in td and td[rc1]:
                rc1_at = td[rc1]
            else:
                rc1_s = (rs._git(["log", "-1", "--format=%ct", rc1_sha], timeout=10) or "").strip() if rc1_sha else ""
                rc1_at = int(rc1_s) if rc1_s.isdigit() else None

            if len(rows) == 1 and (int(rows[0]["pull_idx"]) == 65535 or bytes(rows[0]["merge_hash"]) == b"\x00" * 20):
                pulls = []
            else:
                from webapp.backend.services.commit_facts import commit_ids
                cids = commit_ids([bytes(r["merge_hash"]).hex() for r in rows])
                threads = _pull_threads(list(cids.values()))

                sec_name_to_id: dict[str, int] = {}
                if any(_text(r["sec_name"]) and int(r["sec_id"] or 0) == 0 for r in rows):
                    try:
                        if cur.row_exists("m_git_ref", {"repo_id": repo_id, "ref_name": tag}):
                            from webapp.backend.services.maintainer_service import MaintainerService
                            m_sections, _ = MaintainerService().get_maintainer_data(tag)
                            sec_name_to_id = {s.name: s.sec_id for s in m_sections if getattr(s, "sec_id", None) is not None}
                    except Exception:
                        pass

                pulls = []
                for r in rows:
                    m_sha = bytes(r["merge_hash"]).hex()
                    p1_sha = bytes(r["p1_hash"]).hex()
                    p2_sha = bytes(r["p2_hash"]).hex()
                    m_date = int(r["merge_date"])
                    dirs_raw = _text(r["dirs_json"])
                    dirs = []
                    parents = [p1_sha, p2_sha]
                    if dirs_raw:
                        try:
                            parsed_dirs = json.loads(dirs_raw)
                            if isinstance(parsed_dirs, dict):
                                dirs = parsed_dirs.get("dirs") or []
                                if parsed_dirs.get("parents"):
                                    parents = parsed_dirs["parents"]
                            elif isinstance(parsed_dirs, list):
                                dirs = parsed_dirs
                        except Exception:
                            pass
                    sec_name = _text(r["sec_name"])
                    sec_id = int(r["sec_id"] or 0)
                    if not sec_id and sec_name in sec_name_to_id:
                        sec_id = sec_name_to_id[sec_name]
                    sec = {"sec_id": sec_id or None, "name": sec_name} if sec_name else None
                    day = round((m_date - start) / 86400, 1) if start else None
                    after_rc1 = bool(rc1_at and m_date > rc1_at)
                    thread = threads.get(cids.get(m_sha))
                    pulls.append({
                        "merge": m_sha,
                        "parents": parents,
                        "date": m_date,
                        "day": day,
                        "after_rc1": after_rc1,
                        "subject": _text(r["subject"]),
                        "source_url": _text(r["source_url"]),
                        "ref": _text(r["ref_name"]),
                        "sender": _text(r["sender"]),
                        "commits": int(r["commits"]),
                        "dirs": dirs,
                        "section": sec,
                        "thread": thread,
                        "body": _text(r["body"])[:3000] if r["body"] else "",
                    })

            by_day: Counter = Counter()
            for pl in pulls:
                if pl["day"] is not None:
                    by_day[int(pl["day"])] += pl["commits"]

            return {
                "release": tag,
                "prev": prev_name,
                "source": "database",
                "rc1": rc1,
                "rc1_day": round((rc1_at - start) / 86400, 1) if rc1_at and start else None,
                "pulls": pulls,
                "totals": {
                    "pulls": len(pulls),
                    "commits": sum(x["commits"] for x in pulls),
                    "before_rc1": sum(1 for x in pulls if not x["after_rc1"]),
                    "by_day": [{"day": d, "commits": by_day[d]} for d in sorted(by_day)],
                },
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("db_merge_window of %s failed: %s", tag, exc)
        return None


def _sender(body: str) -> str:
    text = body or ""
    for pat in (
        r"^(?:Pull|Pul|Pyll)\b.*?\b(?:from|frfom|friom|fromm|fro)[,\s]+([^:\n]+?)\s*:",
        r"^Pull\s+.+?\b(?:updates?|fixes|changes?)\s+([A-Z][^:\n]+?):",
        r"^(?:Pull|Merge)\b[^:\n]*\bfrom\s+([A-Z][^:\n.]{2,60}?)(?::|\.\s*$|\s+that\b|\s*$)",
    ):
        m = re.search(pat, text, re.M)
        if m:
            return m.group(1).strip().rstrip(".")
    first_line = text.splitlines()[0] if text else ""
    if not first_line.lstrip().startswith("* "):
        m = re.search(r"\bfrom\s+([A-Z][a-zA-Z.-]+(?:\s+[A-Z][a-zA-Z.-]+){1,3})(?:\s*:|\s+that\s+were|\s*$)", first_line)
        if m:
            return m.group(1).strip()
    return ""


def _pull_threads(commit_ids: list[int]) -> dict[int, dict[str, Any]]:
    """commit_id -> {message_id, thread_id} of its [GIT PULL] mail (m_commit_email relation 4, read only)."""
    if not commit_ids:
        return {}
    try:
        from webapp.backend.database.pool import get_reader
        with get_reader() as reader:
            out = {}
            for i in range(0, len(commit_ids), 500):
                part = commit_ids[i:i + 500]
                for r in reader.select_rows(
                    "m_commit_email", ["commit_id", "message_id", "thread_id"], {"relation_type": 4, "commit_id": part}
                ):
                    out.setdefault(int(r["commit_id"]), {"message_id": r["message_id"], "thread_id": int(r["thread_id"])})
            return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("pull threads unavailable: %s", exc)
        return {}


def merge_window(tag: str, wait: float = 0.0) -> dict[str, Any]:
    _check_release(tag)
    if tag == rs.LATEST_RELEASE:
        info = rs.latest_info()
        ck = f"{get_active_repo_name()}|mw|{MW_FORMAT}|latest|{info['prev_sha'] or '-'}|{info['sha']}|{info['rc1'] or '-'}"
        return _job(f"insights:{ck}", lambda p, _info=info: compute_merge_window(tag, p, latest=_info), ck, wait)
    if not rs.parse_tag(tag)["final"]:
        raise HTTPException(status_code=400, detail="A merge window belongs to a release (vX.Y, <prefix>X.Y), not to an -rc or a stable update.")
    hit = db_merge_window(tag)
    if hit is not None:
        return {"state": "done", "result": hit}
    sha = rs.tag_commits()[tag]
    ck = f"{get_active_repo_name()}|mw|{MW_FORMAT}|{sha}"
    return _job(f"insights:{ck}", lambda p: compute_merge_window(tag, p), ck, wait)


# ------------------------------------------------------------------------------- H7 subsystem health

def bus_factor(counts: list[int]) -> int:
    """Smallest number of authors covering at least half of the commits."""
    total = sum(counts)
    if not total:
        return 0
    acc = 0
    for i, n in enumerate(sorted(counts, reverse=True), 1):
        acc += n
        if acc * 2 >= total:
            return i
    return len(counts)


def flags_for(sec: dict[str, Any], insights_cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = insights_cfg if insights_cfg is not None else _insights_cfg()
    orphan_min = int(cfg.get("orphan_min_commits", 5))
    health_min = int(cfg.get("health_min_commits", 10))
    no_rev_thresh = float(cfg.get("no_review_threshold", 0.10))
    f = []
    status = (sec.get("status") or "").lower()
    if sec["commits"] == 0:
        f.append("no_activity")
    if status.startswith(("orphan", "obsolete")) and sec["commits"] >= orphan_min:
        f.append("orphan_active")
    if sec["commits"] >= health_min and sec["bus_factor"] == 1:
        f.append("single_author")
    if sec["commits"] >= health_min and sec["review_coverage"] < no_rev_thresh:
        f.append("no_review")
    if sec["commits"] and sec["maintainers"] and not any(m.get("last_active") for m in sec["maintainers"]):
        f.append("silent_maintainers")
    return f


HEALTH_ROLES = {CommitRole.REVIEWED_BY: "reviewed_by", CommitRole.ACKED_BY: "acked_by",
                CommitRole.SIGNED_OFF_BY: "signed_off_by", CommitRole.TESTED_BY: "tested_by"}
HEALTH_CHUNK = 2000


def _health_window(tag: str, window: int, *, latest: dict[str, Any] | None = None) -> list[str]:
    if tag == rs.LATEST_RELEASE:
        info = latest or rs.latest_info()
        prev = info["prev"]
        if not prev:
            return [rs.LATEST_RELEASE]
        family = rs.family_of(prev)
        finals = [t for t in rs.releases("mainline") if (p := rs.parse_tag(t))["final"] and p["family"] == family
                  and rs.release_key(t) <= rs.release_key(prev)]
        prev_tags = finals[-(window - 1):] if window > 1 else []
        return [*prev_tags, rs.LATEST_RELEASE]
    family = rs.family_of(tag)
    finals = [t for t in rs.releases("mainline") if (p := rs.parse_tag(t))["final"] and p["family"] == family
              and rs.release_key(t) <= rs.release_key(tag)]
    return finals[-window:]


def _section_pattern_hash(sec: Any) -> bytes:
    pats = getattr(sec, "patterns", ()) or ()
    tokens = sorted(
        f"{int(getattr(p, 'pat_type', 0))}:{str(getattr(p, 'pattern', '')).strip().lstrip('/')}"
        for p in pats
    )
    return hashlib.sha1("\n".join(tokens).encode("utf-8")).digest()


def _sections_manifest_hash(sections: list[Any], catchall: str = "THE REST") -> bytes:
    parts = []
    for s in sections:
        if catchall and s.name == catchall:
            continue
        pat_h = _section_pattern_hash(s).hex()
        status = getattr(s, "status", "") or ""
        parts.append(f"{s.name}\x1f{status}\x1f{pat_h}")
    parts.sort()
    return hashlib.sha1("\n".join(parts).encode("utf-8")).digest()


def _aggregate_health(tag: str, tags: list[str], sections: list[Any], matcher: Any,
                      commits: Callable[[str], Any], file_secs: dict[str, list[str]] | None = None,
                      progress: Callable | None = None,
                      precomputed: dict[str, dict[str, tuple[int, int, dict[str, int], dict[str, int]]]] | None = None) -> dict[str, Any]:
    """The report rows from each release's commits ({date, author, email, trailers, files}); shared by the Git
    and database paths, so both build exactly the same rows. `file_secs` may hold the sections of some paths
    already (path -> section names, THE REST left out); the others are matched with MAINTAINERS."""
    from parser.maintainer_ast.maintainer_types import MaintainerRole
    people: dict[str, dict[str, int]] = defaultdict(dict)  # section -> identity -> last active time
    author_counts: dict[str, Counter] = defaultdict(Counter)
    per_release: dict[str, Counter] = defaultdict(Counter)
    reviewed: Counter = Counter()
    file_secs = dict(file_secs or {})
    catchall = str(_insights_cfg().get("catchall_section", "THE REST") or "")
    for ti, t in enumerate(tags):
        if progress:
            progress(ti, len(tags), f"commits of {t}")
        if precomputed and t in precomputed:
            t_data = precomputed[t]
            for sec_name, (sec_commits, sec_reviewed, auth_dict, act_dict) in t_data.items():
                if catchall and sec_name == catchall:
                    continue
                for auth, c_cnt in auth_dict.items():
                    author_counts[sec_name][auth] += c_cnt
                per_release[sec_name][t] += sec_commits
                reviewed[sec_name] += sec_reviewed
                for k, ts in act_dict.items():
                    if ts > people[sec_name].get(k, 0):
                        people[sec_name][k] = ts
            continue

        for c in commits(t):
            secs: set[str] = set()
            for f in c["files"]:
                if f not in file_secs:
                    file_secs[f] = [s.name for s in matcher.match_file(f) if not catchall or s.name != catchall]
                secs.update(file_secs[f])
            if not secs:
                continue
            author = identity(c["author"], c["email"])
            tr = c.get("trailers") or {}
            reviewers = {identity(n, e) for role in ("reviewed_by", "acked_by") for n, e in tr.get(role, [])} - {author}
            involved = {author} | {identity(n, e) for role in ("reviewed_by", "acked_by", "signed_off_by", "tested_by") for n, e in tr.get(role, [])}
            for sec in secs:
                author_counts[sec][author] += 1
                per_release[sec][t] += 1
                if reviewers:
                    reviewed[sec] += 1
                for k in involved:
                    if c["date"] > people[sec].get(k, 0):
                        people[sec][k] = c["date"]
    out = []
    for sec in sections:
        name = sec.name
        if catchall and name == catchall:
            continue
        counts = author_counts.get(name, Counter())
        n = sum(counts.values())
        maint = []
        for m in sec.members:
            if m.role not in (MaintainerRole.MAINTAINER, getattr(MaintainerRole, "REVIEWER", MaintainerRole.MAINTAINER)):
                continue
            k = identity(m.name, m.email)
            maint.append({"person": m.name, "email": m.email, "role": "R" if m.role != MaintainerRole.MAINTAINER else "M",
                          "last_active": people.get(name, {}).get(k) or None})
        row = {
            "sec_id": sec.sec_id, "name": name, "status": sec.status, "commits": n,
            "per_release": [per_release[name].get(t, 0) for t in tags], "authors": len(counts),
            "review_coverage": round(reviewed[name] / n, 4) if n else 0.0, "bus_factor": bus_factor(list(counts.values())),
            "top_authors": [{"identity": k, "commits": v} for k, v in counts.most_common(5)],
            "maintainers": maint,
        }
        row["flags"] = flags_for(row)
        out.append(row)
    out.sort(key=lambda r: -r["commits"])
    return {"release": tag, "window": tags, "sections": out}


def _maintainer_data(tag: str, *, sha: str | None = None) -> tuple[list[Any], Any]:
    resolved_sha = sha or (rs.latest_info()["sha"] if tag == rs.LATEST_RELEASE else rs.tag_commits().get(tag))
    rev = resolved_sha if tag == rs.LATEST_RELEASE else tag
    return _load_maintainers(rev, resolved_sha)


def _read_precomputed_health(
    cur: Any,
    repo_id: int,
    tags: list[str],
    sections: list[Any],
    tc: dict[str, str],
) -> tuple[dict[str, dict[str, tuple[int, int, dict[str, int], dict[str, int]]]], list[str]]:
    """Validate and load pre-computed `m_release_section_health` rows for `tags`.
    Returns `(precomputed_by_tag, unanswered_tags)`."""
    from parser.git_ast import release_stats

    catchall = str(_insights_cfg().get("catchall_section", "THE REST") or "")
    active_sections = [s for s in sections if not catchall or s.name != catchall]
    sec_pat_hashes = {s.name: _section_pattern_hash(s) for s in active_sections}
    manifest_hash = _sections_manifest_hash(sections, catchall)

    query_tags = [t for t in tags if t != rs.LATEST_RELEASE and t in tc]
    seg_by_tag = {
        r["tag"]: r
        for r in cur.release_segments(repo_id=repo_id, tags=query_tags)
    } if query_tags else {}

    valid_segs: dict[str, int] = {}
    unanswered_tags: list[str] = []
    for t in tags:
        if t == rs.LATEST_RELEASE or t not in tc:
            unanswered_tags.append(t)
            continue
        prev = rs.previous(t)
        seg = seg_by_tag.get(t)
        if (
            not seg
            or int(seg["format"]) != release_stats.FORMAT
            or bytes(seg["tag_hash"]).hex() != tc.get(t)
            or _text(seg["diff_base_tag"]) != (prev or "")
        ):
            unanswered_tags.append(t)
            continue
        valid_segs[t] = int(seg["segment_id"])

    health_by_seg: dict[int, list[dict[str, Any]]] = {}
    if valid_segs:
        try:
            for r in cur.release_section_health(list(valid_segs.values())):
                health_by_seg.setdefault(int(r["segment_id"]), []).append(r)
        except Exception:
            health_by_seg = {}

    precomputed: dict[str, dict[str, tuple[int, int, dict[str, int], dict[str, int]]]] = {}
    for t in tags:
        sid = valid_segs.get(t)
        if sid is None:
            continue
        health_rows = health_by_seg.get(sid)
        if not health_rows or any(int(r["format"]) != HEALTH_FORMAT for r in health_rows):
            unanswered_tags.append(t)
            continue
        sentinel = next((r for r in health_rows if _text(r["sec_name"]) == ""), None)
        if sentinel is None:
            unanswered_tags.append(t)
            continue
        sentinel_pat = bytes(sentinel["pat_hash"])
        by_name = {_text(r["sec_name"]): r for r in health_rows if _text(r["sec_name"]) != ""}
        valid = True
        for s in active_sections:
            if s.name in by_name:
                if bytes(by_name[s.name]["pat_hash"]) != sec_pat_hashes[s.name]:
                    valid = False
                    break
            elif sentinel_pat != manifest_hash:
                valid = False
                break
        if not valid:
            unanswered_tags.append(t)
            continue
        precomputed[t] = {
            sec_n: (
                int(r["commits"]),
                int(r["reviewed_commits"]),
                json.loads(_text(r["authors_json"]) or "{}"),
                json.loads(_text(r["activity_json"]) or "{}"),
            )
            for sec_n, r in by_name.items()
        }
    return precomputed, unanswered_tags


def compute_subsystem_health(tag: str, window: int, progress: Callable | None = None, *, latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """The subsystem-health report: from the database when it covers every release of the window
    (health_from_db()), else one `git log --name-only` pass per release (`source: "git"`)."""
    info = (latest or rs.latest_info()) if tag == rs.LATEST_RELEASE else None
    tags = _health_window(tag, window, latest=info)
    sections, matcher = _maintainer_data(tag, sha=info["sha"]) if info is not None else _maintainer_data(tag)
    if rs.LATEST_RELEASE not in tags:
        try:
            hit = health_from_db(tag, tags, sections, matcher, progress)
        except Exception as exc:  # noqa: BLE001 - Git answers
            logger.warning("subsystem health of %s from the database failed: %s", tag, exc)
            hit = None
        if hit is not None:
            return hit

    # Even when latest is in tags or some tags are uncovered, check DB coverage for non-latest tags
    db_precomputed: dict[str, dict[str, Any]] = {}
    db_commits: dict[str, list[dict[str, Any]]] = {}
    db_file_secs: dict[str, list[str]] = {}
    non_latest = [t for t in tags if t != rs.LATEST_RELEASE]
    if non_latest:
        try:
            from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id, safe_decode
            from webapp.backend.database.pool import get_reader

            tc = rs.tag_commits()
            with get_reader() as cur:
                repo_id = get_active_repo_id(cur)
                network_id = get_active_network_id(cur)
                check_tags = [t for t in non_latest if t in tc]
                db_precomputed, unanswered = (
                    _read_precomputed_health(cur, repo_id, check_tags, sections, tc)
                    if repo_id is not None and check_tags
                    else ({}, [])
                )

                # For unanswered non-latest tags, try health_coverage
                if unanswered and network_id is not None:
                    cov = health_coverage(cur, unanswered)
                    if cov is not None:
                        names: dict[int, str] = {}
                        fnids_seen: set[int] = set()
                        for t in unanswered:
                            segs = cov[t]
                            source, joins, where = _release_commit_source(network_id, repo_id, segs)
                            fetched_rows = cur.select_rows(
                                source,
                                {"commit_id": "c.commit_id", "author_date": "c.author_date", "committer_date": "c.committer_date",
                                 "name": "p.name", "email": "p.email"},
                                where,
                                joins=[*joins, Join(("m_maintainer_person", "p"), {"p.person_id": "c.author_id"}, "left")],
                            )
                            fetched = sorted(fetched_rows, key=lambda r: (int(r["committer_date"] or 0), int(r["commit_id"])), reverse=True)
                            c_dict = {int(r["commit_id"]): {"date": int(r["author_date"] or 0), "author": safe_decode(r["name"]) or "",
                                                             "email": (safe_decode(r["email"]) or "").lower(), "trailers": {}, "fnids": set()}
                                      for r in fetched}
                            ids = sorted(c_dict)
                            for i in range(0, len(ids), HEALTH_CHUNK):
                                part = ids[i:i + HEALTH_CHUNK]
                                for r in cur.select_rows("m_commit_file", ["commit_id", "fnid", "old_fnid"], {"commit_id": part}):
                                    c = c_dict[int(r["commit_id"])]
                                    for f in (int(r["fnid"] or 0), int(r["old_fnid"] or 0)):
                                        if f > 0:
                                            c["fnids"].add(f)
                                roles = [int(x) for x in HEALTH_ROLES]
                                for r in cur.select_rows(
                                    ("m_bridge_commit_person", "b"),
                                    {"commit_id": "b.commit_id", "role_type": "b.role_type", "name": "p.name", "email": "p.email"},
                                    {"b.commit_id": part, "b.role_type": roles},
                                    joins=[Join(("m_maintainer_person", "p"), {"p.person_id": "b.person_id"})],
                                ):
                                    role = HEALTH_ROLES[CommitRole(int(r["role_type"]))]
                                    c_dict[int(r["commit_id"])]["trailers"].setdefault(role, []).append(
                                        [safe_decode(r["name"]) or "", (safe_decode(r["email"]) or "").lower()])
                            db_commits[t] = list(c_dict.values())
                            for c in db_commits[t]:
                                fnids_seen |= c["fnids"]

                        missing = sorted(fnids_seen)
                        for i in range(0, len(missing), HEALTH_CHUNK):
                            part = missing[i:i + HEALTH_CHUNK]
                            names.update({
                                int(r["fnid"]): safe_decode(r["fname"])
                                for r in cur.select_rows("m_file_name", ["fnid", "fname"], {"fnid": part})
                            })
                        catchall = str(_insights_cfg().get("catchall_section", "THE REST") or "")
                        sec_name = {s.sec_id: s.name for s in sections if (not catchall or s.name != catchall) and isinstance(s.sec_id, int)}
                        sec_ids = sorted(sec_name)
                        if sec_ids:
                            try:
                                for i in range(0, len(missing), HEALTH_CHUNK):
                                    part = missing[i:i + HEALTH_CHUNK]
                                    for r in cur.select_rows("m_maintainer_file", ["fnid", "sec_id"], {"fnid": part, "sec_id": sec_ids}):
                                        path = names.get(int(r["fnid"]))
                                        if path is not None:
                                            db_file_secs.setdefault(path, []).append(sec_name[int(r["sec_id"])])
                            except Exception:
                                pass
                        for t, rows in db_commits.items():
                            for c in rows:
                                c["files"] = sorted(names[f] for f in c.pop("fnids") if f in names)
        except Exception as exc:
            logger.debug("partial DB health lookup failed: %s", exc)

    def _commits(t: str) -> list[dict[str, Any]]:
        if t in db_commits:
            return db_commits[t]
        if t == rs.LATEST_RELEASE and info is not None:
            return rs.release_commits_now(t, range_override=(info["prev_sha"], info["sha"]))
        return _release_pass(t)

    report = {
        **_aggregate_health(
            tag,
            tags,
            sections,
            matcher,
            _commits,
            file_secs=db_file_secs,
            progress=progress,
            precomputed=db_precomputed,
        ),
        "source": "git",
    }
    if info is not None:
        report["branch"] = info["branch"]
        report["head"] = info["sha"]
    return report


def health_coverage(cur: Any, tags: list[str]) -> dict[str, list[int]] | None:
    """{release: segment_ids} when the database holds every input of the window, else None: each release's
    range (previous final release .. release) maps to release segments in the current format
    (release_service.walk_segments()), finished ingestions join the two tags (their Step 6.5 wrote the
    commits' files and trailers), and the non-merge commits of those segments in m_commit are as many as
    the segments counted."""
    from parser.git_ast import release_stats
    from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id
    from webapp.backend.services.history_service import _ingest_chain
    tc = rs.tag_commits()
    network_id = get_active_network_id(cur)
    repo_id = get_active_repo_id(cur)
    if network_id is None:
        return None
    out: dict[str, list[int]] = {}
    for t in tags:
        if t == rs.LATEST_RELEASE:
            return None
        prev = rs.previous(t)
        segs = rs.walk_segments(prev, t) if prev else None
        if not segs or _ingest_chain(cur, network_id, tc[prev], tc[t]) is None:
            return None
        row = cur.select_one(
            "m_release_segment",
            {"n": Agg("SUM", "commits"), "lo": Agg("MIN", "format"), "hi": Agg("MAX", "format"), "k": Agg("COUNT", "*")},
            {"segment_id": segs},
        ) or {}
        if int(row.get("k") or 0) != len(segs) or int(row.get("lo") or 0) != release_stats.FORMAT or int(row.get("hi") or 0) != release_stats.FORMAT:
            return None
        source, joins, where = _release_commit_source(network_id, repo_id, segs)
        if cur.count_rows(source, where, joins=joins) != int(row.get("n") or 0):
            return None
        out[t] = segs
    return out


def health_from_db(tag: str, tags: list[str], sections: list[Any], matcher: Any,
                   progress: Callable | None = None) -> dict[str, Any] | None:
    """The report from the database (read only), or None when it does not cover the window (health_coverage()).

    Pre-computed rows from m_release_section_health are read directly when format and pattern hashes match.
    Other releases are read via m_commit, m_commit_file, and m_bridge_commit_person."""
    from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id, safe_decode
    from webapp.backend.database.pool import get_reader

    tc = rs.tag_commits()
    with get_reader() as cur:
        repo_id = get_active_repo_id(cur)
        network_id = get_active_network_id(cur)
        if repo_id is None:
            return None

        precomputed, unanswered_tags = _read_precomputed_health(cur, repo_id, tags, sections, tc)

        if not unanswered_tags:
            report = _aggregate_health(tag, tags, sections, matcher, lambda t: [], progress=progress, precomputed=precomputed)
            return {**report, "source": "database"}

        if network_id is None:
            return None

        # Try health_coverage for unanswered tags
        cov = health_coverage(cur, unanswered_tags)
        if cov is None:
            return None

        names: dict[int, str] = {}
        by_tag: dict[str, list[dict[str, Any]]] = {}
        fnids_seen: set[int] = set()
        for t in unanswered_tags:
            segs = cov[t]
            source, joins, where = _release_commit_source(network_id, repo_id, segs)
            fetched_rows = cur.select_rows(
                source,
                {"commit_id": "c.commit_id", "author_date": "c.author_date", "committer_date": "c.committer_date",
                 "name": "p.name", "email": "p.email"},
                where,
                joins=[*joins, Join(("m_maintainer_person", "p"), {"p.person_id": "c.author_id"}, "left")],
            )
            fetched = sorted(fetched_rows, key=lambda r: (int(r["committer_date"] or 0), int(r["commit_id"])), reverse=True)
            commits = {int(r["commit_id"]): {"date": int(r["author_date"] or 0), "author": safe_decode(r["name"]) or "",
                                             "email": (safe_decode(r["email"]) or "").lower(), "trailers": {}, "fnids": set()}
                       for r in fetched}
            ids = sorted(commits)
            for i in range(0, len(ids), HEALTH_CHUNK):
                part = ids[i:i + HEALTH_CHUNK]
                for r in cur.select_rows("m_commit_file", ["commit_id", "fnid", "old_fnid"], {"commit_id": part}):
                    c = commits[int(r["commit_id"])]
                    for f in (int(r["fnid"] or 0), int(r["old_fnid"] or 0)):
                        if f > 0:
                            c["fnids"].add(f)
                roles = [int(x) for x in HEALTH_ROLES]
                for r in cur.select_rows(
                    ("m_bridge_commit_person", "b"),
                    {"commit_id": "b.commit_id", "role_type": "b.role_type", "name": "p.name", "email": "p.email"},
                    {"b.commit_id": part, "b.role_type": roles},
                    joins=[Join(("m_maintainer_person", "p"), {"p.person_id": "b.person_id"})],
                ):
                    role = HEALTH_ROLES[CommitRole(int(r["role_type"]))]
                    commits[int(r["commit_id"])]["trailers"].setdefault(role, []).append(
                        [safe_decode(r["name"]) or "", (safe_decode(r["email"]) or "").lower()])
            by_tag[t] = list(commits.values())
            for c in by_tag[t]:
                fnids_seen |= c["fnids"]
            if progress:
                progress(tags.index(t), len(tags), f"database: commits of {t}")

        missing = sorted(fnids_seen)
        for i in range(0, len(missing), HEALTH_CHUNK):
            part = missing[i:i + HEALTH_CHUNK]
            names.update({
                int(r["fnid"]): safe_decode(r["fname"])
                for r in cur.select_rows("m_file_name", ["fnid", "fname"], {"fnid": part})
            })
        catchall = str(_insights_cfg().get("catchall_section", "THE REST") or "")
        sec_name = {s.sec_id: s.name for s in sections if (not catchall or s.name != catchall) and isinstance(s.sec_id, int)}
        file_secs: dict[str, list[str]] = {}
        sec_ids = sorted(sec_name)
        if sec_ids:
            try:
                for i in range(0, len(missing), HEALTH_CHUNK):
                    part = missing[i:i + HEALTH_CHUNK]
                    for r in cur.select_rows("m_maintainer_file", ["fnid", "sec_id"], {"fnid": part, "sec_id": sec_ids}):
                        path = names.get(int(r["fnid"]))
                        if path is not None:
                            file_secs.setdefault(path, []).append(sec_name[int(r["sec_id"])])
            except Exception as exc:  # noqa: BLE001 - MAINTAINERS matches every path
                logger.debug("m_maintainer_file unavailable: %s", exc)
                file_secs = {}

        for t, rows in by_tag.items():
            for c in rows:
                c["files"] = sorted(names[f] for f in c.pop("fnids") if f in names)

        report = _aggregate_health(tag, tags, sections, matcher, lambda t: by_tag.get(t, []), file_secs, progress, precomputed=precomputed)
        return {**report, "source": "database"}


def subsystem_health(tag: str, window: int = 4, wait: float = 0.0) -> dict[str, Any]:
    _check_release(tag)
    window = max(1, min(int(window), 12))
    if tag == rs.LATEST_RELEASE:
        info = rs.latest_info()
        ck = f"{get_active_repo_name()}|health|{HEALTH_FORMAT}|latest|{info['prev_sha'] or '-'}|{info['sha']}|{window}|{rs.release_token()}"
        return _job(f"insights:{ck}", lambda p, _info=info: compute_subsystem_health(tag, window, p, latest=_info), ck, wait)
    tags = _health_window(tag, window)
    if rs.LATEST_RELEASE not in tags:
        try:
            sections, matcher = _maintainer_data(tag)
            hit = health_from_db(tag, tags, sections, matcher)
        except Exception as exc:  # noqa: BLE001 - Git answers
            logger.warning("subsystem health of %s from the database failed: %s", tag, exc)
            hit = None
        if hit is not None:
            return {"state": "done", "result": hit}
    sha = rs.tag_commits()[tag]
    # The release membership token separates a report built from the database from one built from Git
    ck = f"{get_active_repo_name()}|health|{HEALTH_FORMAT}|{sha}|{window}|{rs.release_token()}"
    return _job(f"insights:{ck}", lambda p: compute_subsystem_health(tag, window, p), ck, wait)
