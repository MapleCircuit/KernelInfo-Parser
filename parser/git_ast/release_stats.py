"""parser/git_ast/release_stats.py - Release statistics step (core/API.md section 10).

Summarizes every release tag of a repository, ingested or not, into `m_release_segment`,
`m_release_person` and `m_person_first_release`, so the webapp's Insights · Release stats
(webapp/API.md section 4.10) can be answered with a few SELECTs instead of re-reading the history:

- Ranges come from `release_order.release_ranges()`: a segment is the history between a tag and the
  previous tag of its chain (mainline releases and release candidates; each stable series), the
  cut of the webapp's release index; the diff range is the one a release report covers (a release's
  whole cycle).
- Per segment: one `git log` (merges included: commit, parents, author, message) whose trailers are read
  with the parser's own `iter_trailers()` (merges are counted, not summarized), and every commit of the
  segment stored in `m_commit_release` (the first segment of each chain containing it); per diff range
  one `git diff --shortstat`. Ranges run on `WORKERS` threads and are written in release order, one
  transaction per segment, so an interrupted run resumes where it stopped.
- Incremental: a tag already summarized with the same commit, bases and `FORMAT` costs nothing; a moved
  tag, a changed base (a tag inserted before it) or an older format is summarized again.

Persons are the `m_maintainer_person` rows version ingestion uses (`identity.person_key`); persons
first seen here are inserted. Run only while no version ingestion is interrupted (main.py checks):
a rollback deletes persons above the interrupted run's watermark together with the rows naming them.
"""
from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from db_engine.spec import Join
from parser.git_ast.git_commit_parser import iter_trailers
from parser.git_ast.git_types import CommitRole
from parser.git_ast.identity import identity_key, person_key
from parser.git_ast.release_order import parse_tag, release_key, release_ranges, release_tag_names

logger = logging.getLogger(__name__)


# Bump when the stored rows change meaning: every segment is summarized again.
# 2: m_commit_release rows (every commit of the segment), one git log with merges.
FORMAT = 2
WORKERS = 4
CHAIN_MAINLINE = 1
CHAIN_STABLE = 2
NAME_MAX = 128  # m_maintainer_person.name / email
_NO_HASH = b"\x00" * 20
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_RECORD = "\x1e"
_FIELD = "\x1f"
_LOG_FORMAT = "--format=%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%B"
INSERT_CHUNK = 5000
# Trailer roles counted (m_bridge_commit_person role types; Committer is not a contributor here)
_TRAILER_ROLES = {CommitRole.CO_DEVELOPED_BY, CommitRole.SIGNED_OFF_BY, CommitRole.REVIEWED_BY, CommitRole.ACKED_BY,
                  CommitRole.TESTED_BY, CommitRole.REPORTED_BY, CommitRole.SUGGESTED_BY, CommitRole.MERGED_BY,
                  CommitRole.REQUESTED_BY}


# --------------------------------------------------------------------------- pure parts

def stored_person_key(name: str | None, email: str | None) -> tuple[str, str]:
    """`person_key` cut to the column widths, the form a person is looked up and inserted under."""
    n, e = person_key(name, email)
    return n[:NAME_MAX], e[:NAME_MAX]


def summarize_log(text: str) -> tuple[int, Counter]:
    """(commits, Counter of (stored person key, role) -> distinct commits) of a `git log` output; see
    summarize_records()."""
    out = summarize_records(text)
    return out["commits"], out["people"]


def summarize_records(text: str) -> dict[str, Any]:
    """`git log --format=%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%B` output (`_LOG_FORMAT`; the older
    `--no-merges --format=%x1e%an%x1f%ae%x1f%B` is read too) -> {commits, merges, people, shas}.

    A record with two or more parents is a merge: counted in `merges`, not in `commits` or `people`
    (the figures `git log --no-merges` gave). `shas`: every commit, merges included. Within one commit
    and role an identity (`identity_key`) counts once, and the author's own Signed-off-by is left out."""
    commits = merges = 0
    counts: Counter = Counter()
    shas: list[str] = []
    for chunk in text.split(_RECORD):
        head = chunk.split(_FIELD, 1)[0]
        if _FULL_SHA.match(head):
            parts = chunk.split(_FIELD, 4)
            if len(parts) < 5:
                continue
            sha, parents, name, email, body = parts
            shas.append(sha)
            if len(parents.split()) >= 2:
                merges += 1
                continue
        else:
            parts = chunk.split(_FIELD, 2)
            if len(parts) < 3:
                continue
            name, email, body = parts
        commits += 1
        author_identity = identity_key(name, email)
        counts[(stored_person_key(name, email), int(CommitRole.AUTHOR))] += 1
        seen: set[tuple[int, str]] = set()
        for prefix, t_name, t_email in iter_trailers(body):
            for role in (CommitRole.roles_from_trailer_prefix(prefix) if hasattr(CommitRole, "roles_from_trailer_prefix") else (CommitRole.from_trailer_prefix(prefix),)):
                if role not in _TRAILER_ROLES:
                    continue
                ident = identity_key(t_name, t_email)
                if role == CommitRole.SIGNED_OFF_BY and ident == author_identity:
                    continue
                if (int(role), ident) in seen:
                    continue
                seen.add((int(role), ident))
                counts[(stored_person_key(t_name, t_email), int(role))] += 1
    return {"commits": commits, "merges": merges, "people": counts, "shas": shas}


def parse_shortstat(text: str) -> tuple[int, int, int]:
    """` 7818 files changed, 496971 insertions(+), 383598 deletions(-)` -> (files, added, removed)."""
    def num(pattern: str) -> int:
        m = re.search(pattern, text or "")
        return int(m.group(1)) if m else 0
    return num(r"(\d+) files? changed"), num(r"(\d+) insertions?"), num(r"(\d+) deletions?")


def plan(tags: dict[str, str], stored: dict[str, tuple]) -> list[dict[str, Any]]:
    """The ranges to summarize: every release tag whose stored row (tag -> (tag_hash, base_tag,
    base_hash, diff_base_tag, format)) is missing or differs. Release order."""
    todo = []
    for r in release_ranges(tags):
        tag, base, diff_base = r["tag"], r["segment_base"], r["range_base"]
        want = (bytes.fromhex(tags[tag]), base or "", bytes.fromhex(tags[base]) if base else _NO_HASH, diff_base or "", FORMAT)
        if stored.get(tag) != want:
            todo.append({**r, "sha": tags[tag], "base_sha": tags[base] if base else None,
                         "diff_base_sha": tags[diff_base] if diff_base else None,
                         "chain": CHAIN_MAINLINE if r["kind"] == "mainline" else CHAIN_STABLE})
    return todo


# --------------------------------------------------------------------------- git

def _git(repo: Path, args: list[str], timeout: float = 900.0) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace",
                         timeout=timeout, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def release_tags(repo: Path) -> dict[str, str]:
    """Release tags (release_order.release_tag_names: the kernel scheme's, else every `<prefix>X.Y` tag) -> the
    commit they name (annotated tags peeled; tags of trees such as v2.6.11 are not releases)."""
    out = _git(repo, ["for-each-ref", "--format=%(refname:short)%09%(objectname)%09%(objecttype)%09%(*objectname)%09%(*objecttype)",
                      "refs/tags"], timeout=60)
    tags: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, obj, otype, peeled, ptype = parts[:5]
        sha, kind = (peeled, ptype) if peeled else (obj, otype)
        if kind == "commit" and parse_tag(name) is not None and _FULL_SHA.match(sha):
            tags[name] = sha
    return {name: tags[name] for name in release_tag_names(tags)}


def compute(repo: Path, rng: dict[str, Any]) -> dict[str, Any]:
    """Summarize one range from Git: {commits, merges, people, shas, files_changed, lines_added, lines_removed}."""
    span = [rng["sha"]] + ([f"^{rng['base_sha']}"] if rng["base_sha"] else [])
    out = summarize_records(_git(repo, ["log", _LOG_FORMAT, *span]))
    files = added = removed = 0
    if rng["diff_base_sha"]:
        files, added, removed = parse_shortstat(_git(repo, ["diff", "--shortstat", rng["diff_base_sha"], rng["sha"]]))
    return {**out, "files_changed": files, "lines_added": added, "lines_removed": removed}


# --------------------------------------------------------------------------- database

class _Persons:
    """(name, email) -> person_id over m_maintainer_person, inserting the ones not registered yet."""

    def __init__(self, db: Any) -> None:
        rows_raw = db.select_rows("m_maintainer_person", ["person_id", "name", "email"])
        rows = [(int(pid), _text(name), _text(email)) for pid, name, email in rows_raw]
        # A row stored in its normalized form wins over older unnormalized duplicates (main._ingest_commit_range)
        rows.sort(key=lambda r: ((r[1], r[2]) != stored_person_key(r[1], r[2]), r[0]))
        self.ids: dict[tuple[str, str], int] = {}
        for pid, name, email in rows:
            self.ids.setdefault(stored_person_key(name, email), pid)
        self.next_id = max([pid for pid, _, _ in rows] + [0]) + 1

    def resolve(self, db: Any, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], int]:
        new = []
        for key in keys:
            if key not in self.ids:
                self.ids[key] = self.next_id
                new.append((self.next_id, key[0], key[1]))
                self.next_id += 1
        if new:
            db.insert_rows("m_maintainer_person", ["person_id", "name", "email"], new)
        return self.ids


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value or "")


def stored_segments(db: Any, repo_id: int) -> dict[str, tuple]:
    """tag -> (segment_id, tag_hash, base_tag, base_hash, diff_base_tag, format, chain) of a repository."""
    rows = db.select_rows(
        "m_release_segment",
        ["segment_id", "tag", "tag_hash", "base_tag", "base_hash", "diff_base_tag", "format", "chain"],
        {"repo_id": repo_id},
    )
    return {_text(tag): (int(sid), bytes(th), _text(bt), bytes(bh), _text(dbt), int(fmt), int(chain))
            for sid, tag, th, bt, bh, dbt, fmt, chain in rows}


def _first_releases(db: Any, repo_id: int, tag_of: dict[int, str]) -> dict[int, tuple[tuple, int]]:
    """person_id -> (release_key of its first segment, segment_id)."""
    rows = db.select_rows("m_person_first_release", ["person_id", "segment_id"], {"repo_id": repo_id})
    return {int(pid): (release_key(tag_of[int(sid)]), int(sid)) for pid, sid in rows if int(sid) in tag_of}


def _rebuild_first_releases(db: Any, repo_id: int, mainline: dict[int, str]) -> dict[int, tuple[tuple, int]]:
    """Recompute m_person_first_release from the stored author rows (after a mainline segment was replaced)."""
    first: dict[int, tuple[tuple, int]] = {}
    rows = db.select_rows(
        ("m_release_person", "r"),
        ["r.segment_id", "r.person_id"],
        {"s.repo_id": repo_id, "s.chain": CHAIN_MAINLINE, "r.role_type": int(CommitRole.AUTHOR)},
        joins=[Join(("m_release_segment", "s"), {"s.segment_id": "r.segment_id"})],
    )
    for sid, pid in rows:
        key = (release_key(mainline[int(sid)]), int(sid))
        if int(pid) not in first or key < first[int(pid)]:
            first[int(pid)] = key
    db.delete_rows("m_person_first_release", {"repo_id": repo_id})
    out_rows = [(repo_id, pid, sid) for pid, (_k, sid) in first.items()]
    for i in range(0, len(out_rows), 5000):
        db.insert_rows("m_person_first_release", ["repo_id", "person_id", "segment_id"], out_rows[i:i + 5000])
    return first


def write_segment(db: Any, repo_id: int, rng: dict[str, Any], result: dict[str, Any], persons: _Persons,
                  old_id: int | None, first: dict[int, tuple[tuple, int]] | None) -> int:
    """Store one summarized range (replacing its previous row); returns its segment_id. `first` (the
    in-memory m_person_first_release) is updated for a mainline segment, None skips that."""
    if old_id is not None:
        for table in ("m_commit_release", "m_release_person", "m_person_first_release", "m_release_segment"):
            db.delete_rows(table, {"segment_id": old_id})
    db.insert_rows(
        "m_release_segment",
        ["repo_id", "tag", "tag_hash", "chain", "base_tag", "base_hash", "commits", "merges", "diff_base_tag",
         "files_changed", "lines_added", "lines_removed", "format"],
        [(repo_id, rng["tag"], bytes.fromhex(rng["sha"]), rng["chain"], rng["segment_base"] or "",
          bytes.fromhex(rng["base_sha"]) if rng["base_sha"] else _NO_HASH, result["commits"], result["merges"],
          rng["range_base"] or "", result["files_changed"], result["lines_added"], result["lines_removed"], FORMAT)],
    )
    seg_row = db.select_one("m_release_segment", ["segment_id"], {"repo_id": repo_id, "tag": rng["tag"]})
    segment_id = int(seg_row[0]) if seg_row else 0
    ids = persons.resolve(db, (key for key, _role in result["people"]))
    rows: dict[tuple[int, int], int] = Counter()
    for (key, role), n in result["people"].items():
        rows[(ids[key], role)] += n  # two raw spellings of one stored person add up
    db.insert_rows("m_release_person", ["segment_id", "person_id", "role_type", "commits"],
                   [(segment_id, pid, role, n) for (pid, role), n in rows.items()])
    # Every commit of the segment; a commit another segment of the chain already holds keeps it (segments
    # are written in release order, so the earliest wins, as in the webapp's release index)
    shas = [bytes.fromhex(sha) for sha in result.get("shas", ()) if _FULL_SHA.match(sha)]
    for i in range(0, len(shas), INSERT_CHUNK):
        chunk = shas[i:i + INSERT_CHUNK]
        db.insert_rows("m_commit_release", ["repo_id", "commit_hash", "chain", "segment_id"],
                       [(repo_id, sha, rng["chain"], segment_id) for sha in chunk], on_conflict="ignore")
    if first is not None and rng["chain"] == CHAIN_MAINLINE:
        key = release_key(rng["tag"])
        for (pid, role) in rows:
            if role != int(CommitRole.AUTHOR):
                continue
            have = first.get(pid)
            if have is None:
                db.insert_rows("m_person_first_release", ["repo_id", "person_id", "segment_id"],
                               [(repo_id, pid, segment_id)])
            elif key < have[0]:
                db.update_rows("m_person_first_release", {"segment_id": segment_id},
                               {"repo_id": repo_id, "person_id": pid})
            else:
                continue
            first[pid] = (key, segment_id)
    return segment_id


def ingest(db: Any, repo: Path, repo_id: int, progress: Callable[[int, int, str], None] | None = None,
           workers: int = WORKERS) -> dict[str, Any]:
    """Bring the release statistics of `repo` (m_repository `repo_id`) up to its tags. Returns a summary
    {state: "current" | "updated", tags, segments, persons_added, seconds}."""
    started = time.time()
    if db is None:
        return {"state": "current", "tags": 0, "segments": 0, "persons_added": 0, "seconds": 0.0}
    tags = release_tags(repo)
    stored = stored_segments(db, repo_id)
    todo = plan(tags, {t: row[1:6] for t, row in stored.items()})
    if not todo:
        return {"state": "current", "tags": len(tags), "segments": 0, "persons_added": 0, "seconds": 0.0}
    # Replacing a mainline segment can move a person's first release later: recompute those from scratch
    replaced_mainline = any(r["tag"] in stored and r["chain"] == CHAIN_MAINLINE for r in todo)
    persons = _Persons(db)
    first_persons = persons.next_id
    tag_of = {row[0]: t for t, row in stored.items() if row[6] == CHAIN_MAINLINE}
    first = None if replaced_mainline else _first_releases(db, repo_id, tag_of)
    logger.info(f"[Release stats] {len(todo)} of {len(tags)} release tag(s) to summarize"
                + (" (stored format changed: every tag is summarized again, ~9 minutes on the Linux clone)"
                   if any(stored.get(r["tag"]) and stored[r["tag"]][5] != FORMAT for r in todo) else ""))
    # The commits of every segment summarized again leave first: a replaced segment's commits may now belong
    # to an earlier segment of the run, which must not find them held by the old row
    old_ids = [stored[r["tag"]][0] for r in todo if r["tag"] in stored]
    if old_ids:
        with db.transaction():
            db.delete_rows("m_commit_release", {"segment_id": old_ids})
    done = 0
    last_log = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        # Computed in parallel, written in release order (pool.map keeps it): a person's first
        # release is decided by the earliest mainline segment, and an interrupted run resumes cleanly
        for rng, result in zip(todo, pool.map(lambda r: compute(repo, r), todo)):
            old = stored.get(rng["tag"])
            with db.transaction():
                write_segment(db, repo_id, rng, result, persons, old[0] if old else None, first)
            done += 1
            if progress:
                progress(done, len(todo), rng["tag"])
            if time.time() - last_log > 20:
                logger.info(f"[Release stats] {done}/{len(todo)} ({rng['tag']}: {result['commits']} commits)")
                last_log = time.time()
    if replaced_mainline:
        mainline = {row[0]: t for t, row in stored_segments(db, repo_id).items() if row[6] == CHAIN_MAINLINE}
        with db.transaction():
            _rebuild_first_releases(db, repo_id, mainline)
    return {"state": "updated", "tags": len(tags), "segments": done, "persons_added": persons.next_id - first_persons,
            "seconds": round(time.time() - started, 1)}
