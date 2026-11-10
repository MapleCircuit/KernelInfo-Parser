"""webapp/backend/services/patch_apply.py - Apply mailing-list patches to a revision in memory (P1, webapp/API.md section 4.8).

`git am` without `-3` and without fuzz, in pure Python: each hunk's pre-image (context and removed lines) must
match exactly, at its expected line or at the nearest line found searching outward (an offset, as git does).
Files are read from the revision through the persistent Git readers; nothing is written to the repository,
the worktree or the database. Patched texts live in a bounded memory cache under an `apply_id` derived from
(revision sha, message ids), so the same request rebuilds the same id after an eviction or a restart.

    extract_patch(raw_rfc822) -> {subject, author, date, base_commit, diff}
    parse_patch(diff_text)    -> [FilePatch]
    apply_series(sha, [(label, [FilePatch])], read) -> {patches: [...], files: {path: text|None}, base: {...}}
"""
from __future__ import annotations

import difflib
import email
import email.policy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_MESSAGES = 50
MAX_PATCH_BYTES = 5 << 20
MAX_FILES = 500
SEARCH_WINDOW = None  # search the whole file, like git's find_pos

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BASE_RE = re.compile(r"^base-commit:\s*([0-9a-f]{7,40})\s*$", re.M)
_DIFF_START_RE = re.compile(r"^(diff --git |--- (?:a/|/dev/null))", re.M)


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+", text with its newline)

    def images(self) -> tuple[list[str], list[str]]:
        pre = [t for op, t in self.lines if op in (" ", "-")]
        post = [t for op, t in self.lines if op in (" ", "+")]
        return pre, post


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    status: str = "M"  # M, A (created), D (deleted), R (renamed)
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    mode_change: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


# ------------------------------------------------------------------------------------------- extraction
def _payload_text(msg: email.message.Message) -> str:
    """The text of every text/plain or patch part (attachments included), decoded (QP, base64, charsets)."""
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        name = part.get_filename() or ""
        if ctype in ("text/plain", "text/x-patch", "text/x-diff", "application/x-patch") or name.endswith((".patch", ".diff")):
            try:
                text = part.get_content()
            except (LookupError, ValueError):
                raw = part.get_payload(decode=True) or b""
                text = raw.decode("utf-8", errors="replace")
            if isinstance(text, bytes):
                text = text.decode("utf-8", errors="replace")
            parts.append(text)
    return "\n".join(parts)


def extract_patch(raw: str) -> dict[str, Any]:
    """Subject, author, date, `base-commit:` and the diff part of one RFC-822 message (empty diff: no patch)."""
    msg = email.message_from_string(raw or "", policy=email.policy.default)
    body = _payload_text(msg).replace("\r\n", "\n")
    m = _DIFF_START_RE.search(body)
    base = _BASE_RE.search(body)
    return {
        "subject": str(msg.get("Subject", "") or "").strip(),
        "author": str(msg.get("From", "") or "").strip(),
        "date": str(msg.get("Date", "") or "").strip(),
        "base_commit": base.group(1) if base else "",
        "diff": body[m.start():] if m else "",
    }


# ---------------------------------------------------------------------------------------------- parsing
def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if path == "/dev/null":
        return None
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1].encode("latin-1", errors="replace").decode("unicode_escape").encode("latin-1", errors="replace").decode("utf-8", errors="replace")
    path = path.split("\t", 1)[0]
    return path[2:] if path[:2] in ("a/", "b/") else path


def safe_path(path: str | None) -> bool:
    """Patch paths are jailed to the tree: relative, no `..`, no `.git/`, no NUL or backslash."""
    if path is None:
        return True
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        return False
    parts = path.split("/")
    return ".." not in parts and "" not in parts and parts[0] != ".git"


def parse_patch(text: str) -> list[FilePatch]:
    """Files and hunks of a unified diff with git's extended headers. Hunks are read by their line counts,
    so text after the last hunk (the `-- ` signature, a reply) is ignored."""
    lines = text.splitlines(keepends=True)
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git (\"?a/.+?\"?) (\"?b/.+?\"?)\s*$", line)
            cur = FilePatch(_strip_prefix(m.group(1)) if m else None, _strip_prefix(m.group(2)) if m else None)
            files.append(cur)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            old = _strip_prefix(line[4:].rstrip("\n"))
            new = _strip_prefix(lines[i + 1][4:].rstrip("\n"))
            if cur is None or cur.hunks:
                cur = FilePatch(old, new)
                files.append(cur)
            else:
                cur.old_path, cur.new_path = old, new
            if old is None:
                cur.status = "A"
            elif new is None:
                cur.status = "D"
            i += 2
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur.status, cur.old_path = "A", None
            elif line.startswith("deleted file mode"):
                cur.status, cur.new_path = "D", None
            elif line.startswith("rename from "):
                cur.status, cur.old_path = "R", line[12:].rstrip("\n")
            elif line.startswith("rename to "):
                cur.status, cur.new_path = "R", line[10:].rstrip("\n")
            elif line.startswith(("old mode", "new mode")):
                cur.mode_change = True
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur.binary = True
            else:
                h = _HUNK_RE.match(line)
                if h:
                    hunk = Hunk(int(h.group(1)), int(h.group(2) or 1), int(h.group(3)), int(h.group(4) or 1))
                    old_left, new_left = hunk.old_count, hunk.new_count
                    i += 1
                    while i < n and (old_left > 0 or new_left > 0):
                        body = lines[i]
                        op = body[:1]
                        if body.startswith("\\"):
                            i += 1
                            continue
                        if body in ("\n", "\r\n"):
                            op, body = " ", " \n"  # an empty context line whose space was stripped by a mailer
                        if op == " ":
                            old_left -= 1
                            new_left -= 1
                        elif op == "-":
                            old_left -= 1
                        elif op == "+":
                            new_left -= 1
                        else:
                            break
                        hunk.lines.append((op, body[1:]))
                        i += 1
                        # "\ No newline at end of file" applies to the line just read
                        if i < n and lines[i].startswith("\\ No newline"):
                            o, t = hunk.lines[-1]
                            hunk.lines[-1] = (o, t[:-1] if t.endswith("\n") else t)
                            i += 1
                    cur.hunks.append(hunk)
                    continue
        i += 1
    return files


# --------------------------------------------------------------------------------------------- applying
def _matches(image: list[str], pre: list[str], at: int) -> bool:
    return image[at:at + len(pre)] == pre


def find_position(image: list[str], pre: list[str], expected: int, *, anchor_start: bool = False, anchor_end: bool = False) -> int | None:
    """Where `pre` matches exactly: `expected` first, then outward alternately (git's find_pos)."""
    last = len(image) - len(pre)
    if last < 0:
        return None
    if anchor_start and anchor_end:
        return 0 if last == 0 and _matches(image, pre, 0) else None
    if anchor_start:
        return 0 if _matches(image, pre, 0) else None
    if anchor_end:
        return last if _matches(image, pre, last) else None
    expected = max(0, min(expected, last))
    if _matches(image, pre, expected):
        return expected
    for d in range(1, last + 1):
        lo, hi = expected - d, expected + d
        if lo < 0 and hi > last:
            break
        if lo >= 0 and _matches(image, pre, lo):
            return lo
        if hi <= last and _matches(image, pre, hi):
            return hi
    return None


def nearest_partial(image: list[str], pre: list[str], expected: int) -> dict[str, Any]:
    """The region that matches the most pre-image lines (for a failed hunk): {line, matched, of}."""
    if not pre or not image:
        return {"line": max(1, expected + 1), "matched": 0, "of": len(pre)}
    best = (-1, 0)
    span = len(pre)
    for at in range(0, max(1, len(image) - span + 1)):
        score = sum(1 for k in range(span) if at + k < len(image) and image[at + k] == pre[k])
        if score > best[0] or (score == best[0] and abs(at - expected) < abs(best[1] - expected)):
            best = (score, at)
    return {"line": best[1] + 1, "matched": max(0, best[0]), "of": span}


def apply_hunks(image: list[str], hunks: list[Hunk]) -> tuple[list[str] | None, list[dict[str, Any]]]:
    """Apply hunks in order to `image` (lines with their newlines): (new image or None if any failed, results)."""
    out = list(image)
    results = []
    failed = False
    for h in hunks:
        pre, post = h.images()
        trailing = next((k for k, (op, _) in enumerate(reversed(h.lines)) if op != " "), len(h.lines))
        # git apply.c: a hunk starting at line 0/1 must match at the beginning, one without trailing
        # context at the end; there is no fuzz (no context reduction)
        anchor_start = h.old_start <= 1
        anchor_end = trailing == 0
        # The expected place in the image (earlier hunks applied) is the hunk's new position, as in git
        expected = h.new_start - 1 if h.new_start else 0
        pos = find_position(out, pre, expected, anchor_start=anchor_start, anchor_end=anchor_end)
        if pos is None:
            failed = True
            results.append({"status": "failed", "expected_line": expected + 1, "nearest": nearest_partial(out, pre, expected),
                            "context": [t.rstrip("\n") for t in pre[:12]]})
            continue
        out[pos:pos + len(pre)] = post
        offset = pos - expected
        results.append({"status": "offset" if offset else "applied", "line": pos + 1, "offset": offset})
    return (None if failed else out), results


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def apply_series(sha: str, patches: list[tuple[str, list[FilePatch]]], read: Callable[[str], str | None]) -> dict[str, Any]:
    """Apply patches in order on the in-memory files of `sha`. A patch with a failing file changes nothing
    (like `git am`); later patches still run against the files as they are. `read(path)` -> text or None."""
    state: dict[str, list[str] | None] = {}
    base: dict[str, str | None] = {}

    def load(path: str) -> list[str] | None:
        if path not in state:
            text = read(path)
            base[path] = text
            state[path] = _lines(text) if text is not None else None
        return state[path]

    out_patches = []
    nfiles = 0
    for label, files in patches:
        staged: dict[str, list[str] | None] = {}
        results = []
        ok = True
        for fp in files:
            nfiles += 1
            res: dict[str, Any] = {"path": fp.path, "old_path": fp.old_path, "change": fp.status, "hunks": []}
            if nfiles > MAX_FILES:
                res.update(status="unsupported", reason=f"more than {MAX_FILES} files")
            elif not safe_path(fp.old_path) or not safe_path(fp.new_path):
                res.update(status="failed", reason="path outside the tree")
            elif fp.binary:
                res.update(status="unsupported", reason="binary patch")
            else:
                def current(p: str) -> list[str] | None:
                    return staged[p] if p in staged else load(p)
                src = None if fp.status == "A" else current(fp.old_path or fp.path)
                if fp.status == "A" and current(fp.path) is not None:
                    res.update(status="failed", reason="already exists")
                elif fp.status != "A" and src is None:
                    res.update(status="failed", reason="does not exist")
                elif fp.status == "R" and current(fp.new_path or "") is not None:
                    res.update(status="failed", reason="rename target already exists")
                else:
                    new, hunks = apply_hunks(src or [], fp.hunks)
                    res["hunks"] = hunks
                    if new is None:
                        res["status"] = "failed"
                    else:
                        res["status"] = {"A": "created", "D": "deleted"}.get(fp.status) or (
                            "offset" if any(h["status"] == "offset" for h in hunks) else ("renamed" if fp.status == "R" else "applied"))
                        if fp.status == "D":
                            staged[fp.path] = None
                        else:
                            if fp.status == "R" and fp.old_path:
                                load(fp.new_path or "")
                                staged[fp.old_path] = None
                            staged[fp.path] = new
            if res["status"] in ("failed", "unsupported"):
                ok = False
            results.append(res)
        if ok:
            for p, v in staged.items():
                load(p)
                state[p] = v
        out_patches.append({"label": label, "status": "applied" if ok else "failed", "files": results,
                            "offset": ok and any(r["status"] == "offset" for r in results)})
    changed = {p: ("".join(v) if v is not None else None) for p, v in state.items() if (("".join(v)) if v is not None else None) != base.get(p)}
    return {"patches": out_patches, "files": changed, "base_texts": {p: base.get(p) for p in changed}}


def diff_hunks(old: str | None, new: str | None) -> list[dict[str, Any]]:
    """-U0 hunks between two texts in the shape of `GET /api/tools/diff/blobs` rows ([kind, old, new, text])."""
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    hunks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        rows = [["-", i + 1, None, a[i]] for i in range(i1, i2)] + [["+", None, j + 1, b[j]] for j in range(j1, j2)]
        hunks.append({"old_start": i1 + 1, "old_count": i2 - i1, "new_start": j1 + 1, "new_count": j2 - j1, "lines": rows})
    return hunks


def apply_id(sha: str, message_ids: list[str]) -> str:
    return hashlib.sha256(("\n".join([sha, *message_ids])).encode()).hexdigest()[:32]


# ------------------------------------------------------------------------------------------ the service
class PatchApplyService:
    """Fetch messages from the archive, apply them to a revision and keep the result in memory."""

    def __init__(self) -> None:
        from webapp.backend.config import RepoScopedCache
        self._cache: RepoScopedCache = RepoScopedCache(maxsize=16)

    @staticmethod
    def _reader(sha: str) -> Callable[[str], str | None]:
        from webapp.backend.services.git_reader import git_reader

        def read(path: str) -> str | None:
            data, is_dir, _ = git_reader.read_file(sha, path)
            if is_dir or data is None:
                return None
            return data.decode("utf-8", errors="surrogateescape")
        return read

    def apply(self, revision: str, message_ids: list[str]) -> dict[str, Any]:
        from fastapi import HTTPException
        from webapp.backend.services.mailing_list_service import mailing_list_service
        from webapp.backend.services.tree_resolver import git_tree_resolver
        mids = [str(m).strip().strip("<>") for m in message_ids if str(m).strip()]
        if not mids or len(mids) > MAX_MESSAGES:
            raise HTTPException(status_code=400, detail=f"message_ids must list 1 to {MAX_MESSAGES} messages")
        sha = git_tree_resolver.resolve_commit_sha(revision)
        if not sha:
            raise HTTPException(status_code=404, detail=f"Revision not found: {revision}")
        aid = apply_id(sha, mids)
        hit = self._cache.get(aid)
        if hit is not None:
            return {**hit["summary"], "cached": True}
        total = 0
        series: list[tuple[str, list[FilePatch]]] = []
        meta = []
        base_hint = ""
        for mid in mids:
            msg = mailing_list_service.get_message(mid)
            if not msg.get("available"):
                raise HTTPException(status_code=404, detail=f"Message not found: {mid}")
            raw = msg.get("raw") or ""
            total += len(raw.encode("utf-8", errors="replace"))
            if total > MAX_PATCH_BYTES:
                raise HTTPException(status_code=413, detail="At most 5 MB of messages")
            ext = extract_patch(raw)
            base_hint = base_hint or ext["base_commit"]
            files = parse_patch(ext["diff"]) if ext["diff"] else []
            meta.append({"message_id": mid, "subject": msg.get("subject") or ext["subject"], "author": msg.get("author") or ext["author"],
                         "has_diff": bool(files)})
            if files:
                series.append((mid, files))
        result = apply_series(sha, series, self._reader(sha))
        by_mid = {p["label"]: p for p in result["patches"]}
        patches = []
        for m in meta:
            p = by_mid.get(m["message_id"])
            patches.append({**m, "status": p["status"] if p else "no-diff", "offset": bool(p and p["offset"]), "files": p["files"] if p else []})
        applied = [p for p in patches if p["status"] == "applied"]
        hint = None
        if base_hint:
            hint_sha = git_tree_resolver.resolve_commit_sha(base_hint)
            hint = {"commit": base_hint, "resolved": bool(hint_sha), "sha": hint_sha}
        summary = {
            "apply_id": aid,
            "base": {"revision": revision, "sha": sha},
            "base_commit": hint,
            "patches": patches,
            "summary": {"patches": len(series), "applied": len(applied), "offset": sum(1 for p in applied if p["offset"]),
                        "failed": sum(1 for p in patches if p["status"] == "failed"), "files": len(result["files"])},
            "message_ids": mids,
        }
        self._cache[aid] = {"summary": summary, "files": result["files"], "base_texts": result["base_texts"]}
        return summary

    def file(self, aid: str, path: str) -> dict[str, Any]:
        from fastapi import HTTPException
        hit = self._cache.get(aid)
        if hit is None:
            raise HTTPException(status_code=410, detail="This patch result was evicted: apply it again")
        if path not in hit["files"]:
            raise HTTPException(status_code=404, detail=f"The patches do not change {path}")
        new = hit["files"][path]
        old = hit["base_texts"].get(path)
        hunks = diff_hunks(old, new)
        rows = [r for h in hunks for r in h["lines"]]
        return {"apply_id": aid, "path": path, "created": old is None, "deleted": new is None,
                "hunks": hunks, "binary": False, "truncated": False, "identical": not hunks,
                "additions": sum(1 for r in rows if r[0] == "+"), "deletions": sum(1 for r in rows if r[0] == "-"),
                "text": new if old is None else None}


patch_apply_service = PatchApplyService()
