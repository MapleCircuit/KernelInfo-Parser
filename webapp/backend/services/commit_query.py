"""webapp/backend/services/commit_query.py - Structured commit queries over Git (H8, webapp/API.md section 4.6).

`path:fs/ext4 reviewer:tytso since:2011-01-01 in:v3.0 merges:none journal` becomes one `git log` whose
every argument is built here from validated values:
- paths: the pathspec validator of the text search (section 2.1.3), glob magic added on the server;
- names (author, committer, trailer roles, free text): length-capped and escaped for POSIX ERE, so they
  match as text; the patterns all follow `--author=` / `--committer=` / `--grep=` (never positional);
- dates: ISO `YYYY-MM-DD` only;
- revisions: `safe_rev` names resolved to commit SHAs; ranges are joined on the server (`<sha> ^<sha>`).
"""
from __future__ import annotations

import datetime as _dt
import re
import subprocess
from typing import Any

from fastapi import HTTPException

from webapp.backend.config import get_repo_dir
from webapp.backend.security.git import git_pathspecs, is_safe_rev, safe_rev, validate_pathspecs

MAX_TEXT = 100
MAX_OFFSET = 1_000_000
QUERY_TIMEOUT = 45.0
ROLE_TRAILERS = {
    "reviewer": "Reviewed-by", "acked": "Acked-by", "tested": "Tested-by", "reported": "Reported-by",
    "suggested": "Suggested-by", "signed": "Signed-off-by", "codev": "Co-developed-by", "cc": "Cc",
    "fixes": "Fixes", "link": "Link",
}
_ERE_SPECIAL = re.compile(r"([.\[\]()*+?{}|^$\\])")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def ere_escape(text: str) -> str:
    """`a.b(c)` -> `a\\.b\\(c\\)`: the text matched literally by an extended regular expression."""
    return _ERE_SPECIAL.sub(r"\\\1", text)


def clean_text(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_TEXT or any(c in text for c in "\0\r\n"):
        raise HTTPException(status_code=400, detail=f"Invalid {what}: at most {MAX_TEXT} characters on one line.")
    return text


def clean_date(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _DATE.match(text):
        raise HTTPException(status_code=400, detail=f"Invalid {what}: expected YYYY-MM-DD.")
    try:
        _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {what}: {exc}") from exc
    return text


def _resolve(rev: str, what: str) -> str:
    safe_rev(rev, what)
    try:
        out = subprocess.run(["git", "-C", str(get_repo_dir()), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    if not _FULL_SHA.match(out):
        raise HTTPException(status_code=404, detail=f"Unknown {what}: {rev}")
    return out


def parse_roles(roles: list[str] | None) -> list[tuple[str, str]]:
    """["reviewer:tytso", ...] -> [("Reviewed-by", "tytso")]; unknown roles are a 400."""
    out = []
    for item in roles or []:
        key, sep, value = str(item).partition(":")
        key = key.strip().lower()
        if not sep or key not in ROLE_TRAILERS:
            raise HTTPException(status_code=400, detail=f"Unknown trailer role: {key or item!r}")
        value = clean_text(value, f"{key} value")
        if value:
            out.append((ROLE_TRAILERS[key], value))
    return out[:8]


def section_pathspecs(version_name: str, section: str) -> list[str]:
    """F: (include) and X: (exclude) patterns of a MAINTAINERS section as validated path filters."""
    from parser.maintainer_ast.maintainer_types import PatternType
    from webapp.backend.services.maintainer_service import MaintainerService
    sections, _matcher = MaintainerService().get_maintainer_data(version_name)
    sec = str(section).strip()
    match = next((s for s in sections if str(s.sec_id) == sec or s.name == sec), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"Unknown MAINTAINERS section: {section}")
    specs = []
    for p in match.patterns:
        pat = p.pattern.strip()
        if not pat or pat.startswith("/") or ".." in pat.split("/"):
            continue
        if p.pat_type == PatternType.FILE:
            specs.append(pat)
        elif p.pat_type == PatternType.EXCLUDE:
            specs.append(f"!{pat}")
    includes = [s for s in specs if not s.startswith("!")]
    if not includes:
        raise HTTPException(status_code=400, detail=f"Section {match.name} has no F: pattern to filter by.")
    return specs[:64]


def build_log_args(*, start: list[str], paths: list[str], author: str = "", committer: str = "",
                   roles: list[tuple[str, str]] = (), since: str = "", until: str = "", merges: str = "",
                   text: str = "", skip: int = 0, limit: int = 50) -> list[str]:
    """Arguments of the `git log` running a query (values already validated): pure, unit tested."""
    args = ["log", "--no-color", "-E", "-i", "--format=%x1e%H%x1f%an%x1f%ae%x1f%at%x1f%s%x1f%P"]
    if merges == "only":
        args.append("--merges")
    elif merges == "none":
        args.append("--no-merges")
    if author:
        args.append(f"--author={ere_escape(author)}")
    if committer:
        args.append(f"--committer={ere_escape(committer)}")
    greps = [f"--grep=^{ere_escape(trailer)}:.*{ere_escape(value)}" for trailer, value in roles]
    if text:
        greps.append(f"--grep={ere_escape(text)}")
    args.extend(greps)
    if len(greps) > 1:
        args.append("--all-match")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until} 23:59:59")
    args += [f"--skip={max(0, min(int(skip), MAX_OFFSET))}", f"-n{int(limit) + 1}"]
    args += start
    args.append("--")
    args += git_pathspecs(paths) if paths else []
    return args


def query_commits(version_name: str, *, paths: list[str] | None = None, author: str = "", committer: str = "",
                  roles: list[str] | None = None, since: str = "", until: str = "", range_: str = "",
                  release: str = "", merges: str = "", section: str = "", text: str = "",
                  page: int = 1, limit: int = 50) -> dict[str, Any]:
    """Commits matching a structured query, newest first: the shape of GET /commits/{v}/list plus has_more."""
    limit = max(1, min(int(limit), 200))
    page = max(1, int(page))
    specs = validate_pathspecs(paths or [])
    if section:
        specs = specs + section_pathspecs(version_name, section)
    author = clean_text(author, "author")
    committer = clean_text(committer, "committer")
    text = clean_text(text, "text")
    role_list = parse_roles(roles)
    since = clean_date(since, "since")
    until = clean_date(until, "until")
    if merges not in ("", "only", "none", "all"):
        raise HTTPException(status_code=400, detail="merges must be only, none or all.")
    range_desc = ""
    if range_:
        a, sep, b = str(range_).partition("..")
        if not sep or not a or not b or not is_safe_rev(a) or not is_safe_rev(b.lstrip(".")):
            raise HTTPException(status_code=400, detail="range must be <rev>..<rev>.")
        start = [_resolve(b.lstrip("."), "range end"), f"^{_resolve(a, 'range start')}"]
        range_desc = f"{a}..{b.lstrip('.')}"
    elif release:
        from webapp.backend.services.release_service import range_of
        safe_rev(release, "release")
        prev, sha = range_of(release)
        start = [sha] + ([f"^{prev}"] if prev else [])
        range_desc = f"{release} cycle"
    else:
        start = [_resolve(version_name, "revision")]
    args = build_log_args(start=start, paths=specs, author=author, committer=committer, roles=role_list,
                          since=since, until=until, merges="" if merges == "all" else merges, text=text,
                          skip=(page - 1) * limit, limit=limit)
    try:
        proc = subprocess.run(["git", "-C", str(get_repo_dir()), *args], capture_output=True, text=True,
                              timeout=QUERY_TIMEOUT, errors="replace")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="The commit query took too long: narrow it with a path or a release.") from None
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"git unavailable: {exc}") from exc
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=(proc.stderr.strip().splitlines() or ["git log failed"])[-1][:300])
    rows = []
    for rec in proc.stdout.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 6 or not _FULL_SHA.match(parts[0]):
            continue
        rows.append({
            "commit_hash": parts[0], "author_name": parts[1], "author_email": parts[2],
            "author_date": int(parts[3]) if parts[3].isdigit() else 0, "subject": parts[4],
            "is_merge": len(parts[5].split()) > 1,
        })
    has_more = len(rows) > limit
    rows = rows[:limit]
    from webapp.backend.services.commit_facts import commit_ids
    ids = commit_ids(r["commit_hash"] for r in rows)
    for r in rows:
        if r["commit_hash"] in ids:
            r["commit_id"] = ids[r["commit_hash"]]
    return {
        "version": version_name, "page": page, "limit": limit, "total": None, "has_more": has_more,
        "range": range_desc, "commits": rows,
    }
