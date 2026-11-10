"""webapp/backend/services/patch_series.py - Format a multi-file patch series like `git format-patch` (P3, webapp/API.md section 4.6).

The browser sends, per patch, the files it changes with the blob SHA of the original text and the new text;
the originals are read from Git at the base commit (and must still be those blobs), the diffs are built here
with git's conventions (`diff --git`, `index <old>..<new> <mode>`, `\\ No newline at end of file`, new files),
new blob ids are computed like `git hash-object` without writing anything, and the series gets
`[PATCH n/N]` subjects, an optional cover letter with a diffstat and `base-commit:`. The result is checked
by applying it to the base in memory (patch_apply.py).
"""
from __future__ import annotations

import difflib
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException

from webapp.backend.services.patch_apply import apply_series, parse_patch, safe_path

MAX_PATCHES = 50
MAX_PAYLOAD = 5 << 20
ZERO = "0" * 40
_ADDR_RE = re.compile(r"^(?:[^<>@\s\"]+(?:\s+[^<>@\s\"]+)*\s*<[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+>|[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+)$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")


def blob_sha(text: str) -> str:
    """The blob id `git hash-object` gives this text (nothing is written)."""
    data = text.encode("utf-8", errors="surrogateescape")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def valid_address(addr: str) -> bool:
    return bool(_ADDR_RE.match(addr.strip())) and len(addr) <= 320 and "\n" not in addr


def unified(path_a: str | None, path_b: str | None, old: str, new: str, context: int = 3) -> str:
    """Hunks of a unified diff in git's format (`\\ No newline at end of file` included)."""
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    out = [f"--- {'a/' + path_a if path_a else '/dev/null'}\n", f"+++ {'b/' + path_b if path_b else '/dev/null'}\n"]

    def emit(prefix: str, line: str) -> None:
        out.append(prefix + line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")

    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        old_len, new_len = i2 - i1, j2 - j1
        old_start = i1 + 1 if old_len else i1
        new_start = j1 + 1 if new_len else j1
        out.append(f"@@ -{old_start}{'' if old_len == 1 else f',{old_len}'} +{new_start}{'' if new_len == 1 else f',{new_len}'} @@\n")
        for tag, x1, x2, y1, y2 in group:
            if tag == "equal":
                for line in a[x1:x2]:
                    emit(" ", line)
                continue
            for line in a[x1:x2]:
                emit("-", line)
            for line in b[y1:y2]:
                emit("+", line)
    return "".join(out) if len(out) > 2 else ""


def _stat_line(path: str, adds: int, dels: int, width: int, count_width: int) -> str:
    changes = adds + dels
    bar = "+" * adds + "-" * dels
    if changes > 60:
        bar = "+" * round(60 * adds / changes) + "-" * round(60 * dels / changes)
    return f" {path.ljust(width)} | {str(changes).rjust(count_width)} {bar}".rstrip()


def diffstat(files: list[dict[str, Any]]) -> str:
    """`git diff --stat` summary of [{path, adds, dels}]."""
    if not files:
        return ""
    width = max(len(f["path"]) for f in files)
    count_width = max(len(str(f["adds"] + f["dels"])) for f in files)
    lines = [_stat_line(f["path"], f["adds"], f["dels"], width, count_width) for f in files]
    ins = sum(f["adds"] for f in files)
    dels = sum(f["dels"] for f in files)
    parts = [f"{len(files)} file{'s' if len(files) != 1 else ''} changed"]
    if ins:
        parts.append(f"{ins} insertion{'s' if ins != 1 else ''}(+)")
    if dels:
        parts.append(f"{dels} deletion{'s' if dels != 1 else ''}(-)")
    return "\n".join(lines) + "\n " + ", ".join(parts) + "\n"


def slug(subject: str) -> str:
    return _SLUG_RE.sub("-", subject).strip("-").lower()[:52] or "patch"


class PatchSeriesFormatter:
    def __init__(self, read: Callable[[str, str], tuple[str | None, str | None, str]], now: datetime | None = None) -> None:
        """`read(sha, path)` -> (text or None, blob sha or None, mode)."""
        self.read = read
        self.now = now

    def _validate(self, req: dict[str, Any]) -> None:
        patches = req.get("patches")
        if not isinstance(patches, list) or not 0 < len(patches) <= MAX_PATCHES:
            raise HTTPException(status_code=400, detail=f"patches must list 1 to {MAX_PATCHES} patches")
        author = req.get("author") or {}
        if not str(author.get("name") or "").strip() or not valid_address(str(author.get("email") or "")):
            raise HTTPException(status_code=400, detail="author needs a name and a valid email")
        for key in ("to", "cc"):
            for addr in req.get(key) or []:
                if not isinstance(addr, str) or not valid_address(addr):
                    raise HTTPException(status_code=400, detail=f"Invalid address in {key}: {str(addr)[:80]}")
        size = 0
        seen = set()
        for p in patches:
            if not str(p.get("subject") or "").strip():
                raise HTTPException(status_code=400, detail="Every patch needs a subject")
            if not p.get("files"):
                raise HTTPException(status_code=400, detail=f"Patch '{p.get('subject')}' changes no file")
            for f in p["files"]:
                path = str(f.get("path") or "").strip("/")
                if not safe_path(path):
                    raise HTTPException(status_code=400, detail=f"Path outside the tree: {path[:120]}")
                if path in seen:
                    raise HTTPException(status_code=400, detail=f"{path} is in two patches: each file belongs to one patch")
                seen.add(path)
                size += len(str(f.get("modified_content") or ""))
        if size > MAX_PAYLOAD:
            raise HTTPException(status_code=413, detail="At most 5 MB of file content")

    def _header(self, author: dict[str, str], subject: str, date: str, to: list[str], cc: list[str], commit: str) -> str:
        lines = [f"From {commit} Mon Sep 17 00:00:00 2001", f"From: {author['name'].strip()} <{author['email'].strip()}>", f"Date: {date}", f"Subject: {subject}"]
        if to:
            lines.append("To: " + ",\n    ".join(to))
        if cc:
            lines.append("Cc: " + ",\n    ".join(cc))
        return "\n".join(lines) + "\n"

    def format(self, base_sha: str, req: dict[str, Any]) -> dict[str, Any]:
        self._validate(req)
        author = {"name": str(req["author"]["name"]), "email": str(req["author"]["email"])}
        to, cc = list(req.get("to") or []), list(req.get("cc") or [])
        patches = req["patches"]
        total = len(patches)
        cover = req.get("cover") if total > 1 else None
        when = self.now or datetime.now(timezone.utc)
        date = when.strftime("%a, %d %b %Y %H:%M:%S +0000")
        signoff = f"Signed-off-by: {author['name'].strip()} <{author['email'].strip()}>"
        version = int(req.get("version") or 1)
        tag = "PATCH" + (f" v{version}" if version > 1 else "")
        numbered = total > 1
        width = len(str(total))
        out = []
        all_stats: list[dict[str, Any]] = []
        for n, p in enumerate(patches, 1):
            diffs, stats = [], []
            for f in p["files"]:
                path = str(f["path"]).strip("/")
                text, blob, mode = self.read(base_sha, path)
                want = str(f.get("original_blob") or "")
                if text is None and want and want != ZERO:
                    raise HTTPException(status_code=409, detail=f"{path} does not exist at the base commit")
                if text is not None and want != blob:
                    raise HTTPException(status_code=409, detail=f"{path} changed at the base commit (stale original {want[:12]}, base has {str(blob)[:12]}): reload it")
                new = str(f.get("modified_content") or "")
                created = text is None
                body = unified(None if created else path, path, text or "", new)
                if not body:
                    continue
                new_blob = blob_sha(new)
                head = f"diff --git a/{path} b/{path}\n"
                head += f"new file mode 100644\nindex 0000000..{new_blob[:7]}\n" if created else f"index {str(blob)[:7]}..{new_blob[:7]} {mode}\n"
                diffs.append(head + body)
                adds = sum(1 for ln in body.splitlines()[2:] if ln.startswith("+"))
                dels = sum(1 for ln in body.splitlines()[2:] if ln.startswith("-"))
                stats.append({"path": path, "adds": adds, "dels": dels, "created": created})
            if not diffs:
                raise HTTPException(status_code=400, detail=f"Patch '{p['subject']}' changes nothing")
            all_stats.extend(stats)
            subj = _TAG_RE.sub("", str(p["subject"]).strip())
            subject = f"[{tag} {str(n).rjust(width, '0')}/{total}] {subj}" if numbered else f"[{tag}] {subj}"
            msg_body = str(p.get("body") or "").strip()
            if signoff not in msg_body:
                msg_body = (msg_body + "\n\n" if msg_body else "") + signoff
            # mboxrd >From escaping for commit message bodies
            msg_body = re.sub(r"^(>*From )", r">\1", msg_body, flags=re.MULTILINE)
            creates = "".join(f" create mode 100644 {s['path']}\n" for s in stats if s["created"])
            text = (self._header(author, subject, date, to, cc, ZERO) + "\n" + msg_body + "\n---\n" + diffstat(stats) + creates + "\n"
                    + "".join(diffs))
            out.append({"name": f"{str(n).zfill(4)}-{slug(subj)}.patch", "subject": subject, "text": text, "stat": stats})
        base_line = f"\nbase-commit: {base_sha}\n"
        if cover:
            csubj = str(cover.get("subject") or "").strip() or "*** SUBJECT HERE ***"
            shortlog = f"{author['name'].strip()} ({total}):\n" + "".join(f"  {_TAG_RE.sub('', o['subject'])}\n" for o in out)
            merged: dict[str, dict[str, Any]] = {}
            for s in all_stats:
                m = merged.setdefault(s["path"], {"path": s["path"], "adds": 0, "dels": 0})
                m["adds"] += s["adds"]
                m["dels"] += s["dels"]
            c_body = str(cover.get("body") or "").strip() or "*** BLURB HERE ***"
            c_body = re.sub(r"^(>*From )", r">\1", c_body, flags=re.MULTILINE)
            ctext = (self._header(author, f"[{tag} 0/{total}] {csubj}", date, to, cc, ZERO) + "\n"
                     + c_body + "\n\n" + shortlog + "\n"
                     + diffstat(sorted(merged.values(), key=lambda x: x["path"])) + base_line)
            out.insert(0, {"name": "0000-cover-letter.patch", "subject": f"[{tag} 0/{total}] {csubj}", "text": ctext, "stat": []})
        else:
            out[0]["text"] += base_line
        for o in out:
            o["text"] += "-- \n2.43.0\n\n"
        return {"patches": [{"name": o["name"], "subject": o["subject"], "text": o["text"]} for o in out],
                "mbox": "".join(o["text"] for o in out), "diffstat": diffstat(all_stats), "base": base_sha}


def _git_read(sha: str, path: str) -> tuple[str | None, str | None, str]:
    from webapp.backend.config import get_repo_dir
    from webapp.backend.services.git_reader import git_reader
    data, is_dir, blob = git_reader.read_file(sha, path)
    if is_dir or data is None:
        return None, None, "100644"
    mode = "100644"
    try:
        ls = subprocess.run(["git", "-C", str(get_repo_dir()), "ls-tree", sha, "--", path], capture_output=True, text=True, timeout=10).stdout
        if ls:
            mode = ls.split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return data.decode("utf-8", errors="surrogateescape"), blob, mode


def format_series(revision: str, req: dict[str, Any]) -> dict[str, Any]:
    """`POST /api/version/{v}/patch/series`: the formatted series plus whether it applies to its base."""
    from webapp.backend.services.tree_resolver import git_tree_resolver
    if not isinstance(req, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    base = str(req.get("base_commit") or revision)
    sha = git_tree_resolver.resolve_commit_sha(base)
    if not sha:
        raise HTTPException(status_code=404, detail=f"Revision not found: {base}")
    res = PatchSeriesFormatter(_git_read).format(sha, req)

    def read(path: str) -> str | None:
        return _git_read(sha, path)[0]
    check = apply_series(sha, [(p["name"], parse_patch(p["text"])) for p in res["patches"] if not p["name"].startswith("0000-")], read)
    res["check"] = {"applies": all(p["status"] == "applied" for p in check["patches"]),
                    "patches": [{"name": p["label"], "status": p["status"]} for p in check["patches"]]}
    return res
