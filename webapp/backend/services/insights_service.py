"""webapp/backend/services/insights_service.py - Release reports (webapp/API.md section 4.10):
release statistics (H6), merge windows (H5) and subsystem health (H7).

Every report is built from Git through the release index's per-release passes (section 2.17) and
cached on disk per (repository, release commit, parameters): a released tag never changes.
"""
from __future__ import annotations

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
from webapp.backend.config import RepoScopedCache, get_active_repo_name, get_repo_dir
from webapp.backend.services import release_service as rs
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs

logger = logging.getLogger(__name__)

REPORT_FORMAT = 2  # 2: source, first-timers only for mainline, identities once per commit
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

def author_first_seen(progress: Callable | None = None, family: str = rs.KERNEL_FAMILY) -> dict[str, int]:
    """identity -> time of its first commit on the mainline chain of a tag family (`""`: the kernel scheme; one
    pass over history, cached per head)."""
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


def _tag_meta(tag: str) -> dict[str, Any]:
    """{prev, days, rc_count} of a release: tag metadata, read from Git (milliseconds)."""
    prev_name = rs.previous(tag)
    tags = rs.tag_commits()
    days = None
    if prev_name:
        try:
            at = {t: int((rs._git(["log", "-1", "--format=%ct", tags[t]]) or "").strip()) for t in (tag, prev_name)}
            days = round((at[tag] - at[prev_name]) / 86400)
        except (ValueError, KeyError):
            days = None
    series = rs.parse_tag(tag)["series"]
    rc_count = sum(1 for t in tags if (p := rs.parse_tag(t)) and p["series"] == series and p["pre"])
    return {"prev": prev_name, "days": days, "rc_count": rc_count}


def _report(tag: str, source: str, *, commits: int, merges: int, diffstat: tuple[int | None, int, int],
            authors: Counter, roles: dict[str, Counter], names: dict[str, tuple[str, str]],
            employers: dict[str, dict[str, Any]], first_timers: list[str] | None) -> dict[str, Any]:
    """The release stats answer (webapp/API.md section 4.10) from identity-level counts of either source.
    first_timers None: not applicable (a stable update's commits are backports of mainline commits)."""
    meta = _tag_meta(tag)
    files, added, removed = diffstat
    return {
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


def _new_employers() -> dict[str, dict[str, Any]]:
    return defaultdict(lambda: {"commits": 0, "authors": set()})


def compute_release_stats(tag: str, progress: Callable | None = None) -> dict[str, Any]:
    """Release stats from Git: the release's commit pass, `git rev-list --count --merges`, `git diff --shortstat`."""
    prev, sha = rs.range_of(tag)
    commits = _release_pass(tag)
    rng = [sha] + ([f"^{prev}"] if prev else [])
    merges = int((_git(["rev-list", "--count", "--merges", *rng]) or "0").strip() or 0)
    stat = _git(["diff", "--shortstat", prev, sha]) if prev else ""
    m_files = re.search(r"(\d+) files? changed", stat)
    m_add = re.search(r"(\d+) insertions?", stat)
    m_del = re.search(r"(\d+) deletions?", stat)
    p = rs.parse_tag(tag)
    mainline = p["kind"] == "mainline"
    first_seen = author_first_seen(progress, p["family"]) if mainline else {}
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
    first_timers = [k for k, t in earliest.items() if k in first_seen and first_seen[k] >= t] if mainline else None
    return _report(tag, "git", commits=len(commits), merges=merges,
                   diffstat=(int(m_files.group(1)) if m_files else None, int(m_add.group(1)) if m_add else 0,
                             int(m_del.group(1)) if m_del else 0),
                   authors=authors, roles=roles, names=names, employers=employers, first_timers=first_timers)


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
            seg = {
                _text(r["tag"]): r
                for r in cur.select_rows(
                    "m_release_segment",
                    ["segment_id", "tag", "tag_hash", "base_tag", "base_hash", "diff_base_tag", "format", "commits",
                     "merges", "files_changed", "lines_added", "lines_removed"],
                    {"repo_id": repo_id, "tag": members},
                )
            }
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
                (int(r["person_id"]), int(r["role_type"]), int(r["n"]))
                for r in cur.select_rows(
                    "m_release_person",
                    {"person_id": "person_id", "role_type": "role_type", "n": Agg("SUM", "commits")},
                    {"segment_id": ids},
                    group_by=["person_id", "role_type"],
                )
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
                firsts: list[tuple[int, int]] = []
                for i in range(0, len(aliases), 1000):
                    part = aliases[i:i + 1000]
                    firsts += [
                        (int(r["person_id"]), int(r["segment_id"]))
                        for r in cur.select_rows(
                            "m_person_first_release", ["person_id", "segment_id"], {"repo_id": repo_id, "person_id": part}
                        )
                    ]
                sids = sorted({s for _, s in firsts})
                seg_tag: dict[int, str] = {}
                for i in range(0, len(sids), 1000):
                    part = sids[i:i + 1000]
                    seg_tag.update({
                        int(r["segment_id"]): _text(r["tag"])
                        for r in cur.select_rows("m_release_segment", ["segment_id", "tag"], {"segment_id": part})
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


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")


def _check_release(tag: str) -> None:
    p = rs.parse_tag(tag)
    if p is None or tag not in rs.tag_commits():
        raise HTTPException(status_code=404, detail=f"Not a release of this repository: {tag}")


def _stats_key(tag: str) -> str:
    return f"{get_active_repo_name()}|stats|{REPORT_FORMAT}|{rs.tag_commits()[tag]}"


def release_stats(tag: str, wait: float = 0.0) -> dict[str, Any]:
    """Job state of a release's stats: from the parser's tables when they cover it (answered at once),
    else the Git job (202 while it builds, cached on disk per tag commit)."""
    _check_release(tag)
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
        hit = db_release_stats(tag) if rs.parse_tag(tag) else None
        if hit is None:
            hit = _DISK.get(_stats_key(tag))
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
    re.compile(r"^Merge branch '([^']+)'(?: into \S+)?$"),
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


def compute_merge_window(tag: str, progress: Callable | None = None) -> dict[str, Any]:
    prev, sha = rs.range_of(tag)
    merges = rs.merges_now(tag)
    tags = rs.tag_commits()
    p = rs.parse_tag(tag)
    rc1 = next((t for t in tags if (q := rs.parse_tag(t)) and q["series"] == p["series"] and q["pre"] == 1), None)
    prev_name = rs.previous(tag)
    start = int(rs._git(["log", "-1", "--format=%ct", prev], timeout=10).strip()) if prev else 0
    rc1_at = int(rs._git(["log", "-1", "--format=%ct", tags[rc1]], timeout=10).strip()) if rc1 else None
    trees = _tree_index(tag)
    from webapp.backend.services.commit_facts import commit_ids
    ids = commit_ids([m["sha"] for m in merges])
    threads = _pull_threads(list(ids.values()))
    pulls = []
    total = len(merges)
    for i, m in enumerate(merges):
        if progress and i % 20 == 0:
            progress(i, total, "pulls")
        parents = [x for x in m["parents"] if _FULL.match(x)]
        if len(parents) < 2:
            continue
        count = int((_git(["rev-list", "--count", "--no-merges", parents[1], f"^{parents[0]}"], timeout=60) or "0").strip() or 0)
        dirs = []
        try:
            for line in _git(["diff", "--dirstat=files,3,cumulative", parents[0], m["sha"]], timeout=60).splitlines()[:8]:
                pct, _, d = line.strip().partition("% ")
                if d:
                    dirs.append({"dir": d.strip(), "percent": float(pct)})
        except RuntimeError:
            pass
        pull = parse_pull(m["subject"])
        sec = match_tree(trees, pull["url"]) if pull["url"] else None
        day = round((m["date"] - start) / 86400, 1) if start else None
        thread = threads.get(ids.get(m["sha"]))
        pulls.append({
            "merge": m["sha"], "parents": parents, "date": m["date"], "day": day,
            "after_rc1": bool(rc1_at and m["date"] > rc1_at), "subject": m["subject"],
            "source_url": pull["url"], "ref": pull["ref"], "sender": m["committer"] if not pull["url"] else _sender(m["body"]),
            "commits": count, "dirs": dirs, "section": sec, "thread": thread, "body": m["body"][:3000],
        })
    by_day: Counter = Counter()
    for pl in pulls:
        if pl["day"] is not None:
            by_day[int(pl["day"])] += pl["commits"]
    return {
        "release": tag, "prev": prev_name, "rc1": rc1, "rc1_day": round((rc1_at - start) / 86400, 1) if rc1_at and start else None,
        "pulls": pulls,
        "totals": {"pulls": len(pulls), "commits": sum(x["commits"] for x in pulls),
                   "before_rc1": sum(1 for x in pulls if not x["after_rc1"]),
                   "by_day": [{"day": d, "commits": by_day[d]} for d in sorted(by_day)]},
    }


def _sender(body: str) -> str:
    m = re.search(r"Pull (.+?) (?:updates|fixes|changes)? ?from (.+?):", body or "")
    if m:
        return m.group(2).strip()
    m = re.search(r"^Pull .* from ([^:\n]+)", body or "", re.M)
    return m.group(1).strip() if m else ""


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
    if not rs.parse_tag(tag)["final"]:
        raise HTTPException(status_code=400, detail="A merge window belongs to a release (vX.Y, <prefix>X.Y), not to an -rc or a stable update.")
    sha = rs.tag_commits()[tag]
    ck = f"{get_active_repo_name()}|mw|{REPORT_FORMAT}|{sha}"
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


def flags_for(sec: dict[str, Any]) -> list[str]:
    f = []
    status = (sec.get("status") or "").lower()
    if sec["commits"] == 0:
        f.append("no_activity")
    if status.startswith(("orphan", "obsolete")) and sec["commits"] >= 5:
        f.append("orphan_active")
    if sec["commits"] >= 10 and sec["bus_factor"] == 1:
        f.append("single_author")
    if sec["commits"] >= 10 and sec["review_coverage"] < 0.10:
        f.append("no_review")
    if sec["commits"] and sec["maintainers"] and not any(m.get("last_active") for m in sec["maintainers"]):
        f.append("silent_maintainers")
    return f


HEALTH_ROLES = {CommitRole.REVIEWED_BY: "reviewed_by", CommitRole.ACKED_BY: "acked_by",
                CommitRole.SIGNED_OFF_BY: "signed_off_by", CommitRole.TESTED_BY: "tested_by"}
HEALTH_CHUNK = 2000


def _health_window(tag: str, window: int) -> list[str]:
    family = rs.family_of(tag)
    finals = [t for t in rs.releases("mainline") if (p := rs.parse_tag(t))["final"] and p["family"] == family
              and rs.release_key(t) <= rs.release_key(tag)]
    return finals[-window:]


def _aggregate_health(tag: str, tags: list[str], sections: list[Any], matcher: Any,
                      commits: Callable[[str], Any], file_secs: dict[str, list[str]] | None = None,
                      progress: Callable | None = None) -> dict[str, Any]:
    """The report rows from each release's commits ({date, author, email, trailers, files}); shared by the Git
    and database paths, so both build exactly the same rows. `file_secs` may hold the sections of some paths
    already (path -> section names, THE REST left out); the others are matched with MAINTAINERS."""
    from parser.maintainer_ast.maintainer_types import MaintainerRole
    people: dict[str, dict[str, int]] = defaultdict(dict)  # section -> identity -> last active time
    author_counts: dict[str, Counter] = defaultdict(Counter)
    per_release: dict[str, Counter] = defaultdict(Counter)
    reviewed: Counter = Counter()
    file_secs = dict(file_secs or {})
    for ti, t in enumerate(tags):
        if progress:
            progress(ti, len(tags), f"commits of {t}")
        for c in commits(t):
            secs: set[str] = set()
            for f in c["files"]:
                if f not in file_secs:
                    file_secs[f] = [s.name for s in matcher.match_file(f) if s.name != "THE REST"]
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
        if name == "THE REST":
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


def _maintainer_data(tag: str) -> tuple[list[Any], Any]:
    from webapp.backend.services.maintainer_service import MaintainerService
    try:
        return MaintainerService().get_maintainer_data(tag)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"MAINTAINERS of {tag} unavailable: {exc}") from exc


def compute_subsystem_health(tag: str, window: int, progress: Callable | None = None) -> dict[str, Any]:
    """The subsystem-health report: from the database when it covers every release of the window
    (health_from_db()), else one `git log --name-only` pass per release (`source: "git"`)."""
    tags = _health_window(tag, window)
    sections, matcher = _maintainer_data(tag)
    try:
        hit = health_from_db(tag, tags, sections, matcher, progress)
    except Exception as exc:  # noqa: BLE001 - Git answers
        logger.warning("subsystem health of %s from the database failed: %s", tag, exc)
        hit = None
    if hit is not None:
        return hit
    return {**_aggregate_health(tag, tags, sections, matcher, _release_pass, progress=progress), "source": "git"}


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

    Commits of each release's segments without a second parent (`git log --no-merges`), their authors, their
    m_commit_file paths, new and old name (`--no-renames` lists both sides of a rename) and their trailers
    (m_bridge_commit_person: reviewed, acked, signed-off, tested), HEALTH_CHUNK commits per query. A path's
    sections come from m_maintainer_file for the tag's sections when it has rows there (files of the tag's
    tree), else from MAINTAINERS like the Git path (files no longer in the tree)."""
    from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id, safe_decode
    from webapp.backend.database.pool import get_reader
    with get_reader() as cur:
        cov = health_coverage(cur, tags)
        if cov is None:
            return None
        network_id = get_active_network_id(cur)
        repo_id = get_active_repo_id(cur)
        names: dict[int, str] = {}
        by_tag: dict[str, list[dict[str, Any]]] = {}
        fnids_seen: set[int] = set()
        for t in tags:
            segs = cov[t]
            source, joins, where = _release_commit_source(network_id, repo_id, segs)
            fetched_rows = cur.select_rows(
                source,
                {"commit_id": "c.commit_id", "author_date": "c.author_date", "committer_date": "c.committer_date",
                 "name": "p.name", "email": "p.email"},
                where,
                joins=[*joins, Join(("m_maintainer_person", "p"), {"p.person_id": "c.author_id"}, "left")],
            )
            # In `git log` order (newest commit first; the parser registers parents before children), so ties
            # among top authors break the same way as on the Git path
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
        # Sections of the tag's tree files from D01's mapping (THE REST left out)
        sec_name = {s.sec_id: s.name for s in sections if s.name != "THE REST" and isinstance(s.sec_id, int)}
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
    report = _aggregate_health(tag, tags, sections, matcher, lambda t: by_tag[t], file_secs, progress)
    return {**report, "source": "database"}


def subsystem_health(tag: str, window: int = 4, wait: float = 0.0) -> dict[str, Any]:
    _check_release(tag)
    window = max(1, min(int(window), 12))
    sha = rs.tag_commits()[tag]
    # The release membership token separates a report built from the database from one built from Git
    ck = f"{get_active_repo_name()}|health|{HEALTH_FORMAT}|{sha}|{window}|{rs.release_token()}"
    return _job(f"insights:{ck}", lambda p: compute_subsystem_health(tag, window, p), ck, wait)
