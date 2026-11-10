"""parser/git_ast/commit_relations.py - What commit messages say about other commits and mail.

Pure parsers (shared with the webapp's commit relation index, webapp/backend/services/commit_relations.py):

- `Fixes: <hex> ("subject")`                         -> KIND_FIXES
- `This reverts commit <sha>.`                        -> KIND_REVERTS
- `commit <sha> upstream.`, `[ Upstream commit <sha> ]`, `(cherry picked from commit <sha>)`
                                                       -> KIND_UPSTREAM (this commit is a backport of <sha>)
- `Cc: stable@vger.kernel.org # 3.0+`                 -> KIND_STABLE_CC (no target; the annotation)
- `Link: https://lore.kernel.org/r/<mid>`, `Message-Id: <mid>` -> links

`relation_rows()` turns one message into the rows of `m_commit_relation` / `m_commit_link` (core/API.md
section 4, rows 63-64), written at Step 6.5 and by `--backfill-commit-relations` (core/backfills.py).

`resolve_prefixes(repo, prefixes)`: abbreviated commit hashes (CVE records, `Fixes:` tags) to the commit
they name in one repository, with one `git cat-file --batch-check` process. A prefix that names no commit
or several (ambiguous) gets no answer.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

KIND_FIXES, KIND_REVERTS, KIND_UPSTREAM, KIND_STABLE_CC = 1, 2, 3, 4
KIND_BACKFILL_STATE = 0  # commit_id 0 only: progress of --backfill-commit-relations (BACKFILL_* notes)
SUBJECT_MAX = 200
NOTE_MAX = 255
MID_MAX = 255
ZERO_HASH = b"\x00" * 20
BACKFILL_RUNNING = "next:"
BACKFILL_DONE = "done:"

_PREFIX = re.compile(r"^[0-9a-f]{4,40}$")
_FULL = re.compile(r"^[0-9a-f]{40}$")

FIXES_RE = re.compile(
    r"^[ \t]*Fixes:[ \t]*(?:commit[ \t]+)?([0-9a-fA-F]{7,40})\b(?:[ \t]*\([ \t]*[\"']?(.*?)[\"']?[ \t]*\)[ \t]*)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
REVERT_RE = re.compile(r"^[ \t]*This reverts commit ([0-9a-fA-F]{7,40})\b", re.IGNORECASE | re.MULTILINE)
REVERT_SUBJ_RE = re.compile(r'^[ \t]*Revert\s+["\']?([0-9a-fA-F]{7,40})\b', re.IGNORECASE | re.MULTILINE)
UPSTREAM_RES = (
    re.compile(r"^[ \t]*commit ([0-9a-fA-F]{7,40})\b[ \t]*upstream\.?[ \t]*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*\[[ \t]*upstream commit ([0-9a-fA-F]{7,40})\b[ \t]*\]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*\(cherry picked from commit ([0-9a-fA-F]{7,40})\b\)", re.IGNORECASE | re.MULTILINE),
)
STABLE_CC_RE = re.compile(
    r"^[ \t]*Cc:[^\n]*?<?stable@(?:vger\.)?kernel\.org>?[ \t]*(?:#[ \t]*([^\n]*?))?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
LINK_RE = re.compile(
    r"^[ \t]*(?:Link|Closes|Lore):[ \t]*<?https?://(?:lore\.kernel\.org/(?:r|all|[A-Za-z0-9_.+-]+)|lkml\.kernel\.org/r|patch\.msgid\.link|"
    r"marc\.info/\?i=)/?([^\s/\"'>)]+)",
    re.IGNORECASE | re.MULTILINE,
)
MESSAGE_ID_RE = re.compile(r"^[ \t]*(?:Message-Id|Closes|Lore):[ \t]*<?([^\s<>]+@[^\s<>]+)>?", re.IGNORECASE | re.MULTILINE)
PATCHWORK_ID_RE = re.compile(r"^[ \t]*Patchwork-Id:[ \t]*<?([^\s<>]+)>?", re.IGNORECASE | re.MULTILINE)


def normalize_mid(mid: str) -> str:
    """Message-ids compare unbracketed, URL-decoded and case-insensitively (webapp/API.md section 5.26)."""
    m = unquote(str(mid or "")).strip().strip("<>").strip()
    return m.lower()


def parse_relations(text: str) -> dict[str, Any]:
    """Relations named by one commit message (or its trailer lines).

    -> {fixes: [(prefix, subject)], reverts: [hex], upstream: [hex], stable_cc: [annotation], links: [mid]};
    reverts/upstream hold what the message wrote (8-40 hex digits, lower-case), not always a full SHA."""
    out: dict[str, Any] = {"fixes": [], "reverts": [], "upstream": [], "stable_cc": [], "links": []}
    if not text:
        return out
    seen = set()
    for m in FIXES_RE.finditer(text):
        prefix = m.group(1).lower()
        if prefix in seen:
            continue
        seen.add(prefix)
        out["fixes"].append((prefix, (m.group(2) or "").strip()[:SUBJECT_MAX]))
    for m in REVERT_RE.finditer(text):
        sha = m.group(1).lower()
        if sha not in out["reverts"]:
            out["reverts"].append(sha)
    if not out["reverts"]:
        for m in REVERT_SUBJ_RE.finditer(text):
            sha = m.group(1).lower()
            if sha not in out["reverts"]:
                out["reverts"].append(sha)
    for rx in UPSTREAM_RES:
        for m in rx.finditer(text):
            sha = m.group(1).lower()
            if sha not in out["upstream"]:
                out["upstream"].append(sha)
    for m in STABLE_CC_RE.finditer(text):
        out["stable_cc"].append((m.group(1) or "").strip()[:100])
    for rx in (LINK_RE, MESSAGE_ID_RE, PATCHWORK_ID_RE):
        for m in rx.finditer(text):
            mid = normalize_mid(m.group(1))
            if mid and mid not in out["links"]:
                out["links"].append(mid)
    return out


def relation_rows(message: str, own_sha: str = "") -> tuple[list[tuple[int, int, str, str]], list[str]]:
    """([(kind, idx, target_prefix, note)], [message_id]) of one commit message. `idx` is the order within
    the message per kind; a backport naming the commit itself is dropped; identical `Cc: stable`
    annotations count once; message-ids longer than the column are dropped (they would not match)."""
    found = parse_relations(message)
    own = (own_sha or "").lower()
    rels: list[tuple[int, int, str, str]] = []
    for idx, (prefix, subject) in enumerate(found["fixes"]):
        rels.append((KIND_FIXES, idx, prefix, subject[:NOTE_MAX]))
    for idx, target in enumerate(found["reverts"]):
        rels.append((KIND_REVERTS, idx, target, ""))
    upstream = [t for t in found["upstream"] if not (own and own.startswith(t))]
    for idx, target in enumerate(upstream):
        rels.append((KIND_UPSTREAM, idx, target, ""))
    for idx, note in enumerate(dict.fromkeys(found["stable_cc"])):
        rels.append((KIND_STABLE_CC, idx, "", note[:NOTE_MAX]))
    links = [m for m in found["links"] if len(m) <= MID_MAX]
    return rels, links


def prefixes_to_resolve(rels: Iterable[tuple[int, int, str, str]]) -> set[str]:
    """Abbreviated targets of relation rows (full SHAs need no lookup)."""
    return {p for _k, _i, p, _n in rels if p and len(p) < 40}


def target_hash(prefix: str, resolved: dict[str, str]) -> bytes:
    """20-byte target of a written prefix: itself when full, its resolution, else zero (unknown/ambiguous)."""
    if not prefix:
        return ZERO_HASH
    full = prefix if _FULL.match(prefix) else resolved.get(prefix)
    return bytes.fromhex(full) if full else ZERO_HASH


def table_rows(commit_id: int, rels: Iterable[tuple[int, int, str, str]], links: Iterable[str],
               resolved: dict[str, str]) -> tuple[list[tuple], list[tuple]]:
    """m_commit_relation (commit_id, kind, idx, target_hash, target_prefix, note) and m_commit_link
    (commit_id, message_id) rows of one commit."""
    rel_rows = [(commit_id, kind, idx, target_hash(prefix, resolved), prefix, note) for kind, idx, prefix, note in rels]
    link_rows = [(commit_id, mid) for mid in dict.fromkeys(links)]
    return rel_rows, link_rows


def resolve_prefixes(repo: Path | str, prefixes: Iterable[str], timeout: float = 600.0) -> dict[str, str]:
    """{prefix: full commit SHA} for the hex prefixes (4-40 digits, lower-cased) naming exactly one commit."""
    wanted_set = {p.strip().lower() for p in prefixes if p and _PREFIX.match(p.strip().lower())}
    if not wanted_set:
        return {}
    resolved: dict[str, str] = {p: p for p in wanted_set if len(p) == 40}
    need_lookup = sorted([p for p in wanted_set if len(p) < 40])
    if not need_lookup:
        return resolved
    out = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch-check"],
        input="".join(f"{p}^{{commit}}\n" for p in need_lookup).encode(),
        capture_output=True,
        timeout=timeout,
        check=False,
    ).stdout.decode("utf-8", "replace").splitlines()
    for prefix, line in zip(need_lookup, out):
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit" and _FULL.match(parts[0]) and parts[0].startswith(prefix):
            resolved[prefix] = parts[0]
    return resolved
