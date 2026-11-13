"""webapp/backend/services/commit_relations.py - Commit relations (webapp/API.md section 2.16).

"Which commits refer to this commit, and how?" (Fixes: / fixed by, reverts / reverted by, upstream /
backports, Cc: stable, mail links), answered:

1. **From the database** once the parser covers it: `m_commit_relation` and `m_commit_link` (core/API.md
   section 4, rows 63-64), written for every commit the parser inserts and, for older ones, by
   `--backfill-commit-relations` (its state row says "done:"). A commit of the active network's
   `m_commit` is answered there (`source: "db"`); reverse lists only hold commits the parser ingested.
2. **From the SQLite relation index**, for commits the database does not hold (refs never ingested) or
   while the backfill has not finished: two `git log --all` passes (trailers only for Fixes/Cc/Link,
   bodies for reverts and backports; about a minute each on a full Linux clone with its stable branches)
   fill a SQLite file under the disk cache (section 2.14) in a background job (section 2.15); later
   refreshes only read the commits of the new ref heads. It is built only when such a lookup misses
   the database, never at startup.

The message parsers live in parser/git_ast/commit_relations.py (re-exported here).
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Iterator

from fastapi import HTTPException

from parser.git_ast.commit_relations import (  # noqa: F401 - re-exported
    BACKFILL_DONE,
    FIXES_RE,
    KIND_FIXES,
    KIND_REVERTS,
    KIND_STABLE_CC,
    KIND_UPSTREAM,
    LINK_RE,
    MESSAGE_ID_RE,
    REVERT_RE,
    STABLE_CC_RE,
    SUBJECT_MAX,
    UPSTREAM_RES,
    ZERO_HASH,
    normalize_mid,
    parse_relations,
)
from cache_engine import BYTES, INT, TEXT, CacheSpace, Eq, In, StoreSpec, get_cache_engine
from webapp.backend.config import RepoScopedCache, get_active_repo_name, get_repo_dir
from webapp.backend.database.helpers import get_active_network_id, get_active_repo_id
from db_engine.spec import Agg, Join, Value
from webapp.backend.database.pool import get_reader
from webapp.backend.services.jobs import jobs
from webapp.backend.services.sync_service import register_refs_changed

logger = logging.getLogger(__name__)

INDEX_FORMAT = 2  # 2: abbreviated revert/upstream targets kept as prefixes (B12)
BATCH = 5000
LIST_CAP = 100
DB_CHUNK = 500
DB_READY_TTL = 60.0  # a "not ready" answer is checked again after this
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX = re.compile(r"^[0-9a-f]{8,40}$")
_LISTS = ("fixes", "fixed_by", "reverts", "reverted_by", "upstream", "backports")


def iter_records(stream: Iterable[str]) -> Iterator[list[str]]:
    """Records of `git log --format=%x1e<fields separated by %x1f>` read in chunks."""
    buf = ""
    for chunk in stream:
        buf += chunk
        parts = buf.split("\x1e")
        buf = parts.pop()
        for rec in parts:
            if rec.strip():
                yield rec.split("\x1f")
    if buf.strip():
        yield buf.split("\x1f")


# ----------------------------------------------------------------------------------- store

NAMESPACE = "commit-relations"
STORES = (
    StoreSpec("commits", (BYTES("sha"), INT("at", null=True), TEXT("subject", null=True)), key=("sha",), clustered=True),
    StoreSpec("rel", (BYTES("src"), INT("kind"), BYTES("dst", null=True), TEXT("prefix", null=True),
                      TEXT("p8", null=True), TEXT("note", null=True)),
              unique=(("src", "kind", "dst", "prefix", "note"),), indexes=(("src",), ("dst",), ("p8",))),
    StoreSpec("links", (TEXT("mid"), BYTES("sha")), unique=(("mid", "sha"),), indexes=(("sha",),)),
)

PASS_TRAILERS = [
    "log", "--all", "--no-color", "-E", "-i",
    "--format=%x1e%H%x1f%at%x1f%s%x1f%(trailers:key=Fixes,key=Cc,key=Link,key=Message-Id,unfold,separator=%x0a)",
    "--grep=^Fixes:", "--grep=^Cc:.*stable@", "--grep=^Link:", "--grep=^Message-Id:",
]
PASS_BODIES = [
    "log", "--all", "--no-color", "-E", "-i",
    "--format=%x1e%H%x1f%at%x1f%s%x1f%b",
    "--grep=^This reverts commit", "--grep=upstream commit", "--grep=^commit [0-9a-f]{12,40} upstream",
    "--grep=cherry picked from commit",
]


def _target_row(src: bytes, kind: int, target: str) -> tuple:
    if len(target) == 40:
        return (src, kind, bytes.fromhex(target), None, None, None)
    return (src, kind, None, target, target[:8], None)


def git_subjects(keys: Iterable[bytes]) -> dict[bytes, tuple[int, str]]:
    """{sha bytes: (author time, subject)} read from Git (at most 200 commits per call)."""
    out: dict[bytes, tuple[int, str]] = {}
    missing = [k for k in dict.fromkeys(keys) if k][:200]
    if missing:
        text = _git(["log", "--no-walk=unsorted", "--format=%H%x1f%at%x1f%s", *(k.hex() for k in missing)], timeout=30) or ""
        for line in text.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3 and _FULL_SHA.match(parts[0]):
                out[bytes.fromhex(parts[0])] = (int(parts[1]) if parts[1].isdigit() else 0, parts[2][:SUBJECT_MAX])
    return out


class RelationIndex:
    """Cache store (cache_engine) of the relations of one repository (space keyed by repository and INDEX_FORMAT)."""

    def __init__(self) -> None:
        self._resolved: RepoScopedCache = RepoScopedCache(maxsize=20_000)

    @staticmethod
    def space_name() -> str:
        # Versioned by INDEX_FORMAT (bump it when the stored rows change meaning), not by the module's
        # source: an unrelated edit must not throw away an index that took minutes to build
        return f"{get_active_repo_name()}-v{INDEX_FORMAT}"

    def space(self) -> CacheSpace:
        """The relation space of the active repository (stores `commits`, `rel`, `links`; opened on demand)."""
        return get_cache_engine().open(NAMESPACE, self.space_name(), STORES)

    def built(self) -> bool:
        """The cache holds a finished build (checked without creating it)."""
        try:
            return get_cache_engine().exists(NAMESPACE, self.space_name()) and bool(self.meta("built_at"))
        except Exception:  # noqa: BLE001
            return False

    def meta(self, key: str) -> str | None:
        return self.space().get_meta(key)

    def covered_heads(self) -> set[str]:
        raw = self.meta("heads") or ""
        return {h for h in raw.split() if _FULL_SHA.match(h)}

    @staticmethod
    def current_heads() -> set[str]:
        out = _git(["for-each-ref", "--format=%(objectname) %(objecttype) %(*objectname)"], timeout=30) or ""
        heads = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and _FULL_SHA.match(parts[2]):
                heads.add(parts[2])  # an annotated tag: the object it tags
            elif len(parts) >= 2 and parts[1] == "commit" and _FULL_SHA.match(parts[0]):
                heads.add(parts[0])
        return heads

    def is_current(self) -> bool:
        heads = self.covered_heads()
        return bool(heads) and self.current_heads() <= heads

    # ------------------------------------------------------------------ build
    def _write(self, space: CacheSpace, commits: list, rels: list, links: list) -> None:
        with space.batch():
            space.store("commits").put_many(commits, replace=False)
            space.store("rel").put_many(rels, replace=False)
            space.store("links").put_many(links, replace=False)

    def _run_pass(self, args: list[str], revs: list[str] | None, space: CacheSpace,
                  counter: list[int], cancelled: threading.Event | None) -> None:
        """Stream one `git log` pass into the store. `revs`: revisions for --stdin (incremental), else --all."""
        cmd = ["git", "-C", str(get_repo_dir()), *args]
        if revs is not None:
            cmd = [c for c in cmd if c != "--all"] + ["--stdin"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if revs is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
                                bufsize=1 << 20)
        if revs is not None:
            def feed() -> None:
                try:
                    proc.stdin.write("\n".join(revs) + "\n")
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
            threading.Thread(target=feed, daemon=True).start()
        commits: list = []
        rels: list = []
        links: list = []
        try:
            for rec in iter_records(iter(lambda: proc.stdout.read(1 << 20), "")):
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError("cancelled")
                if len(rec) < 4:
                    continue
                sha = rec[0].strip().lower()
                if not _FULL_SHA.match(sha):
                    continue
                src = bytes.fromhex(sha)
                at = int(rec[1]) if rec[1].strip().isdigit() else 0
                commits.append((src, at, rec[2][:SUBJECT_MAX]))
                found = parse_relations("\x1f".join(rec[3:]))
                for prefix, subject in found["fixes"]:
                    rels.append((src, KIND_FIXES, bytes.fromhex(prefix) if len(prefix) == 40 else None, prefix, prefix[:8], subject))
                # Reverts and backports may name an abbreviated hash (odd lengths too): kept as a prefix (B12)
                for target in found["reverts"]:
                    rels.append(_target_row(src, KIND_REVERTS, target))
                for target in found["upstream"]:
                    if not sha.startswith(target):
                        rels.append(_target_row(src, KIND_UPSTREAM, target))
                for note in found["stable_cc"]:
                    rels.append((src, KIND_STABLE_CC, None, None, None, note))
                for mid in found["links"]:
                    links.append((mid, src))
                counter[0] += 1
                if len(commits) >= BATCH:
                    self._write(space, commits, rels, links)
                    commits, rels, links = [], [], []
            if commits:
                self._write(space, commits, rels, links)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout:
                proc.stdout.close()

    def build(self, progress: Callable[..., None] | None = None) -> dict[str, Any]:
        """Full pass on first use; afterwards only the commits of new ref heads."""
        started = time.time()
        space = self.space()
        heads = self.current_heads()
        old = self.covered_heads()
        incremental = bool(old)
        revs = None
        if incremental:
            new = heads - old
            if not new:
                return {"incremental": True, "commits_scanned": 0, "elapsed_s": 0.0}
            revs = sorted(new) + [f"^{h}" for h in sorted(old)]
        counters = [[0], [0]]
        cancelled = getattr(progress, "cancelled", None)
        errors: list[BaseException] = []

        def run(args: list[str], slot: int) -> None:
            try:
                self._run_pass(args, revs, space, counters[slot], cancelled)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(PASS_TRAILERS, 0), daemon=True),
                   threading.Thread(target=run, args=(PASS_BODIES, 1), daemon=True)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
            if progress:
                progress(None, None, f"{'new heads' if incremental else 'full pass'}: {counters[0][0] + counters[1][0]:,} commits read")
        if errors:
            raise RuntimeError(f"relation pass failed: {errors[0]}")
        with space.batch():
            space.set_meta("heads", " ".join(sorted(heads | old)))
            space.set_meta("built_at", str(time.time()))
        return {"incremental": incremental, "commits_scanned": counters[0][0] + counters[1][0],
                "elapsed_s": round(time.time() - started, 1)}

    # ------------------------------------------------------------------ queries
    def subjects(self, shas: Iterable[bytes]) -> dict[bytes, tuple[int, str]]:
        keys = list({s for s in shas if s})
        out: dict[bytes, tuple[int, str]] = {}
        for sha, (_, at, subject) in self.space().store("commits").get_many(keys).items():
            out[sha] = (at, subject)
        missing = [k for k in keys if k not in out]
        if missing:
            # Commits outside both passes (no Fixes/Cc/Link/backport text): read their subjects from Git
            out.update(git_subjects(missing))
        return out

    def resolve_prefix(self, prefix: str) -> str | None:
        """Full SHA of an abbreviated commit id (a Fixes: target); None when unknown or ambiguous."""
        prefix = prefix.lower()
        if not _HEX.match(prefix):
            return None
        if len(prefix) == 40:
            return prefix
        hit = self._resolved.get(prefix)
        if hit is not None:
            return hit or None
        # The ^{commit} suffix is appended here, to a validated hex string (section 2.1.1)
        out = (_git(["rev-parse", "--verify", "--quiet", f"{prefix}^{{commit}}"], timeout=10) or "").strip()
        sha = out if _FULL_SHA.match(out) else ""
        self._resolved[prefix] = sha
        return sha or None

    def relations(self, sha: str) -> dict[str, Any]:
        space = self.space()
        rel, links = space.store("rel"), space.store("links")
        key = bytes.fromhex(sha)
        out: dict[str, Any] = {"sha": sha, "fixes": [], "fixed_by": [], "reverts": [], "reverted_by": [],
                               "upstream": [], "backports": [], "stable_cc": [], "links": []}
        related: list[bytes] = []
        for kind, dst, prefix, note in rel.scan(Eq("src", key), columns=("kind", "dst", "prefix", "note")):
            if kind == KIND_FIXES:
                full = dst.hex() if dst else self.resolve_prefix(prefix or "")
                out["fixes"].append({"sha": full, "prefix": prefix, "subject": note or "", "resolved": bool(full)})
                if full:
                    related.append(bytes.fromhex(full))
            elif kind in (KIND_REVERTS, KIND_UPSTREAM):
                full = dst.hex() if dst else self.resolve_prefix(prefix or "")
                if full:
                    out["reverts" if kind == KIND_REVERTS else "upstream"].append({"sha": full})
                    related.append(bytes.fromhex(full))
            elif kind == KIND_STABLE_CC:
                out["stable_cc"].append(note or "")
        prefixes = set(sha[:k] for k in range(8, 41))
        by_prefix = [(src, kind) for src, kind, prefix in rel.scan(
            Eq("p8", sha[:8]), columns=("src", "kind", "prefix")) if prefix in prefixes]
        fixed_by = [src for src, kind in by_prefix if kind == KIND_FIXES]
        incoming = list(rel.scan([Eq("dst", key), In("kind", (KIND_REVERTS, KIND_UPSTREAM))], columns=("src", "kind")))
        incoming += [(src, kind) for src, kind in by_prefix if kind in (KIND_REVERTS, KIND_UPSTREAM)]
        lists = {"fixed_by": fixed_by,
                 "reverted_by": [src for src, kind in incoming if kind == KIND_REVERTS],
                 "backports": [src for src, kind in incoming if kind == KIND_UPSTREAM]}
        for field, srcs in lists.items():
            srcs = list(dict.fromkeys(srcs))
            if len(srcs) > LIST_CAP:
                # Very old commits collect thousands (Fixes: 1da177e4c3f4): keep the most recent ones
                dates = self.subjects(srcs)
                srcs.sort(key=lambda b: dates.get(b, (0, ""))[0], reverse=True)
                out[f"{field}_total"] = len(srcs)
                srcs = srcs[:LIST_CAP]
            for src in srcs:
                out[field].append({"sha": src.hex()})
                related.append(src)
        out["links"] = [m for (m,) in links.scan(Eq("sha", key), columns=("mid",))]
        subjects = self.subjects(related)
        for field in ("fixes", "fixed_by", "reverts", "reverted_by", "upstream", "backports"):
            for item in out[field]:
                if not item.get("sha"):
                    continue
                at, subject = subjects.get(bytes.fromhex(item["sha"]), (0, ""))
                item["date"] = at
                if subject and not item.get("subject"):
                    item["subject"] = subject
                item.setdefault("subject", "")
            out[field].sort(key=lambda it: it.get("date") or 0)
        own = subjects.get(key) or self.subjects([key]).get(key)
        if own:
            out["date"], out["subject"] = own
        return out

    def fixes_of(self, sha: str) -> list[str]:
        """Full SHAs of the commits a commit names in its `Fixes:` tags (abbreviations resolved)."""
        out: list[str] = []
        rows = self.space().store("rel").scan([Eq("src", bytes.fromhex(sha)), Eq("kind", KIND_FIXES)], columns=("dst", "prefix"))
        for dst, prefix in list(rows):
            full = dst.hex() if dst else self.resolve_prefix(prefix or "")
            if full and full not in out:
                out.append(full)
        return out

    def backports_of(self, sha: str) -> list[str]:
        """Commits saying they are a backport of `sha` (`commit <sha> upstream.`, cherry picks)."""
        rel = self.space().store("rel")
        rows = list(rel.scan([Eq("dst", bytes.fromhex(sha)), Eq("kind", KIND_UPSTREAM)], columns=("src",)))
        prefixes = set(sha[:k] for k in range(8, 40))
        rows += [(src,) for src, prefix in rel.scan([Eq("p8", sha[:8]), Eq("kind", KIND_UPSTREAM)], columns=("src", "prefix"))
                 if prefix in prefixes]
        return list(dict.fromkeys(src.hex() for (src,) in rows))

    def commits_for_mid(self, mid: str) -> list[str]:
        rows = self.space().store("links").scan(Eq("mid", normalize_mid(mid)), columns=("sha",))
        return [r[0].hex() for r in rows]

    def counts(self) -> dict[str, int]:
        space = self.space()
        return {"commits": space.store("commits").count(), "relations": space.store("rel").count(),
                "links": space.store("links").count()}


def _git(args: list[str], timeout: float = 60.0) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(get_repo_dir()), *args], capture_output=True, text=True,
                             timeout=timeout, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


relation_index = RelationIndex()
_STALE: dict[str, bool] = {}


def _job_key() -> str:
    return f"commit-relations:{get_active_repo_name()}"


def ensure_index(wait: float = 0.0) -> dict[str, Any]:
    """Submit the (incremental) build when ref heads moved since the last one; returns the job state."""
    repo = get_active_repo_name()
    covered = relation_index.covered_heads()
    if covered and not _STALE.get(repo, True):
        return {"state": "done", "progress": {"done": 1, "total": 1, "note": "current"}}
    if covered and relation_index.is_current():
        _STALE[repo] = False
        return {"state": "done", "progress": {"done": 1, "total": 1, "note": "current"}}
    key = _job_key()
    state = jobs.get(key)
    if state and state.get("state") == "done":
        jobs.forget(key)
    state = jobs.submit(key, lambda progress: relation_index.build(progress))
    if state["state"] == "building" and wait > 0:
        state = jobs.wait(key, wait) or state
    if state.get("state") == "done":
        _STALE[repo] = False
    # A store from an earlier build still answers while the incremental pass runs
    if state.get("state") == "building" and covered:
        return {**state, "state": "done", "refreshing": True}
    return state


def mark_stale() -> None:
    """The `git` token changed: the next request checks the heads and refreshes incrementally."""
    _STALE.clear()


register_refs_changed(mark_stale)


# ----------------------------------------------------------------------------------- database

_DB_READY: RepoScopedCache = RepoScopedCache(maxsize=64)


def _text(v: Any) -> str:
    return bytes(v).decode("utf-8", "replace") if isinstance(v, (bytes, bytearray, memoryview)) else str(v or "")


def _bytes(v: Any) -> bytes:
    return bytes(v) if v is not None else b""


def db_ready() -> bool:
    """m_commit_relation covers every stored commit (backfill state "done:") and the active repository has
    a network: the database answers. A "not ready" answer is checked again after DB_READY_TTL."""
    hit = _DB_READY.get("ready")
    if hit is not None and (hit[0] or time.monotonic() - hit[1] < DB_READY_TTL):
        return hit[0]
    ok = False
    try:
        with get_reader() as cur:
            row = cur.select_one("m_commit_relation", ["note"], {"commit_id": 0, "kind": 0, "idx": 0}) or {}
            ok = _text(row.get("note")).startswith(BACKFILL_DONE) and get_active_network_id(cur) is not None
    except Exception as exc:  # noqa: BLE001 - an older database (no m_commit_relation)
        logger.debug("commit relation table unavailable: %s", exc)
    _DB_READY["ready"] = (ok, time.monotonic())
    return ok


def relations_token() -> str:
    """Changes whenever relation answers may change (part of report cache keys)."""
    if db_ready():
        try:
            with get_reader() as cur:
                row = cur.select_one("m_commit_relation", {"m": Agg("MAX", "commit_id")}) or {}
                return f"db:{row.get('m')}"
        except Exception:  # noqa: BLE001
            pass
    return f"index:{relation_index.meta('built_at') if relation_index.built() else ''}"


def _db_commits(cur: Any, network_id: int, shas: Iterable[str]) -> dict[str, dict[str, Any]]:
    """{sha: {commit_id, date, subject}} of the commits m_commit holds for the network."""
    keys = [s for s in dict.fromkeys(shas) if s and _FULL_SHA.match(s)]
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(keys), DB_CHUNK):
        part = keys[i:i + DB_CHUNK]
        for r in cur.select_rows(
            "m_commit", ["commit_id", "commit_hash", "author_date", "subject"],
            {"network_id": network_id, "commit_hash": [bytes.fromhex(k) for k in part]},
        ):
            out[_bytes(r["commit_hash"]).hex()] = {"commit_id": int(r["commit_id"]), "date": int(r["author_date"] or 0),
                                                   "subject": _text(r["subject"])[:SUBJECT_MAX]}
    return out


def _forward(cur: Any, commit_ids: list[int], kinds: tuple[int, ...] = (KIND_FIXES, KIND_REVERTS, KIND_UPSTREAM, KIND_STABLE_CC)) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for i in range(0, len(commit_ids), DB_CHUNK):
        part = commit_ids[i:i + DB_CHUNK]
        for r in cur.select_rows(
            "m_commit_relation", ["commit_id", "kind", "idx", "target_hash", "target_prefix", "note"],
            {"commit_id": part, "kind": list(kinds)}, order_by=["commit_id", "kind", "idx"],
        ):
            out.setdefault(int(r["commit_id"]), []).append(r)
    return out


def _target(row: dict[str, Any]) -> str | None:
    """Full SHA of a relation row's target: stored, else the written prefix resolved now (unknown: None)."""
    h = _bytes(row.get("target_hash"))
    if h and h != ZERO_HASH:
        return h.hex()
    prefix = _text(row.get("target_prefix")).lower()
    return relation_index.resolve_prefix(prefix) if prefix else None


def _reverse(cur: Any, network_id: int, sha: str, kinds: tuple[int, ...]) -> list[dict[str, Any]]:
    """Ingested commits naming `sha` (stored target, or an unresolved prefix of it): [{kind, sha, date, subject}]."""
    columns = {"kind": "r.kind", "commit_hash": "c.commit_hash", "author_date": "c.author_date", "subject": "c.subject"}
    joins = [Join(("m_commit", "c"), {"c.commit_id": "r.commit_id"})]
    rows: list[dict[str, Any]] = cur.select_rows(
        ("m_commit_relation", "r"), columns,
        {"r.target_hash": bytes.fromhex(sha), "r.kind": list(kinds), "c.network_id": network_id}, joins=joins,
    )
    prefixes = [sha[:k] for k in range(8, 40)]
    rows.extend(cur.select_rows(
        ("m_commit_relation", "r"), columns,
        {"r.target_hash": ZERO_HASH, "r.kind": list(kinds), "r.target_prefix": prefixes, "c.network_id": network_id},
        joins=joins,
    ))
    seen: set[tuple[int, str]] = set()
    out = []
    for r in rows:
        key = (int(r["kind"]), _bytes(r["commit_hash"]).hex())
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": key[0], "sha": key[1], "date": int(r["author_date"] or 0), "subject": _text(r["subject"])[:SUBJECT_MAX]})
    return out


def db_relations(sha: str) -> dict[str, Any] | None:
    """Relations of an ingested commit from m_commit_relation / m_commit_link, in the shape of
    RelationIndex.relations(); None when the database does not answer (not ready, commit not ingested)."""
    if not _FULL_SHA.match(sha or "") or not db_ready():
        return None
    try:
        with get_reader() as cur:
            network_id = get_active_network_id(cur)
            if network_id is None:
                return None
            own = _db_commits(cur, network_id, [sha]).get(sha)
            if own is None:
                return None
            out: dict[str, Any] = {"sha": sha, "source": "db", **{f: [] for f in _LISTS}, "stable_cc": [], "links": []}
            for r in _forward(cur, [own["commit_id"]]).get(own["commit_id"], []):
                kind = int(r["kind"])
                if kind == KIND_STABLE_CC:
                    out["stable_cc"].append(_text(r["note"]))
                    continue
                full = _target(r)
                if kind == KIND_FIXES:
                    out["fixes"].append({"sha": full, "prefix": _text(r["target_prefix"]), "subject": _text(r["note"]), "resolved": bool(full)})
                elif full:
                    out["reverts" if kind == KIND_REVERTS else "upstream"].append({"sha": full})
            incoming = _reverse(cur, network_id, sha, (KIND_FIXES, KIND_REVERTS, KIND_UPSTREAM))
            for field, kind in (("fixed_by", KIND_FIXES), ("reverted_by", KIND_REVERTS), ("backports", KIND_UPSTREAM)):
                items = [{"sha": r["sha"], "date": r["date"], "subject": r["subject"]} for r in incoming if r["kind"] == kind]
                if len(items) > LIST_CAP:
                    items.sort(key=lambda it: it["date"], reverse=True)
                    out[f"{field}_total"] = len(items)
                    items = items[:LIST_CAP]
                out[field] = items
            out["links"] = [_text(r["message_id"]) for r in cur.select_rows("m_commit_link", ["message_id"], {"commit_id": own["commit_id"]})]
            targets = [it["sha"] for f in ("fixes", "reverts", "upstream") for it in out[f] if it.get("sha")]
            known = _db_commits(cur, network_id, targets)
    except Exception as exc:  # noqa: BLE001 - the relation index answers
        logger.debug("database relations of %s failed: %s", sha, exc)
        return None
    others = git_subjects(bytes.fromhex(t) for t in targets if t not in known)
    for field in _LISTS:
        for item in out[field]:
            if not item.get("sha"):
                continue
            if "date" not in item:
                hit = known.get(item["sha"])
                at, subject = (hit["date"], hit["subject"]) if hit else others.get(bytes.fromhex(item["sha"]), (0, ""))
                item["date"] = at
                if subject and not item.get("subject"):
                    item["subject"] = subject
            item.setdefault("subject", "")
        out[field].sort(key=lambda it: it.get("date") or 0)
    out["date"], out["subject"] = own["date"], own["subject"]
    return out


def db_counts(hashes: list[str]) -> dict[str, dict[str, Any]]:
    """Relation counts of the ingested commits among `hashes` (full SHAs): {sha: {fixes, fixed_by,
    reverted_by, backports, stable_cc}}; commits the database does not hold are left out."""
    out: dict[str, dict[str, Any]] = {}
    keys = [h for h in dict.fromkeys(hashes) if _FULL_SHA.match(h or "")]
    if not keys or not db_ready():
        return out
    try:
        with get_reader() as cur:
            network_id = get_active_network_id(cur)
            if network_id is None:
                return out
            own = _db_commits(cur, network_id, keys)
            by_id = {v["commit_id"]: k for k, v in own.items()}
            fwd = _forward(cur, list(by_id), (KIND_FIXES, KIND_STABLE_CC))
            for cid, sha in by_id.items():
                rows = fwd.get(cid, [])
                out[sha] = {"fixes": sum(int(r["kind"]) == KIND_FIXES for r in rows), "fixed_by": 0, "reverted_by": 0,
                            "backports": 0, "stable_cc": any(int(r["kind"]) == KIND_STABLE_CC for r in rows)}
            for sha in out:
                for r in _reverse(cur, network_id, sha, (KIND_FIXES, KIND_REVERTS, KIND_UPSTREAM)):
                    field = {KIND_FIXES: "fixed_by", KIND_REVERTS: "reverted_by", KIND_UPSTREAM: "backports"}[r["kind"]]
                    out[sha][field] += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("database relation counts failed: %s", exc)
        return {}
    for c in out.values():
        for f in ("fixed_by", "reverted_by", "backports"):
            c[f] = min(c[f], LIST_CAP)
    return out


def db_fixes_of(sha: str) -> list[str] | None:
    """Targets of an ingested commit's `Fixes:` tags (resolved); None when the database does not answer."""
    if not _FULL_SHA.match(sha or "") or not db_ready():
        return None
    try:
        with get_reader() as cur:
            network_id = get_active_network_id(cur)
            own = _db_commits(cur, network_id, [sha]).get(sha) if network_id is not None else None
            if own is None:
                return None
            rows = _forward(cur, [own["commit_id"]], (KIND_FIXES,)).get(own["commit_id"], [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("database Fixes: of %s failed: %s", sha, exc)
        return None
    return list(dict.fromkeys(t for t in (_target(r) for r in rows) if t))


def db_backports_of(sha: str) -> list[str] | None:
    """Ingested commits saying they are a backport of `sha`; None when the database does not answer."""
    if not _FULL_SHA.match(sha or "") or not db_ready():
        return None
    try:
        with get_reader() as cur:
            network_id = get_active_network_id(cur)
            if network_id is None:
                return None
            return [r["sha"] for r in _reverse(cur, network_id, sha, (KIND_UPSTREAM,))]
    except Exception as exc:  # noqa: BLE001
        logger.debug("database backports of %s failed: %s", sha, exc)
        return None


def db_commits_for_mids(mids: list[str]) -> dict[str, list[str]] | None:
    """{normalized mid: [sha]} of the ingested commits whose Link: / Message-Id: names each message;
    None when the database does not answer."""
    keys = [m for m in dict.fromkeys(normalize_mid(m) for m in mids) if m]
    if not db_ready():
        return None
    out: dict[str, list[str]] = {m: [] for m in keys}
    try:
        with get_reader() as cur:
            network_id = get_active_network_id(cur)
            if network_id is None:
                return None
            for i in range(0, len(keys), DB_CHUNK):
                part = keys[i:i + DB_CHUNK]
                for r in cur.select_rows(
                    ("m_commit_link", "l"), {"message_id": "l.message_id", "commit_hash": "c.commit_hash"},
                    {"l.message_id": part, "c.network_id": network_id},
                    joins=[Join(("m_commit", "c"), {"c.commit_id": "l.commit_id"})],
                ):
                    out.setdefault(_text(r["message_id"]), []).append(_bytes(r["commit_hash"]).hex())
    except Exception as exc:  # noqa: BLE001
        logger.debug("database mail links failed: %s", exc)
        return None
    return out


def tags_ingested(tags: list[str]) -> bool:
    """Every commit of the release segments of `tags` (m_commit_release, D03) is in the active network's
    m_commit, so its relation rows exist: reverse lists over those releases are complete. False when a tag
    has no current segment. Cached per tag set and release/relation token."""
    from webapp.backend.services import release_service as rs
    if not tags or not db_ready() or not rs.db_covers_all():
        return False
    key = ("ingested", tuple(sorted(tags)), rs.release_token(), relations_token())
    hit = _DB_READY.get(key)
    if hit is not None:
        return hit
    current = rs.tag_commits()
    ok = False
    try:
        with get_reader() as cur:
            repo_id = get_active_repo_id(cur)
            network_id = get_active_network_id(cur)
            seg: dict[str, int] = {}
            for i in range(0, len(tags), DB_CHUNK):
                part = tags[i:i + DB_CHUNK]
                for r in cur.select_rows(
                    "m_release_segment", ["segment_id", "tag", "tag_hash"], {"repo_id": repo_id, "tag": part}
                ):
                    if _bytes(r["tag_hash"]).hex() == current.get(_text(r["tag"])):
                        seg[_text(r["tag"])] = int(r["segment_id"])
            ok = network_id is not None and all(t in seg for t in tags)
            ids = sorted(seg.values())
            for i in range(0, len(ids), DB_CHUNK) if ok else ():
                part = ids[i:i + DB_CHUNK]
                row = cur.select_one(
                    ("m_commit_release", "cr"),
                    {"n": Agg("COUNT", "*"), "k": Agg("COUNT", "c.commit_id")},
                    {"cr.repo_id": repo_id, "cr.segment_id": part},
                    joins=[Join(("m_commit", "c"), {"c.network_id": Value(network_id), "c.commit_hash": "cr.commit_hash"}, "left")],
                ) or {}
                if int(row.get("n") or 0) != int(row.get("k") or 0):
                    ok = False
                    break
    except Exception as exc:  # noqa: BLE001
        logger.debug("release ingestion coverage failed: %s", exc)
        ok = False
    _DB_READY[key] = ok
    return ok


# ----------------------------------------------------------------------------------- readers

def fixes_of(sha: str) -> list[str]:
    """Commits a commit's `Fixes:` tags name: database first, else the relation index when built."""
    hit = db_fixes_of(sha)
    if hit is not None:
        return hit
    return relation_index.fixes_of(sha) if relation_index.built() else []


def backports_of(sha: str, complete: bool = False) -> list[str]:
    """Commits saying they are a backport of `sha`: database first (when `complete`, the caller checked
    with tags_ingested() that the releases it reads are ingested), else the relation index when built."""
    if complete:
        hit = db_backports_of(sha)
        if hit is not None:
            return hit
    return relation_index.backports_of(sha) if relation_index.built() else []


def relations(sha: str) -> dict[str, Any]:
    """Relations of one commit (validated full or abbreviated SHA): 404 unknown; the database answers for
    ingested commits, else the relation index, or its job state while its first build runs
    ({"state": "building", ...})."""
    from webapp.backend.services.release_service import resolve_sha
    full = sha.lower() if _FULL_SHA.match(str(sha).lower()) else resolve_sha(sha)
    if not full:
        raise HTTPException(status_code=404, detail=f"Unknown commit: {sha}")
    hit = db_relations(full)
    if hit is not None:
        return {"state": "done", "refreshing": False, **hit}
    state = ensure_index()
    if state.get("state") != "done":
        return {"state": state.get("state"), "progress": state.get("progress"), "sha": full}
    return {"state": "done", "refreshing": bool(state.get("refreshing")), "source": "index", **relation_index.relations(full)}


def relation_counts(hashes: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """({sha: counts} answered by the database, [full SHAs it does not hold])."""
    found = db_counts(hashes)
    return found, [h for h in hashes if h not in found]


def status() -> dict[str, Any]:
    built = relation_index.meta("built_at") if relation_index.built() else None
    st = jobs.get(_job_key())
    database = db_ready()
    return {
        "state": (st or {}).get("state") or ("done" if built or database else "missing"),
        "database": database,
        "built_at": float(built) if built else None,
        "progress": (st or {}).get("progress"),
        **(relation_index.counts() if built else {}),
    }
