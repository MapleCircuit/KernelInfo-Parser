"""webapp/backend/services/release_service.py - Release index (webapp/API.md section 2.17).

Shared answers about releases, all computed from Git (git-only versions included):
- ordering (`release_key`), the previous release (`previous`) and a release's commit range (`range_of`);
- the first tag containing a commit (`first_containing`, `release_lookup`): `git describe --contains`
  costs ~14 s per commit on a clone without a commit-graph, so the answer comes from the parser's
  `m_commit_release` rows (every commit of every release segment, core/API.md section 10) when they
  cover every release tag (`db_covers_all`), otherwise from an index mapping every commit of the mainline
  and stable tag chains to the first tag introducing it (built once by a background job, section 2.15,
  stored in SQLite under the disk cache, section 2.14, extended incrementally when tags are fetched);
- the release segments of a range (`walk_segments`) and their commits (`segment_commits`);
- a per-release commit pass (`release_commits`: authors, trailers, files) and the merges of a release
  (`merges`), cached on disk per (repository, tag commit): immutable.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fastapi import HTTPException

from parser.git_ast.git_commit_parser import iter_trailers
from parser.git_ast.git_types import CommitRole
from parser.git_ast.release_order import (  # noqa: F401 - re-exported
    KERNEL_FAMILY,
    family_of,
    final_of,
    parse_tag,
    previous_in,
    release_key,
    release_ranges,
    release_tag_names,
    series_name,
)
from cache_engine import BYTES, INT, TEXT, CacheError, CacheSpace, In, StoreSpec, get_cache_engine
from webapp.backend.config import RepoScopedCache, get_active_repo_name, get_repo_dir
from webapp.backend.database.helpers import get_active_repo_id
from db_engine.spec import Agg, Join
from webapp.backend.database.pool import get_reader
from webapp.backend.security.git import is_safe_rev
from webapp.backend.services.disk_cache import DiskCache
from webapp.backend.services.jobs import jobs
from webapp.backend.services.sync_service import register_refs_changed

logger = logging.getLogger(__name__)

INDEX_FORMAT = 1
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX = re.compile(r"^[0-9a-f]{4,40}$")
RANGE_WORKERS = 4
PASS_FORMAT = 3  # 3: trailers without a name or without <> (iter_trailers)

# --------------------------------------------------------------------------- tag names and ordering
# parse_tag, release_key, series_name, previous_in, final_of: parser/git_ast/release_order.py (shared with
# the parser's release statistics step, so both cut the history into the same ranges)


def parse_describe(text: str) -> str | None:
    """`v3.1-rc1~12^2~3` -> `v3.1-rc1`; `tags/v3.0` -> `v3.0`; `undefined` -> None."""
    s = str(text or "").strip()
    if not s or s == "undefined":
        return None
    if s.startswith("tags/"):
        s = s[5:]
    return re.split(r"[~^]", s, maxsplit=1)[0] or None


# --------------------------------------------------------------------------- git helpers


def _git(args: list[str], timeout: float = 60.0) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(get_repo_dir()), *args], capture_output=True, text=True,
                             timeout=timeout, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git %s failed: %s", args[:2], exc)
        return None
    return out.stdout if out.returncode == 0 else None


_TAGS_CACHE: RepoScopedCache = RepoScopedCache()
_TAGS_LOCK = threading.Lock()
LATEST_RELEASE = "latest"
_LATEST_CACHE: RepoScopedCache = RepoScopedCache(maxsize=4)


def tag_commits(refresh: bool = False) -> dict[str, str]:
    """Release tags of the repository (`release_tag_names()`) -> their commit SHA (annotated tags peeled). Cached
    until the `git` sync token changes (clear_caches)."""
    if not refresh:
        hit = _TAGS_CACHE.get("tags")
        if hit is not None:
            return hit
    with _TAGS_LOCK:
        out = _git(["for-each-ref",
                    "--format=%(refname:short)%09%(objectname)%09%(objecttype)%09%(*objectname)%09%(*objecttype)%09%(committerdate:unix)%09%(*committerdate:unix)",
                    "refs/tags"], timeout=30)
        tags: dict[str, str] = {}
        raw_dates: dict[str, int] = {}
        for line in (out or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name, obj, otype, peeled, ptype = parts[:5]
            sha, kind = (peeled, ptype) if peeled else (obj, otype)
            # Only tags of commits are releases (v2.6.11 tags a tree: history starts at v2.6.12-rc2)
            if kind == "commit" and parse_tag(name) is not None and _FULL_SHA.match(sha):
                tags[name] = sha
                ts_raw = parts[6] if peeled and len(parts) > 6 and parts[6].strip() else (parts[5] if len(parts) > 5 and parts[5].strip() else "")
                try:
                    raw_dates[name] = int(ts_raw)
                except (ValueError, TypeError):
                    raw_dates[name] = 0
        # The kernel scheme's tags when the repository has any, else every prefixed release (man-pages-X.Y, foo-1.2)
        tags = {name: tags[name] for name in release_tag_names(tags)}
        dates = {name: raw_dates.get(name, 0) for name in tags}
        if out is not None:
            _TAGS_CACHE["tags"] = tags
            _TAGS_CACHE["dates"] = dates
        return tags


def tag_dates() -> dict[str, int]:
    """Release tags -> their unix commit timestamp. Cached alongside tag_commits()."""
    if "dates" not in _TAGS_CACHE:
        tag_commits(refresh=True)
    return dict(_TAGS_CACHE.get("dates") or {})


def releases(kind: str = "all") -> list[str]:
    """Release tags sorted by `release_key`: kind "mainline" (releases and candidates), "stable", or "all"."""
    tags = tag_commits()
    names = [t for t in tags if kind == "all" or parse_tag(t)["kind"] == kind]
    return sorted(names, key=release_key)


def _is_ancestor(ancestor_sha: str, desc_sha: str) -> bool:
    return ancestor_sha == desc_sha or _git(["merge-base", "--is-ancestor", ancestor_sha, desc_sha], timeout=10) is not None


def _ref_filter_from_candidates(flag: str, sha: str, candidates: list[str], chunk: int = 500) -> set[str] | None:
    """Single-command `git for-each-ref` ancestry filter (`--merged=<sha>` or `--contains=<sha>`) over `candidates`."""
    if not _FULL_SHA.match(sha or ""):
        return None
    if not candidates:
        return set()
    allowed = set(candidates)
    out_set: set[str] = set()
    for i in range(0, len(candidates), chunk):
        part = candidates[i:i + chunk]
        out = _git(
            ["for-each-ref", f"--{flag}={sha}", "--format=%(refname:short)", *(f"refs/tags/{t}" for t in part)],
            timeout=30,
        )
        if out is None:
            return None
        for line in out.splitlines():
            name = line.strip()
            if name in allowed:
                out_set.add(name)
    return out_set


def _merged_from_candidates(sha: str, candidates: list[str]) -> set[str] | None:
    return _ref_filter_from_candidates("merged", sha, candidates)


def _contains_from_candidates(sha: str, candidates: list[str]) -> set[str] | None:
    return _ref_filter_from_candidates("contains", sha, candidates)


def latest_branch() -> str:
    """Default branch name of the active repository that resolves to a commit, else 'HEAD'."""
    from core.config import detect_default_branch
    from webapp.backend.config import get_active_repository

    repo = get_active_repository()
    configured = (repo.get("default_branch") or "").strip()
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
        if "/" not in configured:
            candidates.append(f"origin/{configured}")
    try:
        detected = detect_default_branch(get_repo_dir())
        if detected and detected not in candidates:
            candidates.append(detected)
    except Exception:  # noqa: BLE001
        pass
    for fb in ("master", "main", "origin/master", "origin/main", "HEAD"):
        if fb not in candidates:
            candidates.append(fb)
    for cand in candidates:
        if resolve_sha(cand):
            return cand
    return ""


def latest_info() -> dict[str, Any]:
    """Current unreleased cycle ('latest'): {branch, sha, prev, prev_sha, rc1, rc1_sha, rc_count}."""
    branch = latest_branch()
    sha = resolve_sha(branch) if branch else None
    if not sha:
        raise HTTPException(status_code=404, detail="Repository has no commits yet.")
    tags = tag_commits()
    cached = _LATEST_CACHE.get("latest")
    if cached is not None and cached[0] is tags and cached[1] == branch and cached[2] == sha:
        return dict(cached[3])
    mainline = releases("mainline")
    finals = [t for t in mainline if parse_tag(t)["final"]]
    merged_finals = _merged_from_candidates(sha, finals)
    prev_name = None
    if merged_finals is not None:
        if merged_finals:
            prev_name = max(merged_finals, key=release_key)
    else:
        for t in reversed(finals):
            if _is_ancestor(tags[t], sha):
                prev_name = t
                break
    prev_sha = tags.get(prev_name) if prev_name else None
    if prev_name:
        prev_family = parse_tag(prev_name)["family"]
    elif mainline:
        prev_family = parse_tag(mainline[0])["family"]
    else:
        prev_family = KERNEL_FAMILY
    prev_key = release_key(prev_name) if prev_name else None
    cand_rcs = [
        t for t in mainline
        if (p := parse_tag(t)) and p["pre"] and p["family"] == prev_family
        and (prev_key is None or release_key(t) > prev_key)
    ]
    merged_rcs = _merged_from_candidates(sha, cand_rcs)
    if merged_rcs is not None:
        post_rcs = [t for t in cand_rcs if t in merged_rcs]
    else:
        post_rcs = [t for t in cand_rcs if _is_ancestor(tags[t], sha)]
    try:
        from webapp.backend.config import get_active_insights_config
        milestone_rc = int(get_active_insights_config().get("milestone_rc", 1))
    except Exception:
        milestone_rc = 1
    if milestone_rc > 0:
        rc1 = next((t for t in post_rcs if parse_tag(t)["pre"] == milestone_rc), None)
    else:
        rc1 = None
    info = {
        "branch": branch,
        "sha": sha,
        "prev": prev_name,
        "prev_sha": prev_sha,
        "rc1": rc1,
        "rc1_sha": tags.get(rc1) if rc1 else None,
        "rc_count": len(post_rcs),
    }
    _LATEST_CACHE["latest"] = (tags, branch, sha, info)
    return dict(info)


def previous(tag: str) -> str | None:
    if tag == LATEST_RELEASE:
        return latest_info()["prev"]
    return previous_in(tag, list(tag_commits()))


def range_of(tag: str) -> tuple[str | None, str]:
    """(commit of the previous release or None, commit of `tag`); 404 for an unknown release tag."""
    if tag == LATEST_RELEASE:
        info = latest_info()
        return info["prev_sha"], info["sha"]
    tags = tag_commits()
    sha = tags.get(tag)
    if not sha:
        raise HTTPException(status_code=404, detail=f"Unknown release tag: {tag}")
    prev = previous_in(tag, list(tags))
    return (tags.get(prev) if prev else None), sha


# --------------------------------------------------------------------------- first containing tag index

NAMESPACE = "release-index"
STORES = (
    StoreSpec("tags", (INT("idx"), TEXT("name", null=True), TEXT("sha", null=True), TEXT("chain", null=True)),
              key=("idx",), unique=(("name",),)),
    StoreSpec("first_tag", (BYTES("sha"), INT("chain"), INT("tag_idx", null=True)), key=("sha", "chain"), clustered=True),
)
CHAIN_MAINLINE = 0
CHAIN_STABLE = 1


class ReleaseIndex:
    """commit -> first mainline tag and first stable tag introducing it, per repository (cache_engine space)."""

    @staticmethod
    def space_name() -> str:
        # Versioned by INDEX_FORMAT (bump it when the stored rows change meaning), not by the module's
        # source: an unrelated edit must not throw away an index that took minutes to build
        return f"{get_active_repo_name()}-v{INDEX_FORMAT}"

    def space(self) -> CacheSpace:
        """The release space of the active repository (stores `tags`, `first_tag`; opened on demand)."""
        return get_cache_engine().open(NAMESPACE, self.space_name(), STORES)

    def _meta(self, key: str) -> str | None:
        return self.space().get_meta(key)

    def built_tags(self) -> dict[str, str]:
        return {name: sha for name, sha in self.space().store("tags").scan(columns=("name", "sha"))}

    def is_current(self) -> bool:
        tags = tag_commits()
        built = self.built_tags()
        return bool(built) and all(built.get(t) == s for t, s in tags.items())

    def lookup(self, sha: str) -> dict[str, str | None] | None:
        """{mainline: tag, stable: tag} for a full SHA, or None when the index does not cover it yet."""
        if not _FULL_SHA.match(sha or ""):
            return None
        space = self.space()
        key = bytes.fromhex(sha)
        rows = space.store("first_tag").get_many([(key, CHAIN_MAINLINE), (key, CHAIN_STABLE)])
        tags = space.store("tags")
        out: dict[str, str | None] = {"mainline": None, "stable": None}
        for (_sha, chain), (_s, _c, tag_idx) in rows.items():
            hit = tags.get(tag_idx)
            if hit:
                out["mainline" if chain == CHAIN_MAINLINE else "stable"] = hit[1]
        return out

    def lookup_many(self, shas: list[str]) -> dict[str, dict[str, str | None]]:
        """`lookup()` of many full SHAs at once (500 per query); SHAs the index does not cover are omitted."""
        keys = list(dict.fromkeys(s for s in shas if _FULL_SHA.match(s or "")))
        out: dict[str, dict[str, str | None]] = {}
        space = self.space()
        names = {idx: name for idx, name in space.store("tags").scan(columns=("idx", "name"))}
        part = [bytes.fromhex(s) for s in keys]
        for sha, chain, tag_idx in space.store("first_tag").scan(In("sha", part)):
            hit = out.setdefault(sha.hex(), {"mainline": None, "stable": None})
            hit["mainline" if chain == CHAIN_MAINLINE else "stable"] = names.get(tag_idx)
        return out

    def stats(self) -> dict[str, Any]:
        try:
            return {
                "tags": self.space().store("tags").count(),
                "built_at": float(self._meta("built_at") or 0) or None,
            }
        except CacheError:
            return {"tags": 0, "built_at": None}

    def build(self, progress: Callable[..., None] | None = None) -> dict[str, Any]:
        """Index every release range not indexed yet (incremental: only new tags cost anything)."""
        tags = tag_commits(refresh=True)
        built = self.built_tags()
        space = self.space()
        tags_store, first_tag = space.store("tags"), space.store("first_tag")
        # A tag that moved (re-tagged) invalidates the index: start over
        if any(name in tags and tags[name] != sha for name, sha in built.items()):
            space.clear()
            built = {}
        # Each range ends at a tag and starts at the tag before it on its chain (release_ranges: the previous
        # mainline tag of its family, the previous update of its stable series)
        ranges = [r for r in release_ranges(tags) if r["kind"] in ("mainline", "stable")]
        jobs_list: list[tuple[str, str | None, int]] = [
            (r["tag"], r["segment_base"], CHAIN_MAINLINE if r["kind"] == "mainline" else CHAIN_STABLE)
            for r in sorted(ranges, key=lambda r: r["kind"] != "mainline") if r["tag"] not in built]
        total = len(jobs_list)
        if progress:
            progress(0, total, "release ranges")
        next_idx = (tags_store.max("idx") or 0) + 1
        idx_of = {name: i for i, name in tags_store.scan(columns=("idx", "name"))}
        for t, _p, chain in jobs_list:
            if t not in idx_of:
                idx_of[t] = next_idx
                next_idx += 1

        def run(item: tuple[str, str | None, int]) -> tuple[str, int, list[str]]:
            t, p, chain = item
            args = ["rev-list", tags[t]] + ([f"^{tags[p]}"] if p and p in tags else [])
            out = _git(args, timeout=600) or ""
            return t, chain, out.split()

        done = 0
        with ThreadPoolExecutor(max_workers=RANGE_WORKERS) as pool:
            # Ranges are computed in parallel; rows are written in release order, so the first tag of
            # a commit reachable from two ranges (a stable chain starting below its parent) is the earliest
            for t, chain, shas in pool.map(run, jobs_list):
                with space.batch():
                    tags_store.put((idx_of[t], t, tags[t], "stable" if chain else "mainline"))
                    first_tag.put_many(
                        ((bytes.fromhex(s), chain, idx_of[t]) for s in shas if _FULL_SHA.match(s)), replace=False)
                done += 1
                if progress:
                    progress(done, total, f"{t}: {len(shas)} commits")
        space.set_meta("built_at", str(time.time()))
        return {"tags": len(tags), "ranges_indexed": total}


release_index = ReleaseIndex()


def _index_job_key() -> str:
    return f"release-index:{get_active_repo_name()}"


# --------------------------------------------------------------------------- release membership in the database
# m_release_segment.chain / m_commit_release.chain (parser/git_ast/release_stats.py)
DB_CHAIN_MAINLINE = 1
DB_CHAIN_STABLE = 2
_DB_COVERAGE: RepoScopedCache = RepoScopedCache(maxsize=4)
_DB_COVERAGE_TTL = 60.0  # a "not covered" answer is checked again after this (release statistics run at parser start)


def _segment_rows(reader: Any) -> list[dict[str, Any]]:
    return list(reader.select_rows(
        "m_release_segment", ["segment_id", "tag", "tag_hash", "chain", "base_tag", "format"],
        {"repo_id": get_active_repo_id(reader)},
    ))


def db_covers_all() -> bool:
    """True when m_commit_release answers for every release tag: each tag of `tag_commits()` has a segment
    with its current commit, stored in the format that writes m_commit_release (release_stats.FORMAT)."""
    hit = _DB_COVERAGE.get("covers")
    if hit is not None and (hit[0] or time.monotonic() - hit[1] < _DB_COVERAGE_TTL):
        return hit[0]
    from parser.git_ast import release_stats
    tags = tag_commits()
    ok = False
    try:
        with get_reader() as cursor:
            stored = {_text(r["tag"]): (_hex(r["tag_hash"]), int(r["format"])) for r in _segment_rows(cursor)}
        ok = bool(tags) and all(stored.get(t) == (sha, release_stats.FORMAT) for t, sha in tags.items())
    except Exception as exc:  # noqa: BLE001 - an older database (no m_release_segment): not covered
        logger.debug("release segment coverage check failed: %s", exc)
    _DB_COVERAGE["covers"] = (ok, time.monotonic())
    return ok


def _text(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v or "")


def _hex(v: Any) -> str:
    return bytes(v).hex() if isinstance(v, (bytes, bytearray, memoryview)) else str(v or "")


def db_lookup(shas: list[str]) -> dict[str, dict[str, str | None]]:
    """{sha: {mainline: tag, stable: tag}} from m_commit_release for the given full SHAs (500 per query).
    A row counts only when its segment's tag still names the same commit (the tag did not move); SHAs
    without a row are omitted."""
    keys = list(dict.fromkeys(s for s in shas if _FULL_SHA.match(s or "")))
    out: dict[str, dict[str, str | None]] = {}
    if not keys:
        return out
    tags = tag_commits()
    try:
        with get_reader() as cursor:
            repo_id = get_active_repo_id(cursor)
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                for r in cursor.select_rows(
                    ("m_commit_release", "r"),
                    {"commit_hash": "r.commit_hash", "chain": "r.chain", "tag": "s.tag", "tag_hash": "s.tag_hash"},
                    {"r.repo_id": repo_id, "r.commit_hash": [bytes.fromhex(x) for x in part]},
                    joins=[Join(("m_release_segment", "s"), {"s.segment_id": "r.segment_id"})],
                ):
                    tag = _text(r["tag"])
                    if tags.get(tag) != _hex(r["tag_hash"]):
                        continue
                    hit = out.setdefault(_hex(r["commit_hash"]), {"mainline": None, "stable": None})
                    hit["mainline" if int(r["chain"]) == DB_CHAIN_MAINLINE else "stable"] = tag
    except Exception as exc:  # noqa: BLE001 - an older database: nothing from it
        logger.debug("m_commit_release lookup failed: %s", exc)
        return {}
    return out


def release_lookup(shas: list[str]) -> dict[str, dict[str, str | None]]:
    """{sha: {mainline, stable}} for the SHAs whose answer is known; a SHA on no release chain answers
    {mainline: None, stable: None}. Database first; the SQLite index when the database does not cover
    every tag (tags fetched since the last parse) and the index is current; SHAs neither can answer yet
    are omitted."""
    keys = list(dict.fromkeys(s for s in shas if _FULL_SHA.match(s or "")))
    if not keys:
        return {}
    if db_covers_all():
        found = db_lookup(keys)
        return {s: found.get(s) or {"mainline": None, "stable": None} for s in keys}
    if release_index.is_current():
        found = release_index.lookup_many(keys)
        return {s: found.get(s) or {"mainline": None, "stable": None} for s in keys}
    return db_lookup(keys)


def release_token() -> str:
    """Changes whenever the release membership answers may change (part of report cache keys)."""
    if db_covers_all():
        try:
            with get_reader() as cursor:
                row = cursor.select_one(
                    "m_release_segment", {"m": Agg("MAX", "segment_id"), "n": Agg("COUNT", "*")},
                    {"repo_id": get_active_repo_id(cursor)},
                ) or {}
            return f"db:{row.get('m')}:{row.get('n')}"
        except Exception:  # noqa: BLE001
            pass
    return f"index:{release_index.stats().get('built_at')}"


def walk_segments(from_tag: str | None, to_tag: str, limit: int = 200) -> list[int] | None:
    """segment_ids whose commits make up from_tag..to_tag on to_tag's chain: from to_tag's segment along
    `base_tag` until from_tag (from_tag=None: to the chain's first tag). None when from_tag is not reached
    within `limit` segments, a segment is missing or stale, or the database has no segments."""
    tags = tag_commits()
    try:
        with get_reader() as cursor:
            rows = {_text(r["tag"]): r for r in _segment_rows(cursor)}
    except Exception:  # noqa: BLE001
        return None
    out: list[int] = []
    tag = to_tag
    while tag and tag != from_tag:
        row = rows.get(tag)
        if row is None or tags.get(tag) != _hex(row["tag_hash"]) or len(out) >= limit:
            return None
        out.append(int(row["segment_id"]))
        tag = _text(row["base_tag"]) or None
    if from_tag is not None and tag != from_tag:
        return None
    return out


def segment_commits(segment_ids: list[int]) -> list[str]:
    """Full SHAs of every commit held by the given segments (m_commit_release)."""
    out: list[str] = []
    if not segment_ids:
        return out
    with get_reader() as reader:
        for i in range(0, len(segment_ids), 200):
            part = segment_ids[i:i + 200]
            out.extend(_hex(r["commit_hash"]) for r in reader.select_rows("m_commit_release", ["commit_hash"], {"segment_id": part}))
    return out


def ensure_release_index(wait: float = 0.0, require_sqlite: bool = False) -> dict[str, Any]:
    """Make sure release membership answers cover every tag: done at once when the database covers them
    (db_covers_all), otherwise submits the (incremental) SQLite build job. `require_sqlite`: callers that
    read the SQLite file itself (stable_service ATTACHes it). Returns the job state ({state: "done"} when
    current)."""
    if not require_sqlite and db_covers_all():
        return {"state": "done", "progress": {"done": 1, "total": 1, "note": "database"}}
    if release_index.is_current():
        return {"state": "done", "progress": {"done": 1, "total": 1, "note": "current"}}
    key = _index_job_key()
    state = jobs.get(key)
    if state and state.get("state") == "done":
        jobs.forget(key)  # an older build: the tags moved on since
    state = jobs.submit(key, lambda progress: release_index.build(progress))
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(key, wait) or state
    return state


_CONTAINS_CACHE: RepoScopedCache = RepoScopedCache(maxsize=50_000)


def resolve_sha(rev: str) -> str | None:
    """Full commit SHA of a validated revision (a SHA prefix of at least 4 hex digits, a tag, a branch)."""
    if not is_safe_rev(rev):
        return None
    out = _git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], timeout=10)
    sha = (out or "").strip()
    return sha if _FULL_SHA.match(sha) else None


def first_containing(sha: str, *, allow_slow: bool = False) -> dict[str, Any]:
    """The first tags containing a commit: {sha, first_tag, first_release, first_stable, released, indexed}.

    From the release index when it covers the commit; otherwise (index still building, or a commit on no
    release chain) `released: false` with the branches containing it, or — with `allow_slow` — the
    answer of `git describe --contains` (seconds per commit)."""
    full = sha if _FULL_SHA.match(sha or "") else resolve_sha(sha)
    if not full:
        raise HTTPException(status_code=404, detail=f"Unknown commit: {sha}")
    hit = _CONTAINS_CACHE.get(full)
    if hit is not None:
        return hit
    tags = tag_commits()
    names = set(tags)
    state = ensure_release_index()
    found = release_lookup([full]).get(full)
    out: dict[str, Any] = {"sha": full, "indexed": state.get("state") == "done"}
    first = (found or {}).get("mainline")
    stable = (found or {}).get("stable")
    if not first and not stable and allow_slow:
        first = parse_describe(_git(["describe", "--contains", "--match", "v[0-9]*", full], timeout=60) or "")
        if first and parse_tag(first) and parse_tag(first)["kind"] == "stable":
            stable, first = first, None
    if first or stable:
        out.update(released=True, first_tag=first, first_release=final_of(first, names) if first else None,
                   first_stable=stable)
        _CONTAINS_CACHE[full] = out  # permanent: tags never move
        return out
    out.update(released=False, first_tag=None, first_release=None, first_stable=None)
    if out["indexed"]:
        out["branches"] = branches_containing(full)
    else:
        out["index_progress"] = state.get("progress")
    return out


def branches_containing(sha: str, limit: int = 20) -> list[str]:
    out = _git(["branch", "-a", "--format=%(refname:short)", "--contains", sha], timeout=60) or ""
    names = [b.strip() for b in out.splitlines() if b.strip() and not b.strip().endswith("/HEAD")]
    return names[:limit]


def _pick_best_tag(candidates: list[str]) -> str | None:
    if not candidates:
        return None
    return max(candidates, key=lambda t: (bool((parse_tag(t) or {}).get("final")), release_key(t)))


def nearest_ancestor_tag(sha: str) -> str | None:
    """Newest release tag reachable as an ancestor of `sha`, or None when none exists."""
    full = sha if _FULL_SHA.match(sha or "") else resolve_sha(sha)
    if not full:
        return None
    tags = tag_commits()
    if not tags:
        return None
    exact = [t for t, t_sha in tags.items() if t_sha == full]
    if exact:
        return _pick_best_tag(exact)

    # 1. Fast backward walk via `git describe` restricted to this repository's tag family pattern(s)
    families = {family_of(t) for t in tags}
    if KERNEL_FAMILY in families:
        match_args = ["--match", "v[0-9]*"]
    else:
        match_args = []
        for fam in sorted(f for f in families if f)[:8]:
            match_args.extend(["--match", f"{fam}[0-9]*"])
    if match_args:
        desc = _git(["describe", "--tags", "--abbrev=0", *match_args, full], timeout=10)
        cand = parse_describe(desc or "")
        if cand and cand in tags:
            return cand

    # 2. Fallback: first-parent decorated history (skips non-release tags)
    deco = _git(["log", "--first-parent", "--decorate=short", "--simplify-by-decoration", "--format=%D", "-n", "100", full], timeout=10) or ""
    for line in deco.splitlines():
        found = [part[5:].strip() for part in line.split(", ") if part.startswith("tag: ") and part[5:].strip() in tags]
        if found:
            return _pick_best_tag(found)

    # 3. Fallback: single-command ancestry check over mainline final releases, then later tags in that series/next cycle
    finals = [t for t in releases("mainline") if parse_tag(t)["final"]]
    merged_finals = _merged_from_candidates(full, finals)
    if merged_finals is None:
        merged_finals = {t for t in finals if _is_ancestor(tags[t], full)}
    if merged_finals:
        base = max(merged_finals, key=release_key)
        base_key = release_key(base)
        next_finals = [release_key(t) for t in finals if release_key(t) > base_key]
        next_final_key = min(next_finals) if next_finals else None
        cand_tags = [
            t for t in tags
            if release_key(t) >= base_key and (next_final_key is None or release_key(t) < next_final_key)
        ]
        matched = _merged_from_candidates(full, cand_tags)
        if matched is None:
            matched = {t for t in cand_tags if _is_ancestor(tags[t], full)}
        if matched:
            best = max(matched, key=release_key)
            same_commit = [t for t in matched if tags.get(t) == tags.get(best)]
            return _pick_best_tag(same_commit) or best
        return base

    # Repo may only have pre-release / non-final tags before any final tag
    non_finals = [t for t in releases("all") if not (parse_tag(t) or {}).get("final")]
    merged_other = _merged_from_candidates(full, non_finals)
    if merged_other is None:
        merged_other = {t for t in non_finals if _is_ancestor(tags[t], full)}
    if merged_other:
        best = max(merged_other, key=release_key)
        same_commit = [t for t in merged_other if tags.get(t) == tags.get(best)]
        return _pick_best_tag(same_commit) or best
    return None


def _series_tag_for(tag: str | None, tags: dict[str, str]) -> str | None:
    if not tag:
        return None
    p = parse_tag(tag)
    if not p:
        return None
    if p["kind"] == "next":
        return tag
    if p["family"] != KERNEL_FAMILY:
        hits = [t for t in tags if (q := parse_tag(t)) is not None and q["final"] and q["series"] == p["series"]]
        if hits:
            return min(hits)
    return series_name(p["series"])


def _final_for(tag: str | None, tags: dict[str, str]) -> str | None:
    if not tag:
        return None
    p = parse_tag(tag)
    if not p:
        return None
    if p["final"]:
        return tag
    if p["kind"] == "stable":
        s = _series_tag_for(tag, tags)
        return s if s in tags else None
    return final_of(tag, set(tags))


def _safe_latest_info() -> dict[str, Any] | None:
    try:
        return latest_info()
    except HTTPException:
        return None


def _is_in_latest_cycle(sha: str, info: dict[str, Any] | None, tags: dict[str, str]) -> bool:
    if not info:
        return False
    latest_sha = info.get("sha")
    if not latest_sha or not _is_ancestor(sha, latest_sha):
        return False
    prev_sha = info.get("prev_sha")
    if prev_sha and (sha == prev_sha or not _is_ancestor(prev_sha, sha)):
        return False
    prev_name = info.get("prev")
    prev_key = release_key(prev_name) if prev_name else None
    candidates = [t for t in tags if prev_key is None or release_key(t) > prev_key]
    if not candidates:
        return True
    contained = _contains_from_candidates(sha, candidates)
    if contained is not None:
        return not contained
    for t in candidates:
        if _is_ancestor(sha, tags[t]):
            return False
    return True


def _tag_in_latest_cycle(tag: str, sha: str) -> bool:
    info = _safe_latest_info()
    if not info:
        return False
    prev_name = info.get("prev")
    if prev_name and release_key(tag) <= release_key(prev_name):
        return False
    return bool(
        _is_ancestor(sha, info["sha"])
        and (not info["prev_sha"] or _is_ancestor(info["prev_sha"], sha))
    )


def resolve_release_tag(rev: str) -> dict[str, Any]:
    """Resolve a tag, 'latest', branch, or commit SHA to its release tag context for Insights."""
    if rev == LATEST_RELEASE:
        info = latest_info()
        tags = tag_commits()
        tag = nearest_ancestor_tag(info["sha"]) or info["prev"]
        return {
            "rev": rev,
            "sha": info["sha"],
            "tag": tag,
            "exact": True,
            "latest": True,
            "source": "latest",
            "final": None,
            "series": _series_tag_for(info["prev"] or tag, tags),
        }

    tags = tag_commits()
    if rev in tags:
        sha = tags[rev]
        final = _final_for(rev, tags)
        is_latest = bool(final is None and (parse_tag(rev) or {}).get("pre") and _tag_in_latest_cycle(rev, sha))
        return {
            "rev": rev,
            "sha": sha,
            "tag": rev,
            "exact": True,
            "latest": is_latest,
            "source": "tag",
            "final": final,
            "series": _series_tag_for(rev, tags),
        }

    sha = resolve_sha(rev)
    if not sha:
        raise HTTPException(status_code=404, detail=f"Unknown revision: {rev}")

    exact_tags = [t for t, t_sha in tags.items() if t_sha == sha]
    if exact_tags:
        tag = _pick_best_tag(exact_tags)
        final = _final_for(tag, tags)
        is_latest = bool(final is None and tag and (parse_tag(tag) or {}).get("pre") and _tag_in_latest_cycle(tag, sha))
        return {
            "rev": rev,
            "sha": sha,
            "tag": tag,
            "exact": False,
            "latest": is_latest,
            "source": "exact_sha",
            "final": final,
            "series": _series_tag_for(tag, tags),
        }

    cont = first_containing(sha, allow_slow=False)
    if cont.get("released"):
        tag = cont.get("first_release") or cont.get("first_tag") or cont.get("first_stable")
        return {
            "rev": rev,
            "sha": sha,
            "tag": tag,
            "exact": False,
            "latest": False,
            "source": "release_index",
            "final": _final_for(tag, tags),
            "series": _series_tag_for(tag, tags),
        }

    tag = nearest_ancestor_tag(sha)
    info = _safe_latest_info()
    is_latest = _is_in_latest_cycle(sha, info, tags)
    return {
        "rev": rev,
        "sha": sha,
        "tag": tag,
        "exact": False,
        "latest": is_latest,
        "source": "ancestor" if tag else "none",
        "final": _final_for(tag, tags),
        "series": _series_tag_for(tag, tags),
    }


# --------------------------------------------------------------------------- per-release commit pass

_PASS_DISK = DiskCache("release-commits", max_bytes=512 << 20)
_COMMIT_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_TRAILER_ROLES = {
    CommitRole.SIGNED_OFF_BY: "signed_off_by", CommitRole.REVIEWED_BY: "reviewed_by",
    CommitRole.ACKED_BY: "acked_by", CommitRole.TESTED_BY: "tested_by", CommitRole.REPORTED_BY: "reported_by",
    CommitRole.SUGGESTED_BY: "suggested_by", CommitRole.CO_DEVELOPED_BY: "co_developed_by",
}


def parse_trailers(message: str) -> dict[str, list[list[str]]]:
    """Contributor trailers of a message by role (git_commit_parser.iter_trailers, CommitRole): the
    parser's own reading, so `Reported-by: a@b` and `Reported-by: <a@b>` (no name: the address is the
    name) count exactly as in m_bridge_commit_person."""
    out: dict[str, list[list[str]]] = {}
    for prefix, name, email in iter_trailers(message or ""):
        role = _TRAILER_ROLES.get(CommitRole.from_trailer_prefix(prefix))
        if role:
            out.setdefault(role, []).append([name, email.lower()])
    return out


def parse_commit_pass(text: str) -> list[dict[str, Any]]:
    """`git log --format=%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%cn%x1f%ce%x1f%s%x1f%B%x1f --name-only` output -> facts."""
    commits = []
    for chunk in text.split(_COMMIT_SEP):
        if not chunk.strip():
            continue
        parts = chunk.split(_FIELD_SEP)
        if len(parts) < 9:
            continue
        sha, an, ae, at, cn, ce, subject, body = parts[:8]
        files = [f for f in parts[8].splitlines() if f.strip()]
        commits.append({
            "sha": sha.strip(),
            "author": an, "email": ae.lower(), "date": int(at) if at.strip().isdigit() else 0,
            "committer": cn, "committer_email": ce.lower(), "subject": subject,
            "trailers": parse_trailers(body),
            "files": files,
        })
    return commits


def _pass_key(sha: str, prev: str | None, what: str) -> str:
    return f"{get_active_repo_name()}|{what}|{PASS_FORMAT}|{prev or '-'}|{sha}"


def _commit_pass(rng: list[str], progress: Callable[..., None] | None = None) -> list[dict[str, Any]]:
    fmt = "%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%cn%x1f%ce%x1f%s%x1f%B%x1f"
    if progress:
        progress(0, 1, "git log")
    out = _git(["log", "--no-merges", "--no-renames", f"--format={fmt}", "--name-only", *rng], timeout=900)
    if out is None:
        raise RuntimeError("git log failed")
    return parse_commit_pass(out)


def release_commits(tag: str, *, wait: float = 0.0) -> dict[str, Any]:
    """Commits a release introduced (prev..tag, no merges) as facts; a background job cached on disk.
    Returns the job state: {state, progress, result?}."""
    prev, sha = range_of(tag)
    rng = [sha] + ([f"^{prev}"] if prev else [])
    key = _pass_key(sha, prev, "commits")
    state = jobs.submit(f"release-commits:{key}", lambda p: _commit_pass(rng, p), cache=_PASS_DISK, cache_key=key)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"release-commits:{key}", wait) or state
    return state


def release_commits_now(
    tag: str,
    progress: Callable[..., None] | None = None,
    *,
    range_override: tuple[str | None, str] | None = None,
) -> list[dict[str, Any]]:
    """`release_commits` computed in the calling thread (for reports that already run as a job: a job
    waiting on queued jobs could starve the worker pool); same disk cache entry."""
    prev, sha = range_override if range_override is not None else range_of(tag)
    key = _pass_key(sha, prev, "commits")
    hit = _PASS_DISK.get(key)
    if hit is not None:
        return hit
    result = _commit_pass([sha] + ([f"^{prev}"] if prev else []), progress)
    _PASS_DISK.put(key, result)
    return result


def merges_now(tag: str, *, range_override: tuple[str | None, str] | None = None) -> list[dict[str, Any]]:
    """`merges` computed in the calling thread (see release_commits_now)."""
    prev, sha = range_override if range_override is not None else range_of(tag)
    key = _pass_key(sha, prev, "merges")
    hit = _PASS_DISK.get(key)
    if hit is not None:
        return hit
    fmt = "%x1e%H%x1f%P%x1f%at%x1f%cn%x1f%ce%x1f%s%x1f%b"
    out = _git(["log", "--first-parent", "--merges", f"--format={fmt}", sha] + ([f"^{prev}"] if prev else []), timeout=300)
    if out is None:
        raise RuntimeError("git log failed")
    result = parse_merges(out)
    _PASS_DISK.put(key, result)
    return result


def parse_merges(text: str) -> list[dict[str, Any]]:
    merges = []
    for chunk in text.split(_COMMIT_SEP):
        if not chunk.strip():
            continue
        parts = chunk.split(_FIELD_SEP)
        if len(parts) < 7:
            continue
        sha, parents, at, cn, ce, subject, body = parts[:7]
        merges.append({
            "sha": sha.strip(), "parents": parents.split(), "date": int(at) if at.strip().isdigit() else 0,
            "committer": cn, "committer_email": ce.lower(), "subject": subject, "body": body.strip()[:4000],
        })
    return merges


def merges(tag: str, *, wait: float = 0.0) -> dict[str, Any]:
    """Merges on the first-parent line of a release range (the pulls of a merge window): job state."""
    prev, sha = range_of(tag)
    rng = [sha] + ([f"^{prev}"] if prev else [])
    key = _pass_key(sha, prev, "merges")

    def build(progress):
        fmt = "%x1e%H%x1f%P%x1f%at%x1f%cn%x1f%ce%x1f%s%x1f%b"
        out = _git(["log", "--first-parent", "--merges", f"--format={fmt}", *rng], timeout=300)
        if out is None:
            raise RuntimeError("git log failed")
        return parse_merges(out)

    state = jobs.submit(f"release-merges:{key}", build, cache=_PASS_DISK, cache_key=key)
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(f"release-merges:{key}", wait) or state
    return state


def clear_caches() -> None:
    """The `git` or `parsed` sync token changed (tags fetched, versions parsed): the tag list and the
    database coverage are stale. Containment answers are only cached once a tag contains the commit (tags
    never move), so they stay valid."""
    _TAGS_CACHE.clear()
    _LATEST_CACHE.clear()
    _DB_COVERAGE.clear()


register_refs_changed(clear_caches)
