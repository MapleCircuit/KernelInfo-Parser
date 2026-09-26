"""webapp/backend/services/maintainer_service.py - Subsystems Catalog, Maintainers Roster & CREDITS."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from webapp.backend.database.pool import get_db_cursor
from webapp.backend.database.helpers import (
    safe_decode,
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

_MAINTAINER_CACHE: dict[str, tuple[list[MaintainerSection], MaintainerMatcher]] = {}
_FILE_SUBSYSTEMS_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


class MaintainerService:
    """Service for exploring subsystems, maintainer roles, CREDITS entries, and matching patches."""

    def get_overview(self, version_name: str, query: str = "", q: str = "") -> dict[str, Any]:
        """List subsystem sections and maintainers with optional query filtering."""
        target_q = query or q
        clean_q = sanitize_like_query(target_q)
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sql = """
                SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list
                FROM m_maintainer_section s
                WHERE s.vid_s <= %s AND (s.vid_e >= %s OR s.vid_e = 0)
            """
            params: list[Any] = [vid, vid]
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
            sql += " ORDER BY s.name ASC;"

            cursor.execute(sql, tuple(params))
            sections_raw = cursor.fetchall()

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

            return {"version": vname, "total_count": len(sections), "sections": sections}

    def get_developers(
        self,
        version_name: str,
        query: str = "",
        q: str = "",
        role: str = "all",
        sort: str = "activity",
    ) -> dict[str, Any]:
        """List all developers, maintainers, reviewers, and contributors recorded in the kernel persona registry."""
        target_q = query or q
        clean_q = sanitize_like_query(target_q)
        clean_role = (role or "all").strip().lower()
        clean_sort = (sort or "activity").strip().lower()

        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sql = """
                SELECT 
                    p.person_id,
                    p.name,
                    p.email,
                    COUNT(DISTINCT s.sec_id) AS subsystems_count,
                    MAX(CASE WHEN m.role_type = 1 THEN 1 ELSE 0 END) AS is_maintainer,
                    MAX(CASE WHEN m.role_type = 2 THEN 1 ELSE 0 END) AS is_reviewer,
                    EXISTS(SELECT 1 FROM m_credits_entry c WHERE c.person_id = p.person_id) AS in_credits,
                    EXISTS(SELECT 1 FROM m_commit com WHERE com.author_id = p.person_id) AS has_commits
                FROM m_maintainer_person p
                LEFT JOIN m_maintainer_member m ON p.person_id = m.person_id
                LEFT JOIN m_maintainer_section s ON m.sec_id = s.sec_id AND s.vid_s <= %s AND (s.vid_e >= %s OR s.vid_e = 0)
            """
            params: list[Any] = [vid, vid]

            where_clauses = []
            if clean_q:
                where_clauses.append("(LOWER(p.name) LIKE LOWER(%s) OR LOWER(p.email) LIKE LOWER(%s))")
                like_str = f"%{clean_q}%"
                params.extend([like_str, like_str])

            if clean_role == "maintainer":
                where_clauses.append("EXISTS (SELECT 1 FROM m_maintainer_member mem JOIN m_maintainer_section sec ON mem.sec_id = sec.sec_id WHERE mem.person_id = p.person_id AND mem.role_type = 1 AND sec.vid_s <= %s AND (sec.vid_e >= %s OR sec.vid_e = 0))")
                params.extend([vid, vid])
            elif clean_role == "reviewer":
                where_clauses.append("EXISTS (SELECT 1 FROM m_maintainer_member mem JOIN m_maintainer_section sec ON mem.sec_id = sec.sec_id WHERE mem.person_id = p.person_id AND mem.role_type = 2 AND sec.vid_s <= %s AND (sec.vid_e >= %s OR sec.vid_e = 0))")
                params.extend([vid, vid])
            elif clean_role == "credits":
                where_clauses.append("EXISTS (SELECT 1 FROM m_credits_entry c WHERE c.person_id = p.person_id)")

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)

            sql += " GROUP BY p.person_id, p.name, p.email"

            if clean_sort == "alpha":
                sql += " ORDER BY p.name ASC, subsystems_count DESC;"
            else:
                sql += " ORDER BY subsystems_count DESC, p.name ASC;"

            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()

            developers = []
            for r in rows:
                p_name = safe_decode(r["name"])
                p_email = safe_decode(r["email"])
                sub_count = int(r["subsystems_count"] or 0)
                is_m = bool(r["is_maintainer"])
                is_r = bool(r["is_reviewer"])
                in_cred = bool(r["in_credits"])
                has_c = bool(r["has_commits"])

                if is_m:
                    primary_role = "Maintainer"
                elif is_r:
                    primary_role = "Reviewer"
                elif in_cred:
                    primary_role = "Credits"
                elif has_c:
                    primary_role = "Author"
                else:
                    primary_role = "Developer"

                developers.append({
                    "person_id": r["person_id"],
                    "name": p_name,
                    "email": p_email,
                    "subsystems_count": sub_count,
                    "is_maintainer": is_m,
                    "is_reviewer": is_r,
                    "in_credits": in_cred,
                    "has_commits": has_c,
                    "primary_role": primary_role,
                })

            return {
                "version": vname,
                "total_count": len(developers),
                "role_filter": clean_role,
                "sort": clean_sort,
                "developers": developers,
            }

    def get_section_detail(self, version_name: str, section_name: str) -> dict[str, Any]:
        """Retrieve subsystem details, pattern rules, and file matches."""
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            if vid is None:
                raise HTTPException(status_code=404, detail=f"Version '{version_name}' not found.")

            sec_param = str(section_name).strip()
            if sec_param.isdigit():
                cursor.execute(
                    """
                    SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list
                    FROM m_maintainer_section s
                    WHERE s.vid_s <= %s AND (s.vid_e >= %s OR s.vid_e = 0) AND (s.sec_id = %s OR s.name = %s)
                    LIMIT 1;
                    """,
                    (vid, vid, int(sec_param), sec_param),
                )
            else:
                cursor.execute(
                    """
                    SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list
                    FROM m_maintainer_section s
                    WHERE s.vid_s <= %s AND (s.vid_e >= %s OR s.vid_e = 0) AND s.name = %s
                    LIMIT 1;
                    """,
                    (vid, vid, sec_param),
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
                JOIN m_bridge_file bf ON mf.fid = bf.fid AND bf.vid = mf.vid
                JOIN m_file_name fn ON bf.fnid = fn.fnid
                WHERE mf.vid = %s AND mf.sec_id = %s
                ORDER BY fn.fname ASC
                LIMIT 500;
                """,
                (vid, sec_id),
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
                    "SELECT person_id, name, email FROM m_maintainer_person WHERE LOWER(email) = LOWER(%s) OR LOWER(name) = LOWER(%s) OR LOWER(email) = LOWER(%s) LIMIT 1;",
                    (clean_id, clean_id, stripped_id),
                )
            person = cursor.fetchone()
            if not person:
                raise HTTPException(status_code=404, detail=f"Person '{person_id_or_email}' not found.")

            person_id = person["person_id"]

            # Maintained sections
            cursor.execute(
                """
                SELECT s.sec_id, s.name, s.status, m.role_type
                FROM m_maintainer_member m
                JOIN m_maintainer_section s ON m.sec_id = s.sec_id
                WHERE m.person_id = %s
                ORDER BY s.name ASC;
                """,
                (person_id,),
            )
            sections = [
                {
                    "sec_id": r["sec_id"],
                    "name": safe_decode(r["name"]),
                    "status": safe_decode(r["status"]),
                    "role_type": r["role_type"],
                    "role_name": "Maintainer" if r["role_type"] == 1 else "Reviewer",
                }
                for r in cursor.fetchall()
            ]

            # CREDITS entry
            cursor.execute(
                """
                SELECT web_page, pgp_key, description, snail_mail
                FROM m_credits_entry
                WHERE person_id = %s
                LIMIT 1;
                """,
                (person_id,),
            )
            credit = cursor.fetchone()

            p_dict = {
                "person_id": person_id,
                "name": safe_decode(person["name"]),
                "email": safe_decode(person["email"]),
            }

            in_credits = bool(credit is not None)

            # Recent commits & latest patch
            cursor.execute(
                """
                SELECT commit_id, commit_hash, author_date, subject
                FROM m_commit
                WHERE author_id = %s
                ORDER BY author_date DESC
                LIMIT 10;
                """,
                (person_id,),
            )
            c_rows = cursor.fetchall()
            recent_commits = [
                {
                    "commit_id": r["commit_id"],
                    "commit_hash": safe_decode(r["commit_hash"]),
                    "author_date": r["author_date"],
                    "subject": safe_decode(r["subject"]),
                }
                for r in c_rows
            ]

            latest_patch = None
            if c_rows:
                lp_row = c_rows[0]
                adate = lp_row["author_date"]
                dt_iso = datetime.fromtimestamp(adate, tz=timezone.utc).isoformat() if adate else ""
                latest_patch = {
                    "commit_id": lp_row["commit_id"],
                    "commit_hash": safe_decode(lp_row["commit_hash"]),
                    "subject": safe_decode(lp_row["subject"]),
                    "author_date": adate,
                    "author_date_iso": dt_iso,
                }

            # Contribution stats across roles in m_bridge_commit_person
            cursor.execute(
                """
                SELECT
                    SUM(CASE WHEN role_type = 1 THEN 1 ELSE 0 END) AS authored,
                    SUM(CASE WHEN role_type = 3 THEN 1 ELSE 0 END) AS co_developed,
                    SUM(CASE WHEN role_type = 4 THEN 1 ELSE 0 END) AS signed_off,
                    SUM(CASE WHEN role_type = 5 THEN 1 ELSE 0 END) AS reviewed,
                    SUM(CASE WHEN role_type = 6 THEN 1 ELSE 0 END) AS acked,
                    SUM(CASE WHEN role_type = 7 THEN 1 ELSE 0 END) AS tested,
                    SUM(CASE WHEN role_type = 8 THEN 1 ELSE 0 END) AS reported
                FROM m_bridge_commit_person
                WHERE person_id = %s;
                """,
                (person_id,),
            )
            b_stats = cursor.fetchone() or {}
            authored_cnt = int(b_stats.get("authored") or 0)
            if not authored_cnt and recent_commits:
                cursor.execute("SELECT COUNT(*) AS cnt FROM m_commit WHERE author_id = %s;", (person_id,))
                cnt_row = cursor.fetchone()
                authored_cnt = int(cnt_row["cnt"]) if cnt_row else 0

            contribution_stats = {
                "authored_commits": authored_cnt,
                "co_developed_commits": int(b_stats.get("co_developed") or 0),
                "signed_off_commits": int(b_stats.get("signed_off") or 0),
                "reviewed_commits": int(b_stats.get("reviewed") or 0),
                "acked_commits": int(b_stats.get("acked") or 0),
                "tested_commits": int(b_stats.get("tested") or 0),
                "reported_commits": int(b_stats.get("reported") or 0),
            }

            return {
                "person": p_dict,
                "person_id": person_id,
                "name": p_dict["name"],
                "email": p_dict["email"],
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
                "latest_patch": latest_patch,
                "contribution_stats": contribution_stats,
                "recent_commits": recent_commits,
            }

    def get_credits(self, version_name: str, query: str = "", q: str = "") -> dict[str, Any]:
        """List historical CREDITS entries."""
        target_q = query or q
        clean_q = sanitize_like_query(target_q)
        with get_db_cursor() as cursor:
            vid, vname = get_version_info(cursor, version_name)
            sql = """
                SELECT c.credit_id, p.person_id, p.name, p.email, c.web_page, c.pgp_key, c.description, c.snail_mail
                FROM m_credits_entry c
                JOIN m_maintainer_person p ON c.person_id = p.person_id
            """
            params: list[Any] = []
            if clean_q:
                sql += " WHERE LOWER(p.name) LIKE LOWER(%s) OR LOWER(p.email) LIKE LOWER(%s) OR LOWER(c.description) LIKE LOWER(%s)"
                params.extend([f"%{clean_q}%", f"%{clean_q}%", f"%{clean_q}%"])
            sql += " ORDER BY p.name ASC LIMIT 500;"

            cursor.execute(sql, tuple(params))
            entries = [
                {
                    "credit_id": r["credit_id"],
                    "person_id": r["person_id"],
                    "name": safe_decode(r["name"]),
                    "email": safe_decode(r["email"]),
                    "web_page": safe_decode(r["web_page"]),
                    "pgp_key": safe_decode(r["pgp_key"]),
                    "description": safe_decode(r["description"]),
                    "snail_mail": safe_decode(r["snail_mail"]),
                }
                for r in cursor.fetchall()
            ]
            return {
                "version": vname,
                "total": len(entries),
                "total_count": len(entries),
                "entries": entries,
                "credits": entries,
            }

    def get_maintainer_data(self, version_name: str) -> tuple[list[MaintainerSection], MaintainerMatcher]:
        """Retrieve or parse MaintainerSection items and MaintainerMatcher for a version."""
        if version_name in _MAINTAINER_CACHE:
            return _MAINTAINER_CACHE[version_name]

        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT s.sec_id, s.name, s.status, s.scm_tree, s.web_page, s.mailing_list, s.vid_s
                FROM m_maintainer_section s
                JOIN m_v_main v ON (s.vid_s <= v.vid AND (s.vid_e = 0 OR s.vid_e >= v.vid))
                WHERE v.vname = %s
                ORDER BY s.vid_s DESC, s.sec_id ASC;
                """,
                (version_name,),
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

    def resolve_subsystems_for_file(self, version_name: str, file_path: str) -> list[dict[str, Any]]:
        """Resolve all subsystems, maintainers, and reviewers governing a file path."""
        if not file_path:
            return []
        cache_key = (version_name, file_path)
        if cache_key in _FILE_SUBSYSTEMS_CACHE:
            return _FILE_SUBSYSTEMS_CACHE[cache_key]

        sections, matcher = self.get_maintainer_data(version_name)
        matched_secs = matcher.match_file(file_path)

        credits_res = self.get_credits(version_name)
        credits_entries = credits_res.get("entries", [])
        credits_emails = {e["email"].lower() for e in credits_entries if e["email"]}
        credits_names = {e["name"].lower() for e in credits_entries if e["name"]}

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
                "all_members": maintainers + reviewers,
            })

        _FILE_SUBSYSTEMS_CACHE[cache_key] = results
        return results

    def match_maintainers(
        self,
        target: str | list[str],
        req_or_patch: Any = None,
        version_name: str = "v3.0",
    ) -> dict[str, Any]:
        """Analyze unified diff patch text or paths and generate exact get_maintainer.pl recipient rosters."""
        if isinstance(target, str) and not (target.endswith(".c") or target.endswith(".h") or "/" in target):
            vname = target
            patch_input = req_or_patch
        elif isinstance(target, list):
            vname = version_name
            patch_input = req_or_patch
        else:
            vname = version_name
            patch_input = target

        patch_text = ""
        if hasattr(patch_input, "patch_text"):
            patch_text = patch_input.patch_text or ""
        elif isinstance(patch_input, dict):
            patch_text = patch_input.get("patch_text") or patch_input.get("patch", "")
        elif isinstance(patch_input, str):
            patch_text = patch_input

        lines = patch_text.splitlines() if patch_text else []
        touched_files: dict[str, int] = {}
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
                    touched_files[curr_file] = touched_files.get(curr_file, 0) + 1
            elif line.startswith("-") and not line.startswith("---"):
                if curr_file:
                    touched_files[curr_file] = touched_files.get(curr_file, 0) + 1

        if not touched_files and curr_file:
            touched_files[curr_file] = 1

        if not touched_files and isinstance(target, list):
            for p in target:
                touched_files[p] = 1

        all_maintainers: dict[str, dict[str, Any]] = {}
        all_reviewers: dict[str, dict[str, Any]] = {}
        all_lists: set[str] = set()
        all_subsystems: set[str] = set()

        file_breakdown = []
        for fpath, mod_lines in touched_files.items():
            subs = self.resolve_subsystems_for_file(vname, fpath)
            file_breakdown.append({
                "file_path": fpath,
                "modified_lines": mod_lines,
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
            "touched_files_count": len(touched_files),
            "files": file_breakdown,
            "subsystems": sorted(all_subsystems),
            "suggested_to": to_list,
            "suggested_cc": cc_list,
            "maintainers": [{**m, "roles": list(m["roles"]), "subsystems": list(m["subsystems"])} for m in all_maintainers.values()],
            "reviewers": [{**r, "roles": list(r["roles"]), "subsystems": list(r["subsystems"])} for r in all_reviewers.values()],
            "mailing_lists": sorted(all_lists),
        }


maintainer_service = MaintainerService()

