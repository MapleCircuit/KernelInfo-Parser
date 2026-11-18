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
  one `git diff --shortstat`. Ranges are computed on a process pool (`process_workers()`; the record summaries are CPU-bound, so threads would be serialized by the GIL; `WORKERS` threads when no pool can start) and are written in release order, one
  transaction per segment, so an interrupted run resumes where it stopped.
- Incremental: a tag already summarized with the same commit, bases and `FORMAT` costs nothing; a moved
  tag, a changed base (a tag inserted before it) or an older format is summarized again.

Persons are the `m_maintainer_person` rows version ingestion uses (`identity.person_key`); persons
first seen here are inserted. Run only while no version ingestion is interrupted (main.py checks):
a rollback deletes persons above the interrupted run's watermark together with the rows naming them.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import multiprocessing
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from db_engine.spec import Join
from parser.git_ast.git_commit_parser import iter_trailers
from parser.git_ast.git_types import CommitRole
from parser.git_ast.identity import identity_key, person_key
from parser.git_ast.release_order import parse_tag, release_key, release_ranges, release_tag_names

logger = logging.getLogger(__name__)


# Bump when the stored rows change meaning: every segment is summarized again.
# 2: m_commit_release rows (every commit of the segment), one git log with merges.
FORMAT = 2
MW_FORMAT = 5
HEALTH_FORMAT = 3
EMPTY_PULL_IDX = 65535
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


def section_pattern_hash(sec: Any) -> bytes:
    items = sorted(
        f"{int(getattr(p, 'pat_type', 0))}:{str(getattr(p, 'pattern', '')).strip().lstrip('/')}"
        for p in getattr(sec, "patterns", ())
    )
    return hashlib.sha1("\n".join(items).encode("utf-8", "replace")).digest()


def sections_manifest_hash(sections: Iterable[Any], catchall_section: str = "THE REST") -> bytes:
    parts = sorted(
        f"{s.name}\x1f{getattr(s, 'status', '')}\x1f{section_pattern_hash(s).hex()}"
        for s in sections
        if getattr(s, "name", "") and s.name != catchall_section
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).digest()


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

    ID_BLOCK = 256

    def __init__(self, db: Any, distributed: bool = False) -> None:
        self.distributed = distributed
        self.added = 0
        self._cursor = 0
        self._limit = -1
        # Ids assigned by mint() and not inserted yet
        self._minted: dict[tuple[str, str], int] = {}
        rows_raw = db.select_rows("m_maintainer_person", ["person_id", "name", "email"])
        rows = [(int(pid), _text(name), _text(email)) for pid, name, email in rows_raw]
        # A row stored in its normalized form wins over older unnormalized duplicates (main._ingest_commit_range)
        rows.sort(key=lambda r: ((r[1], r[2]) != stored_person_key(r[1], r[2]), r[0]))
        self.ids: dict[tuple[str, str], int] = {}
        for pid, name, email in rows:
            self.ids.setdefault(stored_person_key(name, email), pid)
        self.next_id = max([pid for pid, _, _ in rows] + [0]) + 1

    def _mint(self, db: Any) -> int:
        """The next person id: max+1 when alone, else from a block leased with `reserve_id_block` (other nodes
        lease person id blocks concurrently)."""
        if not self.distributed:
            pid = self.next_id
            self.next_id += 1
            return pid
        if self._cursor > self._limit:
            from core.DBLayout import m_maintainer_person
            self._cursor, self._limit = db.reserve_id_block(m_maintainer_person, self.ID_BLOCK)
        pid = self._cursor
        self._cursor += 1
        return pid

    def mint(self, db: Any, keys: Iterable[tuple[str, str]]) -> None:
        """Assign ids to the keys not registered yet, for `resolve()` to insert. Called outside a transaction:
        leasing an id block (distributed mode) is refused inside one."""
        for key in keys:
            if key not in self.ids and key not in self._minted:
                self._minted[key] = self._mint(db)

    def resolve(self, db: Any, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], int]:
        new = []
        for key in keys:
            if key not in self.ids:
                pid = self._minted.pop(key, None)
                if pid is None:
                    pid = self._mint(db)
                self.ids[key] = pid
                new.append((pid, key[0], key[1]))
                self.added += 1
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
        with contextlib.suppress(Exception):
            db.delete_rows("m_cve_release", {"segment_id": old_id})
            db.delete_rows("m_cve_release_state", {"segment_id": old_id})
        with contextlib.suppress(Exception):
            db.delete_rows("m_release_pull", {"segment_id": old_id})
            db.delete_rows("m_release_section_health", {"segment_id": old_id})
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


def write_release_pulls(
    db: Any,
    segment_id: int,
    pulls: list[dict[str, Any]],
    fmt: int = MW_FORMAT,
) -> int:
    """Store pre-computed merge window pull requests for a release segment."""
    db.delete_rows("m_release_pull", {"segment_id": segment_id})
    cols = [
        "segment_id", "pull_idx", "merge_hash", "p1_hash", "p2_hash",
        "merge_date", "commits", "subject", "source_url", "ref_name",
        "sender", "sec_id", "sec_name", "dirs_json", "body", "format"
    ]
    if not pulls:
        sentinel_row = (
            segment_id, EMPTY_PULL_IDX, _NO_HASH, _NO_HASH, _NO_HASH,
            0, 0, "", "", "", "", 0, "", "[]", "", fmt
        )
        db.insert_rows("m_release_pull", cols, [sentinel_row])
        return 1

    rows = []
    for idx, p in enumerate(pulls):
        merge_val = p.get("merge") or p.get("merge_hash")
        merge_h = bytes.fromhex(merge_val) if isinstance(merge_val, str) and _FULL_SHA.match(merge_val) else (bytes(merge_val) if isinstance(merge_val, (bytes, bytearray)) else _NO_HASH)
        parents = p.get("parents") or []
        p1_val = parents[0] if len(parents) > 0 else p.get("p1_hash")
        p1_h = bytes.fromhex(p1_val) if isinstance(p1_val, str) and _FULL_SHA.match(p1_val) else (bytes(p1_val) if isinstance(p1_val, (bytes, bytearray)) else _NO_HASH)
        p2_val = parents[1] if len(parents) > 1 else p.get("p2_hash")
        p2_h = bytes.fromhex(p2_val) if isinstance(p2_val, str) and _FULL_SHA.match(p2_val) else (bytes(p2_val) if isinstance(p2_val, (bytes, bytearray)) else _NO_HASH)
        dirs_json = json.dumps({"dirs": p.get("dirs") or [], "parents": parents}) if len(parents) > 2 else json.dumps(p.get("dirs") or [])
        sec = p.get("section") or {}
        sec_id = int(p.get("sec_id") or sec.get("sec_id") or 0)
        sec_name = str(p.get("sec_name") or sec.get("name") or "")[:255]
        subject = str(p.get("subject") or "")[:255]
        source_url = str(p.get("source_url") or "")[:255]
        ref_name = str(p.get("ref_name") or p.get("ref") or "")[:255]
        sender = str(p.get("sender") or "")[:128]
        body = str(p.get("body") or "")
        merge_date = int(p.get("date") or p.get("merge_date") or 0)
        commits = int(p.get("commits") or 0)
        rows.append((
            segment_id, idx, merge_h, p1_h, p2_h, merge_date, commits,
            subject, source_url, ref_name, sender, sec_id, sec_name,
            dirs_json, body, fmt
        ))

    for i in range(0, len(rows), INSERT_CHUNK):
        db.insert_rows("m_release_pull", cols, rows[i:i + INSERT_CHUNK])
    return len(rows)


def write_release_section_health(
    db: Any,
    segment_id: int,
    sections: Iterable[Any],
    sec_stats: Mapping[str, dict[str, Any]],
    catchall_section: str = "THE REST",
    fmt: int = HEALTH_FORMAT,
) -> int:
    """Store pre-computed release subsystem health metrics."""
    db.delete_rows("m_release_section_health", {"segment_id": segment_id})
    cols = [
        "segment_id", "sec_name", "sec_id", "status", "commits",
        "reviewed_commits", "authors_json", "activity_json", "pat_hash", "format"
    ]
    manifest_hash = sections_manifest_hash(sections, catchall_section)
    sentinel_row = (
        segment_id, "", 0, "", 0, 0, "{}", "{}", manifest_hash, fmt
    )
    rows = [sentinel_row]
    seen_names: set[str] = {""}
    for sec in sections:
        name = getattr(sec, "name", "")
        if not name or name == catchall_section:
            continue
        sec_name = name[:255]
        if sec_name in seen_names:
            continue
        seen_names.add(sec_name)
        st = sec_stats.get(name) or {}
        sec_id = int(getattr(sec, "sec_id", 0) or 0)
        status = str(getattr(sec, "status", "") or "")[:64]
        commits = int(st.get("commits", 0))
        reviewed_commits = int(st.get("reviewed_commits", 0))
        authors_json = json.dumps(st.get("authors") or {})
        activity_json = json.dumps(st.get("activity") or {})
        pat_hash = section_pattern_hash(sec)
        rows.append((
            segment_id, sec_name, sec_id, status, commits,
            reviewed_commits, authors_json, activity_json, pat_hash, fmt
        ))

    for i in range(0, len(rows), INSERT_CHUNK):
        db.insert_rows("m_release_section_health", cols, rows[i:i + INSERT_CHUNK])
    return len(rows)


def _parent_committers(repo: Path, shas: list[str]) -> dict[str, tuple[str, str]]:
    """Batch-fetch `sha -> (committer_name, committer_email)` for second-parent tips of merges."""
    unique = [s for s in dict.fromkeys(shas) if _FULL_SHA.match(s)]
    out_map: dict[str, tuple[str, str]] = {}
    for i in range(0, len(unique), 500):
        chunk = unique[i:i + 500]
        try:
            out = _git(repo, ["log", "--no-walk=unsorted", f"--format=%H{_FIELD}%cn{_FIELD}%ce", *chunk], timeout=60.0)
        except RuntimeError:
            continue
        for line in out.splitlines():
            parts = line.split(_FIELD)
            if len(parts) >= 3 and _FULL_SHA.match(parts[0].strip()):
                out_map[parts[0].strip()] = (parts[1].strip(), parts[2].strip().lower())
    return out_map


class _PullSectionResolver:
    """Resolve a merge pull request's MAINTAINERS section via T: tree URL, changed files/dirs, and maintainer."""

    def __init__(self, sections: list[Any], matcher: Any, insights_cfg: dict[str, Any] | None = None) -> None:
        from parser.maintainer_ast.maintainer_types import MaintainerRole
        from webapp.backend.services.insights_service import (
            _SEC_STOPWORDS,
            _norm_person_name,
            _strip_paren_name,
            normalize_url,
            owner_of,
        )

        self.matcher = matcher
        self.insights_cfg = insights_cfg or {}
        catchall = str(self.insights_cfg.get("catchall_section", "THE REST") or "THE REST")
        self.catchall = catchall
        self.sections = [s for s in sections if getattr(s, "name", "") != catchall]
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
            for tree in re.split(r"[\n;]", getattr(sec, "scm_tree", "") or ""):
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
            for member in getattr(sec, "members", ()):
                weight = 2 if getattr(member, "role", None) == MaintainerRole.MAINTAINER else 1
                email = (getattr(member, "email", "") or "").strip().lower()
                if email:
                    prev = self.maint_email[email].get(sec.name, 0)
                    if weight > prev:
                        self.maint_email[email][sec.name] = weight
                m_name = getattr(member, "name", "")
                for candidate_name in (m_name, _strip_paren_name(m_name)):
                    norm_n = _norm_person_name(candidate_name)
                    if norm_n:
                        prev = self.maint_name[norm_n].get(sec.name, 0)
                        if weight > prev:
                            self.maint_name[norm_n][sec.name] = weight

    def tree_candidates(self, url: str) -> list[Any]:
        from webapp.backend.services.insights_service import normalize_url, owner_of
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
        from webapp.backend.services.insights_service import (
            _norm_person_name,
            _split_sender_identity,
            _strip_paren_name,
        )
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

    def _ref_tokens(self, ref: str, subject: str, url: str = "") -> set[str]:
        from webapp.backend.services.insights_service import _REF_STOPWORDS
        parts = [ref or subject or ""]
        generic_repos = set(self.insights_cfg.get("generic_repo_names", ["linux", "linux-2.6", "kernel"]))
        generic_refs = set(self.insights_cfg.get("generic_ref_words", ["linus", "torvalds", "linux", "next"]))
        if url:
            repo_seg = url.strip().rstrip("/").rsplit("/", 1)[-1]
            if repo_seg.lower().endswith(".git"):
                repo_seg = repo_seg[:-4]
            if repo_seg and repo_seg.lower() not in generic_repos:
                parts.append(repo_seg)
        out = set()
        for src in parts:
            for tok in re.split(r"[^a-z0-9]+", src.lower()):
                if len(tok) < 2 or tok in _REF_STOPWORDS or tok in generic_refs or tok.isdigit():
                    continue
                if re.fullmatch(r"v?\d+|rc\d*", tok):
                    continue
                out.add(tok)
        return out

    @staticmethod
    def _pattern_specificity(sec: Any, paths: list[str]) -> int:
        from parser.maintainer_ast.maintainer_types import PatternType

        best = 0
        for pat in getattr(sec, "patterns", ()):
            if getattr(pat, "pat_type", None) != PatternType.FILE:
                continue
            raw = str(getattr(pat, "pattern", "")).strip().lstrip("/")
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
        from webapp.backend.services.insights_service import _SIGNED_OFF_RE
        if not self.sections:
            return None
        tree_cands = self.tree_candidates(url)
        if len(tree_cands) == 1:
            s = tree_cands[0]
            return {"sec_id": getattr(s, "sec_id", 0), "name": s.name}

        lead_mergers = [str(m).strip().lower() for m in self.insights_cfg.get("lead_mergers", ["Linus Torvalds"]) if m]

        def is_lead(name: str) -> bool:
            nl = name.lower()
            return any(lm in nl for lm in lead_mergers)

        people: list[tuple[str, str]] = []
        if sender and not is_lead(sender):
            people.append((sender, sender_email))
        if not url and committer and not is_lead(committer):
            people.append((committer, committer_email))
        if not people:
            for sob_name, sob_email in _SIGNED_OFF_RE.findall(body or ""):
                if not is_lead(sob_name):
                    people.append((sob_name, sob_email))
        maint_w = self.maintainer_weights(people)

        file_counts: Counter = Counter()
        if self.matcher and changed_paths:
            for path in changed_paths:
                for s in self.matcher.match_file(path):
                    if getattr(s, "name", "") and s.name != self.catchall:
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
            return {"sec_id": getattr(s, "sec_id", 0), "name": s.name}

        if tree_cands:
            best = max(
                tree_cands,
                key=lambda s: (maint_w.get(s.name, 0), ref_overlap.get(s.name, 0)),
            )
            return {"sec_id": getattr(best, "sec_id", 0), "name": best.name}

        maint_only = [self.by_name[n] for n, w in maint_w.items() if w >= 2 and n in self.by_name]
        if len(maint_only) == 1:
            s = maint_only[0]
            return {"sec_id": getattr(s, "sec_id", 0), "name": s.name}
        if len(maint_only) > 1:
            scored = [(ref_overlap.get(s.name, 0), s) for s in maint_only]
            top_ov = max(ov for ov, _ in scored)
            if top_ov > 0:
                winners = [s for ov, s in scored if ov == top_ov]
                if len(winners) == 1:
                    return {"sec_id": getattr(winners[0], "sec_id", 0), "name": winners[0].name}
        return None


def load_tag_maintainers(
    db: Any,
    repo: Path,
    tag: str,
    sha: str,
    maintainers_file: str = "MAINTAINERS",
) -> tuple[list[Any], Any]:
    """Load MAINTAINERS sections and matcher for tag/commit, populates sec.sec_id if in DB."""
    from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
    from parser.maintainer_ast.maintainer_parser import MaintainerParser

    if not maintainers_file:
        return [], MaintainerMatcher([])

    sections: list[Any] = []
    with contextlib.suppress(Exception):
        rev = sha or tag
        text = _git(repo, ["show", f"{rev}:{maintainers_file}"], timeout=30.0)
        if text.strip():
            sections = MaintainerParser(text).parse()

    matcher = MaintainerMatcher(sections)

    if sections and db is not None:
        with contextlib.suppress(Exception):
            commit_id: int | None = None
            if hasattr(db, "table_exists") and db.table_exists("m_git_ref"):
                ref_row = db.select_one("m_git_ref", ["commit_id"], {"ref_name": tag})
                if ref_row and ref_row[0]:
                    commit_id = int(ref_row[0])
            if commit_id is None and hasattr(db, "table_exists") and db.table_exists("m_commit") and sha:
                c_row = db.select_one("m_commit", ["commit_id"], {"commit_hash": bytes.fromhex(sha)})
                if c_row and c_row[0]:
                    commit_id = int(c_row[0])

            if commit_id is not None:
                blob_id: int | None = None
                if hasattr(db, "checkpoint_tree_files"):
                    files = db.checkpoint_tree_files(commit_id, fnames=[maintainers_file])
                    for f in files:
                        fname = f["fname"] if isinstance(f, dict) else f[1]
                        if _text(fname) == maintainers_file:
                            blob_id = int(f["blob_id"] if isinstance(f, dict) else f[2])
                            break
                if blob_id is None and hasattr(db, "table_exists") and db.table_exists("m_bridge_checkpoint") and db.table_exists("m_file_name"):
                    fn_row = db.select_one("m_file_name", ["fnid"], {"file_name": maintainers_file})
                    if fn_row and fn_row[0]:
                        cp_row = db.select_one("m_bridge_checkpoint", ["blob_id"], {"commit_id": commit_id, "fnid": int(fn_row[0])})
                        if cp_row and cp_row[0]:
                            blob_id = int(cp_row[0])

                if blob_id is not None and hasattr(db, "table_exists") and db.table_exists("m_maintainer_section"):
                    from parser.maintainer_ast import maintainer_store
                    sec_map = {}
                    with contextlib.suppress(Exception):
                        sec_map = maintainer_store.blob_section_ids(db, blob_id)
                    if not sec_map:
                        sec_rows = db.select_rows("m_maintainer_section", ["sec_id", "name"], {"blob_id": blob_id})
                        sec_map = {_text(name).strip().lower(): int(sid) for sid, name in sec_rows if sid}
                    for sec in sections:
                        k = getattr(sec, "name", "").strip().lower()
                        if k in sec_map:
                            sec.sec_id = sec_map[k]

    return sections, matcher


def compute_tag_insights(
    repo: Path,
    tag: str,
    sha: str,
    prev_sha: str | None,
    sections: list[Any],
    matcher: Any,
    insights_cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Compute merge-window pulls and subsystem health metrics for a final mainline release."""
    from webapp.backend.services.insights_service import _fallback_sender, _sender, parse_pull

    span = [sha] + ([f"^{prev_sha}"] if prev_sha else [])
    cfg = insights_cfg or {}
    catchall_section = str(cfg.get("catchall_section", "THE REST") or "THE REST")

    # 1. Pulls
    pulls: list[dict[str, Any]] = []
    if cfg.get("merge_window", True):
        try:
            out_log = _git(repo, [
                "log", "--first-parent", "--merges",
                f"--format={_RECORD}%H{_FIELD}%P{_FIELD}%at{_FIELD}%cn{_FIELD}%ce{_FIELD}%s{_FIELD}%b",
                *span
            ], timeout=600.0)
        except Exception:
            out_log = ""

        merges: list[dict[str, Any]] = []
        for chunk in out_log.split(_RECORD):
            if not chunk.strip():
                continue
            parts = chunk.split(_FIELD, 6)
            if len(parts) < 7:
                continue
            m_sha = parts[0].strip()
            if not _FULL_SHA.match(m_sha):
                continue
            parents = parts[1].split()
            if len(parents) < 2:
                continue
            at_str = parts[2].strip()
            merge_date = int(at_str) if at_str.isdigit() else 0
            cn = parts[3].strip()
            ce = parts[4].strip().lower()
            subj = parts[5].strip()
            body = parts[6].rstrip("\r\n")
            merges.append({
                "sha": m_sha,
                "parents": parents,
                "date": merge_date,
                "committer": cn,
                "committer_email": ce,
                "subject": subj,
                "body": body,
            })

        if merges:
            # Step 2: Compute non-merge commit counts per pull
            commit_counts: dict[str, int] = {}
            try:
                rev_list_out = _git(repo, ["rev-list", "--parents", *span], timeout=600.0)
            except Exception:
                rev_list_out = ""

            parent_map: dict[str, list[str]] = {}
            is_merge: dict[str, bool] = {}
            for line in rev_list_out.splitlines():
                p_parts = line.split()
                if not p_parts:
                    continue
                c = p_parts[0]
                ps = p_parts[1:]
                parent_map[c] = ps
                is_merge[c] = len(ps) >= 2

            curr = sha
            reached_prev = False
            fp_chain: list[str] = [sha]
            while curr in parent_map:
                ps = parent_map[curr]
                if not ps:
                    break
                curr = ps[0]
                fp_chain.append(curr)
                if prev_sha and curr == prev_sha:
                    reached_prev = True
                    break

            if not prev_sha:
                reached_prev = True

            if not reached_prev:
                # Fallback to git rev-list --count --no-merges p2 ^p1
                for m in merges:
                    p1 = m["parents"][0]
                    p2 = m["parents"][1]
                    try:
                        cnt_str = _git(repo, ["rev-list", "--count", "--no-merges", p2, f"^{p1}"], timeout=60.0)
                        commit_counts[m["sha"]] = int(cnt_str.strip() or 0)
                    except Exception:
                        commit_counts[m["sha"]] = 0
            else:
                # O(V+E) incremental ancestor set walk from oldest merge to newest merge
                merges_oldest_first = list(reversed(merges))
                seen_ancestors: set[str] = set()
                if prev_sha:
                    seen_ancestors.add(prev_sha)

                trunk_commits = list(reversed(fp_chain))
                trunk_idx = 0

                for m in merges_oldest_first:
                    m_sha = m["sha"]
                    p1 = m["parents"][0]
                    while trunk_idx < len(trunk_commits):
                        tc = trunk_commits[trunk_idx]
                        trunk_idx += 1
                        seen_ancestors.add(tc)
                        if tc == p1:
                            break

                    side_visited: set[str] = set()
                    queue = [p for p in m["parents"][1:] if p in parent_map and p not in seen_ancestors]
                    for p in queue:
                        side_visited.add(p)
                    while queue:
                        node = queue.pop()
                        for p in parent_map.get(node, ()):
                            if p in parent_map and p not in seen_ancestors and p not in side_visited:
                                side_visited.add(p)
                                queue.append(p)

                    cnt = sum(1 for c in side_visited if not is_merge.get(c, False))
                    commit_counts[m_sha] = cnt
                    seen_ancestors.update(side_visited)
                    seen_ancestors.add(m_sha)

            # Step 3: Compute dirstats and changed files
            dirstats_map: dict[str, list[dict[str, Any]]] = {}
            with contextlib.suppress(Exception):
                dirstat_out = _git(repo, [
                    "log", "--first-parent", "--merges", "-m", "--dirstat=files,3,cumulative",
                    f"--format={_RECORD}%H", *span
                ], timeout=600.0)
                for chunk in dirstat_out.split(_RECORD):
                    if not chunk.strip():
                        continue
                    lines = chunk.strip().splitlines()
                    m_sha = lines[0].strip()
                    if not _FULL_SHA.match(m_sha) or m_sha in dirstats_map:
                        continue
                    dirs = []
                    for line in lines[1:9]:
                        pct, _, d = line.strip().partition("% ")
                        if d:
                            with contextlib.suppress(ValueError):
                                dirs.append({"dir": d.strip(), "percent": float(pct)})
                    dirstats_map[m_sha] = dirs

            changed_files_map: dict[str, list[str]] = {}
            if sections:
                with contextlib.suppress(Exception):
                    cf_out = _git(repo, [
                        "log", "--first-parent", "--merges", "-m", "--no-renames",
                        f"--format={_RECORD}%H", "--name-only", *span
                    ], timeout=600.0)
                    for chunk in cf_out.split(_RECORD):
                        if not chunk.strip():
                            continue
                        lines = chunk.strip().splitlines()
                        m_sha = lines[0].strip()
                        if not _FULL_SHA.match(m_sha) or m_sha in changed_files_map:
                            continue
                        changed_files_map[m_sha] = [f.strip() for f in lines[1:] if f.strip()]

            # Step 4: Resolve sender and section
            need_p2 = []
            parsed_merges = []
            for m in merges:
                pull = parse_pull(m["subject"])
                body_sender = _sender(m["body"])
                parsed_merges.append((m, pull, body_sender))
                if not body_sender:
                    need_p2.append(m["parents"][1])

            p2_committers = _parent_committers(repo, need_p2) if need_p2 else {}
            resolver = _PullSectionResolver(sections, matcher, insights_cfg=cfg)

            for m, pull, body_sender in parsed_merges:
                p2 = m["parents"][1]
                p2_name, p2_email = p2_committers.get(p2, ("", ""))
                tree_cands = resolver.tree_candidates(pull["url"]) if pull["url"] else []
                sender, sender_email = _fallback_sender(
                    body_sender,
                    p2_name=p2_name,
                    p2_email=p2_email,
                    committer=m["committer"],
                    url=pull["url"],
                    body=m["body"],
                    tree_cands=tree_cands,
                )

                sec = None
                if len(tree_cands) == 1:
                    s_cand = tree_cands[0]
                    sec = {"sec_id": getattr(s_cand, "sec_id", 0), "name": s_cand.name}
                else:
                    paths = changed_files_map.get(m["sha"], []) if sections else []
                    dirs = dirstats_map.get(m["sha"], [])
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

                pulls.append({
                    "merge": m["sha"],
                    "parents": m["parents"],
                    "date": m["date"],
                    "subject": m["subject"],
                    "source_url": pull.get("url", ""),
                    "ref_name": pull.get("ref", ""),
                    "sender": sender,
                    "sec_id": sec.get("sec_id", 0) if sec else 0,
                    "sec_name": sec.get("name", "") if sec else "",
                    "dirs": dirstats_map.get(m["sha"], []),
                    "body": m["body"],
                    "commits": commit_counts.get(m["sha"], 0),
                })

    # 2. Sec stats
    sec_stats: dict[str, dict[str, Any]] = {}
    if sections and matcher:
        try:
            log_text = _git(repo, [
                "log", "--no-merges", "--no-renames",
                f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%ae{_FIELD}%at{_FIELD}%B{_FIELD}",
                "--name-only", *span
            ], timeout=600.0)
        except Exception:
            log_text = ""

        file_sec_cache: dict[str, list[str]] = {}

        for chunk in log_text.split(_RECORD):
            if not chunk.strip():
                continue
            parts = chunk.split(_FIELD, 5)
            if len(parts) < 6:
                continue
            c_sha = parts[0].strip()
            if not _FULL_SHA.match(c_sha):
                continue
            an = parts[1]
            ae = parts[2]
            at_str = parts[3].strip()
            author_date = int(at_str) if at_str.isdigit() else 0
            body = parts[4]
            files_text = parts[5]

            files = [f.strip() for f in files_text.splitlines() if f.strip()]
            matched_secs: set[str] = set()
            for f in files:
                if f not in file_sec_cache:
                    file_sec_cache[f] = [
                        s.name for s in matcher.match_file(f)
                        if getattr(s, "name", "") and s.name != catchall_section
                    ]
                matched_secs.update(file_sec_cache[f])

            if not matched_secs:
                continue

            author_ident = identity_key(an, ae)
            reviewed_people: set[str] = set()
            involved_people: set[str] = {author_ident}

            for prefix, t_name, t_email in iter_trailers(body):
                t_ident = identity_key(t_name, t_email)
                roles = CommitRole.roles_from_trailer_prefix(prefix) if hasattr(CommitRole, "roles_from_trailer_prefix") else (CommitRole.from_trailer_prefix(prefix),)
                for role in roles:
                    if role in (CommitRole.REVIEWED_BY, CommitRole.ACKED_BY):
                        if t_ident != author_ident:
                            reviewed_people.add(t_ident)
                        involved_people.add(t_ident)
                    elif role in (CommitRole.SIGNED_OFF_BY, CommitRole.TESTED_BY):
                        involved_people.add(t_ident)

            is_reviewed = bool(reviewed_people)

            for sname in matched_secs:
                st = sec_stats.setdefault(sname, {
                    "commits": 0,
                    "reviewed_commits": 0,
                    "authors": {},
                    "activity": {}
                })
                st["commits"] += 1
                if is_reviewed:
                    st["reviewed_commits"] += 1
                st["authors"][author_ident] = st["authors"].get(author_ident, 0) + 1
                for ident in involved_people:
                    if author_date > st["activity"].get(ident, 0):
                        st["activity"][ident] = author_date

    return pulls, sec_stats


def ingest_insights(
    db: Any,
    repo: Path,
    repo_id: int,
    *,
    from_tag: str | None = None,
    insights_cfg: dict[str, Any] | None = None,
    repo_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ingest pre-computed merge window pulls and subsystem health metrics for release segments."""
    started = time.time()
    if db is None:
        return {"state": "unavailable", "segments": 0}
    if hasattr(db, "table_exists"):
        if not (db.table_exists("m_release_pull") and db.table_exists("m_release_section_health")):
            return {"state": "unavailable", "segments": 0}
    else:
        try:
            db.select_one("m_release_pull", ["segment_id"], limit=1)
            db.select_one("m_release_section_health", ["segment_id"], limit=1)
        except Exception:
            return {"state": "unavailable", "segments": 0}

    if repo_entry is None:
        from core.config import get_repository
        with contextlib.suppress(Exception):
            repo_entry = get_repository(str(repo))

    if insights_cfg is None:
        from core.config import resolve_repo_insights
        insights_cfg = resolve_repo_insights(repo_entry)

    rows = db.select_rows(
        "m_release_segment",
        ["segment_id", "tag", "tag_hash", "base_tag", "base_hash", "diff_base_tag"],
        {"repo_id": repo_id},
    )
    if not rows:
        return {"state": "current", "segments": 0}

    tag_to_sha: dict[str, str] = {
        _text(tag): bytes(th).hex()
        for _, tag, th, _, _, _ in rows
        if th and th != _NO_HASH
    }

    from core.config import resolve_preparse_tags
    all_tags = [_text(r[1]) for r in rows]
    selected_tags = set(resolve_preparse_tags(repo_entry, "insights", all_tags))

    target_segments = []
    for sid, tag, th, bt, bh, dbt in rows:
        tag_str = _text(tag)
        if tag_str in selected_tags:
            sha_hex = bytes(th).hex() if th and th != _NO_HASH else ""
            dbt_str = _text(dbt)
            prev_sha = tag_to_sha.get(dbt_str)
            target_segments.append({
                "segment_id": int(sid),
                "tag": tag_str,
                "sha": sha_hex,
                "diff_base_tag": dbt_str,
                "prev_sha": prev_sha,
            })

    target_segments.sort(key=lambda s: release_key(s["tag"]))

    if from_tag:
        from_key = release_key(from_tag)
        target_segments = [s for s in target_segments if release_key(s["tag"]) >= from_key]

    if not target_segments:
        return {"state": "current", "segments": 0}

    target_sids = [s["segment_id"] for s in target_segments]

    current_pull_sids = {
        int(sid) for sid in db.select_values(
            "m_release_pull", "segment_id",
            {"segment_id": target_sids, "format": MW_FORMAT},
            distinct=True,
        )
    }
    current_health_sids = {
        int(sid) for sid in db.select_values(
            "m_release_section_health", "segment_id",
            {"segment_id": target_sids, "format": HEALTH_FORMAT},
            distinct=True,
        )
    }

    missing_segments = [
        s for s in target_segments
        if s["segment_id"] not in current_pull_sids or s["segment_id"] not in current_health_sids
    ]

    if not missing_segments:
        return {"state": "current", "segments": 0}

    maintainers_file = str(insights_cfg.get("maintainers_file", "MAINTAINERS") or "")
    catchall_section = str(insights_cfg.get("catchall_section", "THE REST") or "THE REST")

    count = 0
    for seg in missing_segments:
        sha = seg["sha"]
        if not sha:
            continue
        prev_sha = seg["prev_sha"]
        if not prev_sha and seg["diff_base_tag"]:
            with contextlib.suppress(Exception):
                prev_sha = _git(repo, ["rev-parse", f"{seg['diff_base_tag']}^{{commit}}"], timeout=10.0).strip()

        sections, matcher = load_tag_maintainers(
            db, repo, seg["tag"], sha, maintainers_file=maintainers_file
        )
        pulls, sec_stats = compute_tag_insights(
            repo, seg["tag"], sha, prev_sha, sections, matcher, insights_cfg=insights_cfg
        )
        with db.transaction():
            write_release_pulls(db, seg["segment_id"], pulls, fmt=MW_FORMAT)
            write_release_section_health(
                db, seg["segment_id"], sections, sec_stats,
                catchall_section=catchall_section, fmt=HEALTH_FORMAT
            )
        count += 1

    return {
        "state": "updated",
        "segments": count,
        "seconds": round(time.time() - started, 1),
    }


def process_workers() -> int:
    """Processes of the summarizing pool: min(cpu, 8) - 1, at least 1."""
    return max(1, min(os.cpu_count() or 4, 8) - 1)


def _compute_args(args: tuple[Path, dict[str, Any]]) -> dict[str, Any]:
    """Module-level (picklable) pool entry: touches neither the database nor global state."""
    return compute(*args)


def compute_all(repo: Path, todo: list[dict[str, Any]], workers: int = WORKERS,
                processes: int | None = None) -> Iterator[dict[str, Any]]:
    """The `compute()` results of the ranges, in order, from a process pool (`processes`, default
    `process_workers()`); `workers` threads when the pool cannot start or breaks (and for a single range)."""
    processes = process_workers() if processes is None else processes
    done = 0
    if processes >= 1 and len(todo) > 1:
        try:
            with ProcessPoolExecutor(max_workers=min(processes, len(todo)), mp_context=multiprocessing.get_context("fork")) as pool:
                for result in pool.map(_compute_args, [(repo, r) for r in todo]):
                    done += 1
                    yield result
            return
        except (BrokenProcessPool, OSError, NotImplementedError, ImportError) as exc:
            logger.warning(f"[Release stats] Process pool unavailable ({exc}); using threads.")
    rest = todo[done:]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        yield from pool.map(lambda r: compute(repo, r), rest)


def ingest(db: Any, repo: Path, repo_id: int, progress: Callable[[int, int, str], None] | None = None,
           workers: int = WORKERS, distributed: bool = False) -> dict[str, Any]:
    """Bring the release statistics of `repo` (m_repository `repo_id`) up to its tags. Returns a summary
    {state: "current" | "updated", tags, segments, persons_added, seconds}."""
    started = time.time()
    if db is None:
        return {"state": "current", "tags": 0, "segments": 0, "persons_added": 0, "seconds": 0.0}
    tags = release_tags(repo)
    stored = stored_segments(db, repo_id)
    todo = plan(tags, {t: row[1:6] for t, row in stored.items()})
    if not todo:
        if hasattr(db, "table_exists") and db.table_exists("m_release_pull"):
            try:
                ingest_insights(db, repo, repo_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Release stats] Insights pre-parse failed: %s", exc, exc_info=True)
        return {"state": "current", "tags": len(tags), "segments": 0, "persons_added": 0, "seconds": 0.0}
    # Replacing a mainline segment can move a person's first release later: recompute those from scratch
    replaced_mainline = any(r["tag"] in stored and r["chain"] == CHAIN_MAINLINE for r in todo)
    persons = _Persons(db, distributed)
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
    # Computed in parallel, written in release order: a person's first release is decided by the earliest
    # mainline segment, and an interrupted run resumes cleanly
    for rng, result in zip(todo, compute_all(repo, todo, workers)):
        old = stored.get(rng["tag"])
        persons.mint(db, (key for key, _role in result["people"]))
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
    if hasattr(db, "table_exists") and db.table_exists("m_release_pull"):
        try:
            ingest_insights(db, repo, repo_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Release stats] Insights pre-parse failed: %s", exc, exc_info=True)
    return {"state": "updated", "tags": len(tags), "segments": done, "persons_added": persons.added,
            "seconds": round(time.time() - started, 1)}
