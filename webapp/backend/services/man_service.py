"""webapp/backend/services/man_service.py - Linux man-pages (webapp/API.md section 2.20).

The man-pages repository (the repositories[] entry whose project is `man-pages`) is read from Git at any
revision; the pure parser `parser/man_ast/` turns pages into the document model and their facts
(`m_blob_facts` family 6). Four answers:

1. `index(v)`: every page file of a revision with its NAME summary and `.so` alias target, from the parser's
   stored facts when every page blob has a row, else extracted from Git (cached per commit).
2. `page(v, ...)`: one page by path or by name (+ section), `.so` aliases followed, parsed into the document
   model (cached per blob), with the matching Linux kernel revision and the syscall / symbols it documents.
3. `resolve(name, section, kernel)`: for a kernel revision of the request's repository, the newest
   man-pages release tag dated on or before it that has the page (else a man7.org URL).
4. `tags()`: the man-pages release tags with their dates, newest first.

A tag's date is the committer date of the commit it points to (annotated tags peeled), never the tag's own
creation date: retroactive tags (`man-pages-1.70` was tagged in 2014 on a 2004 commit) date from their release.

Requests name the man-pages repository (`X-KI-Repo` / `?repo=`); another configured repository (the man-pages
repository from a kernel request, the kernel from a man-pages request) is addressed by switching the request's
repository context for the call (`using_repo`), so Git readers, tree listings and RepoScopedCache keys follow it.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from core.globalstuff import is_safe_git_spec
from parser.git_ast.release_order import KERNEL_FAMILY, parse_tag, release_key
from parser.man_ast import man_facts, man_parser
from parser.man_ast.man_paths import (
    SECTION_ORDER,
    page_of_path,
    resolve_so,
    section_rank,
)
from webapp.backend.config import (
    RepoScopedCache,
    get_active_repository,
    get_repo_dir,
    is_manpages_entry,
    list_repositories,
    reset_active_repository,
    resolve_repo_path,
    set_active_repository,
)
from webapp.backend.security.git import is_safe_rev, safe_rev
from webapp.backend.security.jail import is_safe_rel_path
from webapp.backend.services.sync_service import register_refs_changed
from webapp.backend.services.tree_resolver import git_tree_resolver

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9_.:+\-]{1,128}$")
SECTION_RE = re.compile(r"^[0-9][a-z]{0,12}$")
SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
MAN7_URL = "https://man7.org/linux/man-pages/man{s}/{n}.{s}.html"
MAN7_INDEX_URL = "https://man7.org/linux/man-pages/dir_all_alphabetic.html"
MANPAGES_TAG_PREFIX = "man-pages-"
MAX_SO_HOPS = 5
MAX_SYMBOLS = 64
# Per repository: {tag: (committer date of the tagged commit, commit SHA)} until the refs change
_TAG_DATES: RepoScopedCache = RepoScopedCache()
# Per repository and commit: page files ({path: blob}) and {name: {section: path}}
_PAGE_MAPS: RepoScopedCache = RepoScopedCache(maxsize=64)
# Per repository and commit: the index body
_INDEXES: RepoScopedCache = RepoScopedCache(maxsize=32)
# Per blob: facts (man_facts.extract) and document model (man_parser.parse)
_FACTS: RepoScopedCache = RepoScopedCache(maxsize=16384)
_DOCS: RepoScopedCache = RepoScopedCache(maxsize=256)
# Per kernel repository: (man-pages repository, name, section, kernel commit) -> resolve answer
_RESOLVED: RepoScopedCache = RepoScopedCache(maxsize=1024)
_INDEX_LOCK = threading.Lock()


def clear_caches() -> None:
    """Drop what depends on refs (tag lists and dates, resolve answers); commit- and blob-keyed caches stay."""
    _TAG_DATES.clear()
    _RESOLVED.clear()


register_refs_changed(clear_caches)


# --------------------------------------------------------------------------- repositories


def _available(entry: dict[str, Any]) -> bool:
    try:
        return resolve_repo_path(entry).exists()
    except Exception:  # noqa: BLE001
        return False


def manpages_repository() -> dict[str, Any] | None:
    """The request's repository when it is the man-pages, else the first available configured man-pages repository."""
    active = get_active_repository()
    if is_manpages_entry(active):
        return active
    return next((r for r in list_repositories() if is_manpages_entry(r) and r.get("name") and _available(r)), None)


def linux_repository(preferred: str | None = None) -> dict[str, Any] | None:
    """A kernel repository: `preferred` when it names one, else the configured entry of project `linux`
    (an available one first), else the first available repository that is not the man-pages."""
    repos = [r for r in list_repositories() if r.get("name") and not is_manpages_entry(r)]
    if preferred:
        hit = next((r for r in repos if r.get("name") == preferred), None)
        if hit is not None:
            return hit
    linux = [r for r in repos if str(r.get("project") or "linux").strip() in ("linux", "")]
    for group in (linux, repos):
        hit = next((r for r in group if _available(r)), None)
        if hit is not None:
            return hit
    return None


@contextmanager
def using_repo(name: str) -> Iterator[None]:
    """Run the block with `name` as the request's repository (Git readers, tree listings, scoped caches)."""
    token = set_active_repository(name)
    if token is None:
        raise HTTPException(status_code=404, detail=f"Repository '{name}' is not configured.")
    try:
        yield
    finally:
        reset_active_repository(token)


def _man_repo_or_404() -> dict[str, Any]:
    entry = manpages_repository()
    if entry is None:
        raise HTTPException(status_code=404, detail="No man-pages repository is configured.")
    return entry


# --------------------------------------------------------------------------- git


def _git(args: list[str], timeout: float = 30.0, data: bytes | None = None) -> bytes | None:
    try:
        out = subprocess.run(["git", "-C", str(get_repo_dir()), *args], input=data, capture_output=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git %s failed: %s", args[:1], exc)
        return None
    return out.stdout if out.returncode == 0 else None


def tag_dates() -> dict[str, tuple[int, str]]:
    """Commit-pointing tags of the request's repository -> (committer date of the tagged commit, peeled commit
    SHA). Cached until the refs change. An annotated tag's own (tagger) date is not used: a tag dates from the
    commit it names."""
    hit = _TAG_DATES.get("tags")
    if hit is not None:
        return hit
    # %(*committerdate) is the peeled commit's date of an annotated tag, %(committerdate) a lightweight tag's
    fmt = ("--format=%(refname:short)%09%(*committerdate:unix)%(committerdate:unix)"
           "%09%(objecttype)%09%(objectname)%09%(*objecttype)%09%(*objectname)")
    out = _git(["for-each-ref", fmt, "refs/tags"])
    tags: dict[str, tuple[int, str]] = {}
    for line in (out or b"").decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 6 or not parts[1].isdigit():
            continue
        name, committed, otype, obj, ptype, peeled = parts[:6]
        kind, sha = (ptype, peeled) if peeled else (otype, obj)
        if kind != "commit" or not _FULL_SHA.match(sha):
            continue
        tags[name] = (int(committed), sha)
    if out is not None:
        _TAG_DATES["tags"] = tags
    return tags


def rev_date(rev: str, sha: str) -> int | None:
    """Date of a revision: the committer date of its commit (`tag_dates()` for a tag)."""
    hit = tag_dates().get(rev)
    if hit is not None:
        return hit[0]
    if not _FULL_SHA.match(sha or ""):
        return None
    out = _git(["show", "-s", "--format=%ct", sha], timeout=10)
    text = (out or b"").decode("ascii", errors="replace").strip()
    return int(text) if text.isdigit() else None


def _read_blobs(shas: list[str]) -> dict[str, str]:
    """Text of blobs (validated full SHAs) with one `git cat-file --batch`."""
    wanted = [s for s in dict.fromkeys(shas) if _FULL_SHA.match(s) and is_safe_git_spec(s)]
    if not wanted:
        return {}
    raw = _git(["cat-file", "--batch"], timeout=120, data="".join(f"{s}\n" for s in wanted).encode("ascii"))
    texts: dict[str, str] = {}
    pos = 0
    while raw and pos < len(raw):
        nl = raw.find(b"\n", pos)
        if nl < 0:
            break
        header = raw[pos:nl].split()
        if len(header) != 3 or not header[2].isdigit():
            pos = nl + 1  # "<sha> missing"
            continue
        size = int(header[2])
        texts[header[0].decode("ascii")] = raw[nl + 1:nl + 1 + size].decode("utf-8", errors="replace")
        pos = nl + 1 + size + 1
    return texts


def _commit_or_404(v: str) -> str:
    safe_rev(v, "version")
    sha = git_tree_resolver.resolve_commit_sha(v)
    if sha is None:
        raise HTTPException(status_code=404, detail=f"Revision '{v}' is not available in Git.")
    return sha


# --------------------------------------------------------------------------- page files


def page_map(sha: str) -> dict[str, Any]:
    """Page files of a commit: {"paths": {path: blob}, "by_name": {name: {section: path}}}.

    A page present under both `man/manS/` and `manS/` is listed once, at its `man/` path. Root `manS`
    symlinks of the new layout are blobs in the tree listing, so their targets are never listed twice."""
    hit = _PAGE_MAPS.get(sha)
    if hit is not None:
        return hit
    tree = git_tree_resolver.list_tree(sha) or {}
    by_key: dict[tuple[str, str], str] = {}
    for path in tree:
        key = page_of_path(path)
        if key is None:
            continue
        current = by_key.get(key)
        if current is None or (path.startswith("man/") and not current.startswith("man/")):
            by_key[key] = path
    paths = {path: tree[path] for path in by_key.values()}
    by_name: dict[str, dict[str, str]] = {}
    for (name, section), path in by_key.items():
        by_name.setdefault(name, {})[section] = path
    result = {"paths": paths, "by_name": by_name}
    _PAGE_MAPS[sha] = result
    return result


def canonical_path(path: str, pm: dict[str, Any]) -> str | None:
    """The listed path of a page path (either layout), or None."""
    if path in pm["paths"]:
        return path
    key = page_of_path(path)
    if key is None:
        return None
    return pm["by_name"].get(key[0], {}).get(key[1])


def section_candidates(section: str | None, available: set[str] | list[str]) -> list[str]:
    """Sections to try, in order: without a section `SECTION_ORDER` then the others; with one, it first, then
    its siblings sharing the base number (`2` -> `2type`, `2const`; `3type` -> `3`)."""
    avail = set(available)
    if not section:
        return sorted(avail, key=lambda s: (section_rank(s), s))
    base = re.match(r"\d+", section)
    siblings = sorted((s for s in avail if s != section and base and re.match(r"\d+", s).group(0) == base.group(0)),
                      key=lambda s: (section_rank(s), s))
    return ([section] if section in avail else []) + siblings


def find_page(pm: dict[str, Any], name: str, section: str | None) -> str | None:
    """Path of name(section) in a page map (section optional); names match exactly, then case-insensitively."""
    sections = pm["by_name"].get(name)
    if sections is None:
        lower = name.lower()
        key = next((n for n in pm["by_name"] if n.lower() == lower), None)
        sections = pm["by_name"].get(key) if key is not None else None
    if not sections:
        return None
    order = section_candidates(section, list(sections))
    return sections[order[0]] if order else None


def _facts_cached(blob: str, text: str | None = None) -> dict[str, Any] | None:
    hit = _FACTS.get(blob)
    if hit is not None:
        return hit or None
    if text is None:
        text = _read_blobs([blob]).get(blob, "")
    facts = man_facts.extract(text)
    _FACTS[blob] = facts or {}
    return facts


def _facts_of(blobs: list[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """Facts of page blobs and whether the parser's stored rows (family 6) cover all of them."""
    from webapp.backend.services.source_index import db_facts, db_has

    wanted = sorted(set(blobs))
    stored = db_facts(wanted, man_facts.FAMILY, man_facts.FORMAT, man_facts.decode) if wanted else {}
    rest = [b for b in wanted if b not in stored]
    covered = not rest or not (set(rest) - db_has(rest, man_facts.FAMILY, man_facts.FORMAT))
    facts: dict[str, dict[str, Any]] = dict(stored)
    unread = []
    for blob in rest:
        hit = _FACTS.get(blob)
        if hit is None:
            unread.append(blob)
        elif hit:
            facts[blob] = hit
    texts = _read_blobs(unread)
    for blob in unread:
        f = man_facts.extract(texts.get(blob, ""))
        _FACTS[blob] = f or {}
        if f:
            facts[blob] = f
    return facts, covered


def _follow_so(path: str, so: str, pm: dict[str, Any], facts: dict[str, dict[str, Any]]) -> str:
    """Final target of a `.so` alias chain through the facts (loops and long chains stop at the last hop)."""
    seen = {path}
    target = resolve_so(so, path)
    for _ in range(MAX_SO_HOPS):
        found = canonical_path(target, pm)
        if found is None or found in seen:
            return found or target
        seen.add(found)
        nxt = (facts.get(pm["paths"][found]) or {}).get("so")
        if not nxt:
            return found
        target = resolve_so(nxt, found)
    return canonical_path(target, pm) or target


# --------------------------------------------------------------------------- endpoints


def index(v: str) -> dict[str, Any]:
    """Every page file of revision `v` of the man-pages repository (sorted by section order, then name)."""
    entry = _man_repo_or_404()
    with using_repo(entry["name"]):
        sha = _commit_or_404(v)
        hit = _INDEXES.get(sha)
        if hit is None:
            with _INDEX_LOCK:
                hit = _INDEXES.get(sha)
                if hit is None:
                    hit = _build_index(sha)
                    _INDEXES[sha] = hit
        return {"version": v, "repo": entry["name"], **hit}


def _build_index(sha: str) -> dict[str, Any]:
    pm = page_map(sha)
    facts, covered = _facts_of(list(pm["paths"].values()))
    pages = []
    for path, blob in pm["paths"].items():
        name, section = page_of_path(path)  # type: ignore[misc] - every listed path is a page
        f = facts.get(blob) or {}
        so = _follow_so(path, f["so"], pm, facts) if f.get("so") else None
        summary = f.get("s") or ""
        if not summary and so and so in pm["paths"]:
            summary = (facts.get(pm["paths"][so]) or {}).get("s") or ""
        pages.append({"name": name, "section": section, "path": path, "summary": summary, "so": so})
    pages.sort(key=lambda p: (section_rank(p["section"]), p["section"], p["name"].lower(), p["name"]))
    return {"commit": sha, "source": "db" if covered else "git", "total": len(pages), "pages": pages}


def _validate_page_args(path: str | None, name: str | None, section: str | None) -> None:
    if path:
        if not is_safe_rel_path(path) or page_of_path(path) is None or not NAME_RE.match(path.rsplit("/", 1)[-1]):
            raise HTTPException(status_code=400, detail="Invalid path: must be a man page file (manS/name.S).")
        return
    if not name or not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid or missing page name.")
    if section and not SECTION_RE.match(section):
        raise HTTPException(status_code=400, detail="Invalid section.")


def symbols_of(facts: dict[str, Any] | None) -> list[str]:
    """C identifiers a page's SYNOPSIS declares (functions, then types / constants), keywords left out."""
    out: list[str] = []
    for ident in [*(facts or {}).get("fn", []), *(facts or {}).get("id", [])]:
        if isinstance(ident, str) and SYMBOL_RE.match(ident) and ident not in man_facts.C_KEYWORDS and ident not in out:
            out.append(ident)
            if len(out) >= MAX_SYMBOLS:
                break
    return out


def _release_tag_at(date: int | None) -> str | None:
    """Newest kernel release tag (`vX.Y`, release candidates, stable updates and next tags left out) whose commit
    is dated on or before `date` in the request's repository; the oldest one when all are newer; the newest
    without a date."""
    finals = []
    for tag, (when, _sha) in tag_dates().items():
        p = parse_tag(tag)
        if p is not None and p["final"] and p["kind"] == "mainline" and p["family"] == KERNEL_FAMILY:
            finals.append((when, release_key(tag), tag))
    if not finals:
        return None
    if date is None:
        return max(finals)[2]
    before = [f for f in finals if f[0] <= date]
    return max(before)[2] if before else min(finals)[2]


def _linux_block(name: str, section: str, facts: dict[str, Any] | None, man_date: int | None,
                 kernel: str | None, kernel_repo: str | None) -> dict[str, Any]:
    block: dict[str, Any] = {"repo": None, "version": None, "syscall": name if section == "2" else None,
                             "symbols": symbols_of(facts)}
    entry = linux_repository(kernel_repo)
    if entry is None or not _available(entry):
        return block
    block["repo"] = entry["name"]
    with using_repo(entry["name"]):
        if kernel and is_safe_rev(kernel) and git_tree_resolver.resolve_commit_sha(kernel):
            block["version"] = kernel
        else:
            block["version"] = _release_tag_at(man_date)
    return block


def page(v: str, path: str | None = None, name: str | None = None, section: str | None = None,
         kernel: str | None = None, kernel_repo: str | None = None) -> dict[str, Any]:
    """One page at revision `v` (by path, or by name with an optional section), `.so` aliases followed."""
    _validate_page_args(path, name, section)
    entry = _man_repo_or_404()
    with using_repo(entry["name"]):
        sha = _commit_or_404(v)
        pm = page_map(sha)
        page_path = canonical_path(path, pm) if path else find_page(pm, name or "", section or None)
        if page_path is None:
            what = path or (f"{name}({section})" if section else name)
            raise HTTPException(status_code=404, detail=f"Man page '{what}' does not exist at {v}.")
        page_name, page_section = page_of_path(page_path)  # type: ignore[misc]
        resolved, blob, text = page_path, pm["paths"][page_path], None
        seen = {page_path}
        for _ in range(MAX_SO_HOPS + 1):
            text = _read_blobs([blob]).get(blob)
            if text is None:
                raise HTTPException(status_code=404, detail=f"Man page '{resolved}' is not readable at {v}.")
            target = man_parser.so_target(text)
            nxt = canonical_path(resolve_so(target, resolved), pm) if target else None
            if nxt is None or nxt in seen:
                break
            seen.add(nxt)
            resolved, blob = nxt, pm["paths"][nxt]
        doc = _DOCS.get(blob)
        if doc is None:
            doc = man_parser.parse(text or "")
            _DOCS[blob] = doc
        facts = _facts_cached(blob, text) or {}
        man_date = rev_date(v, sha)
        doc = release_header(doc, v, tag_dates().get(v, (None,))[0])
    return {
        "version": v,
        "repo": entry["name"],
        "path": page_path,
        "resolved_path": resolved,
        "name": page_name,
        "section": page_section,
        "aliases": list(facts.get("n") or [page_name]),
        "facts": facts,
        "doc": doc,
        "date": man_date,
        "linux": _linux_block(page_name, page_section, facts, man_date, kernel, kernel_repo),
    }


def release_header(doc: dict[str, Any], v: str, tag_date: int | None) -> dict[str, Any]:
    """`doc` with the unreleased `.TH` placeholders of the Git tree (date `(date)`, source `Linux man-pages
    (unreleased)`) replaced by the release's date (its commit's committer date, UTC) and `Linux man-pages X.Y` when `v` is a
    `man-pages-X.Y` tag; a copy, the cached document stays as parsed."""
    th = doc.get("th") or {}
    version = v[len(MANPAGES_TAG_PREFIX):] if v.startswith(MANPAGES_TAG_PREFIX) else ""
    if not version or tag_date is None:
        return doc
    fixed = dict(th)
    if str(th.get("date", "")).strip() == "(date)":
        fixed["date"] = datetime.fromtimestamp(tag_date, tz=UTC).strftime("%Y-%m-%d")
    if "(unreleased)" in str(th.get("source", "")):
        fixed["source"] = f"Linux man-pages {version}"
    return doc if fixed == th else {**doc, "th": fixed}


def release_tags() -> list[dict[str, Any]]:
    """`man-pages-*` tags of the request's repository: [{name, date, commit}] newest first (release order on ties);
    `date` is the committer date of the tagged commit."""
    rows = [(when, release_key(tag), tag, sha) for tag, (when, sha) in tag_dates().items()
            if tag.startswith(MANPAGES_TAG_PREFIX)]
    rows.sort(reverse=True)
    return [{"name": tag, "date": when, "commit": sha} for when, _key, tag, sha in rows]


def tags() -> dict[str, Any]:
    entry = _man_repo_or_404()
    with using_repo(entry["name"]):
        return {"repo": entry["name"], "tags": release_tags()}


def man7_url(name: str, section: str | None) -> str:
    return MAN7_URL.format(n=name, s=section) if section else MAN7_INDEX_URL


def _default_kernel() -> str:
    """The request's repository's newest kernel release tag, else HEAD."""
    return _release_tag_at(None) or "HEAD"


def resolve(name: str, section: str | None = None, kernel: str | None = None) -> dict[str, Any]:
    """Our copy of name(section) for kernel revision `kernel` of the request's repository (default its newest
    release): the newest man-pages release tag whose commit is dated (committer date) on or before the revision's
    commit, else the oldest one; when the
    page is missing there, older tags first, then newer ones. Without a usable man-pages repository or page,
    `available` is false and `url` names the page on man7.org."""
    if not name or not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid or missing page name.")
    if section and not SECTION_RE.match(section):
        raise HTTPException(status_code=400, detail="Invalid section.")
    if kernel:
        safe_rev(kernel, "kernel revision")
    url = man7_url(name, section)
    unavailable = {"available": False, "url": url, "name": name, "section": section or None}
    kernel = kernel or _default_kernel()
    ksha = git_tree_resolver.resolve_commit_sha(kernel)
    mentry = manpages_repository()
    if ksha is None or mentry is None or not _available(mentry):
        return unavailable
    key = (mentry["name"], name, section or "", ksha)
    hit = _RESOLVED.get(key)
    if hit is None:
        kdate = rev_date(kernel, ksha)
        with using_repo(mentry["name"]):
            found = _find_in_tags(name, section or None, kdate)
        hit = {"found": found, "kernel_date": kdate}
        _RESOLVED[key] = hit
    found = hit["found"]
    if found is None:
        return {**unavailable, "kernel": kernel, "kernel_date": hit["kernel_date"]}
    tag, tag_date, path = found
    page_name, page_section = page_of_path(path)  # type: ignore[misc]
    return {"available": True, "repo": mentry["name"], "version": tag, "path": path, "name": page_name,
            "section": page_section, "kernel": kernel, "kernel_date": hit["kernel_date"], "tag_date": tag_date,
            "url": man7_url(page_name, page_section)}


def _find_in_tags(name: str, section: str | None, date: int | None) -> tuple[str, int, str] | None:
    """(tag, tag date, path) of the first release tag, in preference order around `date`, that has the page.

    One `git cat-file --batch-check` answers every candidate path at every tag."""
    candidates = []
    for tag, (when, sha) in tag_dates().items():
        p = parse_tag(tag) if tag.startswith(MANPAGES_TAG_PREFIX) else None
        if p is not None and not p["pre"]:
            candidates.append((when, release_key(tag), tag, sha))
    if not candidates:
        return None
    candidates.sort()
    if date is None:
        pick = len(candidates) - 1
    else:
        before = [i for i, c in enumerate(candidates) if c[0] <= date]
        pick = before[-1] if before else 0
    order = [*range(pick, -1, -1), *range(pick + 1, len(candidates))]
    sections = [section] + [s for s in SECTION_ORDER if s != section and s.startswith(section[:1])] if section \
        else list(SECTION_ORDER)
    paths = []
    for sec in sections:
        paths.extend([f"man/man{sec}/{name}.{sec}", f"man{sec}/{name}.{sec}"])
    specs = [f"{candidates[i][3]}:{p}" for i in order for p in paths]
    specs = [s for s in specs if is_safe_git_spec(s)]
    out = _git(["cat-file", "--batch-check=%(objecttype)"], timeout=60, data="".join(f"{s}\n" for s in specs).encode())
    if out is None:
        return None
    answers = out.decode("utf-8", errors="replace").splitlines()
    present = {spec for spec, ans in zip(specs, answers) if ans.strip() == "blob"}
    for i in order:
        when, _key, tag, sha = candidates[i]
        for p in paths:
            if f"{sha}:{p}" in present:
                return tag, when, p
    return None
