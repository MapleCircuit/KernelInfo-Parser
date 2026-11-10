"""webapp/backend/security/git.py - Git argument hardening for caller-supplied revisions.

Every endpoint takes a revision as a version name, a branch or a commit SHA, and the services pass
it to git as a positional argument (`git blame <rev> -- <path>`). Git parses an argument starting
with "-" as an option, so an unvalidated revision turns into `--contents=<file>` (git blame prints
that file) or `--output=<file>` (git log writes it). Revisions are therefore validated here before
they reach a command line.
"""
from __future__ import annotations

from fastapi import HTTPException

from core.globalstuff import is_safe_git_rev


def safe_rev(rev: str, what: str = "revision") -> str:
    """Return rev unchanged, or raise HTTP 400 when it is not a plain tag / branch / commit SHA."""
    if not is_safe_git_rev(rev):
        raise HTTPException(status_code=400, detail=f"Invalid {what}: must be a tag, branch or commit SHA.")
    return rev


def is_safe_rev(rev: object) -> bool:
    """Non-raising form of safe_rev(), for services that fall back instead of failing a request."""
    return is_safe_git_rev(rev)


MAX_GREP_PATTERN = 256
MAX_PATHSPECS = 8
MAX_PATHSPEC_LEN = 200
_GLOB_CHARS = set("*?[")


def validate_grep_pattern(pattern: object) -> str:
    """A text-search pattern for `git grep -e` (webapp/API.md section 2.1.3), or HTTP 400.

    1-256 characters without NUL, CR or LF. The pattern always follows `-e`, so a leading "-" is
    read as text; regular expressions are POSIX ERE (`-E`), PCRE (`-P`) is never offered."""
    if not isinstance(pattern, str) or not pattern:
        raise HTTPException(status_code=400, detail="Search pattern is required.")
    if len(pattern) > MAX_GREP_PATTERN:
        raise HTTPException(status_code=400, detail=f"Search pattern longer than {MAX_GREP_PATTERN} characters.")
    if any(c in pattern for c in "\0\r\n"):
        raise HTTPException(status_code=400, detail="Search pattern must be a single line.")
    return pattern


def validate_pathspec(spec: object) -> str:
    """One caller-supplied path filter: relative, no `..` segment, no `:` magic; HTTP 400 otherwise."""
    if not isinstance(spec, str):
        raise HTTPException(status_code=400, detail="Invalid path filter.")
    body = spec[1:] if spec.startswith("!") else spec
    if (not body or len(spec) > MAX_PATHSPEC_LEN or body.startswith(("/", ":", "!"))
            or any(c in body for c in "\0\r\n\\")
            or any(seg == ".." for seg in body.split("/"))):
        raise HTTPException(status_code=400, detail=f"Invalid path filter: {spec[:40]!r}")
    return spec


def validate_pathspecs(specs: object) -> list[str]:
    """Up to 8 path filters (`validate_pathspec`); `!spec` excludes."""
    if specs is None:
        return []
    if isinstance(specs, str):
        specs = [s for s in (p.strip() for p in specs.split(",")) if s]
    specs = list(specs)
    if len(specs) > MAX_PATHSPECS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_PATHSPECS} path filters.")
    return [validate_pathspec(s) for s in specs]


def git_pathspecs(specs: list[str]) -> list[str]:
    """Validated filters as git pathspecs, the magic added on the server: `:(glob)` for includes,
    `:(exclude,glob)` for `!spec`. A wildcard pattern without "/" matches in every directory
    (`*.c` becomes `**/*.c`); a plain path matches the file or everything under the directory."""
    out = []
    for spec in specs:
        exclude = spec.startswith("!")
        body = spec[1:] if exclude else spec
        body = body.rstrip("/") or body
        if "/" not in body and _GLOB_CHARS & set(body):
            body = f"**/{body}"
        out.append(f":({'exclude,' if exclude else ''}glob){body}")
    if out and all(p.startswith(":(exclude") for p in out):
        out.insert(0, ".")  # an exclusion alone needs something to exclude from
    return out


__all__ = [
    "safe_rev", "is_safe_rev", "validate_grep_pattern", "validate_pathspec", "validate_pathspecs",
    "git_pathspecs", "MAX_GREP_PATTERN", "MAX_PATHSPECS",
]
