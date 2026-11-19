"""pipeline/git_sync.py - Thread/process-safe Git locking, automated remote updates, and distributed task queuing."""
from __future__ import annotations

import contextlib
import errno
import fcntl
import fnmatch
import hashlib
import os
import re
import subprocess as sp
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import distributed, ingest_state
from core.config import find_repository_branches
from core.FileHandler import MasterFile, _VERSIONSORT_SUFFIX_ARGS, _VERSION_TAG_RE, _version_tag_predicate
from core.globalstuff import G, is_safe_git_rev
from db_engine import DBError
from parser.git_ast import release_order
from pipeline.state import MF, logger

_IN_CHUNK = 1000
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_DONE_STATES = frozenset({"done"})
_KNOWN_REF_STATES = frozenset({"done", "interrupted", "legacy", "new"})


class GitLockTimeoutError(TimeoutError):
    """Raised when acquiring a repository synchronization lock times out."""


@dataclass(frozen=True)
class GitSyncResult:
    """Outcome of one locked `git remote update` repository synchronization."""

    repo_path: str
    updated: bool
    changed_refs: tuple[str, ...] = ()
    new_tags: tuple[str, ...] = ()
    error: str = ""
    skipped: bool = False
    duration_s: float = 0.0


def _get_thread_lock(lock_path: Path) -> threading.RLock:
    key = str(lock_path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _open_lock_file(repo: Path) -> tuple[Path, int]:
    dot_git = repo / ".git"
    candidate = (dot_git / ".kip-git-sync.lock") if dot_git.is_dir() else (repo / ".kip-git-sync.lock")
    try:
        fd = os.open(str(candidate), os.O_CREAT | os.O_RDWR, 0o666)
        return candidate, fd
    except OSError:
        digest = hashlib.sha1(str(repo).encode("utf-8")).hexdigest()[:16]
        fallback = Path(tempfile.gettempdir()) / f"kernelinfo-git-{digest}.lock"
        fd = os.open(str(fallback), os.O_CREAT | os.O_RDWR, 0o666)
        return fallback, fd


@contextlib.contextmanager
def git_repo_lock(
    repo_dir: str | Path,
    *,
    exclusive: bool = True,
    timeout: float = 120.0,
    poll_interval: float = 0.2,
) -> Iterator[Path]:
    """Acquire a thread-safe and process-safe lock on `repo_dir` without unlinking the lock inode."""
    repo = Path(repo_dir).resolve()
    lock_path, fd = _open_lock_file(repo)
    mode = "exclusive" if exclusive else "shared"
    deadline = time.monotonic() + max(0.0, float(timeout))
    step = max(0.005, float(poll_interval))
    rlock: threading.RLock | None = None
    acquired_rlock = False
    acquired_flock = False

    try:
        if exclusive:
            rlock = _get_thread_lock(lock_path)
            remaining = max(0.0, deadline - time.monotonic())
            acquired_rlock = rlock.acquire(timeout=remaining)
            if not acquired_rlock:
                raise GitLockTimeoutError(
                    f"Timed out acquiring {mode} git lock on '{lock_path}' after {timeout:.1f}s"
                )

        flock_op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(fd, flock_op | fcntl.LOCK_NB)
                acquired_flock = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GitLockTimeoutError(
                        f"Timed out acquiring {mode} git lock on '{lock_path}' after {timeout:.1f}s"
                    ) from exc
                time.sleep(min(step, remaining))

        yield lock_path
    finally:
        if acquired_flock:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
        if acquired_rlock and rlock is not None:
            rlock.release()


def _snapshot_refs(repo: Path, *, peel: bool = False) -> dict[str, str]:
    """Read `git show-ref` into `{ref_name: sha}`, preferring peeled `^{}` targets when `peel=True`."""
    cmd = ["git", "-C", str(repo), "show-ref"]
    if peel:
        cmd.append("-d")
    res = sp.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode not in (0, 1) or not res.stdout:
        return {}
    refs: dict[str, str] = {}
    for line in res.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip().lower(), parts[1].strip()
        if peel and ref.endswith("^{}"):
            refs[ref[:-3]] = sha
        elif peel and ref.startswith("refs/tags/"):
            refs.setdefault(ref, sha)
        else:
            refs[ref] = sha
    return refs


def _close_global_cat_file_if_matching(repo: Path) -> None:
    mf_repo = getattr(MF, "repo_dir", None)
    if mf_repo is None:
        return
    with contextlib.suppress(Exception):
        if repo == Path(mf_repo).resolve():
            MF._close_cat_file_proc()


def _fast_forward_local_branches(repo: Path, refs: dict[str, str]) -> None:
    """Fast-forward local `refs/heads/*` branches behind their remote tracking refs (non-bare repos)."""
    bare_res = sp.run(
        ["git", "-C", str(repo), "rev-parse", "--is-bare-repository"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15.0,
    )
    if bare_res.returncode != 0 or bare_res.stdout.strip().lower() == "true":
        return

    head_res = sp.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15.0,
    )
    checked_out = head_res.stdout.strip() if head_res.returncode == 0 else ""

    for_each = sp.run(
        [
            "git",
            "-C",
            str(repo),
            "for-each-ref",
            "--format=%(refname)\t%(objectname)\t%(upstream)",
            "refs/heads/",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30.0,
    )
    if for_each.returncode != 0:
        return

    for line in for_each.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        refname, local_sha = parts[0].strip(), parts[1].strip()
        upstream = parts[2].strip() if len(parts) > 2 else ""
        if not upstream and refname.startswith("refs/heads/"):
            candidate = f"refs/remotes/origin/{refname[len('refs/heads/') :]}"
            if candidate in refs:
                upstream = candidate
        upstream_sha = refs.get(upstream, "") if upstream else ""
        if not upstream_sha or upstream_sha == local_sha:
            continue

        anc = sp.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", local_sha, upstream_sha],
            capture_output=True,
            text=True,
            check=False,
            timeout=15.0,
        )
        if anc.returncode != 0:
            continue

        if refname == checked_out:
            sp.run(
                ["git", "-C", str(repo), "merge", "--ff-only", "--quiet", upstream_sha],
                capture_output=True,
                text=True,
                check=False,
                timeout=60.0,
            )
        else:
            sp.run(
                ["git", "-C", str(repo), "update-ref", refname, upstream_sha, local_sha],
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )


def sync_git_repository(
    repo_dir: str | Path,
    *,
    lock_timeout: float = 60.0,
    cmd_timeout: float = 600.0,
    prune: bool = True,
    raise_on_error: bool = False,
) -> GitSyncResult:
    """Lock `repo_dir`, run `git remote update [--prune]`, and report changed refs and new tags."""
    start_t = time.monotonic()
    repo = Path(repo_dir).resolve()

    with git_repo_lock(repo, exclusive=True, timeout=lock_timeout):
        _close_global_cat_file_if_matching(repo)

        try:
            remote_res = sp.run(
                ["git", "-C", str(repo), "remote"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30.0,
            )
        except (OSError, sp.SubprocessError) as exc:
            err = str(exc)
            if raise_on_error:
                raise RuntimeError(err) from exc
            return GitSyncResult(repo_path=str(repo), updated=False, error=err, duration_s=time.monotonic() - start_t)

        if remote_res.returncode != 0:
            err = (remote_res.stderr or remote_res.stdout or "git remote failed").strip()
            if raise_on_error:
                raise RuntimeError(err)
            return GitSyncResult(repo_path=str(repo), updated=False, error=err, duration_s=time.monotonic() - start_t)

        if not remote_res.stdout.strip():
            return GitSyncResult(repo_path=str(repo), updated=False, skipped=True, duration_s=time.monotonic() - start_t)

        before_refs = _snapshot_refs(repo, peel=False)
        cmd = ["git", "-C", str(repo), "remote", "update"]
        if prune:
            cmd.append("--prune")
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"

        try:
            res = sp.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=cmd_timeout,
                env=env,
            )
        except sp.TimeoutExpired as exc:
            err = f"git remote update timed out after {cmd_timeout}s"
            if raise_on_error:
                raise RuntimeError(err) from exc
            return GitSyncResult(repo_path=str(repo), updated=False, error=err, duration_s=time.monotonic() - start_t)
        except OSError as exc:
            err = str(exc)
            if raise_on_error:
                raise RuntimeError(err) from exc
            return GitSyncResult(repo_path=str(repo), updated=False, error=err, duration_s=time.monotonic() - start_t)

        if res.returncode != 0:
            err = (res.stderr or res.stdout or f"git remote update exited with status {res.returncode}").strip()
            if raise_on_error:
                raise RuntimeError(err)
            return GitSyncResult(repo_path=str(repo), updated=False, error=err, duration_s=time.monotonic() - start_t)

        _fast_forward_local_branches(repo, _snapshot_refs(repo, peel=False))
        after_refs = _snapshot_refs(repo, peel=False)
        changed = tuple(
            sorted(ref for ref in (set(before_refs) | set(after_refs)) if before_refs.get(ref) != after_refs.get(ref))
        )
        new_tags = tuple(
            sorted(
                ref[len("refs/tags/") :]
                for ref in after_refs
                if ref.startswith("refs/tags/") and ref not in before_refs
            )
        )
        duration = time.monotonic() - start_t
        updated = bool(changed)
        if updated:
            logger.info(
                "[GitSync] Updated '%s': %d changed ref(s), %d new tag(s) in %.2fs.",
                repo,
                len(changed),
                len(new_tags),
                duration,
            )
        else:
            logger.debug("[GitSync] '%s' is up to date (%.2fs).", repo, duration)
        return GitSyncResult(
            repo_path=str(repo),
            updated=updated,
            changed_refs=changed,
            new_tags=new_tags,
            duration_s=duration,
        )


_HISTORICAL_TAG_RE = re.compile(
    r"^v?(?P<a>\d+)\.(?P<b>\d+)(?:\.(?P<c>\d+))?(?:\.(?P<d>\d+))?(?:\.(?P<e>\d+))?"
    r"(?P<letter>[a-zA-Z](?P<plus>\+)?)?"
    r"(?:-?(?P<prekind>alpha|beta|pre|rc|patch)(?P<prenum>\d*))?$"
)


def tag_seq_order(tag: str) -> int:
    """Compute monotonic signed-BIGINT ordinal for `tag` within its track (canonical and historical)."""
    p = release_order.parse_tag(tag)
    if p is not None:
        kind = p.get("kind")
        if kind == "next":
            if tag.startswith("next-") and tag[5:].isdigit():
                return int(tag[5:])
            series = p.get("series") or (release_order.NEXT_SERIES, 0, 0)
            return int(series[1]) * 10000 + int(series[2]) * 100 + int(p.get("stable") or 0)

        series = p.get("series") or ()
        is_prefixed = bool(p.get("family")) or (len(series) >= 4 and series[0] == release_order.PREFIXED_SERIES)
        if is_prefixed:
            major = int(series[2]) if len(series) > 2 else 0
            minor = int(series[3]) if len(series) > 3 else 0
            if p.get("pre"):
                suffix = min(int(p["pre"]), 998)
            elif p.get("stable"):
                suffix = 1000 + min(int(p["stable"]), 999_999)
            else:
                suffix = 999
            return (major * 1_000_000 + minor * 1_000) * 1_000 + suffix

        if kind == "stable":
            return max(1, int(p.get("stable") or 1))

    m = _HISTORICAL_TAG_RE.match(str(tag or ""))
    if not m:
        return 1

    a = int(m.group("a"))
    b = int(m.group("b"))
    c = int(m.group("c") or 0)
    d = int(m.group("d") or 0)
    e = int(m.group("e") or 0)
    letter = m.group("letter") or ""
    plus = bool(m.group("plus"))
    prekind = (m.group("prekind") or "").lower()
    prenum = int(m.group("prenum") or 0)

    if prekind in ("alpha", "beta", "pre", "rc") and not letter:
        base_stage = {"alpha": 100, "beta": 200, "pre": 300, "rc": 400}[prekind]
        stage = base_stage + min(prenum, 99)
    elif letter:
        letter_idx = ord(letter[0].lower()) - ord("a") + 1
        stage = 500 + letter_idx * 200 + (100 if plus else 0) + (min(prenum, 99) if prekind == "patch" else 0)
    elif prekind == "patch":
        stage = 500 + min(prenum, 99)
    elif e:
        stage = 500 + min(e, 99)
    else:
        stage = 500

    return (((a * 1000 + b) * 1000 + c) * 1000 + d) * 10000 + stage


def track_name_for_tag(tag: str, kind: str) -> str:
    """Return distributed queue `track_name` for `(tag, kind)`."""
    if kind == "next":
        return "next"
    if kind == "stable":
        p = release_order.parse_tag(tag)
        if p is not None and p.get("series") is not None:
            return f"stable:{release_order.series_name(p['series'])}"
        return "stable"
    if kind in ("mainline", "main"):
        return "mainline"
    p = release_order.parse_tag(tag)
    if p is not None:
        if p.get("kind") == "next":
            return "next"
        if p.get("kind") == "stable" and p.get("series") is not None:
            return f"stable:{release_order.series_name(p['series'])}"
    return "mainline"


def verify_local_ref_exists(repo_dir: str | Path, ref_name: str) -> bool:
    """Return True if `ref_name` is a safe git revision resolving to a commit in `repo_dir`."""
    if not is_safe_git_rev(ref_name):
        return False
    repo = Path(repo_dir).resolve()
    with git_repo_lock(repo, exclusive=False, timeout=15.0):
        try:
            res = sp.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref_name}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )
            return res.returncode == 0
        except (OSError, sp.SubprocessError):
            return False


def _is_real_git_dir(repo: Path) -> bool:
    return (repo / ".git").exists() or ((repo / "HEAD").is_file() and (repo / "objects").is_dir())


def _discover_version_tags(
    repo: Path,
    *,
    start_ref: str | None = None,
    include_pre_releases: bool = True,
    known_tags: Sequence[str] | None = None,
    all_local: bool = False,
) -> list[str]:
    """Discover version tags via a local `MasterFile()` without mutating global `MF`."""
    prev_linux_dir = getattr(G, "linux_directory", None)
    mf = MasterFile()
    try:
        mf.set_repo_dir(repo)
        if all_local and start_ref is None and _is_real_git_dir(repo):
            git_dir = mf._get_git_dir()
            commit_tags = mf._commit_tag_names()
            command = [
                "git",
                f"--git-dir={git_dir}",
                *_VERSIONSORT_SUFFIX_ARGS,
                "tag",
                "-l",
                "--sort=v:refname",
            ]
            res = sp.run(command, capture_output=True, text=True, check=False)
            if res.returncode != 0 or not res.stdout.strip():
                return []
            raw_tags = [t.strip() for t in res.stdout.splitlines() if t.strip()]
            if not raw_tags:
                return []
            is_version_tag = _version_tag_predicate(raw_tags)
            prefixed = not any(not t.endswith("-tree") and _VERSION_TAG_RE.match(t) for t in raw_tags)
            raw_tags = [t for t in raw_tags if is_version_tag(t)]
            if commit_tags is not None:
                raw_tags = [t for t in raw_tags if t in commit_tags]
            if prefixed:
                raw_tags.sort(key=release_order.release_key)
            raw_tags = [t for t in raw_tags if not t.startswith("next-")] + [
                t for t in raw_tags if t.startswith("next-")
            ]
            if not include_pre_releases:
                filtered = [t for t in raw_tags if not any(pre in t for pre in ("-rc", "-alpha", "-beta", "-pre"))]
                return filtered if filtered else raw_tags
            return raw_tags

        tags = mf.get_version_tags(start_ref=start_ref, include_pre_releases=include_pre_releases)
        # MasterFile.get_version_tags falls back to ["v3.0"] when start_ref=None and v3.0 is absent.
        if start_ref is None and tags == ["v3.0"] and _is_real_git_dir(repo):
            tag_set = set(known_tags) if known_tags is not None else set(mf._git_lines("tag", "-l"))
            if "v3.0" not in tag_set and not mf._git_lines("rev-parse", "--verify", "--quiet", "v3.0^{commit}"):
                rel_tags = sorted(release_order.release_tag_names(tag_set), key=release_order.release_key)
                if not rel_tags:
                    return []
                tags = mf.get_version_tags(start_ref=rel_tags[0], include_pre_releases=include_pre_releases)
        return tags
    finally:
        mf._close_cat_file_proc()
        if prev_linux_dir is not None:
            G.linux_directory = prev_linux_dir


def discover_local_repo_capabilities(
    repo_dir: str | Path,
    network_id: int,
    repo_name: str,
    *,
    include_pre_releases: bool = True,
    supports_preparse: bool = True,
    allowed_tracks: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Inspect all local repository tags and branches to produce `distributed_worker_capabilities` rows."""
    _ = network_id
    repo = Path(repo_dir).resolve()
    local_path = str(repo)
    preparse_flag = 1 if supports_preparse else 0

    with git_repo_lock(repo, exclusive=False):
        tags = _discover_version_tags(repo, include_pre_releases=include_pre_releases, all_local=True)
        branches = find_repository_branches(repo)

    buckets: dict[str, list[tuple[str, int]]] = {}
    ranges = release_order.release_ranges(tags)
    ranged_tags: set[str] = set()
    for rng in ranges:
        tag = rng["tag"]
        kind = rng["kind"]
        ranged_tags.add(tag)
        track = track_name_for_tag(tag, kind)
        buckets.setdefault(track, []).append((tag, tag_seq_order(tag)))

    for tag in tags:
        if tag not in ranged_tags:
            buckets.setdefault("mainline", []).append((tag, tag_seq_order(tag)))

    capabilities: list[dict[str, Any]] = []
    for track, items in buckets.items():
        min_ref, min_ord = min(items, key=lambda pair: pair[1])
        max_ref, max_ord = max(items, key=lambda pair: pair[1])
        capabilities.append(
            {
                "repo_name": repo_name,
                "track_name": track,
                "min_seq_order": min_ord,
                "max_seq_order": max_ord,
                "min_ref": min_ref,
                "max_ref": max_ref,
                "supports_preparse": preparse_flag,
                "local_repo_path": local_path,
            }
        )

    for branch in branches:
        capabilities.append(
            {
                "repo_name": repo_name,
                "track_name": f"branch:{branch}",
                "min_seq_order": 0,
                "max_seq_order": 0,
                "min_ref": branch,
                "max_ref": branch,
                "supports_preparse": preparse_flag,
                "local_repo_path": local_path,
            }
        )

    if allowed_tracks is not None:
        patterns = [str(pat) for pat in allowed_tracks]
        capabilities = [
            cap
            for cap in capabilities
            if any(fnmatch.fnmatchcase(cap["track_name"], pat) for pat in patterns)
        ]

    return capabilities


def _norm_sha(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray)):
        h = bytes(val).hex().lower()
    else:
        h = str(val).strip().lower()
    return "" if h == "0" * 40 else h


def _unpack_ref_state_entry(val: Any) -> tuple[int, str, str] | None:
    if isinstance(val, dict):
        cid = int(val.get("commit_id") or 0)
        state = str(val.get("state") or "new")
        sha = _norm_sha(val.get("commit_hash") or val.get("sha") or val.get("hash") or val.get("target_sha"))
        return (cid, state, sha)
    if isinstance(val, (tuple, list)) and len(val) >= 3:
        cid = int(val[0] or 0)
        v1, v2 = val[1], val[2]
        if isinstance(v1, str) and v1 in _KNOWN_REF_STATES:
            return (cid, v1, _norm_sha(v2))
        if isinstance(v2, str) and v2 in _KNOWN_REF_STATES:
            return (cid, v2, _norm_sha(v1))
        if isinstance(v2, (bytes, bytearray)) or (isinstance(v2, str) and len(v2.strip()) >= 32):
            return (cid, str(v1 or "new"), _norm_sha(v2))
        if isinstance(v1, (bytes, bytearray)) or (isinstance(v1, str) and len(v1.strip()) >= 32):
            return (cid, str(v2 or "new"), _norm_sha(v1))
        return (cid, str(v1 or "new"), _norm_sha(v2))
    return None


def _lookup_commit_shas_by_id(db: Any, network_id: int, cids: Sequence[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    unique = sorted({int(c) for c in cids if c and int(c) > 0})
    if not unique:
        return out
    try:
        for i in range(0, len(unique), _IN_CHUNK):
            chunk = unique[i : i + _IN_CHUNK]
            rows = db.select_rows(
                "m_commit",
                ["commit_id", "commit_hash"],
                {"network_id": int(network_id), "commit_id": chunk},
                as_dict=False,
            )
            for cid, chash in rows:
                sha = _norm_sha(chash)
                if sha:
                    out[int(cid)] = sha
    except DBError as exc:
        logger.debug("[GitSync] Could not query m_commit hashes: %s", exc)
    return out


def _load_repo_ref_states(db: Any, repo_id: int, network_id: int) -> dict[str, tuple[int, str, str]]:
    """Load `{ref_name: (commit_id, state, stored_sha_hex)}` via `fetch_repo_ref_hashes` or fallback."""
    hash_fn = getattr(ingest_state, "fetch_repo_ref_hashes", None)
    raw_hashes = hash_fn(db, repo_id, network_id) if callable(hash_fn) else None

    if isinstance(raw_hashes, dict) and raw_hashes:
        sample = next(iter(raw_hashes.values()))
        if _unpack_ref_state_entry(sample) is not None:
            parsed: dict[str, tuple[int, str, str]] = {}
            for ref, entry in raw_hashes.items():
                unpacked = _unpack_ref_state_entry(entry)
                if unpacked is not None:
                    parsed[str(ref)] = unpacked
            return parsed

        # `fetch_repo_ref_hashes` returned `{ref_name: sha}`
        version_states = ingest_state.fetch_repo_version_states(db, repo_id, network_id)
        out: dict[str, tuple[int, str, str]] = {}
        missing_cids: list[int] = []
        for ref, (cid, st) in version_states.items():
            sha = _norm_sha(raw_hashes.get(ref))
            if st in _DONE_STATES and not sha and cid > 0:
                missing_cids.append(cid)
            out[ref] = (cid, st, sha)
        for ref, sha_val in raw_hashes.items():
            if ref not in out:
                out[str(ref)] = (0, "new", _norm_sha(sha_val))
        if missing_cids:
            by_cid = _lookup_commit_shas_by_id(db, network_id, missing_cids)
            for ref, (cid, st, sha) in list(out.items()):
                if not sha and cid in by_cid:
                    out[ref] = (cid, st, by_cid[cid])
        return out

    version_states = ingest_state.fetch_repo_version_states(db, repo_id, network_id)
    done_cids = [cid for cid, st in version_states.values() if st in _DONE_STATES and cid > 0]
    by_cid = _lookup_commit_shas_by_id(db, network_id, done_cids)
    return {
        ref: (cid, st, by_cid.get(cid, "") if st in _DONE_STATES else "")
        for ref, (cid, st) in version_states.items()
    }


def _lookup_local_ref_sha(full_refs: dict[str, str], ref_name: str, *, is_tag: bool = False) -> str:
    if is_tag:
        return full_refs.get(f"refs/tags/{ref_name}", "")
    return (
        full_refs.get(ref_name)
        or full_refs.get(f"refs/heads/{ref_name}")
        or full_refs.get(f"refs/remotes/{ref_name}")
        or full_refs.get(f"refs/tags/{ref_name}")
        or ""
    )


def _load_preparse_progress(db: Any) -> tuple[set[str], set[int]]:
    preparsed_tags: set[str] = set()
    kapi_cids: set[int] = set()
    try:
        preparsed_tags = {
            str(t) for t in db.select_values("insights_subsystem_version_delta", "version_tag", distinct=True)
        }
    except DBError:
        preparsed_tags = set()
    try:
        kapi_cids = {
            int(c) for c in db.select_values("m_kapi_release_summary", "commit_id", distinct=True) if c is not None
        }
    except DBError:
        kapi_cids = set()
    return preparsed_tags, kapi_cids


def _load_existing_dist_tasks(db: Any, network_id: int, repo_name: str) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        rows = db.select_rows(
            "distributed_tasks",
            [
                "task_type",
                "ref_name",
                "repo_id",
                "track_name",
                "target_sha",
                "commit_id",
                "base_ref_name",
                "base_commit_id",
                "priority",
                "seq_order",
                "status",
            ],
            {"network_id": int(network_id), "repo_name": repo_name},
            as_dict=True,
        )
        return {(str(r["task_type"]), str(r["ref_name"])): r for r in rows}
    except DBError:
        return {}


def _is_task_row_changed(row: tuple[Any, ...], existing: dict[str, Any] | None) -> bool:
    if existing is None:
        return True
    (_net_id, repo_id, _rname, _ttype, track, _ref, target_sha, row_cid, base_ref, base_cid, priority, seq_order, status, _c, _u) = row
    ex_status = str(existing.get("status") or "")
    ex_sha = str(existing.get("target_sha") or "")
    sha_moved = bool(target_sha and ex_sha and str(target_sha) != ex_sha)

    if ex_status == "claimed":
        return str(status) == "completed"
    if ex_status == "failed":
        return sha_moved or str(status) == "completed"

    if sha_moved or ex_status != str(status):
        return True
    if int(existing.get("repo_id") or 0) != int(repo_id):
        return True
    if str(existing.get("track_name") or "") != str(track):
        return True
    if str(existing.get("base_ref_name") or "") != str(base_ref):
        return True
    if int(existing.get("priority") or 0) != int(priority):
        return True
    if int(existing.get("seq_order") or 0) != int(seq_order):
        return True
    if int(row_cid or 0) > 0 and int(existing.get("commit_id") or 0) != int(row_cid):
        return True
    if int(base_cid or 0) > 0 and int(existing.get("base_commit_id") or 0) != int(base_cid):
        return True
    if target_sha and not ex_sha:
        return True
    return False


def sync_and_enqueue_repo_tasks(
    db: Any,
    network_id: int,
    repo_id: int,
    repo_name: str,
    repo_dir: str | Path,
    *,
    start_ref: str | None = None,
    include_pre_releases: bool = True,
    delegate_stable: bool = True,
    delegate_branches: Sequence[str] | None = None,
    enqueue_main: bool = True,
    enqueue_preparse: bool = True,
    is_kernel: bool = True,
    repo_feature_lookup: Callable[[str], bool] | None = None,
    skip_repo_steps: Collection[str] = (),
) -> dict[str, int]:
    """Discover local tags/branches, detect moved refs, and publish prioritized delta rows to `distributed_tasks`.

    With `repo_feature_lookup` (the repository's feature flags), the repository-level step tasks (CVE list,
    release statistics; `pipeline/step_tasks.py` `enqueue_repo_tasks()`) not in `skip_repo_steps` are published
    too, reopened only when their fingerprint moved (`repo` count)."""
    counts = {
        "total": 0,
        "enqueued": 0,
        "unchanged": 0,
        "repo": 0,
        "main": 0,
        "stable": 0,
        "next": 0,
        "branch": 0,
        "preparse": 0,
    }
    if db is None:
        return counts

    repo = Path(repo_dir).resolve()
    clean_start = str(start_ref).strip() or None if start_ref is not None else None
    existing_tasks = _load_existing_dist_tasks(db, int(network_id), repo_name)

    with git_repo_lock(repo, exclusive=False):
        full_refs = _snapshot_refs(repo, peel=True)
        known_tags = [r[len("refs/tags/") :] for r in full_refs if r.startswith("refs/tags/")]
        tags = _discover_version_tags(
            repo,
            start_ref=clean_start,
            include_pre_releases=include_pre_releases,
            known_tags=known_tags,
        )
        ranges = release_order.release_ranges(tags)
        range_by_tag = {rng["tag"]: rng for rng in ranges}
        delegated_tag_set = {rng["tag"] for rng in ranges if rng["kind"] in ("stable", "next")}
        main_tags = [t for t in tags if t not in delegated_tag_set]
        main_sorted = sorted(dict.fromkeys(main_tags), key=lambda t: (tag_seq_order(t), t))

        raw_states = _load_repo_ref_states(db, repo_id, network_id)
        resolved_states: dict[str, tuple[int, str, str]] = {}
        for ref, (cid, st, stored_sha) in raw_states.items():
            is_tag_ref = f"refs/tags/{ref}" in full_refs
            local_sha = _lookup_local_ref_sha(full_refs, ref, is_tag=is_tag_ref)
            if st == "done" and local_sha and stored_sha and local_sha != stored_sha:
                resolved_states[ref] = (0, "moved", local_sha)
            else:
                resolved_states[ref] = (cid, st, local_sha)

        compat_states = {
            ref: (0 if st == "moved" else cid, "new" if st == "moved" else st)
            for ref, (cid, st, _sha) in resolved_states.items()
        }

        now = int(time.time())
        rows: list[tuple[Any, ...]] = []
        preparse_candidates: list[tuple[str, str, int, str]] = []

        def _ref_state(tag_name: str) -> tuple[int, str, str]:
            local_sha = _lookup_local_ref_sha(full_refs, tag_name, is_tag=True)
            cid, st, _ = resolved_states.get(tag_name, (0, "new", local_sha))
            if st == "done" and local_sha and tag_name in raw_states:
                stored_sha = raw_states[tag_name][2]
                if stored_sha and local_sha != stored_sha:
                    return 0, "moved", local_sha
            return (int(cid or 0) if st != "moved" else 0), st, local_sha

        def _base_cid_for(base_ref: str) -> int:
            if not base_ref:
                return 0
            b_cid, b_st, _ = resolved_states.get(base_ref, (0, "new", ""))
            return int(b_cid or 0) if b_st != "moved" else 0

        for idx, tag in enumerate(main_sorted):
            base_ref = main_sorted[idx - 1] if idx > 0 else ""
            row_cid, st, local_sha = _ref_state(tag)
            base_cid = _base_cid_for(base_ref)
            seq_ord = tag_seq_order(tag)
            status = "completed" if st == "done" else "pending"
            if enqueue_main:
                rows.append((
                    int(network_id), int(repo_id), repo_name, "main", "mainline", tag, local_sha,
                    row_cid, base_ref, base_cid, 10, seq_ord, status, now, now,
                ))
                counts["main"] += 1
            if tag in range_by_tag:
                preparse_candidates.append((tag, "mainline", row_cid, local_sha))

        for rng in ranges:
            tag = rng["tag"]
            kind = rng["kind"]
            if kind not in ("stable", "next"):
                continue
            base_ref = rng["segment_base"] or ""
            row_cid, st, local_sha = _ref_state(tag)
            base_cid = _base_cid_for(base_ref)
            seq_ord = tag_seq_order(tag)
            track = track_name_for_tag(tag, kind)
            status = "completed" if st == "done" else "pending"

            if kind == "stable":
                if delegate_stable or enqueue_main:
                    pri = 5 if delegate_stable else 10
                    rows.append((
                        int(network_id), int(repo_id), repo_name, "stable", track, tag, local_sha,
                        row_cid, base_ref, base_cid, pri, seq_ord, status, now, now,
                    ))
                    counts["stable"] += 1
                preparse_candidates.append((tag, kind, row_cid, local_sha))
            elif kind == "next":
                rows.append((
                    int(network_id), int(repo_id), repo_name, "next", "next", tag, local_sha,
                    row_cid, base_ref, base_cid, 5, seq_ord, status, now, now,
                ))
                counts["next"] += 1

        branches = list(dict.fromkeys(distributed.delegated_branches(delegate_branches, repo)))
        if branches:
            branch_bases: dict[str, tuple[str, int]] = {}
            to_resolve: list[tuple[str, str]] = []
            for b_ref in branches:
                local_sha = _lookup_local_ref_sha(full_refs, b_ref, is_tag=False)
                ex_b = existing_tasks.get(("branch", b_ref))
                if ex_b is not None and local_sha and str(ex_b.get("target_sha") or "") == local_sha:
                    b_base_ref = str(ex_b.get("base_ref_name") or "")
                    b_base_cid = compat_states.get(b_base_ref, (0, "new"))[0] if b_base_ref else 0
                    if b_base_cid <= 0:
                        b_base_cid = int(ex_b.get("base_commit_id") or 0)
                    branch_bases[b_ref] = (b_base_ref, int(b_base_cid or 0))
                else:
                    to_resolve.append((b_ref, local_sha))

            if to_resolve:
                tag_map = distributed._load_tag_map(repo)
                if len(to_resolve) > 1:
                    max_workers = min(8, len(to_resolve), max(1, os.cpu_count() or 4))
                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        merge_results = list(
                            pool.map(
                                lambda item: distributed._resolve_branch_merge_base(repo, item[0], tag_map=tag_map),
                                to_resolve,
                            )
                        )
                else:
                    merge_results = [distributed._resolve_branch_merge_base(repo, to_resolve[0][0], tag_map=tag_map)]

                for (b_ref, _local_sha), (base_ref, merge_sha) in zip(to_resolve, merge_results):
                    base_cid = distributed._resolve_base_commit_id(db, network_id, base_ref, merge_sha, compat_states)
                    branch_bases[b_ref] = (base_ref, int(base_cid or 0))

            for b_ref in branches:
                local_sha = _lookup_local_ref_sha(full_refs, b_ref, is_tag=False)
                cid, st, _ = resolved_states.get(b_ref, (0, "new", local_sha))
                if st == "done" and local_sha and b_ref in raw_states:
                    stored_sha = raw_states[b_ref][2]
                    if stored_sha and local_sha != stored_sha:
                        st = "moved"
                        cid = 0
                base_ref, base_cid = branch_bases[b_ref]
                status = "completed" if st == "done" else "pending"
                row_cid = int(cid or 0) if st != "moved" else 0
                rows.append((
                    int(network_id), int(repo_id), repo_name, "branch", f"branch:{b_ref}", b_ref, local_sha,
                    row_cid, base_ref, int(base_cid or 0), 5, 1, status, now, now,
                ))
                counts["branch"] += 1

    if enqueue_preparse and is_kernel and preparse_candidates:
        preparsed_tags, kapi_cids = _load_preparse_progress(db)
        logger.debug(
            "[GitSync] Preparse state for '%s': %d insights tag(s), %d KAPI commit(s).",
            repo_name,
            len(preparsed_tags),
            len(kapi_cids),
        )
        for tag, kind, row_cid, local_sha in preparse_candidates:
            st = resolved_states.get(tag, (0, "new", ""))[1]
            ex_pp = existing_tasks.get(("preparse", tag))
            if (
                ex_pp is not None
                and str(ex_pp.get("status") or "") == "completed"
                and st != "moved"
                and (not local_sha or str(ex_pp.get("target_sha") or "") == local_sha)
            ):
                continue
            if tag not in preparsed_tags or st == "moved":
                rows.append((
                    int(network_id), int(repo_id), repo_name, "preparse", track_name_for_tag(tag, kind), tag,
                    local_sha, row_cid, "", 0, 1, tag_seq_order(tag), "pending", now, now,
                ))
                counts["preparse"] += 1

    counts["total"] = len(rows)
    delta_rows = [
        row for row in rows if _is_task_row_changed(row, existing_tasks.get((str(row[3]), str(row[5]))))
    ]
    counts["unchanged"] = len(rows) - len(delta_rows)
    if delta_rows:
        counts["enqueued"] = int(db.enqueue_distributed_tasks(delta_rows) or 0)
    if repo_feature_lookup is not None:
        from pipeline.step_tasks import enqueue_repo_tasks
        counts["repo"] = int(enqueue_repo_tasks(
            db, network_id, repo_id, repo_name, str(repo_dir),
            feature_lookup=repo_feature_lookup, is_kernel=is_kernel, skip_steps=skip_repo_steps,
        ) or 0)
        counts["enqueued"] += counts["repo"]
    return counts
