"""webapp/backend/services/maintainer_service.py - Subsystems Catalog, Maintainers Roster & CREDITS."""
from __future__ import annotations
from webapp.backend.config import RepoScopedCache
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
    hash_hex,
    get_version_info,
)
from webapp.backend.security.sql import sanitize_like_query
from parser.maintainer_ast.maintainer_types import (
    MaintainerRole,
    PatternType,
    MaintainerPerson,
    PatternRule,
    MaintainerSection,
)
from parser.maintainer_ast.maintainer_matcher import MaintainerMatcher

# Bounded: keyed by the requested version, and any revision a caller names (a commit SHA) adds a whole
# MAINTAINERS model
_MAINTAINER_CACHE: dict[str, tuple[list[MaintainerSection], MaintainerMatcher]] = RepoScopedCache(maxsize=16)
# Bounded: the key carries the requested file path, so each distinct path a caller sends adds an entry
_FILE_SUBSYSTEMS_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = RepoScopedCache(maxsize=4096)
# Sections of one MAINTAINERS blob / entries of one CREDITS blob (content addressed, keyed by blob_id)
_BLOB_SECTIONS_CACHE: dict[int, list[int]] = RepoScopedCache(maxsize=64)
_BLOB_CREDITS_CACHE: dict[int, list[int]] = RepoScopedCache(maxsize=64)
# (emails, names) of the version's CREDITS entries, for the in_credits flags of resolve_subsystems_for_file
_CREDITS_KEYS_CACHE: dict[str, tuple[set[str], set[str]]] = RepoScopedCache(maxsize=16)
# Persona identity index (see MaintainerService._identity_index), keyed by registry signature
_IDENTITY_CACHE: dict[str, Any] = RepoScopedCache(maxsize=4)
# Files of one patch that match_maintainers() resolves; the rest are reported as skipped
MAX_PATCH_FILES = 200


def _clean_name(name: str | None) -> str:
    """Display form of a persona name: trims whitespace, surrounding quotes and a leaked 'From:' header prefix."""
    clean = " ".join((name or "").split())
    if clean[:5].casefold() == "from:":
        clean = clean[5:].strip()
    while len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in "\"'":
        clean = clean[1:-1].strip()
    return clean


def _identity_key(name: str | None, email: str | None) -> str:
    """Normalized identity of a persona: m_maintainer_person holds one row per (name, email) alias,
    so the same developer appears under several person_ids. Group by case/whitespace-folded name,
    falling back to the email when the name is empty."""
    norm_name = _clean_name(name).casefold()
    if norm_name:
        return f"n:{norm_name}"
    return f"e:{(email or '').strip().casefold()}"


def _merge_emails(emails: list[str | None]) -> list[str]:
    """Split comma-joined email fields and return the distinct addresses, first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for field in emails:
        for addr in (field or "").split(","):
            addr = addr.strip()
            if addr and addr.casefold() not in seen:
                seen.add(addr.casefold())
                out.append(addr)
    return out


def _in_ids(column: str, ids: list[int] | None) -> tuple[str, list[int]]:
    """SQL condition `column IN (ids)` and its parameters; never true for an empty or missing list."""
    if not ids:
        return "1 = 0", []
    return f"{column} IN ({','.join(['%s'] * len(ids))})", list(ids)


def _build_identity_index(personas: Any) -> dict[int, str]:
    """Map each (person_id, name, email) persona to its identity key.

    Personas group by normalized name. A name-less persona joins the named identity that owns its
    address; when several named identities use it (typo'd name, nickname), the one using it on the
    most personas, then the largest, wins. Named identities are never merged with each other by
    email: shared or mistyped addresses would chain distinct people together."""
    pid_to_key: dict[int, str] = {}
    pid_emails: dict[int, list[str]] = {}
    sizes: dict[str, int] = {}
    email_owners: dict[str, dict[str, int]] = {}
    for pid, name, email in personas:
        key = _identity_key(name, email)
        pid_to_key[pid] = key
        pid_emails[pid] = [a.casefold() for a in _merge_emails([email])]
        sizes[key] = sizes.get(key, 0) + 1
        if key.startswith("n:"):
            for addr in pid_emails[pid]:
                owners = email_owners.setdefault(addr, {})
                owners[key] = owners.get(key, 0) + 1
    for pid, key in pid_to_key.items():
        if not key.startswith("e:"):
            continue
        candidates: dict[str, int] = {}
        for addr in pid_emails[pid]:
            for owner, cnt in email_owners.get(addr, {}).items():
                candidates[owner] = candidates.get(owner, 0) + cnt
        if candidates:
            pid_to_key[pid] = max(candidates, key=lambda k: (candidates[k], sizes[k], k))
    return pid_to_key


class MaintainerService:
    """Service for exploring subsystems, maintainer roles, CREDITS entries, and matching patches."""

    @staticmethod
    def _get_version_blobs(cursor: Any, vid: int | None, vname: str) -> tuple[int | None, int | None]:
        m_blob_id = None
        c_blob_id = None
        if vid and vid > 0:
            cursor.execute(
                """
                SELECT fn.fname, bc.blob_id
                FROM m_bridge_checkpoint bc
                JOIN m_file_name fn ON bc.fnid = fn.fnid
                WHERE bc.commit_id = %s AND fn.fname IN ('MAINTAINERS', 'CREDITS');
                """,
                (vid,),
            )
            for row in cursor.fetchall():
                fname = safe_decode(row["fname"])
                if fname == "MAINTAINERS":
                    m_blob_id = row["blob_id"]
                elif fname == "CREDITS":
                    c_blob_id = row["blob_id"]
        if m_blob_id is None or c_blob_id is None:
            try:
                from webapp.backend.services.tree_resolver import git_tree_resolver
                bp = git_tree_resolver.blob_paths(cursor, vname)
                if bp:
                    for b_id, paths in bp.items():
                        if "MAINTAINERS" in paths and m_blob_id is None:
                            m_blob_id = b_id
                        if "CREDITS" in paths and c_blob_id is None:
                            c_blob_id = b_id
            except Exception:
                pass
        return m_blob_id, c_blob_id

    @staticmethod
    def _blob_section_ids(cursor: Any, blob_id: int) -> list[int]:
        """sec_ids of the sections one MAINTAINERS blob holds.

        Sections are content addressed: one unchanged since an earlier MAINTAINERS keeps the row stored
        with that earlier blob (`m_maintainer_section.blob_id`), so a blob's sections are the rows whose
        AST is one of the blob's tags. One row per section name, the newest when several share it.
        """
        cached = _BLOB_SECTIONS_CACHE.get(blob_id)
        if cached is not None:
            return cached
        cursor.execute(
            """
            SELECT MAX(s.sec_id) AS sec_id, s.name
            FROM m_bridge_tag bt
            JOIN m_tag t ON t.tag_id = bt.tag_id
            JOIN m_maintainer_section s ON s.ast_id = t.ast_id
            WHERE bt.blob_id = %s AND s.sec_id > 0
            GROUP BY s.ast_id, s.name;
            """,
            (blob_id,),
        )
        newest: dict[str, int] = {}
        for r in cursor.fetchall():
            key = (safe_decode(r["name"]) or "").strip().lower()
            newest[key] = max(newest.get(key, 0), int(r["sec_id"]))
        ids = sorted(newest.values())
        _BLOB_SECTIONS_CACHE[blob_id] = ids
        return ids

    @staticmethod
    def _blob_credit_ids(cursor: Any, blob_id: int) -> list[int]:
        """credit_ids of the entries one CREDITS blob holds (found through its tags, as sections are)."""
        cached = _BLOB_CREDITS_CACHE.get(blob_id)
        if cached is not None:
            return cached
        cursor.execute(
            """
            SELECT DISTINCT ce.credit_id
            FROM m_bridge_tag bt
            JOIN m_tag t ON t.tag_id = bt.tag_id
            JOIN m_credits_entry ce ON ce.ast_id = t.ast_id
            WHERE bt.blob_id = %s AND ce.credit_id > 0;
            """,
            (blob_id,),
        )
        ids = sorted(int(r["credit_id"]) for r in cursor.fetchall())
        _BLOB_CREDITS_CACHE[blob_id] = ids
        return ids

    @staticmethod
    def _version_scope(cursor: Any, vid: int | None, vname: str) -> tuple[list[int] | None, list[int] | None]:
        """(sec_ids, credit_ids) of the version's MAINTAINERS and CREDITS files; None for a file whose
        blob was never indexed (nothing of it is listed rather than every section ever parsed)."""
        m_blob_id, c_blob_id = MaintainerService._get_version_blobs(cursor, vid, vname)
        sec_ids = MaintainerService._blob_section_ids(cursor, m_blob_id) if m_blob_id else None
        credit_ids = MaintainerService._blob_credit_ids(cursor, c_blob_id) if c_blob_id else None
        return sec_ids, credit_ids

    @staticmethod
    def _identity_index(cursor: Any) -> tuple[dict[int, str], dict[str, list[int]]]:
        """(person_id -> identity key, identity key -> person_ids) over m_maintainer_person.

        Shared by the Developers directory and the profile modal so both resolve the same aliases.
        Cached per repository until the persona registry grows."""
        cursor.execute("SELECT COUNT(*) AS cnt, MAX(person_id) AS max_id FROM m_maintainer_person;")
        sig_row = cursor.fetchone() or {}
        sig = (int(sig_row.get("cnt") or 0), int(sig_row.get("max_id") or 0))
        cached = _IDENTITY_CACHE.get("index")
        if cached is not None and cached[0] == sig:
            return cached[1], cached[2]
        cursor.execute("SELECT person_id, name, email FROM m_maintainer_person WHERE person_id > 0;")
        pid_to_key = _build_identity_index(
            (int(r["person_id"]), safe_decode(r["name"]), safe_decode(r["email"])) for r in cursor.fetchall()
        )
        members: dict[str, list[int]] = {}
        for pid, key in pid_to_key.items():
            members.setdefault(key, []).append(pid)
        for pids in members.values():
            pids.sort()
        _IDENTITY_CACHE["index"] = (sig, pid_to_key, members)
        return pid_to_key, members

    def get_overview(self, version_name: str, query: str = "") -> dict[str, Any]:
        """List subsystem sections and maintainers with optional query filtering."""
        target_q = query
        clean_q = sanitize_like_query(target_q)
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sec_ids, _ = self._version_scope(cursor, vid, vname)
            in_secs, params = _in_ids("s.sec_id", sec_ids)

            sql = f"""
                SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list
                FROM m_maintainer_section s
                WHERE {in_secs}
            """
            if clean_q:
                sql += """
                    AND (
                        LOWER(s.name) LIKE LOWER(%s)
                        OR EXISTS (
                            SELECT 1 FROM m_maintainer_member mem
                            JOIN m_maintainer_person per ON mem.person_id = per.person_id
                            WHERE mem.sec_id = s.sec_id AND (LOWER(per.name) LIKE LOWER(%s) OR LOWER(per.email) LIKE LOWER(%s))
                        )
                    )
                """
                like_str = f"%{clean_q}%"
                params.extend([like_str, like_str, like_str])
            sql += " ORDER BY s.sec_id DESC;"

            cursor.execute(sql, tuple(params))
            all_raw = cursor.fetchall()
            seen_names: set[str] = set()
            sections_raw: list[dict[str, Any]] = []
            for r in all_raw:
                s_name = safe_decode(r["name"]) or ""
                norm_name = s_name.strip().lower()
                if norm_name in seen_names:
                    continue
                seen_names.add(norm_name)
                sections_raw.append(r)

            sections_raw.sort(key=lambda s: (safe_decode(s["name"]) or "").lower())

            sec_ids = [s["sec_id"] for s in sections_raw]
            members_by_sec: dict[int, list[dict[str, Any]]] = {}
            if sec_ids:
                format_strings = ",".join(["%s"] * len(sec_ids))
                cursor.execute(
                    f"""
                    SELECT m.sec_id, p.person_id, p.name, p.email, m.role_type
                    FROM m_maintainer_member m
                    JOIN m_maintainer_person p ON m.person_id = p.person_id
                    WHERE m.sec_id IN ({format_strings})
                    ORDER BY m.sec_id ASC, m.priority ASC;
                    """,
                    tuple(sec_ids),
                )
                for m in cursor.fetchall():
                    s_id = m["sec_id"]
                    curr_list = members_by_sec.setdefault(s_id, [])
                    if len(curr_list) < 5:
                        curr_list.append({
                            "person_id": m["person_id"],
                            "name": safe_decode(m["name"]),
                            "email": safe_decode(m["email"]),
                            "role_type": m["role_type"],
                            "role_name": "Maintainer" if m["role_type"] == 1 else "Reviewer",
                        })

            sections = []
            for s in sections_raw:
                sec_id = s["sec_id"]
                sections.append({
                    "sec_id": sec_id,
                    "name": safe_decode(s["name"]),
                    "status": safe_decode(s["status"]),
                    "scm_tree": safe_decode(s["scm_tree"]),
                    "web_page": safe_decode(s["web_page"]),
                    "mailing_list": safe_decode(s["mailing_list"]),
                    "maintainers": members_by_sec.get(sec_id, []),
                })

            return {"version": vname, "indexed": sec_ids is not None, "sections": sections}

    def get_developers(
        self,
        version_name: str,
        query: str = "",
        role: str = "all",
        sort: str = "activity",
    ) -> dict[str, Any]:
        """List all developers, maintainers, reviewers, and contributors recorded in the kernel persona registry."""
        target_q = query
        clean_role = (role or "all").strip().lower()
        clean_sort = (sort or "activity").strip().lower()

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sec_ids, credit_ids = self._version_scope(cursor, vid, vname)
            in_credits_sql, credit_params = _in_ids("c.credit_id", credit_ids)
            in_secs_sql, sec_params = _in_ids("s.sec_id", sec_ids)

            sql = f"""
                SELECT
                    p.person_id,
                    p.name,
                    p.email,
                    COUNT(DISTINCT s.name) AS subsystems_count,
                    MAX(CASE WHEN s.sec_id IS NOT NULL AND m.role_type = 1 THEN 1 ELSE 0 END) AS is_maintainer,
                    MAX(CASE WHEN s.sec_id IS NOT NULL AND m.role_type = 2 THEN 1 ELSE 0 END) AS is_reviewer,
                    EXISTS(SELECT 1 FROM m_credits_entry c WHERE c.person_id = p.person_id AND {in_credits_sql}) AS in_credits,
                    EXISTS(SELECT 1 FROM m_commit com WHERE com.author_id = p.person_id) AS has_commits
                FROM m_maintainer_person p
                LEFT JOIN m_maintainer_member m ON p.person_id = m.person_id
                LEFT JOIN m_maintainer_section s ON m.sec_id = s.sec_id AND {in_secs_sql}
            """
            params: list[Any] = [*credit_params, *sec_params]
            sql += " WHERE p.person_id > 0 GROUP BY p.person_id, p.name, p.email;"

            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()

            # Merge alias personas (same developer under several person_ids) into one entry.
            pid_to_key, _ = self._identity_index(cursor)
            groups: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                pid = int(r["person_id"])
                groups.setdefault(pid_to_key.get(pid, f"p:{pid}"), []).append(r)

            # Search matches any alias's name or address, and keeps the whole merged identity
            needle = (target_q or "").strip()[:100].casefold()
            if needle:
                groups = {
                    k: g for k, g in groups.items()
                    if any(needle in (safe_decode(r["name"]) or "").casefold() or needle in (safe_decode(r["email"]) or "").casefold() for r in g)
                }

            # Distinct subsystem names per alias, needed only where several aliases carry subsystems
            multi_pids = [
                r["person_id"]
                for g in groups.values() if sum(1 for r in g if r["subsystems_count"]) > 1
                for r in g if r["subsystems_count"]
            ]
            sec_names_by_pid: dict[int, set[str]] = {}
            if multi_pids:
                fmt = ",".join(["%s"] * len(multi_pids))
                cursor.execute(
                    f"""
                    SELECT DISTINCT m.person_id, s.name
                    FROM m_maintainer_member m
                    JOIN m_maintainer_section s ON m.sec_id = s.sec_id
                    WHERE m.person_id IN ({fmt}) AND {in_secs_sql};
                    """,
                    (*multi_pids, *sec_params),
                )
                for sr in cursor.fetchall():
                    sec_names_by_pid.setdefault(sr["person_id"], set()).add(safe_decode(sr["name"]) or "")

            developers = []
            for g in groups.values():
                # Representative alias: one with a name and email, most subsystems, CREDITS/commit presence
                rep = max(
                    g,
                    key=lambda r: (
                        bool(_clean_name(safe_decode(r["name"]))),
                        bool((safe_decode(r["email"]) or "").strip()),
                        int(r["subsystems_count"] or 0),
                        bool(r["in_credits"]),
                        bool(r["has_commits"]),
                        -int(r["person_id"]),
                    ),
                )
                p_name = _clean_name(safe_decode(rep["name"]))
                p_email = safe_decode(rep["email"])
                if len(g) > 1 and any(sec_names_by_pid.get(r["person_id"]) for r in g):
                    sub_names: set[str] = set()
                    for r in g:
                        sub_names |= sec_names_by_pid.get(r["person_id"], set())
                    sub_count = len(sub_names)
                else:
                    sub_count = max(int(r["subsystems_count"] or 0) for r in g)
                is_m = any(bool(r["is_maintainer"]) for r in g)
                is_r = any(bool(r["is_reviewer"]) for r in g)
                in_cred = any(bool(r["in_credits"]) for r in g)
                has_c = any(bool(r["has_commits"]) for r in g)

                if clean_role == "maintainer" and not is_m:
                    continue
                if clean_role == "reviewer" and not is_r:
                    continue
                if clean_role == "credits" and not in_cred:
                    continue

                developers.append({
                    "person_id": rep["person_id"],
                    "name": p_name,
                    "email": p_email,
                    "emails": _merge_emails([p_email] + [safe_decode(r["email"]) for r in g]),
                    "alias_person_ids": sorted(int(r["person_id"]) for r in g),
                    "subsystems_count": sub_count,
                    "is_maintainer": is_m,
                    "is_reviewer": is_r,
                    "in_credits": in_cred,
                    "has_commits": has_c,
                })

            if clean_sort == "alpha":
                developers.sort(key=lambda d: ((d["name"] or d["email"] or "").casefold(), -d["subsystems_count"]))
            else:
                developers.sort(key=lambda d: (-d["subsystems_count"], (d["name"] or d["email"] or "").casefold()))

            return {
                "version": vname,
                "sort": clean_sort,
                "developers": developers,
            }

    def get_section_detail(self, version_name: str, section_name: str) -> dict[str, Any]:
        """Retrieve subsystem details, pattern rules, and file matches."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sec_ids, _ = self._version_scope(cursor, vid, vname)
            in_secs_sql, sec_params = _in_ids("s.sec_id", sec_ids)

            sec_param = str(section_name).strip()
            if sec_param.isdigit():
                cursor.execute(
                    """
                    SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list
                    FROM m_maintainer_section s
                    WHERE s.sec_id > 0 AND (s.sec_id = %s OR s.name = %s)
                    ORDER BY s.sec_id DESC
                    LIMIT 1;
                    """,
                    (int(sec_param), sec_param),
                )
            else:
                cursor.execute(
                    f"""
                    SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list
                    FROM m_maintainer_section s
                    WHERE s.name = %s AND {in_secs_sql}
                    ORDER BY s.sec_id DESC
                    LIMIT 1;
                    """,
                    (sec_param, *sec_params),
                )
            sec_row = cursor.fetchone()
            if not sec_row:
                raise HTTPException(status_code=404, detail=f"Subsystem section '{section_name}' not found.")

            sec_id = sec_row["sec_id"]

            # Members
            cursor.execute(
                """
                SELECT p.person_id, p.name, p.email, m.role_type,
                       EXISTS(SELECT 1 FROM m_credits_entry c WHERE c.person_id = p.person_id) AS in_credits
                FROM m_maintainer_member m
                JOIN m_maintainer_person p ON m.person_id = p.person_id
                WHERE m.sec_id = %s
                ORDER BY m.priority ASC;
                """,
                (sec_id,),
            )
            members = [
                {
                    "person_id": m["person_id"],
                    "name": safe_decode(m["name"]),
                    "email": safe_decode(m["email"]),
                    "role_type": m["role_type"],
                    "role": "Maintainer" if m["role_type"] == 1 else "Reviewer",
                    "role_name": "Maintainer" if m["role_type"] == 1 else "Reviewer",
                    "in_credits": bool(m["in_credits"]),
                }
                for m in cursor.fetchall()
            ]

            # Patterns
            cursor.execute(
                """
                SELECT pat_type, pattern, priority
                FROM m_maintainer_pattern
                WHERE sec_id = %s
                ORDER BY priority ASC;
                """,
                (sec_id,),
            )
            patterns = [
                {
                    "pattern_type": safe_decode(r["pat_type"]),
                    "pattern": safe_decode(r["pattern"]),
                    "priority": r["priority"],
                }
                for r in cursor.fetchall()
            ]

            # Touched / Maintained Files sample
            cursor.execute(
                """
                SELECT DISTINCT fn.fname
                FROM m_maintainer_file mf
                JOIN m_file_name fn ON mf.fnid = fn.fnid
                WHERE mf.sec_id = %s
                ORDER BY fn.fname ASC
                LIMIT 500;
                """,
                (sec_id,),
            )
            files = [{"fname": safe_decode(r["fname"])} for r in cursor.fetchall()]

            sec_dict = {
                "sec_id": sec_id,
                "name": safe_decode(sec_row["name"]),
                "status": safe_decode(sec_row["status"]),
                "scm_tree": safe_decode(sec_row["scm_tree"]),
                "web_page": safe_decode(sec_row["web_page"]),
                "mailing_list": safe_decode(sec_row["mailing_list"]),
                "members": members,
                "patterns": patterns,
                "files": files,
                "file_count": len(files),
            }
            return {
                "version": vname,
                "section": sec_dict,
                **sec_dict,
            }

    def get_person_profile(self, version_name: str, person_id_or_email: int | str) -> dict[str, Any]:
        """Fetch developer profile, maintained sections, and CREDITS biographical data."""
        import urllib.parse
        clean_id = urllib.parse.unquote(str(person_id_or_email)).strip()
        if "%" in clean_id:
            clean_id = urllib.parse.unquote(clean_id).strip()
        stripped_id = clean_id.strip("<>'\" \t\r\n")

        with get_db_cursor() as cursor:
            if stripped_id.isdigit():
                cursor.execute(
                    "SELECT person_id, name, email FROM m_maintainer_person WHERE person_id = %s LIMIT 1;",
                    (int(stripped_id),),
                )
            else:
                cursor.execute(
                    "SELECT person_id, name, email FROM m_maintainer_person WHERE LOWER(email) = LOWER(%s) OR LOWER(name) = LOWER(%s) OR LOWER(email) = LOWER(%s) ORDER BY (name = '') ASC, person_id ASC LIMIT 1;",
                    (clean_id, clean_id, stripped_id),
                )
            person = cursor.fetchone()
            if not person:
                raise HTTPException(status_code=404, detail=f"Person '{person_id_or_email}' not found.")

            person_id = person["person_id"]
            pid_to_key, key_members = self._identity_index(cursor)
            alias_ids = key_members.get(pid_to_key.get(int(person_id), ""), [int(person_id)])
            id_fmt = ",".join(["%s"] * len(alias_ids))
            cursor.execute(f"SELECT email FROM m_maintainer_person WHERE person_id IN ({id_fmt}) ORDER BY person_id;", tuple(alias_ids))
            alias_emails = [safe_decode(r["email"]) for r in cursor.fetchall()]

            vid, vname = get_version_info(cursor, version_name)
            sec_ids, credit_ids = self._version_scope(cursor, vid, vname)
            in_secs_sql, sec_params = _in_ids("s.sec_id", sec_ids)
            in_credits_sql, credit_params = _in_ids("credit_id", credit_ids)

            # Maintained sections
            cursor.execute(
                f"""
                SELECT s.sec_id, s.name, s.status, m.role_type
                FROM m_maintainer_member m
                JOIN m_maintainer_section s ON m.sec_id = s.sec_id
                WHERE m.person_id IN ({id_fmt}) AND {in_secs_sql}
                ORDER BY s.sec_id DESC, m.role_type ASC;
                """,
                (*alias_ids, *sec_params),
            )
            seen_sec_names = set()
            sections = []
            for r in cursor.fetchall():
                s_name = safe_decode(r["name"]) or ""
                norm = s_name.strip().lower()
                if norm in seen_sec_names:
                    continue
                seen_sec_names.add(norm)
                sections.append({
                    "sec_id": r["sec_id"],
                    "name": s_name,
                    "status": safe_decode(r["status"]),
                    "role_type": r["role_type"],
                    "role_name": "Maintainer" if r["role_type"] == 1 else "Reviewer",
                })
            sections.sort(key=lambda s: s["name"].lower())

            # CREDITS entry (newest revision across aliases)
            cursor.execute(
                f"""
                SELECT web_page, pgp_key, description, snail_mail
                FROM m_credits_entry
                WHERE person_id IN ({id_fmt}) AND {in_credits_sql}
                ORDER BY credit_id DESC
                LIMIT 1;
                """,
                (*alias_ids, *credit_params),
            )
            credit = cursor.fetchone()

            p_dict = {
                "person_id": person_id,
                "name": safe_decode(person["name"]),
                "email": safe_decode(person["email"]),
                "emails": _merge_emails([safe_decode(person["email"])] + alias_emails),
                "alias_person_ids": alias_ids,
            }

            in_credits = bool(credit is not None)

            # Recent commits & latest patch
            cursor.execute(
                f"""
                SELECT commit_id, commit_hash, author_date, subject
                FROM m_commit
                WHERE author_id IN ({id_fmt})
                ORDER BY author_date DESC
                LIMIT 10;
                """,
                tuple(alias_ids),
            )
            c_rows = cursor.fetchall()
            recent_commits = [
                {
                    "commit_id": r["commit_id"],
                    "commit_hash": hash_hex(r["commit_hash"]),
                    "author_date": r["author_date"],
                    "subject": safe_decode(r["subject"]),
                }
                for r in c_rows
            ]

            # Contribution stats across roles in m_bridge_commit_person
            cursor.execute(
                f"""
                SELECT
                    COUNT(DISTINCT CASE WHEN role_type = 1 THEN commit_id END) AS authored,
                    COUNT(DISTINCT CASE WHEN role_type = 4 THEN commit_id END) AS signed_off,
                    COUNT(DISTINCT CASE WHEN role_type = 5 THEN commit_id END) AS reviewed
                FROM m_bridge_commit_person
                WHERE person_id IN ({id_fmt});
                """,
                tuple(alias_ids),
            )
            b_stats = cursor.fetchone() or {}
            authored_cnt = int(b_stats.get("authored") or 0)
            if not authored_cnt and recent_commits:
                cursor.execute(f"SELECT COUNT(*) AS cnt FROM m_commit WHERE author_id IN ({id_fmt});", tuple(alias_ids))
                cnt_row = cursor.fetchone()
                authored_cnt = int(cnt_row["cnt"]) if cnt_row else 0

            contribution_stats = {
                "authored_commits": authored_cnt,
                "signed_off_commits": int(b_stats.get("signed_off") or 0),
                "reviewed_commits": int(b_stats.get("reviewed") or 0),
            }

            return {
                "person": p_dict,
                "person_id": person_id,
                "name": p_dict["name"],
                "email": p_dict["email"],
                "emails": p_dict["emails"],
                "alias_person_ids": alias_ids,
                "in_credits": in_credits,
                "sections": sections,
                "subsystems": sections,
                "subsystems_count": len(sections),
                "credits": {
                    "web_page": safe_decode(credit["web_page"]) if credit else None,
                    "pgp_key": safe_decode(credit["pgp_key"]) if credit else None,
                    "description": safe_decode(credit["description"]) if credit else None,
                    "snail_mail": safe_decode(credit["snail_mail"]) if credit else None,
                } if credit else None,
                "contribution_stats": contribution_stats,
                "recent_commits": recent_commits,
            }

    def get_credits(self, version_name: str, query: str = "") -> dict[str, Any]:
        """List historical CREDITS entries."""
        target_q = query
        clean_q = sanitize_like_query(target_q)
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            _, credit_ids = self._version_scope(cursor, vid, vname)
            in_credits_sql, params = _in_ids("c.credit_id", credit_ids)

            sql = f"""
                SELECT c.credit_id, p.person_id, p.name, p.email, c.web_page, c.pgp_key, c.description, c.snail_mail
                FROM m_credits_entry c
                JOIN m_maintainer_person p ON c.person_id = p.person_id
                WHERE {in_credits_sql}
            """
            if clean_q:
                sql += " AND (LOWER(p.name) LIKE LOWER(%s) OR LOWER(p.email) LIKE LOWER(%s) OR LOWER(c.description) LIKE LOWER(%s))"
                params.extend([f"%{clean_q}%", f"%{clean_q}%", f"%{clean_q}%"])
            sql += " ORDER BY c.credit_id DESC;"

            cursor.execute(sql, tuple(params))
            # Rows are newest-first: older revisions of an entry (superseded CREDITS blobs) and the
            # same developer under other alias personas collapse into the newest entry.
            by_identity: dict[str, dict[str, Any]] = {}
            for r in cursor.fetchall():
                pid = r["person_id"]
                key = _identity_key(safe_decode(r["name"]), safe_decode(r["email"]))
                existing = by_identity.get(key)
                if existing is not None:
                    existing["emails"] = _merge_emails(existing["emails"] + [safe_decode(r["email"])])
                    if pid not in existing["alias_person_ids"]:
                        existing["alias_person_ids"].append(pid)
                    for fld in ("web_page", "pgp_key", "description", "snail_mail"):
                        if not existing[fld]:
                            existing[fld] = safe_decode(r[fld])
                    continue
                by_identity[key] = {
                    "credit_id": r["credit_id"],
                    "person_id": pid,
                    "name": _clean_name(safe_decode(r["name"])),
                    "email": safe_decode(r["email"]),
                    "web_page": safe_decode(r["web_page"]),
                    "pgp_key": safe_decode(r["pgp_key"]),
                    "description": safe_decode(r["description"]),
                    "snail_mail": safe_decode(r["snail_mail"]),
                    "emails": _merge_emails([safe_decode(r["email"])]),
                    "alias_person_ids": [pid],
                }

            entries = list(by_identity.values())
            for e in entries:
                # Newest CREDITS revision may have lost the address; fall back to the merged list
                if not (e["email"] or "").strip() and e["emails"]:
                    e["email"] = ", ".join(e["emails"])
                e["alias_person_ids"].sort()
            entries.sort(key=lambda e: (e["name"] or "").lower())
            return {
                "version": vname,
                "total": len(entries),
                "entries": entries,
                "credits": entries,
            }

    def get_maintainer_data(self, version_name: str) -> tuple[list[MaintainerSection], MaintainerMatcher]:
        """Retrieve or parse MaintainerSection items and MaintainerMatcher for a version."""
        # .get() into a local: the cache evicts, so contains-then-get could race a miss
        cached = _MAINTAINER_CACHE.get(version_name)
        if cached is not None:
            return cached

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            sec_ids, _ = self._version_scope(cursor, vid, vname)
            in_secs_sql, sec_params = _in_ids("s.sec_id", sec_ids)

            cursor.execute(
                f"""
                SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list
                FROM m_maintainer_section s
                WHERE {in_secs_sql}
                ORDER BY s.sec_id DESC;
                """,
                tuple(sec_params),
            )
            sec_rows = cursor.fetchall()
            sec_map: dict[int, MaintainerSection] = {}
            seen_sec_names: set[str] = set()

            for r in sec_rows:
                sec_id = r["sec_id"]
                sec_name = safe_decode(r["name"]) or ""
                sec_name_key = sec_name.strip().lower()
                if sec_name_key in seen_sec_names:
                    continue
                seen_sec_names.add(sec_name_key)

                sec = MaintainerSection(
                    name=sec_name,
                    status=safe_decode(r["status"]) or "Maintained",
                    scm_tree=safe_decode(r["scm_tree"]) or "",
                    web_page=safe_decode(r["web_page"]) or "",
                    mailing_list=safe_decode(r["mailing_list"]) or "",
                )
                setattr(sec, "sec_id", sec_id)
                sec_map[sec_id] = sec

            # Members
            if sec_map:
                id_placeholders = ",".join(["%s"] * len(sec_map))
                cursor.execute(
                    f"""
                    SELECT m.sec_id, p.person_id, p.name, p.email, m.role_type, m.priority
                    FROM m_maintainer_member m
                    JOIN m_maintainer_person p ON m.person_id = p.person_id
                    WHERE m.sec_id IN ({id_placeholders})
                    ORDER BY m.priority ASC;
                    """,
                    tuple(sec_map.keys()),
                )
                for mr in cursor.fetchall():
                    s_id = mr["sec_id"]
                    if s_id in sec_map:
                        person = MaintainerPerson(
                            name=safe_decode(mr["name"]) or "",
                            email=safe_decode(mr["email"]) or "",
                            role=MaintainerRole(mr["role_type"]) if mr["role_type"] in (1, 2, 3, 4) else MaintainerRole.MAINTAINER,
                        )
                        setattr(person, "person_id", mr["person_id"])
                        sec_map[s_id].members.append(person)

                # Patterns
                cursor.execute(
                    f"""
                    SELECT pat.sec_id, pat.pat_type, pat.pattern, pat.priority
                    FROM m_maintainer_pattern pat
                    WHERE pat.sec_id IN ({id_placeholders})
                    ORDER BY pat.priority ASC;
                    """,
                    tuple(sec_map.keys()),
                )
                for pr in cursor.fetchall():
                    s_id = pr["sec_id"]
                    if s_id in sec_map:
                        rule = PatternRule(
                            pat_type=PatternType(pr["pat_type"]) if pr["pat_type"] in (1, 2, 3, 4) else PatternType.FILE,
                            pattern=safe_decode(pr["pattern"]) or "",
                        )
                        sec_map[s_id].patterns.append(rule)

        sections = list(sec_map.values())
        matcher = MaintainerMatcher(sections)
        _MAINTAINER_CACHE[version_name] = (sections, matcher)
        return sections, matcher

    def _credits_keys(self, version_name: str) -> tuple[set[str], set[str]]:
        """Lower-cased emails and names of the version's CREDITS entries, read once per version."""
        keys = _CREDITS_KEYS_CACHE.get(version_name)
        if keys is None:
            entries = self.get_credits(version_name).get("entries", [])
            keys = (
                {e["email"].lower() for e in entries if e["email"]},
                {e["name"].lower() for e in entries if e["name"]},
            )
            _CREDITS_KEYS_CACHE[version_name] = keys
        return keys

    def resolve_subsystems_for_file(self, version_name: str, file_path: str) -> list[dict[str, Any]]:
        """Resolve all subsystems, maintainers, and reviewers governing a file path."""
        if not file_path:
            return []
        cache_key = (version_name, file_path)
        cached = _FILE_SUBSYSTEMS_CACHE.get(cache_key)
        if cached is not None:
            return cached

        sections, matcher = self.get_maintainer_data(version_name)
        matched_secs = matcher.match_file(file_path)

        credits_emails, credits_names = self._credits_keys(version_name)

        results = []
        for sec in matched_secs:
            sec_id = getattr(sec, "sec_id", None)
            maintainers = []
            for m in sec.get_maintainers():
                in_cred = (m.email.lower() in credits_emails) if m.email else (m.name.lower() in credits_names)
                maintainers.append({
                    "person_id": getattr(m, "person_id", None),
                    "name": m.name,
                    "email": m.email,
                    "role": "Maintainer",
                    "role_name": "Maintainer",
                    "in_credits": in_cred,
                })

            reviewers = []
            for r in sec.get_reviewers():
                in_cred = (r.email.lower() in credits_emails) if r.email else (r.name.lower() in credits_names)
                reviewers.append({
                    "person_id": getattr(r, "person_id", None),
                    "name": r.name,
                    "email": r.email,
                    "role": "Reviewer",
                    "role_name": "Reviewer",
                    "in_credits": in_cred,
                })

            results.append({
                "sec_id": sec_id,
                "name": sec.name,
                "status": sec.status,
                "scm_tree": sec.scm_tree,
                "web_page": sec.web_page,
                "mailing_list": sec.mailing_list,
                "maintainers": maintainers,
                "reviewers": reviewers,
            })

        _FILE_SUBSYSTEMS_CACHE[cache_key] = results
        return results

    def match_maintainers(self, version_name: str, patch_text: str = "", paths: list[str] | None = None) -> dict[str, Any]:
        """get_maintainer.pl recipients of a unified diff and/or explicit file paths at a version."""
        vname = version_name
        candidate_paths: list[str] = list(paths or [])
        patch_text = patch_text or ""

        lines = patch_text.splitlines() if patch_text else []
        touched_files: dict[str, None] = {}  # insertion-ordered set of the files the patch changes
        curr_file = None
        for line in lines:
            if line.startswith("diff --git a/"):
                parts = line.split(" ")
                if len(parts) >= 4 and parts[3].startswith("b/"):
                    curr_file = parts[3][2:].strip()
            elif line.startswith("+++ b/"):
                curr_file = line[6:].strip()
            elif line.startswith("--- a/") and not curr_file:
                curr_file = line[6:].strip()
            elif line.startswith("+") and not line.startswith("+++"):
                if curr_file:
                    touched_files[curr_file] = None
            elif line.startswith("-") and not line.startswith("---"):
                if curr_file:
                    touched_files[curr_file] = None

        if not touched_files and curr_file:
            touched_files[curr_file] = None

        for p in candidate_paths:
            if p and p not in touched_files:
                touched_files[p] = None

        # Every file is matched against every MAINTAINERS pattern, so the work is linear in the
        # number of files the patch names and is paid even when nothing matches (2,000 files took
        # ~14s before this cap). The rest are counted and reported instead of matched.
        skipped_files = max(0, len(touched_files) - MAX_PATCH_FILES)
        if skipped_files:
            touched_files = dict(list(touched_files.items())[:MAX_PATCH_FILES])

        all_maintainers: dict[str, dict[str, Any]] = {}
        all_reviewers: dict[str, dict[str, Any]] = {}
        all_lists: set[str] = set()
        all_subsystems: set[str] = set()

        file_breakdown = []
        for fpath in touched_files:
            subs = self.resolve_subsystems_for_file(vname, fpath)
            file_breakdown.append({
                "file_path": fpath,
                "subsystems": subs,
            })
            for s in subs:
                all_subsystems.add(s["name"])
                if s.get("mailing_list"):
                    all_lists.add(s["mailing_list"])
                for m in s.get("maintainers", []):
                    m_key = m.get("email") or m.get("name")
                    if m_key:
                        if m_key not in all_maintainers:
                            all_maintainers[m_key] = {**m, "roles": set(), "subsystems": set()}
                        all_maintainers[m_key]["roles"].add("maintainer")
                        all_maintainers[m_key]["subsystems"].add(s["name"])
                for r in s.get("reviewers", []):
                    r_key = r.get("email") or r.get("name")
                    if r_key:
                        if r_key not in all_reviewers:
                            all_reviewers[r_key] = {**r, "roles": set(), "subsystems": set()}
                        all_reviewers[r_key]["roles"].add("reviewer")
                        all_reviewers[r_key]["subsystems"].add(s["name"])

        to_list = [f"{m['name']} <{m['email']}>" if m.get('email') else m['name'] for m in all_maintainers.values()]
        cc_list = [f"{r['name']} <{r['email']}>" if r.get('email') else r['name'] for r in all_reviewers.values()]
        cc_list.extend(sorted(all_lists))

        return {
            "version": vname,
            "skipped_files_count": skipped_files,
            "files": file_breakdown,
            "subsystems": sorted(all_subsystems),
            "suggested_to": to_list,
            "suggested_cc": cc_list,
            "maintainers": [{**m, "roles": list(m["roles"]), "subsystems": list(m["subsystems"])} for m in all_maintainers.values()],
            "reviewers": [{**r, "roles": list(r["roles"]), "subsystems": list(r["subsystems"])} for r in all_reviewers.values()],
        }
