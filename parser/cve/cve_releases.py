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
- New release segments (or segments with a moved tag or format bump) are evaluated once per segment inside
  their own transaction, making full backfills resumable and atomic per segment.
"""
from __future__ import annotations

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
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from db_engine.spec import Agg, Join
from parser.cve.cve_ingest import LAYOUTS, _ids_of, changed_paths
from parser.cve.cve_records import HEXSHA, version_status
from parser.git_ast.commit_relations import (
    BACKFILL_DONE,
    KIND_BACKFILL_STATE,
    KIND_FIXES,
    KIND_UPSTREAM,
    ZERO_HASH,
    parse_relations,
    resolve_prefixes,
)
from parser.git_ast.release_order import parse_tag, release_key, series_name

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
_CVE_BATCH = 500
_SHA_BATCH = 1000
_INSERT_CHUNK = 5000


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
        self.base_key = self.release_key(series_name(self.p["series"])) if self.p else None

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
        return bool(self.p and self.p["stable"] and q and q["series"] == self.p["series"])

    def first_here(self, sha: str | None) -> bool:
        """The commit first appears in this release: its stable update, its -rc, or its cycle (-rc1 .. final)."""
        t = self.tags(sha)
        if not t or not self.p:
            return False
        first = t.get("stable") if self.p["stable"] else t.get("mainline")
        if not first:
            return False
        if self.p["stable"] or self.p["pre"]:
            return first == self.release
        q = self.parse_tag(first)
        return bool(q) and not q["stable"] and q["series"] == self.p["series"]


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
    """True when a fixed CVE has a fixing commit first released in `ctx.release`."""
    if state != "fixed":
        return False
    return any(ctx.first_here(r["fixed"]) for r in (rec.get("ranges") or ()) if r.get("fixed"))


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
        full = self._full(sha)
        return self._tags.get(full) if full else None

    def backports(self, sha: str) -> list[str]:
        full = self._full(sha)
        return self._backports.get(full, []) if full else []

    def fixes_tags(self, sha: str) -> list[str]:
        full = self._full(sha)
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


def _git_backports_map(repo: Path, targets: set[str]) -> dict[str, list[str]]:
    """Scan Git commit messages for `upstream` / `cherry picked` references to `targets` (full SHAs)."""
    if not targets or not _git_has_commits(repo):
        return {}
    # Index targets by their first 7 hex chars (UPSTREAM_RES requires 7-40 hex chars) for O(1) prefix matching
    by_prefix7: dict[str, list[str]] = defaultdict(list)
    for sha in targets:
        by_prefix7[sha[:7]].append(sha)

    proc = subprocess.Popen(
        [
            "git", "-C", str(repo), "log", "--all", "--regexp-ignore-case",
            "--grep=upstream", "--grep=cherry picked from commit",
            "--format=%x1e%H%x1f%B",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=1024 * 1024,
    )
    assert proc.stdout is not None
    try:
        raw = proc.stdout.read().decode("utf-8", "replace")
    finally:
        proc.stdout.close()
        proc.wait(timeout=600)

    backports: dict[str, list[str]] = defaultdict(list)
    ambiguous_prefixes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for chunk in raw.split("\x1e"):
        if not chunk:
            continue
        sep = chunk.find("\x1f")
        if sep < 0:
            continue
        bp_sha = chunk[:sep].strip().lower()
        if not _FULL.match(bp_sha):
            continue
        body = chunk[sep + 1:]
        for up in parse_relations(body)["upstream"]:
            if up in targets:
                if bp_sha not in backports[up]:
                    backports[up].append(bp_sha)
                continue
            if len(up) < 40:
                cands = [t for t in by_prefix7.get(up[:7], ()) if t.startswith(up)]
                if len(cands) == 1:
                    # Verify against git only if ambiguous in the whole repo? Wait: resolve_prefixes handles repo ambiguity
                    ambiguous_prefixes[up].append((bp_sha, cands[0]))
                elif len(cands) > 1:
                    ambiguous_prefixes[up].append((bp_sha, ""))
    if ambiguous_prefixes:
        resolved = resolve_prefixes(repo, ambiguous_prefixes.keys())
        for up, items in ambiguous_prefixes.items():
            full = resolved.get(up)
            if full and full in targets:
                for bp_sha, _ in items:
                    if bp_sha not in backports[full]:
                        backports[full].append(bp_sha)
    return dict(backports)


def _db_covers_commits(db: Any, repo_id: int, network_id: int | None) -> bool:
    """True when `m_commit_relation` finished backfilling and `m_commit` holds all commits of `m_commit_release`."""
    from db_engine.spec import Value

    if network_id is None:
        return False
    try:
        row = db.select_one(
            "m_commit_relation", ["note"], {"commit_id": 0, "kind": KIND_BACKFILL_STATE, "idx": 0}, as_dict=True
        )
        if not row or not _text(row["note"]).startswith(BACKFILL_DONE):
            return False
        sids = db.select_values("m_release_segment", "segment_id", {"repo_id": repo_id})
        if not sids:
            return False
        for i in range(0, len(sids), 200):
            part = sids[i:i + 200]
            counts = db.select_one(
                ("m_commit_release", "cr"),
                {"n": Agg("COUNT", "*"), "k": Agg("COUNT", "c.commit_id")},
                {"cr.repo_id": repo_id, "cr.segment_id": part},
                joins=[Join(("m_commit", "c"), {"c.network_id": Value(network_id), "c.commit_hash": "cr.commit_hash"}, "left")],
                as_dict=True,
            ) or {}
            if int(counts.get("n") or 0) != int(counts.get("k") or 0):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def build_lookup_data(
    db: Any,
    repo_id: int,
    recs: list[dict[str, Any]],
    kernel_repo: Path | None = None,
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
        for i in range(0, len(short_shas), _SHA_BATCH):
            part = short_shas[i:i + _SHA_BATCH]
            for r in db.select_rows(
                "m_cve_commit_hash", ["prefix", "commit_hash"], {"repo_id": repo_id, "prefix": part}, as_dict=True
            ):
                short_to_full[_text(r["prefix"])] = _hex(r["commit_hash"])
        if kernel_repo is not None:
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
    sha_tags: dict[str, dict[str, str | None]] = {}
    for i in range(0, len(needed_shas), _SHA_BATCH):
        part = [bytes.fromhex(s) for s in needed_shas[i:i + _SHA_BATCH]]
        for r in db.select_rows(
            ("m_commit_release", "cr"),
            {"commit_hash": "cr.commit_hash", "chain": "cr.chain", "tag": "s.tag"},
            {"cr.repo_id": repo_id, "cr.commit_hash": part},
            joins=[Join(("m_release_segment", "s"), {"s.segment_id": "cr.segment_id"})],
            as_dict=True,
        ):
            sha = _hex(r["commit_hash"])
            entry = sha_tags.setdefault(sha, {"mainline": None, "stable": None})
            entry["mainline" if int(r["chain"]) == DB_CHAIN_MAINLINE else "stable"] = _text(r["tag"])

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
        rows = list(db.select_rows("m_cve", ["cve_id", "data"]))
    else:
        for i in range(0, len(cve_ids), _CVE_BATCH):
            rows.extend(db.select_rows("m_cve", ["cve_id", "data"], {"cve_id": cve_ids[i:i + _CVE_BATCH]}))
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
            out.append(rec)
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
    """CVE IDs in `m_cve_commit` whose commit (full or via `m_cve_commit_hash`) lies in `segment_ids`."""
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
    hexes = sorted({h.hex() for h in hashes})
    out: set[str] = set()
    for i in range(0, len(hexes), _CVE_BATCH):
        part = hexes[i:i + _CVE_BATCH]
        for cid in db.select_values("m_cve_commit", "cve_id", {"commit_hash": part}, distinct=True):
            out.add(_text(cid))
    # Also resolve prefixes in m_cve_commit_hash pointing to these commit hashes
    prefixes: list[str] = []
    for i in range(0, len(hexes), _CVE_BATCH):
        part_bytes = [bytes.fromhex(h) for h in hexes[i:i + _CVE_BATCH]]
        for p in db.select_values(
            "m_cve_commit_hash", "prefix", {"repo_id": repo_id, "commit_hash": part_bytes}, distinct=True
        ):
            prefixes.append(_text(p))
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
    return max(1, min(os.cpu_count() or 4, 8) - 1)


_POOL_DATA: dict[str, Any] = {}


def _init_worker(recs: list[dict[str, Any]], lookup: dict[str, Any], ptags: dict[str, Any], rkeys: dict[str, Any]) -> None:
    _POOL_DATA["recs"] = recs
    _POOL_DATA["lookup"] = lookup
    _POOL_DATA["ptags"] = ptags
    _POOL_DATA["rkeys"] = rkeys


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
                on_conflict="update",
            )
        db.insert_rows(
            "m_cve_release_state",
            _STATE_COLUMNS,
            [state_row],
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
) -> dict[str, Any]:
    """Bring `m_cve_release` and `m_cve_release_state` for `repo_id` up to `m_cve_source.head`.

    Returns summary `{state: "missing" | "unavailable" | "current" | "updated", segments, incremental, full, seconds}`.
    """
    started = time.time()
    if db is None:
        return {"state": "unavailable", "segments": 0, "incremental": 0, "full": 0}
    if hasattr(db, "table_exists"):
        if not (
            db.table_exists("m_cve_source")
            and db.table_exists("m_release_segment")
            and db.table_exists("m_cve_release_state")
            and db.table_exists("m_cve_release")
        ):
            return {"state": "unavailable", "segments": 0, "incremental": 0, "full": 0}

    src = _cve_source(db)
    if src is None:
        return {"state": "missing", "segments": 0, "incremental": 0, "full": 0}

    seg_rows = db.select_rows(
        "m_release_segment",
        ["segment_id", "tag", "tag_hash"],
        {"repo_id": repo_id},
        as_dict=True,
    )
    if not seg_rows:
        return {"state": "current", "segments": 0, "incremental": 0, "full": 0, "head": src["head"]}

    segments: list[dict[str, Any]] = []
    for r in seg_rows:
        tag = _text(r["tag"])
        segments.append({"segment_id": int(r["segment_id"]), "tag": tag, "tag_hash": _hex(r["tag_hash"])})
    all_sids = [s["segment_id"] for s in segments]
    max_sid = max(all_sids)
    segments.sort(key=lambda s: release_key(s["tag"]))
    if from_tag:
        from_key = release_key(from_tag)
        segments = [s for s in segments if release_key(s["tag"]) >= from_key]
    if not segments:
        return {"state": "current", "segments": 0, "incremental": 0, "full": 0, "head": src["head"]}

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
        if st["cve_head"] == src["head"] and not extra_set and not (lag is not None and new_seg_ids.get(lag)):
            continue
        inc_groups[(st["cve_head"], lag)].append(seg)

    # Resolve diff CVE IDs per distinct old `cve_head`; any group whose diff cannot be computed falls back to full.
    inc_plans: list[tuple[list[dict[str, Any]], list[str]]] = []
    root = LAYOUTS.get(src["layout"])
    for (old_head, lag), grp_segs in inc_groups.items():
        extra = extra_set | (new_seg_ids.get(lag, set()) if lag is not None else set())
        if old_head == src["head"]:
            inc_plans.append((grp_segs, sorted(extra)))
        elif prev_head and old_head == prev_head and changed_ids is not None:
            inc_plans.append((grp_segs, sorted(set(changed_ids) | extra)))
        elif cve_repo is not None and root:
            paths = changed_paths(cve_repo, old_head, src["head"], root)
            if paths is None:
                full_segs.extend(grp_segs)
            else:
                inc_plans.append((grp_segs, sorted(set(_ids_of(paths)) | extra)))
        else:
            full_segs.extend(grp_segs)

    if not inc_plans and not full_segs:
        return {"state": "current", "segments": 0, "incremental": 0, "full": 0, "head": src["head"]}

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

    for grp_segs, diff_ids in inc_plans:
        if not diff_ids:
            # Head changed without touching any kernel CVE IDs (e.g. non-kernel CVEs in cvelistV5)
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

        diff_recs = _load_records(db, diff_ids)
        lookup = build_lookup_data(db, repo_id, diff_recs, kernel_repo=kernel_repo)
        all_tags = {s["tag"] for s in grp_segs}
        for tags_entry in lookup["sha_tags"].values():
            if tags_entry.get("mainline"):
                all_tags.add(tags_entry["mainline"])
            if tags_entry.get("stable"):
                all_tags.add(tags_entry["stable"])
        ptags = {t: parse_tag(t) for t in all_tags}
        rkeys = {t: release_key(t) for t in all_tags}
        for s in grp_segs:
            rows = evaluate_segment_records(
                s["tag"], diff_recs, lookup, parsed_tags=ptags, release_keys=rkeys
            )
            _write_segment_incremental(db, repo_id, s, src, diff_ids, rows, built_at, max_sid)
            inc_count += 1
            _tick("incremental", inc_count, inc_total, s["tag"], len(rows))

    if full_segs:
        full_segs.sort(key=lambda s: release_key(s["tag"]))
        all_recs = _load_records(db)
        logger.info(
            f"[CVE Releases] {len(full_segs)} release segment(s) to evaluate in full against {len(all_recs)} CVE record(s)."
        )
        lookup = build_lookup_data(db, repo_id, all_recs, kernel_repo=kernel_repo)
        all_tags = {s["tag"] for s in full_segs}
        for tags_entry in lookup["sha_tags"].values():
            if tags_entry.get("mainline"):
                all_tags.add(tags_entry["mainline"])
            if tags_entry.get("stable"):
                all_tags.add(tags_entry["stable"])
        ptags = {t: parse_tag(t) for t in all_tags}
        rkeys = {t: release_key(t) for t in all_tags}

        seg_by_id = {s["segment_id"]: s for s in full_segs}
        total = len(full_segs)
        written: set[int] = set()
        use_pool = total >= 4 and len(all_recs) >= 200 and _process_workers() > 1
        if use_pool:
            try:
                items = [(s["segment_id"], s["tag"]) for s in full_segs]
                with ProcessPoolExecutor(
                    max_workers=_process_workers(),
                    initializer=_init_worker,
                    initargs=(all_recs, lookup, ptags, rkeys),
                ) as pool:
                    for seg_id, rows in pool.map(_worker_eval, items):
                        _write_segment_full(db, repo_id, seg_by_id[seg_id], src, rows, built_at, max_sid)
                        written.add(seg_id)
                        full_count += 1
                        _tick("full", full_count, total, seg_by_id[seg_id]["tag"], len(rows))
            except (BrokenProcessPool, OSError, PermissionError):
                use_pool = False

        if not use_pool:
            for s in full_segs:
                if s["segment_id"] in written:
                    continue
                rows = evaluate_segment_records(
                    s["tag"], all_recs, lookup, parsed_tags=ptags, release_keys=rkeys
                )
                _write_segment_full(db, repo_id, s, src, rows, built_at, max_sid)
                full_count += 1
                _tick("full", full_count, total, s["tag"], len(rows))

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
