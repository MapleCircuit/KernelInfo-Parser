"""parser/git_ast/insights_deltas.py - Pure single-pass release delta extractor & parallel pre-parser.

Computes disjoint per-release-tag delta metrics along each release series track:
- `insights_subsystem_version_delta`: subsystem activity, diffstat, author counts, and review/test/Fixes/Cc:stable counts
- `insights_subsystem_author_delta`: per-author commit counts and latest activity timestamps per subsystem
- `insights_stable_version_delta`: stable-update backports, CVE links, Fixes classification, and fix latency

All extraction is pure (Git CLI + in-memory parsing, no DB inside worker processes). Parent writes
idempotent upserts (`on_conflict="update"`) in release order inside `with db.transaction():`.
"""
from __future__ import annotations

import contextlib
import logging
import multiprocessing
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from parser.git_ast.commit_relations import parse_relations, resolve_prefixes
from parser.git_ast.git_commit_parser import iter_trailers
from parser.git_ast.git_types import CommitRole
from parser.git_ast.identity import author_identity_id, identity_key
from parser.git_ast.release_order import (
    parse_tag,
    release_key,
    release_ranges,
    release_tag_names,
    series_name,
    series_ord_for,
    series_track_for,
)
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher
from parser.maintainer_ast.maintainer_parser import MaintainerParser

logger = logging.getLogger(__name__)

_RECORD = "\x1e"
_FIELD = "\x1f"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX_PREFIX = re.compile(r"^[0-9a-f]{7,40}$")
INSERT_CHUNK = 5000

SUBSYSTEM_DELTA_COLUMNS: tuple[str, ...] = (
    "version_tag",
    "subsystem_name",
    "parent_tag",
    "series_track",
    "series_ord",
    "release_ts",
    "maintainer_status",
    "commit_count",
    "merge_commit_count",
    "lines_added",
    "lines_removed",
    "files_touched_count",
    "distinct_author_count",
    "fixes_commit_count",
    "stable_cc_count",
    "reviewed_commit_count",
    "tested_commit_count",
)

AUTHOR_DELTA_COLUMNS: tuple[str, ...] = (
    "version_tag",
    "subsystem_name",
    "author_identity_id",
    "series_track",
    "series_ord",
    "author_key",
    "commit_count",
    "last_active_ts",
)

STABLE_DELTA_COLUMNS: tuple[str, ...] = (
    "version_tag",
    "subsystem_name",
    "parent_tag",
    "base_mainline_tag",
    "series_track",
    "series_ord",
    "release_ts",
    "total_commits",
    "backport_commits",
    "fixes_tag_commits",
    "cc_stable_commits",
    "cve_linked_commits",
    "lines_added",
    "lines_removed",
    "same_cycle_fixes_count",
    "cross_release_fixes_count",
    "sum_fix_latency_days",
    "resolved_fixes_count",
)

# Shared read-only worker context inherited copy-on-write across forked workers.
_WORKER_CTX: dict[str, Any] = {}


def _git(repo: Path, args: list[str], timeout: float = 900.0) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def _release_tags(repo: Path) -> dict[str, str]:
    """Map release tag name -> peeled commit SHA for `repo`."""
    out = _git(
        repo,
        [
            "for-each-ref",
            "--format=%(refname:short)%09%(objectname)%09%(objecttype)%09%(*objectname)%09%(*objecttype)",
            "refs/tags",
        ],
        timeout=60.0,
    )
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


def _tag_commit_timestamp(repo: Path, sha: str) -> int:
    with contextlib.suppress(Exception):
        out = _git(repo, ["log", "-1", "--format=%ct", sha], timeout=30.0).strip()
        if out.isdigit():
            return int(out)
    return 0


def _load_maintainers(repo: Path, rev: str, maintainers_file: str) -> tuple[list[Any], Any]:
    if not maintainers_file:
        return [], MaintainerMatcher([])
    sections: list[Any] = []
    with contextlib.suppress(Exception):
        text = _git(repo, ["show", f"{rev}:{maintainers_file}"], timeout=30.0)
        if text.strip():
            sections = MaintainerParser(text).parse()
    return sections, MaintainerMatcher(sections)


def _match_file_sections(
    matcher: Any,
    fpath: str,
    catchall_section: str,
    cache: dict[str, list[str]],
) -> list[str]:
    cached = cache.get(fpath)
    if cached is not None:
        return cached
    if matcher is None:
        cache[fpath] = []
        return []
    matched = [
        s.name
        for s in matcher.match_file(fpath)
        if getattr(s, "name", "") and (not catchall_section or s.name != catchall_section)
    ]
    # Preserve order while deduplicating section names within one file.
    deduped = list(dict.fromkeys(matched))
    cache[fpath] = deduped
    return deduped


def _resolve_target_timestamps(repo: Path, shas: Iterable[str]) -> dict[str, int]:
    unique = [s for s in dict.fromkeys(shas) if _FULL_SHA.match(s)]
    out_map: dict[str, int] = {}
    for i in range(0, len(unique), 500):
        chunk = unique[i : i + 500]
        with contextlib.suppress(Exception):
            out = _git(repo, ["log", "--no-walk=unsorted", "--format=%H %ct", *chunk], timeout=60.0)
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and _FULL_SHA.match(parts[0]) and parts[1].isdigit():
                    out_map[parts[0]] = int(parts[1])
    return out_map


def _parse_numstat_commit_pass(
    repo: Path,
    span: list[str],
    matcher: Any,
    catchall_section: str,
    file_sec_cache: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Single `--no-merges --no-renames --numstat` pass returning parsed commit dicts & `sha -> commit_ts`."""
    fmt = f"--format={_RECORD}%H{_FIELD}%P{_FIELD}%an{_FIELD}%ae{_FIELD}%at{_FIELD}%ct{_FIELD}%B{_FIELD}"
    try:
        log_text = _git(
            repo,
            ["-c", "core.quotepath=false", "log", "--no-merges", "--no-renames", "--numstat", fmt, *span],
            timeout=900.0,
        )
    except RuntimeError:
        return [], {}

    commits: list[dict[str, Any]] = []
    span_commit_ts: dict[str, int] = {}
    for chunk in log_text.split(_RECORD):
        if not chunk:
            continue
        parts = chunk.split(_FIELD, 6)
        if len(parts) < 7:
            continue
        sha = parts[0].strip()
        if not _FULL_SHA.match(sha):
            continue
        an, ae = parts[2], parts[3]
        at_s, ct_s = parts[4].strip(), parts[5].strip()
        author_ts = int(at_s) if at_s.isdigit() else 0
        commit_ts = int(ct_s) if ct_s.isdigit() else author_ts
        span_commit_ts[sha] = commit_ts

        rest = parts[6]
        if _FIELD in rest:
            body, numstat_text = rest.rsplit(_FIELD, 1)
        else:
            body, numstat_text = rest, ""

        sec_lines: dict[str, list[int]] = {}
        sec_files: dict[str, set[str]] = {}
        for raw_line in numstat_text.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                continue
            cols = line.split("\t", 2)
            if len(cols) != 3:
                continue
            add_s, rem_s, fpath = cols[0].strip(), cols[1].strip(), cols[2].strip()
            if not fpath:
                continue
            added = int(add_s) if add_s.isdigit() else 0
            removed = int(rem_s) if rem_s.isdigit() else 0
            for sname in _match_file_sections(matcher, fpath, catchall_section, file_sec_cache):
                acc = sec_lines.setdefault(sname, [0, 0])
                acc[0] += added
                acc[1] += removed
                sec_files.setdefault(sname, set()).add(fpath)

        if not sec_lines:
            continue

        commits.append(
            {
                "sha": sha,
                "an": an,
                "ae": ae,
                "author_ts": author_ts or commit_ts,
                "commit_ts": commit_ts,
                "body": body,
                "sec_lines": sec_lines,
                "sec_files": sec_files,
            }
        )
    return commits, span_commit_ts


def _parse_merge_pass(
    repo: Path,
    span: list[str],
    matcher: Any,
    catchall_section: str,
    file_sec_cache: dict[str, list[str]],
    *,
    need_merge_metadata: bool,
) -> tuple[Counter[str], list[dict[str, Any]], dict[str, list[str]]]:
    """Single `--first-parent --merges -m --name-only` pass for subsystem merge counts and pull records."""
    merge_counts: Counter[str] = Counter()
    merges: list[dict[str, Any]] = []
    changed_files_map: dict[str, list[str]] = {}

    if not matcher and not need_merge_metadata:
        return merge_counts, merges, changed_files_map

    fmt = f"--format={_RECORD}%H{_FIELD}%P{_FIELD}%at{_FIELD}%cn{_FIELD}%ce{_FIELD}%s{_FIELD}%b{_FIELD}"
    try:
        out_log = _git(
            repo,
            ["-c", "core.quotepath=false", "log", "--first-parent", "--merges", "-m", "--no-renames", "--name-only", fmt, *span],
            timeout=600.0,
        )
    except RuntimeError:
        return merge_counts, merges, changed_files_map

    seen_merges: set[str] = set()
    for chunk in out_log.split(_RECORD):
        if not chunk.strip():
            continue
        parts = chunk.split(_FIELD, 6)
        if len(parts) < 7:
            continue
        m_sha = parts[0].strip()
        if not _FULL_SHA.match(m_sha) or m_sha in seen_merges:
            continue
        parents = parts[1].split()
        if len(parents) < 2:
            continue
        seen_merges.add(m_sha)

        at_str = parts[2].strip()
        merge_date = int(at_str) if at_str.isdigit() else 0
        cn = parts[3].strip()
        ce = parts[4].strip().lower()
        subj = parts[5].strip()
        rest = parts[6]
        if _FIELD in rest:
            body, files_text = rest.rsplit(_FIELD, 1)
        else:
            body, files_text = rest, ""
        body = body.rstrip("\r\n")
        files = [f.strip() for f in files_text.splitlines() if f.strip()]

        if matcher is not None:
            matched_secs: set[str] = set()
            for f in files:
                matched_secs.update(_match_file_sections(matcher, f, catchall_section, file_sec_cache))
            for sname in matched_secs:
                merge_counts[sname] += 1

        if need_merge_metadata:
            changed_files_map[m_sha] = files
            merges.append(
                {
                    "sha": m_sha,
                    "parents": parents,
                    "date": merge_date,
                    "committer": cn,
                    "committer_email": ce,
                    "subject": subj,
                    "body": body,
                }
            )

    return merge_counts, merges, changed_files_map


def _classify_fix_cycle(
    full_sha: str,
    tag: str,
    cur_parsed: dict[str, Any] | None,
    span_commit_ts: Mapping[str, int],
    sha_to_release: Mapping[str, tuple[str, int]] | None,
) -> str | None:
    if full_sha in span_commit_ts:
        return "same"
    if sha_to_release is None or full_sha not in sha_to_release:
        return None
    tgt_tag = sha_to_release[full_sha][0]
    if tgt_tag == tag:
        return "same"
    tgt_parsed = parse_tag(tgt_tag)
    if (
        cur_parsed is not None
        and tgt_parsed is not None
        and cur_parsed["family"] == tgt_parsed["family"]
        and cur_parsed["series"] == tgt_parsed["series"]
    ):
        return "same"
    return "cross"


def extract_release_deltas_from_git(
    repo: Path,
    tag: str,
    sha: str,
    parent_tag: str | None,
    parent_sha: str | None,
    *,
    sections: list[Any] | None = None,
    matcher: Any = None,
    catchall_section: str = "THE REST",
    cve_commit_hashes: set[bytes] | None = None,
    sha_to_release: Mapping[str, tuple[str, int]] | None = None,
    compute_pulls: bool = True,
    insights_cfg: Mapping[str, Any] | None = None,
    maintainers_file: str = "MAINTAINERS",
) -> dict[str, Any]:
    """Pure single-pass Git extractor for incremental subsystem, author, and stable deltas."""
    repo = Path(repo)
    cfg = dict(insights_cfg or {})
    if "catchall_section" in cfg and catchall_section == "THE REST":
        catchall_section = str(cfg.get("catchall_section", "THE REST") or "")
    if "maintainers_file" in cfg and maintainers_file == "MAINTAINERS":
        maintainers_file = str(cfg.get("maintainers_file", "MAINTAINERS") or "")

    if sections is None or matcher is None:
        sections, matcher = _load_maintainers(repo, sha or tag, maintainers_file)

    cur_parsed = parse_tag(tag)
    is_stable = bool(cur_parsed and cur_parsed["kind"] == "stable")
    is_final_mainline = bool(cur_parsed and cur_parsed["kind"] == "mainline" and cur_parsed["final"])
    series_track = series_track_for(tag)[:64]
    series_ord = series_ord_for(tag)
    parent_tag_str = (parent_tag or "")[:64]
    base_mainline_tag = series_name(cur_parsed["series"])[:64] if is_stable and cur_parsed is not None else ""
    release_ts = _tag_commit_timestamp(repo, sha or tag)

    sec_status_map: dict[str, str] = {}
    for sec in sections:
        name = getattr(sec, "name", "")
        if name and name != catchall_section:
            sec_status_map[name] = str(getattr(sec, "status", "") or "")[:64]

    span = [sha] + ([f"^{parent_sha}"] if parent_sha else [])
    file_sec_cache: dict[str, list[str]] = {}

    parsed_commits, span_commit_ts = _parse_numstat_commit_pass(
        repo,
        span,
        matcher if sections else None,
        catchall_section,
        file_sec_cache,
    )

    want_pulls = bool(compute_pulls and cfg.get("merge_window", True) and (cur_parsed is None or is_final_mainline))
    merge_counts, merges, changed_files_map = _parse_merge_pass(
        repo,
        span,
        matcher if sections else None,
        catchall_section,
        file_sec_cache,
        need_merge_metadata=want_pulls,
    )

    pulls: list[dict[str, Any]] = []
    if want_pulls and merges:
        from parser.git_ast.release_stats import _build_pulls_from_merges

        pulls = _build_pulls_from_merges(
            repo,
            sha,
            parent_sha,
            span,
            merges,
            changed_files_map,
            sections,
            matcher,
            cfg,
        )

    subsys_acc: dict[str, dict[str, Any]] = {}
    author_acc: dict[tuple[str, int], dict[str, Any]] = {}
    stable_acc: dict[str, dict[str, Any]] = {}
    sec_stats: dict[str, dict[str, Any]] = {}

    stable_pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    short_prefixes: set[str] = set()

    for c in parsed_commits:
        author_key = identity_key(c["an"], c["ae"])
        author_id = author_identity_id(author_key)
        author_ts = c["author_ts"]
        commit_ts = c["commit_ts"]
        body = c["body"]

        is_reviewed = False
        is_tested = False
        involved_people: set[str] = {author_key}
        for prefix, t_name, t_email in iter_trailers(body):
            t_ident = identity_key(t_name, t_email)
            roles = (
                CommitRole.roles_from_trailer_prefix(prefix)
                if hasattr(CommitRole, "roles_from_trailer_prefix")
                else (CommitRole.from_trailer_prefix(prefix),)
            )
            for role in roles:
                if role in (CommitRole.REVIEWED_BY, CommitRole.ACKED_BY):
                    if t_ident != author_key:
                        is_reviewed = True
                    involved_people.add(t_ident)
                elif role == CommitRole.TESTED_BY:
                    is_tested = True
                    involved_people.add(t_ident)
                elif role == CommitRole.SIGNED_OFF_BY:
                    involved_people.add(t_ident)

        rels = parse_relations(body)
        has_fixes = bool(rels["fixes"])
        has_stable_cc = bool(rels["stable_cc"])
        has_upstream = bool(rels["upstream"])

        if is_stable and (has_fixes or (cve_commit_hashes and has_upstream)):
            for pfx, _subj in rels["fixes"]:
                if len(pfx) < 40:
                    short_prefixes.add(pfx)
            if cve_commit_hashes:
                for u_hex in rels["upstream"]:
                    if len(u_hex) < 40:
                        short_prefixes.add(u_hex)
            stable_pending.append((c, rels))
        elif is_stable:
            stable_pending.append((c, rels))

        for sname, (added, removed) in c["sec_lines"].items():
            sub = subsys_acc.setdefault(
                sname,
                {
                    "commit_count": 0,
                    "lines_added": 0,
                    "lines_removed": 0,
                    "files": set(),
                    "author_ids": set(),
                    "fixes_commit_count": 0,
                    "stable_cc_count": 0,
                    "reviewed_commit_count": 0,
                    "tested_commit_count": 0,
                },
            )
            sub["commit_count"] += 1
            sub["lines_added"] += added
            sub["lines_removed"] += removed
            sub["files"].update(c["sec_files"][sname])
            sub["author_ids"].add(author_id)
            if has_fixes:
                sub["fixes_commit_count"] += 1
            if has_stable_cc:
                sub["stable_cc_count"] += 1
            if is_reviewed:
                sub["reviewed_commit_count"] += 1
            if is_tested:
                sub["tested_commit_count"] += 1

            auth = author_acc.setdefault(
                (sname, author_id),
                {
                    "author_key": author_key[:255],
                    "commit_count": 0,
                    "last_active_ts": 0,
                },
            )
            auth["commit_count"] += 1
            if author_ts > auth["last_active_ts"]:
                auth["last_active_ts"] = author_ts

            st_health = sec_stats.setdefault(
                sname,
                {
                    "commits": 0,
                    "reviewed_commits": 0,
                    "authors": {},
                    "activity": {},
                },
            )
            st_health["commits"] += 1
            if is_reviewed:
                st_health["reviewed_commits"] += 1
            st_health["authors"][author_key] = st_health["authors"].get(author_key, 0) + 1
            for ident in involved_people:
                if author_ts > st_health["activity"].get(ident, 0):
                    st_health["activity"][ident] = author_ts

            if is_stable:
                stb = stable_acc.setdefault(
                    sname,
                    {
                        "total_commits": 0,
                        "backport_commits": 0,
                        "fixes_tag_commits": 0,
                        "cc_stable_commits": 0,
                        "cve_linked_commits": 0,
                        "lines_added": 0,
                        "lines_removed": 0,
                        "same_cycle_fixes_count": 0,
                        "cross_release_fixes_count": 0,
                        "sum_fix_latency_days": 0.0,
                        "resolved_fixes_count": 0,
                    },
                )
                stb["total_commits"] += 1
                if has_upstream:
                    stb["backport_commits"] += 1
                if has_fixes:
                    stb["fixes_tag_commits"] += 1
                if has_stable_cc:
                    stb["cc_stable_commits"] += 1
                stb["lines_added"] += added
                stb["lines_removed"] += removed

    if is_stable and stable_pending:
        resolved_short = resolve_prefixes(repo, short_prefixes) if short_prefixes else {}
        need_target_ts: set[str] = set()
        for _c, rels in stable_pending:
            for pfx, _subj in rels["fixes"]:
                full = pfx if _FULL_SHA.match(pfx) else resolved_short.get(pfx)
                if full and _FULL_SHA.match(full) and full not in span_commit_ts:
                    need_target_ts.add(full)
        target_ts_map = _resolve_target_timestamps(repo, need_target_ts) if need_target_ts else {}

        for c, rels in stable_pending:
            c_sha = c["sha"]
            commit_ts = c["commit_ts"]
            is_cve = False
            if cve_commit_hashes:
                if bytes.fromhex(c_sha) in cve_commit_hashes:
                    is_cve = True
                else:
                    for u_hex in rels["upstream"]:
                        full_u = u_hex if _FULL_SHA.match(u_hex) else resolved_short.get(u_hex)
                        if full_u and _FULL_SHA.match(full_u) and bytes.fromhex(full_u) in cve_commit_hashes:
                            is_cve = True
                            break
            if is_cve:
                for sname in c["sec_lines"]:
                    stable_acc[sname]["cve_linked_commits"] += 1

            for pfx, _subj in rels["fixes"]:
                full_fix = pfx if _FULL_SHA.match(pfx) else resolved_short.get(pfx)
                if not full_fix or not _FULL_SHA.match(full_fix):
                    continue
                target_ts = span_commit_ts.get(full_fix)
                if target_ts is None:
                    target_ts = target_ts_map.get(full_fix)
                if target_ts is None:
                    continue
                latency_days = max(0.0, (commit_ts - target_ts) / 86400.0)
                cycle_kind = _classify_fix_cycle(full_fix, tag, cur_parsed, span_commit_ts, sha_to_release)
                for sname in c["sec_lines"]:
                    stb = stable_acc[sname]
                    stb["resolved_fixes_count"] += 1
                    stb["sum_fix_latency_days"] += latency_days
                    if cycle_kind == "same":
                        stb["same_cycle_fixes_count"] += 1
                    elif cycle_kind == "cross":
                        stb["cross_release_fixes_count"] += 1

    tag_col = tag[:64]
    subsystem_deltas: list[dict[str, Any]] = [
        {
            "version_tag": tag_col,
            "subsystem_name": "",
            "parent_tag": parent_tag_str,
            "series_track": series_track,
            "series_ord": series_ord,
            "release_ts": release_ts,
            "maintainer_status": "",
            "commit_count": 0,
            "merge_commit_count": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "files_touched_count": 0,
            "distinct_author_count": 0,
            "fixes_commit_count": 0,
            "stable_cc_count": 0,
            "reviewed_commit_count": 0,
            "tested_commit_count": 0,
        }
    ]

    all_subsystems = sorted(set(subsys_acc) | set(merge_counts))
    seen_sub_keys: set[str] = {""}
    for sname in all_subsystems:
        sub_key = sname[:255]
        if not sub_key or sub_key in seen_sub_keys:
            continue
        seen_sub_keys.add(sub_key)
        st = subsys_acc.get(sname)
        subsystem_deltas.append(
            {
                "version_tag": tag_col,
                "subsystem_name": sub_key,
                "parent_tag": parent_tag_str,
                "series_track": series_track,
                "series_ord": series_ord,
                "release_ts": release_ts,
                "maintainer_status": sec_status_map.get(sname, ""),
                "commit_count": st["commit_count"] if st else 0,
                "merge_commit_count": int(merge_counts.get(sname, 0)),
                "lines_added": st["lines_added"] if st else 0,
                "lines_removed": st["lines_removed"] if st else 0,
                "files_touched_count": len(st["files"]) if st else 0,
                "distinct_author_count": len(st["author_ids"]) if st else 0,
                "fixes_commit_count": st["fixes_commit_count"] if st else 0,
                "stable_cc_count": st["stable_cc_count"] if st else 0,
                "reviewed_commit_count": st["reviewed_commit_count"] if st else 0,
                "tested_commit_count": st["tested_commit_count"] if st else 0,
            }
        )

    author_deltas: list[dict[str, Any]] = []
    coalesced_authors: dict[tuple[str, int], dict[str, Any]] = {}
    for (sname, aid), auth in sorted(author_acc.items()):
        sub_key = sname[:255]
        if not sub_key:
            continue
        key = (sub_key, int(aid))
        prev = coalesced_authors.get(key)
        if prev is None:
            coalesced_authors[key] = {
                "version_tag": tag_col,
                "subsystem_name": sub_key,
                "author_identity_id": int(aid),
                "series_track": series_track,
                "series_ord": series_ord,
                "author_key": auth["author_key"],
                "commit_count": auth["commit_count"],
                "last_active_ts": auth["last_active_ts"],
            }
        else:
            prev["commit_count"] += auth["commit_count"]
            if auth["last_active_ts"] > prev["last_active_ts"]:
                prev["last_active_ts"] = auth["last_active_ts"]
    author_deltas.extend(coalesced_authors.values())

    stable_deltas: list[dict[str, Any]] = []
    if is_stable:
        stable_deltas.append(
            {
                "version_tag": tag_col,
                "subsystem_name": "",
                "parent_tag": parent_tag_str,
                "base_mainline_tag": base_mainline_tag,
                "series_track": series_track,
                "series_ord": series_ord,
                "release_ts": release_ts,
                "total_commits": 0,
                "backport_commits": 0,
                "fixes_tag_commits": 0,
                "cc_stable_commits": 0,
                "cve_linked_commits": 0,
                "lines_added": 0,
                "lines_removed": 0,
                "same_cycle_fixes_count": 0,
                "cross_release_fixes_count": 0,
                "sum_fix_latency_days": 0.0,
                "resolved_fixes_count": 0,
            }
        )
        seen_stable_keys: set[str] = {""}
        for sname in sorted(stable_acc):
            sub_key = sname[:255]
            if not sub_key or sub_key in seen_stable_keys:
                continue
            seen_stable_keys.add(sub_key)
            stb = stable_acc[sname]
            stable_deltas.append(
                {
                    "version_tag": tag_col,
                    "subsystem_name": sub_key,
                    "parent_tag": parent_tag_str,
                    "base_mainline_tag": base_mainline_tag,
                    "series_track": series_track,
                    "series_ord": series_ord,
                    "release_ts": release_ts,
                    "total_commits": stb["total_commits"],
                    "backport_commits": stb["backport_commits"],
                    "fixes_tag_commits": stb["fixes_tag_commits"],
                    "cc_stable_commits": stb["cc_stable_commits"],
                    "cve_linked_commits": stb["cve_linked_commits"],
                    "lines_added": stb["lines_added"],
                    "lines_removed": stb["lines_removed"],
                    "same_cycle_fixes_count": stb["same_cycle_fixes_count"],
                    "cross_release_fixes_count": stb["cross_release_fixes_count"],
                    "sum_fix_latency_days": round(float(stb["sum_fix_latency_days"]), 6),
                    "resolved_fixes_count": stb["resolved_fixes_count"],
                }
            )

    return {
        "tag": tag,
        "sha": sha,
        "parent_tag": parent_tag_str,
        "series_track": series_track,
        "series_ord": series_ord,
        "release_ts": release_ts,
        "base_mainline_tag": base_mainline_tag,
        "is_stable": is_stable,
        "subsystem_deltas": subsystem_deltas,
        "author_deltas": author_deltas,
        "stable_deltas": stable_deltas,
        "pulls": pulls,
        "sec_stats": sec_stats,
        "sections": sections,
    }


def _row_tuple(row: Mapping[str, Any] | tuple[Any, ...], cols: tuple[str, ...]) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        return tuple(row[c] for c in cols)
    return tuple(row)


def write_deltas_in_tx(db: Any, extracted: Mapping[str, Any], *, replace: bool = False) -> int:
    """Write delta rows inside the caller's active transaction."""
    tag = str(extracted.get("tag") or "")[:64]
    if replace and tag:
        db.delete_rows("insights_subsystem_version_delta", {"version_tag": tag})
        db.delete_rows("insights_subsystem_author_delta", {"version_tag": tag})
        db.delete_rows("insights_stable_version_delta", {"version_tag": tag})

    sub_rows = [_row_tuple(r, SUBSYSTEM_DELTA_COLUMNS) for r in extracted.get("subsystem_deltas") or ()]
    auth_rows = [_row_tuple(r, AUTHOR_DELTA_COLUMNS) for r in extracted.get("author_deltas") or ()]
    stable_rows = [_row_tuple(r, STABLE_DELTA_COLUMNS) for r in extracted.get("stable_deltas") or ()]

    written = 0
    for table, cols, rows in (
        ("insights_subsystem_version_delta", SUBSYSTEM_DELTA_COLUMNS, sub_rows),
        ("insights_subsystem_author_delta", AUTHOR_DELTA_COLUMNS, auth_rows),
        ("insights_stable_version_delta", STABLE_DELTA_COLUMNS, stable_rows),
    ):
        for i in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[i : i + INSERT_CHUNK]
            db.insert_rows(table, list(cols), chunk, on_conflict="update")
            written += len(chunk)
    return written


def write_insights_deltas(db: Any, extracted: Mapping[str, Any], *, replace: bool = False) -> int:
    """Idempotently persist extracted delta payloads inside a single transaction."""
    with db.transaction():
        return write_deltas_in_tx(db, extracted, replace=replace)


def load_cve_commit_hashes(db: Any, repo_id: int | None = None) -> set[bytes]:
    """Load 20-byte CVE fix commit hashes from `m_cve_commit` and `m_cve_commit_hash`."""
    if db is None:
        return set()
    existing = db.existing_tables(["m_cve_commit", "m_cve_commit_hash"])
    if "m_cve_commit" not in existing:
        return set()

    out: set[bytes] = set()
    short_prefixes: list[str] = []
    for (raw_hash,) in db.select_rows("m_cve_commit", ["commit_hash"], {"role": 1}, distinct=True, as_dict=False):
        h = raw_hash.decode("utf-8", "replace") if isinstance(raw_hash, (bytes, bytearray)) else str(raw_hash or "")
        h = h.strip().lower()
        if _FULL_SHA.match(h):
            out.add(bytes.fromhex(h))
        elif h:
            short_prefixes.append(h)

    if short_prefixes and "m_cve_commit_hash" in existing:
        for i in range(0, len(short_prefixes), 1000):
            chunk = short_prefixes[i : i + 1000]
            where: dict[str, Any] = {"prefix": chunk}
            if repo_id is not None:
                where["repo_id"] = int(repo_id)
            for (b_hash,) in db.select_rows("m_cve_commit_hash", ["commit_hash"], where, as_dict=False):
                if b_hash:
                    out.add(bytes(b_hash))
    return out


def _rev_list_worker(args: tuple[Path, str, str, str | None]) -> tuple[str, list[str]]:
    repo, tag, sha, base_sha = args
    span = [sha] + ([f"^{base_sha}"] if base_sha else [])
    try:
        out = _git(repo, ["rev-list", *span], timeout=300.0)
    except RuntimeError:
        return tag, []
    return tag, [line.strip() for line in out.splitlines() if _FULL_SHA.match(line.strip())]


def build_sha_to_release(
    repo: Path,
    tag_shas: Mapping[str, str],
    workers: int = 1,
) -> dict[str, tuple[str, int]]:
    """Build `commit_sha -> (first_release_tag, series_ord)` across mainline then stable segments."""
    if not tag_shas:
        return {}
    ranges = release_ranges(tag_shas.keys())
    mainline_tasks = [
        (repo, r["tag"], tag_shas[r["tag"]], tag_shas.get(r["segment_base"]) if r["segment_base"] else None)
        for r in ranges
        if r["kind"] != "stable" and r["tag"] in tag_shas
    ]
    stable_tasks = [
        (repo, r["tag"], tag_shas[r["tag"]], tag_shas.get(r["segment_base"]) if r["segment_base"] else None)
        for r in ranges
        if r["kind"] == "stable" and r["tag"] in tag_shas
    ]
    tasks = mainline_tasks + stable_tasks
    if not tasks:
        return {}

    if workers > 1 and len(tasks) > 4:
        try:
            with ProcessPoolExecutor(
                max_workers=min(workers, len(tasks)),
                mp_context=multiprocessing.get_context("fork"),
            ) as pool:
                results = list(pool.map(_rev_list_worker, tasks))
        except (BrokenProcessPool, OSError, NotImplementedError, ImportError):
            results = [_rev_list_worker(t) for t in tasks]
    else:
        results = [_rev_list_worker(t) for t in tasks]

    sha_to_rel: dict[str, tuple[str, int]] = {}
    for tag, shas in results:
        tag_entry = (tag, series_ord_for(tag))
        for c_sha in shas:
            sha_to_rel.setdefault(c_sha, tag_entry)
    return sha_to_rel


def _resolve_delta_parents(
    db: Any,
    all_tag_shas: Mapping[str, str],
    selected: list[str],
    explicit_tags: bool,
) -> dict[str, str | None]:
    ranges_by_tag = {r["tag"]: r for r in release_ranges(all_tag_shas.keys())}
    stored_by_track: dict[str, list[tuple[int, str]]] = defaultdict(list)
    if explicit_tags and db is not None and "insights_subsystem_version_delta" in db.existing_tables(["insights_subsystem_version_delta"]):
        rows = db.select_rows(
            "insights_subsystem_version_delta",
            ["series_track", "series_ord", "version_tag"],
            {"subsystem_name": ""},
            as_dict=False,
        )
        for track, s_ord, vtag in rows:
            t_str = track.decode("utf-8", "replace") if isinstance(track, (bytes, bytearray)) else str(track or "")
            v_str = vtag.decode("utf-8", "replace") if isinstance(vtag, (bytes, bytearray)) else str(vtag or "")
            stored_by_track[t_str].append((int(s_ord), v_str))

    prev_on_track: dict[str, str] = {}
    parent_map: dict[str, str | None] = {}
    for tag in selected:
        p = parse_tag(tag)
        track = series_track_for(tag)
        if track in prev_on_track:
            parent_map[tag] = prev_on_track[track]
        elif explicit_tags:
            cur_ord = series_ord_for(tag)
            earlier = [vt for s_ord, vt in stored_by_track.get(track, ()) if s_ord < cur_ord and vt in all_tag_shas]
            if earlier:
                parent_map[tag] = max(earlier, key=series_ord_for)
            else:
                rng = ranges_by_tag.get(tag)
                parent_map[tag] = rng.get("segment_base") if rng else None
        elif p and p["kind"] == "stable":
            base_ml = series_name(p["series"])
            parent_map[tag] = base_ml if base_ml in all_tag_shas else None
        elif p and p["kind"] == "next":
            rng = ranges_by_tag.get(tag)
            parent_map[tag] = rng.get("segment_base") if rng else None
        else:
            parent_map[tag] = None
        prev_on_track[track] = tag
    return parent_map


def _extract_worker(job: tuple[Path, str, str, str | None, str | None, str, str, dict[str, Any]]) -> dict[str, Any]:
    repo, tag, sha, parent_tag, parent_sha, catchall, maintainers_file, cfg = job
    return extract_release_deltas_from_git(
        repo,
        tag,
        sha,
        parent_tag,
        parent_sha,
        catchall_section=catchall,
        cve_commit_hashes=_WORKER_CTX.get("cve_commit_hashes"),
        sha_to_release=_WORKER_CTX.get("sha_to_release"),
        compute_pulls=False,
        insights_cfg=cfg,
        maintainers_file=maintainers_file,
    )


def ingest_insights_deltas(
    db: Any,
    repo: Path,
    repo_id: int | None = None,
    *,
    from_tag: str | None = None,
    tags: Iterable[str] | None = None,
    workers: int | None = None,
    force: bool = False,
    repo_entry: dict[str, Any] | None = None,
    insights_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract and idempotently persist `insights_*_delta` rows for selected tags in `repo`."""
    started = time.perf_counter()
    if db is None:
        return {"state": "unavailable", "tags": 0, "written": 0, "rows": 0, "seconds": 0.0}

    required = ("insights_subsystem_version_delta", "insights_subsystem_author_delta", "insights_stable_version_delta")
    if len(db.existing_tables(required)) < len(required):
        return {"state": "unavailable", "tags": 0, "written": 0, "rows": 0, "seconds": 0.0}

    repo = Path(repo)
    cfg_entry = repo_entry
    if cfg_entry is None:
        from core.config import get_repository

        with contextlib.suppress(Exception):
            cfg_entry = get_repository(str(repo))

    if insights_cfg is None:
        from core.config import resolve_repo_insights

        insights_cfg = resolve_repo_insights(cfg_entry)

    maintainers_file = str(insights_cfg.get("maintainers_file", "MAINTAINERS") or "")
    catchall_section = str(insights_cfg.get("catchall_section", "THE REST") or "")

    all_tag_shas = _release_tags(repo)
    if not all_tag_shas:
        return {"state": "current", "tags": 0, "written": 0, "rows": 0, "seconds": 0.0}

    explicit_tags = tags is not None
    if explicit_tags:
        selected = sorted(
            [t for t in dict.fromkeys(tags or ()) if t in all_tag_shas],
            key=release_key,
        )
    elif repo_entry is not None:
        from core.config import resolve_preparse_tags

        selected = resolve_preparse_tags(repo_entry, "insights", all_tag_shas.keys())
    else:
        selected = sorted(all_tag_shas.keys(), key=release_key)

    parent_map = _resolve_delta_parents(db, all_tag_shas, selected, explicit_tags)

    if from_tag:
        fk = release_key(from_tag)
        selected = [t for t in selected if release_key(t) >= fk]

    if not selected:
        return {"state": "current", "tags": 0, "written": 0, "rows": 0, "seconds": 0.0}

    stored_subsys = {
        (vtag.decode("utf-8", "replace") if isinstance(vtag, (bytes, bytearray)) else str(vtag)): (
            ptag.decode("utf-8", "replace") if isinstance(ptag, (bytes, bytearray)) else str(ptag or "")
        )
        for vtag, ptag in db.select_rows(
            "insights_subsystem_version_delta",
            ["version_tag", "parent_tag"],
            {"subsystem_name": "", "version_tag": selected},
            as_dict=False,
        )
    }
    stored_stable = {
        (vtag.decode("utf-8", "replace") if isinstance(vtag, (bytes, bytearray)) else str(vtag)): (
            ptag.decode("utf-8", "replace") if isinstance(ptag, (bytes, bytearray)) else str(ptag or "")
        )
        for vtag, ptag in db.select_rows(
            "insights_stable_version_delta",
            ["version_tag", "parent_tag"],
            {"subsystem_name": "", "version_tag": selected},
            as_dict=False,
        )
    }

    todo: list[str] = []
    for tag in selected:
        want_parent = parent_map.get(tag) or ""
        p = parse_tag(tag)
        is_stable = bool(p and p["kind"] == "stable")
        if force:
            todo.append(tag)
        elif stored_subsys.get(tag) != want_parent:
            todo.append(tag)
        elif is_stable and stored_stable.get(tag) != want_parent:
            todo.append(tag)

    if not todo:
        return {
            "state": "current",
            "tags": len(selected),
            "written": 0,
            "rows": 0,
            "seconds": round(time.perf_counter() - started, 2),
        }

    pool_workers = max(1, os.cpu_count() or 4) if workers is None else max(1, int(workers))
    has_stable = any((p := parse_tag(t)) is not None and p["kind"] == "stable" for t in todo)
    cve_hashes = load_cve_commit_hashes(db, repo_id) if has_stable else None
    sha_to_rel = build_sha_to_release(repo, all_tag_shas, workers=pool_workers) if has_stable else None

    jobs = [
        (
            repo,
            tag,
            all_tag_shas[tag],
            parent_map.get(tag),
            all_tag_shas.get(parent_map[tag]) if parent_map.get(tag) else None,
            catchall_section,
            maintainers_file,
            dict(insights_cfg),
        )
        for tag in todo
    ]

    written_tags = 0
    total_rows = 0

    def _consume(results: Iterable[dict[str, Any]]) -> None:
        nonlocal written_tags, total_rows
        for res in results:
            tag = res["tag"]
            is_existing = tag in stored_subsys or tag in stored_stable
            total_rows += write_insights_deltas(db, res, replace=is_existing)
            written_tags += 1

    _WORKER_CTX["cve_commit_hashes"] = cve_hashes
    _WORKER_CTX["sha_to_release"] = sha_to_rel
    try:
        if pool_workers <= 1 or len(jobs) <= 1:
            _consume(_extract_worker(j) for j in jobs)
        else:
            done = 0
            try:
                with ProcessPoolExecutor(
                    max_workers=min(pool_workers, len(jobs)),
                    mp_context=multiprocessing.get_context("fork"),
                ) as pool:
                    for res in pool.map(_extract_worker, jobs):
                        _consume((res,))
                        done += 1
            except (BrokenProcessPool, OSError, NotImplementedError, ImportError) as exc:
                logger.warning("[Insights deltas] Process pool unavailable (%s); using threads.", exc)
                rest = jobs[done:]
                if rest:
                    with ThreadPoolExecutor(max_workers=min(pool_workers, len(rest))) as pool:
                        _consume(pool.map(_extract_worker, rest))
    finally:
        _WORKER_CTX.clear()

    return {
        "state": "updated",
        "tags": len(selected),
        "written": written_tags,
        "rows": total_rows,
        "seconds": round(time.perf_counter() - started, 2),
    }
