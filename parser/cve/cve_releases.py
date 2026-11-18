"""parser/cve/cve_releases.py - Pre-evaluate release-to-CVE verdicts (`m_cve_release`, `m_cve_release_state`).

Evaluates kernel CVE records (`m_cve`) against the repository's release segments (`m_release_segment`,
`m_commit_release`, `m_cve_commit_hash`, `m_commit_relation` / Git) using the shared pure `affects()` rules,
and stores the matching verdicts (`affected`, `possibly`, `fixed`) in `m_cve_release` alongside each
segment's evaluated CVE list commit hash (`m_cve_release_state.cve_head`).

Incremental refresh:
- Segments whose `tag_hash`, `cve_head`, `cve_layout`, `cve_format`, and `rel_format` already match
  `m_cve_source` cost nothing.
- When `m_cve_source.head` advances from `old_head` to `new_head`, only CVE IDs touched in
  `git diff --name-only old_head new_head` (plus any CVEs whose commit prefixes were newly resolved or
  whose commits landed in newly added release segments) are re-evaluated.
- New release segments (or segments with a moved tag or format bump) are evaluated once per segment and
  written in batched atomic transactions, making full backfills resumable per batch.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import subprocess
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from core.DBLayout import m_cve_release, m_cve_release_state
from db_engine import CAP_CONCURRENT_CONNECTIONS, ConnectionLostError, DeadlockError
from db_engine.spec import Agg, Join
from parser.cve.cve_ingest import LAYOUTS, _ids_of, changed_paths
from parser.cve.cve_records import HEXSHA, normalize_record_tuples, version_status
from parser.git_ast.commit_relations import (
    BACKFILL_DONE,
    KIND_BACKFILL_STATE,
    KIND_FIXES,
    KIND_UPSTREAM,
    ZERO_HASH,
    parse_relations,
    resolve_prefixes,
)
from parser.git_ast.release_order import final_of, parse_tag, release_key, series_name
from parser.git_ast.release_stats import release_tags

logger = logging.getLogger(__name__)

CVE_RELEASE_FORMAT = 1

STATE_AFFECTED = 1
STATE_POSSIBLY = 2
STATE_FIXED = 3
STATE_TO_ID: dict[str, int] = {"affected": STATE_AFFECTED, "possibly": STATE_POSSIBLY, "fixed": STATE_FIXED}
ID_TO_STATE: dict[int, str] = {v: k for k, v in STATE_TO_ID.items()}

BASIS_NONE = 0
BASIS_RECORD = 1
BASIS_FIXES_TAG = 2
BASIS_REFERENCE = 3
BASIS_DESCRIPTION = 4
BASIS_VERSIONS = 5
BASIS_TO_ID: dict[str, int] = {
    "": BASIS_NONE,
    "record": BASIS_RECORD,
    "fixes-tag": BASIS_FIXES_TAG,
    "reference": BASIS_REFERENCE,
    "description": BASIS_DESCRIPTION,
    "versions": BASIS_VERSIONS,
}
ID_TO_BASIS: dict[int, str] = {v: k for k, v in BASIS_TO_ID.items()}

DB_CHAIN_MAINLINE = 1
DB_CHAIN_STABLE = 2
_FULL = re.compile(r"^[0-9a-f]{40}$")
_FULL_BYTES = re.compile(rb"^[0-9a-f]{40}$")
_UPSTREAM_BYTES_RE = re.compile(
    rb"^[ \t]*(?:commit ([0-9a-fA-F]{7,40})\b[ \t]*upstream\.?[ \t]*$"
    rb"|\[[ \t]*upstream commit ([0-9a-fA-F]{7,40})\b[ \t]*\]"
    rb"|\(cherry picked from commit ([0-9a-fA-F]{7,40})\b\))",
    re.IGNORECASE | re.MULTILINE,
)
_CVE_BATCH = 500
_SHA_BATCH = 1000
_INSERT_CHUNK = 10000
_SEG_INC_BATCH = 200
_FULL_SEGMENT_BATCH = 25


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")


def _hex(value: Any) -> str:
    return bytes(value).hex() if isinstance(value, (bytes, bytearray, memoryview)) else str(value or "")


# --------------------------------------------------------------------------- pure release evaluation

class BaseReleaseContext:
    """Pure containment and release-order checks for one release tag.

    Subclasses implement `tags(sha)`, `backports(sha)`, and `fixes_tags(sha)`.
    """

    def __init__(
        self,
        release: str,
        *,
        parsed_tags: dict[str, Any] | None = None,
        release_keys: dict[str, Any] | None = None,
    ) -> None:
        self.release = release
        self._ptags: dict[str, Any] = parsed_tags if parsed_tags is not None else {}
        self._rkeys: dict[str, Any] = release_keys if release_keys is not None else {}
        self.p = self.parse_tag(release)
        self.version = release[1:] if release.startswith("v") else release
        self.key = self.release_key(release)
        is_stable = bool(self.p and self.p.get("kind") != "next" and self.p.get("stable"))
        self.base_key = self.release_key(series_name(self.p["series"])) if is_stable else None

    def release_key(self, tag: str) -> Any:
        if tag not in self._rkeys:
            self._rkeys[tag] = release_key(tag)
        return self._rkeys[tag]

    def parse_tag(self, tag: str) -> Any:
        if tag not in self._ptags:
            self._ptags[tag] = parse_tag(tag)
        return self._ptags[tag]

    def tags(self, sha: str | None) -> dict[str, Any] | None:
        return None

    def backports(self, sha: str) -> list[str]:
        return []

    def fixes_tags(self, sha: str) -> list[str]:
        return []

    def contains(self, sha: str | None) -> bool:
        t = self.tags(sha)
        if not t or not self.p:
            return False
        m, s = t.get("mainline"), t.get("stable")
        if self.p.get("kind") == "next":
            if m and self.release_key(m) <= self.key:
                return True
            q = self.parse_tag(s) if s else None
            return bool(q) and q.get("kind") == "next" and self.release_key(s) <= self.key
        if not self.p["stable"]:
            return bool(m) and self.release_key(m) <= self.key
        if m and self.release_key(m) <= self.base_key:
            return True
        q = self.parse_tag(s) if s else None
        return bool(q) and q["series"] == self.p["series"] and q["stable"] <= self.p["stable"]

    def applies(self, fix: str) -> bool:
        """A fixing commit on the release's branch (or not released anywhere yet)."""
        t = self.tags(fix)
        if not t:
            return True
        if t.get("mainline"):
            return True
        q = self.parse_tag(t["stable"]) if t.get("stable") else None
        if not self.p or not q:
            return False
        if self.p.get("kind") == "next":
            return q.get("kind") == "next"
        return bool(self.p["stable"] and q["series"] == self.p["series"])

    def first_here(self, sha: str | None) -> bool:
        """The commit first appears in this release: its stable update, its -rc, or its cycle (-rc1 .. final)."""
        t = self.tags(sha)
        if not t or not self.p:
            return False
        is_next = self.p.get("kind") == "next"
        first = t.get("stable") if (is_next or self.p["stable"]) else t.get("mainline")
        if not first:
            return False
        if is_next or self.p["stable"] or self.p["pre"]:
            return first == self.release
        q = self.parse_tag(first)
        return bool(q) and q.get("kind") != "next" and not q["stable"] and q["series"] == self.p["series"]


def affects(rec: dict[str, Any], release: str, ctx: BaseReleaseContext) -> tuple[str, str]:
    """(state, basis) of a record for a release tag.

    state: affected | fixed | possibly | unaffected | unknown
    basis: record | fixes-tag | reference | description | versions | ""
    """
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
        fixes_bps = rec.get("_commit_fixes_bps")
        if fixes_bps is not None:
            for sha in fixes_bps:
                if ctx.contains(sha):
                    return "fixed", "reference"
            intros = rec.get("_commit_intros") or ()
            if intros:
                return ("affected" if any(ctx.contains(i) for i in intros) else "unaffected"), "fixes-tag"
            return ("possibly", "reference") if fixes_bps else ("unknown", "")
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


def is_fixed_here(rec: dict[str, Any], state: str, ctx: BaseReleaseContext) -> bool:
    """True when a fixed CVE has a fixing commit (or stable backport) first released in `ctx.release`."""
    if state != "fixed":
        return False
    if rec.get("kind") == "commit":
        fixes_bps = rec.get("_commit_fixes_bps")
        if fixes_bps is not None:
            return any(ctx.first_here(sha) for sha in fixes_bps)
        return any(
            ctx.first_here(r["fixed"]) or any(ctx.first_here(b) for b in ctx.backports(r["fixed"]))
            for r in (rec.get("ranges") or ()) if r.get("fixed")
        )
    return any(ctx.first_here(r["fixed"]) for r in (rec.get("ranges") or ()) if r.get("fixed"))


def first_final_release(tags_info: dict[str, str | None] | None, tag_names: set[str]) -> str | None:
    """Resolve the first final (non-`-rc`) mainline release tag containing a commit from its release-lookup
    tags (`{mainline, stable}`). Returns `None` when unversioned, stable-only, or `-rc`-only."""
    mainline = (tags_info or {}).get("mainline")
    if not mainline:
        return None
    p = parse_tag(mainline)
    if p is None or p.get("kind") != "mainline":
        return None
    return final_of(mainline, tag_names)


def earliest_final_release(tags_infos: Iterable[dict[str, str | None] | None], tag_names: set[str]) -> str | None:
    """Return the earliest first final mainline release tag across `tags_infos`, or `None` if none."""
    finals = [fr for info in tags_infos if (fr := first_final_release(info, tag_names))]
    return min(finals, key=release_key) if finals else None


class LookupReleaseContext(BaseReleaseContext):
    """In-memory `BaseReleaseContext` backed by pre-fetched bulk commit lookups."""

    def __init__(
        self,
        release: str,
        lookup: dict[str, Any],
        *,
        parsed_tags: dict[str, Any] | None = None,
        release_keys: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(release, parsed_tags=parsed_tags, release_keys=release_keys)
        self._short: dict[str, str] = lookup["short_to_full"]
        self._tags: dict[str, dict[str, str | None]] = lookup["sha_tags"]
        self._backports: dict[str, list[str]] = lookup["backports_map"]
        self._fixes: dict[str, list[str]] = lookup["fixes_map"]

    def _full(self, sha: str | None) -> str | None:
        if not sha:
            return None
        if _FULL.match(sha):
            return sha
        return self._short.get(sha)

    def tags(self, sha: str | None) -> dict[str, str | None] | None:
        if not sha:
            return None
        hit = self._tags.get(sha)
        if hit is not None:
            return hit
        full = self._short.get(sha)
        return self._tags.get(full) if full else None

    def backports(self, sha: str) -> list[str]:
        if not sha:
            return []
        hit = self._backports.get(sha)
        if hit is not None:
            return hit
        full = self._short.get(sha)
        return self._backports.get(full, []) if full else []

    def fixes_tags(self, sha: str) -> list[str]:
        if not sha:
            return []
        hit = self._fixes.get(sha)
        if hit is not None:
            return hit
        full = self._short.get(sha)
        return self._fixes.get(full, []) if full else []


def evaluate_segment_records(
    tag: str,
    recs: list[dict[str, Any]],
    lookup: dict[str, Any],
    *,
    parsed_tags: dict[str, Any] | None = None,
    release_keys: dict[str, Any] | None = None,
) -> list[tuple[str, int, int, int]]:
    """Evaluate every record in `recs` against `tag`.

    Returns `[(cve_id, state_id, basis_id, fixed_here), ...]` for matching records (`affected`, `possibly`, `fixed`).
    """
    ctx = LookupReleaseContext(tag, lookup, parsed_tags=parsed_tags, release_keys=release_keys)
    if ctx.p is None:
        return []
    out: list[tuple[str, int, int, int]] = []
    for rec in recs:
        state_str, basis_str = affects(rec, tag, ctx)
        state_id = STATE_TO_ID.get(state_str)
        if state_id is None:
            continue
        basis_id = BASIS_TO_ID.get(basis_str, BASIS_NONE)
        fh = 1 if is_fixed_here(rec, state_str, ctx) else 0
        out.append((rec["id"], state_id, basis_id, fh))
    return out


# --------------------------------------------------------------------------- Git & DB bulk lookup

def _git_has_commits(repo: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            capture_output=True,
            timeout=10,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _git_commit_messages(repo: Path, shas: list[str]) -> dict[str, str]:
    """Read raw commit messages for full SHAs using one `git cat-file --batch`."""
    if not shas:
        return {}
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=1024 * 1024,
    )
    payload = "".join(f"{s}\n" for s in shas if _FULL.match(s)).encode("ascii")
    assert proc.stdin is not None and proc.stdout is not None
    stdin = proc.stdin

    def _feed() -> None:
        # Fed from a thread: writing the whole payload before draining stdout deadlocks once
        # git's output fills the pipe buffer (git stops reading stdin while its stdout is full).
        try:
            stdin.write(payload)
        except OSError:
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    feeder = threading.Thread(target=_feed, name="cve-cat-file-feed", daemon=True)
    feeder.start()
    out: dict[str, str] = {}
    while True:
        header = proc.stdout.readline()
        if not header:
            break
        parts = header.split()
        if len(parts) < 2:
            continue
        if len(parts) >= 3 and parts[1] == b"commit":
            sha = parts[0].decode("ascii", "replace")
            size = int(parts[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)
            text = body.decode("utf-8", "replace")
            sep = text.find("\n\n")
            if sep >= 0:
                out[sha] = text[sep + 2:]
        elif len(parts) >= 3 and parts[1] in (b"blob", b"tree", b"tag"):
            size = int(parts[2])
            proc.stdout.read(size + 1)
    try:
        proc.stdout.close()
    except OSError:
        pass
    feeder.join(timeout=30)
    proc.wait(timeout=30)
    return out


def _build_backport_shards(repo: Path) -> list[list[str]] | None:
    """Partition `repo`'s release tags into disjoint rev-list shards for parallel `git log` scanning."""
    tags = release_tags(repo)
    if len(tags) < 32:
        return None
    mainline_tags: list[tuple[tuple, str, dict[str, Any]]] = []
    by_series: dict[tuple, list[tuple[tuple, str]]] = defaultdict(list)
    for t in tags:
        p = parse_tag(t)
        if not p:
            continue
        k = release_key(t)
        if p["kind"] == "mainline":
            mainline_tags.append((k, t, p))
        else:
            by_series[(p["family"], p["kind"], p["series"])].append((k, t))
    if not mainline_tags:
        return None
    mainline_tags.sort()
    finals = [f"refs/tags/{t}" for _, t, p in mainline_tags if p["final"]]
    if not finals:
        return None
    ml_tip = f"refs/tags/{mainline_tags[-1][1]}"
    not_finals = [f"^{r}" for r in finals] + [f"^{ml_tip}"]

    workers = min(os.cpu_count() or 4, 16)
    shards: list[list[str]] = [[] for _ in range(workers)]
    if by_series and workers >= 3:
        cut_tag = finals[(len(finals) * 2) // 3]
        cut_key = release_key(cut_tag.removeprefix("refs/tags/"))
        shards[0] = [f"refs/tags/{t}" for k, t, _ in mainline_tags if k <= cut_key]
        after_cut = [f"refs/tags/{t}" for k, t, _ in mainline_tags if k > cut_key]
        if after_cut:
            shards[1] = after_cut + [f"^{r}" for r in finals if release_key(r.removeprefix("refs/tags/")) <= cut_key]
        series_items = sorted(by_series.items(), key=lambda kv: len(kv[1]), reverse=True)
        loads = [0] * (workers - 2)
        for _k, items in series_items:
            best = min(range(workers - 2), key=lambda i: loads[i])
            shards[2 + best].extend(f"refs/tags/{t}" for _, t in items)
            loads[best] += len(items)
        for i in range(2, workers):
            if shards[i]:
                shards[i].extend(not_finals)
    else:
        step = max(1, (len(finals) + workers - 1) // workers)
        prev_cut: list[str] = []
        for idx, start in enumerate(range(0, len(finals), step)):
            if idx >= workers:
                break
            end = min(len(finals), start + step)
            chunk_refs = finals[start:end]
            if end == len(finals):
                chunk_refs = [*chunk_refs, ml_tip]
            shards[idx] = [*chunk_refs, *prev_cut]
            prev_cut.extend(f"^{r}" for r in finals[start:end])
    return [s for s in shards if s]


def _scan_backport_shard(
    repo: Path,
    revs: list[str] | None,
    greps: list[str],
    targets_b: set[bytes],
    prefix7_b: set[bytes],
) -> list[tuple[str, str]]:
    cmd = ["git", "-C", str(repo), "log", "--no-color", "--no-merges", "-F", "-i", *greps, "--format=%x1e%H%x1f%B"]
    if revs is None:
        cmd.append("--all")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1024 * 1024)
    else:
        cmd.append("--stdin")
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1024 * 1024
        )
        assert proc.stdin is not None
        payload = ("\n".join(revs) + "\n").encode("utf-8")

        def _feed() -> None:
            try:
                proc.stdin.write(payload)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        feeder = threading.Thread(target=_feed, name="cve-bp-feed", daemon=True)
        feeder.start()
    assert proc.stdout is not None
    hits: list[tuple[str, str]] = []
    tail = b""

    def _parse_chunk(chunk_b: bytes) -> None:
        if not chunk_b:
            return
        sep = chunk_b.find(b"\x1f")
        if sep < 0:
            return
        sha_b = chunk_b[:sep].strip().lower()
        if not _FULL_BYTES.match(sha_b):
            return
        bp_sha: str | None = None
        for m in _UPSTREAM_BYTES_RE.finditer(chunk_b, sep + 1):
            up_b = (m.group(1) or m.group(2) or m.group(3)).lower()
            if up_b in targets_b or (len(up_b) < 40 and up_b[:7] in prefix7_b):
                if bp_sha is None:
                    bp_sha = sha_b.decode("ascii")
                hits.append((bp_sha, up_b.decode("ascii")))

    try:
        while True:
            buf = proc.stdout.read(4 * 1024 * 1024)
            if not buf:
                break
            data = tail + buf
            parts = data.split(b"\x1e")
            tail = parts.pop()
            for part in parts:
                _parse_chunk(part)
        if tail:
            _parse_chunk(tail)
    finally:
        proc.stdout.close()
        proc.wait(timeout=600)
        if revs is not None:
            feeder.join(timeout=30)
    return hits


def _git_backports_map(repo: Path, targets: set[str]) -> dict[str, list[str]]:
    """Scan Git commit messages for `upstream` / `cherry picked` references to `targets` (full SHAs)."""
    norm_targets = {t.lower() for t in targets if t}
    if not norm_targets or not _git_has_commits(repo):
        return {}
    # Index targets by their first 7 hex chars (UPSTREAM_RES requires 7-40 hex chars) for O(1) prefix matching
    by_prefix7: dict[str, list[str]] = defaultdict(list)
    for sha in norm_targets:
        by_prefix7[sha[:7]].append(sha)
    targets_b = {s.encode("ascii") for s in norm_targets}
    prefix7_b = {p.encode("ascii") for p in by_prefix7}
    greps = (
        [f"--grep={p}" for p in sorted(by_prefix7)]
        if len(by_prefix7) <= 4
        else ["--grep=upstream", "--grep=cherry picked from commit"]
    )

    shards = _build_backport_shards(repo)
    if shards and len(shards) > 1:
        with ThreadPoolExecutor(max_workers=len(shards)) as pool:
            all_hits = [
                pair
                for shard_hits in pool.map(
                    lambda revs: _scan_backport_shard(repo, revs, greps, targets_b, prefix7_b), shards
                )
                for pair in shard_hits
            ]
    else:
        all_hits = _scan_backport_shard(repo, None, greps, targets_b, prefix7_b)

    backports: dict[str, list[str]] = defaultdict(list)
    ambiguous_prefixes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for bp_sha, up in all_hits:
        if up in norm_targets:
            if bp_sha not in backports[up]:
                backports[up].append(bp_sha)
            continue
        if len(up) < 40:
            cands = [t for t in by_prefix7.get(up[:7], ()) if t.startswith(up)]
            if len(cands) == 1:
                ambiguous_prefixes[up].append((bp_sha, cands[0]))
            elif len(cands) > 1:
                ambiguous_prefixes[up].append((bp_sha, ""))
    if ambiguous_prefixes:
        resolved = resolve_prefixes(repo, ambiguous_prefixes.keys())
        for up, items in ambiguous_prefixes.items():
            full = resolved.get(up)
            if full and full in norm_targets:
                for bp_sha, _ in items:
                    if bp_sha not in backports[full]:
                        backports[full].append(bp_sha)
    return dict(backports)


def _db_covers_commits(db: Any, repo_id: int, network_id: int | None) -> bool:
    """True when `m_commit_relation` finished backfilling and `m_commit` holds all commits of `m_commit_release`."""
    from db_engine.spec import NotExists

    if network_id is None:
        return False
    try:
        row = db.select_one(
            "m_commit_relation", ["note"], {"commit_id": 0, "kind": KIND_BACKFILL_STATE, "idx": 0}, as_dict=True
        )
        if not row or not _text(row["note"]).startswith(BACKFILL_DONE):
            return False
        if db.select_one("m_release_segment", ["segment_id"], {"repo_id": repo_id}) is None:
            return False
        missing = db.select_one(
            ("m_commit_release", "cr"),
            ["cr.repo_id"],
            [
                {"cr.repo_id": repo_id},
                NotExists(("m_commit", "c"), {"c.commit_hash": "cr.commit_hash"}, {"c.network_id": network_id}),
            ],
        )
        return missing is None
    except Exception:  # noqa: BLE001
        return False


def build_lookup_data(
    db: Any,
    repo_id: int,
    recs: list[dict[str, Any]],
    kernel_repo: Path | None = None,
    *,
    prefixes_resolved: bool = False,
    seg_tags: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Resolve abbreviated SHAs, release membership tags, and `commit`-kind relations in bulk."""
    raw_shas = [
        s
        for rec in recs
        for r in (rec.get("ranges") or ())
        for s in (r.get("introduced"), r.get("fixed"))
        if s
    ]
    short_shas = sorted({s for s in raw_shas if not _FULL.match(s) and HEXSHA.match(s)})
    short_to_full: dict[str, str] = {}
    if short_shas:
        if len(short_shas) > _SHA_BATCH:
            short_set = set(short_shas)
            for prefix_raw, ch_raw in db.select_rows(
                "m_cve_commit_hash", ["prefix", "commit_hash"], {"repo_id": repo_id}, as_dict=False
            ):
                p_str = _text(prefix_raw)
                if p_str in short_set:
                    short_to_full[p_str] = _hex(ch_raw)
        else:
            for i in range(0, len(short_shas), _SHA_BATCH):
                part = short_shas[i:i + _SHA_BATCH]
                for prefix_raw, ch_raw in db.select_rows(
                    "m_cve_commit_hash", ["prefix", "commit_hash"], {"repo_id": repo_id, "prefix": part}, as_dict=False
                ):
                    short_to_full[_text(prefix_raw)] = _hex(ch_raw)
        if kernel_repo is not None and not prefixes_resolved:
            missing = [s for s in short_shas if s not in short_to_full]
            if missing:
                short_to_full.update(resolve_prefixes(kernel_repo, missing))

    def to_full(sha: str | None) -> str | None:
        if not sha:
            return None
        return sha if _FULL.match(sha) else short_to_full.get(sha)

    commit_fixes = sorted({
        full
        for rec in recs
        if rec.get("kind") == "commit"
        for r in (rec.get("ranges") or ())
        if r.get("fixed") and (full := to_full(r.get("fixed")))
    })

    fixes_map: dict[str, list[str]] = {}
    backports_map: dict[str, list[str]] = {}

    if commit_fixes:
        repo_row = db.select_one("m_repository", ["network_id"], {"repo_id": repo_id}, as_dict=True)
        network_id = int(repo_row["network_id"]) if repo_row and repo_row.get("network_id") is not None else None
        use_db_relations = kernel_repo is None or _db_covers_commits(db, repo_id, network_id)

        if use_db_relations and network_id is not None:
            cid_to_sha: dict[int, str] = {}
            for i in range(0, len(commit_fixes), _SHA_BATCH):
                part = [bytes.fromhex(s) for s in commit_fixes[i:i + _SHA_BATCH]]
                for r in db.select_rows(
                    "m_commit", ["commit_id", "commit_hash"], {"network_id": network_id, "commit_hash": part}, as_dict=True
                ):
                    cid_to_sha[int(r["commit_id"])] = _hex(r["commit_hash"])
            if cid_to_sha:
                cids = sorted(cid_to_sha)
                unresolved_fx: list[tuple[str, str]] = []
                for i in range(0, len(cids), _SHA_BATCH):
                    part = cids[i:i + _SHA_BATCH]
                    for r in db.select_rows(
                        "m_commit_relation",
                        ["commit_id", "idx", "target_hash", "target_prefix"],
                        {"commit_id": part, "kind": KIND_FIXES},
                        order_by=["commit_id", "idx"],
                        as_dict=True,
                    ):
                        src_sha = cid_to_sha[int(r["commit_id"])]
                        th = bytes(r["target_hash"]) if r.get("target_hash") is not None else ZERO_HASH
                        if th != ZERO_HASH:
                            t_sha = th.hex()
                            lst = fixes_map.setdefault(src_sha, [])
                            if t_sha not in lst:
                                lst.append(t_sha)
                        else:
                            tp = _text(r.get("target_prefix")).strip().lower()
                            if tp:
                                unresolved_fx.append((src_sha, tp))
                if unresolved_fx and kernel_repo is not None:
                    res_fx = resolve_prefixes(kernel_repo, {tp for _, tp in unresolved_fx})
                    for src_sha, tp in unresolved_fx:
                        full = res_fx.get(tp)
                        if full:
                            lst = fixes_map.setdefault(src_sha, [])
                            if full not in lst:
                                lst.append(full)

            for i in range(0, len(commit_fixes), _SHA_BATCH):
                part = [bytes.fromhex(s) for s in commit_fixes[i:i + _SHA_BATCH]]
                for r in db.select_rows(
                    ("m_commit_relation", "r"),
                    {"target_hash": "r.target_hash", "commit_hash": "c.commit_hash"},
                    {"r.kind": KIND_UPSTREAM, "r.target_hash": part, "c.network_id": network_id},
                    joins=[Join(("m_commit", "c"), {"c.commit_id": "r.commit_id"})],
                    as_dict=True,
                ):
                    target = _hex(r["target_hash"])
                    bp = _hex(r["commit_hash"])
                    lst = backports_map.setdefault(target, [])
                    if bp not in lst:
                        lst.append(bp)
        elif kernel_repo is not None:
            msgs = _git_commit_messages(kernel_repo, commit_fixes)
            raw_fx: dict[str, list[str]] = {}
            all_fx_prefixes: set[str] = set()
            for sha, msg in msgs.items():
                prefs = [p for p, _subj in parse_relations(msg)["fixes"] if p]
                if prefs:
                    raw_fx[sha] = prefs
                    all_fx_prefixes.update(prefs)
            if all_fx_prefixes:
                res_fx = resolve_prefixes(kernel_repo, all_fx_prefixes)
                for sha, prefs in raw_fx.items():
                    resolved_lst: list[str] = []
                    for p in prefs:
                        full = res_fx.get(p)
                        if full and full not in resolved_lst:
                            resolved_lst.append(full)
                    if resolved_lst:
                        fixes_map[sha] = resolved_lst
            backports_map = _git_backports_map(kernel_repo, set(commit_fixes))

    needed_shas = sorted({
        *{full for s in raw_shas if (full := to_full(s))},
        *{x for lst in fixes_map.values() for x in lst},
        *{x for lst in backports_map.values() for x in lst},
    })
    if seg_tags is None:
        seg_tags = {
            int(sid): _text(tag)
            for sid, tag in db.select_rows(
                "m_release_segment", ["segment_id", "tag"], {"repo_id": repo_id}, as_dict=False
            )
        }
    sha_tags: dict[str, dict[str, str | None]] = {}
    for i in range(0, len(needed_shas), _SHA_BATCH):
        part = [bytes.fromhex(s) for s in needed_shas[i:i + _SHA_BATCH]]
        for ch_raw, chain_raw, sid_raw in db.select_rows(
            "m_commit_release",
            ["commit_hash", "chain", "segment_id"],
            {"repo_id": repo_id, "commit_hash": part},
            as_dict=False,
        ):
            tag = seg_tags.get(int(sid_raw))
            if tag is None:
                continue
            sha = _hex(ch_raw)
            entry = sha_tags.setdefault(sha, {"mainline": None, "stable": None})
            entry["mainline" if int(chain_raw) == DB_CHAIN_MAINLINE else "stable"] = tag

    for short, full in short_to_full.items():
        if full in sha_tags:
            sha_tags[short] = sha_tags[full]
        if full in fixes_map:
            fixes_map[short] = fixes_map[full]
        if full in backports_map:
            backports_map[short] = backports_map[full]

    return {
        "short_to_full": short_to_full,
        "sha_tags": sha_tags,
        "fixes_map": fixes_map,
        "backports_map": backports_map,
    }


# --------------------------------------------------------------------------- DB readers & incremental sync

def _cve_source(db: Any) -> dict[str, Any] | None:
    row = db.select_one(
        "m_cve_source", ["name", "layout", "head", "format", "records", "built_at"], order_by=["-built_at"], as_dict=True
    )
    if not row:
        return None
    return {
        "name": _text(row["name"]),
        "layout": _text(row["layout"]),
        "head": _hex(row["head"]),
        "format": int(row["format"]),
        "records": int(row["records"]),
        "built_at": int(row["built_at"]),
    }


def _load_records(db: Any, cve_ids: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if cve_ids is None:
        rows = list(db.select_rows("m_cve", ["cve_id", "data"], as_dict=False))
    else:
        for i in range(0, len(cve_ids), _CVE_BATCH):
            rows.extend(db.select_rows("m_cve", ["cve_id", "data"], {"cve_id": cve_ids[i:i + _CVE_BATCH]}, as_dict=False))
    out: list[dict[str, Any]] = []
    for r in rows:
        data = r["data"] if isinstance(r, dict) else r[1]
        if not data:
            continue
        try:
            rec = json.loads(zlib.decompress(bytes(data)))
        except (zlib.error, ValueError, TypeError):
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(normalize_record_tuples(rec))
    out.sort(key=lambda r: r["id"])
    return out


def cve_ids_for_prefixes(db: Any, prefixes: Iterable[str]) -> list[str]:
    """Return distinct CVE IDs in `m_cve_commit` referencing any commit prefix in `prefixes`."""
    prefs = sorted({str(p).strip().lower() for p in prefixes if p})
    if not prefs:
        return []
    out: set[str] = set()
    for i in range(0, len(prefs), _CVE_BATCH):
        part = prefs[i:i + _CVE_BATCH]
        for cid in db.select_values("m_cve_commit", "cve_id", {"commit_hash": part}, distinct=True):
            out.add(_text(cid))
    return sorted(out)


def unresolved_prefixes(db: Any, repo_id: int) -> list[str]:
    """Abbreviated commit hashes in `m_cve_commit` that currently lack an `m_cve_commit_hash` row for `repo_id`."""
    from db_engine.spec import Length, NotExists, Pred, lt

    rows = db.select_values(
        ("m_cve_commit", "c"),
        "c.commit_hash",
        [
            Pred(Length("c.commit_hash"), lt(40)),
            NotExists(("m_cve_commit_hash", "h"), {"h.prefix": "c.commit_hash"}, {"h.repo_id": repo_id}),
        ],
        distinct=True,
    )
    return [_text(r) for r in rows if r]


def cve_ids_for_segments(db: Any, repo_id: int, segment_ids: list[int]) -> list[str]:
    """CVE IDs in `m_cve_commit` whose commit (full, via `m_cve_commit_hash`, or `m_commit_relation`) lies in `segment_ids`."""
    if not segment_ids:
        return []
    hashes: list[bytes] = []
    for i in range(0, len(segment_ids), 200):
        hashes.extend(
            bytes(h)
            for h in db.select_values(
                "m_commit_release", "commit_hash", {"repo_id": repo_id, "segment_id": segment_ids[i:i + 200]}, distinct=True
            )
            if h
        )
    if not hashes:
        return []
    hash_set = set(hashes)
    hexes_set = {h.hex() for h in hash_set}

    has_rel_tables = (
        len(db.existing_tables(["m_repository", "m_commit", "m_commit_relation"])) == 3
        if hasattr(db, "existing_tables")
        else (
            not hasattr(db, "table_exists")
            or (
                db.table_exists("m_repository")
                and db.table_exists("m_commit")
                and db.table_exists("m_commit_relation")
            )
        )
    )
    if has_rel_tables:
        repo_row = db.select_one("m_repository", ["network_id"], {"repo_id": repo_id}, as_dict=True)
        network_id = int(repo_row["network_id"]) if repo_row and repo_row.get("network_id") is not None else None
        if network_id is not None:
            hash_list = sorted(hash_set)
            cids: list[int] = []
            for i in range(0, len(hash_list), _SHA_BATCH):
                part = hash_list[i:i + _SHA_BATCH]
                for cid in db.select_values("m_commit", "commit_id", {"network_id": network_id, "commit_hash": part}):
                    cids.append(int(cid))
            for i in range(0, len(cids), _SHA_BATCH):
                part = cids[i:i + _SHA_BATCH]
                for th in db.select_values(
                    "m_commit_relation", "target_hash", {"commit_id": part, "kind": KIND_UPSTREAM}
                ):
                    if th and bytes(th) != ZERO_HASH:
                        bth = bytes(th)
                        hash_set.add(bth)
                        hexes_set.add(bth.hex())
            for i in range(0, len(hash_list), _SHA_BATCH):
                part = hash_list[i:i + _SHA_BATCH]
                for r in db.select_rows(
                    ("m_commit_relation", "r"),
                    ["c.commit_hash"],
                    {"r.kind": KIND_FIXES, "r.target_hash": part, "c.network_id": network_id},
                    joins=[Join(("m_commit", "c"), {"c.commit_id": "r.commit_id"})],
                    as_dict=False,
                ):
                    if r and r[0]:
                        bch = bytes(r[0])
                        hash_set.add(bch)
                        hexes_set.add(bch.hex())

    hexes = sorted(hexes_set)
    out: set[str] = set()
    for i in range(0, len(hexes), _CVE_BATCH):
        part = hexes[i:i + _CVE_BATCH]
        for cid in db.select_values("m_cve_commit", "cve_id", {"commit_hash": part}, distinct=True):
            out.add(_text(cid))
    # Also resolve prefixes in m_cve_commit_hash pointing to these commit hashes using repo_id PK prefix
    prefixes = [
        _text(p)
        for p, ch in db.select_rows(
            "m_cve_commit_hash", ["prefix", "commit_hash"], {"repo_id": repo_id}, as_dict=False
        )
        if ch and bytes(ch) in hash_set
    ]
    if prefixes:
        out.update(cve_ids_for_prefixes(db, prefixes))
    return sorted(out)


def _cve_ids_since(db: Any, repo_id: int, segment_ids: list[int], watermarks: Iterable[int]) -> dict[int, set[str]]:
    """For each watermark, the CVE IDs whose commits lie in a segment of `segment_ids` above it (one band query per watermark)."""
    out: dict[int, set[str]] = {}
    acc: set[str] = set()
    upper: int | None = None
    for wm in sorted(set(watermarks), reverse=True):
        band = [sid for sid in segment_ids if sid > wm and (upper is None or sid <= upper)]
        if band:
            acc.update(cve_ids_for_segments(db, repo_id, band))
        out[wm] = set(acc)
        upper = wm
    return out


def _process_workers() -> int:
    return max(1, min(os.cpu_count() or 4, 16) - 1)


def _slim_record(rec: dict[str, Any], lookup: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep only fields read by `evaluate_segment_records()` before sending records to worker processes."""
    kind = rec.get("kind", "cna")
    slim: dict[str, Any] = {
        "id": rec["id"],
        "kind": kind,
        "ranges": [
            {"introduced": r.get("introduced"), "fixed": r.get("fixed")}
            for r in (rec.get("ranges") or ())
        ],
        "entries": rec.get("entries") or [],
    }
    if lookup is not None and kind == "commit":
        short_to_full = lookup["short_to_full"]
        bp_map = lookup["backports_map"]
        fx_map = lookup["fixes_map"]
        fixes_bps: list[str] = []
        seen_fb: set[str] = set()
        intros: list[str] = []
        seen_in: set[str] = set()
        for r in slim["ranges"]:
            f = r.get("fixed")
            if not f:
                continue
            full_f = f if _FULL.match(f) else short_to_full.get(f)
            cand_f = full_f or f
            if cand_f not in seen_fb:
                seen_fb.add(cand_f)
                fixes_bps.append(cand_f)
            for b in bp_map.get(cand_f, ()):
                if b not in seen_fb:
                    seen_fb.add(b)
                    fixes_bps.append(b)
            for intro in fx_map.get(cand_f, ()):
                if intro not in seen_in:
                    seen_in.add(intro)
                    intros.append(intro)
        slim["_commit_fixes_bps"] = tuple(fixes_bps)
        slim["_commit_intros"] = tuple(intros)
    return slim


_POOL_DATA: dict[str, Any] = {}


def _close_worker_db() -> None:
    w_db = _POOL_DATA.pop("db", None)
    if w_db is not None:
        try:
            w_db.close()
        except Exception:  # noqa: BLE001
            pass


def _init_worker(
    recs: list[dict[str, Any]],
    lookup: dict[str, Any],
    ptags: dict[str, Any],
    rkeys: dict[str, Any],
    db_cls: Any = None,
    repo_id: int = 0,
    src: dict[str, Any] | None = None,
    built_at: int = 0,
    max_sid: int = 0,
) -> None:
    _POOL_DATA["recs"] = recs
    _POOL_DATA["lookup"] = lookup
    _POOL_DATA["ptags"] = ptags
    _POOL_DATA["rkeys"] = rkeys
    _POOL_DATA["repo_id"] = repo_id
    _POOL_DATA["src"] = src
    _POOL_DATA["built_at"] = built_at
    _POOL_DATA["max_sid"] = max_sid
    if db_cls is not None:
        if not os.environ.get("DB_USER"):
            from core.config import get_config, sync_environ
            sync_environ(get_config())
        _POOL_DATA["db"] = db_cls()
        atexit.register(_close_worker_db)


def _worker_eval(item: tuple[int, str]) -> tuple[int, list[tuple[str, int, int, int]]]:
    seg_id, tag = item
    rows = evaluate_segment_records(
        tag,
        _POOL_DATA["recs"],
        _POOL_DATA["lookup"],
        parsed_tags=_POOL_DATA["ptags"],
        release_keys=_POOL_DATA["rkeys"],
    )
    return seg_id, rows


def _worker_eval_and_write_batch(batch: list[dict[str, Any]]) -> list[tuple[int, str, int]]:
    recs = _POOL_DATA["recs"]
    lookup = _POOL_DATA["lookup"]
    ptags = _POOL_DATA["ptags"]
    rkeys = _POOL_DATA["rkeys"]
    evaluated = [
        (
            seg,
            evaluate_segment_records(
                seg["tag"], recs, lookup, parsed_tags=ptags, release_keys=rkeys
            ),
        )
        for seg in batch
    ]
    w_db = _POOL_DATA["db"]
    repo_id = _POOL_DATA["repo_id"]
    src = _POOL_DATA["src"]
    built_at = _POOL_DATA["built_at"]
    max_sid = _POOL_DATA["max_sid"]
    for attempt in range(3):
        try:
            _write_segments_full_batch(w_db, repo_id, evaluated, src, built_at, max_sid)
            break
        except (DeadlockError, ConnectionLostError):
            if attempt == 2:
                raise
            w_db.check_if_connected()
    return [(seg["segment_id"], seg["tag"], len(rows)) for seg, rows in evaluated]


_STATE_COLUMNS = [
    "segment_id", "repo_id", "tag_hash", "cve_head", "cve_layout", "cve_format", "rel_format", "built_at", "max_segment_id",
]


def _state_row(repo_id: int, seg: dict[str, Any], src: dict[str, Any], built_at: int, max_sid: int) -> tuple:
    """`m_cve_release_state` row (`_STATE_COLUMNS` order) of `seg` evaluated at `src["head"]` against segments up to `max_sid`."""
    return (
        seg["segment_id"],
        repo_id,
        bytes.fromhex(seg["tag_hash"]),
        bytes.fromhex(src["head"]),
        src["layout"],
        src["format"],
        CVE_RELEASE_FORMAT,
        built_at,
        max_sid,
    )


def _write_segment_full(
    db: Any,
    repo_id: int,
    seg: dict[str, Any],
    src: dict[str, Any],
    rows: list[tuple[str, int, int, int]],
    built_at: int,
    max_sid: int,
) -> None:
    seg_id = seg["segment_id"]
    rel_rows = [(seg_id, cid, st_id, b_id, fh) for cid, st_id, b_id, fh in rows]
    state_row = _state_row(repo_id, seg, src, built_at, max_sid)
    with db.transaction():
        db.delete_rows("m_cve_release", {"segment_id": seg_id})
        for i in range(0, len(rel_rows), _INSERT_CHUNK):
            db.insert_rows(
                "m_cve_release",
                ["segment_id", "cve_id", "state", "basis", "fixed_here"],
                rel_rows[i:i + _INSERT_CHUNK],
                on_conflict="error",
            )
        db.insert_rows(
            "m_cve_release_state",
            _STATE_COLUMNS,
            [state_row],
            on_conflict="update",
        )


def _write_segments_full_batch(
    db: Any,
    repo_id: int,
    batch: list[tuple[dict[str, Any], list[tuple[str, int, int, int]]]],
    src: dict[str, Any],
    built_at: int,
    max_sid: int,
) -> None:
    seg_ids = [seg["segment_id"] for seg, _rows in batch]
    rel_rows = [
        (seg["segment_id"], cid, st_id, b_id, fh)
        for seg, rows in batch
        for cid, st_id, b_id, fh in rows
    ]
    state_rows = [_state_row(repo_id, seg, src, built_at, max_sid) for seg, _rows in batch]
    with db.transaction():
        if db.row_exists("m_cve_release", {"segment_id": seg_ids}):
            db.delete_rows("m_cve_release", {"segment_id": seg_ids})
        for i in range(0, len(rel_rows), _INSERT_CHUNK):
            db.insert_rows(
                "m_cve_release",
                ["segment_id", "cve_id", "state", "basis", "fixed_here"],
                rel_rows[i:i + _INSERT_CHUNK],
                on_conflict="error",
            )
        db.insert_rows(
            "m_cve_release_state",
            _STATE_COLUMNS,
            state_rows,
            on_conflict="update",
        )


def _write_segment_incremental(
    db: Any,
    repo_id: int,
    seg: dict[str, Any],
    src: dict[str, Any],
    diff_ids: list[str],
    rows: list[tuple[str, int, int, int]],
    built_at: int,
    max_sid: int,
) -> None:
    seg_id = seg["segment_id"]
    rel_rows = [(seg_id, cid, st_id, b_id, fh) for cid, st_id, b_id, fh in rows]
    state_row = _state_row(repo_id, seg, src, built_at, max_sid)
    with db.transaction():
        for i in range(0, len(diff_ids), _CVE_BATCH):
            db.delete_rows("m_cve_release", {"segment_id": seg_id, "cve_id": diff_ids[i:i + _CVE_BATCH]})
        for i in range(0, len(rel_rows), _INSERT_CHUNK):
            db.insert_rows(
                "m_cve_release",
                ["segment_id", "cve_id", "state", "basis", "fixed_here"],
                rel_rows[i:i + _INSERT_CHUNK],
                on_conflict="update",
            )
        db.insert_rows(
            "m_cve_release_state",
            _STATE_COLUMNS,
            [state_row],
            on_conflict="update",
        )


def _write_segments_incremental_batch(
    db: Any,
    repo_id: int,
    segs: list[dict[str, Any]],
    src: dict[str, Any],
    diff_ids: list[str],
    eval_rows: list[tuple[int, list[tuple[str, int, int, int]]]],
    built_at: int,
    max_sid: int,
) -> None:
    seg_ids = [s["segment_id"] for s in segs]
    rel_rows = [
        (seg_id, cid, st_id, b_id, fh)
        for seg_id, rows in eval_rows
        for cid, st_id, b_id, fh in rows
    ]
    state_rows = [_state_row(repo_id, s, src, built_at, max_sid) for s in segs]
    with db.transaction():
        for j in range(0, len(diff_ids), _SEG_INC_BATCH):
            db.delete_rows(
                "m_cve_release",
                {"segment_id": seg_ids, "cve_id": diff_ids[j:j + _SEG_INC_BATCH]},
            )
        for i in range(0, len(rel_rows), _INSERT_CHUNK):
            db.insert_rows(
                "m_cve_release",
                ["segment_id", "cve_id", "state", "basis", "fixed_here"],
                rel_rows[i:i + _INSERT_CHUNK],
                on_conflict="update",
            )
        for i in range(0, len(state_rows), _INSERT_CHUNK):
            db.insert_rows(
                "m_cve_release_state",
                _STATE_COLUMNS,
                state_rows[i:i + _INSERT_CHUNK],
                on_conflict="update",
            )


_DEFAULT_WRITE_SEGMENT_FULL = _write_segment_full
_DEFAULT_WRITE_SEGMENT_INCREMENTAL = _write_segment_incremental


def sync(
    db: Any,
    repo_id: int,
    *,
    kernel_repo: Path | None = None,
    cve_repo: Path | None = None,
    prev_head: str | None = None,
    changed_ids: list[str] | None = None,
    extra_cve_ids: list[str] | None = None,
    force: bool = False,
    from_tag: str | None = None,
    progress: Callable[..., None] | None = None,
    prefixes_resolved: bool = False,
) -> dict[str, Any]:
    """Bring `m_cve_release` and `m_cve_release_state` for `repo_id` up to `m_cve_source.head`.

    Returns summary `{state: "missing" | "unavailable" | "current" | "updated", segments, incremental, full, seconds}`.
    """
    started = time.time()
    if db is None:
        return {"state": "unavailable", "segments": 0, "incremental": 0, "full": 0, "seconds": round(time.time() - started, 1)}
    req_tables = ("m_cve_source", "m_release_segment", "m_cve_release_state", "m_cve_release")
    if hasattr(db, "existing_tables"):
        if len(db.existing_tables(req_tables)) < len(req_tables):
            return {"state": "unavailable", "segments": 0, "incremental": 0, "full": 0, "seconds": round(time.time() - started, 1)}
    elif hasattr(db, "table_exists"):
        if not all(db.table_exists(t) for t in req_tables):
            return {"state": "unavailable", "segments": 0, "incremental": 0, "full": 0, "seconds": round(time.time() - started, 1)}

    if hasattr(db, "index_exists") and hasattr(db, "drop_tables") and hasattr(db, "create_table"):
        if db.index_exists("cve_release_cve_idx", m_cve_release):
            logger.info(
                "[CVE Releases] Dropping outdated m_cve_release / m_cve_release_state (cve_release_cve_idx); "
                "recreating without secondary index."
            )
            db.drop_tables(["m_cve_release", "m_cve_release_state"], ignore_fk=True, if_exists=True)
            db.create_table([m_cve_release_state, m_cve_release])

    src = _cve_source(db)
    if src is None:
        return {"state": "missing", "segments": 0, "incremental": 0, "full": 0, "seconds": round(time.time() - started, 1)}

    seg_rows = db.select_rows(
        "m_release_segment",
        ["segment_id", "tag", "tag_hash"],
        {"repo_id": repo_id},
        as_dict=True,
    )
    if not seg_rows:
        return {"state": "current", "segments": 0, "incremental": 0, "full": 0, "head": src["head"], "seconds": round(time.time() - started, 1)}

    segments: list[dict[str, Any]] = []
    seg_tags: dict[int, str] = {}
    for r in seg_rows:
        sid = int(r["segment_id"])
        tag = _text(r["tag"])
        seg_tags[sid] = tag
        segments.append({"segment_id": sid, "tag": tag, "tag_hash": _hex(r["tag_hash"])})
    all_sids = [s["segment_id"] for s in segments]
    max_sid = max(all_sids)
    segments.sort(key=lambda s: release_key(s["tag"]))
    if from_tag:
        from_key = release_key(from_tag)
        segments = [s for s in segments if release_key(s["tag"]) >= from_key]
    if not segments:
        return {"state": "current", "segments": 0, "incremental": 0, "full": 0, "head": src["head"], "seconds": round(time.time() - started, 1)}

    state_rows = db.select_rows(
        "m_cve_release_state",
        ["segment_id", "tag_hash", "cve_head", "cve_layout", "cve_format", "rel_format", "max_segment_id"],
        {"repo_id": repo_id},
        as_dict=True,
    )
    states = {
        int(r["segment_id"]): {
            "tag_hash": _hex(r["tag_hash"]),
            "cve_head": _hex(r["cve_head"]),
            "cve_layout": _text(r["cve_layout"]),
            "cve_format": int(r["cve_format"]),
            "rel_format": int(r["rel_format"]),
            "max_segment_id": int(r["max_segment_id"] or 0),
        }
        for r in state_rows
    }

    extra_set: set[str] = set(extra_cve_ids or ())

    full_segs: list[dict[str, Any]] = []
    # (old cve_head, max_segment_id of the stored verdicts when segments were added since, else None) -> segments
    inc_groups: dict[tuple[str, int | None], list[dict[str, Any]]] = defaultdict(list)
    compatible_segs: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for seg in segments:
        st = states.get(seg["segment_id"])
        compatible = (
            not force
            and st is not None
            and st["tag_hash"] == seg["tag_hash"]
            and st["cve_layout"] == src["layout"]
            and st["cve_format"] == src["format"]
            and st["rel_format"] == CVE_RELEASE_FORMAT
        )
        if not compatible:
            full_segs.append(seg)
            continue
        compatible_segs.append((seg, st))

    # Segments added after a stored verdict was evaluated (segment_id above its max_segment_id) can change it for
    # the CVEs whose commits they contain. Segments that merely lack a state (an interrupted first build) do not:
    # the stored verdicts already saw them.
    new_seg_ids = _cve_ids_since(
        db, repo_id, all_sids, {st["max_segment_id"] for _, st in compatible_segs if st["max_segment_id"] < max_sid}
    )
    for seg, st in compatible_segs:
        lag = st["max_segment_id"] if st["max_segment_id"] < max_sid else None
        if st["cve_head"] == src["head"] and not extra_set and lag is None:
            continue
        inc_groups[(st["cve_head"], lag)].append(seg)

    # Resolve diff CVE IDs per distinct old `cve_head`; any group whose diff cannot be computed falls back to full.
    inc_plans: list[tuple[list[dict[str, Any]], list[str]]] = []
    root = LAYOUTS.get(src["layout"])
    diff_cache: dict[str, list[str] | None] = {}
    for (old_head, lag), grp_segs in inc_groups.items():
        extra = extra_set | (new_seg_ids.get(lag, set()) if lag is not None else set())
        if old_head == src["head"]:
            inc_plans.append((grp_segs, sorted(extra)))
        elif prev_head and old_head == prev_head and changed_ids is not None:
            inc_plans.append((grp_segs, sorted(set(changed_ids) | extra)))
        elif cve_repo is not None and root:
            if old_head not in diff_cache:
                diff_cache[old_head] = changed_paths(cve_repo, old_head, src["head"], root)
            paths = diff_cache[old_head]
            if paths is None:
                full_segs.extend(grp_segs)
            else:
                inc_plans.append((grp_segs, sorted(set(_ids_of(paths)) | extra)))
        else:
            full_segs.extend(grp_segs)

    if not inc_plans and not full_segs:
        return {"state": "current", "segments": 0, "incremental": 0, "full": 0, "head": src["head"], "seconds": round(time.time() - started, 1)}

    built_at = int(time.time())
    inc_count = 0
    full_count = 0
    inc_total = sum(len(g) for g, _ in inc_plans)
    if inc_total:
        n_ids = len({cid for _, ids in inc_plans for cid in ids})
        logger.info(f"[CVE Releases] {inc_total} release segment(s) to refresh incrementally ({n_ids} changed CVE(s)).")
    last_log = time.time()

    def _tick(kind: str, done: int, total: int, tag: str, rows: int) -> None:
        nonlocal last_log
        if progress and done % 25 == 0:
            progress(done, total, tag)
        if time.time() - last_log > 20:
            logger.info(f"[CVE Releases] {kind} {done}/{total} ({tag}: {rows} CVE verdict(s))")
            last_log = time.time()

    need_inc_ids = sorted({cid for _, ids in inc_plans for cid in ids if cid})
    all_recs: list[dict[str, Any]] = []
    rec_by_id: dict[str, dict[str, Any]] = {}
    lookup: dict[str, Any] = {}
    ptags: dict[str, Any] = {}
    rkeys: dict[str, Any] = {}

    if full_segs:
        all_recs = _load_records(db)
        rec_by_id = {r["id"]: r for r in all_recs}
        lookup = build_lookup_data(
            db, repo_id, all_recs, kernel_repo=kernel_repo, prefixes_resolved=prefixes_resolved, seg_tags=seg_tags
        )
    elif need_inc_ids:
        inc_recs = _load_records(db, need_inc_ids)
        rec_by_id = {r["id"]: r for r in inc_recs}
        if inc_recs:
            lookup = build_lookup_data(
                db, repo_id, inc_recs, kernel_repo=kernel_repo, prefixes_resolved=prefixes_resolved, seg_tags=seg_tags
            )

    if lookup:
        all_tags = {s["tag"] for s in full_segs} | {s["tag"] for g, _ in inc_plans for s in g}
        for tags_entry in lookup["sha_tags"].values():
            if tags_entry.get("mainline"):
                all_tags.add(tags_entry["mainline"])
            if tags_entry.get("stable"):
                all_tags.add(tags_entry["stable"])
        ptags = {t: parse_tag(t) for t in all_tags}
        rkeys = {t: release_key(t) for t in all_tags}

    use_single_inc = _write_segment_incremental is not _DEFAULT_WRITE_SEGMENT_INCREMENTAL
    for grp_segs, diff_ids in inc_plans:
        if not diff_ids:
            # Head changed without touching any kernel CVE IDs (or lag watermark caught up with 0 affected CVEs)
            state_rows_out = [_state_row(repo_id, s, src, built_at, max_sid) for s in grp_segs]
            with db.transaction():
                for i in range(0, len(state_rows_out), _INSERT_CHUNK):
                    db.insert_rows(
                        "m_cve_release_state",
                        _STATE_COLUMNS,
                        state_rows_out[i:i + _INSERT_CHUNK],
                        on_conflict="update",
                    )
            inc_count += len(grp_segs)
            continue

        diff_recs = [rec_by_id[cid] for cid in diff_ids if cid in rec_by_id]
        if use_single_inc or len(grp_segs) == 1:
            for s in grp_segs:
                rows = (
                    evaluate_segment_records(
                        s["tag"], diff_recs, lookup, parsed_tags=ptags, release_keys=rkeys
                    )
                    if diff_recs
                    else []
                )
                _write_segment_incremental(db, repo_id, s, src, diff_ids, rows, built_at, max_sid)
                inc_count += 1
                _tick("incremental", inc_count, inc_total, s["tag"], len(rows))
        else:
            grp_segs.sort(key=lambda s: s["segment_id"])
            eval_rows: list[tuple[int, list[tuple[str, int, int, int]]]] = [
                (
                    s["segment_id"],
                    evaluate_segment_records(
                        s["tag"], diff_recs, lookup, parsed_tags=ptags, release_keys=rkeys
                    )
                    if diff_recs
                    else [],
                )
                for s in grp_segs
            ]
            for i in range(0, len(grp_segs), _SEG_INC_BATCH):
                seg_batch = grp_segs[i:i + _SEG_INC_BATCH]
                row_batch = eval_rows[i:i + _SEG_INC_BATCH]
                _write_segments_incremental_batch(
                    db, repo_id, seg_batch, src, diff_ids, row_batch, built_at, max_sid
                )
                inc_count += len(seg_batch)
                _tick("incremental", inc_count, inc_total, seg_batch[-1]["tag"], len(row_batch[-1][1]))

    if full_segs:
        full_segs.sort(key=lambda s: s["segment_id"])
        logger.info(
            f"[CVE Releases] {len(full_segs)} release segment(s) to evaluate in full against {len(all_recs)} CVE record(s)."
        )
        seg_by_id = {s["segment_id"]: s for s in full_segs}
        total = len(full_segs)
        written: set[int] = set()
        use_single_full = _write_segment_full is not _DEFAULT_WRITE_SEGMENT_FULL
        workers = _process_workers()
        use_pool = total >= 4 and len(all_recs) >= 200 and workers > 1
        can_parallel_write = (
            not use_single_full
            and hasattr(db, "has_capability")
            and bool(db.has_capability(CAP_CONCURRENT_CONNECTIONS))
        )
        pending_batch: list[tuple[dict[str, Any], list[tuple[str, int, int, int]]]] = []

        def _flush_full_batch() -> None:
            nonlocal full_count
            if not pending_batch:
                return
            if use_single_full or len(pending_batch) == 1:
                for seg_obj, seg_rows_out in pending_batch:
                    _write_segment_full(db, repo_id, seg_obj, src, seg_rows_out, built_at, max_sid)
                    written.add(seg_obj["segment_id"])
                    full_count += 1
                    _tick("full", full_count, total, seg_obj["tag"], len(seg_rows_out))
            else:
                _write_segments_full_batch(db, repo_id, pending_batch, src, built_at, max_sid)
                for seg_obj, seg_rows_out in pending_batch:
                    written.add(seg_obj["segment_id"])
                    full_count += 1
                    _tick("full", full_count, total, seg_obj["tag"], len(seg_rows_out))
            pending_batch.clear()

        if use_pool:
            try:
                slim_recs = [_slim_record(r, lookup) for r in all_recs]
                if can_parallel_write:
                    batches = [
                        full_segs[i:i + _FULL_SEGMENT_BATCH]
                        for i in range(0, len(full_segs), _FULL_SEGMENT_BATCH)
                    ]
                    with ProcessPoolExecutor(
                        max_workers=workers,
                        initializer=_init_worker,
                        initargs=(slim_recs, lookup, ptags, rkeys, type(db), repo_id, src, built_at, max_sid),
                    ) as pool:
                        for batch_stats in pool.map(_worker_eval_and_write_batch, batches, chunksize=1):
                            for seg_id, tag, n_rows in batch_stats:
                                written.add(seg_id)
                                full_count += 1
                                _tick("full", full_count, total, tag, n_rows)
                else:
                    items = [(s["segment_id"], s["tag"]) for s in full_segs]
                    chunksize = max(1, min(16, len(items) // (workers * 4)))
                    with ProcessPoolExecutor(
                        max_workers=workers,
                        initializer=_init_worker,
                        initargs=(slim_recs, lookup, ptags, rkeys),
                    ) as pool:
                        for seg_id, rows in pool.map(_worker_eval, items, chunksize=chunksize):
                            pending_batch.append((seg_by_id[seg_id], rows))
                            if len(pending_batch) >= _FULL_SEGMENT_BATCH:
                                _flush_full_batch()
                    _flush_full_batch()
            except (BrokenProcessPool, OSError, PermissionError):
                pending_batch.clear()
                use_pool = False

        if not use_pool:
            for s in full_segs:
                if s["segment_id"] in written:
                    continue
                rows = evaluate_segment_records(
                    s["tag"], all_recs, lookup, parsed_tags=ptags, release_keys=rkeys
                )
                pending_batch.append((s, rows))
                if len(pending_batch) >= _FULL_SEGMENT_BATCH:
                    _flush_full_batch()
            _flush_full_batch()

    elapsed = round(time.time() - started, 1)
    logger.info(
        f"[CVE Releases] Synced {inc_count + full_count} release segment(s) "
        f"({inc_count} incremental, {full_count} full) at {src['head'][:12]} in {elapsed}s."
    )
    return {
        "state": "updated",
        "segments": inc_count + full_count,
        "incremental": inc_count,
        "full": full_count,
        "head": src["head"],
        "seconds": elapsed,
    }
